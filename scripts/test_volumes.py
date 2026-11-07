# -*- coding: utf-8 -*-
"""volumes.py 的单元测试：盘符归一、细化根推导、阶段识别、本机枚举。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import volumes


class TNormDrive(unittest.TestCase):
    def test_forms(self):
        for s in ("d", "D:", "D:\\", " d ", "D:////", "D:/"):
            self.assertEqual(volumes.norm_drive(s), "D:", s)

    def test_rejects(self):
        for s in ("", "C:\\Windows", "CD", ".", "/x", "1:", None):
            self.assertEqual(volumes.norm_drive(s), "", repr(s))

    def test_same(self):
        self.assertTrue(volumes.same_drive("c:", "C:\\"))
        self.assertFalse(volumes.same_drive("C:", "D:"))
        self.assertFalse(volumes.same_drive("", ""))


class TDetailRoot(unittest.TestCase):
    def test_home_on_drive(self):
        self.assertEqual(volumes.detail_root_for("C:", r"C:\Users\alice"),
                         r"C:\Users\alice")
        self.assertEqual(volumes.detail_root_for("c", r"C:/Users/alice"),
                         r"C:\Users\alice")

    def test_home_outside(self):
        # 扫 D 盘时不能拿 C 盘的家目录当细化根：接不进全盘树
        self.assertEqual(volumes.detail_root_for("D:", r"C:\Users\alice"), "")

    def test_empty(self):
        self.assertEqual(volumes.detail_root_for("C:", ""), "")
        self.assertEqual(volumes.detail_root_for("", r"C:\Users\alice"), "")

    def test_default_home(self):
        # 不传 home 时取当前账户家目录，默认就在系统盘上
        home = (os.environ.get("USERPROFILE") or "").rstrip("\\")
        self.assertEqual(volumes.detail_root_for(os.environ.get("SystemDrive") or "C:"),
                         home)


class TScanPhase(unittest.TestCase):
    BASE = ("  盘符        C:\n"
            "[1/3] 扫描 C:\\ 目录树（全盘 + 细化）…\n"
            "  [scan] 1,024 文件 · 0.10 GB · C:\\Windows\n")

    def test_stages(self):
        self.assertEqual(volumes.scan_phase(self.BASE), "tree")
        self.assertEqual(volumes.scan_phase(self.BASE + "[2/3] 测量链接 …\n"), "links")
        self.assertEqual(
            volumes.scan_phase(self.BASE + "[2/3] x\n[整形] y\n[snap] z\n"), "merge")

    def test_last_mark_wins(self):
        log = "[1/3] a\n[2/3] b\n[1/3] c\n"
        self.assertEqual(volumes.scan_phase(log), "tree")

    def test_unknown(self):
        self.assertEqual(volumes.scan_phase(""), "start")
        self.assertEqual(volumes.scan_phase("随便什么\n"), "start")

    def test_progress_line_is_not_stage(self):
        # [scan] 是进度行，别当成阶段标记
        self.assertEqual(volumes.scan_phase("  [scan] 1 文件 · 0.00 GB · C:\\\n"), "start")


@unittest.skipUnless(os.name == "nt", "盘符枚举只对 Windows 有意义")
class TListVolumes(unittest.TestCase):
    def test_shape(self):
        vols = volumes.list_volumes()
        self.assertTrue(vols, "至少应该列出一个盘")
        roots = []
        for v in vols:
            self.assertRegex(v["root"], r"^[A-Z]:$")
            self.assertIn(v["kind"], ("fixed", "removable"))
            self.assertGreater(v["capGB"], 0)
            self.assertGreaterEqual(v["usedPct"], 0.0)
            self.assertLessEqual(v["usedPct"], 100.0)
            self.assertGreaterEqual(v["freeGB"], 0.0)
            self.assertIsInstance(v["system"], bool)
            roots.append(v["root"])
        self.assertEqual(roots, sorted(roots), "按盘符排序")
        self.assertEqual(len(roots), len(set(roots)), "不能有重复")


if __name__ == "__main__":
    unittest.main(verbosity=2)
