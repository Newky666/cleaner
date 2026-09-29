# System Cleaner · 系统加速器

English | [简体中文](README.md)

> **Windows cleaning & boosting toolkit** written in pure Python standard library —
> **zero third-party dependencies** (no `pip install`; it talks to Windows through `ctypes`).
> Memory cleanup · Junk cleanup · Game boost · Startup & large-file management,
> with both a unified CLI and a desktop GUI.

![version](https://img.shields.io/badge/version-2.0-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen) ![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-informational)

---

## Table of Contents

- [1. Features](#1-features)
- [2. Quick Start](#2-quick-start)
- [3. Unified CLI](#3-unified-cli)
- [4. One-click Optimization](#4-one-click-optimization)
- [5. Doctor / Self-check](#5-doctor--self-check)
- [6. Memory Cleaning](#6-memory-cleaning)
- [7. Junk Cleaning](#7-junk-cleaning)
- [8. Game Booster](#8-game-booster)
- [9. System Tools](#9-system-tools)
- [10. Desktop GUI](#10-desktop-gui)
- [11. Project Layout](#11-project-layout)
- [12. Under the Hood](#12-under-the-hood)
- [13. Privacy & Safety](#13-privacy--safety)
- [14. Known Limitations](#14-known-limitations)
- [15. Contributing](#15-contributing)
- [16. License](#16-license)

---

## 1. Features

| Feature | Description |
| --- | --- |
| **Memory cleaning** | Trim idle working sets, flush modified pages, purge standby / low-priority standby lists, flush the system file cache. Three profiles plus a background watch mode. |
| **Junk cleaning** | 13 categories of regenerable junk: temp files, recycle bin, browser cache, thumbnails, WER reports, crash dumps, recent items, DirectX shader cache, dev-tool caches (npm/pip/yarn/NuGet/uv), Windows Update, Delivery Optimization, font cache, CBS/DISM logs. |
| **Game booster** | High priority class + memory priority + EcoQoS throttling off + periodic background memory trimming (the game process itself is protected). Everything is restored on exit. |
| **System tools** | Enable/disable startup entries (fully reversible), large-file scanner. |

Guiding principle: **only remove regenerable, safe-to-delete data**. Every delete is independently
error-tolerant, locked files are skipped, and everything runs locally.

---

## 2. Quick Start

```bat
git clone https://github.com/Newky666/cleaner.git
cd cleaner

py cleaner.py              :: overview panel (RAM / lists / disk / privileges)
py cleaner.py doctor       :: self-check: what works in the current environment
py cleaner.py once         :: one-click: junk + memory
py cleaner.py gui          :: launch the desktop GUI
```

> On Windows, if `python` resolves to the Microsoft Store stub, use **`py`** instead (Python 3.10+).

**Prefer double-click?** The bundled `.bat` files request administrator rights automatically:

| File | What it does |
| --- | --- |
| `run_gui.bat` | Opens the GUI (recommended for daily use) |
| `run_clean.bat` | Runs one deep memory clean immediately |
| `run_game_mode.bat` | Watches for fullscreen games and boosts them |
| `run_tests.bat` | Runs the regression test suite |

### Packaging an exe (optional)

```bat
py -m pip install pyinstaller     :: only needed for building
py build.py                       :: or double-click build.bat
```

| Output | Description |
| --- | --- |
| **`dist\Cleaner.exe`** | **The main program**: desktop GUI, just double-click it. An embedded UAC manifest requests administrator rights on launch, so every feature works |
| `dist\cleaner-cli.exe` | Command-line build, identical to `py cleaner.py ...` |

* Single-file builds: copy them to any Windows PC and run — **no Python required on the target machine**.
* Runtime files are written next to the exe: `cleaner.log`, `cleaner_config.json`.
* Faster startup: `py build.py --onedir`. No UAC prompt: `--no-uac` (system-level cleaning will be limited).
* PyInstaller is a **build-time only** dependency — the produced exe still ships **zero third-party dependencies**.

---

## 3. Unified CLI

One `cleaner.py` entry point. Top-level sub-commands forward their arguments to the matching module,
so every legacy command you already know still works.

```bat
:: Overview & diagnostics
py cleaner.py                      overview panel
py cleaner.py status [--json]      current memory state
py cleaner.py doctor [--json]      environment self-check

:: One-click
py cleaner.py once [-p standard] [-c temp,browser] [--dry-run] [-y] [--json]

:: Four feature areas
py cleaner.py mem clean -p standard -y           memory cleaning
py cleaner.py mem watch -i 900 --threshold 70    watch mode
py cleaner.py junk scan                          scan junk size
py cleaner.py junk clean -c temp,browser -y      clean selected categories
py cleaner.py game auto --poll 5                 game mode (auto-detect fullscreen)
py cleaner.py tools startups                     startup manager
py cleaner.py tools largefiles -d C:\ -m 500     large file scanner
py cleaner.py gui                                desktop GUI
```

Global flags: `--elevate` (re-run the whole command after the UAC prompt), `-v/--verbose`
(debug log + step-by-step output), `--version`.

> Legacy entry points still work: `py memory_cleaner.py clean -p standard -y`, `py junk_cleaner.py scan`, …

---

## 4. One-click Optimization

Runs **junk cleaning → memory cleaning** in order (files first, then RAM) and prints one summary:

```bat
py cleaner.py once                 :: asks for confirmation
py cleaner.py once -y              :: no prompt
py cleaner.py once -p deep -y      :: use the aggressive memory profile
py cleaner.py once --dry-run       :: preview only, nothing is deleted
py cleaner.py once --no-memory -y  :: junk only
```

Defaults to the low-risk, regenerable set: `temp / browser / thumbnail / recent / crash`.
Pick your own with `-c temp,browser,thumbnail`.

```
==================================================================
One-click optimization report
==================================================================
  Temp files              freed 1.5 GB      removed 5585 files
  Thumbnail cache         freed 912.8 MB    removed 30 files
  Browser cache           freed 38.5 MB     removed 477 files
  Memory                  used -974.6 MB, free pages +288.6 MB, 0.81 s
------------------------------------------------------------------
Total: 1.4 s      Privileges: standard user
==================================================================
```

---

## 5. Doctor / Self-check

Answers the question "why did cleaning do nothing?" — item by item:

```
[ OK ] Operating system            win32 nt
[ OK ] Python version             3.10.5
[warn] Administrator privileges     no
         -> re-run with --elevate or "Run as administrator"
[ OK ] Process snapshot            318 processes
[warn] SeProfileSingleProcess      purge standby list / flush dirty pages
         -> requires administrator rights
[ OK ] System disk space           58.1 GB free (19.4%)
```

---

## 6. Memory Cleaning

| Profile | Actions | When to use |
| --- | --- | --- |
| `light` | Trim background working sets only | Idle machine, every 15–30 min |
| `standard` | + flush modified pages + purge standby list | **Before launching a game** |
| `deep` | + system-wide working-set trim + low-priority standby + flush file cache | Benchmarks / heavy AAA titles |

```bat
py cleaner.py status                       current state
py cleaner.py mem clean -p standard -y
py cleaner.py mem clean --dry-run          preview only
py cleaner.py mem watch -i 900 --threshold 70
py cleaner.py mem list -n 15               top memory consumers
py cleaner.py mem profiles                 profile details
```

**What it really does**: returns physical pages that idle programs are holding, writes dirty pages
back to disk early, and drops part of the file cache — so heavy applications start with more genuinely
free memory. It is not magic that turns 8 GB into 16 GB.

---

## 7. Junk Cleaning

13 categories, each independently selectable; sizes are scanned first, results are counted per file,
locked files are skipped:

| key | Description | Risk | Needs admin |
| --- | --- | --- | --- |
| `temp` | User & system temp files | Low | No |
| `recycle` | Recycle bin | Low | No |
| `browser` | Chrome / Edge / Brave / Firefox caches | Low | No |
| `thumbnail` | Explorer thumbnail & icon cache | Low | No |
| `wer` | Windows Error Reporting | Low | No |
| `crash` | Crash dumps (`.dmp`) | Medium | No |
| `recent` | Recent items (privacy) | Low | No |
| `d3d` | DirectX shader cache | Medium | No |
| `devcache` | npm / pip / yarn / NuGet / uv package caches | Low | No |
| `wupdate` | Windows Update downloads | Medium | Yes |
| `delivery` | Delivery Optimization cache | Low | Yes |
| `fontcache` | Font cache | Low | Yes |
| `logs` | CBS / DISM component logs | Low | Yes |

```bat
py cleaner.py junk scan
py cleaner.py junk clean -c temp,browser,thumbnail -y
py cleaner.py junk clean --dry-run
```

Admin-only categories are **skipped explicitly and counted separately** instead of failing silently.

---

## 8. Game Booster

```bat
py cleaner.py game list -n 20                     candidate processes (by memory)
py cleaner.py game boost --name game.exe          boost a process + keep cleaning background
py cleaner.py game boost --pid 1234 --duration 3600
py cleaner.py game auto --poll 5                  auto-detect fullscreen games
py cleaner.py game tweaks --show|--apply|--restore  MMCSS registry tweaks (backed up)
```

Boosting = high priority class + Normal memory priority + EcoQoS throttling disabled + periodic
background memory trimming, while **the game process is protected**. Priority, memory priority and
throttling state are all restored when the game exits or the window is closed.

---

## 9. System Tools

```bat
py cleaner.py tools startups                    list every startup entry
py cleaner.py tools startups --disable "App"    disable (moved to backup, reversible)
py cleaner.py tools startups --enable "App"     restore
py cleaner.py tools largefiles -d C:\ -m 500 -n 50
```

Covers registry `Run` / `RunOnce` (HKCU + HKLM 64/32-bit) and both Startup folders.
Disabling moves the entry to a backup location (registry → sibling `RunDisabled` / `RunOnceDisabled`
subkey; folder → `Disabled` directory). Both operations are idempotent and never lose data.

---

## 10. Desktop GUI

```bat
py cleaner.py gui     :: or double-click run_gui.bat
```

* **One-click optimization** button in the header: uses the currently checked junk categories + the
  selected memory profile.
* **Memory**: live usage bar whose color follows the load level, page-list statistics, three profiles,
  scheduled cleaning (15 min – 2 h).
* **Junk**: scan all 13 categories, check/uncheck per row, admin-only categories are labelled.
* **Game boost**: process list with **clickable column sorting**, double-click a row to boost it,
  option to auto-detect fullscreen games.
* **System tools**: startup enable/disable + large-file scan (open containing folder in one click).
* Status bar shows the **currently running task** with an animated indicator; `F5` refreshes the
  current list; every long operation runs on a worker thread so the UI never freezes.
* **High-DPI aware** (sharp on 2K/4K displays); offers a "Restart elevated" button when not admin.

---

## 11. Project Layout

```
cleaner/
├── cleaner.py            unified CLI (overview / once / doctor / passthrough / gui)  <- CLI entry
├── cleaner_gui.py        desktop GUI (tkinter, zero dependencies)                   <- GUI entry
├── build.py / build.bat  packaging script (needs PyInstaller) -> dist\Cleaner.exe
├── cleaner.ico           app icon (generated with the stdlib, see build.py)
├── common.py             shared infrastructure (format / logging / privileges / UAC / registry)
├── memory_cleaner.py     memory engine + CLI (status/clean/profiles/list/watch)
├── junk_cleaner.py       junk engine + CLI (scan/clean)
├── system_tools.py       system toolbox + CLI (startups/largefiles)
├── game_booster.py       game mode + CLI (list/boost/auto/tweaks)
├── run_gui.bat           double-click -> GUI (auto elevation)
├── run_clean.bat         double-click -> deep memory clean
├── run_game_mode.bat     double-click -> game mode
├── run_tests.bat         double-click -> run tests
├── tests/                regression tests (python -m unittest discover -s tests)
├── README.md             中文文档
├── README_EN.md          English documentation
├── .gitignore
└── LICENSE               MIT
```

Generated at runtime (git-ignored): `cleaner.log`, `cleaner_config.json`, `game_tweaks_backup.json`.

---

## 12. Under the Hood

| Feature | API |
| --- | --- |
| Total / used memory | `GlobalMemoryStatusEx`, `GetPerformanceInfo` |
| Free / standby / modified pages | `NtQuerySystemInformation(SystemMemoryListInformation = 0x50)` |
| Process snapshot (all processes in one call) | `NtQuerySystemInformation(SystemProcessInformation = 0x05)` |
| Trim working sets | `EmptyWorkingSet` (needs `PROCESS_SET_QUOTA`) |
| Purge standby list / flush dirty pages | `NtSetSystemInformation(0x50, 4 / 3)` (needs `SeProfileSingleProcessPrivilege`) |
| Flush system file cache | `SetSystemFileCacheSize(-1,-1,0)` (needs `SeIncreaseQuotaPrivilege`) |
| Priority / memory priority | `SetPriorityClass`, `SetProcessInformation(ProcessMemoryPriority)` |
| Disable EcoQoS | `SetProcessInformation(ProcessPowerThrottling)` |
| Fullscreen game detection | `SHQueryUserNotificationState` |
| Recycle bin | `SHEmptyRecycleBinW`, `SHQueryRecycleBinW` |
| Junk scan & delete | `os.walk` + `os.remove` + `shutil.rmtree` (skips junctions, skips locked files) |
| Startup entries | `winreg` (Run/RunOnce; disable = move to a backup subkey) |

Reference projects: **Mem Reduct**, **memory-monitor**, **mmozeiko/FlushFileCache**,
**CodeDead/MemPlus**, **BleachBit** (cleanup path catalogue).

---

## 13. Privacy & Safety

* **No network, no telemetry, no uploads.** There is not a single network call in the codebase.
* **Auditable**: under 3 000 lines of pure-stdlib Python — read the deletion logic yourself.
* **Clear boundaries**: only caches, temp files and logs are touched; never your documents, photos or downloads.
* **Reversible**: startup changes and registry tweaks are backed up before modification and can be restored.
* **Least privilege**: works without elevation (user-mode trimming); system-level actions require admin and are clearly labelled.

> ⚠️ In games with anti-cheat, judge the risk yourself. The tool only uses public Windows APIs
> — no injection, no memory reading — but caution is still advised.

---

## 14. Known Limitations

* Windows 10/11 only (some NT behaviours may differ on Server editions).
* On future Windows versions the internal NT structures may change; the code sanity-checks the values
  and gracefully degrades (fall back to physical-memory-only stats or to per-process queries).
* Protected processes (including some anti-cheat and critical system processes) cannot be trimmed or
  reprioritised and are skipped.
* To uninstall, delete the folder; if you applied registry tweaks, run
  `py cleaner.py game tweaks --restore` first.

---

## 15. Contributing

```bat
py -m unittest discover -s tests -t .     :: run the suite (37 tests currently)
```

Issues and PRs are welcome. Before submitting, please ensure:

1. No third-party dependencies — the project must stay **dependency-free**.
2. New/changed logic comes with tests.
3. Anything that deletes files or writes the registry must be individually error-tolerant, idempotent,
   and distinguish `failed` (in use) from `skipped` (insufficient privileges) in its result.

---

## 16. License

[MIT](LICENSE) © 2026 Newky
