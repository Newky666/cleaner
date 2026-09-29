#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py - 把 cleaner 打包成 Windows 可执行程序

产出两个 exe(放在 dist\\ 下):
  * Cleaner.exe      图形界面版, 双击即用。窗口程序(不弹黑框) + 内嵌 UAC 清单,
                     一启动就会申请管理员权限, 这样"清空待机列表/系统级垃圾"等动作全部可用。
  * cleaner-cli.exe  命令行版, 保留完整 CLI(配合 --elevate 可自行提权), 不含 tkinter, 体积更小。

打包**只在开发时需要 PyInstaller**; 生成的 exe 本身不依赖任何第三方库, 拷到别的电脑直接能跑。

用法:
  py build.py                 构建两个 exe(默认单文件模式)
  py build.py --only gui      只构建图形界面版
  py build.py --only cli      只构建命令行版
  py build.py --onedir        用目录模式(启动更快, 但 dist 下是一个文件夹)
  py build.py --no-uac        图形版不内嵌管理员清单(双击不弹 UAC)
  py build.py --icon my.ico   指定自己的图标

首次使用请先安装打包工具:
  py -m pip install pyinstaller
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import zlib
from typing import List, Optional, Sequence

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(BASE_DIR, "dist")
BUILD_DIR = os.path.join(BASE_DIR, "build")
ICON_PATH = os.path.join(BASE_DIR, "cleaner.ico")

# 可执行程序显示名称(会出现在 UAC 弹窗与任务管理器里)
APP_NAME = "Cleaner"


# ---------------------------------------------------------------------------
# 图标: 用标准库直接生成, 免去额外二进制资源
#   Windows Vista+ 支持 ICO 里直接嵌 PNG, 所以只需要 zlib 手写一帧 PNG 即可
# ---------------------------------------------------------------------------
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _make_png(size: int = 64) -> bytes:
    """画一个"蓝色圆角方块 + 三道白色加速线"的图标。"""
    bg = (59, 130, 246, 255)      # 主色蓝
    fg = (255, 255, 255, 255)
    radius = size * 0.22
    r2 = radius * radius

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG 每行开头的 filter 字节
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            # 圆角矩形内部判定
            cx = min(max(px, radius), size - radius)
            cy = min(max(py, radius), size - radius)
            inside = (px - cx) ** 2 + (py - cy) ** 2 <= r2

            color = (0, 0, 0, 0)
            if inside:
                color = bg
                # 三道斜向加速线(把点投影到线段上求距离)
                for index in range(3):
                    x1, y1 = size * (0.26 + index * 0.11), size * (0.72)
                    x2, y2 = size * (0.62 + index * 0.11), size * (0.30)
                    dx, dy = x2 - x1, y2 - y1
                    length2 = dx * dx + dy * dy
                    t = ((px - x1) * dx + (py - y1) * dy) / length2
                    t = min(1.0, max(0.0, t))
                    dist2 = (px - (x1 + t * dx)) ** 2 + (py - (y1 + t * dy)) ** 2
                    if dist2 <= (size * 0.055) ** 2 and 0.05 < t < 0.95:
                        color = fg
            rows.extend(color)

    header = struct.pack(">2I5B", size, size, 8, 6, 0, 0, 0)  # 8bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + _png_chunk(b"IEND", b""))


def make_icon(path: str = ICON_PATH, size: int = 64) -> Optional[str]:
    """生成一个 ICO 文件, 成功返回路径, 失败返回 None(打包仍会继续, 只是用默认图标)。"""
    try:
        png = _make_png(size)
        # ICONDIR(6字节) + ICONDIRENTRY(16字节) + PNG 数据
        entry = struct.pack("<4B2H2I", size % 256, size % 256, 0, 0, 1, 32,
                            len(png), 6 + 16)
        with open(path, "wb") as fp:
            fp.write(struct.pack("<3H", 0, 1, 1) + entry + png)
        return path
    except Exception as exc:  # 图标失败不该阻断打包
        print("  [跳过] 生成图标失败: %s" % exc)
        return None


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------
def _run(args: Sequence[str]) -> int:
    print("  $ py -m PyInstaller %s" % " ".join(args))
    result = subprocess.run([sys.executable, "-m", "PyInstaller", *args],
                            cwd=BASE_DIR)
    return result.returncode


def _ensure_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        print("未检测到 PyInstaller, 请先安装(仅打包需要):\n"
              "    py -m pip install pyinstaller")
        return False


def build_gui(onefile: bool, uac: bool, icon: Optional[str]) -> int:
    print("\n[1/2] 构建图形界面版 Cleaner.exe ...")
    args: List[str] = [
        "cleaner_gui.py",
        "--name", APP_NAME,
        "--windowed",              # 不弹控制台黑框
        "--clean", "--noconfirm",
        "--distpath", DIST_DIR,
        "--workpath", BUILD_DIR,
        "--specpath", BUILD_DIR,
    ]
    if onefile:
        args.append("--onefile")
    else:
        args.append("--onedir")
    if uac:
        args.append("--uac-admin")  # 内嵌管理员清单, 双击即申请提权
    if icon and os.path.exists(icon):
        args.extend(["--icon", icon])
    return _run(args)


def build_cli(onefile: bool, icon: Optional[str]) -> int:
    print("\n[2/2] 构建命令行版 cleaner-cli.exe ...")
    args: List[str] = [
        "cleaner.py",
        "--name", "cleaner-cli",
        "--console",               # 命令行版需要控制台
        "--clean", "--noconfirm",
        "--distpath", DIST_DIR,
        "--workpath", BUILD_DIR,
        "--specpath", BUILD_DIR,
        # 命令行版完全用不到界面库, 排除掉能明显减小体积
        "--exclude-module", "tkinter",
        "--exclude-module", "_tkinter",
        "--exclude-module", "tkinter.ttk",
    ]
    if onefile:
        args.append("--onefile")
    else:
        args.append("--onedir")
    if icon and os.path.exists(icon):
        args.extend(["--icon", icon])
    return _run(args)


def _report() -> None:
    print("\n" + "=" * 60)
    print("构建产物 (dist\\)")
    print("=" * 60)
    if not os.path.isdir(DIST_DIR):
        print("  没有找到 dist 目录, 构建可能失败。")
        return
    for name in sorted(os.listdir(DIST_DIR)):
        path = os.path.join(DIST_DIR, name)
        if os.path.isfile(path):
            print("  %-24s %8.1f MB" % (name, os.path.getsize(path) / 1024 / 1024))
        else:
            total = 0
            for root, _dirs, files in os.walk(path):
                for item in files:
                    try:
                        total += os.path.getsize(os.path.join(root, item))
                    except OSError:
                        pass
            print("  %-24s %8.1f MB (目录模式)" % (name + "\\", total / 1024 / 1024))
    print("-" * 60)
    print("  双击 dist\\Cleaner.exe 即可使用(会自动申请管理员权限)。")
    print("  日志与配置会写在 exe 同目录下: cleaner.log / cleaner_config.json")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build", description="把 cleaner 打包成 Windows exe")
    parser.add_argument("--only", choices=("gui", "cli"), help="只构建其中一个")
    parser.add_argument("--onedir", action="store_true",
                        help="目录模式(启动更快; 默认是单文件模式)")
    parser.add_argument("--no-uac", action="store_true",
                        help="图形版不申请管理员权限")
    parser.add_argument("--icon", default=ICON_PATH,
                        help="自定义图标(.ico), 为空则自动生成")
    args = parser.parse_args(argv)

    if not _ensure_pyinstaller():
        return 1

    icon = args.icon
    if icon and not os.path.exists(icon):
        print("图标不存在, 自动生成一个 ...")
        icon = make_icon(ICON_PATH)
    elif icon:
        print("使用图标: %s" % icon)

    exit_code = 0
    if args.only in (None, "gui"):
        if build_gui(onefile=not args.onedir, uac=not args.no_uac, icon=icon):
            exit_code = 1
    if args.only in (None, "cli"):
        if build_cli(onefile=not args.onedir, icon=icon):
            exit_code = 1

    _report()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
