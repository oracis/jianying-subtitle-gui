# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

设计取向（用户明确要求）：
  * **模型不绑进 exe**：首次转换时按需下载到 %LOCALAPPDATA%\\JianyingSubtitle\\models
  * exe 只装程序 + 运行时依赖，体积可控
  * ffmpeg 随程序分发（没有它完全无法解码，属于刚需）

打包命令：
  pyinstaller build.spec --noconfirm
产物：
  dist/JianyingSubtitleGUI/JianyingSubtitleGUI.exe
"""
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP_DIR = os.path.dirname(os.path.abspath(SPEC))

# ---------- 数据文件 ----------
datas = [
    # 剪映草稿骨架（关键：保证 JSON 结构与剪映 6.x 兼容）
    (os.path.join(APP_DIR, "app", "assets", "draft_content_skeleton.json"), "app/assets"),
    (os.path.join(APP_DIR, "app", "assets", "draft_meta_info.json"), "app/assets"),
    # 术语包（领域错字 + 专名）。放在数据里而不是写死进代码，
    # 换题材时用户直接改 JSON 即可，不用重新打包 exe。
    (os.path.join(APP_DIR, "app", "assets", "terms"), "app/assets/terms"),
]

# faster-whisper 自带的 silero VAD 模型（vad_filter 需要）
# 注意：**不能**在这里 import faster_whisper 来定位——它顶层 import av 会失败，
# 异常被吞掉后 VAD 就静默漏打包（曾因此打包后运行报 NoSuchFile: silero_vad_v6.onnx）。
# 直接按文件系统路径查找，绕开 import。
import sysconfig

_vad_found = False
for _base in [sysconfig.get_paths().get("purelib", ""),
              sysconfig.get_paths().get("platlib", ""),
              os.path.join(sys.prefix, "Lib", "site-packages")]:
    if not _base:
        continue
    _vad = os.path.join(_base, "faster_whisper", "assets", "silero_vad_v6.onnx")
    if os.path.isfile(_vad):
        datas.append((_vad, "faster_whisper/assets"))
        print(f"[build] 打包 VAD 模型: {_vad}")
        _vad_found = True
        break
if not _vad_found:
    print("[build] 警告：未找到 silero_vad_v6.onnx，VAD 过滤将不可用！")

# onnxruntime 的 DLL
datas += collect_data_files("onnxruntime")

# zhconv 的繁简字典（544KB json，缺了会静默不做繁转简）
datas += collect_data_files("zhconv")

# ---------- ffmpeg（刚需，随程序分发） ----------
_ff_cands = [
    os.path.join(APP_DIR, "ffmpeg.exe"),
    r"C:/Users/DELL/tools/ffmpeg/bin/ffmpeg.exe",
    r"C:/ffmpeg/bin/ffmpeg.exe",
    r"D:/ffmpeg/bin/ffmpeg.exe",
]
for _c in _ff_cands:
    if os.path.isfile(_c):
        datas.append((_c, "."))
        print(f"[build] 打包 ffmpeg: {_c}")
        break
else:
    print("[build] 警告：未找到 ffmpeg.exe，程序将依赖用户机器上的 ffmpeg")

# ---------- 隐藏导入 ----------
hiddenimports = [
    "faster_whisper",
    "faster_whisper.transcribe",
    "faster_whisper.audio",
    "faster_whisper.feature_extractor",
    "faster_whisper.tokenizer",
    "faster_whisper.utils",
    "faster_whisper.vad",
    "ctranslate2",
    "tokenizers",
    "onnxruntime",
    "onnxruntime.capi",
    "onnxruntime.capi.onnxruntime_pybind11_state",
    "huggingface_hub",
    "numpy",
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    # 本项目模块
    "config",
    "model_manager",
    "asr",
    "jianying",
    "resegment",
    "textpost",
    "core",
    # 繁简转换（纯 Python + 字典，580KB）
    "zhconv",
]

# ---------- 排除（缩小体积） ----------
excludes = [
    "av",                 # 我们用 stub 绕过，真 av 不需要
    "pyJianYingDraft",    # 草稿改为手写 JSON，不需要
    "pymediainfo",
    "imageio",
    "imageio_ffmpeg",
    "uiautomation",
    "comtypes",
    "matplotlib",
    "scipy",
    "pandas",
    "torch",              # FunASR 用的，本程序不用
    "funasr",
    "modelscope",
    "torchaudio",
    # sherpa-onnx 是调研时期试的中文专用模型后端，未接入主线：
    # 实测在 Skylake（无 VNNI）上 int8 推理反而比 whisper 慢，不打包
    "sherpa_onnx",
    "sherpa_onnx_core",
    "IPython",
    "pytest",
    "setuptools",
    "pip",
    # 以下为纯体积优化：本程序不做图像处理、不用 xet 传输、不用可视化面板
    "PIL",
    "Pillow",
    "hf_xet",             # 模型用 urllib 直连下载，不走 xet
    "tensorboardX",
    "soundfile",
    "tkinter.test",
    "test",
    "unittest",
    "pydoc_data",
]


a = Analysis(
    [os.path.join(APP_DIR, "app", "gui.py")],
    pathex=[os.path.join(APP_DIR, "app")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JianyingSubtitleGUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 窗口程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JianyingSubtitleGUI",
)
