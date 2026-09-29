#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_cleaner.py - Windows 内存清理引擎 + 命令行工具

设计参考的开源实现:
  * Mem Reduct (github.com/Henrypp/MemReduct)
      - NtSetSystemInformation(SystemMemoryListInformation) 清空待机列表(Standby List)
  * memory-monitor (docs.rs/memory-monitor, src/cleaner.rs)
      - 三阶段清理: EmptyWorkingSet -> FlushModifiedList -> PurgeStandbyList
  * mmozeiko/FlushFileCache (gist)
      - SeIncreaseQuotaPrivilege + SetSystemFileCacheSize(-1, -1, 0) 刷新系统文件缓存
  * CodeDead/MemPlus
      - 权限提升(LUID/TOKEN_PRIVILEGES)与 NT 调用的组织方式

特点:
  * 纯标准库(ctypes)实现, 不需要 pip 安装任何第三方包
  * 每一步独立容错: 非管理员时自动降级, 仍能完成用户态部分(裁剪工作集)
  * 清理前后读取内核真实页表统计(空闲页/待机页/已修改页), 用数据量化清理效果

用法:
  python memory_cleaner.py                     # 查看当前内存状态
  python memory_cleaner.py clean               # 标准清理(推荐)
  python memory_cleaner.py clean -p light      # 轻度清理
  python memory_cleaner.py clean -p deep       # 深度清理
  python memory_cleaner.py clean --dry-run     # 只预览不做任何修改
  python memory_cleaner.py status --json       # 以 JSON 输出状态(给别的程序调用)
  python memory_cleaner.py --elevate clean     # 自动弹 UAC 以管理员身份运行
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

if __package__ in (None, ""):  # 允许被同目录模块直接 import
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402  公共基础设施(格式化/日志/权限/UAC)
    LOG, MB, GB, BASE_DIR, IS_WINDOWS, Timer,
    format_bar, human, is_admin, elevate_and_rerun,
    setup_logging, default_log_path, winreg_module,
)

# ---------------------------------------------------------------------------
# Windows API 常量
# ---------------------------------------------------------------------------
ERROR_NOT_ALL_ASSIGNED = 1300

TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002

SE_PROFILE_SINGLE_PROCESS_NAME = "SeProfileSingleProcessPrivilege"  # 内存列表操作
SE_INCREASE_QUOTA_NAME = "SeIncreaseQuotaPrivilege"                 # 系统文件缓存

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100
PROCESS_SET_INFORMATION = 0x0200
PROCESS_VM_READ = 0x0010

# NtSetSystemInformation / NtQuerySystemInformation 的信息类别
SYSTEM_MEMORY_LIST_INFORMATION = 0x50     # 80  内存列表(清理/统计)
SYSTEM_FILE_CACHE_INFORMATION = 0x15      # 21  系统文件缓存
# 注意: 数值取名为 *_CLASS, 避免与下面的同名 ctypes 结构体冲突
SYSTEM_PROCESS_INFORMATION_CLASS = 0x05   #  5  进程快照(一次性枚举全部进程)
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004  # 缓冲区不够大, 需要扩容重试

# SYSTEM_MEMORY_LIST_COMMAND
MEMORY_EMPTY_WORKING_SETS = 2
MEMORY_FLUSH_MODIFIED_LIST = 3
MEMORY_PURGE_STANDBY_LIST = 4
MEMORY_PURGE_LOW_PRIORITY_STANDBY_LIST = 5

MEMORY_LIST_COMMAND_NAMES = {
    MEMORY_EMPTY_WORKING_SETS: "清空全部工作集",
    MEMORY_FLUSH_MODIFIED_LIST: "写出已修改页 (Flush Modified List)",
    MEMORY_PURGE_STANDBY_LIST: "清空待机列表 (Purge Standby List)",
    MEMORY_PURGE_LOW_PRIORITY_STANDBY_LIST: "清空低优先级待机页",
}

NTSTATUS_HINTS = {
    0xC0000061: "权限不足 (STATUS_PRIVILEGE_NOT_HELD), 请以管理员身份运行",
    0xC000000D: "参数无效 (STATUS_INVALID_PARAMETER)",
    0xC0000022: "拒绝访问 (STATUS_ACCESS_DENIED)",
    0xC00000BB: "该系统版本不支持此命令 (STATUS_NOT_SUPPORTED)",
    0xC0000004: "数据长度不匹配 (STATUS_INFO_LENGTH_MISMATCH)",
}

# 默认不裁剪的进程(裁剪它们会造成系统卡顿或直接失败)
DEFAULT_SKIP_NAMES = {
    "idle", "system", "registry", "memory compression", "secure system",
    "smss.exe", "csrss.exe", "wininit.exe", "services.exe", "lsass.exe",
    "winlogon.exe", "fontdrvhost.exe", "dwm.exe", "audiodg.exe",
    "wudfhost.exe", "wlanext.exe",
}

# ---------------------------------------------------------------------------
# ctypes 结构与函数原型
# ---------------------------------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wintypes.DWORD),
        ("ProcessCount", wintypes.DWORD),
        ("ThreadCount", wintypes.DWORD),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD),
                ("Privileges", LUID_AND_ATTRIBUTES * 1)]


kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.SetSystemFileCacheSize.argtypes = [
    ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
kernel32.SetSystemFileCacheSize.restype = wintypes.BOOL
kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
kernel32.GetPriorityClass.restype = wintypes.DWORD
kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.SetPriorityClass.restype = wintypes.BOOL
kernel32.SetProcessInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetProcessInformation.restype = wintypes.BOOL

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
psapi.EmptyWorkingSet.restype = wintypes.BOOL
psapi.EnumProcesses.argtypes = [
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
psapi.EnumProcesses.restype = wintypes.BOOL
psapi.GetPerformanceInfo.argtypes = [
    ctypes.POINTER(PERFORMANCE_INFORMATION), wintypes.DWORD]
psapi.GetPerformanceInfo.restype = wintypes.BOOL
psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.LookupPrivilegeValueW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
advapi32.AdjustTokenPrivileges.argtypes = [
    wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
    wintypes.DWORD, ctypes.POINTER(TOKEN_PRIVILEGES), ctypes.POINTER(wintypes.DWORD)]
advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

ntdll.NtSetSystemInformation.argtypes = [
    ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong]
ntdll.NtSetSystemInformation.restype = ctypes.c_long
ntdll.NtQuerySystemInformation.argtypes = [
    ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
ntdll.NtQuerySystemInformation.restype = ctypes.c_long

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.IsUserAnAdmin.restype = wintypes.BOOL
shell32.ShellExecuteW.argtypes = [
    wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = ctypes.c_void_p


class SystemMemoryListInfo(ctypes.Structure):
    """内核内存列表统计, 用于量化清理效果(单位: 物理页)。

    参考: NtQuerySystemInformation(SystemMemoryListInformation = 0x50)
    """
    _fields_ = [
        ("ZeroPageCount", ctypes.c_size_t),
        ("FreePageCount", ctypes.c_size_t),
        ("ModifiedPageCount", ctypes.c_size_t),
        ("ModifiedNoWritePageCount", ctypes.c_size_t),
        ("BadPageCount", ctypes.c_size_t),
        ("PageCountByPriority", ctypes.c_size_t * 8),
        ("RepurposedPagesByPriority", ctypes.c_size_t * 8),
        ("ModifiedPageCountPageFile", ctypes.c_size_t),
    ]


class UNICODE_STRING(ctypes.Structure):
    """内核字符串, NtQuerySystemInformation 返回的进程名用它表示。"""
    _fields_ = [("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR)]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    """SystemProcessInformation 的单条记录(读到 WorkingSetSize 即可, 后面字段忽略)。"""
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("Reserved", ctypes.c_longlong * 3),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", wintypes.LONG),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", wintypes.ULONG),
        ("SessionId", wintypes.ULONG),
        ("PageDirectoryBase", ctypes.c_size_t),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", wintypes.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
    ]


# ---------------------------------------------------------------------------
# 基础工具
#   human() / format_bar() / is_admin() / setup_logging() / default_log_path()
#   / elevate_and_rerun() 已收敛到 common.py, 由本模块顶部统一导入并向外转发,
#   旧写法 `from memory_cleaner import human, is_admin` 仍然有效。
# ---------------------------------------------------------------------------
def enable_privilege(name: str) -> bool:
    """在当前进程的访问令牌里启用某个特权(例如 SeProfileSingleProcessPrivilege)。"""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        ctypes.set_last_error(0)
        if not advapi32.AdjustTokenPrivileges(
                token, False, ctypes.byref(tp), 0, None, None):
            return False
        # AdjustTokenPrivileges 即使"部分成功"也会返回 TRUE, 需要看 GetLastError
        return ctypes.get_last_error() != ERROR_NOT_ALL_ASSIGNED
    finally:
        kernel32.CloseHandle(token)


def enable_clean_privileges() -> Dict[str, bool]:
    """启用内存清理所需的全部特权, 返回每个特权的启用结果。"""
    result = {}
    for name in (SE_PROFILE_SINGLE_PROCESS_NAME, SE_INCREASE_QUOTA_NAME):
        result[name] = enable_privilege(name)
    LOG.debug("特权启用结果: %s", result)
    return result


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class MemoryStatus:
    """物理内存 / 提交内存 / 系统缓存 快照。"""
    total: int = 0
    available: int = 0
    used: int = 0
    percent: float = 0.0
    commit_total: int = 0
    commit_limit: int = 0
    system_cache: int = 0
    page_size: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageLists:
    """内核内存列表(单位: 字节)。"""
    free: int = 0          # 空闲页(真正可以直接使用的内存)
    zero: int = 0          # 已清零空闲页
    modified: int = 0      # 已修改页(需要写回磁盘)
    standby: int = 0       # 待机页总数(即"已缓存", 系统认为可回收)
    standby_low: int = 0   # 低优先级待机页(最容易被回收的部分)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StepResult:
    """单个清理步骤的结果。"""
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CleanReport:
    """一次完整清理的报告。"""
    profile: str = ""
    dry_run: bool = False
    admin: bool = False
    before: Optional[MemoryStatus] = None
    after: Optional[MemoryStatus] = None
    pages_before: Optional[PageLists] = None
    pages_after: Optional[PageLists] = None
    steps: List[StepResult] = field(default_factory=list)
    worked_set_freed: int = 0
    trimmed: int = 0
    failed: int = 0
    skipped: int = 0
    elapsed: float = 0.0

    @property
    def standby_freed(self) -> int:
        if not (self.pages_before and self.pages_after):
            return 0
        return max(0, self.pages_before.standby - self.pages_after.standby)

    @property
    def free_gained(self) -> int:
        if not (self.pages_before and self.pages_after):
            return 0
        before = self.pages_before.free + self.pages_before.zero
        after = self.pages_after.free + self.pages_after.zero
        return max(0, after - before)

    @property
    def cache_freed(self) -> int:
        if not (self.before and self.after):
            return 0
        return max(0, self.before.system_cache - self.after.system_cache)

    @property
    def used_reduced(self) -> int:
        """"使用中"物理内存减少量, 最直观的效果指标。"""
        if not (self.before and self.after):
            return 0
        return max(0, self.before.used - self.after.used)

    def to_dict(self) -> dict:
        data = {
            "profile": self.profile,
            "dry_run": self.dry_run,
            "admin": self.admin,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "pages_before": self.pages_before.to_dict() if self.pages_before else None,
            "pages_after": self.pages_after.to_dict() if self.pages_after else None,
            "steps": [s.to_dict() for s in self.steps],
            "worked_set_freed": self.worked_set_freed,
            "trimmed": self.trimmed,
            "failed": self.failed,
            "skipped": self.skipped,
            "elapsed": round(self.elapsed, 3),
            "standby_freed": self.standby_freed,
            "free_gained": self.free_gained,
            "cache_freed": self.cache_freed,
            "used_reduced": self.used_reduced,
        }
        return data


# ---------------------------------------------------------------------------
# 内存状态读取(量化清理效果)
# ---------------------------------------------------------------------------
def nt_status_message(status: int) -> str:
    """把 NTSTATUS 转成可读文本。"""
    code = status & 0xFFFFFFFF
    hint = NTSTATUS_HINTS.get(code)
    if hint:
        return "0x%08X (%s)" % (code, hint)
    return "0x%08X" % code


def query_performance_information() -> Optional[PERFORMANCE_INFORMATION]:
    perf = PERFORMANCE_INFORMATION()
    perf.cb = ctypes.sizeof(PERFORMANCE_INFORMATION)
    if psapi.GetPerformanceInfo(ctypes.byref(perf), perf.cb):
        return perf
    return None


def system_page_size() -> int:
    perf = query_performance_information()
    if perf and perf.PageSize:
        return int(perf.PageSize)
    return 4096


def query_memory_status() -> MemoryStatus:
    """读取物理内存总量/可用量/提交量/系统缓存。"""
    st = MemoryStatus()
    st.page_size = system_page_size()

    msex = MEMORYSTATUSEX()
    msex.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if kernel32.GlobalMemoryStatusEx(ctypes.byref(msex)):
        st.total = int(msex.ullTotalPhys)
        st.available = int(msex.ullAvailPhys)
        st.used = st.total - st.available
        st.percent = float(msex.dwMemoryLoad) if st.total else 0.0

    perf = query_performance_information()
    if perf:
        page = int(perf.PageSize) or st.page_size
        if not st.total:  # 极端情况下的回退路径
            st.total = int(perf.PhysicalTotal) * page
            st.available = int(perf.PhysicalAvailable) * page
            st.used = max(0, st.total - st.available)
            st.percent = round(st.used * 100.0 / st.total, 1) if st.total else 0.0
        st.commit_total = int(perf.CommitTotal) * page
        st.commit_limit = int(perf.CommitLimit) * page
        st.system_cache = int(perf.SystemCache) * page
    return st


def query_page_lists() -> Optional[PageLists]:
    """读取内核内存列表: 空闲页 / 待机页 / 已修改页。

    需要 NtQuerySystemInformation(SystemMemoryListInformation), 普通用户即可读取;
    若系统阻止该调用则返回 None, 程序会自动退化为只报告物理内存数据。
    """
    info = SystemMemoryListInfo()
    ret_len = ctypes.c_ulong(0)
    status = ntdll.NtQuerySystemInformation(
        SYSTEM_MEMORY_LIST_INFORMATION, ctypes.byref(info),
        ctypes.sizeof(info), ctypes.byref(ret_len))
    if status < 0:
        LOG.debug("NtQuerySystemInformation(0x50) 失败: %s", nt_status_message(status))
        return None

    page = system_page_size()
    lists = PageLists(
        free=int(info.FreePageCount) * page,
        zero=int(info.ZeroPageCount) * page,
        modified=int(info.ModifiedPageCount) * page,
        standby=sum(int(info.PageCountByPriority[i]) for i in range(8)) * page,
        standby_low=int(info.PageCountByPriority[0]) * page,
    )

    # 结构体布局若在未来的系统上变化, 数值会明显不合理 -> 直接丢弃
    st = query_memory_status()
    if st.total:
        total_pages = st.total // page
        used_pages = (lists.free + lists.zero + lists.modified + lists.standby) // page
        if used_pages > total_pages * 2:
            LOG.debug("页表统计数值异常(%s 页 > 物理内存 %s 页), 已忽略",
                      used_pages, total_pages)
            return None
    return lists


# ---------------------------------------------------------------------------
# 进程枚举
# ---------------------------------------------------------------------------
@dataclass
class ProcessInfo:
    pid: int
    name: str
    exe: str = ""
    working_set: int = 0
    private: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def list_pids() -> List[int]:
    """枚举系统中所有进程 PID。"""
    count = 1024
    while True:
        buf = (wintypes.DWORD * count)()
        needed = wintypes.DWORD(0)
        if not psapi.EnumProcesses(buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            LOG.debug("EnumProcesses 失败, GetLastError=%s", ctypes.get_last_error())
            return []
        used = needed.value // ctypes.sizeof(wintypes.DWORD)
        if used < count:
            return [int(buf[i]) for i in range(used) if buf[i] != 0]
        count *= 2
        if count > 65536:
            return [int(buf[i]) for i in range(used) if buf[i] != 0]


def open_process(pid: int, access: int = 0) -> int:
    """打开进程, 依次尝试更宽松的权限组合, 失败返回 0。"""
    candidates = []
    if access:
        candidates.append(access)
    candidates += [
        PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA | PROCESS_SET_INFORMATION,
        PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA,
        PROCESS_QUERY_LIMITED_INFORMATION,
    ]
    for rights in candidates:
        handle = kernel32.OpenProcess(rights, False, pid)
        if handle:
            return handle
    return 0


def process_exe(pid: int) -> str:
    """取进程完整路径(取不到返回空串, 例如受保护进程)。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def process_name(pid: int) -> str:
    """取进程文件名(小写); 内核进程返回 system/idle 之类的占位名。"""
    if pid == 0:
        return "idle"
    if pid == 4:
        return "system"
    exe = process_exe(pid)
    return os.path.basename(exe).lower() if exe else ""


def snapshot_processes() -> Dict[int, Tuple[str, int, int]]:
    """一次性读取全部进程的 (名称, 工作集, 私有内存), 失败返回空字典。

    原先的做法是"枚举 PID -> 逐个 OpenProcess 取名字 -> 再 OpenProcess 取内存",
    每个进程要 2~3 次系统调用; 进程数上百时这部分占了清理耗时的大头。
    这里改用 ``NtQuerySystemInformation(SystemProcessInformation)``, 一次调用
    就把 PID / 名称 / 工作集 / 私有内存全部取回。

    结构体布局在未来系统上可能变化, 所以做了合理性校验; 一旦异常返回空字典,
    调用方自动回退到逐个查询的旧路径, 不会让功能失效。
    """
    if not IS_WINDOWS:
        return {}

    size = 1 << 20  # 1 MB 起步
    for _attempt in range(6):
        buf = ctypes.create_string_buffer(size)
        ret_len = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(
            SYSTEM_PROCESS_INFORMATION_CLASS, buf, size, ctypes.byref(ret_len))
        if status == 0:
            parsed = _parse_process_snapshot(buf)
            return parsed
        if status == STATUS_INFO_LENGTH_MISMATCH:
            needed = int(ret_len.value or 0)
            size = min(max(size * 2, needed + (1 << 16)), 64 << 20)
            continue
        LOG.debug("NtQuerySystemInformation(0x05) 失败: %s", nt_status_message(status))
        return {}
    return {}


def _parse_process_snapshot(buf) -> Dict[int, Tuple[str, int, int]]:
    """解析 SystemProcessInformation 缓冲区 -> {pid: (name, working_set, private)}。"""
    result: Dict[int, Tuple[str, int, int]] = {}
    entry_size = ctypes.sizeof(SYSTEM_PROCESS_INFORMATION)
    total = ctypes.sizeof(buf)
    offset = 0
    impossible = 4 * 1024 * GB  # 单个进程工作集不可能超过 4TB, 用于识别结构体错位

    for _count in range(8192):
        if offset + entry_size > total:
            break
        entry = ctypes.cast(ctypes.byref(buf, offset),
                            ctypes.POINTER(SYSTEM_PROCESS_INFORMATION)).contents
        try:
            pid = int(entry.UniqueProcessId or 0)
            working_set = int(entry.WorkingSetSize)
            private = int(entry.PrivatePageCount)
            if pid and working_set <= impossible:
                if pid == 4:
                    name = "system"
                else:
                    raw = entry.ImageName.Buffer or ""
                    name = os.path.basename(raw).lower() if raw else ""
                result[pid] = (name, working_set, private)
        except (ValueError, OverflowError):
            LOG.debug("进程快照解析中断于偏移 %d", offset)
            return {}

        step = int(entry.NextEntryOffset)
        if step <= 0:
            break
        offset += step

    # 空结果说明解析失败或系统不支持, 让调用方走回退路径
    return result if result else {}


def process_memory(pid: int) -> Tuple[int, int]:
    """返回 (工作集大小, 私有内存), 失败返回 (0, 0)。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0, 0
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize), int(counters.PrivateUsage)
        return 0, 0
    finally:
        kernel32.CloseHandle(handle)


def list_processes(min_working_set: int = 0, with_name: bool = True,
                   use_snapshot: bool = True) -> List[ProcessInfo]:
    """列出进程, 按工作集从大到小排序。

    优先走进程快照(一次系统调用); 快照不可用时自动回退到逐个查询。
    快照路径下 exe 字段留空(省掉 N 次 OpenProcess), 需要完整路径时可用
    :func:`process_exe` 按需补充。
    """
    result: List[ProcessInfo] = []

    snap = snapshot_processes() if use_snapshot else {}
    if snap:
        for pid, (name, ws, private) in snap.items():
            if ws < min_working_set:
                continue
            result.append(ProcessInfo(pid=pid, name=name, exe="",
                                      working_set=ws, private=private))
        result.sort(key=lambda p: p.working_set, reverse=True)
        return result

    for pid in list_pids():
        ws, private = process_memory(pid)
        if ws < min_working_set:
            continue
        exe = process_exe(pid) if with_name else ""
        result.append(ProcessInfo(
            pid=pid,
            name=os.path.basename(exe).lower() if exe else ("system" if pid == 4 else ""),
            exe=exe,
            working_set=ws,
            private=private,
        ))
    result.sort(key=lambda p: p.working_set, reverse=True)
    return result


def foreground_process() -> Tuple[int, str]:
    """返回当前前台窗口的 (pid, 进程名)。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0, ""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return 0, ""
    return int(pid.value), process_name(int(pid.value))


def is_fullscreen_app_running() -> bool:
    """用 SHQueryUserNotificationState 判断是否有全屏(游戏/演示)程序在运行。"""
    try:
        state = ctypes.c_int(0)
        shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
        shell32.SHQueryUserNotificationState.restype = ctypes.c_long
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        return state.value in (2, 3)  # QUNS_BUSY=2, QUNS_RUNNING_D3D_FULL_SCREEN=3
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 清理动作
# ---------------------------------------------------------------------------
def nt_memory_list_command(command: int) -> Tuple[bool, str]:
    """执行 NtSetSystemInformation(SystemMemoryListInformation, command)。

    需要 SeProfileSingleProcessPrivilege(管理员默认拥有)。
    """
    value = ctypes.c_ulong(command)
    status = ntdll.NtSetSystemInformation(
        SYSTEM_MEMORY_LIST_INFORMATION, ctypes.byref(value), ctypes.sizeof(value))
    if status >= 0:
        return True, MEMORY_LIST_COMMAND_NAMES.get(command, "命令 %d" % command)
    return False, nt_status_message(status)


def flush_system_file_cache() -> Tuple[bool, str]:
    """刷新(清空)系统文件缓存, 需要 SeIncreaseQuotaPrivilege。"""
    ctypes.set_last_error(0)
    if kernel32.SetSystemFileCacheSize(ctypes.c_size_t(-1), ctypes.c_size_t(-1), 0):
        return True, "SetSystemFileCacheSize(-1, -1, 0)"

    err = ctypes.get_last_error()
    if enable_privilege(SE_INCREASE_QUOTA_NAME):
        value = ctypes.c_longlong(-1)
        status = ntdll.NtSetSystemInformation(
            SYSTEM_FILE_CACHE_INFORMATION, ctypes.byref(value), ctypes.sizeof(value))
        if status >= 0:
            return True, "NtSetSystemInformation(SystemFileCacheInformation, -1)"
        return False, "WinError=%s; NT 备选失败: %s" % (err, nt_status_message(status))
    return False, "WinError=%s (%s)" % (err, ctypes.FormatError(err))


def trim_working_sets(skip_names: Optional[Iterable[str]] = None,
                      exclude_pids: Iterable[int] = ()) -> Dict[str, int]:
    """对每个可访问的进程调用 EmptyWorkingSet, 把闲置内存页交还系统。

    返回统计字典: trimmed / freed / failed / skipped / total
    """
    skip = {n.lower() for n in (skip_names if skip_names is not None else DEFAULT_SKIP_NAMES)}
    exclude = set(exclude_pids) | {0, 4, os.getpid()}

    stats = {"trimmed": 0, "freed": 0, "failed": 0, "skipped": 0, "total": 0}
    rights = PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA | PROCESS_VM_READ
    fallback_rights = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA | PROCESS_VM_READ

    # 先用快照拿进程名(省掉每个进程一次 OpenProcess + 一次路径查询)
    snap = snapshot_processes()
    pids = sorted(snap) if snap else list_pids()

    for pid in pids:
        stats["total"] += 1
        if pid in exclude:
            stats["skipped"] += 1
            continue
        name = snap.get(pid, ("", 0, 0))[0] or process_name(pid)
        if name and name in skip:
            stats["skipped"] += 1
            continue

        handle = kernel32.OpenProcess(rights, False, pid)
        measurable = True
        if not handle:
            handle = kernel32.OpenProcess(fallback_rights, False, pid)
        if not handle:
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA, False, pid)
            measurable = False
        if not handle:
            stats["failed"] += 1
            continue

        try:
            before = 0
            if measurable:
                counters = PROCESS_MEMORY_COUNTERS_EX()
                counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    before = int(counters.WorkingSetSize)
                else:
                    measurable = False

            if not psapi.EmptyWorkingSet(handle):
                stats["failed"] += 1
                continue
            stats["trimmed"] += 1

            if measurable and before:
                after = before
                counters = PROCESS_MEMORY_COUNTERS_EX()
                counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    after = int(counters.WorkingSetSize)
                stats["freed"] += max(0, before - after)
        finally:
            kernel32.CloseHandle(handle)
    return stats


# ---------------------------------------------------------------------------
# 清理档位(Profiles)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Profile:
    key: str
    title: str
    description: str
    trim_working_sets: bool = True
    system_wide_empty_working_sets: bool = False
    flush_modified: bool = False
    purge_low_priority: bool = False
    purge_standby: bool = False
    flush_file_cache: bool = False

    def actions(self) -> List[str]:
        items = []
        if self.trim_working_sets:
            items.append("裁剪后台进程工作集 (EmptyWorkingSet)")
        if self.system_wide_empty_working_sets:
            items.append("系统级清空全部工作集")
        if self.flush_modified:
            items.append("写出已修改页 (Flush Modified List)")
        if self.purge_low_priority:
            items.append("清空低优先级待机页")
        if self.purge_standby:
            items.append("清空待机列表 (Purge Standby List)")
        if self.flush_file_cache:
            items.append("刷新系统文件缓存 (SetSystemFileCacheSize)")
        return items


PROFILES: Dict[str, Profile] = {
    "light": Profile(
        key="light",
        title="轻度清理",
        description="只把后台程序里闲置的内存页交还系统。速度快、几乎无副作用, 适合日常定时执行。",
        trim_working_sets=True,
    ),
    "standard": Profile(
        key="standard",
        title="标准清理(推荐)",
        description="裁剪工作集 + 写出已修改页 + 清空待机列表。开游戏前点一下, 效果最明显。",
        trim_working_sets=True,
        flush_modified=True,
        purge_standby=True,
    ),
    "deep": Profile(
        key="deep",
        title="深度清理",
        description=("在标准清理基础上, 再加系统级清空全部工作集、清空低优先级待机页、"
                    "刷新系统文件缓存。最激进, 清理后首次读盘会略慢, 适合跑分/开大型游戏前使用。"),
        trim_working_sets=True,
        system_wide_empty_working_sets=True,
        flush_modified=True,
        purge_low_priority=True,
        purge_standby=True,
        flush_file_cache=True,
    ),
}


def get_profile(key: str) -> Profile:
    """按名称取清理档位(支持 light / standard / deep 及中文别名)。"""
    alias = {
        "轻度": "light", "轻度清理": "light", "light": "light",
        "标准": "standard", "标准清理": "standard", "standard": "standard", "normal": "standard",
        "深度": "deep", "深度清理": "deep", "deep": "deep", "aggressive": "deep",
    }
    normalized = alias.get(str(key).strip().lower(), str(key).strip().lower())
    if normalized not in PROFILES:
        raise KeyError("未知的清理档位: %s (可选: %s)" % (key, ", ".join(PROFILES)))
    return PROFILES[normalized]


# ---------------------------------------------------------------------------
# 执行清理
# ---------------------------------------------------------------------------
ProgressFn = Optional[Callable[[str], None]]


def run_clean(profile: str = "standard",
              dry_run: bool = False,
              preserve_pids: Iterable[int] = (),
              skip_names: Optional[Iterable[str]] = None,
              progress: ProgressFn = None) -> CleanReport:
    """按档位执行一次清理, 返回结构化报告。

    preserve_pids: 这些进程永远不会被裁剪(例如正在运行的游戏)。
    """
    prof = get_profile(profile)
    preserve = tuple(sorted({int(p) for p in preserve_pids}))
    report = CleanReport(profile=prof.key, dry_run=dry_run, admin=is_admin())
    started = time.time()

    def emit(message: str) -> None:
        LOG.info(message)
        if progress:
            try:
                progress(message)
            except Exception:  # GUI 回调异常不应影响清理
                LOG.debug("progress 回调异常", exc_info=True)

    def add_step(name: str, ok: bool, detail: str = "") -> None:
        report.steps.append(StepResult(name=name, ok=ok, detail=detail))
        tag = "[完成]" if ok else "[失败]"
        emit("%s %s%s" % (tag, name, (" - " + detail) if detail else ""))

    report.before = query_memory_status()
    report.pages_before = query_page_lists()
    emit("清理前: 已用 %s / %s (%.1f%%), 可用 %s, 系统缓存 %s" % (
        human(report.before.used), human(report.before.total), report.before.percent,
        human(report.before.available), human(report.before.system_cache)))

    if dry_run:
        emit("预览模式(未做任何修改), 该档位将执行以下动作:")
        for action in prof.actions():
            emit("  - %s" % action)
        report.after = report.before
        report.pages_after = report.pages_before
        report.elapsed = time.time() - started
        return report

    privs = enable_clean_privileges()
    missing = [name for name, ok in privs.items() if not ok]
    if missing:
        emit("提示: 未能启用特权 %s, 相关步骤需要以管理员身份运行" % ", ".join(missing))

    if prof.trim_working_sets:
        stats = trim_working_sets(skip_names, exclude_pids=preserve)
        report.trimmed = stats["trimmed"]
        report.failed = stats["failed"]
        report.skipped = stats["skipped"]
        report.worked_set_freed = stats["freed"]
        add_step("裁剪进程工作集", stats["trimmed"] > 0,
                 "成功 %d / 跳过 %d / 无权访问 %d, 回收约 %s" % (
                     stats["trimmed"], stats["skipped"], stats["failed"],
                     human(stats["freed"])))

    if prof.system_wide_empty_working_sets:
        ok, detail = nt_memory_list_command(MEMORY_EMPTY_WORKING_SETS)
        add_step(MEMORY_LIST_COMMAND_NAMES[MEMORY_EMPTY_WORKING_SETS], ok, detail)
        time.sleep(0.3)

    if prof.flush_modified:
        ok, detail = nt_memory_list_command(MEMORY_FLUSH_MODIFIED_LIST)
        add_step(MEMORY_LIST_COMMAND_NAMES[MEMORY_FLUSH_MODIFIED_LIST], ok, detail)
        if ok:
            time.sleep(0.5)  # 等待系统把脏页写回磁盘

    if prof.purge_low_priority:
        ok, detail = nt_memory_list_command(MEMORY_PURGE_LOW_PRIORITY_STANDBY_LIST)
        add_step(MEMORY_LIST_COMMAND_NAMES[MEMORY_PURGE_LOW_PRIORITY_STANDBY_LIST], ok, detail)

    if prof.purge_standby:
        ok, detail = nt_memory_list_command(MEMORY_PURGE_STANDBY_LIST)
        add_step(MEMORY_LIST_COMMAND_NAMES[MEMORY_PURGE_STANDBY_LIST], ok, detail)

    if prof.flush_file_cache:
        ok, detail = flush_system_file_cache()
        add_step("刷新系统文件缓存", ok, detail)

    time.sleep(0.4)  # 让系统把页表变化反映到新一次查询里
    report.after = query_memory_status()
    report.pages_after = query_page_lists()
    report.elapsed = time.time() - started

    emit("清理完成: 待机缓存减少 %s, 空闲页增加 %s, 耗时 %.1f 秒" % (
        human(report.standby_freed), human(report.free_gained), report.elapsed))
    return report


# ---------------------------------------------------------------------------
# 文本输出
# ---------------------------------------------------------------------------
def format_status(status: Optional[MemoryStatus] = None,
                  pages: Optional[PageLists] = None,
                  as_json: bool = False) -> str:
    """输出当前内存状态。"""
    status = status or query_memory_status()
    if pages is None and not as_json:
        pages = query_page_lists()

    if as_json:
        payload = {"status": status.to_dict(),
                   "pages": pages.to_dict() if pages else None,
                   "admin": is_admin()}
        return json.dumps(payload, ensure_ascii=False, indent=2)

    lines = [
        "物理内存: %s" % format_bar(status.percent),
        "  总量       : %s" % human(status.total),
        "  使用中     : %s" % human(status.used),
        "  可用       : %s" % human(status.available),
        "  系统缓存   : %s  (任务管理器里的\"已缓存\")" % human(status.system_cache),
        "  提交量     : %s / %s" % (human(status.commit_total), human(status.commit_limit)),
    ]
    if pages:
        lines += [
            "页表统计(内核):",
            "  空闲页     : %s  (可直接使用)" % human(pages.free + pages.zero),
            "  待机页     : %s  (其中低优先级 %s)" % (human(pages.standby), human(pages.standby_low)),
            "  已修改页   : %s" % human(pages.modified),
        ]
    lines.append("运行权限: %s" % ("管理员(可执行全部清理动作)" if is_admin()
                              else "普通用户(仅能裁剪自己的工作集, 建议以管理员运行)"))
    return "\n".join(lines)


def format_report(report: CleanReport) -> str:
    """输出清理报告。"""
    prof = PROFILES.get(report.profile)
    lines = ["=" * 64]
    lines.append("清理报告 - %s%s" % (prof.title if prof else report.profile,
                                 "  (预览, 未做修改)" if report.dry_run else ""))
    lines.append("=" * 64)
    if report.before:
        lines.append("清理前: %s" % format_bar(report.before.percent))
        lines.append("        已用 %s / %s, 系统缓存 %s" % (
            human(report.before.used), human(report.before.total),
            human(report.before.system_cache)))
    for step in report.steps:
        lines.append("%s %s%s" % ("[完成]" if step.ok else "[失败]", step.name,
                                 (" - " + step.detail) if step.detail else ""))
    if report.after:
        lines.append("清理后: %s" % format_bar(report.after.percent))
        lines.append("        已用 %s / %s, 系统缓存 %s" % (
            human(report.after.used), human(report.after.total),
            human(report.after.system_cache)))
    lines.append("-" * 64)
    lines.append("内存占用下降: %s (使用中 %s -> %s)" % (
        human(report.used_reduced),
        human(report.before.used) if report.before else "-",
        human(report.after.used) if report.after else "-"))
    lines.append("待机缓存释放: %s  (待机页被清空后变成空闲页)" % human(report.standby_freed))
    lines.append("空闲页增加  : %s  (真正可立即使用的内存)" % human(report.free_gained))
    lines.append("工作集回收  : %s (裁剪 %d 个进程; 这些页多数会转为待机缓存)" % (
        human(report.worked_set_freed), report.trimmed))
    lines.append("耗时        : %.2f 秒" % report.elapsed)
    if not report.admin:
        lines.append("提示: 以管理员身份运行可执行清空待机列表/文件缓存等完整动作。")
    lines.append("=" * 64)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 命令行接口
# ---------------------------------------------------------------------------
# elevate_and_rerun() / default_log_path() / setup_logging() 已移到 common.py
# (由本模块顶部统一导入, 旧调用方无需改动)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory_cleaner",
        description="Windows 内存清理工具(纯标准库实现, 内核接口参考 Mem Reduct / RAMMap)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("示例:\n"
                "  python memory_cleaner.py status\n"
                "  python memory_cleaner.py clean -p standard --yes\n"
                "  python memory_cleaner.py clean -p deep --dry-run\n"
                "  python memory_cleaner.py watch -i 900 --threshold 70\n"
                "  python memory_cleaner.py --elevate clean -p deep\n"
                "  python cleaner_gui.py          # 图形界面\n"))
    parser.add_argument("--elevate", action="store_true", help="先弹 UAC 以管理员身份重新运行")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="显示当前内存状态")
    p_status.add_argument("--json", action="store_true", help="以 JSON 格式输出")

    p_clean = sub.add_parser("clean", help="执行一次内存清理")
    p_clean.add_argument("-p", "--profile", default="standard",
                         help="清理档位: light(轻度) / standard(标准) / deep(深度)")
    p_clean.add_argument("--dry-run", action="store_true", help="只预览要做什么, 不实际执行")
    p_clean.add_argument("--json", action="store_true", help="以 JSON 格式输出报告")
    p_clean.add_argument("--keep", type=int, action="append", default=[],
                         help="保护指定 PID(例如游戏进程), 可重复使用")
    p_clean.add_argument("-y", "--yes", action="store_true", help="不再询问, 直接开始")

    sub.add_parser("profiles", help="列出所有清理档位及其动作")

    p_list = sub.add_parser("list", help="列出占用内存最多的进程")
    p_list.add_argument("-n", "--top", type=int, default=15, help="显示前 N 个, 默认 15")

    p_watch = sub.add_parser("watch", help="定时循环清理(守护模式, Ctrl+C 退出)")
    p_watch.add_argument("-i", "--interval", type=int, default=600, help="清理间隔秒数, 默认 600")
    p_watch.add_argument("-p", "--profile", default="light", help="清理档位, 默认 light")
    p_watch.add_argument("--threshold", type=float, default=0.0,
                         help="仅当内存使用率高于该百分比(%)时才清理, 默认 0 = 每次都清理")
    return parser


def cmd_status(args: argparse.Namespace) -> int:
    print(format_status(as_json=bool(getattr(args, "json", False))))
    return 0


def cmd_profiles(_args: argparse.Namespace) -> int:
    for prof in PROFILES.values():
        print("%s (%s)" % (prof.title, prof.key))
        print("  说明: %s" % prof.description)
        for action in prof.actions():
            print("     - %s" % action)
        print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    top = max(1, int(args.top))
    processes = list_processes(min_working_set=1)[:top]
    status = query_memory_status()
    print("物理内存: %s  已用 %s / %s" % (
        format_bar(status.percent), human(status.used), human(status.total)))
    print("%-8s %-32s %12s %12s" % ("PID", "进程名", "工作集", "私有内存"))
    print("-" * 68)
    for proc in processes:
        print("%-8d %-32s %12s %12s" % (
            proc.pid, (proc.name or "?")[:32],
            human(proc.working_set), human(proc.private)))
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    try:
        prof = get_profile(args.profile)
    except KeyError as exc:
        print("错误: %s" % exc)
        return 2

    if not is_admin():
        print("提示: 当前不是管理员权限。清空待机列表/文件缓存等动作需要管理员,")
        print("      可以加 --elevate 参数自动弹出 UAC, 或右键以管理员身份运行。")
    print("档位: %s - %s" % (prof.title, prof.description))
    if not args.yes and not args.dry_run and sys.stdin and sys.stdin.isatty():
        try:
            input("按回车开始清理 (Ctrl+C 取消) ...")
        except KeyboardInterrupt:
            print("\n已取消。")
            return 130

    report = run_clean(profile=prof.key, dry_run=bool(args.dry_run),
                       preserve_pids=args.keep,
                       progress=print if args.verbose else None)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        prof = get_profile(args.profile)
    except KeyError as exc:
        print("错误: %s" % exc)
        return 2

    interval = max(5, int(args.interval))
    threshold = float(args.threshold)
    print("守护模式已启动: 档位=%s, 间隔=%d 秒, 内存使用率阈值=%s" % (
        prof.title, interval, ("%.1f%%" % threshold) if threshold > 0 else "无"))
    if not is_admin():
        print("提示: 非管理员权限下只能裁剪工作集, 建议以管理员身份运行。")
    print("按 Ctrl+C 退出。\n")

    try:
        while True:
            status = query_memory_status()
            stamp = time.strftime("%H:%M:%S")
            if status.percent < threshold:
                print("[%s] 内存使用率 %.1f%% 未达阈值 %.1f%%, 跳过本次清理"
                      % (stamp, status.percent, threshold))
            else:
                report = run_clean(profile=prof.key, progress=print)
                print("[%s] 清理完成, 待机缓存减少 %s, 空闲页增加 %s" % (
                    stamp, human(report.standby_freed), human(report.free_gained)))
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n守护模式已停止。")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(bool(getattr(args, "verbose", False)), default_log_path())

    if getattr(args, "elevate", False) and not is_admin():
        if elevate_and_rerun(argv if argv is not None else sys.argv[1:]):
            return 0
        print("UAC 提权已取消或失败, 将以当前权限继续运行。")

    handlers = {
        "status": cmd_status,
        "clean": cmd_clean,
        "profiles": cmd_profiles,
        "list": cmd_list,
        "watch": cmd_watch,
    }
    handler = handlers.get(args.command or "")
    if handler is None:
        parser.print_help()
        print("\n当前内存状态:\n%s" % format_status())
        return 0
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
