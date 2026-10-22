# -*- coding: utf-8 -*-
"""ASR 封装：faster-whisper，绕过 av 依赖。

为什么要绕过 av：
  faster_whisper.audio 顶层 `import av`，而 av 只被 decode_audio() 用来解容器。
  我们自己用 ffmpeg -> wav -> wave -> numpy 数组喂给 transcribe()，根本不走 decode_audio。
  因此给 sys.modules 注入一个空 av 模块即可，省掉 av 这个重依赖（打包体积小很多，
  也避开了 av 在多 Python 版本上 wheel 缺失的坑）。

输出统一为 [(start_sec, end_sec, text), ...]，供 resegment 消费。
"""
import os
import sys
import types
import wave
import subprocess

# ---- 必须在 import faster_whisper 之前注入 av 占位 ----
if "av" not in sys.modules:
    _av_stub = types.ModuleType("av")
    _av_stub.__getattr__ = lambda name: _av_stub
    _av_stub.__version__ = "0.0.0-stub"
    sys.modules["av"] = _av_stub


# 字幕无标点（2026-10-22 用户拍板，对齐剪映原生效果）：
# 实测结论——不要在这里做，统一在 textpost.clean() 后处理剔除。原因：
#   * initial_prompt 引导：标点 19 -> 9 个，不稳，且挤占解码上下文错字变多
#   * suppress_tokens 抑制标点 token：普通管线 0 标点，但批量管线
#     （BatchedInferencePipeline，产品默认 1.68x 提速路径）下模型失去标点
#     锚点会退化成"……"洪流 + 复读机幻觉（实测 60s 片段 560 个标点乱入）
#   * word_timestamps 的 prepend/append_punctuations 只管标点挂到哪个词上，
#     管不住生成
def _ffmpeg_works(path):
    """验证 ffmpeg 可真正执行（scoop 等 shim 会报错，需排除）。"""
    try:
        p = subprocess.run([path, "-version"], capture_output=True, timeout=15,
                           encoding="utf-8", errors="ignore")
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode == 0 and "ffmpeg version" in out.lower()
    except Exception:
        return False


def find_ffmpeg():
    """定位可用的 ffmpeg。

    注意：PATH 里可能有 scoop 之类的 shim（"Could not create process"），
    因此每个候选都要实跑验证，不可用就跳过。
    """
    here = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, "frozen", False) else __file__))
    meipass = getattr(sys, "_MEIPASS", "")

    cands = [
        # 1) 程序自带（打包时放同级）
        os.path.join(here, "ffmpeg.exe"),
        os.path.join(here, "bin", "ffmpeg.exe"),
        os.path.join(meipass, "ffmpeg.exe") if meipass else None,
        os.path.join(meipass, "bin", "ffmpeg.exe") if meipass else None,
        # 2) PATH
        shutil_which("ffmpeg"),
        # 3) 本机常见安装位置
        r"C:/Users/DELL/tools/ffmpeg/bin/ffmpeg.exe",
        r"C:/ffmpeg/bin/ffmpeg.exe",
        r"D:/ffmpeg/bin/ffmpeg.exe",
        r"C:/Program Files/ffmpeg/bin/ffmpeg.exe",
    ]
    for c in cands:
        if c and os.path.isfile(c) and _ffmpeg_works(c):
            return c
    return None


def shutil_which(name):
    from shutil import which
    try:
        return which(name)
    except Exception:
        return None


def ffprobe_duration(ffmpeg, media):
    """用 ffmpeg 探测时长（不依赖 ffprobe，避免多带一个二进制）。"""
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-i", media],
                           capture_output=True, text=True, encoding="utf-8", errors="ignore")
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def to_wav16k(ffmpeg, media, out_wav, on_progress=None):
    """任意音视频 -> 16k 单声道 PCM16 wav。"""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", media,
           "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", out_wav, "-y"]
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore")
    if p.returncode != 0 or not os.path.isfile(out_wav):
        raise RuntimeError(f"音频解码失败: {p.stderr[-500:]}")
    return out_wav


def load_wav_array(wav_path):
    """读取 16k 单声道 wav -> float32 numpy 数组。"""
    import numpy as np
    with wave.open(wav_path, "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            raise ValueError(f"需要 16kHz 单声道，实际 {w.getframerate()}Hz/{w.getnchannels()}ch")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def default_threads():
    """推理线程数：取物理核数（逻辑核数 / 2），可用环境变量强制指定。

    为什么不能直接用 os.cpu_count()：它返回的是**逻辑线程数**。
    i7-6600U 是 2 核 4 线程，os.cpu_count()==4，但 CTranslate2 的 int8
    矩阵运算属于计算密集型，4 个线程挤在 2 个物理核上会争抢同一组执行单元，
    实测比 2 线程更慢（超线程只对访存密集负载有帮助）。
    """
    env = os.environ.get("JYSUB_CPU_THREADS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    logical = os.cpu_count() or 4
    return max(1, logical // 2)


class Transcriber:
    """包一层 faster-whisper，懒加载模型。"""

    def __init__(self, model_path, cpu_threads=None):
        # cpu_threads 默认取「物理核数」，不是逻辑线程数。
        # 踩坑：os.cpu_count() 返回逻辑线程数（i7-6600U 是 2C4T 返回 4），
        # CTranslate2 的 int8 GEMM 是计算密集型，2 个物理核上开 4 线程会让
        # 超线程互相争抢执行单元，实测反而更慢。超线程一般按 2 倍逻辑核推算。
        self.model_path = model_path
        self.cpu_threads = cpu_threads or default_threads()
        self._model = None
        self._batched = None

    def load(self):
        from faster_whisper import WhisperModel
        if self._model is None:
            self._model = WhisperModel(
                self.model_path, device="cpu", compute_type="int8",
                cpu_threads=self.cpu_threads)
        return self._model

    def load_batched(self):
        """批量推理管线：把 VAD 切出的多段拼成一个 batch 送进 encoder。

        为什么值得用（实测 240s 片段，base 模型，i7-6600U 2 核）：
          普通 transcribe 39.4s（RTF 0.164）-> 批量 b=8 23.5s（RTF 0.098）
          1.68x 加速，而且 word_timestamps 完全保留（885 个字级单元），
          意味着断句算法一行都不用改。

        实测排除项，别再试：
          * batch_size=16 -> 25.2s，比 8 慢（2 核吃不下这么大的 batch）
          * chunk_length=15 -> 37.2s，切太碎反而慢
          * hotwords -> 没修好错字，还新引入错误（形成一条 -> 有此形成立条）
        """
        if self._batched is None:
            from faster_whisper import BatchedInferencePipeline
            self._batched = BatchedInferencePipeline(model=self.load())
        return self._batched

    def transcribe(self, audio_array, language="zh", on_segment=None, initial_prompt=None,
                   word_timestamps=False, return_units=False, batch_size=8):
        """转写。

        word_timestamps=True 时同时拿到**字级时间戳**，这是分段质量的关键：
          * 实测开销几乎为零（60s 片段 13.5s vs 13.8s，base 模型本来就走 DTW 对齐）
          * 中文会逐字给出 start/end，字与字之间的空隙就是真实停顿
          * 有了它就不需要 ffmpeg 静音检测，也不依赖标点断句

        return_units=True 时返回 (segs, units)：
          segs  = [(start, end, text)]  文本已繁转简 + 纠错
          units = [(word, start, end)]  字级单元，供按停顿断句使用
        on_segment(text, start, end) 每出一段回调一次（用于流式显示）。
        batch_size>1 时走批量推理管线（快 1.68x）；<=1 或加载失败退回普通模式。
        """
        import textpost
        kwargs = dict(language=language, vad_filter=True, beam_size=1,
                      vad_parameters=dict(min_silence_duration_ms=300),
                      condition_on_previous_text=False)
        if word_timestamps:
            kwargs["word_timestamps"] = True
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt
        # 字幕无标点：不在 whisper 参数层处理（实测见 textpost.py 顶部注释），
        # 由 textpost.clean() 统一剔除标点。

        segs_iter = None
        if batch_size and batch_size > 1:
            try:
                segs_iter = self.load_batched().transcribe(
                    audio_array, batch_size=batch_size, **kwargs)
            except Exception:
                segs_iter = None
        if segs_iter is None:
            segs_iter = self.load().transcribe(audio_array, **kwargs)
        segs, info = segs_iter

        out, units = [], []
        for s in segs:
            text = textpost.clean((s.text or "").strip())
            if not text:
                continue
            out.append((float(s.start), float(s.end), text))
            if on_segment:
                on_segment(text, float(s.start), float(s.end))
            if word_timestamps:
                for wd in (getattr(s, "words", None) or []):
                    w = (wd.word or "")
                    if w.strip():
                        units.append((textpost.to_simplified(w),
                                      float(wd.start), float(wd.end)))
        if return_units:
            return out, units
        return out


def transcribe_media(ffmpeg, media, model_path, tmp_wav,
                     language="zh", on_segment=None, initial_prompt=None,
                     cpu_threads=None):
    """一步到位：媒体 -> wav -> numpy -> 转写。返回 [(start, end, text)]。"""
    to_wav16k(ffmpeg, media, tmp_wav)
    audio = load_wav_array(tmp_wav)
    tr = Transcriber(model_path, cpu_threads=cpu_threads)
    return tr.transcribe(audio, language=language, on_segment=on_segment,
                         initial_prompt=initial_prompt)
