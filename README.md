# 系统清理与加速工具 · System Cleaner

[English](README_EN.md) | 简体中文

> **系统加速器** — 纯 Python 标准库实现的 Windows 清理/加速工具，**零第三方依赖**（不用 `pip install`，只用 `ctypes` 直接调用 Windows 原生接口）。
> 内存清理 · 垃圾清理 · 游戏加速 · 启动项与大文件管理，一套命令行 + 一套图形界面全覆盖。

![版本](https://img.shields.io/badge/version-2.0-blue) ![许可证](https://img.shields.io/badge/license-MIT-green) ![依赖](https://img.shields.io/badge/dependencies-none-brightgreen) ![平台](https://img.shields.io/badge/platform-Windows%2010%2F11-informational)

---

## 目录

- [1. 它能做什么](#1-它能做什么)
- [2. 快速开始](#2-快速开始)
- [3. 统一命令行入口](#3-统一命令行入口)
- [4. 一键优化](#4-一键优化)
- [5. 环境自检](#5-环境自检)
- [6. 内存清理](#6-内存清理)
- [7. 垃圾清理](#7-垃圾清理)
- [8. 游戏加速](#8-游戏加速)
- [9. 系统工具](#9-系统工具)
- [10. 图形界面](#10-图形界面)
- [11. 文件结构](#11-文件结构)
- [12. 实现原理](#12-实现原理)
- [13. 隐私与安全](#13-隐私与安全)
- [14. 已知限制](#14-已知限制)
- [15. 参与贡献](#15-参与贡献)

---

## 1. 它能做什么

| 能力 | 说明 |
| --- | --- |
| **内存清理** | 裁剪闲置工作集、写出脏页、清空待机列表/低优先级待机页、刷新系统文件缓存。三档可选，支持守护模式定时执行 |
| **垃圾清理** | 13 类可再生垃圾：临时文件、回收站、浏览器缓存、缩略图、错误报告、崩溃转储、最近记录、DX 着色器、开发工具缓存、Windows 更新、传递优化、字体缓存、系统日志 |
| **游戏加速** | 游戏进程高优先级 + 内存优先级 + 关闭 EcoQoS 节流 + 后台定时清内存（绝不碰游戏进程），退出自动还原 |
| **系统工具** | 开机启动项禁用/恢复（可随时还原）、大文件扫描与定位 |

设计原则：**只清理可再生、可安全删除的内容**，每个删除动作独立容错，被占用的文件自动跳过；所有操作 100% 在本机完成。

---

## 2. 快速开始

```bat
git clone https://github.com/Newky666/cleaner.git
cd cleaner

py cleaner.py              :: 概览面板(内存/页表/磁盘/权限)
py cleaner.py doctor       :: 环境自检, 看看哪些功能当前可用
py cleaner.py once         :: 一键优化: 垃圾 + 内存
py cleaner.py gui          :: 打开图形界面
```

> Windows 上若 `python` 指向 Microsoft Store 占位符，请统一使用 **`py`**（Python 3.10+）。

**不想敲命令？** 双击即可（会自动请求管理员权限）：

| 双击 | 效果 |
| --- | --- |
| `run_gui.bat` | 打开图形界面（日常推荐） |
| `run_clean.bat` | 立刻做一次深度内存清理 |
| `run_game_mode.bat` | 挂机检测全屏游戏并自动加速 |
| `run_tests.bat` | 跑一遍回归测试 |

---

## 3. 统一命令行入口

一个 `cleaner.py` 管全部功能；顶层子命令参数**完全透传**给对应模块，所以你熟悉的旧命令都还能用。

```bat
:: 概览与自检
py cleaner.py                      概览面板
py cleaner.py status [--json]      当前内存状态
py cleaner.py doctor [--json]      环境自检

:: 一键优化
py cleaner.py once [-p standard] [-c temp,browser] [--dry-run] [-y] [--json]

:: 四大功能
py cleaner.py mem clean -p standard -y           内存清理
py cleaner.py mem watch -i 900 --threshold 70    守护模式定时清理
py cleaner.py junk scan                          扫描垃圾大小
py cleaner.py junk clean -c temp,browser -y      清理指定类别
py cleaner.py game auto --poll 5                 游戏模式(自动检测全屏游戏)
py cleaner.py tools startups                     启动项管理
py cleaner.py tools largefiles -d C:\ -m 500     大文件扫描
py cleaner.py gui                                图形界面
```

全局参数：`--elevate`（弹 UAC 提权后重跑整条命令）、`-v/--verbose`（调试日志 + 逐步骤输出）、`--version`。

> 老入口依然保留：`py memory_cleaner.py clean -p standard -y`、`py junk_cleaner.py scan` 等，行为不变。

---

## 4. 一键优化

顺序执行 **垃圾清理 → 内存清理**（先删文件再清内存，顺序更合理），最后给一份汇总报告：

```bat
py cleaner.py once                 :: 交互确认后执行
py cleaner.py once -y              :: 不再询问
py cleaner.py once -p deep -y      :: 内存档位换成深度清理
py cleaner.py once --dry-run       :: 只预览会释放多少, 不删任何东西
py cleaner.py once --no-memory -y  :: 只清垃圾不碰内存
```

默认只清理低风险、可再生的类别：`temp / browser / thumbnail / recent / crash`；想指定类别用 `-c temp,browser,thumbnail`。

报告示例：

```
==================================================================
一键优化报告
==================================================================
  系统临时文件           释放 1.5 GB     删除 5585 个文件
  缩略图缓存            释放 912.8 MB   删除 30 个文件
  浏览器缓存            释放 38.5 MB    删除 477 个文件
  内存清理             占用下降 974.6 MB, 空闲页增加 288.6 MB, 耗时 0.81 秒
------------------------------------------------------------------
总耗时: 1.4 秒     权限: 普通用户
==================================================================
```

---

## 5. 环境自检

回答「为什么我清理没效果」，逐项列出可用性：

```
[ OK ] 操作系统                   win32 nt
[ OK ] Python 版本              3.10.5
[注意] 管理员权限                  否
         -> 加 --elevate 或右键以管理员运行以获得完整能力
[ OK ] 进程快照                   318 个进程
[注意] SeProfileSingleProcess 特权 清空待机列表 / 写出脏页
         -> 需要管理员权限
[ OK ] 系统盘空间                  可用 58.1 GB (19.4%)
```

---

## 6. 内存清理

| 档位 | 动作 | 建议场景 |
| --- | --- | --- |
| `light` 轻度 | 只裁剪后台进程工作集 | 平时挂机，每 15~30 分钟一次 |
| `standard` 标准 | + 写出已修改页 + 清空待机列表 | **开游戏前点一下** |
| `deep` 深度 | + 系统级清空工作集 + 低优先级待机页 + 刷新文件缓存 | 跑分 / 开大型游戏前 |

```bat
py cleaner.py status                       :: 看当前内存状态
py cleaner.py mem clean -p standard -y
py cleaner.py mem clean --dry-run          :: 只预览
py cleaner.py mem watch -i 900 --threshold 70
py cleaner.py mem list -n 15               :: 占内存最多的进程
py cleaner.py mem profiles                 :: 档位说明
```

**它到底做了什么**：把后台程序占着但暂时不用的物理页交回系统、把脏页提前写回磁盘、丢弃部分磁盘缓存，让大型程序启动时拥有更多真正空闲的内存页。它不是「把 8G 变 16G」的魔术。

---

## 7. 垃圾清理

13 个类别，每类可单独勾选；清理前先扫描显示大小，删除后逐个统计，被占用文件自动跳过：

| key | 说明 | 风险 | 需管理员 |
| --- | --- | --- | --- |
| `temp` | 用户/系统临时文件 | 低 | 否 |
| `recycle` | 回收站 | 低 | 否 |
| `browser` | Chrome / Edge / Brave / Firefox 缓存 | 低 | 否 |
| `thumbnail` | 资源管理器缩略图与图标缓存 | 低 | 否 |
| `wer` | Windows 错误报告 | 低 | 否 |
| `crash` | 崩溃转储 `.dmp` | 中 | 否 |
| `recent` | 最近访问记录（隐私） | 低 | 否 |
| `d3d` | DirectX 着色器缓存 | 中 | 否 |
| `devcache` | npm / pip / yarn / NuGet / uv 包缓存 | 低 | 否 |
| `wupdate` | Windows 更新下载缓存 | 中 | 是 |
| `delivery` | 传递优化下载缓存 | 低 | 是 |
| `fontcache` | 字体缓存 | 低 | 是 |
| `logs` | CBS / DISM 组件日志 | 低 | 是 |

```bat
py cleaner.py junk scan
py cleaner.py junk clean -c temp,browser,thumbnail -y
py cleaner.py junk clean --dry-run
```

非管理员运行时，需要管理员的类别会**主动跳过并在结果里单独计数**（不会静默失败）。

---

## 8. 游戏加速

```bat
py cleaner.py game list -n 20                     :: 候选进程(按内存排序)
py cleaner.py game boost --name game.exe          :: 加速指定进程并持续清后台
py cleaner.py game boost --pid 1234 --duration 3600
py cleaner.py game auto --poll 5                  :: 自动检测全屏游戏(推荐挂机)
py cleaner.py game tweaks --show|--apply|--restore :: MMCSS 注册表调优(可备份还原)
```

加速 = 调度优先级调到高 + 内存优先级调到 Normal + 关闭 EcoQoS 能效节流 + 定时清理后台内存（**游戏进程本身受保护**）。
游戏退出 / 关闭窗口自动把优先级、内存优先级、节流状态**全部还原**（已备份原始值）。

---

## 9. 系统工具

```bat
py cleaner.py tools startups                     :: 列出全部启动项
py cleaner.py tools startups --disable "某程序"   :: 禁用(移入备份, 可恢复)
py cleaner.py tools startups --enable "某程序"    :: 恢复
py cleaner.py tools largefiles -d C:\ -m 500 -n 50
```

覆盖注册表 `Run` / `RunOnce`（HKCU + HKLM 64/32 位）与两个「启动」文件夹。
禁用 = 移入备份位置（注册表 → 同级 `RunDisabled` / `RunOnceDisabled` 子键；文件夹 → `Disabled` 目录），**可随时恢复，原始数据不丢**。禁用/启用操作幂等，重复执行不会出错。

---

## 10. 图形界面

```bat
py cleaner.py gui     :: 或双击 run_gui.bat
```

界面要点：

* **一键优化**（右上角主按钮）：按当前勾选的垃圾类别 + 当前内存档位顺序执行。
* **内存清理**：实时占用条（颜色随占用等级变化）、页表统计、三档清理、自动定时清理（15 分钟 ~ 2 小时）。
* **垃圾清理**：13 类垃圾扫描 + 勾选清理；需要管理员的类别会显式标注，双击列表或用顶部按钮全选。
* **游戏加速**：进程列表**点击列头即可排序**，双击进程立即加速；可勾选自动检测全屏游戏。
* **系统工具**：启动项禁用/恢复 + 大文件扫描（可一键打开所在文件夹）。
* 状态栏实时显示**执行中任务**与滚动指示条；`F5` 刷新当前列表；所有耗时操作均在后台线程执行，界面不卡。
* 已做**高 DPI 感知**，2K/4K 屏上不会发虚；非管理员时右上角提供「提权重启」。

---

## 11. 文件结构

```
cleaner/
├── cleaner.py            统一命令行入口(概览 / once / doctor / 四大模块透传 / gui)
├── common.py             公共基础设施(格式化 / 日志 / 权限 / UAC / 路径 / winreg)
├── memory_cleaner.py     内存清理引擎 + CLI(status/clean/profiles/list/watch)
├── junk_cleaner.py       垃圾清理引擎 + CLI(scan/clean)
├── system_tools.py       系统工具箱 + CLI(startups/largefiles)
├── game_booster.py       游戏模式 + CLI(list/boost/auto/tweaks)
├── cleaner_gui.py        图形界面(tkinter, 零第三方依赖)
├── run_gui.bat           双击 -> 图形界面(自动提权)
├── run_clean.bat         双击 -> 深度内存清理
├── run_game_mode.bat     双击 -> 游戏模式
├── run_tests.bat         双击 -> 跑回归测试
├── tests/                回归测试(python -m unittest discover -s tests)
├── README.md             中文文档
├── README_EN.md          English documentation
├── .gitignore
└── LICENSE               MIT
```

运行期生成（已 gitignore）：`cleaner.log`、`cleaner_config.json`、`game_tweaks_backup.json`。

---

## 12. 实现原理

| 功能 | 接口 |
| --- | --- |
| 内存总量/占用 | `GlobalMemoryStatusEx`、`GetPerformanceInfo` |
| 空闲页/待机页/已修改页 | `NtQuerySystemInformation(SystemMemoryListInformation = 0x50)` |
| 进程快照（一次性枚举全部进程） | `NtQuerySystemInformation(SystemProcessInformation = 0x05)` |
| 裁剪进程工作集 | `EmptyWorkingSet`（需 `PROCESS_SET_QUOTA`） |
| 清空待机列表 / 写出脏页 | `NtSetSystemInformation(0x50, 4 / 3)`（需 `SeProfileSingleProcessPrivilege`） |
| 刷新系统文件缓存 | `SetSystemFileCacheSize(-1,-1,0)`（需 `SeIncreaseQuotaPrivilege`） |
| 进程优先级 / 内存优先级 | `SetPriorityClass`、`SetProcessInformation(ProcessMemoryPriority)` |
| 关闭 EcoQoS | `SetProcessInformation(ProcessPowerThrottling)` |
| 全屏游戏检测 | `SHQueryUserNotificationState` |
| 回收站 | `SHEmptyRecycleBinW`、`SHQueryRecycleBinW` |
| 垃圾扫描删除 | `os.walk` + `os.remove` + `shutil.rmtree`（跳过符号链接，占用文件跳过） |
| 启动项 | `winreg`（Run/RunOnce，禁用 = 移入备份子键） |

参考的开源项目：**Mem Reduct**、**memory-monitor**、**mmozeiko/FlushFileCache**、**CodeDead/MemPlus**、**BleachBit**（清理路径清单）。

---

## 13. 隐私与安全

* **无联网、无遥测、无上传**：代码里没有任何网络请求，所有操作都在本机完成。
* **开源可审计**：不到 3000 行纯标准库 Python，你可以直接读完所有删除逻辑。
* **边界清晰**：只清理缓存/临时/日志类可再生内容，不碰文档、照片、下载等用户数据。
* **可回溯**：每个文件删除独立容错；启动项与注册表调优都先备份再改动，支持一键还原。
* **最小权限**：非管理员也能用（只做用户态裁剪），系统级动作需提权，界面/CLI 都会明确提示。

> ⚠️ 带反作弊的游戏请自行判断风险：本工具只调用公开的 Windows API，不做注入、不读进程内存，但仍建议谨慎。

---

## 14. 已知限制

* 仅支持 Windows 10/11（部分 NT 接口在 Server 版本上行为可能不同）。
* 某些系统版本上 `NtQuerySystemInformation` 结构体可能不同：代码带数值合理性校验，校验不过就自动降级为「只报告物理内存」或回退到逐个查询，功能不受影响。
* 受保护进程（含部分反作弊与系统关键进程）无法裁剪/调优先级，会被跳过。
* 卸载：删除整个目录即可；若应用过注册表调优，请先 `py cleaner.py game tweaks --restore`。

---

## 15. 参与贡献

```bat
py -m unittest discover -s tests -t .     :: 跑回归测试(目前 37 条)
```

欢迎提 issue 与 PR。提交前请确保：

1. 不引入第三方依赖（必须保持「零依赖」特性）；
2. 新增/修改的逻辑有对应测试用例；
3. 涉及删除或写注册表的功能必须：独立容错 + 幂等 + 结果里区分 `failed`（被占用）与 `skipped`（权限不足）。

---

## 许可证

[MIT](LICENSE) © 2026 Newky
