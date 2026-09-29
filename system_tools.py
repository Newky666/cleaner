#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""system_tools.py - 系统工具箱(启动项管理 + 大文件扫描)

纯标准库实现, 不依赖第三方包。

  1. 启动项管理
     * 列出注册表 Run / RunOnce 与"启动"文件夹里的自启动项
     * 禁用 = 把条目移动到同级的 RunDisabled 子键(或备份文件夹), 可一键恢复
     * 启用 = 从备份处移回
  2. 大文件扫描
     * 递归扫描指定目录, 找出占用空间最大的文件(不进入目录联接/符号链接)

用法:
  python system_tools.py startups                  # 列出启动项
  python system_tools.py startups --disable NAME   # 禁用某个启动项
  python system_tools.py startups --enable NAME    # 恢复某个启动项
  python system_tools.py largefiles -d C:\\ -m 500 # 扫描 C 盘大于 500MB 的文件
"""

from __future__ import annotations

import argparse
import heapq
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
from common import (  # noqa: E402  统一的基础设施, 去掉本文件的重复实现
    LOG, MB, IS_WINDOWS, WIN_PATHS, Timer,
    human, is_admin, winreg_module, setup_logging, default_log_path,
)

_winreg_module = winreg_module  # 兼容旧的内部调用名


# ---------------------------------------------------------------------------
# 启动项管理
# ---------------------------------------------------------------------------
# 注册表自启动键: (显示名, 根键, 路径, 是否 RunOnce)
REG_RUN_KEYS: List[Tuple[str, str, str, bool]] = [
    ("当前用户", "HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", False),
    ("本机(64位)", "HKLM", r"Software\Microsoft\Windows\CurrentVersion\Run", False),
    ("本机(32位)", "HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", False),
    ("当前用户(一次)", "HKCU", r"Software\Microsoft\Windows\CurrentVersion\RunOnce", True),
    ("本机(一次)", "HKLM", r"Software\Microsoft\Windows\CurrentVersion\RunOnce", True),
]

# key 分段方案(Windows 文件名不允许含 "|", 所以用它做分隔符是安全的):
#   注册表: <root>|<注册表路径>|<Enabled|Disabled>|<值名>       共 4 段
#   文件夹: <启动文件夹>|<Enabled|Disabled>|<文件名>            共 3 段
_STATE_ENABLED = "Enabled"
_STATE_DISABLED = "Disabled"


def _root_from_name(root_name: str):
    """把 "HKCU"/"HKLM" 转成 winreg 根键常量。"""
    return common.reg_root(root_name)


def disabled_subkey(run_path: str) -> str:
    """某个 Run 键对应的"禁用备份子键": Run -> RunDisabled, RunOnce -> RunOnceDisabled。"""
    leaf = run_path.rsplit("\\", 1)[-1] or "Run"
    return run_path + "\\" + leaf + "Disabled"


def disabled_folder_of(startup_folder: str) -> str:
    return os.path.join(startup_folder, "Disabled")


def _startup_folders() -> List[Tuple[str, str]]:
    """返回 (来源名, 启动文件夹路径) 列表。"""
    appdata = WIN_PATHS["appdata"]
    programdata = WIN_PATHS["programdata"]
    return [
        ("启动文件夹(当前用户)",
         os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")),
        ("启动文件夹(所有用户)",
         os.path.join(programdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")),
    ]


@dataclass
class StartupItem:
    """一条开机自启动项。

    key 必须全局唯一 -- 旧实现给"文件夹里的启用项"和"Disabled 子目录里的禁用项"
    用了同一个 key(同名文件会出现两条相同 key), 界面把它当 Treeview 的 iid 时
    会直接抛重复 id 异常。这里把 root 与禁用状态都编进 key, 保证唯一并且能反查出
    写入所需的根键, 不再靠 source 字符串猜 HKCU/HKLM。
    """
    key: str            # 唯一标识: root|路径|名称 或 文件夹| Disabled |名称
    name: str
    command: str
    source: str         # 展示用的来源说明
    location: str       # "registry" 或 "folder"
    enabled: bool = True
    once: bool = False  # 是否为 RunOnce(只执行一次)

    def to_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "command": self.command,
                "source": self.source, "location": self.location,
                "enabled": self.enabled, "once": self.once}

    @property
    def display_source(self) -> str:
        tail = "" if self.enabled else " (已禁用)"
        return self.source + tail


def _enum_reg_values(winreg, root, path: str) -> List[Tuple[str, str]]:
    """枚举某个注册表键下的所有 (名称, 值), 键不存在返回空列表。"""
    values: List[Tuple[str, str]] = []
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                values.append((name, str(value)))
                index += 1
                if index > 4096:  # 安全阀
                    break
    except OSError:
        return []
    return values


def list_startup_items() -> List[StartupItem]:
    """列出所有自启动项(注册表 Run/RunOnce + 启动文件夹, 含已禁用项)。"""
    items: List[StartupItem] = []
    winreg = winreg_module()

    if winreg:
        for label, root_name, path, once in REG_RUN_KEYS:
            root = _root_from_name(root_name)
            # 启用中的项
            for name, value in _enum_reg_values(winreg, root, path):
                items.append(StartupItem(
                    key="%s|%s|%s|%s" % (root_name, path, _STATE_ENABLED, name),
                    name=name, command=value, source="%s\\%s" % (label, name),
                    location="registry", enabled=True, once=once))
            # 被我们禁用的项(备份在 RunDisabled / RunOnceDisabled 子键里)
            disabled_path = disabled_subkey(path)
            for name, value in _enum_reg_values(winreg, root, disabled_path):
                items.append(StartupItem(
                    key="%s|%s|%s|%s" % (root_name, path, _STATE_DISABLED, name),
                    name=name, command=value, source="%s\\%s" % (label, name),
                    location="registry", enabled=False, once=once))

    for label, folder in _startup_folders():
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder)):
                if os.path.isdir(os.path.join(folder, name)):
                    continue  # Disabled 子目录不当成启动项
                if name.lower() == "desktop.ini":
                    continue  # 资源管理器自己建的视图配置, 不是自启动项
                items.append(StartupItem(
                    key="%s|%s|%s" % (folder, _STATE_ENABLED, name),
                    name=name, command=os.path.join(folder, name),
                    source=label, location="folder", enabled=True))
        disabled_folder = disabled_folder_of(folder)
        if os.path.isdir(disabled_folder):
            for name in sorted(os.listdir(disabled_folder)):
                items.append(StartupItem(
                    key="%s|%s|%s" % (folder, _STATE_DISABLED, name),
                    name=name, command=os.path.join(disabled_folder, name),
                    source=label, location="folder", enabled=False))
    return items


def _reg_value_exists(winreg, root, path: str, name: str) -> bool:
    """某个注册表键下是否存在指定值。"""
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, name)
        return True
    except OSError:
        return False


def _move_reg_value(winreg, root, src_path: str, dst_path: str, name: str) -> None:
    """把一个注册表值从 src_path 搬到 dst_path(保留原始数据类型)。"""
    with winreg.OpenKey(root, src_path, 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        value, kind = winreg.QueryValueEx(key, name)
    with winreg.CreateKeyEx(root, dst_path, 0, winreg.KEY_SET_VALUE) as dkey:
        winreg.SetValueEx(dkey, name, 0, kind, value)
    with winreg.OpenKey(root, src_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.DeleteValue(key, name)


def disable_startup_item(item: StartupItem) -> Tuple[bool, str]:
    """禁用启动项(注册表: 移入备份子键; 文件夹: 移入 Disabled 子目录)。

    幂等: 重复禁用不会报错, 而是提示"已经是禁用状态"。
    """
    winreg = winreg_module()
    if item.location == "registry":
        if not winreg:
            return False, "当前平台不支持注册表操作"
        if not item.enabled:
            return True, "%s 已经是禁用状态" % item.name
        root_name, path, _state, name = item.key.split("|", 3)
        root = _root_from_name(root_name)
        disabled_path = disabled_subkey(path)
        try:
            _move_reg_value(winreg, root, path, disabled_path, name)
        except FileNotFoundError:
            if _reg_value_exists(winreg, root, disabled_path, name):
                return True, "%s 已经是禁用状态" % name
            return False, "找不到 %s, 它可能已被其它程序删除" % name
        except OSError as exc:
            return False, "禁用 %s 失败: %s (系统级启动项需要管理员权限)" % (name, exc)
        return True, "已禁用 %s (备份在 %s, 可随时恢复)" % (name, disabled_path)

    # 文件夹方式
    if not item.enabled:
        return True, "%s 已经是禁用状态" % item.name
    folder, _state, name = item.key.split("|", 2)
    src = os.path.join(folder, name)
    dst = os.path.join(disabled_folder_of(folder), name)
    if not os.path.exists(src):
        if os.path.exists(dst):
            return True, "%s 已经是禁用状态" % name
        return False, "找不到源文件: %s" % src
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return True, "已禁用 %s (已移入 Disabled 文件夹, 可随时恢复)" % name
    except OSError as exc:
        return False, "禁用 %s 失败: %s" % (name, exc)


def enable_startup_item(item: StartupItem) -> Tuple[bool, str]:
    """恢复被禁用的启动项(同样幂等)。"""
    winreg = winreg_module()
    if item.location == "registry":
        if not winreg:
            return False, "当前平台不支持注册表操作"
        if item.enabled:
            return False, "%s 不是禁用状态" % item.name
        root_name, run_path, state, name = item.key.split("|", 3)
        if state != _STATE_DISABLED:
            return False, "记录状态异常, 无法恢复 %s" % name
        root = _root_from_name(root_name)
        disabled_path = disabled_subkey(run_path)
        try:
            _move_reg_value(winreg, root, disabled_path, run_path, name)
        except FileNotFoundError:
            if _reg_value_exists(winreg, root, run_path, name):
                return True, "%s 已经是启用状态" % name
            return False, "找不到 %s 的备份数据, 无法恢复" % name
        except OSError as exc:
            return False, "恢复 %s 失败: %s (系统级启动项需要管理员权限)" % (name, exc)
        return True, "已恢复 %s" % name

    # 文件夹方式
    if item.enabled:
        return False, "%s 不是禁用状态" % item.name
    folder, state, name = item.key.split("|", 2)
    if state != _STATE_DISABLED:
        return False, "记录状态异常, 无法恢复 %s" % name
    src = os.path.join(disabled_folder_of(folder), name)
    dst = os.path.join(folder, name)
    if not os.path.exists(src):
        if os.path.exists(dst):
            return True, "%s 已经是启用状态" % name
        return False, "找不到已禁用的文件: %s" % src
    try:
        os.makedirs(folder, exist_ok=True)
        shutil.move(src, dst)
        return True, "已恢复 %s" % name
    except OSError as exc:
        return False, "恢复 %s 失败: %s" % (name, exc)


# ---------------------------------------------------------------------------
# 大文件扫描
# ---------------------------------------------------------------------------
@dataclass
class LargeFile:
    path: str
    size: int

    def to_dict(self) -> dict:
        return {"path": self.path, "size": self.size}


def scan_large_files(root: str, min_size: int = 500 * 1024 * 1024,
                     top_n: int = 100,
                     progress: Optional[Callable[[str], None]] = None,
                     should_cancel: Optional[Callable[[], bool]] = None
                     ) -> List[LargeFile]:
    """递归扫描 root 目录, 返回大于 min_size 的最多 top_n 个文件(按大小降序)。

    三个优化点:
      * os.scandir 取目录项, DirEntry 自带类型信息, 不必对每个子目录再调一次 islink
      * 用大小为 top_n 的小顶堆保留"目前最大的 N 个", 而不是把所有命中文件都存下来再排序:
        扫全盘时命中可能上万个, 内存占用从 O(命中数) 降到 O(top_n)
      * should_cancel 让界面能随时中断扫描
    """
    root = os.path.abspath(root)
    heap: List[Tuple[int, str]] = []   # (size, path) 小顶堆, 只保留最大的 top_n 个
    scanned_dirs = 0
    pending = [root]

    while pending:
        if should_cancel and should_cancel():
            break
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        scanned_dirs += 1
        if progress and scanned_dirs % 200 == 0:
            progress("已扫描 %d 个目录, 命中 %s 个文件..." % (scanned_dirs, len(heap)))
        for entry in entries:
            try:
                # 不进入符号链接 / 目录联接, 避免死循环与重复统计
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                    continue
                size = entry.stat().st_size
            except OSError:
                continue
            if size < min_size:
                continue
            if len(heap) < top_n:
                heapq.heappush(heap, (size, entry.path))
            elif size > heap[0][0]:
                heapq.heapreplace(heap, (size, entry.path))

    files = [LargeFile(path=path, size=size)
             for size, path in sorted(heap, key=lambda item: item[0], reverse=True)]
    return files


# ---------------------------------------------------------------------------
# 命令行接口
# ---------------------------------------------------------------------------
def find_startup_item(name: str, items: Optional[List[StartupItem]] = None,
                      enabled: Optional[bool] = None) -> Optional[StartupItem]:
    """按名称查找启动项; enabled 可限定 "只要启用中的 / 只要已禁用的"。"""
    wanted = name.strip().lower()
    for item in (items if items is not None else list_startup_items()):
        if item.name.lower() != wanted:
            continue
        if enabled is None or item.enabled == enabled:
            return item
    return None


def cmd_startups(args: argparse.Namespace) -> int:
    if args.disable:
        item = find_startup_item(args.disable, enabled=True)
        if item is None:
            print("未找到名为 %s 的启用中启动项" % args.disable)
            return 2
        ok, message = disable_startup_item(item)
        print(message)
        return 0 if ok else 1
    if args.enable:
        item = find_startup_item(args.enable, enabled=False)
        if item is None:
            print("未找到名为 %s 的已禁用启动项" % args.enable)
            return 2
        ok, message = enable_startup_item(item)
        print(message)
        return 0 if ok else 1

    items = list_startup_items()
    print("%-6s %-26s %-20s %-8s %s" % ("状态", "名称", "位置", "类型", "命令"))
    print("-" * 108)
    for item in items:
        print("%-6s %-26s %-20s %-8s %s" % (
            "启用" if item.enabled else "禁用", item.name[:26],
            item.source[:20], "一次性" if item.once else "常规",
            item.command[:60]))
    keys = [i.key for i in items]
    print("\n共 %d 个启动项(唯一 key %d 个)。用 --disable NAME / --enable NAME 管理。"
          % (len(items), len(set(keys))))
    if not is_admin():
        print("提示: 未以管理员运行, 系统级(HKLM)启动项无法修改。")
    return 0


def cmd_largefiles(args: argparse.Namespace) -> int:
    min_size = int(args.min_size * 1024 * 1024)
    files = scan_large_files(args.directory, min_size=min_size, top_n=args.top,
                             progress=lambda m: print(m, file=sys.stderr))
    if args.json:
        import json
        print(json.dumps([f.to_dict() for f in files], ensure_ascii=False, indent=2))
        return 0
    print("%12s  %s" % ("大小", "路径"))
    print("-" * 90)
    total = 0
    for f in files:
        print("%12s  %s" % (human(f.size), f.path))
        total += f.size
    print("-" * 90)
    print("共 %d 个大于 %s 的文件, 合计 %s" % (len(files), human(min_size), human(total)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="system_tools",
        description="系统工具箱: 启动项管理 + 大文件扫描",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("示例:\n"
                "  python system_tools.py startups\n"
                "  python system_tools.py startups --disable \"某程序\"\n"
                "  python system_tools.py largefiles -d C:\\\\ -m 500\n"))
    sub = parser.add_subparsers(dest="command")

    p_startups = sub.add_parser("startups", help="列出/管理开机自启动项")
    p_startups.add_argument("--disable", help="禁用指定名称的启动项")
    p_startups.add_argument("--enable", help="恢复指定名称的启动项")

    p_large = sub.add_parser("largefiles", help="扫描目录中的大文件")
    p_large.add_argument("-d", "--directory", default="C:\\", help="要扫描的目录, 默认 C:\\")
    p_large.add_argument("-m", "--min-size", type=float, default=500.0,
                         help="最小文件大小(单位 MB), 默认 500")
    p_large.add_argument("-n", "--top", type=int, default=100, help="最多显示多少个, 默认 100")
    p_large.add_argument("--json", action="store_true", help="以 JSON 输出")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "startups":
        return cmd_startups(args)
    if args.command == "largefiles":
        return cmd_largefiles(args)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
