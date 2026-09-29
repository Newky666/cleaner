#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_booster.py - 游戏模式(进程优先级 / EcoQoS / 定时清理 / 系统调优)

针对"打游戏更流畅"做的四件事, 每一项都可以单独关闭:

  1. 提升游戏进程调度优先级   SetPriorityClass(HIGH_PRIORITY_CLASS)
  2. 提升游戏进程内存优先级   SetProcessInformation(ProcessMemoryPriority)
     让内核更不愿意把游戏的物理页换出去(减少卡顿)
  3. 关闭游戏进程的能效节流   SetProcessInformation(ProcessPowerThrottling)
     Win10/11 会给"后台感"的进程降频(EcoQoS), 关掉它可避免掉频
  4. 游戏运行时定时清理后台进程工作集 / 清空待机列表(绝不碰游戏自身)

退出(或游戏结束)时会自动把优先级与节流状态恢复成原样。

用法:
  python game_booster.py list -n 20                 # 列出候选进程(按内存排序)
  python game_booster.py boost --name game.exe      # 加速指定进程并持续清理
  python game_booster.py boost --pid 1234 --duration 3600
  python game_booster.py auto                       # 自动检测全屏游戏并加速
  python game_booster.py tweaks --show              # 查看 MMCSS/游戏任务 注册表调优现状
  python game_booster.py tweaks --apply             # 应用经典游戏调优(需管理员, 自动备份)
  python game_booster.py tweaks --restore           # 还原注册表
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
from common import winreg_module  # noqa: E402
import memory_cleaner as mc  # noqa: E402  (与本模块同目录)
from memory_cleaner import (  # noqa: E402
    DEFAULT_SKIP_NAMES, LOG, human, is_admin, is_fullscreen_app_running,
    foreground_process, list_processes, process_name, process_exe,
    query_memory_status, query_page_lists, setup_logging, default_log_path,
    trim_working_sets, nt_memory_list_command, MEMORY_PURGE_STANDBY_LIST,
    enable_clean_privileges,
)

kernel32 = mc.kernel32
ntdll = mc.ntdll

# 调度优先级
NORMAL_PRIORITY_CLASS = 0x00000020
IDLE_PRIORITY_CLASS = 0x00000040
HIGH_PRIORITY_CLASS = 0x00000080
REALTIME_PRIORITY_CLASS = 0x00000100
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000

PRIORITY_CLASS_NAMES = {
    IDLE_PRIORITY_CLASS: "低(Idle)",
    BELOW_NORMAL_PRIORITY_CLASS: "低于正常",
    NORMAL_PRIORITY_CLASS: "正常",
    ABOVE_NORMAL_PRIORITY_CLASS: "高于正常",
    HIGH_PRIORITY_CLASS: "高",
    REALTIME_PRIORITY_CLASS: "实时",
}

# 内存优先级(公开枚举最高只能到 Normal = 5)
MEMORY_PRIORITY_VERY_LOW = 1
MEMORY_PRIORITY_LOW = 2
MEMORY_PRIORITY_MEDIUM = 3
MEMORY_PRIORITY_BELOW_NORMAL = 4
MEMORY_PRIORITY_NORMAL = 5

# PROCESS_INFORMATION_CLASS
PROCESS_INFO_MEMORY_PRIORITY = 0
PROCESS_INFO_POWER_THROTTLING = 4

# PROCESS_POWER_THROTTLING_STATE
POWER_THROTTLING_CURRENT_VERSION = 1
POWER_THROTTLING_EXECUTION_SPEED = 0x1
POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4

PROCESS_ACCESS_FOR_BOOST = (
    mc.PROCESS_SET_INFORMATION | mc.PROCESS_QUERY_LIMITED_INFORMATION | mc.PROCESS_SET_QUOTA
)


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG),
                ("ControlMask", wintypes.ULONG),
                ("StateMask", wintypes.ULONG)]


ntdll.NtSetTimerResolution.argtypes = [
    ctypes.c_ulong, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ulong)]
ntdll.NtSetTimerResolution.restype = ctypes.c_long
ntdll.NtQueryTimerResolution.argtypes = [
    ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
    ctypes.POINTER(ctypes.c_ulong)]
ntdll.NtQueryTimerResolution.restype = ctypes.c_long

kernel32.GetProcessInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.GetProcessInformation.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# 计时器精度(减少调度延迟, 可选)
# ---------------------------------------------------------------------------
def query_timer_resolution() -> Tuple[float, float, float]:
    """返回 (当前, 最小, 最大) 计时器精度, 单位毫秒。"""
    cur = ctypes.c_ulong(0)
    mn = ctypes.c_ulong(0)
    mx = ctypes.c_ulong(0)
    if ntdll.NtQueryTimerResolution(
            ctypes.byref(mn), ctypes.byref(mx), ctypes.byref(cur)) < 0:
        return 0.0, 0.0, 0.0
    return cur.value / 10000.0, mn.value / 10000.0, mx.value / 10000.0


def set_timer_resolution(milliseconds: float = 0.5) -> Tuple[bool, str]:
    """把计时器精度调到目标值(0.5ms = 2000Hz)。

    注意: 从 Windows 10 2004 起, 计时器精度只对"调用了该 API 的进程"生效,
    所以它对其它程序的影响有限; 保留此功能是因为很多游戏本身也会这么调。
    """
    if milliseconds <= 0:
        return False, "参数无效"
    desired = int(round(milliseconds * 10000.0))  # 毫秒 -> 100ns 单位
    actual = ctypes.c_ulong(0)
    status = ntdll.NtSetTimerResolution(desired, 1, ctypes.byref(actual))
    if status < 0:
        return False, "NtSetTimerResolution 失败: %s" % mc.nt_status_message(status)
    return True, "计时器精度已设为 %.2f ms(内核实际 %.3f ms)" % (
        milliseconds, actual.value / 10000.0)


def release_timer_resolution() -> None:
    """恢复系统默认计时器精度。"""
    actual = ctypes.c_ulong(0)
    ntdll.NtSetTimerResolution(0, 0, ctypes.byref(actual))


# ---------------------------------------------------------------------------
# 单个进程的调优
# ---------------------------------------------------------------------------
def set_memory_priority(handle: int, priority: int = MEMORY_PRIORITY_NORMAL) -> bool:
    """设置进程内存优先级(1~5, 5 = Normal, 公开 API 的最高档)。"""
    value = ctypes.c_ulong(int(priority))
    return bool(kernel32.SetProcessInformation(
        handle, PROCESS_INFO_MEMORY_PRIORITY, ctypes.byref(value), ctypes.sizeof(value)))


def set_power_throttling(handle: int, disable: bool = True) -> bool:
    """关闭/恢复进程的能效节流(EcoQoS)。"""
    state = PROCESS_POWER_THROTTLING_STATE()
    state.Version = POWER_THROTTLING_CURRENT_VERSION
    state.ControlMask = POWER_THROTTLING_EXECUTION_SPEED | POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
    state.StateMask = 0 if disable else POWER_THROTTLING_EXECUTION_SPEED
    return bool(kernel32.SetProcessInformation(
        handle, PROCESS_INFO_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state)))


def read_memory_priority(handle: int) -> Optional[int]:
    value = ctypes.c_ulong(0)
    if kernel32.GetProcessInformation(
            handle, PROCESS_INFO_MEMORY_PRIORITY, ctypes.byref(value), ctypes.sizeof(value)):
        return int(value.value)
    return None


def read_power_throttling(handle: int) -> Optional[int]:
    state = PROCESS_POWER_THROTTLING_STATE()
    state.Version = POWER_THROTTLING_CURRENT_VERSION
    if kernel32.GetProcessInformation(
            handle, PROCESS_INFO_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state)):
        return int(state.StateMask)
    return None


def open_for_boost(pid: int) -> int:
    """以调优所需的权限打开进程, 失败返回 0。"""
    return kernel32.OpenProcess(PROCESS_ACCESS_FOR_BOOST, False, pid)


@dataclass
class BoostState:
    """一次加速操作的状态, 用于事后完整还原。"""
    pid: int = 0
    name: str = ""
    original_priority: int = 0
    priority_set: bool = False
    original_memory_priority: Optional[int] = None   # 还原时能精确写回原值
    memory_priority_set: bool = False
    ecoqos_disabled: bool = False
    timer_resolution: Optional[float] = None
    applied_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD


def process_alive(pid: int) -> bool:
    """进程是否仍在运行。"""
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


# 控制台事件(Windows)
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

_console_handlers: List[object] = []  # 回调必须保持引用, 否则会被垃圾回收


def install_console_restore_handler(booster: "GameBooster") -> bool:
    """注册控制台事件处理: 直接点窗口 X 关闭时也能恢复进程设置。

    Ctrl+C 仍走正常的 KeyboardInterrupt 流程(那边也有 finally 恢复)。
    """
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _handler(event: int) -> bool:
        if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            try:
                booster.request_stop()
                booster.restore()
            except Exception:
                LOG.debug("控制台事件恢复失败", exc_info=True)
            return True
        return False

    callback = handler_type(_handler)
    _console_handlers.append(callback)
    try:
        kernel32.SetConsoleCtrlHandler(callback, True)
        return True
    except Exception:
        LOG.debug("注册控制台事件处理失败", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# 游戏模式控制器
# ---------------------------------------------------------------------------
class GameBooster:
    """把游戏进程调到高性能状态, 并在游戏运行时持续清理后台内存。"""

    def __init__(self,
                 trim: bool = True,
                 trim_interval: int = 300,
                 purge_standby: bool = True,
                 timer_resolution_ms: Optional[float] = None,
                 preserve_extra: Sequence[int] = ()):
        self.trim = bool(trim)
        self.trim_interval = max(30, int(trim_interval))
        self.purge_standby = bool(purge_standby)
        self.timer_resolution_ms = timer_resolution_ms
        self.preserve_extra = {int(p) for p in preserve_extra}
        self.state: Optional[BoostState] = None
        self.stats = {"trims": 0, "purged": 0, "freed": 0}
        # 默认不额外输出(调用方可通过 on_event 接管, 例如命令行设为 print)
        self.on_event: Callable[[str], None] = lambda message: None
        self._stop_event = threading.Event()
        self._last_trim = 0.0

    # -- 日志 -------------------------------------------------------------
    def _emit(self, message: str) -> None:
        LOG.info(message)
        try:
            self.on_event(message)
        except Exception:
            LOG.debug("on_event 回调异常", exc_info=True)

    @property
    def boosted_pid(self) -> int:
        return self.state.pid if self.state else 0

    # -- 加速 / 恢复 -------------------------------------------------------
    def boost(self, pid: int) -> BoostState:
        """对指定进程开启高性能模式。"""
        name = process_name(pid) or ("PID %d" % pid)
        if self.state and self.state.pid == pid:
            return self.state
        if self.state:
            self.restore()

        handle = open_for_boost(pid)
        if not handle:
            raise RuntimeError("无法打开进程 %s (PID %d), 请尝试以管理员身份运行" % (name, pid))

        state = BoostState(pid=pid, name=name, applied_at=time.time())
        try:
            state.original_priority = int(kernel32.GetPriorityClass(handle))
            # 先记下原始内存优先级, 退出时才能精确还原(旧实现只置不算), 而不是留之谜之值
            state.original_memory_priority = read_memory_priority(handle)
            state.priority_set = bool(kernel32.SetPriorityClass(handle, HIGH_PRIORITY_CLASS))
            state.memory_priority_set = set_memory_priority(handle, MEMORY_PRIORITY_NORMAL)
            state.ecoqos_disabled = set_power_throttling(handle, True)
        finally:
            kernel32.CloseHandle(handle)

        if self.timer_resolution_ms:
            ok, detail = set_timer_resolution(self.timer_resolution_ms)
            if ok:
                state.timer_resolution = self.timer_resolution_ms
                self._emit(detail)

        self.state = state
        self._last_trim = 0.0
        self._emit("[加速] %s (PID %d): 优先级 %s -> 高; 内存优先级=%s; 关闭能效节流=%s" % (
            name, pid,
            PRIORITY_CLASS_NAMES.get(state.original_priority, str(state.original_priority)),
            "成功" if state.memory_priority_set else "不支持",
            "成功" if state.ecoqos_disabled else "不支持"))
        return state

    def restore(self) -> None:
        """恢复被加速进程的原始设置。"""
        state = self.state
        if not state:
            return
        self.state = None

        handle = open_for_boost(state.pid)
        if handle:
            try:
                if state.original_priority:
                    kernel32.SetPriorityClass(handle, state.original_priority)
                if state.ecoqos_disabled:
                    set_power_throttling(handle, False)
                if state.memory_priority_set and state.original_memory_priority:
                    set_memory_priority(handle, state.original_memory_priority)
            finally:
                kernel32.CloseHandle(handle)
        if state.timer_resolution:
            release_timer_resolution()
        self._emit("[恢复] %s (PID %d) 已还原优先级/内存优先级/节流设置" % (
            state.name, state.pid))

    # -- 后台清理 ---------------------------------------------------------
    def clean_background(self, force: bool = False) -> Dict[str, int]:
        """裁剪后台进程工作集(必要时清空待机列表), 绝不碰游戏本身。"""
        now = time.time()
        if not force and (now - self._last_trim) < self.trim_interval:
            return {}
        self._last_trim = now
        if not self.trim:
            return {}

        exclude = set(self.preserve_extra)
        if self.state:
            exclude.add(self.state.pid)
        stats = trim_working_sets(DEFAULT_SKIP_NAMES, exclude_pids=exclude)
        self.stats["trims"] += 1
        self.stats["freed"] += stats["freed"]
        self._emit("[清理] 裁剪 %d 个后台进程, 回收约 %s" % (
            stats["trimmed"], human(stats["freed"])))

        if self.purge_standby:
            if not is_admin():
                self._emit("[提示] 清空待机列表需要管理员权限, 已跳过")
            else:
                enable_clean_privileges()
                ok, detail = nt_memory_list_command(MEMORY_PURGE_STANDBY_LIST)
                if ok:
                    self.stats["purged"] += 1
                    self._emit("[清理] 已清空待机列表, 把缓存页交还给系统")
                else:
                    LOG.debug("清空待机列表失败: %s", detail)
        return stats

    # -- 自动模式 ---------------------------------------------------------
    def request_stop(self) -> None:
        """请求停止自动模式(线程安全)。"""
        self._stop_event.set()

    def run_auto(self, poll: float = 5.0, duration: float = 0.0,
                 clean_when_fullscreen: bool = True) -> None:
        """自动检测全屏游戏: 检测到就加速 + 定时清理; 游戏退出后自动恢复。

        poll: 检测间隔秒数; duration: 运行总时长(0 = 一直运行到 Ctrl+C / stop)。
        """
        enable_clean_privileges()
        self._stop_event.clear()
        self._last_trim = 0.0
        started = time.time()
        self._emit("[自动模式] 已启动, 每 %.0f 秒检查一次全屏程序" % poll)

        try:
            while not self._stop_event.is_set():
                if duration and (time.time() - started) >= duration:
                    self._emit("[自动模式] 已达到设定的运行时长, 退出")
                    break

                if is_fullscreen_app_running():
                    pid, _name = foreground_process()
                    if pid and pid != os.getpid():
                        if not self.state or self.state.pid != pid:
                            try:
                                self.boost(pid)
                            except RuntimeError as exc:
                                self._emit("[跳过] %s" % exc)
                        elif clean_when_fullscreen:
                            self.clean_background()
                elif self.state and not process_alive(self.state.pid):
                    self.restore()

                self._stop_event.wait(max(1.0, float(poll)))
        finally:
            self.restore()
            self._emit("[自动模式] 已停止(累计清理 %d 次, 约回收 %s)" % (
                self.stats["trims"], human(self.stats["freed"])))


# ---------------------------------------------------------------------------
# 注册表游戏调优(MMCSS 多媒体调度 / 游戏任务优先级), 可备份可还原
# ---------------------------------------------------------------------------
BACKUP_PATH = os.path.join(common.BASE_DIR, "game_tweaks_backup.json")
MMCSS_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
GAMES_TASK_KEY = MMCSS_KEY + r"\Tasks\Games"

# (注册表路径, 值名, 目标值, 类型)
TWEAKS: List[Tuple[str, str, object, str]] = [
    (MMCSS_KEY, "SystemResponsiveness", 10, "dword"),
    (MMCSS_KEY, "NetworkThrottlingIndex", 0xFFFFFFFF, "dword"),
    (GAMES_TASK_KEY, "GPU Priority", 8, "dword"),
    (GAMES_TASK_KEY, "Priority", 6, "dword"),
    (GAMES_TASK_KEY, "Scheduling Category", "High", "string"),
    (GAMES_TASK_KEY, "SFIO Priority", "High", "string"),
]


def _read_reg(root: int, path: str, name: str) -> Tuple[bool, Optional[object]]:
    """读 HKLM 下的某个注册表值: 返回 (是否存在, 值)。"""
    winreg = winreg_module()
    if not winreg:
        return False, None
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, name)
            return True, value
    except OSError:
        return False, None


def read_tweaks() -> List[dict]:
    """读取调优项的当前值, 用于界面/命令行展示。"""
    winreg = winreg_module()
    if not winreg:
        return []
    items = []
    for path, name, target, kind in TWEAKS:
        exists, value = _read_reg(winreg.HKEY_LOCAL_MACHINE, path, name)
        items.append({
            "key": path,
            "name": name,
            "current": value if exists else None,
            "target": target,
            "applied": bool(exists and value == target),
            "type": kind,
        })
    return items


def apply_tweaks(backup_path: str = BACKUP_PATH) -> List[str]:
    """写入游戏调优注册表项(首次会自动备份原始值)。"""
    winreg = winreg_module()
    if not winreg:
        return ["当前系统缺少 winreg 模块, 已跳过注册表调优"]
    messages: List[str] = []

    if not os.path.exists(backup_path):
        backup = []
        for path, name, _target, kind in TWEAKS:
            exists, value = _read_reg(winreg.HKEY_LOCAL_MACHINE, path, name)
            backup.append({"key": path, "name": name, "exists": exists,
                           "value": value, "type": kind})
        try:
            with open(backup_path, "w", encoding="utf-8") as fp:
                json.dump(backup, fp, ensure_ascii=False, indent=2)
            messages.append("已备份原始注册表值 -> %s" % backup_path)
        except OSError as exc:
            messages.append("备份失败(不影响继续写入): %s" % exc)

    for path, name, target, kind in TWEAKS:
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path, 0,
                                    winreg.KEY_SET_VALUE) as key:
                if kind == "dword":
                    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(target))
                else:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(target))
            messages.append("已设置 %s = %s" % (name, target))
        except OSError as exc:
            messages.append("设置 %s 失败: %s (需要管理员权限)" % (name, exc))
    return messages


def restore_tweaks(backup_path: str = BACKUP_PATH) -> List[str]:
    """从备份文件还原注册表调优。"""
    winreg = winreg_module()
    if not winreg:
        return ["当前系统缺少 winreg 模块"]
    if not os.path.exists(backup_path):
        return ["找不到备份文件 %s, 无法还原(可能从未应用过调优)" % backup_path]

    try:
        with open(backup_path, "r", encoding="utf-8") as fp:
            backup = json.load(fp)
    except (OSError, ValueError) as exc:
        return ["读取备份失败: %s" % exc]

    messages: List[str] = []
    for item in backup:
        path, name = item.get("key", ""), item.get("name", "")
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path, 0,
                                    winreg.KEY_SET_VALUE) as key:
                if item.get("exists"):
                    kind = winreg.REG_DWORD if item.get("type") == "dword" else winreg.REG_SZ
                    winreg.SetValueEx(key, name, 0, kind, item.get("value"))
                    messages.append("已还原 %s = %s" % (name, item.get("value")))
                else:
                    try:
                        winreg.DeleteValue(key, name)
                        messages.append("已删除新增项 %s" % name)
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            messages.append("还原 %s 失败: %s (需要管理员权限)" % (name, exc))

    try:
        os.remove(backup_path)
    except OSError:
        pass
    return messages


# ---------------------------------------------------------------------------
# 命令行接口
# ---------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> int:
    processes = list_processes(min_working_set=1)[:max(1, args.top)]
    status = query_memory_status()
    pages = query_page_lists()
    print("当前内存: %s 已用 %s / %s" % (mc.format_bar(status.percent),
                                     human(status.used), human(status.total)))
    if pages:
        print("待机页 %s, 空闲页 %s" % (human(pages.standby), human(pages.free + pages.zero)))
    print("%-8s %-36s %12s" % ("PID", "进程名(可用于 --name)", "工作集"))
    print("-" * 60)
    for proc in processes:
        print("%-8d %-36s %12s" % (proc.pid, (proc.name or "?")[:36], human(proc.working_set)))
    return 0


def find_process_by_name(name: str,
                         processes: Optional[List[mc.ProcessInfo]] = None
                         ) -> Optional[mc.ProcessInfo]:
    """按文件名/路径片段找进程。

    list_processes() 在进程快照路径下 exe 字段为空(省掉 N 次 OpenProcess),
    所以这里先按名字匹配; 名字没命中时才为内存占用最大的若干个进程补充完整路径再匹配。
    """
    wanted = name.strip().lower()
    procs = processes if processes is not None else list_processes()
    matches = [p for p in procs if p.name == wanted or wanted in p.name]
    if not matches:
        for proc in procs[:20]:
            if not proc.exe:
                proc.exe = process_exe(proc.pid)
        matches = [p for p in procs if wanted in (p.exe or "").lower()]
    if not matches:
        return None
    matches.sort(key=lambda p: p.working_set, reverse=True)
    return matches[0]


def cmd_boost(args: argparse.Namespace) -> int:
    pid = int(args.pid or 0)
    if not pid and args.name:
        found = find_process_by_name(args.name)
        if not found:
            print("没有找到名称匹配 %s 的进程" % args.name)
            return 2
        pid = found.pid
        print("匹配到 %s (PID %d, 工作集 %s)" % (found.name, pid,
                                            human(found.working_set)))
        print("匹配到 %s (PID %d, 工作集 %s)" % (matches[0].name, pid,
                                          human(matches[0].working_set)))
    if not pid:
        print("请用 --pid 或 --name 指定要加速的游戏进程")
        return 2
    if not is_admin():
        print("提示: 非管理员权限只能调整当前用户拥有的进程, 建议以管理员运行。")

    booster = GameBooster(trim=not args.no_trim,
                          trim_interval=args.trim_interval,
                          purge_standby=not args.no_purge,
                          timer_resolution_ms=args.timer if args.timer > 0 else None)
    booster.on_event = print
    install_console_restore_handler(booster)  # 直接关窗口也能恢复设置
    try:
        booster.boost(pid)
        booster.clean_background(force=True)
    except RuntimeError as exc:
        print("错误: %s" % exc)
        return 1

    print("加速已生效。按 Ctrl+C 结束并自动恢复原始设置。")
    started = time.time()
    try:
        while True:
            if args.duration and (time.time() - started) >= args.duration:
                break
            if not process_alive(pid):
                print("目标进程已退出, 结束加速。")
                break
            time.sleep(min(5.0, max(1.0, booster.trim_interval / 6.0)))
            booster.clean_background()
    except KeyboardInterrupt:
        print("\n收到中断信号。")
    finally:
        booster.restore()
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    booster = GameBooster(trim=True,
                          trim_interval=args.trim_interval,
                          purge_standby=not args.no_purge,
                          timer_resolution_ms=args.timer if args.timer > 0 else None)
    booster.on_event = print
    install_console_restore_handler(booster)  # 直接关窗口也能恢复设置
    if not is_admin():
        print("提示: 非管理员权限只能调整当前用户拥有的进程, 建议以管理员运行。")
    print("自动模式运行中(检测到全屏游戏会自动加速)。按 Ctrl+C 退出。")
    try:
        booster.run_auto(poll=args.poll, duration=args.duration)
    except KeyboardInterrupt:
        print("\n收到中断信号, 正在恢复...")
        booster.request_stop()
        booster.restore()
    return 0


def cmd_tweaks(args: argparse.Namespace) -> int:
    if args.apply:
        for message in apply_tweaks():
            print(message)
        print("提示: 这些调优重启后完全生效; 不满意可用 --restore 还原。")
        return 0
    if args.restore:
        for message in restore_tweaks():
            print(message)
        return 0

    items = read_tweaks()
    if not items:
        print("无法读取注册表调优项。")
        return 1
    print("%-22s %-24s %-14s %s" % ("值名", "当前值", "推荐值", "状态"))
    print("-" * 78)
    for item in items:
        print("%-22s %-24s %-14s %s" % (
            item["name"], str(item["current"]), str(item["target"]),
            "已调优" if item["applied"] else "未调优"))
    print("\n说明: SystemResponsiveness 越小, 分给多媒体/游戏的调度配额越多;")
    print("      Games 任务的 GPU/Priority 越高, 游戏渲染与磁盘 IO 越优先。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="game_booster",
        description="游戏模式: 进程优先级 + 关闭 EcoQoS + 后台内存清理 + 注册表调优",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("示例:\n"
                "  python game_booster.py list -n 20\n"
                "  python game_booster.py boost --name game.exe\n"
                "  python game_booster.py auto --poll 5\n"
                "  python game_booster.py tweaks --show\n"
                "  python game_booster.py tweaks --apply\n"
                "  python game_booster.py tweaks --restore\n"))
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="列出占用内存最多的进程")
    p_list.add_argument("-n", "--top", type=int, default=20, help="显示前 N 个, 默认 20")

    p_boost = sub.add_parser("boost", help="加速指定进程并持续清理后台内存")
    p_boost.add_argument("--pid", type=int, help="目标进程 PID")
    p_boost.add_argument("--name", help="目标进程文件名, 例如 game.exe")
    p_boost.add_argument("--trim-interval", type=int, default=300,
                         help="后台清理间隔秒数, 默认 300")
    p_boost.add_argument("--no-trim", action="store_true", help="只调优先级, 不做后台内存清理")
    p_boost.add_argument("--no-purge", action="store_true", help="不清空待机列表")
    p_boost.add_argument("--timer", type=float, default=0.0,
                         help="把计时器精度设为该毫秒数(如 0.5), 0 = 不修改")
    p_boost.add_argument("--duration", type=int, default=0, help="持续秒数, 0 = 直到 Ctrl+C")

    p_auto = sub.add_parser("auto", help="自动检测全屏游戏并加速(推荐)")
    p_auto.add_argument("--poll", type=float, default=5.0, help="检测间隔秒数, 默认 5")
    p_auto.add_argument("--trim-interval", type=int, default=300, help="后台清理间隔秒数")
    p_auto.add_argument("--duration", type=int, default=0, help="持续秒数, 0 = 直到 Ctrl+C")
    p_auto.add_argument("--timer", type=float, default=0.0, help="计时器精度毫秒数, 0 = 不修改")
    p_auto.add_argument("--no-purge", action="store_true", help="不清空待机列表")

    p_tweaks = sub.add_parser("tweaks", help="MMCSS/游戏任务注册表调优(可备份可还原)")
    group = p_tweaks.add_mutually_exclusive_group()
    group.add_argument("--show", action="store_true", help="显示当前值(默认行为)")
    group.add_argument("--apply", action="store_true", help="应用调优(需要管理员)")
    group.add_argument("--restore", action="store_true", help="从备份文件还原")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(bool(getattr(args, "verbose", False)), default_log_path())

    handlers = {
        "list": cmd_list,
        "boost": cmd_boost,
        "auto": cmd_auto,
        "tweaks": cmd_tweaks,
    }
    handler = handlers.get(args.command or "")
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
