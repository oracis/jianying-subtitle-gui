# -*- coding: utf-8 -*-
"""核心流程：音频 -> ASR -> 停顿检测 -> 重分段 -> 剪映草稿。

全部环节都通过回调上报进度，供 GUI 显示。支持取消。

设计要点（耗时归因）：
  * ASR 是大头，且是唯一必须"等"的环节
  * 停顿检测 约 6-7s、重分段 <0.1s、建草稿 约 1-2s
  * 所以真正拖慢体验的只有模型档位；默认 base 在 4 核 CPU 上约 5 分钟/27分钟音频
"""
import os
import re
import subprocess
import tempfile
import time

import asr
import jianying
import resegment
import model_manager
import textpost


class Cancelled(Exception):
    pass


class Progress:
    """进度上报：stage + 百分比 + 文案。"""

    def __init__(self, cb=None, cancel_flag=None):
        self.cb = cb or (lambda *a: None)
        self.cancel_flag = cancel_flag

    def __call__(self, stage, pct, msg):
        self.cb(stage, pct, msg)
        if self.cancel_flag and self.cancel_flag():
            raise Cancelled("用户已取消")


def detect_silences(ffmpeg, media, out_path, noise_db=-32, min_dur=0.18):
    """ffmpeg silencedetect -> 静音区间文件。返回停顿数。"""
    p = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", media, "-af",
         f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore")
    n = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for line in p.stderr.splitlines():
            if "silence_start" in line or "silence_end" in line:
                f.write(line.strip() + "\n")
                n += 1
    return n // 2


def run(media_path, cfg, progress_cb=None, cancel_flag=None,
        model_dir_override=None):
    """完整流水线。

    cfg: config.load() 的字典
    progress_cb(stage, pct, msg)
    返回 dict: {draft_path, cues, duration_us, stats, timings, srt_path}
    """
    P = Progress(progress_cb, cancel_flag)
    timings = {}
    t_all = time.time()

    ffmpeg = asr.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg。请把 ffmpeg.exe 放到程序目录，或安装到 PATH。")

    if not os.path.isfile(media_path):
        raise FileNotFoundError(f"文件不存在: {media_path}")

    draft_root = cfg["draft_dir"]
    if not draft_root or not os.path.isdir(draft_root):
        raise RuntimeError(
            f"草稿目录无效: {draft_root}\n请在设置里指定剪映草稿箱目录"
            f"（通常形如 .../JianyingPro/User Data/Projects/com.lveditor.draft）")

    # ---------- 1. 模型按需下载 ----------
    size = cfg.get("model_size", "base")
    # 防御：旧配置里可能残留已下架的档位（tiny/medium/large-v3），
    # 直接拿来用会静默下载 1.5GB+ 并跑几十分钟，这里统一回退到 base。
    if size not in model_manager.MODEL_INFO:
        P("model", 0, 0, f"配置档位 {size!r} 已下架，自动回退 base")
        size = "base"
    t0 = time.time()
    if model_dir_override and model_manager.is_ready(size, None):
        model_dir = model_manager.model_dir(size)
    else:
        def mcb(stage, cur, total, msg):
            P("model", cur if total else 0, msg)
        model_dir = model_manager.ensure_model(size, progress=mcb,
                                               cancel_flag=cancel_flag)
    timings["model"] = time.time() - t0

    # ---------- 2. 解码音频 ----------
    P("decode", 0, "解码音频…")
    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="jysub_")
    wav = os.path.join(tmpdir, "audio16k.wav")
    asr.to_wav16k(ffmpeg, media_path, wav)
    timings["decode"] = time.time() - t0

    duration_us = jianying.media_duration_us(ffmpeg, media_path)
    total_sec = duration_us / 1e6 if duration_us else 0

    # ---------- 3. ASR（同时取字级时间戳） ----------
    # 先装配术语：领域包提供错字规则 + 专名，自定义 terms 再追加。
    # 专名走 initial_prompt 注入模型，从源头减少错字；
    # 错字规则只做事后兜底。两者都来自数据文件，换题材不用改代码。
    extra_fixes, terms = [], []
    for pack in [p.strip() for p in str(cfg.get("term_pack") or "").split(",") if p.strip()]:
        f, t = textpost.load_pack(pack)
        extra_fixes.extend(f)
        terms.extend(t)
        P("asr", 0, f"术语包 {pack}：{len(f)} 条纠错 / {len(t)} 个专名")
    for t in re.split(r"[,，、\s]+", str(cfg.get("terms") or "")):
        if t.strip():
            terms.append(t.strip())
    asr_prompt = textpost.build_prompt(terms, cfg.get("asr_prompt") or "")

    P("asr", 0, f"识别语音（模型 {size}，音频 {total_sec/60:.1f} 分钟）…")
    t0 = time.time()
    audio_arr = asr.load_wav_array(wav)

    # 流式回调：按已识别时长估算进度
    def on_seg(text, s, e):
        pct = int(min(99, e / total_sec * 100)) if total_sec else 0
        P("asr", pct, f"识别中 {e/60:.1f}/{total_sec/60:.1f} 分钟…")

    tr = asr.Transcriber(model_dir)
    raw, units = tr.transcribe(audio_arr, language="zh", on_segment=on_seg,
                               initial_prompt=asr_prompt or None,
                               word_timestamps=True, return_units=True)
    timings["asr"] = time.time() - t0
    if not raw:
        raise RuntimeError("语音识别没有返回任何内容，请检查音频是否有声音。")

    # ---------- 4~5. 断句 ----------
    # 主路径：字级时间戳的字间空隙（更准，且不用跑 ffmpeg 静音检测）
    # 回退：模型没给词级时间戳时，退回 ffmpeg 静音检测 + 标点规则（v2）
    t0 = time.time()
    silences = []
    n_sil = 0
    method = "words"
    if units:
        P("segment", 0, "按语音停顿断句（字级时间戳）…")
        cues = resegment.resegment_words(
            units,
            max_chars=cfg.get("max_chars", resegment.WORD_MAX_CHARS),
            pause=cfg.get("word_pause", resegment.WORD_PAUSE))
    else:
        method = "silence+punct"
        P("silence", 0, "检测语音停顿…")
        sil_path = os.path.join(tmpdir, "silences.txt")
        n_sil = detect_silences(ffmpeg, media_path, sil_path,
                                cfg.get("noise_db", -32), cfg.get("min_pause", 0.18))
        P("segment", 0, "按停顿重分段…")
        silences = resegment.parse_silences(sil_path)
        cues = resegment.resegment(raw, silences, max_chars=cfg.get("max_chars", 24))

    if not cues:
        raise RuntimeError("断句结果为空")

    # 最终文本清洗：整条做（逐字做无法纠错词组，如「科卡因」要连起来才能匹配）
    cues = [[s, e, textpost.clean(t, extra_fixes)] for s, e, t in cues]
    cues = [c for c in cues if c[2].strip()]
    if not cues:
        raise RuntimeError("文本清洗后为空")

    st = resegment.stats(cues)
    timings["segment"] = time.time() - t0
    timings.setdefault("silence", 0.0)
    st["method"] = method
    st["units"] = len(units)

    # 输出 SRT 备查（写到程序自己的输出目录，不污染用户的音频目录）
    srt_path = None
    try:
        out_dir = os.path.join(
            os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
            "JianyingSubtitle", "output")
        os.makedirs(out_dir, exist_ok=True)
        srt_name = (cfg.get("draft_name")
                    or os.path.splitext(os.path.basename(media_path))[0]) + ".srt"
        srt_path = os.path.join(out_dir, srt_name)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(resegment.to_srt(cues))
    except Exception:
        srt_path = None

    # ---------- 6. 建草稿 ----------
    # 剪映若在运行，必须结束，否则它退出时用内存索引覆盖我们的注册
    if cfg.get("kill_jianying_first", True) and jianying.is_jianying_running():
        P("draft", 0, "检测到剪映正在运行，先关闭它以保护草稿索引…")
        jianying.kill_jianying()

    P("draft", 50, "生成剪映草稿…")
    t0 = time.time()
    draft_name = cfg.get("draft_name") or os.path.splitext(os.path.basename(media_path))[0]
    W, H = (int(x) for x in str(cfg.get("canvas", "1920x1080")).lower().split("x"))
    folder, dur_us, replaced = jianying.build_draft(
        media_path, cues, draft_root, draft_name,
        canvas=(W, H), duration_us=duration_us, ffmpeg=ffmpeg,
        font_size=cfg.get("font_size", 5.0))
    timings["draft"] = time.time() - t0
    timings["total"] = time.time() - t_all

    P("done", 100, f"完成：{len(cues)} 条字幕 -> {folder}")

    return {
        "draft_path": folder,
        "draft_name": draft_name,
        "cues": cues,
        "raw_count": len(raw),
        "duration_us": dur_us,
        "stats": st,
        "silences": n_sil,
        "timings": timings,
        "srt_path": srt_path,
        "replaced": replaced,
    }
