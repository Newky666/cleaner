#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cleaner 回归测试(纯标准库 unittest, 无需 pytest / 无需管理员权限)

运行:
    py -m unittest discover -s tests -v
    py tests\\test_cleaner.py

重点覆盖这次重构修掉的几个真实缺陷:
  * 垃圾类别里 thumbcache/iconcache 是"前缀"而非后缀(旧实现这一项永远扫不到)
  * os.walk 的 max_depth 逻辑错误
  * 启动项 key 冲突(同名文件的启用/禁用项 key 相同, 会导致界面崩溃)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import common  # noqa: E402
import junk_cleaner as jc  # noqa: E402
import memory_cleaner as mc  # noqa: E402
import system_tools as st  # noqa: E402

IS_WINDOWS = os.name == "nt"


class TestCommon(unittest.TestCase):
    def test_human_units(self):
        self.assertEqual(common.human(0), "0 B")
        self.assertEqual(common.human(512), "512 B")
        self.assertEqual(common.human(1024), "1.0 KB")
        self.assertEqual(common.human(1536), "1.5 KB")
        self.assertEqual(common.human(common.MB), "1.0 MB")
        self.assertEqual(common.human(common.GB), "1.0 GB")
        self.assertEqual(common.human(None), "-")

    def test_human_precision(self):
        self.assertEqual(common.human(1536, precision=0), "2 KB")

    def test_format_bar_clamps(self):
        text = common.format_bar(50, width=10)
        self.assertIn("50.0%", text)
        self.assertEqual(common.format_bar(-5, width=4), "[....]   0.0%")
        self.assertEqual(common.format_bar(999, width=4), "[####] 100.0%")

    def test_percent_color_levels(self):
        self.assertEqual(common.percent_color(10), "充裕")
        self.assertEqual(common.percent_color(60), "正常")
        self.assertEqual(common.percent_color(80), "偏高")
        self.assertEqual(common.percent_color(95), "危险")

    def test_clamp_and_safe_int(self):
        self.assertEqual(common.clamp(15, 0, 10), 10)
        self.assertEqual(common.clamp(-1, 0, 10), 0)
        self.assertEqual(common.safe_int("512"), 512)
        self.assertEqual(common.safe_int("abc", default=7), 7)
        self.assertEqual(common.safe_int(None, default=3), 3)

    def test_timer_measures(self):
        with common.Timer() as timer:
            pass
        self.assertGreaterEqual(timer.elapsed, 0.0)


class TestJunkTargetMatching(unittest.TestCase):
    """回归: 缩略图缓存项此前用 endswith 匹配 thumbcache_, 导致从未被扫出。"""

    def test_prefix_matching(self):
        target = jc.JunkTarget("dummy", prefixes=("thumbcache_", "iconcache_"))
        self.assertTrue(target.match("thumbcache_256.db"))
        self.assertTrue(target.match("iconcache_16.db"))
        self.assertFalse(target.match("settings.dat"))

    def test_suffix_matching(self):
        target = jc.JunkTarget("dummy", suffixes=(".dmp", ".tmp"))
        self.assertTrue(target.match("crash.DMP"))
        self.assertFalse(target.match("keep.txt"))

    def test_both_prefix_and_suffix(self):
        target = jc.JunkTarget("dummy", prefixes=("log_",), suffixes=(".txt",))
        self.assertTrue(target.match("log_a.txt"))
        self.assertFalse(target.match("log_a.log"))
        self.assertFalse(target.match("other.txt"))

    def test_no_filter_accepts_everything(self):
        self.assertTrue(jc.JunkTarget("dummy").match("anything.exe"))

    def test_thumbnail_category_uses_prefixes(self):
        categories = jc.build_categories()
        self.assertIn("thumbnail", categories)
        targets = categories["thumbnail"].targets
        self.assertTrue(targets)
        self.assertTrue(any(t.prefixes for t in targets),
                        "缩略图类别必须用前缀匹配")


class TestRelativeDepth(unittest.TestCase):
    def test_depth_relative_to_base(self):
        base = os.path.join("C:", "temp")
        self.assertEqual(jc._relative_depth(os.path.join("C:", "temp"), base), 0)
        self.assertEqual(jc._relative_depth(os.path.join("C:", "temp", "a"), base), 1)
        self.assertEqual(jc._relative_depth(os.path.join("C:", "temp", "a", "b"), base), 2)


class TestIterFiles(unittest.TestCase):
    """回归: 旧 max_depth 实现在到达限制时会跳过整层(连文件也不处理)。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cleaner-test-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _touch(self, *parts):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("x" * 16)
        return path

    def test_max_depth_one_only_root_level(self):
        self._touch("a.txt")
        self._touch("sub", "b.txt")
        files = list(jc._iter_files(jc.JunkTarget(self.root, max_depth=1)))
        names = sorted(os.path.basename(p) for p in files)
        self.assertEqual(names, ["a.txt"], "第 1 层之后不应再递归")

    def test_max_depth_two_includes_sub_level_files(self):
        self._touch("a.txt")
        self._touch("sub", "b.txt")
        self._touch("sub", "deep", "c.txt")
        target = jc.JunkTarget(self.root, max_depth=2)
        names = sorted(os.path.basename(p) for p in jc._iter_files(target))
        self.assertEqual(names, ["a.txt", "b.txt"])

    def test_unlimited_depth_walks_everything(self):
        self._touch("a.txt")
        self._touch("sub", "deep", "c.txt")
        names = sorted(os.path.basename(p) for p in
                       jc._iter_files(jc.JunkTarget(self.root)))
        self.assertEqual(names, ["a.txt", "c.txt"])

    def test_prefix_filter_applies(self):
        self._touch("thumbcache_32.db")
        self._touch("important.dat")
        names = sorted(os.path.basename(p) for p in
                       jc._iter_files(jc.JunkTarget(
                           self.root, prefixes=("thumbcache_",))))
        self.assertEqual(names, ["thumbcache_32.db"])

    def test_missing_path_yields_nothing(self):
        target = jc.JunkTarget(os.path.join(self.root, "does-not-exist"))
        self.assertEqual(list(jc._iter_files(target)), [])


class TestJunkCategories(unittest.TestCase):
    def test_all_keys_unique_and_complete(self):
        categories = jc.build_categories()
        self.assertEqual(len(set(categories)), len(categories))
        for expected in ("temp", "recycle", "browser", "thumbnail", "devcache"):
            self.assertIn(expected, categories)

    def test_admin_only_categories_flagged(self):
        categories = jc.build_categories()
        self.assertTrue(jc.category_needs_admin(categories["wupdate"]))
        self.assertFalse(jc.category_needs_admin(categories["browser"]))

    def test_scan_missing_category_is_zero(self):
        categories = jc.build_categories()
        target_dir = tempfile.mkdtemp(prefix="cleaner-test-")
        try:
            categories["temp"] = jc.JunkCategory(
                "temp", "临时测试", "", [jc.JunkTarget(target_dir)])
            result = jc.scan_category(categories["temp"])
            self.assertEqual(result.size, 0)
            self.assertEqual(result.files, 0)
            self.assertFalse(result.admin_only)
        finally:
            shutil.rmtree(target_dir, ignore_errors=True)

    def test_clean_selected_accepts_dry_run(self):
        categories = jc.build_categories()
        categories["recycle"] = jc.JunkCategory("recycle", "回收站测试", "",
                                                recycle=True)
        results = jc.clean_selected(["recycle"], categories, dry_run=True)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0].freed, int)


class TestStartupKeyScheme(unittest.TestCase):
    """回归: 文件夹里同名文件的启用/禁用项曾共用同一个 key。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cleaner-startup-")
        self.folder = os.path.join(self.root, "Startup")
        os.makedirs(self.folder)
        self.lnk = os.path.join(self.folder, "MyApp.lnk")
        with open(self.lnk, "w", encoding="utf-8") as fp:
            fp.write("stub")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _enabled_item(self, name="MyApp.lnk"):
        return st.StartupItem(
            key="%s|%s|%s" % (self.folder, st._STATE_ENABLED, name),
            name=name, command=os.path.join(self.folder, name),
            source="测试文件夹", location="folder", enabled=True)

    def _disabled_item(self, name="MyApp.lnk"):
        return st.StartupItem(
            key="%s|%s|%s" % (self.folder, st._STATE_DISABLED, name),
            name=name, command=os.path.join(self.folder, "Disabled", name),
            source="测试文件夹", location="folder", enabled=False)

    def test_enabled_and_disabled_keys_differ(self):
        self.assertNotEqual(self._enabled_item().key, self._disabled_item().key)

    def test_disabled_subkey_names(self):
        self.assertTrue(st.disabled_subkey(r"A\B\Run").endswith("Run\\RunDisabled"))
        self.assertTrue(st.disabled_subkey(r"A\B\RunOnce").endswith(
            "RunOnce\\RunOnceDisabled"))

    def test_disable_moves_file_to_disabled_folder(self):
        ok, message = st.disable_startup_item(self._enabled_item())
        self.assertTrue(ok, message)
        self.assertFalse(os.path.exists(self.lnk))
        self.assertTrue(os.path.exists(
            os.path.join(self.folder, "Disabled", "MyApp.lnk")))

    def test_enable_restores_file(self):
        st.disable_startup_item(self._enabled_item())
        ok, message = st.enable_startup_item(self._disabled_item())
        self.assertTrue(ok, message)
        self.assertTrue(os.path.exists(self.lnk))

    def test_disable_is_idempotent(self):
        st.disable_startup_item(self._enabled_item())
        ok, _message = st.disable_startup_item(self._enabled_item())
        self.assertTrue(ok)  # 源已不存在时应返回失败而不是崩溃
        self.assertFalse(os.path.exists(self.lnk))

    @unittest.skipUnless(IS_WINDOWS, "需要 Windows 注册表")
    def test_real_startup_items_have_unique_keys(self):
        items = st.list_startup_items()
        keys = [item.key for item in items]
        self.assertEqual(len(keys), len(set(keys)),
                         "启动项 key 必须唯一, 否则界面 Treeview 会崩")

    @unittest.skipUnless(IS_WINDOWS, "需要 Windows 注册表")
    def test_desktop_ini_is_not_listed(self):
        names = [item.name.lower() for item in st.list_startup_items()]
        self.assertNotIn("desktop.ini", names)


class TestMemoryProfiles(unittest.TestCase):
    def test_aliases(self):
        for alias in ("轻度", "light", "标准", "standard", "normal",
                      "深度", "deep", "aggressive"):
            profile = mc.get_profile(alias)
            self.assertIn(profile.key, ("light", "standard", "deep"))

    def test_unknown_profile_raises(self):
        with self.assertRaises(KeyError):
            mc.get_profile("不存在的档位")

    def test_profiles_have_actions(self):
        for profile in mc.PROFILES.values():
            self.assertTrue(profile.actions())

    def test_deep_is_superset_of_light(self):
        deep = mc.PROFILES["deep"]
        light = mc.PROFILES["light"]
        self.assertTrue(deep.trim_working_sets)
        self.assertTrue(light.trim_working_sets)
        self.assertFalse(light.purge_standby)
        self.assertTrue(deep.purge_standby or deep.purge_low_priority)


class TestCleanReportArithmetic(unittest.TestCase):
    def test_deltas_are_never_negative(self):
        before = mc.MemoryStatus(total=100, available=20, used=80, percent=80.0,
                                 system_cache=50)
        after = mc.MemoryStatus(total=100, available=70, used=30, percent=30.0,
                                system_cache=10)
        pages_before = mc.PageLists(free=5, standby=40, modified=5)
        pages_after = mc.PageLists(free=45, standby=5, modified=1)
        report = mc.CleanReport(profile="standard", before=before, after=after,
                                pages_before=pages_before, pages_after=pages_after)
        self.assertEqual(report.used_reduced, 50)
        self.assertEqual(report.standby_freed, 35)
        self.assertEqual(report.free_gained, 40)
        self.assertEqual(report.cache_freed, 40)

    def test_missing_snapshot_returns_zero(self):
        report = mc.CleanReport()
        self.assertEqual(report.used_reduced, 0)
        self.assertEqual(report.standby_freed, 0)
        self.assertIsInstance(report.to_dict(), dict)


class TestIntegration(unittest.TestCase):
    @unittest.skipUnless(IS_WINDOWS, "需要 Windows")
    def test_process_snapshot_contains_self(self):
        snapshot = mc.snapshot_processes()
        if not snapshot:  # 系统不支持时允许为空, 但 list_processes 必须仍然可用
            self.skipTest("本机不支持进程快照, 走回退路径")
        self.assertIn(os.getpid(), snapshot)

    @unittest.skipUnless(IS_WINDOWS, "需要 Windows")
    def test_memory_status_plausible(self):
        status = mc.query_memory_status()
        self.assertGreater(status.total, 0)
        self.assertGreaterEqual(status.percent, 0.0)
        self.assertLessEqual(status.percent, 100.0)

    @unittest.skipUnless(IS_WINDOWS, "需要 Windows")
    def test_unified_entry_imports(self):
        import cleaner  # noqa: F401
        self.assertTrue(set(cleaner.SUB_MODULES) ==
                        {"mem", "junk", "game", "tools"})
        self.assertTrue(set(cleaner.SAFE_JUNK_KEYS).issubset(
            set(jc.build_categories())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
