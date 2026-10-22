# -*- coding: utf-8 -*-
"""ASR 文本后处理：繁转简、错字纠正、术语注入。

================================ 设计原则 ================================
**错字表不能无限膨胀。** 之前的做法是把一本书里发现的错字一条条写死进代码，
结果 82 条规则里有 60 多条只对那一本书有效：换一段讲芯片、讲医学的音频，
全部作废，又要从头补。这条路没有尽头。

所以错字分两类，分开治：

1. **跨领域通用**（GENERIC_FIXES）
   与主题无关的同音误写，如「流毡->浏览」「支权->职权」。数量有限、
   任何内容都可能命中，写死在代码里是合理的。

2. **领域/本书专属**（assets/terms/*.json）
   毒品犯罪词族、专有名词。**每换一个领域就是一套新词**，所以放在
   JSON 里当数据，不写进代码：
     * `assets/terms/毒品犯罪.json`  领域错字包，同类内容可复用
     * `assets/terms/黑金世界.json`  单本书的专名与专属错字
   新增内容只需加 JSON，不用改代码、不用重新打包。

**更高优先级的做法是把术语喂给模型**（见 build_prompt），让它在识别时
就听对，而不是事后替换。事后替换永远只是补丁。
===========================================================================
"""

import json
import os
import re

_zhconv = None
_zhconv_tried = False


def _get_zhconv():
    global _zhconv, _zhconv_tried
    if _zhconv_tried:
        return _zhconv
    _zhconv_tried = True
    try:
        import zhconv
        _zhconv = zhconv
    except Exception:
        _zhconv = None
    return _zhconv


def to_simplified(text):
    """繁体 -> 简体。失败时原样返回（不抛异常，保证主流程不断）。"""
    if not text:
        return text
    z = _get_zhconv()
    if z is None:
        return text
    try:
        return z.convert(text, "zh-cn")
    except Exception:
        return text


# ============================ 1. 跨领域通用错字 ============================
# 只放「与题材无关」的误写。毒品/医学/科技专属的词一律不进这里。
# 顺序：长词在前，避免被短规则先吃掉。
GENERIC_FIXES = [
    ("流毡器", "浏览器"),
    ("流毡", "浏览"),
    ("支权", "职权"),
    ("询诗", "趋势"),
    ("盖扩", "概括"),
    ("扩商", "扩张"),
    ("手取", "收取"),
    ("受全人", "受权人"),
    ("走适", "走私"),
    ("走適", "走私"),
    ("示范工贝", "事半功倍"),
    ("分射社长", "分社社长"),
    ("的运营书", "的运营术"),
    ("贩壮", "贩运"),
]


# ============================ 2. 术语包加载 ============================
def assets_terms_dir():
    """术语包目录：打包后在 _MEIPASS/assets/terms，源码运行在 ../assets/terms。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "assets", "terms"),
        os.path.join(getattr(__import__("sys"), "_MEIPASS", ""), "assets", "terms"),
        os.path.join(here, "assets", "terms"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    return os.path.abspath(candidates[0])


def list_packs():
    """可用术语包名列表（不含扩展名）。"""
    d = assets_terms_dir()
    if not os.path.isdir(d):
        return []
    return sorted(os.path.splitext(f)[0] for f in os.listdir(d)
                  if f.lower().endswith(".json"))


def load_pack(name):
    """读术语包 -> (fixes, terms)。

    fixes: [(错, 对), ...]  错字替换规则
    terms: [专名, ...]      注入 ASR prompt 的专有名词
    找不到返回 ([], [])，不抛异常。
    """
    if not name:
        return [], []
    p = os.path.join(assets_terms_dir(), f"{name}.json")
    if not os.path.isfile(p):
        return [], []
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return [], []
    fixes = [tuple(x) for x in d.get("fixes", []) if len(x) == 2]
    terms = [str(x) for x in d.get("terms", []) if str(x).strip()]
    return fixes, terms


# ============================ 3. 术语注入 prompt ============================
def build_prompt(terms, extra=""):
    """把术语表拼成 ASR 的 initial_prompt。

    为什么用 prompt 而不是只做事后替换：
      whisper 的 initial_prompt 会作为解码的上下文前缀，直接影响用词选择。
      把「古柯树 / 锡那罗亚 / 萨尔瓦多」这类词提前告诉模型，它更容易听对，
      这是从源头减少错字，比事后打补丁可迁移得多。

    长度控制：prompt 太长会挤占解码上下文，实测控制在 200 字内比较稳。
    """
    base = "以下是简体中文的口语内容转写，请使用简体中文并保留标点。"
    parts = [base]
    if extra:
        parts.append(extra.strip())
    if terms:
        # 去重 + 限长，避免 prompt 撑爆
        seen, uniq = set(), []
        for t in terms:
            t = t.strip()
            if t and t not in seen:
                seen.add(t)
                uniq.append(t)
        joined = "、".join(uniq)[:160]
        parts.append(f"可能出现的专有名词：{joined}。")
    return "".join(parts)


# ============================ 4. 应用 ============================
def apply_fixes(text, fixes):
    """按顺序做字符串替换。fixes 为 [(错, 对), ...]。"""
    if not text or not fixes:
        return text
    for bad, good in fixes:
        if bad and bad in text:
            text = text.replace(bad, good)
    return text


# 字幕无标点（2026-10-22 用户拍板，对齐剪映原生效果）：保留汉字/字母/数字/空白，
# 其余全部剔除。为什么不用 whisper 参数在源头抑制（实测 60s 片段）：
#   initial_prompt 引导 19->9 个不稳且错字变多；suppress_tokens 普通管线 0 个，
#   但批量管线（产品默认提速路径）下退化成"……"洪流+复读机幻觉。
_PUNCT_RE = re.compile(r"[^\u4e00-\u9fa5a-zA-Z0-9\s]")


def strip_punct(text):
    """剔除全部标点与符号（中文字幕惯例）。保留空白，稍后统一折叠。"""
    if not text:
        return text
    return _PUNCT_RE.sub("", text)


def clean(text, extra_fixes=None):
    """完整后处理：繁体转简 -> 通用纠错 -> 领域纠错 -> 去标点 -> 去多余空白。

    extra_fixes: 领域/本书专属的替换规则（由 core 从术语包读入）。
    ASR 段文本和字级单元拼接后的字幕文本都会经过这里（word.word 里也带标点，
    靠字幕级这一次 clean 兜住，见 core.run 的 cues 清洗）。
    """
    if not text:
        return ""
    text = to_simplified(text)
    text = apply_fixes(text, GENERIC_FIXES)
    if extra_fixes:
        text = apply_fixes(text, extra_fixes)
    text = strip_punct(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def zhconv_available():
    return _get_zhconv() is not None
