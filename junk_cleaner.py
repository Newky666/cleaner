#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""junk_cleaner.py - Windows 垃圾文件清理引擎 + 命令行工具

纯标准库实现(ctypes + shutil + glob), 不依赖任何第三方包。

可清理的垃圾类别:
  * 系统临时文件    (%TEMP% / C:\\Windows\\Temp)
  * 回收站          (SHEmptyRecycleBinW, 支持查询容量)
  * 浏览器缓存      (Chrome / Edge / Firefox 的 Cache、Code Cache、GPUCache)
  * 缩略图缓存      (Explorer thumbcache / iconcache)
  * Windows 更新缓存 (SoftwareDistribution\\Download)
  * 错误报告        (WER 崩溃转储 / 报告队列)
  * DirectX 着色器缓存 (D3DSCache)
  * 最近使用记录    (Recent 快捷方式, 隐私清理)

设计原则:
  * 只删除"可再生/可安全删除"的缓存与临时文件, 不碰用户文档
  * 每个文件删除独立容错, 被占用(锁定)的文件自动跳过
  * 清理前后用字节数 + 文件数量化效果

用法:
  python junk_cleaner.py scan            # 扫描各类垃圾, 显示大小
  python junk_cleaner.py clean           # 清理全部(交互确认)
  python junk_cleaner.py clean -c temp,recycle,browser -y
  python junk_cleaner.py clean --dry-run
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import os
import shutil
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402  统一的基础设施, 去掉本文件的重复实现
    LOG, MB, IS_WINDOWS, WIN_PATHS, Timer,
    human, is_admin, setup_logging, default_log_path,
)

ProgressFn = Optional[Callable[[str], None]]


# ---------------------------------------------------------------------------
# 回收站 / Windows API
# ---------------------------------------------------------------------------
shell32 = ctypes.WinDLL("shell32", use_last_error=True) if IS_WINDOWS else None

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


shell32.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
shell32.SHEmptyRecycleBinW.restype = wintypes.LONG
shell32.SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SHQUERYRBINFO)]
shell32.SHQueryRecycleBinW.restype = wintypes.LONG


def recycle_bin_info() -> Tuple[int, int]:
    """返回 (字节数, 文件数)。失败返回 (0, 0)。"""
    if not shell32:
        return 0, 0
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    hr = shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    if hr != 0:
        return 0, 0
    return int(info.i64Size), int(info.i64NumItems)


def empty_recycle_bin() -> Tuple[bool, str]:
    """清空回收站(所有驱动器), 返回 (是否成功, 描述)。"""
    if not shell32:
        return False, "当前平台不是 Windows"
    freed, _items = recycle_bin_info()  # 先取大小, 清理成功后用来报告实际释放量
    hr = shell32.SHEmptyRecycleBinW(
        None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    if hr < 0:  # S_OK = 0
        return False, "HRESULT 0x%08X" % (hr & 0xFFFFFFFF)
    return True, "回收站已清空(释放 %s)" % human(freed)


def recycle_bin_freed() -> int:
    """清空回收站并返回实际释放的字节数(失败返回 0)。"""
    freed, items = recycle_bin_info()
    ok, _detail = empty_recycle_bin()
    return freed if ok else 0


# ---------------------------------------------------------------------------
# 路径工具 (统一取自 common.WIN_PATHS)
# ---------------------------------------------------------------------------
LOCALAPPDATA = WIN_PATHS["localappdata"]
APPDATA = WIN_PATHS["appdata"]
PROGRAMDATA = WIN_PATHS["programdata"]
WINDIR = WIN_PATHS["windir"]
TEMP = WIN_PATHS["temp"]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class JunkTarget:
    """一个清理目标: 目录或 glob 模式, 可按文件名前后缀筛选。"""
    path: str
    suffixes: Tuple[str, ...] = ()          # 为空表示目录内全部文件(后缀匹配)
    prefixes: Tuple[str, ...] = ()          # 前缀匹配, 例如 thumbcache_*.db
    delete_root: bool = False               # 是否连目录本身一起删(如 Cache 目录)
    max_depth: int = 0                      # 0 = 不限制, 1 = 只看该目录这一层
    needs_admin: bool = False

    def match(self, filename: str) -> bool:
        """文件名是否命中该目标的筛选条件。"""
        lower = filename.lower()
        if self.suffixes and not lower.endswith(self.suffixes):
            return False
        if self.prefixes and not lower.startswith(self.prefixes):
            return False
        return True


@dataclass
class JunkCategory:
    key: str
    title: str
    description: str
    targets: List[JunkTarget] = field(default_factory=list)
    risk: str = "低"
    # 特殊处理: 回收站不通过文件系统遍历
    recycle: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key, "title": self.title, "description": self.description,
            "risk": self.risk, "recycle": self.recycle,
        }


def build_categories() -> Dict[str, JunkCategory]:
    """构造全部垃圾类别(路径按当前环境动态展开)。"""
    browser_globs = [
        os.path.join(LOCALAPPDATA, "Google", "Chrome", "User Data", "*", "Cache"),
        os.path.join(LOCALAPPDATA, "Google", "Chrome", "User Data", "*", "Code Cache"),
        os.path.join(LOCALAPPDATA, "Google", "Chrome", "User Data", "*", "GPUCache"),
        os.path.join(LOCALAPPDATA, "Microsoft", "Edge", "User Data", "*", "Cache"),
        os.path.join(LOCALAPPDATA, "Microsoft", "Edge", "User Data", "*", "Code Cache"),
        os.path.join(LOCALAPPDATA, "Microsoft", "Edge", "User Data", "*", "GPUCache"),
        os.path.join(LOCALAPPDATA, "BraveSoftware", "Brave-Browser", "User Data", "*", "Cache"),
        os.path.join(LOCALAPPDATA, "BraveSoftware", "Brave-Browser", "User Data", "*", "Code Cache"),
        os.path.join(LOCALAPPDATA, "Mozilla", "Firefox", "Profiles", "*", "cache2"),
        os.path.join(LOCALAPPDATA, "Mozilla", "Firefox", "Profiles", "*", "startupCache"),
    ]
    categories: Dict[str, JunkCategory] = {}

    categories["temp"] = JunkCategory(
        "temp", "系统临时文件", "用户与系统目录中的临时文件(.tmp/.temp/.log 等)",
        [
            JunkTarget(TEMP),
            JunkTarget(os.path.join(WINDIR, "Temp"), needs_admin=True),
        ],
    )
    categories["recycle"] = JunkCategory(
        "recycle", "回收站", "清空回收站中已删除的文件", recycle=True,
    )
    categories["browser"] = JunkCategory(
        "browser", "浏览器缓存", "Chrome / Edge / Firefox 的网页缓存(会重新生成)",
        [JunkTarget(p, delete_root=True) for p in browser_globs],
    )
    categories["thumbnail"] = JunkCategory(
        "thumbnail", "缩略图缓存", "文件资源管理器缩略图与图标缓存(会重新生成)",
        # 修复: 这类文件名是 thumbcache_32.db / iconcache_16.db, 是"前缀"而非后缀,
        #       旧写法用 endswith 匹配, 导致这一项从来没被扫出来过。
        [JunkTarget(os.path.join(LOCALAPPDATA, "Microsoft", "Windows", "Explorer"),
                    prefixes=("thumbcache_", "iconcache_"), max_depth=1)],
    )
    categories["wer"] = JunkCategory(
        "wer", "错误报告", "Windows 错误报告(WER)与诊断日志",
        [
            JunkTarget(os.path.join(LOCALAPPDATA, "Microsoft", "Windows", "WER")),
            JunkTarget(os.path.join(PROGRAMDATA, "Microsoft", "Windows", "WER"),
                       needs_admin=True),
        ],
    )
    categories["d3d"] = JunkCategory(
        "d3d", "DirectX 着色器缓存", "显卡着色器编译缓存(清理后首次进游戏会重新编译)",
        [
            JunkTarget(os.path.join(LOCALAPPDATA, "D3DSCache")),
            JunkTarget(os.path.join(LOCALAPPDATA, "NVIDIA", "DXCache")),
            JunkTarget(os.path.join(LOCALAPPDATA, "AMD", "DxCache")),
        ],
    )
    categories["crash"] = JunkCategory(
        "crash", "崩溃转储", "程序崩溃时留下的 .dmp 转储文件",
        [JunkTarget(os.path.join(LOCALAPPDATA, "CrashDumps"), suffixes=(".dmp",))],
    )
    categories["recent"] = JunkCategory(
        "recent", "最近使用记录", "开始菜单/文件管理器中的最近访问快捷方式(隐私)",
        [
            JunkTarget(os.path.join(APPDATA, "Microsoft", "Windows", "Recent"),
                       suffixes=(".lnk",)),
        ],
    )
    categories["wupdate"] = JunkCategory(
        "wupdate", "Windows 更新缓存", "已下载的 Windows 更新安装包(可安全删除, 需要管理员)",
        [JunkTarget(os.path.join(WINDIR, "SoftwareDistribution", "Download"),
                    needs_admin=True)],
    )
    categories["delivery"] = JunkCategory(
        "delivery", "传递优化缓存", "Windows 传递优化(Windows 更新/P2P 分发)的下载缓存, 需要管理员",
        [
            JunkTarget(os.path.join(WINDIR, "SoftwareDistribution", "DeliveryOptimization"),
                       needs_admin=True),
            JunkTarget(os.path.join(WINDIR, "ServiceProfiles", "NetworkService",
                                    "AppData", "Local", "Microsoft", "Windows",
                                    "DeliveryOptimization"), needs_admin=True),
        ],
        risk="低",
    )
    categories["fontcache"] = JunkCategory(
        "fontcache", "字体缓存", "Windows 字体缓存文件(下次启动会重建), 需要管理员",
        [
            JunkTarget(os.path.join(WINDIR, "ServiceProfiles", "LocalService",
                                    "AppData", "Local", "FontCache"), needs_admin=True),
            JunkTarget(os.path.join(LOCALAPPDATA, "FontCache"), needs_admin=True),
        ],
        risk="低",
    )
    categories["logs"] = JunkCategory(
        "logs", "系统日志文件", "Windows 组件服务(CBS/DISM)日志, 通常很大且可再生, 需要管理员",
        [
            JunkTarget(os.path.join(WINDIR, "Logs", "CBS"),
                       suffixes=(".log", ".txt", ".bak", ".cab"), needs_admin=True),
            JunkTarget(os.path.join(WINDIR, "Logs", "DISM"),
                       suffixes=(".log", ".txt", ".cab"), needs_admin=True),
        ],
        risk="低",
    )
    categories["devcache"] = JunkCategory(
        "devcache", "开发工具缓存", "npm / pip / NuGet / yarn 的包缓存(可重新下载)",
        [
            JunkTarget(os.path.join(LOCALAPPDATA, "npm-cache")),
            JunkTarget(os.path.join(LOCALAPPDATA, "pip", "cache")),
            JunkTarget(os.path.join(LOCALAPPDATA, "Yarn", "Cache")),
            JunkTarget(os.path.join(LOCALAPPDATA, "NuGet", "http-cache")),
            JunkTarget(os.path.join(LOCALAPPDATA, "uv", "cache")),
            JunkTarget(os.path.join(WIN_PATHS["home"], ".cache", "pip")),
        ],
    )
    return categories


# ---------------------------------------------------------------------------
# 扫描与删除
# ---------------------------------------------------------------------------
def _expand_paths(pattern: str) -> List[str]:
    """把目录或 glob 模式展开成真实路径列表。"""
    if any(ch in pattern for ch in "*?[]"):
        try:
            return sorted(glob.glob(pattern))
        except Exception:
            return []
    return [pattern] if os.path.exists(pattern) else []


def _relative_depth(root: str, base: str) -> int:
    """root 相对 base 的层级(base 自身为 0 层)。"""
    if os.path.abspath(root) == os.path.abspath(base):
        return 0
    return os.path.relpath(root, base).count(os.sep) + 1


def _iter_files(target: JunkTarget,
                should_cancel: Optional[Callable[[], bool]] = None) -> Iterable[str]:
    """遍历一个目标里的所有待删除文件(不进入符号链接/目录联接)。

    max_depth 语义: 1 = 只要 base 目录下的文件; 2 = 再下一层子目录; 0 = 不限制。
    (旧实现用 continue 提前跳过整个 os.walk 回合, 既漏文件又让层级计数出错)

    改用 os.scandir 迭代: Windows 上目录项的"是否目录"信息随目录枚举一起返回,
    不需要像 os.walk 那样对每个子目录再补一次 os.path.islink 判断, 目录多时能省一截。
    """
    for base in _expand_paths(target.path):
        if not os.path.exists(base):
            continue
        if os.path.isfile(base):
            if target.match(os.path.basename(base)):
                yield base
            continue

        pending = [base]
        while pending:
            root = pending.pop()
            depth = _relative_depth(root, base)
            try:
                entries = list(os.scandir(root))
            except OSError:
                continue
            for index, entry in enumerate(entries):
                # 每 256 项检查一次取消信号, 长扫描也能随时停
                if should_cancel and index % 256 == 0 and should_cancel():
                    return
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not target.max_depth or depth + 1 < target.max_depth:
                            pending.append(entry.path)
                    elif target.match(entry.name):
                        yield entry.path
                except OSError:
                    continue


def scan_target(target: JunkTarget,
                should_cancel: Optional[Callable[[], bool]] = None
                ) -> Tuple[int, int]:
    """返回目标的 (字节数, 文件数)。"""
    size, count = 0, 0
    for path in _iter_files(target, should_cancel):
        try:
            size += os.path.getsize(path)
            count += 1
        except OSError:
            pass
    return size, count


def clean_paths(paths: Iterable[str],
                progress: ProgressFn = None) -> Dict[str, int]:
    """按文件路径列表删除(复用扫描结果, 省掉第二次目录遍历)。"""
    result = {"removed": 0, "freed": 0, "failed": 0}
    for index, path in enumerate(paths):
        try:
            size = os.path.getsize(path)
            os.remove(path)
            result["removed"] += 1
            result["freed"] += size
        except OSError:  # 被占用/已不存在的文件自动跳过
            result["failed"] += 1
        if progress and index and index % 200 == 0:
            progress("已删除 %d 个文件, 释放 %s" % (result["removed"],
                                              human(result["freed"])))
    return result


def clean_target(target: JunkTarget,
                 progress: ProgressFn = None,
                 should_cancel: Optional[Callable[[], bool]] = None
                 ) -> Dict[str, int]:
    """删除目标里的文件, 返回 {removed, freed, failed}。"""
    result = clean_paths(
        (p for p in _iter_files(target, should_cancel)), progress=progress)
    if target.delete_root:
        for base in _expand_paths(target.path):
            if os.path.isdir(base) and not os.path.islink(base):
                try:
                    shutil.rmtree(base, ignore_errors=True)
                except OSError:
                    pass
    return result


# ---------------------------------------------------------------------------
# 类别级扫描 / 清理
# ---------------------------------------------------------------------------
@dataclass
class JunkScanResult:
    key: str
    title: str
    risk: str
    size: int = 0
    files: int = 0
    admin_only: bool = False   # 该类别是否需要管理员才能清理
    paths: List[str] = field(default_factory=list)  # 仅 with_paths=True 时填充

    def to_dict(self) -> dict:
        # paths 可能有几万条, 不适合塞进 JSON, 这里刻意不输出
        return {"key": self.key, "title": self.title, "risk": self.risk,
                "size": self.size, "files": self.files,
                "admin_only": self.admin_only}


@dataclass
class JunkCleanResult:
    key: str
    title: str
    freed: int = 0
    removed: int = 0
    failed: int = 0     # 被占用/无权访问而跳过
    skipped: int = 0    # 因权限不足整项目略过(需要管理员)

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "freed": self.freed,
                "removed": self.removed, "failed": self.failed,
                "skipped": self.skipped}


def category_needs_admin(category: JunkCategory) -> bool:
    """该类别是否包含需要管理员权限的目标。"""
    return any(target.needs_admin for target in category.targets)


def scan_category(category: JunkCategory,
                  with_paths: bool = False,
                  should_cancel: Optional[Callable[[], bool]] = None
                  ) -> JunkScanResult:
    """扫描单个类别的大小与文件数。

    with_paths=True 时会顺手把文件清单记下来, 之后清理可以直接复用,
    省掉"扫一遍 + 清的时候再遍历一遍"的第二次目录遍历。
    """
    result = JunkScanResult(key=category.key, title=category.title, risk=category.risk,
                            admin_only=category_needs_admin(category))
    if category.recycle:
        result.size, result.files = recycle_bin_info()
        return result
    for target in category.targets:
        if should_cancel and should_cancel():
            break
        if with_paths:
            paths: List[str] = []
            size, count = 0, 0
            for path in _iter_files(target, should_cancel):
                try:
                    size += os.path.getsize(path)
                    count += 1
                except OSError:
                    continue
                paths.append(path)
            result.paths.extend(paths)
            result.size += size
            result.files += count
        else:
            size, count = scan_target(target, should_cancel)
            result.size += size
            result.files += count
    return result


def clean_category(category: JunkCategory,
                   progress: ProgressFn = None,
                   paths: Optional[Sequence[str]] = None,
                   should_cancel: Optional[Callable[[], bool]] = None
                   ) -> JunkCleanResult:
    """清理单个类别。

    非管理员时会主动跳过需要管理员的目标(而不是硬删然后报一堆失败),
    这样 "清理了 0 个文件" 的原因在结果里一目了然。

    paths: 来自上一次 scan_category(with_paths=True) 的文件清单, 传了就不再重新遍历目录。
    """
    result = JunkCleanResult(key=category.key, title=category.title)
    if category.recycle:
        freed_before, items_before = recycle_bin_info()
        ok, _detail = empty_recycle_bin()
        if ok:
            result.removed = items_before
            result.freed = freed_before
        else:
            result.failed = 1
        return result

    # 复用扫描结果: 删完文件后, 需要连目录一起删的 target 再走一次 rmtree
    if paths is not None:
        stats = clean_paths(paths, progress=progress)
        result.freed += stats["freed"]
        result.removed += stats["removed"]
        result.failed += stats["failed"]
        for target in category.targets:
            if target.delete_root:
                for base in _expand_paths(target.path):
                    if os.path.isdir(base) and not os.path.islink(base):
                        shutil.rmtree(base, ignore_errors=True)
        return result

    admin = is_admin()
    for target in category.targets:
        if should_cancel and should_cancel():
            break
        if target.needs_admin and not admin:
            result.skipped += 1
            continue
        stats = clean_target(target, progress=progress,
                             should_cancel=should_cancel)
        result.freed += stats["freed"]
        result.removed += stats["removed"]
        result.failed += stats["failed"]
    return result


def scan_all(categories: Optional[Dict[str, JunkCategory]] = None,
             progress: ProgressFn = None,
             with_paths: bool = False,
             should_cancel: Optional[Callable[[], bool]] = None
             ) -> List[JunkScanResult]:
    cats = categories or build_categories()
    results: List[JunkScanResult] = []
    for category in cats.values():
        if should_cancel and should_cancel():
            break
        if progress:
            progress("正在扫描 %s ..." % category.title)
        results.append(scan_category(category, with_paths=with_paths,
                                     should_cancel=should_cancel))
    return results


def clean_selected(keys: Iterable[str],
                   categories: Optional[Dict[str, JunkCategory]] = None,
                   dry_run: bool = False,
                   progress: ProgressFn = None,
                   paths_map: Optional[Dict[str, Sequence[str]]] = None,
                   should_cancel: Optional[Callable[[], bool]] = None
                   ) -> List[JunkCleanResult]:
    """按 key 列表清理, 返回每类的结果。

    paths_map: {类别 key: 文件清单}, 来自上一次 scan_all(with_paths=True)。
    界面上"先扫描再清理"的流程传进来, 清理阶段就不必第二次遍历目录。
    """
    cats = categories or build_categories()
    results: List[JunkCleanResult] = []
    for key in keys:
        category = cats.get(key)
        if not category:
            continue
        if should_cancel and should_cancel():
            break
        if progress:
            progress("正在清理 %s ..." % category.title)
        if dry_run:
            scan = scan_category(category)
            results.append(JunkCleanResult(
                key=category.key, title=category.title, freed=scan.size,
                removed=scan.files, failed=0))
        else:
            paths = (paths_map or {}).get(key)
            results.append(clean_category(category, progress=progress,
                                          paths=paths,
                                          should_cancel=should_cancel))
    return results


# ---------------------------------------------------------------------------
# 命令行接口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="junk_cleaner",
        description="Windows 垃圾文件清理工具(临时文件 / 浏览器缓存 / 回收站等)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("示例:\n"
                "  python junk_cleaner.py scan\n"
                "  python junk_cleaner.py clean -c temp,recycle,browser -y\n"
                "  python junk_cleaner.py clean --dry-run\n"))
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="扫描各类垃圾并显示大小")
    p_scan.add_argument("--json", action="store_true", help="以 JSON 输出")

    p_clean = sub.add_parser("clean", help="清理垃圾文件")
    p_clean.add_argument("-c", "--categories", default="",
                         help="要清理的类别 key, 逗号分隔; 默认全部(见 scan 输出)")
    p_clean.add_argument("-y", "--yes", action="store_true", help="不再询问直接清理")
    p_clean.add_argument("--dry-run", action="store_true", help="只预览不删除")
    p_clean.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser


def cmd_scan(args: argparse.Namespace) -> int:
    results = scan_all()
    if args.json:
        import json
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
        return 0
    print("%-10s %-16s %-6s %12s %12s" % ("key", "类别", "风险", "大小", "文件数"))
    print("-" * 62)
    total_size, total_files = 0, 0
    for r in results:
        print("%-10s %-16s %-6s %12s %12d" % (
            r.key, r.title, r.risk, human(r.size), r.files))
        total_size += r.size
        total_files += r.files
    print("-" * 62)
    print("合计可清理: %s, %d 个文件" % (human(total_size), total_files))
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    cats = build_categories()
    if args.categories:
        keys = [k.strip() for k in args.categories.split(",") if k.strip()]
        unknown = [k for k in keys if k not in cats]
        if unknown:
            print("未知类别: %s (可选: %s)" % (", ".join(unknown), ", ".join(cats)))
            return 2
    else:
        keys = list(cats.keys())

    print("将清理 %d 个类别%s:" % (len(keys), " (预览, 不删除)" if args.dry_run else ""))
    for key in keys:
        scan = scan_category(cats[key])
        print("  - %s: %s, %d 个文件" % (scan.title, human(scan.size), scan.files))

    if not args.dry_run and not args.yes:
        try:
            input("按回车开始清理 (Ctrl+C 取消) ...")
        except KeyboardInterrupt:
            print("\n已取消。")
            return 130

    started = time.time()
    results = clean_selected(keys, cats, dry_run=args.dry_run)
    if args.json:
        import json
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
    else:
        total_freed, total_removed = 0, 0
        for r in results:
            print("  %s: 释放 %s, 删除 %d 个文件%s" % (
                r.title, human(r.freed), r.removed,
                ", %d 个被占用跳过" % r.failed if r.failed else ""))
            total_freed += r.freed
            total_removed += r.removed
        print("完成: 共释放 %s, 删除 %d 个文件, 耗时 %.1f 秒" % (
            human(total_freed), total_removed, time.time() - started))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "clean":
        return cmd_clean(args)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
