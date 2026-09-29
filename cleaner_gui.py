#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cleaner_gui.py - 系统清理与加速工具图形界面(tkinter, 零第三方依赖)

界面采用多标签页布局:
  * 内存清理  : 实时内存占用 / 页表统计 / 三档清理 / 自动定时清理
  * 垃圾清理  : 临时文件 / 浏览器缓存 / 回收站 / 缩略图缓存 / 错误报告等
  * 游戏加速  : 进程高优先级 + 关 EcoQoS + 定时后台清理 / 自动检测全屏游戏
  * 系统工具  : 开机启动项管理(禁用/恢复) + 大文件扫描

所有耗时操作都在后台线程执行, 界面不会卡死; 不是管理员时提供 UAC 提权按钮。

直接运行: py cleaner_gui.py   (或双击 run_gui.bat 自动请求管理员权限)
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import memory_cleaner as mc  # noqa: E402
import game_booster as gb  # noqa: E402
import junk_cleaner as jc  # noqa: E402
import system_tools as st  # noqa: E402
import cleaner as cli  # noqa: E402  统一入口(一键优化 / 概览)
from memory_cleaner import (  # noqa: E402
    PROFILES, human, is_admin, list_processes, query_memory_status,
    query_page_lists, run_clean, setup_logging, default_log_path,
    elevate_and_rerun, format_report,
)
from junk_cleaner import category_needs_admin  # noqa: E402

# 打包成 exe 后要落到 exe 所在目录(见 common.BASE_DIR 的说明)
CONFIG_PATH = os.path.join(common.BASE_DIR, "cleaner_config.json")
AUTO_INTERVALS = {
    "关闭": 0,
    "10 分钟": 600,
    "15 分钟": 900,
    "30 分钟": 1800,
    "1 小时": 3600,
    "2 小时": 7200,
}

APP_TITLE = "系统清理与加速工具"
APP_VERSION = "3.0"

# ---------------------------------------------------------------------------
# 主题配色 / 字体
#   统一到这里维护; 界面所有控件都从这两个常量族取名, 改主题只需改这一处
# ---------------------------------------------------------------------------
BG = "#15171c"          # 窗口底色
FG = "#e8eaed"          # 主文字
SUBTLE = "#9aa3b2"      # 次要说明文字
PANEL = "#1d2027"       # 面板/输入框
PANEL2 = "#242831"      # 悬停/斑马纹
BORDER = "#2c313b"      # 分隔线
ACCENT = "#3b82f6"      # 主色(按钮/进度条)
ACCENT_ACTIVE = "#2f6fd0"
OK_COLOR = "#4ade80"
WARN_COLOR = "#fbbf24"
DANGER_COLOR = "#f87171"

FONT_FAMILY = "Microsoft YaHei UI"   # 中文显示比 Segoe UI 更稳
MONO_FAMILY = "Consolas"

LEVEL_COLORS = {"充裕": OK_COLOR, "正常": ACCENT, "偏高": WARN_COLOR, "危险": DANGER_COLOR}


def enable_high_dpi() -> None:
    """打开高 DPI 感知, 否则 2K/4K 屏上界面会发虚。

    只在 Windows 有效, 失败则静默忽略(不影响启动)。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        # 先用 Win10 1703+ 的 Per-Monitor V2, 不支持时退回系统级 DPI 感知
        try:
            ctypes.windll.shcore.SetProcessDpiAwarenessContext(-4)
            return
        except (AttributeError, OSError):
            pass
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # pragma: no cover - 老系统或异常环境
        pass


class QueueLogHandler(logging.Handler):
    """把日志推进队列, 由界面线程安全地显示。"""

    def __init__(self, log_queue: "queue.Queue[str]"):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:  # pragma: no cover
            pass


class CleanerApp(tk.Tk):
    """主窗口(多标签页)。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("%s v%s" % (APP_TITLE, APP_VERSION))
        self.geometry("980x780")
        self.minsize(880, 680)
        self.configure(bg=BG)

        setup_logging(False, default_log_path())
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        mc.LOG.addHandler(handler)

        self.config_data = self._load_config()
        self.booster = gb.GameBooster(trim_interval=int(self.config_data.get("trim_interval", 300)))
        self.auto_clean_seconds = int(self.config_data.get("auto_clean_seconds", 0))
        self._last_auto_clean = time.time()
        self._game_thread: Optional[threading.Thread] = None
        self._busy = False
        self._busy_tasks: List[str] = []
        self._last_summary = ""
        self.process_list = []
        self._proc_sort: Optional[str] = None
        self._proc_reverse = True
        self._game_status = ""
        self._cancel_scan = threading.Event()  # 垃圾/大文件扫描的中断信号
        self._ui_queue: "queue.Queue" = queue.Queue()
        self.booster.trim = True
        self.booster.purge_standby = bool(self.config_data.get("purge_standby", True))

        self._build_style()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<F5>", self._on_f5)
        self._center_on_screen()

        self._refresh_status()
        self._poll_log_queue()
        self._poll_ui_queue()
        self._poll_auto_clean()
        self._poll_game_state()
        self._on_refresh_processes()
        self._on_refresh_startups()
        self.log("界面已启动。当前权限: %s" % ("管理员" if is_admin() else "普通用户"))
        if not is_admin():
            self.log("提示: 清空待机列表/文件缓存/系统级垃圾清理需要管理员权限, 可点击右上角按钮提权。")

    # -- 配置 -------------------------------------------------------------
    def _load_config(self) -> Dict[str, object]:
        try:
            import json
            with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except (OSError, ValueError):
            return {}

    def _save_config(self) -> None:
        try:
            import json
            data = {
                "profile": self.profile_var.get(),
                "auto_clean_seconds": self.auto_clean_seconds,
                "trim_interval": self.booster.trim_interval,
                "purge_standby": bool(self.purge_var.get()),
                "game_auto": bool(self.game_auto_var.get()),
                "junk_selected": sorted(self.junk_selected),
            }
            with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # -- 样式 -------------------------------------------------------------
    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # 全局默认字体: 中文环境用微软雅黑, 避免 widgets 用点阵宋体导致观感割裂
        self.option_add("*Font", (FONT_FAMILY, 9))
        for name in ("TkText", "TEntry", "TCombobox"):
            self.option_add("*%s*Font" % name, (FONT_FAMILY, 9))

        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                        bordercolor=BORDER, focuscolor=ACCENT)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Panel.TLabel", background=PANEL, foreground=FG)
        style.configure("Subtle.TLabel", background=BG, foreground=SUBTLE)
        style.configure("Title.TLabel", font=(FONT_FAMILY, 10, "bold"),
                        background=BG, foreground=ACCENT)
        style.configure("Big.TLabel", font=(FONT_FAMILY, 19, "bold"),
                        background=BG, foreground=FG)
        style.configure("Percent.TLabel", font=(FONT_FAMILY, 20, "bold"),
                        background=BG, foreground=ACCENT)
        style.configure("Mono.TLabel", font=(MONO_FAMILY, 9), background=BG,
                        foreground=SUBTLE)
        # 权限徽标
        for level, color in (("Ok", OK_COLOR), ("Warn", WARN_COLOR)):
            style.configure("%s.Badge.TLabel" % level, background=PANEL2,
                            foreground=color, relief="flat",
                            padding=(10, 4), font=(FONT_FAMILY, 9, "bold"))

        # 复选框/单选框: 去掉难看的默认方块边框倾角, 深色底统一
        for widget in ("TCheckbutton", "TRadiobutton"):
            style.configure(widget, background=BG, foreground=FG, padding=(4, 2))
            style.map(widget,
                      background=[("active", BG)],
                      foreground=[("active", ACCENT)])

        # 按钮: 常规胶囊式 + 主按钮(accent)带悬停/按下反馈
        style.configure("TButton", padding=(11, 6), background=PANEL2,
                        foreground=FG, bordercolor=BORDER, lightcolor=PANEL2,
                        darkcolor=PANEL2, relief="flat")
        style.map("TButton",
                  background=[("active", BORDER), ("pressed", PANEL)],
                  foreground=[("disabled", SUBTLE)])
        style.configure("Accent.TButton", padding=(14, 7), background=ACCENT,
                        foreground="#ffffff", bordercolor=ACCENT,
                        lightcolor=ACCENT, darkcolor=ACCENT,
                        font=(FONT_FAMILY, 9, "bold"))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_ACTIVE), ("pressed", ACCENT_ACTIVE)],
                  foreground=[("disabled", "#cbd5e1")])
        style.configure("Danger.TButton", background=DANGER_COLOR,
                        foreground="#2b1114", bordercolor=DANGER_COLOR,
                        font=(FONT_FAMILY, 9, "bold"))

        # 输入框/下拉框
        style.configure("TEntry", fieldbackground=PANEL, foreground=FG,
                        insertcolor=FG, bordercolor=BORDER, padding=(6, 4))
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL,
                        foreground=FG, arrowcolor=FG, bordercolor=BORDER,
                        padding=(6, 4))
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

        # 进度条(统一外观, 颜色会按内存占用等级动态切换)
        style.configure("Bar.Horizontal.TProgressbar", troughcolor=PANEL2,
                        background=ACCENT, bordercolor=PANEL2, thickness=16,
                        lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("Tiny.Horizontal.TProgressbar", troughcolor=PANEL,
                        background=ACCENT, thickness=6)

        # 列表: 斑马纹 + 悬停 + 选中态, 长时间看表格不串行
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=FG, bordercolor=BORDER, rowheight=26)
        style.configure("Treeview.Heading", background=PANEL2, foreground=FG,
                        font=(FONT_FAMILY, 9, "bold"), relief="flat",
                        padding=(6, 5))
        style.map("Treeview.Heading",
                  background=[("active", BORDER)],
                  foreground=[("active", ACCENT)])
        style.map("Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#ffffff")])
        self.option_add("*Treeview*Font", (FONT_FAMILY, 9))

        # 标签页: 选中色块 + 悬停反馈
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(6, 4, 6, 0))
        style.configure("TNotebook.Tab", background=PANEL, foreground=SUBTLE,
                        padding=(18, 8), font=(FONT_FAMILY, 10))
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT), ("active", PANEL2)],
                  foreground=[("selected", "#ffffff"), ("active", FG)])

        # 分组框
        style.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="flat")
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT,
                        font=(FONT_FAMILY, 9, "bold"))

        style.configure("TSeparator", background=BORDER)

    def _apply_tree_stripes(self, tree: ttk.Treeview) -> None:
        """给 Treeview 加奇数行浅色底, 提升长列表可读性。"""
        tree.tag_configure("odd", background=PANEL2)
        tree.tag_configure("even", background=PANEL)
        for index, iid in enumerate(tree.get_children()):
            tree.item(iid, tags=("odd" if index % 2 else "even",))

    # -- 界面构建 ---------------------------------------------------------
    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_header()
        self._build_notebook()
        self._build_log()

        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Frame(self, padding=(10, 3))
        status_bar.grid(row=3, column=0, sticky="ew")
        status_bar.columnconfigure(1, weight=1)
        # 忙碌时的滚动指示条(执行清理/扫描时给出明确反馈)
        self.busy_bar = ttk.Progressbar(status_bar, mode="indeterminate",
                                        style="Tiny.Horizontal.TProgressbar",
                                        length=90, maximum=100)
        self.busy_bar.grid(row=0, column=0, padx=(0, 8))
        ttk.Label(status_bar, textvariable=self.status_var, style="Subtle.TLabel",
                  anchor="w").grid(row=0, column=1, sticky="ew")
        ttk.Label(status_bar, text="v%s  |  F5 刷新列表  |  全部操作均在后台线程执行"
                  % APP_VERSION, style="Subtle.TLabel").grid(row=0, column=2, sticky="e")

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(14, 12, 14, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        titles = ttk.Frame(header)
        titles.grid(row=0, column=0, sticky="w")
        ttk.Label(titles, text=APP_TITLE, style="Big.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(titles, text="System Cleaner  ·  内存 / 垃圾 / 游戏加速 / 启动项",
                  style="Subtle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))

        # 权限徽标: 管理员=绿色, 普通用户=橙色提示
        admin = is_admin()
        self.admin_var = tk.StringVar(value="管理员" if admin else "普通用户")
        badge_style = "Ok.Badge.TLabel" if admin else "Warn.Badge.TLabel"
        ttk.Label(header, textvariable=self.admin_var, style=badge_style).grid(
            row=0, column=1, sticky="e", padx=(10, 8))
        ttk.Button(header, text="一键优化", style="Accent.TButton",
                   command=self._on_once).grid(row=0, column=2, sticky="e")
        ttk.Button(header, text="关于", command=self._on_about).grid(
            row=0, column=3, sticky="e", padx=(8, 0))
        if not admin:
            ttk.Button(header, text="提权重启",
                       command=self._on_elevate).grid(row=1, column=2,
                                                      columnspan=2, sticky="e",
                                                      pady=(6, 0))

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=6, pady=2)

        self.memory_tab = ttk.Frame(self.notebook)
        self.junk_tab = ttk.Frame(self.notebook)
        self.game_tab = ttk.Frame(self.notebook)
        self.tools_tab = ttk.Frame(self.notebook)

        self.notebook.add(self.memory_tab, text="  内存清理  ")
        self.notebook.add(self.junk_tab, text="  垃圾清理  ")
        self.notebook.add(self.game_tab, text="  游戏加速  ")
        self.notebook.add(self.tools_tab, text="  系统工具  ")

        self._build_memory_tab()
        self._build_junk_tab()
        self._build_game_tab()
        self._build_tools_tab()

    # ---- 内存清理标签页 -------------------------------------------------
    def _build_memory_tab(self) -> None:
        frame = self.memory_tab
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        stats = ttk.Frame(frame, padding=(12, 12, 12, 4))
        stats.grid(row=0, column=0, sticky="ew")
        stats.columnconfigure(0, weight=1)

        top_row = ttk.Frame(stats)
        top_row.grid(row=0, column=0, sticky="ew")
        top_row.columnconfigure(0, weight=1)

        self.percent_var = tk.StringVar(value="0.0%")
        ttk.Label(top_row, textvariable=self.percent_var, style="Percent.TLabel").grid(
            row=0, column=0, sticky="w")
        self.level_var = tk.StringVar(value="-")
        ttk.Label(top_row, textvariable=self.level_var, style="Subtle.TLabel").grid(
            row=0, column=1, sticky="e")

        self.bar = ttk.Progressbar(stats, style="Bar.Horizontal.TProgressbar",
                                   maximum=100.0, value=0)
        self.bar.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        self.detail_var = tk.StringVar(value="-")
        ttk.Label(stats, textvariable=self.detail_var,
                  font=(MONO_FAMILY, 9)).grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        self.pages_var = tk.StringVar(value="-")
        ttk.Label(stats, textvariable=self.pages_var, style="Subtle.TLabel").grid(
            row=3, column=0, sticky="w", pady=(2, 0))

        controls = ttk.LabelFrame(frame, text=" 内存清理 ", padding=(10, 8))
        controls.grid(row=1, column=0, sticky="ew", padx=12, pady=(8, 4))
        controls.columnconfigure(5, weight=1)

        ttk.Label(controls, text="清理档位:").grid(row=0, column=0, sticky="w")
        self.profile_var = tk.StringVar(value=str(self.config_data.get("profile", "standard")))
        for index, prof in enumerate(PROFILES.values(), start=1):
            ttk.Radiobutton(controls, text=prof.title, value=prof.key,
                            variable=self.profile_var).grid(
                row=0, column=index, sticky="w", padx=(6, 0))
        ttk.Button(controls, text="立即清理", style="Accent.TButton",
                   command=self._on_clean).grid(row=0, column=4, sticky="e", padx=(14, 0))

        ttk.Label(controls, text="自动清理:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        default_label = "关闭"
        for label, seconds in AUTO_INTERVALS.items():
            if seconds and seconds == self.auto_clean_seconds:
                default_label = label
        self.auto_var = tk.StringVar(value=default_label)
        combo = ttk.Combobox(controls, textvariable=self.auto_var, state="readonly",
                             values=list(AUTO_INTERVALS.keys()), width=10)
        combo.grid(row=1, column=1, columnspan=2, sticky="w", pady=(8, 0))
        combo.bind("<<ComboboxSelected>>", self._on_auto_changed)
        ttk.Label(controls, text="每隔一段时间自动执行轻度清理, 适合挂机保持内存充足",
                  style="Subtle.TLabel").grid(row=1, column=3, columnspan=2,
                                              sticky="w", padx=(14, 0), pady=(8, 0))

        desc = ttk.Label(frame, text="说明: 内存清理释放「使用中」内存与系统缓存, 让游戏/大型程序有更多可用内存。"
                                     "标准档位即可满足大多数场景。",
                         style="Subtle.TLabel", wraplength=900)
        desc.grid(row=2, column=0, sticky="w", padx=12, pady=(6, 4))
        # 留白占位(日志已放到窗口底部)
        ttk.Frame(frame).grid(row=3, column=0, sticky="nsew")

    # ---- 垃圾清理标签页 -------------------------------------------------
    def _build_junk_tab(self) -> None:
        frame = self.junk_tab
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        top = ttk.Frame(frame, padding=(12, 10, 12, 0))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(3, weight=1)
        ttk.Button(top, text="扫描垃圾", command=self._on_scan_junk).grid(row=0, column=0)
        ttk.Button(top, text="全选", command=lambda: self._on_junk_select(True)).grid(
            row=0, column=1, padx=(6, 0))
        ttk.Button(top, text="全不选", command=lambda: self._on_junk_select(False)).grid(
            row=0, column=2, padx=(6, 0))
        ttk.Button(top, text="停止", command=self._on_cancel_scan).grid(
            row=0, column=3, padx=(6, 0))
        self.junk_total_var = tk.StringVar(value="尚未扫描")
        ttk.Label(top, textvariable=self.junk_total_var, style="Title.TLabel").grid(
            row=0, column=3, sticky="e")

        ttk.Label(frame, text="点击行首「☑」切换是否清理; 垃圾文件可安全删除, 被占用的文件会自动跳过。",
                  style="Subtle.TLabel").grid(row=1, column=0, sticky="w", padx=12, pady=(2, 2))

        tree_frame = ttk.Frame(frame, padding=(12, 0))
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self.junk_tree = ttk.Treeview(
            tree_frame, columns=("sel", "title", "risk", "size", "files", "desc"),
            show="headings")
        for key, title, width, anchor in (
                ("sel", "选择", 60, "center"), ("title", "类别", 130, "w"),
                ("risk", "风险", 60, "center"), ("size", "大小", 90, "e"),
                ("files", "文件数", 80, "e"), ("desc", "说明", 360, "w")):
            self.junk_tree.heading(key, text=title)
            self.junk_tree.column(key, width=width, anchor=anchor)
        self.junk_tree.grid(row=0, column=0, sticky="nsew")
        self.junk_tree.bind("<Button-1>", self._on_junk_tree_click)

        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.junk_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.junk_tree.configure(yscrollcommand=scroll.set)

        bottom = ttk.Frame(frame, padding=(12, 8))
        bottom.grid(row=3, column=0, sticky="ew")
        ttk.Button(bottom, text="清理选中垃圾", style="Accent.TButton",
                   command=self._on_clean_junk).pack(side="left")
        ttk.Button(bottom, text="清空回收站", command=self._on_empty_recycle).pack(
            side="left", padx=(6, 0))
        ttk.Label(bottom, text="首次扫描会遍历系统目录, 可能需要几秒到十几秒",
                  style="Subtle.TLabel").pack(side="right")

        # 初始化类别行
        self.junk_cats = jc.build_categories()
        self.junk_scan_results: Dict[str, jc.JunkScanResult] = {}
        saved = set(self.config_data.get("junk_selected", []))
        self.junk_selected = {k for k in saved if k in self.junk_cats}
        if not self.junk_selected:
            self.junk_selected = {"temp", "browser", "thumbnail", "recycle"}
        for cat in self.junk_cats.values():
            mark = "☑" if cat.key in self.junk_selected else "☐"
            self.junk_tree.insert("", "end", iid=cat.key,
                                  values=(mark, cat.title, cat.risk, "-", "-", cat.description))
        self._apply_tree_stripes(self.junk_tree)

    # ---- 游戏加速标签页 -------------------------------------------------
    def _build_game_tab(self) -> None:
        frame = self.game_tab
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        controls = ttk.LabelFrame(frame, text=" 游戏模式 ", padding=(10, 8))
        controls.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        controls.columnconfigure(4, weight=1)

        self.game_auto_var = tk.BooleanVar(value=bool(self.config_data.get("game_auto", False)))
        ttk.Checkbutton(controls, text="自动检测全屏游戏并加速",
                        variable=self.game_auto_var,
                        command=self._on_game_auto_toggle).grid(row=0, column=0, sticky="w")

        self.purge_var = tk.BooleanVar(value=bool(self.config_data.get("purge_standby", True)))
        ttk.Checkbutton(controls, text="游戏时清空待机列表(需管理员)",
                        variable=self.purge_var).grid(row=0, column=1, sticky="w", padx=(16, 0))

        ttk.Label(controls, text="后台清理间隔(秒):").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.trim_interval_var = tk.StringVar(value=str(self.booster.trim_interval))
        entry = ttk.Entry(controls, textvariable=self.trim_interval_var, width=8)
        entry.grid(row=0, column=3, sticky="w", padx=(4, 0))
        entry.bind("<FocusOut>", self._on_trim_interval_changed)

        ttk.Label(controls, text="加速 = 高优先级 + 关 EcoQoS + 保护游戏进程不被清理",
                  style="Subtle.TLabel").grid(row=1, column=0, columnspan=5,
                                              sticky="w", pady=(6, 0))

        list_frame = ttk.LabelFrame(frame, text=" 进程列表 (双击 = 加速该进程) ", padding=(6, 6))
        list_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(4, 10))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(list_frame, columns=("pid", "name", "ws", "private"),
                                 show="headings")
        for key, title, width in (("pid", "PID", 90), ("name", "进程名", 340),
                                  ("ws", "工作集 ↕", 130), ("private", "私有内存 ↕", 130)):
            self.tree.heading(key, text=title,
                               command=lambda c=key: self._sort_processes(c))
            self.tree.column(key, width=width, anchor="w" if key == "name" else "center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _event: self._on_boost_selected())

        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        buttons = ttk.Frame(list_frame)
        buttons.grid(row=0, column=2, sticky="ns", padx=(8, 0))
        ttk.Button(buttons, text="刷新列表", command=self._on_refresh_processes).pack(fill="x")
        ttk.Button(buttons, text="加速选中进程",
                   command=self._on_boost_selected).pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="停止加速", command=self._on_stop_boost).pack(fill="x", pady=(6, 0))

    # ---- 系统工具标签页 -------------------------------------------------
    def _build_tools_tab(self) -> None:
        frame = self.tools_tab
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        # 启动项管理
        startup_frame = ttk.LabelFrame(frame, text=" 开机启动项管理 ", padding=(6, 6))
        startup_frame.grid(row=0, column=0, sticky="nsew", padx=12, pady=(10, 4))
        startup_frame.columnconfigure(0, weight=1)
        startup_frame.rowconfigure(1, weight=1)

        sbar = ttk.Frame(startup_frame)
        sbar.grid(row=0, column=0, sticky="ew")
        ttk.Button(sbar, text="刷新", command=self._on_refresh_startups).pack(side="left")
        ttk.Button(sbar, text="禁用选中", command=self._on_disable_startup).pack(
            side="left", padx=(6, 0))
        ttk.Button(sbar, text="恢复选中", command=self._on_enable_startup).pack(
            side="left", padx=(6, 0))
        ttk.Label(sbar, text="禁用 = 移入备份, 可随时恢复; 修改系统项需管理员",
                  style="Subtle.TLabel").pack(side="right")

        self.startup_tree = ttk.Treeview(
            startup_frame, columns=("state", "name", "command", "source"), show="headings")
        for key, title, width, anchor in (
                ("state", "状态", 60, "center"), ("name", "名称", 200, "w"),
                ("command", "命令/路径", 320, "w"), ("source", "来源", 140, "w")):
            self.startup_tree.heading(key, text=title)
            self.startup_tree.column(key, width=width, anchor=anchor)
        self.startup_tree.grid(row=1, column=0, sticky="nsew")
        ss = ttk.Scrollbar(startup_frame, orient="vertical", command=self.startup_tree.yview)
        ss.grid(row=1, column=1, sticky="ns")
        self.startup_tree.configure(yscrollcommand=ss.set)
        self.startup_items: List[st.StartupItem] = []

        # 大文件扫描
        large_frame = ttk.LabelFrame(frame, text=" 大文件扫描 ", padding=(6, 6))
        large_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 10))
        large_frame.columnconfigure(0, weight=1)
        large_frame.rowconfigure(1, weight=1)

        lbar = ttk.Frame(large_frame)
        lbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(lbar, text="目录:").pack(side="left")
        self.large_dir_var = tk.StringVar(value="C:\\")
        ttk.Entry(lbar, textvariable=self.large_dir_var, width=30).pack(side="left", padx=(4, 8))
        ttk.Label(lbar, text="大于(MB):").pack(side="left")
        self.large_min_var = tk.StringVar(value="500")
        ttk.Entry(lbar, textvariable=self.large_min_var, width=8).pack(side="left", padx=(4, 8))
        ttk.Button(lbar, text="开始扫描", command=self._on_scan_large).pack(side="left")
        ttk.Button(lbar, text="停止", command=self._on_cancel_scan).pack(
            side="left", padx=(6, 0))
        ttk.Button(lbar, text="打开所在文件夹", command=self._on_open_large_folder).pack(
            side="left", padx=(6, 0))

        self.large_tree = ttk.Treeview(large_frame, columns=("size", "path"), show="headings")
        self.large_tree.heading("size", text="大小")
        self.large_tree.heading("path", text="路径")
        self.large_tree.column("size", width=110, anchor="e")
        self.large_tree.column("path", width=640, anchor="w")
        self.large_tree.grid(row=1, column=0, sticky="nsew")
        ls = ttk.Scrollbar(large_frame, orient="vertical", command=self.large_tree.yview)
        ls.grid(row=1, column=1, sticky="ns")
        self.large_tree.configure(yscrollcommand=ls.set)

    def _build_log(self) -> None:
        frame = ttk.Frame(self, padding=(10, 2, 10, 4))
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        bar = ttk.Frame(frame)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="日志", style="Title.TLabel").pack(side="left")
        ttk.Button(bar, text="清空日志", command=self._clear_log).pack(side="right")

        self.log_text = ScrolledText(frame, height=7, bg=PANEL, fg=FG,
                                     insertbackground=FG, wrap="word",
                                     font=("Consolas", 9), relief="flat")
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

    # -- 日志(线程安全) ----------------------------------------------------
    def log(self, message: str) -> None:
        """写一条日志。

        统一走 mc.LOG: 由 QueueLogHandler 送回界面显示, 同时被 FileHandler 落盘到
        cleaner.log。这样界面上的每一步操作都会留下可追溯的记录(排查问题时很有用)。
        """
        mc.LOG.info(message)

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 800:  # 防止日志无限增长
            self.log_text.delete("1.0", "200.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._poll_log_queue)

    # -- 状态刷新 ---------------------------------------------------------
    def _refresh_status(self) -> None:
        try:
            status = query_memory_status()
            pages = query_page_lists()
            self._apply_memory_gauge(status)
            if pages:
                self.pages_var.set(
                    "待机页(可回收缓存) %s     空闲页(可立即使用) %s     已修改页 %s" % (
                        human(pages.standby), human(pages.free + pages.zero),
                        human(pages.modified)))
            else:
                self.pages_var.set("待机页/空闲页统计不可用(系统未提供该信息)")
        except Exception as exc:  # 界面不能因为一次读取失败就崩掉
            self.log("[错误] 读取内存状态失败: %s" % exc)
        self.after(1500, self._refresh_status)

    def _apply_memory_gauge(self, status) -> None:
        """更新内存占用条 / 百分比 / 等级提示(按等级调色)。"""
        self.bar["value"] = status.percent
        self.percent_var.set("%.1f%%" % status.percent)
        level = common.percent_color(status.percent)
        self.level_var.set("内存占用%s · 可用 %s" % (level, human(status.available)))
        color = LEVEL_COLORS.get(level, ACCENT)
        ttk.Style(self).configure("Bar.Horizontal.TProgressbar",
                                  background=color, lightcolor=color, darkcolor=color)
        self.detail_var.set(
            "总计 %s     可用 %s     系统缓存 %s     提交 %s / %s" % (
                human(status.total), human(status.available),
                human(status.system_cache), human(status.commit_total),
                human(status.commit_limit)))

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                action = self._ui_queue.get_nowait()
                action()
        except queue.Empty:
            pass
        self.after(100, self._poll_ui_queue)

    def _poll_auto_clean(self) -> None:
        if self.auto_clean_seconds and (time.time() - self._last_auto_clean) >= self.auto_clean_seconds:
            self._last_auto_clean = time.time()
            self.log("自动清理触发(每 %d 秒一次)" % self.auto_clean_seconds)
            self._start_clean()
        self.after(1000, self._poll_auto_clean)

    def _poll_game_state(self) -> None:
        pid = self.booster.boosted_pid
        if pid and not gb.process_alive(pid):
            self.booster.restore()
            pid = 0
        self._game_status = ("游戏模式: 已加速 %s (PID %d)" % (
            self.booster.state.name, pid)) if pid else "游戏模式: 未加速"
        if pid:
            self._run_async(self.booster.clean_background, name="后台清理",
                            quiet=True, exclusive=False)
        self._sync_busy()
        self.after(3000, self._poll_game_state)

    # -- 通用后台任务 -----------------------------------------------------
    def _run_async(self, work, done=None, name: str = "任务", quiet: bool = False,
                   exclusive: bool = True) -> bool:
        """把耗时操作丢到后台线程, 结果通过 UI 队列回到主线程。

        exclusive=True : 独占任务(清理/扫描/一键优化), 同一时刻只允许一个,
                         后来的请求会被拒绝并提示, 避免重复执行危险操作。
        exclusive=False: 轻量只读任务(刷新列表/后台裁剪), 允许并发。
                         旧实现对所有任务都上同一把锁, 导致启动时
                         "刷新进程列表" 会把 "刷新启动项" 直接挤掉。
        """
        if exclusive and self._busy:
            if not quiet:
                self.log("上一个任务还在执行, 请稍候再试。")
            return False
        if exclusive:
            self._busy = True
        self._enter_busy(name)

        def runner() -> None:
            result = None
            try:
                result = work()
            except Exception as exc:
                self.log("[错误] %s 失败: %s" % (name, exc))
            finally:
                if exclusive:
                    self._busy = False
                self._ui_queue.put(lambda: self._leave_busy(name))
            if done is not None:
                self._ui_queue.put(lambda: done(result))

        threading.Thread(target=runner, daemon=True, name=name).start()
        return True

    # -- 忙碌指示 ---------------------------------------------------------
    def _enter_busy(self, name: str) -> None:
        self._busy_tasks.append(name)
        self._ui_queue.put(self._sync_busy)

    def _leave_busy(self, name: str) -> None:
        if name in self._busy_tasks:
            self._busy_tasks.remove(name)
        self._ui_queue.put(self._sync_busy)

    def _sync_busy(self) -> None:
        """按当前未完成的任务数开关状态栏滚动条, 并合成状态栏文案。"""
        if self._busy_tasks:
            if not getattr(self, "_busy_started", False):
                self.busy_bar.start(12)
                self._busy_started = True
            self.status_var.set("执行中: %s" % " / ".join(self._busy_tasks))
            return
        self.busy_bar.stop()
        self._busy_started = False
        parts = [self._game_status] if getattr(self, "_game_status", "") else []
        if self._last_summary:
            parts.append(self._last_summary)
        self.status_var.set("   |   ".join(parts) if parts else "就绪")

    def _on_f5(self, _event=None) -> None:
        """F5: 刷新当前标签页里的列表。"""
        try:
            index = self.notebook.index("current")
        except tk.TclError:
            index = 0
        if index == 2:
            self._on_refresh_processes()
        elif index == 3:
            self._on_refresh_startups()
        else:
            self._refresh_now()

    def _on_about(self) -> None:
        messagebox.showinfo(
            "关于 %s" % APP_TITLE,
            "%s v%s\n\n"
            "纯 Python 标准库实现的 Windows 系统清理工具, 零第三方依赖。\n\n"
            "能力: 内存清理 / 垃圾清理 / 游戏加速 / 启动项与大文件管理\n"
            "快捷键: F5 刷新当前列表\n"
            "当前权限: %s\n\n"
            "所有操作均在本机完成, 不含任何数据上传行为。"
            % (APP_TITLE, APP_VERSION,
               "管理员" if is_admin() else "普通用户(部分功能受限)"))

    def _center_on_screen(self) -> None:
        """把窗口摆到屏幕中上部。"""
        self.update_idletasks()
        width = self.winfo_width() or 980
        height = self.winfo_height() or 780
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 3)
        self.geometry("%dx%d+%d+%d" % (width, height, x, y))

    # -- 进程列表与游戏加速 -------------------------------------------------
    def _render_processes(self, processes) -> None:
        self.tree.delete(*self.tree.get_children())
        for proc in processes or []:
            self.tree.insert("", "end", values=(
                proc.pid, proc.name or "?", human(proc.working_set), human(proc.private)))
        self._apply_tree_stripes(self.tree)

    _PROC_SORT_ATTRS = {"pid": "pid", "name": "name",
                        "ws": "working_set", "private": "private"}

    def _sort_processes(self, column: str, toggle: bool = True) -> None:
        """点击列头排序(PID / 进程名 / 工作集 / 私有内存)。

        toggle=True  用户点击: 同列再点一次切换升/降序
        toggle=False 列表刷新后按当前偏好重排, 不翻转方向
        """
        attrs = self._PROC_SORT_ATTRS
        column = attrs.get(column) and column or "ws"
        attr = attrs[column]
        if toggle:
            if self._proc_sort == column:
                self._proc_reverse = not self._proc_reverse
            else:
                self._proc_sort, self._proc_reverse = column, True
        elif self._proc_sort in attrs:
            column, attr = self._proc_sort, attrs[self._proc_sort]
        if not self.process_list:
            return

        def key_of(proc):
            value = getattr(proc, attr)
            return value if isinstance(value, (int, float)) else str(value or "")

        self.process_list.sort(key=key_of, reverse=self._proc_reverse)
        self._render_processes(self.process_list)

    def _on_refresh_processes(self) -> None:
        def work():
            return list_processes(min_working_set=1)[:40]

        def done(processes) -> None:
            self.process_list = list(processes or [])
            # 沿用当前排序偏好(默认按工作集从大到小)
            self._sort_processes(self._proc_sort or "ws", toggle=False)

        self._run_async(work, done, name="刷新进程", quiet=True, exclusive=False)

    def _selected_process(self) -> Tuple[int, str]:
        selection = self.tree.selection()
        if not selection:
            return 0, ""
        values = self.tree.item(selection[0], "values")
        try:
            return int(values[0]), str(values[1])
        except (IndexError, ValueError):
            return 0, ""

    def _on_boost_selected(self) -> None:
        pid, name = self._selected_process()
        if not pid:
            messagebox.showinfo("提示", "请先在列表中选择一个进程(游戏)。")
            return
        self.booster.trim = True
        self.booster.purge_standby = bool(self.purge_var.get())
        self.log("正在加速 %s (PID %d) ..." % (name, pid))

        def work():
            return self.booster.boost(pid)

        self._run_async(work, name="加速进程")

    def _on_stop_boost(self) -> None:
        self.booster.restore()

    def _on_elevate(self) -> None:
        if is_admin():
            messagebox.showinfo("提示", "当前已经是管理员权限。")
            return
        if elevate_and_rerun([], script=os.path.abspath(__file__)):
            self.log("已请求以管理员身份重新启动, 本窗口即将关闭。")
            self.after(800, self.destroy)
        else:
            messagebox.showwarning("提权失败", "UAC 提权被取消或失败, 请右键 -> 以管理员身份运行。")

    def _on_close(self) -> None:
        try:
            self.booster.request_stop()
            self.booster.restore()
            self._save_config()
        finally:
            self.destroy()

    # -- 清理 -------------------------------------------------------------
    def _on_clean(self) -> None:
        self._start_clean()

    def _start_clean(self) -> None:
        profile = self.profile_var.get()
        prof = PROFILES.get(profile)
        if prof is None:
            profile = "standard"
        if not is_admin():
            self.log("提示: 非管理员权限只能裁剪工作集; 清空待机列表/文件缓存需要管理员。")

        preserve: List[int] = []
        if self.booster.boosted_pid:
            preserve.append(self.booster.boosted_pid)

        def work():
            return run_clean(profile=profile, preserve_pids=preserve)

        def done(report) -> None:
            if report is None:
                self.log("清理未完成。")
                return
            self.log("清理结果: 内存占用下降 %s, 空闲页增加 %s, 待机缓存减少 %s, 耗时 %.2f 秒" % (
                human(report.used_reduced), human(report.free_gained),
                human(report.standby_freed), report.elapsed))
            self._last_summary = "上次清理(%s) 下降 %s" % (
                prof.title if prof else profile, human(report.used_reduced))
            self._refresh_now()

        self.log("开始执行 %s ..." % (prof.title if prof else profile))
        self._run_async(work, done, name="清理")

    # -- 一键优化 -----------------------------------------------------------
    def _on_once(self) -> None:
        """整合入口: 垃圾清理(当前勾选类别) + 内存清理(当前档位) 顺序执行。"""
        profile = self.profile_var.get()
        if profile not in PROFILES:
            profile = "standard"
        keys = sorted(self.junk_selected) or list(cli.SAFE_JUNK_KEYS)
        if not messagebox.askyesno(
                "一键优化",
                "将依次执行:\n"
                "  ① 垃圾清理 %d 个类别 (可在「垃圾清理」页勾选)\n"
                "  ② 内存清理 - %s\n\n是否继续?" % (len(keys), PROFILES[profile].title)):
            return
        if not is_admin():
            self.log("提示: 非管理员权限下, 系统级垃圾与待机列表清理会自动跳过。")

        def work():
            return cli.run_once(profile=profile, junk_keys=keys,
                                progress=self.log)

        def done(summary) -> None:
            if not summary:
                self.log("一键优化未完成。")
                return
            junk = summary.get("junk", []) or []
            freed = sum(i["freed"] for i in junk)
            removed = sum(i["removed"] for i in junk)
            skipped = sum(i.get("skipped", 0) for i in junk)
            memory = summary.get("memory") or {}
            self.log("一键优化完成: 垃圾释放 %s (%d 个文件), 内存占用下降 %s, 耗时 %.1f 秒%s"
                     % (human(freed), removed, human(memory.get("used_reduced", 0)),
                        summary.get("elapsed", 0.0),
                        (", %d 项需管理员已跳过" % skipped) if skipped else ""))
            self._last_summary = "一键优化 释放 %s" % human(freed + memory.get("used_reduced", 0))
            self._refresh_now()
            self._on_scan_junk()

        self.log("开始一键优化(垃圾 %d 类 + 内存 %s) ..." % (
            len(keys), PROFILES[profile].title))
        self._run_async(work, done, name="一键优化")

    def _refresh_now(self) -> None:
        """立刻刷新一次状态显示(清理完成后调用, 不等待下一次轮询)。"""
        try:
            self._apply_memory_gauge(query_memory_status())
        except Exception as exc:  # 界面不能因为一次读取失败就崩掉
            self.log("[错误] 读取内存状态失败: %s" % exc)

    # -- 垃圾清理 ---------------------------------------------------------
    def _on_junk_tree_click(self, event) -> None:
        if self.junk_tree.identify_region(event.x, event.y) != "cell":
            return
        iid = self.junk_tree.identify_row(event.y)
        column = self.junk_tree.identify_column(event.x)
        if iid and column == "#1":
            self._toggle_junk(iid)

    def _toggle_junk(self, key: str) -> None:
        if key in self.junk_selected:
            self.junk_selected.discard(key)
            mark = "☐"
        else:
            self.junk_selected.add(key)
            mark = "☑"
        self.junk_tree.set(key, "sel", mark)
        self._update_junk_total()

    def _on_junk_select(self, select_all: bool) -> None:
        self.junk_selected = set(self.junk_cats.keys()) if select_all else set()
        for key, cat in self.junk_cats.items():
            self.junk_tree.set(key, "sel", "☑" if key in self.junk_selected else "☐")
        self._update_junk_total()

    def _update_junk_total(self) -> None:
        total = 0
        total_files = 0
        for key in self.junk_selected:
            result = self.junk_scan_results.get(key)
            if result:
                total += result.size
                total_files += result.files
        if self.junk_scan_results:
            self.junk_total_var.set("已选可清理: %s (%d 个文件)" % (jc.human(total), total_files))
        else:
            self.junk_total_var.set("尚未扫描")

    def _on_cancel_scan(self) -> None:
        """请求中断当前正在进行的扫描/清理(线程安全)。"""
        if self._busy_tasks:
            self._cancel_scan.set()
            self.log("已请求停止, 当前步骤完成后中断...")
        else:
            self.log("当前没有正在进行的扫描。")

    def _on_scan_junk(self) -> None:
        self.log("开始扫描垃圾文件, 请稍候...")
        self.junk_scan_results = {}
        self._cancel_scan.clear()

        def work():
            # with_paths=True: 把文件清单留下来, 清理时直接复用, 省掉第二次遍历
            return jc.scan_all(self.junk_cats, progress=self.log, with_paths=True,
                               should_cancel=self._cancel_scan.is_set)

        def done(results) -> None:
            for result in results or []:
                self.junk_scan_results[result.key] = result
                if self.junk_tree.exists(result.key):
                    self.junk_tree.set(result.key, "size", jc.human(result.size))
                    self.junk_tree.set(result.key, "files", str(result.files))
                    # 需要管理员的类别显式标注, 避免"选了却没清理"的困惑
                    category = self.junk_cats.get(result.key)
                    if category and category_needs_admin(category) and not is_admin():
                        self.junk_tree.set(result.key, "risk", "需管理员")
            self._update_junk_total()
            if self._cancel_scan.is_set():
                self.log("扫描已中断(结果只统计到中断位置)。")
            else:
                self.log("垃圾扫描完成, 点击「清理选中垃圾」释放空间。")

        self._run_async(work, done, name="扫描垃圾")

    def _on_clean_junk(self) -> None:
        keys = sorted(self.junk_selected)
        if not keys:
            messagebox.showinfo("提示", "请先选择要清理的类别。")
            return
        if not messagebox.askyesno("确认清理", "将清理 %d 个类别的垃圾文件, 是否继续?" % len(keys)):
            return
        self.log("开始清理 %d 个类别的垃圾文件..." % len(keys))
        self._cancel_scan.clear()
        # 复用刚扫描出的文件清单: 清理阶段不再重新遍历目录
        paths_map = {key: result.paths for key, result in self.junk_scan_results.items()
                     if result.paths}

        def work():
            return jc.clean_selected(keys, self.junk_cats, progress=self.log,
                                     paths_map=paths_map,
                                     should_cancel=self._cancel_scan.is_set)

        def done(results) -> None:
            rows = results or []
            freed = sum(r.freed for r in rows)
            removed = sum(r.removed for r in rows)
            failed = sum(r.failed for r in rows)
            skipped = sum(r.skipped for r in rows)
            extra = ""
            if failed:
                extra += ", %d 个被占用跳过" % failed
            if skipped:
                extra += ", %d 项因缺少管理员权限跳过" % skipped
            self.log("垃圾清理完成: 释放 %s, 删除 %d 个文件%s" % (
                jc.human(freed), removed, extra))
            self._last_summary = "上次垃圾清理 释放 %s" % jc.human(freed)
            self._on_scan_junk()

        self._run_async(work, done, name="清理垃圾")

    def _on_empty_recycle(self) -> None:
        def work():
            return jc.empty_recycle_bin()

        def done(result) -> None:
            if not result:
                self.log("清空回收站: 未完成")
                return
            ok, detail = result
            self.log("清空回收站: %s" % detail)
            self._on_scan_junk()

        self._run_async(work, done, name="清空回收站")

    # -- 系统工具 ---------------------------------------------------------
    def _on_refresh_startups(self) -> None:
        def work():
            return st.list_startup_items()

        def done(items) -> None:
            self.startup_items = items or []
            self.startup_tree.delete(*self.startup_tree.get_children())
            seen = set()
            for item in self.startup_items:
                # 防御: 万一将来出现重复 key, iid 冲突会让 Treeview 直接抛异常
                if item.key in seen:
                    continue
                seen.add(item.key)
                self.startup_tree.insert("", "end", iid=item.key, values=(
                    "启用" if item.enabled else "禁用", item.name,
                    item.command, item.display_source))
            self._apply_tree_stripes(self.startup_tree)

        self._run_async(work, done, name="刷新启动项", quiet=True, exclusive=False)

    def _selected_startup_item(self) -> Optional[st.StartupItem]:
        selection = self.startup_tree.selection()
        if not selection:
            return None
        for item in self.startup_items:
            if item.key == selection[0]:
                return item
        return None

    def _on_disable_startup(self) -> None:
        item = self._selected_startup_item()
        if not item:
            messagebox.showinfo("提示", "请先选择一个启动项。")
            return
        if not item.enabled:
            messagebox.showinfo("提示", "该启动项已经是禁用状态。")
            return
        ok, message = st.disable_startup_item(item)
        self.log(message)
        self._on_refresh_startups()

    def _on_enable_startup(self) -> None:
        item = self._selected_startup_item()
        if not item:
            messagebox.showinfo("提示", "请先选择一个启动项。")
            return
        if item.enabled:
            messagebox.showinfo("提示", "该启动项已经是启用状态。")
            return
        ok, message = st.enable_startup_item(item)
        self.log(message)
        self._on_refresh_startups()

    def _on_scan_large(self) -> None:
        directory = self.large_dir_var.get().strip()
        if not directory or not os.path.isdir(directory):
            messagebox.showinfo("提示", "请输入一个有效的目录路径。")
            return
        try:
            min_mb = max(1.0, float(self.large_min_var.get()))
        except ValueError:
            min_mb = 500.0
        min_size = int(min_mb * 1024 * 1024)
        self.log("开始扫描 %s 中大于 %s 的文件..." % (directory, jc.human(min_size)))
        self._cancel_scan.clear()

        def work():
            return st.scan_large_files(directory, min_size=min_size, top_n=200,
                                       progress=self.log,
                                       should_cancel=self._cancel_scan.is_set)

        def done(files) -> None:
            self.large_tree.delete(*self.large_tree.get_children())
            for f in files or []:
                self.large_tree.insert("", "end", values=(jc.human(f.size), f.path))
            self._apply_tree_stripes(self.large_tree)
            if self._cancel_scan.is_set():
                self.log("扫描已中断, 已列出中断前找到的 %d 个文件。" % len(files or []))
            else:
                self.log("大文件扫描完成: 找到 %d 个大于 %s 的文件。" % (
                    len(files or []), jc.human(min_size)))

        self._run_async(work, done, name="扫描大文件")

    def _on_open_large_folder(self) -> None:
        selection = self.large_tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选择一个文件。")
            return
        path = self.large_tree.item(selection[0], "values")[1]
        try:
            os.startfile(os.path.dirname(path))  # type: ignore[attr-defined]
        except OSError as exc:
            self.log("无法打开文件夹: %s" % exc)

    # -- 设置项 -----------------------------------------------------------
    def _on_auto_changed(self, _event=None) -> None:
        self.auto_clean_seconds = int(AUTO_INTERVALS.get(self.auto_var.get(), 0))
        self._last_auto_clean = time.time()
        self._save_config()
        self.log("自动清理: %s" % ("已关闭" if not self.auto_clean_seconds
                                else "每 %d 分钟一次" % (self.auto_clean_seconds // 60)))

    def _on_trim_interval_changed(self, _event=None) -> None:
        try:
            value = max(30, int(float(self.trim_interval_var.get())))
        except ValueError:
            value = 300
        self.trim_interval_var.set(str(value))
        self.booster.trim_interval = value
        self._save_config()
        self.log("游戏模式后台清理间隔: %d 秒" % value)

    def _on_game_auto_toggle(self) -> None:
        enabled = bool(self.game_auto_var.get())
        self._save_config()
        if enabled:
            if self._game_thread and self._game_thread.is_alive():
                return
            self.booster.trim = True
            self.booster.purge_standby = bool(self.purge_var.get())
            self._game_thread = threading.Thread(
                target=self.booster.run_auto, kwargs={"poll": 5.0},
                daemon=True, name="game-auto")
            self._game_thread.start()
            self.log("游戏模式: 已开启自动检测(检测到全屏游戏会自动提升优先级并清理后台内存)")
        else:
            self.booster.request_stop()
            self._game_thread = None
            self.booster.restore()
            self.log("游戏模式: 已关闭自动检测")


def main() -> int:
    enable_high_dpi()  # 必须在创建 Tk 之前调用, 否则高分屏上会发虚
    app = CleanerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
