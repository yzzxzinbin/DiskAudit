# -*- coding: utf-8 -*-
"""ticket.gen() 的删除边界。

ticket 是唯一一份「人写的文字」，它的生成逻辑里有一句
`if not items: os.remove(path)`。而「没有候选」有两种完全不同的原因：
缺口真的清零了，或者数据根本没读出来（tree 缺失 / purpose.json 损坏）。
后者如果也删，用户填了一半的内容就静默没了。
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

import ticket

TREE = {
    "root": "C:", "rootLabel": "C:\\", "depth": 6, "fileMinMB": 20,
    "minMB": 40, "minMBSub": 12, "subRoot": "", "home": r"C:\Users\User",
    "systemDrive": "C:", "totalBytes": 0, "fileCount": 0, "dirCount": 0,
    "skipped": [], "tree": {"n": "C:", "b": 0, "c": []},
}
PURPOSE = {"version": 1, "note": "", "tags": [],
           "exact": [], "names": [], "paths": []}


class GenTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="diskaudit-ticket-")
        self.data = os.path.join(self.base, "data")
        self.content = os.path.join(self.base, "content")
        os.makedirs(self.data)
        os.makedirs(self.content)
        self.tk = os.path.join(self.content, "ticket.md")
        self.write(self.tk, "# 用户填了一半的 ticket\n正文\n")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def write(self, p, text):
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def write_json(self, p, obj):
        self.write(p, json.dumps(obj, ensure_ascii=False))

    def tree(self):
        self.write_json(os.path.join(self.data, "tree.json"), TREE)

    def purpose(self):
        self.write_json(os.path.join(self.content, "purpose.json"), PURPOSE)

    def test_missing_tree_keeps_ticket(self):
        """树还没生成：不能把「没数据」当成「没缺口」。"""
        self.purpose()
        self.assertEqual(ticket.gen(self.data, self.content, self.tk, limit=0), (None, 0))
        self.assertTrue(os.path.exists(self.tk))

    def test_broken_purpose_keeps_ticket(self):
        """用途库解析不出来，同样是「没数据」。"""
        self.tree()
        self.write(os.path.join(self.content, "purpose.json"), "{ 坏掉的 json")
        self.assertEqual(ticket.gen(self.data, self.content, self.tk, limit=0), (None, 0))
        self.assertTrue(os.path.exists(self.tk))

    def test_no_gaps_removes_ticket(self):
        """数据齐全、确实没有缺口时才删旧 ticket（别把守卫做成永不删）。"""
        self.tree()
        self.purpose()
        self.assertEqual(ticket.gen(self.data, self.content, self.tk, limit=0), (None, 0))
        self.assertFalse(os.path.exists(self.tk))

    def test_legacy_name_is_renamed(self):
        """旧版文件名：改名而不是当「没有 ticket」重写，填的正文必须原样保留。"""
        os.remove(self.tk)
        old = os.path.join(self.content, ticket.LEGACY_NAME)
        self.write(old, "# 用户填了一半的 ticket\n正文\n")

        self.assertEqual(ticket.adopt(self.tk), self.tk)
        self.assertFalse(os.path.exists(old))
        with io.open(self.tk, encoding="utf-8") as f:
            self.assertIn("用户填了一半", f.read())

    def test_both_names_keeps_new(self):
        """两个都在：以新文件为准，旧的原样留着，不替用户做删除决定。"""
        old = os.path.join(self.content, ticket.LEGACY_NAME)
        self.write(old, "旧的\n")

        self.assertEqual(ticket.adopt(self.tk), self.tk)
        self.assertTrue(os.path.exists(old))
        with io.open(self.tk, encoding="utf-8") as f:
            self.assertIn("用户填了一半", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
