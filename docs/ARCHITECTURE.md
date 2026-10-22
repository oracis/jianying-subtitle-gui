# 架构文档：剪映字幕粗剪工具

| 项 | 内容 |
|---|---|
| 版本 | v0.9 |
| 日期 | 2026-10-21 |
| 形态 | Windows 桌面应用（PyInstaller onedir）+ CLI |

---

## 1. 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│  表现层  gui.py (Tkinter)                                     │
│  ├─ 拖拽区（Windows 原生 Shell API，ctypes，零第三方依赖）      │
│  ├─ 进度条 / 日志 / 设置面板                                   │
│  └─ 线程 + 队列（worker 不阻塞 UI，queue 回传进度）              │
├──────────────────────────────────────────────────────────────┤
│  编排层  core.py                                              │
│  run(): 模型就绪 → 解码 → ASR → 停顿检测 → 重分段 → 建草稿       │
│  统一 progress_cb(stage, pct, msg) 上报，支持 cancel_flag       │
├──────────────────────────────────────────────────────────────┤
│  领域层                                                        │
│  ├─ asr.py           faster-whisper 封装（绕过 av 依赖）        │
│  ├─ resegment.py     停顿对齐分段算法（核心 IP）                │
│  └─ jianying.py      草稿 JSON 生成 + 草稿箱注册 + 进程控制      │
├──────────────────────────────────────────────────────────────┤
│  基础设施层                                                     │
│  ├─ model_manager.py 模型按需下载（多源回退 / 断点续传 / 校验）   │
│  ├─ config.py        配置持久化 + 剪映路径自动探测               │
│  └─ assets/          草稿 JSON 骨架模板                        │
├──────────────────────────────────────────────────────────────┤
│  外部依赖                                                       │
│  ├─ ffmpeg.exe       解码/静音检测（随包分发）                   │
│  ├─ ctranslate2      whisper 推理引擎（CPU int8）               │
│  ├─ onnxruntime      silero VAD                                │
│  └─ 模型文件          运行时下载，不打包                         │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. 模块职责

| 模块 | 职责 | 关键点 |
|---|---|---|
| `gui.py` | 界面、拖拽、线程调度、命令行入口 | 拖拽用原生 API；`--selftest` / `--convert` 两个非 GUI 入口 |
| `core.py` | 流程编排、进度上报、取消 | 所有阶段计时，产出 timings 便于性能分析 |
| `asr.py` | 音频解码 + whisper 推理 | **注入 av stub**；ffmpeg 候选需实跑验证 |
| `resegment.py` | 文本切分 + 时间轴对齐 | **纯算法，无外部依赖**，易测试 |
| `jianying.py` | 草稿 JSON 生成、注册、剪映进程控制 | 基于官方骨架填充；写前 kill 剪映 |
| `model_manager.py` | 模型下载与校验 | 不依赖 huggingface_hub 缓存语义 |
| `config.py` | 配置读写 + 路径探测 | 首次启动自动填 |

---

## 3. 数据流

```
用户拖入 audio.m4a
       │
       ▼
[1] 确保模型就绪          model_manager.ensure_model()
       │                  缺则下载到 %LOCALAPPDATA%\JianyingSubtitle\models\
       ▼
[2] 解码为 16k 单声道 WAV  ffmpeg -ar 16000 -ac 1 -c:a pcm_s16le
       │
       ▼
[3] 读成 float32 数组      wave (stdlib) + numpy
       │                  ← 这一步替代了 av 的 decode_audio
       ▼
[4] ASR                    faster-whisper (CPU int8, vad_filter=True)
       │                   产出 [(start_s, end_s, text), ...]
       ▼
[5] 静音检测               ffmpeg silencedetect=noise=-32dB:d=0.18
       │                   产出静音区间 [(sil_start, sil_end), ...]
       ▼
[6] 重分段（核心）          resegment.resegment()
       │                   文本只在标点处切 → 拟合到发声块 → 边界落在停顿
       ▼
[7] 输出 SRT               <音频同名>.srt（备查/二次分发）
       │
       ▼
[8] 生成草稿 JSON          jianying.build_draft()
       │                   骨架模板 + 音频轨 + 字幕轨 + 封面
       ▼
[9] 注册草稿箱             root_meta_info.json → all_draft_store
       │
       ▼
剪映草稿箱出现新草稿
```

### 阶段耗时（27.75 分钟音频，base 模型，i7-6600U：2 物理核 / 4 逻辑线程）

实测环境：2015 款 Skylake U 系列，无独显，无 VNNI 指令。

| 阶段 | 耗时 | 占比 |
|---|---|---|
| 模型就绪 | 0s（已缓存） | — |
| 解码 16k 单声道 | 6.3s | 3.7% |
| **ASR（批量 b=8）** | **159.9s** | **94.3%** |
| **字级重分段** | **0.02s** | **0.01%** |
| 建草稿 | 0.7s | 0.4% |
| **合计（墙钟）** | **169.9s = 2.83 分钟** | |

> **架构含义**：性能优化只有一条路 —— 加 ASR 速度。分段本身是 0.02 秒，
> 无优化空间。任何「优化分段」的努力都是无效的。

### 已做过的提速（全部免费，均实测）

| 手段 | 效果 | 说明 |
|---|---|---|
| 批量推理 `batch_size=8` | **1.68x** | 同一模型，VAD 多段拼 batch 送 encoder |
| `cpu_threads` = 物理核数(2) | 1.7x | 原按逻辑线程(4)设，超线程争抢反而慢 |
| ~~`hotwords` 热词~~ | 负收益 | 没修好错字，还引入新错（形成一条→有此形成立条） |
| ~~`batch_size=16`~~ | 变慢 | 2 核吃不下大 batch |
| ~~`chunk_length=15`~~ | 变慢 | 切太碎 |

`cpu_threads` 实测（240s 片段，base + 批量）：

| threads | 墙钟 | CPU 时间 | 并行度 | RTF |
|---|---|---|---|---|
| 1 | 34.1s | 30.2s | 0.89x | 0.142 |
| **2** | **22.5s** | 39.0s | **1.73x** | **0.0937** |
| 3 | 25.8s | 56.5s | 2.19x | 0.108 |
| 4 | 38.3s | 60.5s | 1.58x | 0.160 |

threads=2 时并行度 1.73x 说明**两个物理核已压满**，再加进程并行无收益。
**RTF 0.094 是本机物理上限。**

### 调研后放弃的替代引擎

| 方案 | 中文质量 | 本机速度 | 放弃原因 |
|---|---|---|---|
| SenseVoice-Small (sherpa-onnx int8) | **最好**（CER 3.6%，自带标点） | RTF 0.13→0.23 超线性 | Skylake 无 VNNI，int8 量化走软件模拟，量化优势全废 |
| Paraformer-zh (sherpa-onnx int8) | 好（CER 3.95%，无标点） | RTF 0.19–0.21 | 同上，且 2 进程并行仅 1.37x |
| Groq Whisper large-v3（云端） | 很好 | **约 8 秒** | 免费层可用但需注册海外账号，用户明确不接受 |

> 中文专用模型的质量优势是真实的（同段音频 SenseVoice 输出「毒品泛滥的重灾区」
> 「毒贩子都是既狡猾又残忍」，whisper base 是「贩漫」「技巧华」）。
> 若将来换到支持 VNNI 的 CPU（10 代以上），这两条路可直接启用。

---

## 4. 核心算法：停顿对齐分段

`resegment.py`，纯函数、无 IO、可单测。

### 4.1 步骤

1. **发声块（burst）**：静音区间之间的有声段。每个 burst 边界 = 一个真实语音停顿
2. **文本切分**：只在真实标点处切（句末标点 `。！？` 优先，其次 `，、；：`），
   **绝不硬切词中间**；超长句在 `[0.45×max, 1.45×max]` 窗口内找最靠前的小句标点
3. **语速拟合**：对每个 ASR 段，用**该段自身的局部语速**（字数/发声时长），
   为每个文本块挑选「累积发声时长最接近期望」的 burst 数量
4. **时间轴还原**：第 j 块结束 = 第 j+1 块开始（连续无缝），边界因此必然落在 burst 边界即停顿上
5. **清洗**：合并过短碎片（<0.5s 且 <12 字），强制严格单调无重叠

### 4.2 为什么不用纯字符数均分

纯按字数均分会让字幕边界落在词中间，且与语音节奏脱节。
实测：按字数均分时「最严重的社 / 会问题之一」会被切开；用局部语速拟合后不会。

### 4.3 业界对照

| 参考 | 采用的部分 |
|---|---|
| OpenAI Whisper `SubtitlesWriter.iterate_result()` | 词间空隙超阈值断句的思路 |
| Netflix 字幕规范（labvent 规则集） | 每行 ≤42 字符 / ≤2 行 / ≤20cps / 单条 5~7s / 停顿 ≥1s 断句 |
| FunASR `paraformer-zh + fsmn-vad + ct-punc` | 中文标准栈的对照（本方案用 whisper + ffmpeg VAD 替代） |

### 4.4 实测结果

| 数据集 | 输入段 | 输出条数 | 平均 | 中位 | 最长 | 边界命中停顿 | 异常 |
|---|---|---|---|---|---|---|---|
| 黑金世界（远端ASR，带标点） | 209 | 421 | 3.45s | 3.34s | 6.89s | **100%** | 0 |
| 黑金世界（本地base，无标点） | 468 | 499 | 2.97s | 2.89s | 7.09s | 由算法保证 | 0 |

> 注意第二行：ASR 段数（468）与输出条数（499）接近，说明**无标点时细分能力退化**。
> 这印证了「标点恢复」（PRD F16）的必要性。

---

## 5. 剪映草稿格式

### 5.1 为什么手写 JSON 而不用 pyJianYingDraft

| | pyJianYingDraft | 手写 |
|---|---|---|
| 依赖 | 连带 pymediainfo / imageio / uiautomation | 无 |
| py3.13 兼容 | uiautomation 装不上，需桩模块 | 无问题 |
| 打包体积 | 大 | 小 |
| 可控性 | 黑盒 | 完全可控 |

**做法**：用 pyJianYingDraft 生成一份真实草稿 → 清空素材 → 存为
`app/assets/draft_content_skeleton.json`（28 顶层键 / 46 materials 键）→ 运行时在骨架上填充。

实测：与 pyJianYingDraft 产物**顶层键、materials 键、片段字段零差异**。

### 5.2 关键字段

| 字段 | 值 | 说明 |
|---|---|---|
| `canvas_config` | 1920×1080 / ratio 16:9 | 横屏 |
| `materials.audios[0].path` | 原音频绝对路径 | **移动音频会断链** |
| 文本片段 `clip.transform.y` | `-0.8` | **归一化坐标**（不是像素），画面下方 |
| 音频片段 `extra_material_refs` | 指向一个 `speeds` 素材 | 必须配 |
| `tracks[].is_default_name` | false | 轨道需带此字段 |

### 5.3 草稿箱注册

`root_meta_info.json`（明文索引）：

```json
{
  "all_draft_store": [ { ...草稿条目... } ],
  "draft_ids": N,
  "root_path": "..."
}
```

规则：
- 按 `draft_name` 先剔除同名旧项再 append（避免草稿箱重名）
- 未替换时 `draft_ids + 1`

**⚠️ 必须写前关闭剪映**：剪映运行时把索引存在内存，退出时写回磁盘，会覆盖新条目。

---

## 6. 关键技术决策

| # | 决策 | 理由 | 踩过的坑 |
|---|---|---|---|
| D1 | 模型不打包，运行时下载 | exe 12MB vs 500MB | — |
| D2 | 注入 `sys.modules['av']` stub | `faster_whisper.audio` 顶层 `import av`，但 av 只被 `decode_audio()` 用；我们传 numpy 数组绕过 | av 在 py3.13 无 13.x wheel；19.x 与 13.x 混装导致 pyd 冲突 |
| D3 | 自己 ffmpeg+wave+numpy 解码 | 去掉 av 依赖，打包更干净 | — |
| D4 | 草稿 JSON 手写 + 官方骨架 | 去掉 3 个重依赖 | 凭空构造的 JSON 键严重不全，剪映会拒绝 |
| D5 | Windows 原生拖拽（ctypes） | 零第三方依赖 | **必须用 `SetWindowLongPtrW`**；`SetWindowLongW` 第三参是 32 位 LONG，放 8 字节回调指针会 OverflowError。另需用 `root.frame()` 取顶层 HWND |
| D6 | ffmpeg 候选必须实跑验证 | scoop 的 shim 会假成功 | `shutil.which("ffmpeg")` 会命中坏 shim，报 "Could not determine if target is a GUI app" |
| D7 | spec 里用 `sysconfig` 定位 VAD 模型 | **不能** `import faster_whisper`（会因缺 av 失败） | 异常被 `except: pass` 吞掉 → VAD 静默漏打包 → 运行时 `NoSuchFile` |
| D8 | 生成前 `taskkill` 剪映 | 防索引覆盖 | 用户手动删草稿后索引仍在，需注意 |
| D9 | PyInstaller onedir（非 onefile） | 启动快、便于替换 ffmpeg | onefile 每次解压 281MB 太慢 |

---

## 7. 打包结构

```
dist/JianyingSubtitleGUI/          281 MB
├── JianyingSubtitleGUI.exe         12 MB   入口（windowed，无黑框）
└── _internal/                    269 MB
    ├── ffmpeg.exe                 98 MB   解码刚需
    ├── ctranslate2/               57 MB   推理引擎
    ├── onnxruntime/               33 MB   VAD
    ├── numpy.libs/                20 MB   线性代数
    ├── faster_whisper/assets/
    │   └── silero_vad_v6.onnx     1.2 MB  VAD 模型（必须显式打包）
    ├── app/assets/                       草稿骨架模板
    └── ... 其余 Python 运行时
```

**不打包**：模型（455MB~3GB，运行时下载）、PIL、hf_xet、torch、FunASR、pyJianYingDraft

---

## 8. 运行时目录

```
%LOCALAPPDATA%\JianyingSubtitle\
├── config.json          配置（剪映路径、档位、画布）
├── selftest.log         自检结果
├── convert.log          CLI 转换日志
└── models\
    └── faster-whisper-<base|small>\
        ├── config.json
        ├── model.bin
        ├── tokenizer.json
        └── vocabulary.txt
```

---

## 9. 环境约束与已知限制

| 限制 | 现状 | 影响 |
|---|---|---|
| CPU | 4 核 | small 跑 27 分钟音频需 10 分钟 |
| GPU | Intel HD 520 核显，**无 CUDA** | 无法 GPU 加速，大模型不可行 |
| 中文识别质量 | base 错字明显，small 较好，**均有繁体残留** | 需 F14/F15/F16 |
| 剪映版本 | 6.0.1.11779 | 其他版本需验证 JSON 兼容性 |
| 音频路径 | 草稿引用原绝对路径 | 移动/删除源文件会导致素材丢失 |

---

## 10. 扩展点

| 扩展 | 落点 |
|---|---|
| 换 ASR 引擎（如 FunASR） | 替换 `asr.Transcriber`，保持 `[(start,end,text)]` 契约 |
| 换分段策略 | `resegment.resegment()` 是纯函数，可直接替换 |
| 支持剪映新版本 | 替换 `assets/draft_content_skeleton.json` |
| 加 GPU | `asr.Transcriber.load()` 里检测 CUDA 并切 `device="cuda"` |
| 加标点恢复 | 在 `core.run()` 的 ASR 之后、resegment 之前插一层 |
