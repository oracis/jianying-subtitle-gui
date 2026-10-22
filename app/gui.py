# -*- coding: utf-8 -*-
"""剪映字幕粗剪工具 —— Tkinter GUI 主程序。

设计取向（用户偏好）：
  * 普通人打开即用：拖音频进去就开始，不需要命令行
  * 零第三方 GUI 依赖：拖拽用 Windows 原生 Shell API（ctypes），不用 tkinterdnd2
  * 路径可设置：剪映安装目录 / 草稿箱目录都能改，首次自动探测

界面结构：
  ┌─────────────────────────────────────┐
  │  [拖拽区：把音频拖到这里]            │
  │  或 [选择文件]                       │
  ├─────────────────────────────────────┤
  │  进度条 + 状态文字                   │
  ├─────────────────────────────────────┤
  │  设置：草稿目录 / 安装目录 / 模型档位 │
  ├─────────────────────────────────────┤
  │  日志区                              │
  └─────────────────────────────────────┘
"""
import os
import queue
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 兼容直接运行与打包运行
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config as cfgmod
import core
import model_manager
import jianying
import asr

APP_NAME = "剪映字幕粗剪"
APP_VER = "1.0"

AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".wma",
              ".mp4", ".mkv", ".mov", ".avi", ".flv", ".ts", ".m4v", ".webm")


# ---------------- Windows 原生拖拽（零依赖） ----------------
def enable_drop(widget, on_drop):
    """用 Shell API 让 Tk 顶层窗口接受文件拖放。仅 Windows 有效。

    64 位注意：必须用 SetWindowLongPtrW 才能放下 8 字节回调指针，
    用 SetWindowLongW 会 OverflowError（这是最初的 bug）。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        is64 = ctypes.sizeof(ctypes.c_void_p) == 8

        # 选对 API 并声明签名（不声明会按 int 处理导致指针截断）
        _SetWndProc = getattr(user32, "SetWindowLongPtrW", None) if is64 else None
        if _SetWndProc is None:
            _SetWndProc = user32.SetWindowLongW
        _SetWndProc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        _SetWndProc.restype = ctypes.c_void_p

        _CallWndProc = user32.CallWindowProcW
        _CallWndProc.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                                 wintypes.WPARAM, wintypes.LPARAM]
        _CallWndProc.restype = ctypes.c_void_p

        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                           wintypes.LPWSTR, wintypes.UINT]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragFinish.argtypes = [wintypes.HANDLE]

        WM_DROPFILES = 0x0233
        GWL_WNDPROC = -4

        # 取真正的顶层 HWND：winfo_id() 是子窗口，frame() 给的是顶层
        widget.update_idletasks()
        try:
            fr = widget.frame()
            hwnd = int(fr, 16) if isinstance(fr, str) else int(fr)
        except Exception:
            hwnd = widget.winfo_id()
            parent = user32.GetParent(hwnd)
            if parent:
                hwnd = parent

        shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                                     wintypes.WPARAM, wintypes.LPARAM)
        state = {"old": None}

        def _proc(h, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                try:
                    hdrop = wintypes.HANDLE(wparam)
                    n = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                    files = []
                    for i in range(n):
                        ln = shell32.DragQueryFileW(hdrop, i, None, 0)
                        buf = ctypes.create_unicode_buffer(ln + 1)
                        shell32.DragQueryFileW(hdrop, i, buf, ln + 1)
                        files.append(buf.value)
                    shell32.DragFinish(hdrop)
                    if files:
                        on_drop(files)
                except Exception:
                    pass
                return 0
            return _CallWndProc(state["old"], h, msg, wparam, lparam)

        cb = WNDPROC(_proc)
        old = _SetWndProc(wintypes.HWND(hwnd), GWL_WNDPROC,
                          ctypes.cast(cb, ctypes.c_void_p))
        state["old"] = old

        # 防回调被 GC 回收
        widget._drop_proc = cb
        widget._drop_proc_state = state
        widget._drop_hwnd = hwnd
        return True
    except Exception:
        return False


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = cfgmod.load()
        self.q = queue.Queue()
        self.worker = None
        self.cancel_flag = False
        self.last_result = None
        self.current_file = None
        self.pending_file = None  # 转换中被拖入的新文件：取消完成后自动开始
        self.queue_files = []     # 待转换队列（多文件/文件夹拖入，手动开始）
        self.batch_running = False
        self.made_drafts = []     # 批量时本轮已生成的草稿名
        self.done_paths = []      # 批量时本轮已成功的文件路径

        root.title(f"{APP_NAME} v{APP_VER}")
        root.geometry("720x680")
        root.minsize(640, 600)

        self._build_ui()
        self._apply_cfg_to_ui()
        self._poll_queue()

        # 挂拖拽
        ok = enable_drop(root, self._on_drop)
        if not ok:
            self.log("提示：当前系统不支持原生拖拽，请用「选择文件」按钮。")

    # ---------- UI ----------
    def _build_ui(self):
        pad = dict(padx=10, pady=6)

        # 拖拽区
        self.drop_frame = tk.Frame(self.root, bg="#1f2a44", height=150,
                                   highlightthickness=2,
                                   highlightbackground="#3d5a99")
        self.drop_frame.pack(fill=tk.X, **pad)
        self.drop_frame.pack_propagate(False)

        self.drop_label = tk.Label(
            self.drop_frame,
            text="把音频 / 视频拖到这里\n\n"
                 "单个文件：拖入即开始\n"
                 "多个文件 / 文件夹：先入队，确认后点「开始转换」\n\n"
                 "支持 mp3 m4a wav flac mp4 mkv mov …",
            bg="#1f2a44", fg="#9fb6e0", font=("Microsoft YaHei UI", 12),
            justify="center")
        self.drop_label.pack(expand=True)

        # 待转换队列：拖入多个文件或文件夹时出现，未开始前可增删。
        # 单个文件仍是「拖入即开始」，不进队列。
        self.queue_box = ttk.LabelFrame(self.root, text="待转换队列")
        self.lst_queue = tk.Listbox(self.queue_box, height=4, selectmode=tk.EXTENDED,
                                    font=("Microsoft YaHei UI", 9))
        self.lst_queue.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                            padx=(8, 0), pady=6)
        _sbq = ttk.Scrollbar(self.queue_box, command=self.lst_queue.yview)
        _sbq.pack(side=tk.RIGHT, fill=tk.Y, pady=6, padx=(0, 2))
        self.lst_queue.configure(yscrollcommand=_sbq.set)
        _qbtns = tk.Frame(self.queue_box)
        _qbtns.pack(side=tk.RIGHT, fill=tk.Y, padx=6, pady=6)
        self.btn_q_remove = ttk.Button(_qbtns, text="移除选中", width=9,
                                       command=self.queue_remove)
        self.btn_q_remove.pack(pady=2)
        self.btn_q_clear = ttk.Button(_qbtns, text="清空", width=9,
                                      command=self.queue_clear)
        self.btn_q_clear.pack(pady=2)

        btn_row = tk.Frame(self.root)
        btn_row.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Button(btn_row, text="选择文件…", command=self.choose_file).pack(side=tk.LEFT)
        self.btn_open = ttk.Button(btn_row, text="打开草稿箱目录",
                                   command=self.open_draft_dir)
        self.btn_open.pack(side=tk.LEFT, padx=6)
        self.btn_start = ttk.Button(btn_row, text="开始转换", command=self.start)
        self.btn_start.pack(side=tk.RIGHT)
        # 取消：只在转换中可用（拖入即开始后，必须有中止手段）
        self.btn_cancel = ttk.Button(btn_row, text="取消", command=self.cancel_convert,
                                     state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.RIGHT, padx=6)

        # 进度
        prog = tk.Frame(self.root)
        prog.pack(fill=tk.X, padx=10)
        self.pbar = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.pbar.pack(fill=tk.X)
        self.status = tk.Label(prog, text="等待文件…", anchor="w",
                               font=("Microsoft YaHei UI", 9))
        self.status.pack(fill=tk.X, pady=(2, 0))

        # 设置区
        box = ttk.LabelFrame(self.root, text="设置")
        box.pack(fill=tk.X, padx=10, pady=8)

        grid = tk.Frame(box)
        grid.pack(fill=tk.X, padx=8, pady=6)
        grid.columnconfigure(1, weight=1)

        tk.Label(grid, text="草稿箱目录").grid(row=0, column=0, sticky="w", pady=2)
        self.var_draft = tk.StringVar()
        ttk.Entry(grid, textvariable=self.var_draft).grid(row=0, column=1,
                                                          sticky="ew", padx=6)
        ttk.Button(grid, text="浏览", width=6,
                   command=lambda: self._pick_dir(self.var_draft)).grid(row=0, column=2)

        tk.Label(grid, text="剪映安装目录").grid(row=1, column=0, sticky="w", pady=2)
        self.var_install = tk.StringVar()
        ttk.Entry(grid, textvariable=self.var_install).grid(row=1, column=1,
                                                            sticky="ew", padx=6)
        ttk.Button(grid, text="浏览", width=6,
                   command=lambda: self._pick_dir(self.var_install)).grid(row=1, column=2)

        row2 = tk.Frame(box)
        row2.pack(fill=tk.X, padx=8, pady=(0, 6))

        tk.Label(row2, text="模型档位").pack(side=tk.LEFT)
        self.var_model = tk.StringVar(value="base")
        self.cmb_model = ttk.Combobox(
            row2, textvariable=self.var_model, state="readonly", width=26,
            values=[f"{k}  {v['label']}" for k, v in model_manager.MODEL_INFO.items()])
        self.cmb_model.pack(side=tk.LEFT, padx=6)

        tk.Label(row2, text="画布").pack(side=tk.LEFT, padx=(12, 0))
        self.var_canvas = tk.StringVar(value="1920x1080")
        ttk.Combobox(row2, textvariable=self.var_canvas, state="readonly", width=12,
                     values=["1920x1080", "1080x1920", "1280x720", "3840x2160"]
                     ).pack(side=tk.LEFT, padx=6)

        # 热词：书名/人名等专名，注入 ASR 提示词，显著降低专名错字（2026-10-22 加）
        row_hot = tk.Frame(box)
        row_hot.pack(fill=tk.X, padx=8, pady=(0, 6))
        tk.Label(row_hot, text="热词").pack(side=tk.LEFT)
        self.var_terms = tk.StringVar()
        ttk.Entry(row_hot, textvariable=self.var_terms).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        tk.Label(row_hot, text="书名/人名，逗号分隔", fg="#8a93a6",
                 font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)

        row3 = tk.Frame(box)
        row3.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.var_kill = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="生成前自动关闭剪映（避免草稿被覆盖）",
                        variable=self.var_kill).pack(side=tk.LEFT)
        self.var_launch = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="完成后打开剪映",
                        variable=self.var_launch).pack(side=tk.LEFT, padx=12)

        # 日志
        logbox = ttk.LabelFrame(self.root, text="日志")
        logbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.txt = tk.Text(logbox, height=9, wrap=tk.WORD,
                           font=("Consolas", 9), bg="#11151f", fg="#c9d6e8",
                           insertbackground="#c9d6e8")
        self.txt.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb = ttk.Scrollbar(logbox, command=self.txt.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt.configure(yscrollcommand=sb.set)

    def _apply_cfg_to_ui(self):
        self.var_draft.set(self.cfg.get("draft_dir", ""))
        self.var_install.set(self.cfg.get("install_dir", ""))
        self.var_canvas.set(self.cfg.get("canvas", "1920x1080"))
        self.var_terms.set(self.cfg.get("terms", "") or "")
        ms = self.cfg.get("model_size", "base")
        for k, v in model_manager.MODEL_INFO.items():
            if k == ms:
                self.var_model.set(f"{k}  {v['label']}")
                break
        self.var_kill.set(bool(self.cfg.get("kill_jianying_first", True)))

    def _sync_cfg(self):
        m = self.var_model.get().split()[0] if self.var_model.get() else "base"
        self.cfg.update({
            "draft_dir": self.var_draft.get().strip(),
            "install_dir": self.var_install.get().strip(),
            "model_size": m,
            "canvas": self.var_canvas.get().strip() or "1920x1080",
            "terms": self.var_terms.get().strip(),
            "kill_jianying_first": bool(self.var_kill.get()),
        })
        try:
            cfgmod.save(self.cfg)
        except Exception as e:
            self.log(f"配置保存失败: {e}")

    # ---------- 工具 ----------
    def log(self, msg):
        self.txt.insert(tk.END, msg + "\n")
        self.txt.see(tk.END)

    def _pick_dir(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or "C:/")
        if d:
            var.set(d.replace("/", "\\"))
            self._sync_cfg()

    def open_draft_dir(self):
        d = self.var_draft.get()
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showwarning(APP_NAME, "草稿目录不存在")

    # ---------- 拖拽 / 选择 ----------
    def _on_drop(self, files):
        # 本函数在 WNDPROC 回调里执行：Windows 会在 Tk 主循环
        # （Tcl_DoOneEvent，GIL 已释放）执行期间重入这里。
        # 此处绝不能碰任何 Tk 控件或 root.after——重入 Tk 会触发
        # PyEval_RestoreThread 致命错误（进程 0xC0000409 崩溃）。
        # 只做纯 Python 操作，把路径丢进队列，由主循环的
        # _poll_queue（120ms 轮询）取出后再更新 UI。
        if files:
            # 传整个列表（可能是多选文件或文件夹），在轮询里再展开处理
            self.q.put(("drop", list(files)))

    def choose_file(self):
        fs = filedialog.askopenfilenames(
            title="选择音频或视频（可多选）",
            filetypes=[("音视频", " ".join("*" + e for e in AUDIO_EXTS)),
                       ("所有文件", "*.*")])
        if fs:
            self._handle_drop(list(fs))

    # ---------- 队列（多文件 / 文件夹） ----------
    def _handle_drop(self, paths):
        """单个文件 -> 立即开始；多个文件或文件夹 -> 入队等手动开始。"""
        media, had_dir = [], False
        for p in paths:
            if os.path.isdir(p):
                had_dir = True
                for root_, _, fs in os.walk(p):
                    for f in sorted(fs):
                        if f.lower().endswith(AUDIO_EXTS):
                            media.append(os.path.join(root_, f))
            elif p.lower().endswith(AUDIO_EXTS):
                media.append(p)
            else:
                self.log(f"忽略非音视频文件: {os.path.basename(p)}")
        if not media:
            if paths:
                messagebox.showinfo(APP_NAME, "没有找到音频/视频文件")
            return

        if len(media) == 1 and not had_dir:
            self._set_file(media[0])   # 单文件：原来的拖入即开始
            return

        # 多文件 / 文件夹：进队列，等用户点「开始转换」
        added = 0
        for m in media:
            if m not in self.queue_files:
                self.queue_files.append(m)
                added += 1
        self._refresh_queue()
        self.log(f"已加入队列 {added} 个文件（共 {len(self.queue_files)} 个），"
                 f"确认后点「开始转换」")
        if self.batch_running:
            self.log("当前正在转换，新加入的文件会在本轮之后生效（需重新点开始）")

    def _refresh_queue(self):
        n = len(self.queue_files)
        self.lst_queue.delete(0, tk.END)
        for i, f in enumerate(self.queue_files, 1):
            try:
                size_mb = os.path.getsize(f) / 1024 / 1024
                self.lst_queue.insert(tk.END, f"{i}. {os.path.basename(f)}  ({size_mb:.1f}MB)")
            except OSError:
                self.lst_queue.insert(tk.END, f"{i}. {os.path.basename(f)}")
        if n:
            if not self.queue_box.winfo_ismapped():
                self.queue_box.pack(fill=tk.X, padx=10, pady=(0, 6),
                                    after=self.drop_frame)
            self.btn_start.config(text=f"开始转换（{n} 个）")
            self.drop_label.config(text=f"已加入 {n} 个文件\n\n确认队列后点右侧「开始转换」")
        else:
            if self.queue_box.winfo_ismapped():
                self.queue_box.pack_forget()
            if not (self.worker and self.worker.is_alive()):
                self.btn_start.config(text="开始转换")

    def queue_remove(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "转换中不能改队列，可先点「取消」")
            return
        sel = list(self.lst_queue.curselection())
        for i in reversed(sel):
            if 0 <= i < len(self.queue_files):
                self.log(f"移出队列: {os.path.basename(self.queue_files.pop(i))}")
        self._refresh_queue()

    def queue_clear(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "转换中不能改队列，可先点「取消」")
            return
        self.queue_files.clear()
        self._refresh_queue()
        self.drop_label.config(text="把音频 / 视频拖到这里\n\n"
                                    "支持 mp3 m4a wav flac mp4 mkv mov …\n"
                                    "多个文件或文件夹会进入队列")

    def _set_file(self, path):
        # 拖入即开始（用户拍板）：但正在转换时不能直接抢——先取消当前任务，
        # 把新文件记为 pending，等取消生效后由 _poll_queue 自动接着开始。
        if self.worker and self.worker.is_alive():
            self.pending_file = path
            self.cancel_flag = True
            self.btn_cancel.config(state=tk.DISABLED)
            self.log(f"发现新文件，正在中止当前任务：{os.path.basename(path)}")
            self.status.config(text="正在取消当前任务，随后自动开始新文件…")
            return
        self.current_file = path
        name = os.path.basename(path)
        self.drop_label.config(text=f"已选择：\n{name}\n\n正在自动转换，可点「取消」中止")
        self.status.config(text=f"待处理：{name}")
        self.log(f"选择文件: {path}")
        self.start()

    def cancel_convert(self):
        """中止当前转换（ASR 每条字幕回调时检查，1-3 秒内生效）。"""
        if not (self.worker and self.worker.is_alive()):
            return
        self.cancel_flag = True
        self.btn_cancel.config(state=tk.DISABLED)
        self.status.config(text="正在取消…")
        self.log("用户取消，正在中止…")

    # ---------- 执行 ----------
    def start(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "正在转换中，请稍候")
            return
        # 队列里有多文件 -> 批量；否则单文件
        if self.queue_files:
            self._start_batch()
            return
        if not self.current_file:
            messagebox.showinfo(APP_NAME, "请先拖入或选择音频文件")
            return
        if not self._check_ready_to_run():
            return

        self.cancel_flag = False
        self.btn_start.config(state=tk.DISABLED, text="转换中…")
        self.btn_cancel.config(state=tk.NORMAL)
        self.pbar["value"] = 0
        self.log("-" * 50)
        self.log(f"开始处理: {os.path.basename(self.current_file)}")

        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def _check_ready_to_run(self):
        """同步配置 + 校验草稿箱目录。返回 False 表示不能开始。"""
        self._sync_cfg()
        if not self.cfg.get("draft_dir") or not os.path.isdir(self.cfg["draft_dir"]):
            messagebox.showerror(APP_NAME,
                                 "草稿箱目录无效。\n请点「浏览」选择剪映的草稿箱目录，\n"
                                 "通常形如：\n...\\JianyingPro\\User Data\\Projects\\com.lveditor.draft")
            return False
        return True

    def _start_batch(self):
        if not self._check_ready_to_run():
            return
        n = len(self.queue_files)
        self.cancel_flag = False
        self.batch_running = True
        self.made_drafts = []
        self.done_paths = []
        self.btn_start.config(state=tk.DISABLED, text=f"批量转换中 0/{n}…")
        self.btn_cancel.config(state=tk.NORMAL)
        self.pbar["value"] = 0
        self.log("-" * 50)
        self.log(f"开始批量转换：{n} 个文件")
        self._set_queue_btns(tk.DISABLED)
        self.worker = threading.Thread(target=self._work_batch, daemon=True)
        self.worker.start()

    def _set_queue_btns(self, state):
        try:
            self.btn_q_remove.config(state=state)
            self.btn_q_clear.config(state=state)
        except Exception:
            pass

    def _work_batch(self):
        files = list(self.queue_files)
        n = len(files)
        ok_cnt, fails = 0, []
        for i, f in enumerate(files, 1):
            self.q.put(("batch", i, n, f))
            try:
                res = core.run(
                    f, self.cfg,
                    progress_cb=lambda s, p, m, _i=i: self.q.put(
                        ("prog", s, p, f"[{_i}/{n}] {m}")),
                    cancel_flag=lambda: self.cancel_flag)
                ok_cnt += 1
                self.q.put(("batch_item_ok", res, f))
            except core.Cancelled:
                self.q.put(("cancel", None))
                return
            except Exception as e:
                fails.append((os.path.basename(f), str(e)))
                self.q.put(("prog", "error", 0, f"[{i}/{n}] 失败: {e}"))
        self.q.put(("batch_done", (ok_cnt, fails)))

    def _work(self):
        try:
            res = core.run(self.current_file, self.cfg,
                           progress_cb=lambda s, p, m: self.q.put(("prog", s, p, m)),
                           cancel_flag=lambda: self.cancel_flag)
            self.q.put(("done", res))
        except core.Cancelled:
            self.q.put(("cancel", None))
        except Exception as e:
            self.q.put(("error", (e, traceback.format_exc())))

    def _poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "prog":
                    _, stage, pct, msg = item
                    self.pbar["value"] = max(0, min(100, pct))
                    self.status.config(text=msg)
                    if stage in ("decode", "silence", "segment", "draft") or pct in (0, 100):
                        self.log(msg)
                elif kind == "drop":
                    self._handle_drop(item[1])
                elif kind == "batch":
                    _, bi, bn, name = item
                    self.log(f"===== [{bi}/{bn}] {os.path.basename(name)} =====")
                elif kind == "batch_done":
                    self._on_batch_done(item[1])
                elif kind == "batch_item_ok":
                    r = item[1]
                    self.done_paths.append(item[2])
                    st, tm = r["stats"], r["timings"]
                    self.log(f"  ✓ {r['draft_name']}：{st['count']} 条字幕，"
                             f"{tm['total']:.0f}s")
                    self.made_drafts.append(r["draft_name"])
                elif kind == "done":
                    self._on_done(item[1])
                elif kind == "cancel":
                    self.log("已取消")
                    # 批量取消：已成功的出队，剩下的留着方便重来
                    if self.done_paths:
                        self.queue_files = [f for f in self.queue_files
                                            if f not in self.done_paths]
                        self.done_paths = []
                        self._refresh_queue()
                    self._reset_btn()
                elif kind == "error":
                    e, tb = item[1]
                    self.log(f"错误: {e}")
                    self.log(tb)
                    self.status.config(text=f"失败: {e}")
                    self._reset_btn()
                    messagebox.showerror(APP_NAME, str(e))
        except queue.Empty:
            pass
        # 转换中被拖入的新文件：等当前任务真正结束（取消/完成/失败）后自动开始。
        # 放在轮询里判断而不是绑在 cancel 分支，取消晚到或任务刚好跑完都不会漏。
        if self.pending_file and not (self.worker and self.worker.is_alive()):
            f, self.pending_file = self.pending_file, None
            self.log(f"自动开始新文件: {os.path.basename(f)}")
            self._set_file(f)
        self.root.after(120, self._poll_queue)

    def _on_done(self, res):
        st = res["stats"]
        tm = res["timings"]
        self.pbar["value"] = 100
        self.status.config(text=f"完成：{st['count']} 条字幕")
        self.log("")
        self.log(f"完成！字幕 {st['count']} 条（ASR 原始 {res['raw_count']} 段）")
        self.log(f"  时长 平均 {st['avg_dur']:.2f}s / 中位 {st['median_dur']:.2f}s "
                 f"/ 最长 {st['max_dur']:.2f}s")
        self.log(f"  断句方式 {st.get('method')}（字级单元 {st.get('units')} 个），"
                 f"时间轴异常 {st['anomalies']}")
        self.log(f"  耗时: 模型 {tm['model']:.1f}s | ASR {tm['asr']:.1f}s | "
                 f"断句 {tm['segment']:.2f}s | 草稿 {tm['draft']:.1f}s")
        self.log(f"  总耗时 {tm['total']:.1f}s")
        self.log(f"  草稿: {res['draft_path']}")
        if res.get("srt_path"):
            self.log(f"  字幕: {res['srt_path']}")
        self._reset_btn()

        if self.var_launch.get():
            inst = self.var_install.get()
            if inst and os.path.isfile(os.path.join(inst, "JianyingPro.exe")):
                try:
                    self.log("正在打开剪映…")
                    jianying.launch_jianying(inst)
                except Exception as e:
                    self.log(f"打开剪映失败: {e}")
            else:
                self.log("未配置剪映安装目录，跳过自动打开")

        messagebox.showinfo(APP_NAME,
                            f"完成！\n\n字幕 {st['count']} 条\n"
                            f"草稿：{res['draft_name']}\n\n"
                            f"请在剪映草稿箱中查看。")

    def _on_batch_done(self, payload):
        ok_cnt, fails = payload
        self.pbar["value"] = 100
        total = ok_cnt + len(fails)
        self.status.config(text=f"批量完成：成功 {ok_cnt}/{total}")
        self.log("")
        self.log(f"批量完成：成功 {ok_cnt}/{total}")
        for name, err in fails:
            self.log(f"  ✗ {name}：{err}")
        # 成功的出队；失败的留在队列里方便重试
        failed_names = {n for n, _ in fails}
        self.queue_files = [f for f in self.queue_files
                            if os.path.basename(f) in failed_names]
        self._refresh_queue()
        self._reset_btn()

        msg = f"批量转换完成\n\n成功 {ok_cnt} 个"
        if fails:
            msg += f"\n失败 {len(fails)} 个（仍留在队列，可修正后重试）"
        msg += "\n\n请在剪映草稿箱中查看。"
        messagebox.showinfo(APP_NAME, msg)
        if ok_cnt and self.var_launch.get():
            inst = self.var_install.get()
            if inst and os.path.isfile(os.path.join(inst, "JianyingPro.exe")):
                try:
                    self.log("正在打开剪映…")
                    jianying.launch_jianying(inst)
                except Exception as e:
                    self.log(f"打开剪映失败: {e}")

    def _reset_btn(self):
        self.batch_running = False
        self.btn_start.config(state=tk.NORMAL,
                              text=f"开始转换（{len(self.queue_files)} 个）"
                              if self.queue_files else "开始转换")
        self.btn_cancel.config(state=tk.DISABLED)
        self._set_queue_btns(tk.NORMAL)


def selftest():
    """自检：验证打包后的依赖、ffmpeg、路径探测是否正常。

    结果写入 %LOCALAPPDATA%\\JianyingSubtitle\\selftest.log
    用法：JianyingSubtitleGUI.exe --selftest
    """
    import traceback
    lines = []
    ok = True

    def add(k, v, good=None):
        nonlocal ok
        mark = ""
        if good is True:
            mark = "  [OK]"
        elif good is False:
            mark = "  [FAIL]"
            ok = False
        lines.append(f"{k:22s}: {v}{mark}")

    lines.append(f"=== {APP_NAME} v{APP_VER} 自检 ===")
    lines.append(f"打包运行: {getattr(sys, 'frozen', False)}")
    lines.append(f"执行文件: {sys.executable}")
    lines.append(f"_MEIPASS: {getattr(sys, '_MEIPASS', '(无)')}")
    lines.append("")

    # 模块
    for m in ["tkinter", "numpy", "faster_whisper", "ctranslate2", "onnxruntime",
              "tokenizers", "zhconv", "config", "model_manager", "asr", "jianying",
              "resegment", "textpost", "core"]:
        try:
            __import__(m)
            add(f"import {m}", "成功", True)
        except Exception as e:
            add(f"import {m}", f"{type(e).__name__}: {e}", False)

    lines.append("")
    # ffmpeg
    try:
        ff = asr.find_ffmpeg()
        add("ffmpeg 路径", ff or "(未找到)", bool(ff))
    except Exception as e:
        add("ffmpeg 查找", f"异常 {e}", False)

    # 资源模板
    try:
        import jianying as jy
        sk = jy._load_skeleton()
        add("草稿骨架模板", f"顶层键 {len(sk)} / materials {len(sk['materials'])}",
            len(sk) > 20)
        mt = jy._load_meta_skeleton()
        add("meta 模板", f"键数 {len(mt)}", len(mt) > 10)
    except Exception as e:
        add("资源模板", f"{type(e).__name__}: {e}", False)

    lines.append("")
    # 路径探测
    try:
        add("草稿箱目录", cfgmod.detect_draft_dir() or "(未探测到)", bool(cfgmod.detect_draft_dir()))
        add("剪映安装目录", cfgmod.detect_install_dir() or "(未探测到)", bool(cfgmod.detect_install_dir()))
    except Exception as e:
        add("路径探测", f"异常 {e}", False)

    # 模型
    try:
        for name in model_manager.MODEL_INFO:
            d = model_manager.model_dir(name)
            add(f"模型 {name}", f"{'已就绪' if model_manager.is_ready(name) else '待下载'} | {d}",
                None)
    except Exception as e:
        add("模型检查", f"异常 {e}", False)

    lines.append("")
    lines.append(f"=== 结论: {'全部通过' if ok else '存在失败项'} ===")

    out = os.path.join(cfgmod.config_dir(), "selftest.log")
    try:
        os.makedirs(cfgmod.config_dir(), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass

    # 也弹窗显示（windowed 程序没有控制台），8 秒后自动关闭避免阻塞
    try:
        r = tk.Tk()
        r.withdraw()
        r.after(8000, r.destroy)
        messagebox.showinfo(f"{APP_NAME} 自检", "\n".join(lines) + f"\n\n已写入: {out}")
        r.destroy()
    except Exception:
        pass
    return 0 if ok else 1


def cli_convert(argv):
    """命令行转换模式（无 GUI），用于自动化测试与批量处理。

    用法：JianyingSubtitleGUI.exe --convert "音频路径" [--draft-name 名称] [--model base]
    日志写入 %LOCALAPPDATA%\\JianyingSubtitle\\convert.log
    """
    import argparse
    ap = argparse.ArgumentParser(prog="JianyingSubtitleGUI")
    ap.add_argument("--convert", required=True, help="要转换的音频/视频路径")
    ap.add_argument("--draft-name", default=None)
    ap.add_argument("--model", default=None, choices=list(model_manager.MODEL_INFO))
    ap.add_argument("--canvas", default=None)
    ap.add_argument("--no-kill", action="store_true", help="不要自动关闭剪映")
    args = ap.parse_args(argv)

    cfg = cfgmod.load()
    if args.model:
        cfg["model_size"] = args.model
    if args.canvas:
        cfg["canvas"] = args.canvas
    if args.draft_name:
        cfg["draft_name"] = args.draft_name
    if args.no_kill:
        cfg["kill_jianying_first"] = False

    log_path = os.path.join(cfgmod.config_dir(), "convert.log")
    os.makedirs(cfgmod.config_dir(), exist_ok=True)
    f = open(log_path, "w", encoding="utf-8")

    def log(msg):
        f.write(str(msg) + "\n")
        f.flush()

    def cb(stage, pct, msg):
        log(f"[{pct:3d}%] {stage:9s} {msg}")

    rc = 1
    try:
        log(f"=== 转换开始 {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        log(f"输入: {args.convert}")
        log(f"模型: {cfg.get('model_size')}  画布: {cfg.get('canvas')}")
        log(f"草稿目录: {cfg.get('draft_dir')}")
        res = core.run(args.convert, cfg, progress_cb=cb)
        st = res["stats"]
        tm = res["timings"]
        log("")
        log(f"--- 结果 ---")
        log(f"字幕条数: {st['count']} (ASR 原始 {res['raw_count']} 段)")
        log(f"时长 平均{st['avg_dur']:.2f}s 中位{st['median_dur']:.2f}s 最长{st['max_dur']:.2f}s")
        log(f"断句方式: {st.get('method')} (字级单元 {st.get('units')} 个) 时间轴异常 {st['anomalies']}")
        log(f"耗时 模型{tm['model']:.1f}s ASR{tm['asr']:.1f}s "
            f"断句{tm['segment']:.2f}s 草稿{tm['draft']:.1f}s 总计{tm['total']:.1f}s")
        log(f"草稿路径: {res['draft_path']}")
        log(f"SRT: {res.get('srt_path')}")
        log("=== 转换成功 ===")
        rc = 0
    except Exception as e:
        import traceback
        log(f"!!! 失败: {type(e).__name__}: {e}")
        log(traceback.format_exc())
    finally:
        f.close()

    return rc


def main():
    # 自检模式
    if "--selftest" in sys.argv:
        return selftest()

    # 命令行转换模式
    if "--convert" in sys.argv:
        return cli_convert(sys.argv[1:])

    # 高 DPI 适配
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main() or 0)
