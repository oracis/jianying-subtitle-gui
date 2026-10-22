# 剪映字幕粗剪工具（GUI）

拖一个音频/视频进窗口，自动生成**按语音停顿分段**的字幕，并直接放进剪映草稿箱。

## 为什么不把模型绑进 exe

exe 只装程序本体（约 60-70MB）。模型在**第一次转换时按需下载**，缓存到：

```
%LOCALAPPDATA%\JianyingSubtitle\models\faster-whisper-<档位>\
```

好处：
- 安装包小，启动快
- 可以随时换档位，不用重新打包
- 只用一次的人不必下载 484MB

## 模型档位怎么选（实测数据，务必先看）

| 档位 | 体积 | 29.5 分钟音频总耗时 | 中文准确率 | 说明 |
|---|---|---|---|---|
| base | 145MB | **约 3 分钟** | 差 | 快，但中文错字多，只适合快速看分段效果 |
| small | 484MB | 约 10 分钟 | 一般 | 折中 |

RTF = 识别耗时 / 音频时长。**核数越多越快**。

> **medium 已下架**（2026-10-22）：质量确实够（29.5 分钟音频约 46 分钟、错字仅 5~8 处），
> 但在 2 核 4 线程机器上跑一集要占掉整晚，性价比不可接受；留着档位只会让人误选后
> 静默下载 1.5GB。要跑大模型请换 8 核以上机器，并自行从 ModelScope 拉
> `gpustack/faster-whisper-medium`，再把 `MODEL_INFO` 里那一档加回来。

⚠️ 实测结论（2026-10-22，同一段 90 秒音频）：

- `base` 中文错字密集（"忠实"→"充实"、"畅销书"→"唱校书"、"刽子手"→"贵子手"），
  整段 90 秒里 40+ 处错误，**不适合直接出成品字幕**
- `medium` 仅 5~8 处，且大多是罕用人名/书名（质量够，但已因速度下架）
- `large-v3-turbo` 时延与 medium 相当，但中文反而略差（解码器只有 4 层），**不推荐**
- **热词表（专名提示）性价比最高**：把书名/人名填进「热词」，
  即使 base 也能把"爱情刽子手 / 当尼采哭泣 / 直视骄阳"这类词纠正对

### 中文场景的现实建议

2 核 4 线程的老笔记本跑不动 large 级模型。三条路：

1. **只求分段骨架** → 用 `base` + 热词表，3 分钟出稿，自己顺一遍错字
2. **要成品字幕** → 直接用剪映原生识别，或换台 8 核以上的机器跑 medium
3. **换引擎** → SenseVoice-Small（非自回归，230MB，中文专精，理论上能压到 base 的速度）；
   待做，见 Issues

## 用法

1. 双击 `JianyingSubtitleGUI.exe`
2. **拖一个**音频/视频进窗口 → 直接开始转换；**拖多个/整个文件夹** → 进入待转换队列，
   可以先增删改，确认后点「开始转换」
3. 首次会提示下载模型，等进度条走完
4. 转换中点「取消」可随时中止（当前批次推理结束后停下），想换文件直接再拖一个即可
5. 完成弹窗 → 打开剪映，草稿箱里就有新草稿

### 设置项

| 项 | 说明 |
|---|---|
| 草稿箱目录 | 剪映草稿箱路径，首次自动探测。通常形如 `...\JianyingPro\User Data\Projects\com.lveditor.draft` |
| 剪映安装目录 | 剪映程序目录（含 `JianyingPro.exe`），用于「完成后打开剪映」 |
| 模型档位 | base / small（medium 已按实测速度下架，见上方说明） |
| 热词 | 书名、人名等专名，逗号分隔。会作为 ASR 提示词注入，显著降低专名错字 |
| 画布 | 1920x1080（横屏）/ 1080x1920（竖屏）/ 3840x2160 |
| 生成前自动关闭剪映 | **建议保持勾选**，见下方注意事项 |

## ⚠️ 重要：剪映必须关闭

剪映运行时会把索引存在内存里，**退出时写回磁盘**，会覆盖掉我们刚写的新草稿条目
（草稿文件还在，但草稿箱里看不到）。

所以程序在生成草稿前会自动 `taskkill` 剪映——这也是「生成前自动关闭剪映」选项的作用。

## 分段是怎么做的

不是自己造轮子，走的是业界标准链路：

1. **ASR** — faster-whisper（本地 CPU，int8 量化，批量推理加速）
2. **找停顿** — `ffmpeg silencedetect=noise=-32dB:d=0.18`
3. **切分文本** — 用 whisper 字级时间戳对齐到真实停顿，**只在停顿处断条**，
   绝不硬切词中间；输出**不带标点**（与剪映原生字幕一致，中文视频字幕惯例）
4. **对齐时间轴** — 对每个 ASR 段，用其**局部语速**把文本块拟合到该段内的「发声块」上，
   使字幕边界精确落在真实停顿处
5. **清洗** — 合并过短碎片（<0.5s 且 <12字），强制时间轴严格单调无重叠

参考规则集：Whisper `SubtitlesWriter.iterate_result()`、Netflix 字幕规范
（每行 ≤42 字符 / ≤2 行 / ≤20cps / 单条 5~7s / 停顿 ≥1s 断句）。

实测这份 27 分钟音频：421 条字幕，平均 3.45s，**100% 边界落在真实停顿上**，0 重叠。

## ffmpeg 依赖

程序需要 ffmpeg 解码音频。查找顺序：

1. exe 同级的 `ffmpeg.exe` / `bin\ffmpeg.exe`
2. PATH 里的 ffmpeg（**会验证可执行**，跳过 scoop 等坏 shim）
3. 常见安装位置 `C:\ffmpeg\bin`、`D:\ffmpeg\bin` 等

找不到会明确报错。建议把 `ffmpeg.exe` 直接放在 exe 同目录。

## 源码结构

```
jianying-subtitle-gui/
├── app/
│   ├── gui.py                 Tkinter 界面 + Windows 原生拖拽（ctypes，零依赖）
│   ├── core.py                流程编排（ASR→停顿→分段→草稿）
│   ├── asr.py                 faster-whisper 封装（绕过 av 依赖）
│   ├── resegment.py           停顿对齐分段算法（核心）
│   ├── jianying.py            草稿 JSON 生成 + 草稿箱注册
│   ├── model_manager.py       模型按需下载
│   ├── config.py              配置持久化 + 路径自动探测
│   └── assets/                草稿骨架模板
└── build.spec                 PyInstaller 配置
```

## 自己打包

```bash
pip install pyinstaller
cd jianying-subtitle-gui
pyinstaller build.spec --noconfirm
# 产物：dist/JianyingSubtitleGUI/JianyingSubtitleGUI.exe
```

打包要点：
- 排除 `av`（用 `sys.modules` 注入 stub 绕过，因为它只被 `decode_audio()` 用，
  而我们自己用 ffmpeg+wave+numpy 解码）
- 排除 `pyJianYingDraft` / `pymediainfo` / `uiautomation`（草稿改为手写 JSON）
- 带上 `faster_whisper/assets/silero_vad_v6.onnx`（VAD 需要）
