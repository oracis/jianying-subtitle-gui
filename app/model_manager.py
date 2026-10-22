# -*- coding: utf-8 -*-
"""模型管理器：运行时按需下载 faster-whisper 模型。

设计原则（用户明确要求）：
  * **模型不绑进 exe**，exe 只装程序
  * 首次转换时检测模型是否存在；不存在则下载（带进度回调）
  * 下载后缓存到用户目录，后续复用
  * 多源回退：hf-mirror 优先，失败换官方源

不依赖 huggingface_hub 的缓存语义，直接把文件下到自己的目录：
  %LOCALAPPDATA%\\JianyingSubtitle\\models\\faster-whisper-<name>\\{config.json, model.bin, tokenizer.json, vocabulary.txt}
"""
import os
import shutil
import urllib.request
import urllib.error

# 每个模型需要哪些文件
MODEL_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]

# 各档位模型的体积与说明（用于 UI 展示）
# 耗时按「27 分钟音频」折算，来自 i7-6600U（2 核 4 线程，无独显）实测：
#   base + 批量推理 batch=8 -> RTF 0.0937 -> 端到端实测 2.8 分钟
#   small 全程实测 601.7s ≈ 10 分钟（RTF 0.361）
# tiny 已下架（2026-10-21）：中文错字率不可用（"毒贩"→"讻伴"），且上了批量推理后
# base 只比 tiny 慢约 1 秒、质量却好一档，tiny 没有任何存在意义。
MODEL_INFO = {
    "base":   {"size_mb": 145, "label": "快速（145MB，27分钟≈3分钟，中文错字多）",
               "desc": "速度最快；中文错字密集，适合只看分段效果"},
    "small":  {"size_mb": 484, "label": "折中（484MB，27分钟≈10分钟）",
               "desc": "错字比 base 少，短音频推荐"},
    "medium": {"size_mb": 1500, "label": "准确（1.5GB，27分钟≈40分钟）",
               "desc": "错字很少，接近可用；2核4线程机器很慢，建议睡前跑"},
}
# 2026-10-22 实测（同一段 90 秒中文音频）：
#   base   40+ 处错字（忠实→充实、畅销书→唱校书、刽子手→贵子手）——不可直接出片
#   medium 5~8 处（多为罕用人名书名）——配合热词表基本可用
#   large-v3-turbo 时延与 medium 相当但中文略差（解码器仅 4 层），不上架

# 下载源（按优先级）：
# 1. ModelScope（阿里，国内直连免代理，实测 1.9MB/s，最稳）
# 2. hf-mirror（HF 国内镜像，部分网络下直连会被重置，需代理兜底）
# 3. huggingface 官方
MIRRORS = [
    "https://modelscope.cn/models/gpustack/{repo_name}/resolve/master/{file}",
    "https://hf-mirror.com/{repo}/resolve/main/{file}",
    "https://huggingface.co/{repo}/resolve/main/{file}",
]

REPO_TPL = "Systran/faster-whisper-{name}"

_UA = "JianyingSubtitleGUI/1.0"

# 下载路径上任何一次失败都可能是网络被重置（WinError 10054）：
# GUI 进程没有 shell 的代理环境变量，直连 hf-mirror 在部分网络下会被墙重置，
# 所以按「直连 -> 系统代理 -> 常见本地代理端口」逐个探测，取第一个可用的。
_COMMON_PROXY_PORTS = ["10808", "10809", "7890", "7897", "33210", "1080", "8889"]


def _iter_proxy_specs():
    """候选网络配置（依次尝试）：None=强制直连，其余为代理 URL。"""
    yield None
    try:
        sys_proxies = urllib.request.getproxies()
    except Exception:
        sys_proxies = {}
    seen = set()
    for scheme in ("http", "https"):
        p = sys_proxies.get(scheme)
        if p and p not in seen:
            seen.add(p)
            yield p
    for port in _COMMON_PROXY_PORTS:
        p = f"http://127.0.0.1:{port}"
        if p not in seen:
            seen.add(p)
            yield p


def _build_opener(proxy):
    if proxy is None:
        # 显式清空代理，避免 urllib 自动拾取系统代理干扰「直连」测试
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def _probe(opener, url, timeout=8):
    """用 1KB Range 请求快速验证「某 opener + 某源」是否可用。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Range": "bytes=0-1023"})
    with opener.open(req, timeout=timeout) as r:
        r.read(1024)


def models_root(app_dir=None):
    """模型根目录：<app_dir>/models 或 %LOCALAPPDATA%/JianyingSubtitle/models。"""
    if app_dir:
        return os.path.join(app_dir, "models")
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "JianyingSubtitle", "models")


def model_dir(name, app_dir=None):
    return os.path.join(models_root(app_dir), f"faster-whisper-{name}")


def is_ready(name, app_dir=None):
    """模型是否已完整下载（存在且 model.bin 大小合理）。"""
    d = model_dir(name, app_dir)
    for f in MODEL_FILES:
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            return False
    # model.bin 至少 1MB，防止半截文件被当成完整
    if os.path.getsize(os.path.join(d, "model.bin")) < 1024 * 1024:
        return False
    return True


def _download_one(opener, url, dest, progress, cancel_flag=None):
    """下载单个文件，带进度回调。progress(downloaded_bytes, total_bytes, filename)。"""
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with opener.open(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    progress(done, total, os.path.basename(dest))
                    if cancel_flag and cancel_flag():
                        raise RuntimeError("用户已取消")
        os.replace(tmp, dest)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)  # 半截文件不能留，否则下次会被当成"已下载"
        except OSError:
            pass
        raise


def ensure_model(name, progress=None, app_dir=None, cancel_flag=None):
    """确保模型可用；缺则下载。返回模型目录路径。

    progress: 回调 (stage:str, cur:int, total:int, msg:str)
    cancel_flag: 可调用对象，返回 True 时中止
    """
    if progress is None:
        progress = lambda *a, **k: None

    if is_ready(name, app_dir):
        d = model_dir(name, app_dir)
        progress("ready", 1, 1, f"模型已就绪: {d}")
        return d

    d = model_dir(name, app_dir)
    os.makedirs(d, exist_ok=True)
    repo = REPO_TPL.format(name=name)
    info = MODEL_INFO.get(name, {})
    progress("start", 0, 100, f"开始下载模型 {name}（约 {info.get('size_mb', '?')}MB）")

    # ---------- 第一步：探测可用的网络路径（直连 / 系统代理 / 本地代理） ----------
    opener = None
    used_via = None
    for spec in _iter_proxy_specs():
        op = _build_opener(spec)
        for murl in MIRRORS:
            try:
                _probe(op, murl.format(repo=repo, file="config.json"))
                opener, used_via = op, (spec or "直连")
                break
            except Exception:
                continue
        if opener:
            break
    if opener is None:
        raise RuntimeError(
            "网络不可用：直连与代理均无法访问模型源（hf-mirror / huggingface）。"
            "请确认能上网，或本机代理（如 v2rayN/Clash 的 HTTP 端口）已开启后重试。")
    progress("start", 0, 100, f"网络就绪（经 {used_via}）")

    # ---------- 第二步：逐个文件下载，失败自动重探测网络再重试 ----------
    for fi, fname in enumerate(MODEL_FILES, 1):
        dest = os.path.join(d, fname)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            continue  # 断点续传：已存在则跳过
        last_err = None
        ok = False
        for attempt in range(3):
            for murl in MIRRORS:
                url = murl.format(repo=repo, repo_name=repo, file=fname)
                try:
                    def _cb(done, total, fn, _fi=fi):
                        pct = int((_fi - 1) / len(MODEL_FILES) * 100 + (done / total * 100 / len(MODEL_FILES))) if total else 0
                        mb = done / 1024 / 1024
                        tmb = total / 1024 / 1024 if total else 0
                        progress("download", pct, 100, f"{fn}  {mb:.1f}/{tmb:.1f} MB")
                    _download_one(opener, url, dest, _cb, cancel_flag)
                    ok = True
                    break
                except RuntimeError as e:
                    last_err = e
                    if "用户已取消" in str(e):
                        raise
                    continue
                except Exception as e:
                    last_err = e
                    continue
            if ok:
                break
            # 本轮全部失败：网络可能变了，重新探测代理
            opener = None
            for spec in _iter_proxy_specs():
                op = _build_opener(spec)
                for murl in MIRRORS:
                    try:
                        _probe(op, murl.format(repo=repo, file="config.json"))
                        opener, used_via = op, (spec or "直连")
                        break
                    except Exception:
                        continue
                if opener:
                    break
            if opener is None:
                break
        if not ok:
            raise RuntimeError(f"下载 {fname} 失败（所有源与网络路径均不可用）: {last_err}")

        if cancel_flag and cancel_flag():
            raise RuntimeError("用户已取消")

    # 校验完整性
    if not is_ready(name, app_dir):
        raise RuntimeError(f"模型 {name} 下载后校验失败，请重试")

    progress("done", 100, 100, f"模型就绪: {d}")
    return d


def purge_model(name, app_dir=None):
    """删除某档模型。"""
    d = model_dir(name, app_dir)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False
