# -*- coding: utf-8 -*-
"""剪映草稿生成：在官方骨架上填充，不依赖 pyJianYingDraft。

为什么自己写：
  pyJianYingDraft 会连带 pymediainfo / imageio / uiautomation 等依赖，
  打包体积大，且 uiautomation 在 Python 3.13 上装不上。
  我们只需要「一条音频轨 + 一条字幕轨」，直接填 JSON 更可控。

骨架来源：
  app/assets/draft_content_skeleton.json 是从 pyJianYingDraft 生成的真实草稿
  清空素材后保存的，保留了全部 28 个顶层键 / 46 个 materials 键 / 完整 config，
  确保剪映 6.x 能正常读取（剪映只在自己保存时才加密，生成时可读明文）。

关键字段（实测所得）：
  * 文本片段定位用**归一化坐标**，y=-0.8 即画面下方
  * 片段需要一批 enable_* 开关字段
  * 音频片段需要配一个 speeds 素材并写入 extra_material_refs
"""
import json
import os
import re
import subprocess
import sys
import time
import uuid


def _assets_dir():
    """assets 目录：兼容 PyInstaller 打包后的 _MEIPASS。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, "assets"),
        os.path.join(getattr(sys, "_MEIPASS", ""), "assets"),
        os.path.join(getattr(sys, "_MEIPASS", ""), "app", "assets"),
    ]
    for c in cands:
        if os.path.isdir(c):
            return c
    return os.path.join(here, "assets")


def _load_skeleton():
    p = os.path.join(_assets_dir(), "draft_content_skeleton.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_meta_skeleton():
    p = os.path.join(_assets_dir(), "draft_meta_info.json")
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _nid():
    return uuid.uuid4().hex


def _new_draft_id():
    return str(uuid.uuid4()).upper()


# ---------------- 时长探测 ----------------
def media_duration_us(ffmpeg, media):
    """用 ffmpeg 解析时长（不依赖 ffprobe）。返回微秒。"""
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-i", media],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
        if m:
            h, mi, s = m.groups()
            return int((int(h) * 3600 + int(mi) * 60 + float(s)) * 1_000_000)
    except Exception:
        pass
    return 0


# ---------------- SRT ----------------
def _ts(t):
    ms = int(round(t * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def cues_to_srt(cues):
    return "\n".join(
        f"{i}\n{_ts(s)} --> {_ts(e)}\n{t}\n"
        for i, (s, e, t) in enumerate(cues, 1))


# ---------------- 素材构造 ----------------
def _text_material(text, font_size=5.0):
    content = {
        "styles": [{
            "fill": {"alpha": 1.0, "content": {"render_type": "solid",
                                               "solid": {"alpha": 1.0,
                                                         "color": [1.0, 1.0, 1.0]}}},
            "range": [0, len(text)],
            "size": font_size,
            "bold": False,
            "italic": False,
            "underline": False,
            "strokes": [],
        }],
        "text": text,
    }
    return {
        "id": _nid(),
        "content": json.dumps(content, ensure_ascii=False),
        "typesetting": 0,
        "alignment": 1,
        "letter_spacing": 0.0,
        "line_spacing": 0.02,
        "line_feed": 1,
        "line_max_width": 0.82,
        "force_apply_line_max_width": False,
        "check_flag": 7,
        "type": "subtitle",
        "global_alpha": 1.0,
    }


def _audio_material(path, duration_us):
    mid = _nid()
    return {
        "app_id": 0,
        "category_id": "",
        "category_name": "local",
        "check_flag": 3,
        "copyright_limit_type": "none",
        "duration": duration_us,
        "effect_id": "",
        "formula_id": "",
        "id": mid,
        "local_material_id": mid,
        "music_id": mid,
        "name": os.path.basename(path),
        "path": path,
        "source_platform": 0,
        "type": "extract_music",
        "wave_points": [],
    }


def _speed_material():
    return {"curve_speed": None, "id": _nid(), "mode": 0, "speed": 1.0, "type": "speed"}


_SEG_BASE = {
    "enable_adjust": True,
    "enable_color_correct_adjust": False,
    "enable_color_curves": True,
    "enable_color_match_adjust": False,
    "enable_color_wheels": True,
    "enable_lut": True,
    "enable_smart_color_adjust": False,
    "last_nonzero_volume": 1.0,
    "reverse": False,
    "track_attribute": 0,
    "track_render_index": 0,
    "visible": True,
    "common_keyframes": [],
    "keyframe_refs": [],
    "speed": 1.0,
    "volume": 1.0,
    "is_tone_modify": False,
}


def _audio_segment(mat_id, speed_id, duration_us):
    s = dict(_SEG_BASE)
    s.update({
        "id": _nid(),
        "material_id": mat_id,
        "target_timerange": {"start": 0, "duration": duration_us},
        "source_timerange": {"start": 0, "duration": duration_us},
        "extra_material_refs": [speed_id],
        "clip": None,
        "hdr_settings": None,
        "render_index": 0,
    })
    return s


def _text_segment(mat_id, start_us, end_us, y=-0.8):
    dur = max(end_us - start_us, 100_000)
    s = dict(_SEG_BASE)
    s.update({
        "id": _nid(),
        "material_id": mat_id,
        "target_timerange": {"start": start_us, "duration": dur},
        "source_timerange": None,
        "extra_material_refs": [],
        "clip": {
            "alpha": 1.0,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": y},
        },
        "uniform_scale": {"on": True, "value": 1.0},
        "render_index": 1,
    })
    return s


def _track(ttype, name, segments):
    return {
        "attribute": 0,
        "flag": 0,
        "id": _nid(),
        "is_default_name": False,
        "name": name,
        "segments": segments,
        "type": ttype,
    }


# ---------------- 主流程 ----------------
def build_draft(audio_path, cues, draft_root, draft_name,
                canvas=(1920, 1080), fps=30, duration_us=0,
                ffmpeg=None, font_size=5.0, cover_color=(22, 33, 62),
                text_y=-0.8):
    """生成剪映草稿并注册进草稿箱。返回 (草稿目录, 时长us, 是否覆盖同名)。"""
    audio_path = os.path.abspath(audio_path)
    folder_path = os.path.join(draft_root, draft_name)
    os.makedirs(folder_path, exist_ok=True)

    if not duration_us:
        duration_us = media_duration_us(ffmpeg, audio_path) if ffmpeg else 0
    if not duration_us:
        duration_us = int((cues[-1][1] if cues else 0) * 1_000_000) + 500_000

    W, H = canvas
    draft_id = _new_draft_id()
    now_us = int(time.time() * 1_000_000)

    # ---- 在骨架上填充 ----
    content = _load_skeleton()
    content["id"] = draft_id
    content["name"] = draft_name
    content["duration"] = duration_us
    content["fps"] = fps
    content["canvas_config"] = {
        "height": H, "width": W,
        "ratio": "16:9" if W / H > 1.5 else ("9:16" if H / W > 1.5 else "original"),
    }
    content["create_time"] = now_us
    content["update_time"] = now_us
    content["static_cover_image_path"] = os.path.join(
        folder_path, "draft_cover.jpg").replace("\\", "/")

    # 素材
    amat = _audio_material(audio_path, duration_us)
    smat = _speed_material()
    text_mats = []
    text_segs = []
    for (s, e, t) in cues:
        m = _text_material(t, font_size)
        text_mats.append(m)
        text_segs.append(_text_segment(m["id"], int(s * 1_000_000),
                                       int(e * 1_000_000), y=text_y))

    content["materials"]["audios"] = [amat]
    content["materials"]["texts"] = text_mats
    content["materials"]["speeds"] = [smat]

    # 轨道
    content["tracks"] = [
        _track("audio", "audio_1", [_audio_segment(amat["id"], smat["id"], duration_us)]),
        _track("text", "字幕", text_segs),
    ]

    with open(os.path.join(folder_path, "draft_content.json"), "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=1)

    # ---- draft_meta_info.json ----
    meta = _load_meta_skeleton()
    meta.update({
        "draft_cover": os.path.join(folder_path, "draft_cover.jpg").replace("\\", "/"),
        "draft_fold_path": folder_path.replace("\\", "/"),
        "draft_id": draft_id,
        "draft_name": draft_name,
        "draft_root_path": draft_root.replace("\\", "/"),
        "draft_timeline_materials_size": os.path.getsize(audio_path) if os.path.isfile(audio_path) else 0,
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_duration": duration_us,
    })
    meta["draft_materials"] = [{
        "type": 0, "value": [{
            "duration": duration_us, "file_Path": audio_path.replace("\\", "/"),
            "height": 0, "id": draft_id, "import_time": now_us,
            "import_time_ms": now_us, "item_source": 1,
            "md5": "", "metetype": "music",
            "roughcut_time_range": {"duration": duration_us, "start": 0},
            "sub_time_range": {"duration": -1, "start": -1},
            "type": 0, "width": 0,
        }],
    }]
    with open(os.path.join(folder_path, "draft_meta_info.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=4)

    # ---- 封面 ----
    if ffmpeg:
        try:
            cw, ch = (540, 304) if W >= H else (270, 480)
            color = "0x%02x%02x%02x" % tuple(cover_color)
            subprocess.run([ffmpeg, "-f", "lavfi", "-i", f"color=c={color}:s={cw}x{ch}",
                            "-frames:v", "1",
                            os.path.join(folder_path, "draft_cover.jpg"), "-y"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    replaced = register_draft(draft_root, meta)
    return folder_path, duration_us, replaced


def register_draft(draft_root, meta):
    """把草稿写入 root_meta_info.json 的 all_draft_store。"""
    root_path = os.path.join(draft_root, "root_meta_info.json")
    if not os.path.isfile(root_path):
        raise RuntimeError(f"找不到草稿箱索引: {root_path}")
    with open(root_path, "r", encoding="utf-8") as f:
        root = json.load(f)

    store = root.setdefault("all_draft_store", [])
    before = len(store)
    root["all_draft_store"] = [e for e in store
                               if e.get("draft_name") != meta["draft_name"]]
    replaced = before - len(root["all_draft_store"])
    root["all_draft_store"].append(meta)
    if not replaced:
        root["draft_ids"] = root.get("draft_ids", 0) + 1
    root.setdefault("draft_ids", len(root["all_draft_store"]))
    root.setdefault("root_path", draft_root.replace("\\", "/"))

    with open(root_path, "w", encoding="utf-8") as f:
        json.dump(root, f, ensure_ascii=False, indent=4)
    return bool(replaced)


# ---------------- 剪映进程控制 ----------------
def is_jianying_running():
    try:
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe"],
                           capture_output=True, text=True, timeout=10,
                           encoding="utf-8", errors="ignore")
        return "JianyingPro.exe" in (p.stdout or "")
    except Exception:
        return False


def kill_jianying():
    """结束剪映。必须做：剪映退出时会把内存里的索引写回磁盘，
    覆盖掉我们刚注册的草稿条目。"""
    try:
        subprocess.run(["taskkill", "/F", "/IM", "JianyingPro.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15)
        time.sleep(1.2)
        return True
    except Exception:
        return False


def launch_jianying(install_dir):
    exe = os.path.join(install_dir, "JianyingPro.exe")
    if not os.path.isfile(exe):
        raise RuntimeError(f"找不到剪映程序: {exe}")
    subprocess.Popen([exe], cwd=install_dir,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
