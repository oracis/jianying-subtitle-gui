# -*- coding: utf-8 -*-
"""配置持久化：剪映路径、模型档位、画布等。

配置文件位置：%LOCALAPPDATA%\\JianyingSubtitle\\config.json
首次运行自动探测剪映路径。
"""
import json
import os
import glob


def config_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "JianyingSubtitle")


def config_path():
    return os.path.join(config_dir(), "config.json")


# 剪映草稿目录的常见位置
DRAFT_CANDIDATES = [
    lambda: os.path.join(os.path.expanduser("~"), "AppData", "Local", "JianyingPro",
                         "User Data", "Projects", "com.lveditor.draft"),
    lambda: os.path.join(os.environ.get("LOCALAPPDATA", ""), "JianyingPro",
                         "User Data", "Projects", "com.lveditor.draft"),
]

# 剪映安装目录的常见位置
INSTALL_CANDIDATES = [
    r"D:/Jianying6.01/Jianying_Pro",
    r"C:/Program Files/JianyingPro",
    r"C:/Program Files (x86)/JianyingPro",
    r"D:/Program Files/JianyingPro",
    r"D:/JianyingPro",
    r"C:/JianyingPro",
]


def detect_draft_dir():
    """自动探测草稿目录（认 root_meta_info.json 存在与否）。"""
    for fn in DRAFT_CANDIDATES:
        try:
            p = fn()
        except Exception:
            continue
        if p and os.path.isfile(os.path.join(p, "root_meta_info.json")):
            return p.replace("\\", "/")
    # 兜底：扫 AppData 下所有 Jianying* 目录
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    for p in glob.glob(os.path.join(base, "Jianying*", "User Data", "Projects",
                                    "com.lveditor.draft")):
        if os.path.isfile(os.path.join(p, "root_meta_info.json")):
            return p.replace("\\", "/")
    return ""


def detect_install_dir():
    """自动探测剪映安装目录（找 JianyingPro.exe）。"""
    for p in INSTALL_CANDIDATES:
        pp = p.replace("/", os.sep)
        if os.path.isfile(os.path.join(pp, "JianyingPro.exe")):
            return p
    # 扫盘顶层两层
    for drive in ["C:", "D:", "E:", "F:"]:
        for pat in [f"{drive}/Jianying*/JianyingPro.exe",
                    f"{drive}/*/Jianying*/JianyingPro.exe"]:
            hits = glob.glob(pat)
            if hits:
                return os.path.dirname(hits[0]).replace("\\", "/")
    return ""


DEFAULTS = {
    "install_dir": "",       # 剪映安装目录（用于「打开剪映」按钮）
    "draft_dir": "",         # 草稿箱目录（com.lveditor.draft）
    "model_size": "base",    # base / small
    "canvas": "1920x1080",   # 画布尺寸
    "max_chars": 24,         # 单条字幕最大字数
    "noise_db": -32,         # 静音检测阈值
    "min_pause": 0.18,       # 最短静音秒数
    "auto_open_jianying": True,   # 生成后是否自动打开剪映
    "kill_jianying_first": True,  # 写草稿前是否结束剪映进程
    # ---- 术语与纠错（错字问题的正解：喂给模型，而不是事后穷举替换） ----
    # term_pack: 启用哪些术语包（assets/terms/<名>.json），逗号分隔可叠加。
    #            换题材时换包名即可，不用改代码。
    # terms:     本次临时补充的专名，逗号分隔，直接拼进 ASR prompt。
    "term_pack": "",
    "terms": "",
    "asr_prompt": "",        # 额外 prompt 前缀，留空则只用默认引导 + 术语
}


def load():
    """读配置，缺失项用自动探测/默认值补齐。"""
    cfg = dict(DEFAULTS)
    p = config_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # 自动填空值
    if not cfg.get("draft_dir"):
        cfg["draft_dir"] = detect_draft_dir()
    if not cfg.get("install_dir"):
        cfg["install_dir"] = detect_install_dir()
    return cfg


def save(cfg):
    os.makedirs(config_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
