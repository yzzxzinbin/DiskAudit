# -*- coding: utf-8 -*-
"""storage.py 的单元测试：盘根归一、目录键、current.json 的校验与回退。

这一层是「按盘分目录」的唯一真源。它一旦判断错，data 与 content 就会
指到两个不同的盘上去，所以边界都钉在这里。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import storage


def wr(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


class NormTest(unittest.TestCase):
    def test_drive_forms(self):
        for src in ("c", "C", "c:", "C:", "C:\\", " c: ", "c:/"):
            self.assertEqual(storage.norm(src), "C:", repr(src))

    def test_non_drive_path_kept(self):
        self.assertEqual(storage.norm("D:\\Work\\"), "D:\\Work")
        self.assertEqual(storage.norm(""), "")
        self.assertEqual(storage.norm(None), "")

    def test_key_is_safe(self):
        self.assertEqual(storage.key("C:"), "C")
        self.assertEqual(storage.key("D:\\Work"), "DWORK")
        # 全是标点的输入不能变成空目录名，也不能带出路径分隔符
        self.assertEqual(storage.key("../.."), "ROOT")
        self.assertEqual(storage.key(""), "ROOT")

    def test_key_has_no_separators(self):
        for src in ("C:", "..\\..\\etc", "C:/x/y", "//?/C:"):
            k = storage.key(src)
            self.assertNotIn("/", k)
            self.assertNotIn("\\", k)
            self.assertNotIn("..", k)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="diskaudit-store-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_data_and_content_share_the_key(self):
        ds = storage.data_store(self.d, "K:")
        cs = storage.content_store(os.path.join(self.d, "content"), "K:")
        # 两个根不同，但同一个盘必须落在同名目录里，否则读一个盘会读到另一个盘的文字层
        self.assertEqual(os.path.basename(ds["dir"]), "K")
        self.assertEqual(os.path.basename(cs["dir"]), "K")
        self.assertEqual(ds["dir"], os.path.join(self.d, "drives", "K"))
        self.assertEqual(cs["dir"], os.path.join(self.d, "content", "drives", "K"))

    def test_paths_are_inside_their_own_drive(self):
        """一个盘的任何路径都不许跑到别的盘目录里去。"""
        a = storage.data_store(self.d, "C:")
        b = storage.data_store(self.d, "D:")
        for k in ("tree", "links", "meta", "summary", "history"):
            self.assertTrue(a[k].startswith(a["dir"] + os.sep), k)
            self.assertFalse(a[k].startswith(b["dir"] + os.sep), k)


class CurrentTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="diskaudit-current-")
        self.drives = os.path.join(self.d, "drives")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def make_drive(self, root, at, tree=True, summary=True):
        ds = storage.data_store(self.d, root)
        os.makedirs(ds["dir"], exist_ok=True)
        if summary:
            wr(ds["summary"], {"root": root, "drive": root, "at": at})
        if tree:
            wr(ds["tree"], {"root": root, "tree": {"n": root, "c": []}})
        return ds

    def test_reads_current_json(self):
        self.make_drive("C:", "2026-10-24 05:00")
        self.make_drive("K:", "2026-10-20 05:00")
        storage.write_current(self.d, "K:")
        self.assertEqual(storage.read_current(self.d), "K:")
        # 写法不统一也不会读丢
        storage.write_current(self.d, "k:\\")
        self.assertEqual(storage.read_current(self.d), "K:")

    def test_stale_current_falls_back_to_newest_drive_with_tree(self):
        """current.json 指着没树的盘时必须换一个，不能把服务带到 503。"""
        self.make_drive("C:", "2026-10-24 05:00")
        self.make_drive("K:", "2026-10-30 05:00")
        storage.write_current(self.d, "D:")
        self.assertEqual(storage.read_current(self.d), "K:")

    def test_no_tree_anywhere_means_first_run(self):
        self.make_drive("C:", "2026-10-24 05:00", tree=False)
        storage.write_current(self.d, "C:")
        self.assertEqual(storage.read_current(self.d), "")

    def test_broken_current_json_falls_back(self):
        self.make_drive("C:", "2026-10-24 05:00")
        with io.open(os.path.join(self.d, "current.json"), "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(storage.read_current(self.d), "C:")

    def test_nothing_at_all(self):
        self.assertEqual(storage.read_current(self.d), "")
        self.assertEqual(storage.archived(self.d), [])

    def test_has_tree_ignores_summary_and_history(self):
        """只有 drive.json 或只有 history/ 都不算「有数据」。"""
        ds = self.make_drive("C:", "2026-10-24 05:00", tree=False)
        os.makedirs(ds["history"], exist_ok=True)
        self.assertFalse(storage.has_tree(self.d, "C:"))
        wr(ds["tree"], {"root": "C:"})
        self.assertTrue(storage.has_tree(self.d, "C:"))
        self.assertFalse(storage.has_tree(self.d, "D:"))

    # ---- 活动盘指针（「在看哪个盘」和「上次扫的盘」是两件事）----

    def test_active_roundtrip(self):
        self.make_drive("C:", "2026-10-24 05:00")
        self.assertTrue(storage.write_active(self.d, "c:\\"))
        self.assertEqual(storage.read_active(self.d), "C:")

    def test_active_and_current_are_independent(self):
        self.make_drive("C:", "2026-10-20 05:00")
        self.make_drive("K:", "2026-10-30 05:00")
        storage.write_current(self.d, "C:")
        storage.write_active(self.d, "K:")
        self.assertEqual(storage.read_current(self.d), "C:")
        self.assertEqual(storage.read_active(self.d), "K:")

    def test_active_refuses_drive_without_tree(self):
        """没树的盘不记：记了下次打开也是 404。"""
        self.assertFalse(storage.write_active(self.d, "D:"))
        self.assertFalse(os.path.exists(os.path.join(self.d, "active.json")))

    def test_active_goes_stale_when_tree_disappears(self):
        ds = self.make_drive("C:", "2026-10-24 05:00")
        storage.write_active(self.d, "C:")
        os.remove(ds["tree"])
        self.assertEqual(storage.read_active(self.d), "")
        self.assertEqual(storage.read_active(self.d, "K:"), "K:")

    def test_active_missing_returns_fallback(self):
        self.assertEqual(storage.read_active(self.d, "D:"), "D:")
        self.assertEqual(storage.read_active(self.d), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
