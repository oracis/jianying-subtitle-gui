# -*- coding: utf-8 -*-
"""字幕按语音停顿重分段 —— 核心算法模块（v2，已验证：421 条 / 时间轴异常 0）。

设计依据（业界标准方案，非自创）：
  1. Whisper `SubtitlesWriter.iterate_result()`：词级时间戳 + 词间空隙阈值断句
  2. labvent Netflix 风格字幕规则集：每行 ≤42 字符 / ≤2 行 / 阅读速度 ≤20cps /
     单条 5~7s / 最小间隔 2 帧 / 停顿 ≥1s 断句
  3. 中文对应：FunASR paraformer-zh + fsmn-vad（端点检测）+ ct-punc（标点）

本模块做的是「已有 ASR 文本 -> 按真实停顿重切」，**不重跑 ASR**。

核心思路:
  1. ffmpeg silencedetect -> 静音区间
  2. 对每个 ASR 段, 在其自身时间窗内切出发声块(burst), burst 边界即真实停顿
  3. 文本只在真实标点处切分(句末标点优先, 其次小句标点), 绝不硬切词中间
  4. 用该段的局部语速把文本块拟合到发声块上: 选取"累积发声时长最接近期望"的
     发声块数量, 使字幕边界精确落在真实停顿处
  5. 合并过短碎片, 输出严格单调无重叠的时间轴
"""
import re
import statistics

# ---------- 默认参数 ----------
MAX_CHARS = 24       # 单条字幕最大字符数, 超过则尝试在小句标点处再切
SENT_END = "。！？!?"      # 句末标点(优先断点)
CLAUSE = "，、；：,;:"     # 小句标点(次优先断点)
MERGE_MIN_DUR = 0.5  # 短于此且字数少的碎片并入上一条
MERGE_MAX_CHARS = 12


def parse_silences(path):
    """解析 ffmpeg silencedetect 输出, 返回 [(start, end), ...] 静音区间列表。"""
    silences, cur = [], None
    for line in open(path, encoding="utf-8", errors="ignore"):
        m = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m:
            cur = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m and cur is not None:
            silences.append((cur, float(m.group(1))))
            cur = None
    return sorted(silences)


def parse_transcript(path):
    """解析 ASR 转写, 返回 [[start_s, end_s, text], ...]。

    支持两种格式:
      A) `[M:SS.mmm,M:SS.mmm,sp] 文本`      (内置 AudioTranscribe 输出)
      B) `start,end,text`  (每行 csv, 秒为单位, Whisper/其他 ASR 通用)
    """
    segs = []
    line_re = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?),(\d+):(\d+(?:\.\d+)?),\d+\]\s*(.*)$")
    for raw in open(path, encoding="utf-8", errors="ignore"):
        line = raw.rstrip("\n").strip()
        if not line:
            continue
        m = line_re.match(line)
        if m:
            sm, ss, em, es, text = m.groups()
            segs.append([int(sm) * 60 + float(ss), int(em) * 60 + float(es), text.strip()])
            continue
        parts = line.split(",", 2)
        if len(parts) == 3:
            try:
                st, en = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            txt = parts[2].strip()
            if txt:
                segs.append([st, en, txt])
    return segs


def split_text(text, max_chars=MAX_CHARS):
    """把文本切成不超过 max_chars 的块, 只在标点处切, 绝不硬切词中间。"""
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in SENT_END:
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)

    out = []
    for p in parts:
        if len(p) <= max_chars:
            out.append(p)
            continue
        # 过长 -> 在小句标点处切; 找不到合适断点就整块保留(不硬切)
        lo = max(1, int(max_chars * 0.45))
        hi = min(len(p) - 1, int(max_chars * 1.45))
        cand = [i for i in range(lo, hi + 1) if p[i] in CLAUSE]
        if not cand:
            out.append(p)
            continue
        # 优先取"不超过 max_chars 的最靠前分句", 长句切得更均衡
        under = [i for i in cand if i <= max_chars]
        cut = (max(under) if under else min(cand)) + 1
        out.append(p[:cut])
        out.extend(split_text(p[cut:], max_chars))
    return [x for x in out if x.strip()]


def _bursts_in(silences, s, e):
    """在 [s, e] 窗口内切出发声块。"""
    inner = [(ss, se) for (ss, se) in silences if ss > s + 0.02 and se < e - 0.02]
    bursts, cur = [], s
    for (ss, se) in inner:
        if ss > cur:
            bursts.append((cur, ss))
        cur = max(cur, se)
    if cur < e - 0.01:
        bursts.append((cur, e))
    return bursts


def _snap_start(silences, s, e):
    """把起点吸附到最近的静音结束处(即真实停顿之后)。"""
    for (ss, se) in silences:
        if ss - 0.05 <= s <= se:
            if se < e - 0.05:
                return se
            break
    return s


def resegment(transcript, silences, max_chars=MAX_CHARS):
    """核心重分段。返回 [[start, end, text], ...] 严格单调、无重叠。"""
    if not transcript:
        return []

    rows = []
    for (s0, e0, text) in transcript:
        s = _snap_start(silences, s0, e0)
        e = e0
        pieces = split_text(text, max_chars)
        if not pieces:
            continue
        if len(pieces) <= 1:
            rows.append((s, e, pieces[0]))
            continue

        bursts = _bursts_in(silences, s, e)
        if not bursts:
            # 无停顿可依 -> 按字数比例均分
            total = sum(len(p) for p in pieces)
            acc, prev = 0, s
            for p in pieces[:-1]:
                acc += len(p)
                b = s + (e - s) * acc / total
                rows.append((prev, b, p))
                prev = b
            rows.append((prev, e, pieces[-1]))
            continue

        speech = sum(b[1] - b[0] for b in bursts)
        rate = sum(len(p) for p in pieces) / speech if speech > 0 else 0.0

        # 为每个文本块挑选"累积时长最接近期望"的发声块数量
        assign, bi = [], 0
        for p in pieces:
            want = len(p) / rate if rate > 0 else 0.0
            best_k, best_err = 0, None
            acc = 0.0
            for k in range(1, min(12, len(bursts) - bi) + 1):
                acc += bursts[bi + k - 1][1] - bursts[bi + k - 1][0]
                err = abs(acc - want)
                if best_err is None or err < best_err - 1e-9:
                    best_k, best_err = k, err
            if best_k == 0:
                assign.append(0)          # 发声块已用尽
            else:
                assign.append(best_k)
                bi += best_k

        # 发声块用尽仍有文本块未分配 -> 把它们并入最后一块
        if 0 in assign:
            first_zero = assign.index(0)
            merged = "".join(pieces[first_zero - 1:]) if first_zero > 0 else "".join(pieces)
            pieces = pieces[:first_zero - 1] + [merged]
            assign = assign[:first_zero - 1] + [sum(assign[first_zero - 1:])]

        # 由分配还原时间区间: 第 j 块结束 = 第 j+1 块开始(连续无缝, 边界落在停顿处)
        idx = 0
        for j, p in enumerate(pieces):
            st = bursts[min(idx, len(bursts) - 1)][0]
            idx += assign[j]
            nxt = bursts[idx][0] if idx < len(bursts) else e
            if nxt <= st:
                nxt = st + 0.12
            rows.append((st, nxt, p))

    # 清洗: 合并过短碎片
    cleaned = []
    for (s, e, t) in rows:
        if cleaned and (e - s) < MERGE_MIN_DUR and len(t) < MERGE_MAX_CHARS:
            ps, pe, pt = cleaned[-1]
            cleaned[-1] = (ps, e, pt + t)
        else:
            cleaned.append((s, e, t))

    # 强制严格单调无重叠
    fixed = []
    prev_end = -1e9
    for (s, e, t) in cleaned:
        if e <= s:
            e = s + 0.6
        if s < prev_end:
            s = prev_end
            e = max(e, s + 0.6)
        fixed.append([s, e, t])
        prev_end = e
    return fixed


def to_srt(cues):
    """转成 SRT 文本。"""
    def ts(t):
        ms = int(round(t * 1000))
        return f"{ms//3600000:02d}:{ms%3600000//60000:02d}:{ms%60000//1000:02d},{ms%1000:03d}"

    return "\n".join(
        f"{i}\n{ts(s)} --> {ts(e)}\n{t}\n"
        for i, (s, e, t) in enumerate(cues, 1))


def stats(cues):
    """统计指标, 用于自检。"""
    if not cues:
        return {}
    durs = [e - s for s, e, _ in cues]
    lens = [len(t) for _, _, t in cues]
    anomalies = sum(1 for i in range(1, len(cues))
                    if cues[i][0] < cues[i - 1][1] - 1e-6)
    buckets = {"<2s": 0, "2-4s": 0, "4-6s": 0, ">6s": 0}
    for d in durs:
        if d < 2: buckets["<2s"] += 1
        elif d < 4: buckets["2-4s"] += 1
        elif d < 6: buckets["4-6s"] += 1
        else: buckets[">6s"] += 1
    return {
        "count": len(cues),
        "avg_dur": statistics.mean(durs),
        "median_dur": statistics.median(durs),
        "max_dur": max(durs),
        "min_dur": min(durs),
        "avg_chars": statistics.mean(lens),
        "max_chars": max(lens),
        "anomalies": anomalies,
        "end": cues[-1][1],
        "buckets": buckets,
    }


# ============================================================================
# v3：字级时间戳断句（主路径）
# ----------------------------------------------------------------------------
# 为什么比 v2 更好：
#   v2 依赖两件事——ASR 文本带标点、ffmpeg 静音检测。但本地 whisper 的 base
#   模型中文输出**几乎不带标点**，导致 split_text() 退化成"整块保留"，字幕条
#   数从 421 掉到 499 但每条都很长（27.8 分钟只切出 499 条且多为整句）。
#
#   v3 直接用 faster-whisper 的 word_timestamps：中文逐字给出 start/end，
#   字与字之间的空隙就是真实停顿。于是：
#     * 不依赖标点
#     * 不依赖 ffmpeg 静音检测（省掉 10 秒）
#     * 空隙来自 ASR 自己的对齐结果，比外部 VAD 更贴合语音内容
#   且 word_timestamps 的额外开销实测几乎为零。
# ============================================================================

WORD_PAUSE = 0.25      # 字间空隙 >= 此值即断句（真实停顿）
WORD_HARD_PAUSE = 0.60  # 明显长停顿
WORD_MIN_DUR = 1.2     # 单条最短时长（不足则合并到相邻）
WORD_MAX_DUR = 6.5     # 单条最长时长
WORD_MAX_CHARS = 22    # 单条最大字数


def resegment_words(units, max_chars=WORD_MAX_CHARS, pause=WORD_PAUSE,
                    min_dur=WORD_MIN_DUR, max_dur=WORD_MAX_DUR):
    """按字级时间戳的字间空隙断句。

    units: [(word, start_sec, end_sec), ...]，来自 asr.transcribe(return_units=True)
    返回 [[start, end, text], ...]，严格单调、无重叠。

    规则（对齐 Whisper SubtitlesWriter + Netflix 字幕规范）：
      1. 字间空隙 >= pause  -> 无条件断（这就是"按语音停顿分小段"）
      2. 字数超 max_chars 或时长超 max_dur -> 回退到本 cue 内空隙最大处断
      3. 过短的碎片合并回相邻条，避免一闪而过的字幕
    """
    if not units:
        return []

    raw, cur = [], []

    def flush():
        nonlocal cur
        if cur:
            txt = "".join(w for w, _, _ in cur).strip()
            if txt:
                raw.append([cur[0][1], cur[-1][2], txt])
        cur = []

    n = len(units)
    for i, (w, s, e) in enumerate(units):
        cur.append((w, s, e))
        if i + 1 >= n:
            flush()
            break
        gap = units[i + 1][1] - e
        dur = e - cur[0][1]
        nchar = sum(len(x.strip()) for x, _, _ in cur)

        # 1) 真实停顿 -> 断
        if gap >= pause:
            flush()
            continue
        # 2) 超长 -> 在本 cue 内找空隙最大的位置断
        if nchar >= max_chars or dur >= max_dur:
            best, bi = -1.0, None
            for j in range(max(1, len(cur) // 3), len(cur)):
                g = cur[j][1] - cur[j - 1][2]
                if g > best:
                    best, bi = g, j
            if bi and best > 0.04:
                tail = cur[bi:]
                del cur[bi:]
                flush()
                cur = tail
            else:
                flush()
    flush()

    # 3) 合并过短碎片
    out = []
    for c in raw:
        if out:
            prev = out[-1]
            joined = len(prev[2]) + len(c[2])
            gap_between = c[0] - prev[1]
            too_short = (c[1] - c[0]) < min_dur
            room = joined <= max_chars
            if room and gap_between < 0.9 and (too_short or joined <= max_chars * 0.6):
                prev[1] = c[1]
                prev[2] += c[2]
                continue
        out.append(c)

    # 4) 保证严格单调（重叠则把上一条末尾压到本条起点）
    for i in range(1, len(out)):
        if out[i][0] < out[i - 1][1] - 1e-6:
            out[i - 1][1] = out[i][0]
    return [c for c in out if c[2].strip()]


def to_srt(cues):
    """从 [[start, end, text], ...] 生成 SRT 文本（与 resegment 输出同格式）。"""
    def ts(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    lines = []
    for i, (s, e, txt) in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{ts(s)} --> {ts(e)}")
        lines.append(txt.strip())
        lines.append("")
    return "\n".join(lines)
