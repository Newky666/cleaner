#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cleaner.py - 系统清理与加速工具的统一入口

以前要记住四个脚本(memory_cleaner / junk_cleaner / game_booster / system_tools),
现在统一从这里进:

    py cleaner.py                    概览面板(内存 / 页表 / 磁盘 / 权限)
    py cleaner.py gui                打开图形界面
    py cleaner.py status [--json]    当前内存状态
    py cleaner.py mem ...            内存清理 (子命令见下)
    py cleaner.py junk ...           垃圾清理
    py cleaner.py game ...           游戏加速
    py cleaner.py tools ...          系统工具(启动项 / 大文件)
    py cleaner.py once               一键优化: 垃圾 + 内存顺序执行并汇总报告
    py cleaner.py doctor             环境自检(哪些功能在当前权限下可用)
    py cleaner.py --elevate mem clean -p deep     弹 UAC 提权后执行

子命令的参数直接透传给对应模块, 与单独运行各脚本完全一致, 例如:

    py cleaner.py mem clean -p standard -y
    py cleaner.py mem watch -i 900 --threshold 70
    py cleaner.py junk scan
    py cleaner.py junk clean -c temp,browser,thumbnail -y
    py cleaner.py game auto --poll 5
    py cleaner.py tools largefiles -d C:\\ -m 500
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from typing import List, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
from common import (  # noqa: E402
    LOG, MB, IS_WINDOWS, WIN_PATHS, Timer, human, format_bar, is_admin,
    setup_logging, default_log_path, elevate_and_rerun,
)

VERSION = "3.0"

try:  # 任一模块导入失败也要给出可读提示, 而不是裸 traceback
    import memory_cleaner as mc
    import junk_cleaner as jc
    import system_tools as st
    import game_booster as gb
except ImportError as exc:  # pragma: no cover
    print("导入失败: %s\n请在 %s 目录下运行, 并确保各 *_cleaner.py 文件完整。"
          % (exc, common.BASE_DIR))
    raise

# 顶层命令 -> 负责执行的模块
SUB_MODULES = {
    "mem": mc,
    "junk": jc,
    "game": gb,
    "tools": st,
}

# 一键优化默认清理的垃圾类别(只挑低风险、可再生的)
SAFE_JUNK_KEYS = ("temp", "browser", "thumbnail", "recent", "crash")


# ---------------------------------------------------------------------------
# 概览面板
# ---------------------------------------------------------------------------
def disk_free(path: str = "C:\\") -> tuple:
    """返回 (总容量, 已用, 可用, 可用百分比)。失败返回全 0。"""
    try:
        usage = shutil.disk_usage(path)
        free_percent = usage.free * 100.0 / usage.total if usage.total else 0.0
        return usage.total, usage.used, usage.free, free_percent
    except OSError:
        return 0, 0, 0, 0.0


def print_overview() -> None:
    """打印一眼看清机器状况的概览面板。"""
    status = mc.query_memory_status()
    pages = mc.query_page_lists()

    print("=" * 66)
    print("系统清理与加速工具 v%s    (%s)" % (VERSION, common.BASE_DIR))
    print("=" * 66)
    print("物理内存: %s  占用等级: %s" % (
        format_bar(status.percent), common.percent_color(status.percent)))
    print("  总量 %s / 已用 %s / 可用 %s / 系统缓存 %s" % (
        human(status.total), human(status.used), human(status.available),
        human(status.system_cache)))
    if pages:
        print("  待机页(可回收) %s   空闲页(可立即用) %s   已修改页 %s" % (
            human(pages.standby), human(pages.free + pages.zero),
            human(pages.modified)))

    total, used, free, free_percent = disk_free(WIN_PATHS["windir"][:2] + os.sep)
    if total:
        print("系统盘剩余: %s 可用 (共 %s, 已用 %s)" % (
            human(free), human(total), human(used)))
        if free_percent < 10:
            print("  提示: 可用空间不足 10%%, 建议跑一次 `py cleaner.py junk clean -y`")

    print("运行权限: %s" % ("管理员(全部功能可用)" if is_admin()
                        else "普通用户(部分系统级功能需要管理员)"))
    print("-" * 66)
    print("常用命令:")
    print("  py cleaner.py gui                  打开图形界面")
    print("  py cleaner.py once                 一键优化(垃圾 + 内存)")
    print("  py cleaner.py mem clean -p standard -y")
    print("  py cleaner.py junk scan            扫描各类垃圾大小")
    print("  py cleaner.py game auto            挂机检测全屏游戏并加速")
    print("  py cleaner.py doctor               检查哪些功能在当前环境可用")
    print("  py cleaner.py mem|junk|game|tools --help   查看子命令参数")
    print("=" * 66)


# ---------------------------------------------------------------------------
# 一键优化
# ---------------------------------------------------------------------------
def run_once(profile: str = "standard",
             junk_keys: Sequence[str] = SAFE_JUNK_KEYS,
             include_junk: bool = True,
             include_memory: bool = True,
             dry_run: bool = False,
             progress=None) -> dict:
    """顺序执行一次"垃圾清理 + 内存清理", 返回汇总结果。

    先删垃圾(可能释放出磁盘缓存/句柄), 再清内存, 顺序上更合理。
    """
    def emit(message: str) -> None:
        LOG.info(message)
        if progress:
            try:
                progress(message)
            except Exception:
                LOG.debug("progress 回调异常", exc_info=True)

    summary = {"profile": profile, "dry_run": dry_run, "admin": is_admin(),
               "junk": [], "memory": None, "elapsed": 0.0}

    with Timer() as timer:
        if include_junk:
            cats = jc.build_categories()
            keys = [k for k in junk_keys if k in cats]
            if not keys:
                keys = [k for k in cats if cats[k].risk == "低"]
            emit("① 清理垃圾: %s" % ", ".join(cats[k].title for k in keys))
            summary["junk"] = [r.to_dict() for r in
                               jc.clean_selected(keys, cats, dry_run=dry_run,
                                                 progress=progress)]
            freed = sum(r["freed"] for r in summary["junk"])
            removed = sum(r["removed"] for r in summary["junk"])
            emit("   垃圾清理完成: 释放 %s, 删除 %d 个文件" % (human(freed), removed))

        if include_memory:
            emit("② 清理内存: 档位 %s" % profile)
            report = mc.run_clean(profile=profile, dry_run=dry_run,
                                  progress=progress)
            summary["memory"] = report.to_dict()
            emit("   内存清理完成: 占用下降 %s, 空闲页增加 %s" % (
                human(report.used_reduced), human(report.free_gained)))

    summary["elapsed"] = round(timer.elapsed, 2)
    return summary


def print_once_summary(summary: dict) -> None:
    print("=" * 66)
    print("一键优化报告%s" % ("  (预览, 未做修改)" if summary.get("dry_run") else ""))
    print("=" * 66)
    for item in summary.get("junk", []):
        note = ""
        if item.get("failed"):
            note += ", %d 个被占用跳过" % item["failed"]
        if item.get("skipped"):
            note += ", %d 项需管理员" % item["skipped"]
        print("  %-16s 释放 %-10s 删除 %s 个文件%s"
              % (item["title"], human(item["freed"]), item["removed"], note))

    memory = summary.get("memory")
    if memory:
        print("  %-16s 占用下降 %s, 空闲页增加 %s, 耗时 %.2f 秒"
              % ("内存清理", human(memory["used_reduced"]),
                 human(memory["free_gained"]), memory["elapsed"]))
    print("-" * 66)
    print("总耗时: %.2f 秒     权限: %s" % (
        summary.get("elapsed", 0.0), "管理员" if summary.get("admin") else "普通用户"))
    print("=" * 66)


# ---------------------------------------------------------------------------
# 环境自检
# ---------------------------------------------------------------------------
def run_doctor() -> List[dict]:
    """逐项检查各功能在当前环境/权限下是否可用, 返回结构化结果。"""
    checks: List[dict] = []

    def add(name: str, ok: bool, detail: str, hint: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint})

    add("操作系统", IS_WINDOWS,
        "%s %s" % (sys.platform, os.name),
        "本工具仅支持 Windows" if not IS_WINDOWS else "")
    add("Python 版本", True, sys.version.split()[0])

    admin = is_admin()
    add("管理员权限", admin, "是" if admin else "否",
        "" if admin else "加 --elevate 或右键以管理员运行以获得完整能力")

    status = mc.query_memory_status()
    add("读取内存状态", status.total > 0,
        "总量 %s, 占用 %.1f%%" % (human(status.total), status.percent))

    pages = mc.query_page_lists()
    add("读取内核页表统计", pages is not None,
        "待机 %s / 空闲 %s" % (human(pages.standby),
                               human(pages.free + pages.zero)) if pages else "不可用",
        "" if pages else "不影响清理, 只是无法量化效果")

    snap = mc.snapshot_processes()
    add("进程快照", bool(snap), "%d 个进程" % len(snap),
        "" if snap else "将回退到逐个查询(较慢, 功能不受影响)")

    privileges = mc.enable_clean_privileges() if admin else {}
    profile_ok = privileges.get(mc.SE_PROFILE_SINGLE_PROCESS_NAME, False)
    add("SeProfileSingleProcess 特权", profile_ok,
        "清空待机列表 / 写出脏页",
        "" if profile_ok else "需要管理员权限")
    quota_ok = privileges.get(mc.SE_INCREASE_QUOTA_NAME, False)
    add("SeIncreaseQuota 特权", quota_ok, "刷新系统文件缓存",
        "" if quota_ok else "需要管理员权限")

    total, _used, free, free_percent = disk_free(WIN_PATHS["windir"][:2] + os.sep)
    add("系统盘空间", free_percent >= 10 or free > 10 * 1024 * MB,
        "可用 %s (%.1f%%)" % (human(free), free_percent),
        "" if free_percent >= 10 else "空间紧张, 建议先清理垃圾")

    writable = os.access(common.BASE_DIR, os.W_OK)
    add("程序目录可写", writable, common.BASE_DIR,
        "" if writable else "日志与配置文件将无法写入")

    return checks


def print_doctor(checks: List[dict]) -> None:
    print("=" * 66)
    print("环境自检")
    print("=" * 66)
    for item in checks:
        mark = "[ OK ]" if item["ok"] else "[注意]"
        line = "%s %-22s %s" % (mark, item["name"], item["detail"])
        if item["hint"]:
            line += "\n         -> %s" % item["hint"]
        print(line)
    failed = [c for c in checks if not c["ok"]]
    print("-" * 66)
    if failed:
        print("%d 项受限: %s" % (len(failed), ", ".join(c["name"] for c in failed)))
        print('受限项多为"需要管理员"的系统级动作, 用 `py cleaner.py --elevate ...` 提权即可。')
    else:
        print("全部检查通过, 所有功能都可用。")
    print("=" * 66)


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------
def start_gui() -> int:
    try:
        import cleaner_gui  # noqa: WPS433 (延迟导入, 避免命令行模式依赖 tkinter)
    except ImportError as exc:
        print("无法启动图形界面: %s\n请确认 Python 安装包含 tkinter。" % exc)
        return 1
    return cleaner_gui.main()


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cleaner",
        description="系统清理与加速工具 - 统一入口(内存清理 / 垃圾清理 / 游戏加速 / 系统工具)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("子命令参数与原脚本完全一致, 例如:\n"
                "  py cleaner.py mem clean -p standard -y\n"
                "  py cleaner.py junk clean -c temp,browser -y\n"
                "  py cleaner.py game boost --name game.exe\n"
                "  py cleaner.py tools startups\n\n"
                "直接运行不带参数: 打印系统概览面板\n"))
    parser.add_argument("--version", action="version",
                        version="cleaner v%s" % VERSION)
    parser.add_argument("--elevate", action="store_true",
                        help="先弹 UAC 以管理员身份重新运行(提权失败不阻塞)")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", help="打开图形界面")
    sub.add_parser("status", help="显示内存状态")

    for key, help_text in (("mem", "内存清理(status/clean/profiles/list/watch)"),
                           ("junk", "垃圾清理(scan/clean)"),
                           ("game", "游戏加速(list/boost/auto/tweaks)"),
                           ("tools", "系统工具(startups/largefiles)")):
        sub.add_parser(key, add_help=False, help=help_text)

    p_once = sub.add_parser("once", help="一键优化: 垃圾清理 + 内存清理")
    p_once.add_argument("-p", "--profile", default="standard",
                        help="内存清理档位: light/standard/deep, 默认 standard")
    p_once.add_argument("-c", "--categories", default="",
                        help="要清理的垃圾类别, 逗号分隔; 默认低风险组合")
    p_once.add_argument("--no-junk", action="store_true", help="跳过垃圾清理")
    p_once.add_argument("--no-memory", action="store_true", help="跳过内存清理")
    p_once.add_argument("--dry-run", action="store_true", help="只预览不做修改")
    p_once.add_argument("-y", "--yes", action="store_true", help="不再询问直接执行")
    p_once.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    p_doc = sub.add_parser("doctor", help="环境自检: 检查哪些功能可用")
    p_doc.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    known = build_parser().parse_known_args(argv)[0]
    setup_logging(bool(getattr(known, "verbose", False)), default_log_path())

    # 提权: 把整条命令原样交给新的管理员进程
    if getattr(known, "elevate", False) and not is_admin():
        if elevate_and_rerun(argv, script=os.path.abspath(__file__)):
            return 0
        print("UAC 提权已取消或失败, 以当前权限继续运行。")

    command = known.command or ""
    if command in SUB_MODULES:
        rest = [a for a in argv[1:] if a != "--version"]
        return SUB_MODULES[command].main(rest)
    if command == "gui":
        return start_gui()
    if command == "status":
        return mc.main(["status"] + argv[1:])
    if command == "once":
        return cmd_once(known)
    if command == "doctor":
        return cmd_doctor(known)

    print_overview()
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    if getattr(args, "categories", ""):
        keys = [k.strip() for k in args.categories.split(",") if k.strip()]
        cats = jc.build_categories()
        unknown = [k for k in keys if k not in cats]
        if unknown:
            print("未知类别: %s\n可用: %s" % (", ".join(unknown), ", ".join(cats)))
            return 2
    else:
        keys = list(SAFE_JUNK_KEYS)

    if not args.dry_run and not args.yes and sys.stdin and sys.stdin.isatty():
        try:
            input("将执行垃圾清理 + 内存清理(%s), 按回车开始 (Ctrl+C 取消) ..."
                  % args.profile)
        except KeyboardInterrupt:
            print("\n已取消。")
            return 130

    summary = run_once(profile=args.profile, junk_keys=keys,
                       include_junk=not args.no_junk,
                       include_memory=not args.no_memory,
                       dry_run=bool(args.dry_run),
                       progress=print if args.verbose else None)
    if args.json:
        import json
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print_once_summary(summary)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_doctor()
    if getattr(args, "json", False):
        import json
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        print_doctor(checks)
    return 0 if all(c["ok"] for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
