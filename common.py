#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.py - cleaner 各模块共用的基础设施

把原先散落在 memory_cleaner / junk_cleaner / system_tools / game_booster 里
重复实现的部分收敛到一处:

  * ``human()``     统一的字节格式化(原先被复制了 3 份)
  * 日志            统一的文件/控制台日志配置
  * 管理员判定/UAC   ``is_admin()`` / ``elevate_and_rerun()``
  * 环境变量路径      ``WIN_PATHS``
  * winreg 辅助      ``winreg_module()`` / ``reg_read()`` / ``reg_write()``
  * 小工具           ``Timer`` / ``format_bar()`` / ``TrackedCounter``

约束:
  * 只用标准库, 不引入第三方依赖
  * 在非 Windows 平台上 import 不报错, 相关函数降级返回安全值 --
    这样纯逻辑部分(格式化/统计)可以在任意平台上被单元测试覆盖
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from typing import Dict, Iterable, Optional, Sequence, Tuple

APP_NAME = "cleaner"

MB = 1024 * 1024
GB = 1024 * MB

IS_WINDOWS = os.name == "nt"

# 打包成 exe 后(PyInstaller), __file__ 指向临时解压目录 _MEIPASS, 那个目录每次运行
# 都会变、退出即删。所以配置/日志/备份必须落到 exe 自己所在的目录, 否则设置无法保存。
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _detect_base_dir() -> str:
    """程序"数据目录": 开发时是源码目录, 打包后是 exe 所在目录。"""
    if IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _detect_base_dir()

LOG = logging.getLogger("cleaner")

_SIZE_UNITS: Tuple[str, ...] = ("B", "KB", "MB", "GB", "TB")


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------
def human(nbytes: Optional[float], precision: int = 1) -> str:
    """把字节数格式化成易读字符串, 例如 1536 -> "1.5 KB"。"""
    if nbytes is None:
        return "-"
    value = float(nbytes)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit in _SIZE_UNITS:
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return "%s%d %s" % (sign, int(value), unit)
            return "%s%.*f %s" % (sign, precision, value, unit)
        value /= 1024.0
    return "-"


def format_bar(percent: float, width: int = 30) -> str:
    """生成文本进度条, 例如 "[######..........]  20.0%"。"""
    percent = max(0.0, min(100.0, float(percent)))
    filled = int(round(percent / 100.0 * width))
    return "[%s%s] %5.1f%%" % ("#" * filled, "." * (width - filled), percent)


def percent_color(percent: float) -> str:
    """按占用率给个粗略的健康等级, 供界面/报告使用。"""
    if percent >= 90:
        return "危险"
    if percent >= 75:
        return "偏高"
    if percent >= 50:
        return "正常"
    return "充裕"


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def default_log_path() -> str:
    """日志文件路径(与程序同目录)。"""
    return os.path.join(BASE_DIR, "cleaner.log")


def setup_logging(verbose: bool = False,
                  log_file: Optional[str] = None,
                  quiet: bool = False) -> None:
    """配置全局日志: 始终写文件(便于事后排查), 按需写 stderr。

    verbose=True  打开 DEBUG 级别并在控制台输出
    quiet=True    完全静默(只保留文件)
    """
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.handlers.clear()
    LOG.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                  "%Y-%m-%d %H:%M:%S")
    target = log_file if log_file is not None else default_log_path()
    try:
        handler = logging.FileHandler(target, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.setLevel(logging.DEBUG)
        LOG.addHandler(handler)
    except OSError:
        pass  # 日志写不了不应该让主流程失败

    if verbose and not quiet:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        LOG.addHandler(console)


class Timer:
    """计时上下文管理器, 用于统计各步骤耗时。

    with Timer() as t:
        do_something()
    print(t.elapsed)
    """

    def __init__(self) -> None:
        self.started = 0.0
        self.elapsed = 0.0

    def __enter__(self) -> "Timer":
        self.started = time.time()
        return self

    def __exit__(self, *_exc) -> bool:
        self.elapsed = time.time() - self.started
        return False


# ---------------------------------------------------------------------------
# 权限 / UAC 提权
# ---------------------------------------------------------------------------
def winreg_module():
    """返回 winreg 模块; 非 Windows 或缺失时返回 None(避免到处 try/import)。"""
    if not IS_WINDOWS:
        return None
    try:
        import winreg  # noqa: WPS433 (Windows 专用标准库)
        return winreg
    except ImportError:
        return None


def is_admin() -> bool:
    """当前进程是否以管理员权限运行(非 Windows 恒为 False)。"""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.WinDLL("shell32", use_last_error=True).IsUserAnAdmin())
    except Exception:  # pragma: no cover - 理论上不会出错
        return False


def elevate_and_rerun(extra_args: Optional[Sequence[str]] = None,
                      script: Optional[str] = None) -> bool:
    """通过 UAC 以管理员身份重新启动本脚本(或指定脚本)。

    成功发起返回 True(不代表子进程执行成功)。已经提权失败时不抛异常, 由调用方降级处理。
    """
    if not IS_WINDOWS:
        return False

    import ctypes
    from ctypes import wintypes

    args = [str(a) for a in (extra_args if extra_args is not None else sys.argv[1:])]
    args = [a for a in args if a != "--elevate"]
    joined = " ".join(('"%s"' % a if " " in a else a) for a in args)

    target = script or os.path.abspath(sys.argv[0])
    if getattr(sys, "frozen", False):  # 打包成 exe 后的情况
        exe, params = sys.executable, joined
    else:
        exe = sys.executable
        params = '"%s" %s' % (target, joined)

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    result = shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    return bool(result and result > 32)


# ---------------------------------------------------------------------------
# Windows 常用路径
# ---------------------------------------------------------------------------
def env_path(name: str, default: str) -> str:
    """读环境变量, 为空时回退到默认值。"""
    value = os.environ.get(name, "")
    return value if value else default


def build_win_paths() -> Dict[str, str]:
    """收集清理/系统工具需要的 Windows 目录(按当前环境动态展开)。"""
    home = os.path.expanduser("~")
    return {
        "home": home,
        "localappdata": env_path("LOCALAPPDATA", os.path.join(home, "AppData", "Local")),
        "appdata": env_path("APPDATA", os.path.join(home, "AppData", "Roaming")),
        "programdata": env_path("PROGRAMDATA", r"C:\ProgramData"),
        "windir": env_path("WINDIR", r"C:\Windows"),
        # 必须用顶层 import: 动态 __import__ 不会被 PyInstaller 的静态分析发现,
        # 打包后会报 ModuleNotFoundError(实测踩过一次)
        "temp": tempfile.gettempdir(),
        "desktop": os.path.join(home, "Desktop"),
    }


WIN_PATHS: Dict[str, str] = build_win_paths()


# ---------------------------------------------------------------------------
# 注册表辅助
# ---------------------------------------------------------------------------
def reg_root(root_name: str):
    """把 "HKCU"/"HKLM" 这样的名字转成 winreg 根键句柄常量。"""
    winreg = winreg_module()
    if not winreg:
        raise RuntimeError("当前平台不支持 winreg")
    return {
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
        "HKU": winreg.HKEY_USERS,
    }[root_name]


def reg_read(root_name: str, path: str, name: str) -> Tuple[bool, Optional[object]]:
    """读注册表值: 返回 (是否存在, 值)。键/值不存在时返回 (False, None)。"""
    winreg = winreg_module()
    if not winreg:
        return False, None
    try:
        with winreg.OpenKey(reg_root(root_name), path, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, name)
            return True, value
    except OSError:
        return False, None


def reg_write(root_name: str, path: str, name: str, value: object,
              kind: Optional[int] = None) -> Tuple[bool, str]:
    """写注册表值(自动建键)。kind 为空时按 Python 类型推断 REG_SZ / REG_DWORD。"""
    winreg = winreg_module()
    if not winreg:
        return False, "当前平台不支持 winreg"
    if kind is None:
        kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
    try:
        with winreg.CreateKeyEx(reg_root(root_name), path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, kind, value)
        return True, "已写入 %s = %s" % (name, value)
    except OSError as exc:
        return False, "写入 %s 失败: %s (通常需要管理员权限)" % (name, exc)


def reg_delete_value(root_name: str, path: str, name: str) -> Tuple[bool, str]:
    """删除注册表值。值本身不存在时视为成功。"""
    winreg = winreg_module()
    if not winreg:
        return False, "当前平台不支持 winreg"
    try:
        with winreg.OpenKey(reg_root(root_name), path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True, "已删除 %s" % name
    except FileNotFoundError:
        return True, "%s 原本就不存在" % name
    except OSError as exc:
        return False, "删除 %s 失败: %s" % (name, exc)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def sum_sizes(items: Iterable[object], attr: str = "size") -> int:
    """对一组对象的某个整数字段求和(用于"合计可清理"这类统计)。"""
    total = 0
    for item in items:
        total += int(getattr(item, attr, 0) or 0)
    return total


def safe_int(text: object, default: int = 0) -> int:
    """把可能是 "512"、"512MB"、None 的输入安全转成整数。"""
    if text is None:
        return default
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
