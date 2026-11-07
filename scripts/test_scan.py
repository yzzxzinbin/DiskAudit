# -*- coding: utf-8 -*-
"""scan_tree.py 的单元测试：临时目录 + 真实 junction，不碰真实磁盘。

重点盯两件事：
    - 合并模式（细化根在扫描根内，一次遍历两本账）的细化树
      必须与「对细化根单独扫一遍」的产物逐字段相等；
    - reparse point 一律跳过，绝不把联接点目标算进体积。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "collect"))

import scan_tree


def write(path, size):
    with open(path, "wb") as f:
        f.write(b"x" * size)


def make_junction(link, target):
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, target],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if r.returncode != 0:
        raise AssertionError(
            "无法创建 junction（测试不允许跳过）：%s" %
            (r.stderr or r.stdout).decode("mbcs", "replace")
        )


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp(prefix="diskaudit-scan-")
        self.root = os.path.join(self.top, "root")
        self.outside = os.path.join(self.top, "outside")
        os.makedirs(self.root)
        os.makedirs(os.path.join(self.outside, "sub"))
        #                root
        #                ├─ a/            ← 细化根
        #                │  ├─ big.bin        20000
        #                │  ├─ tiny.bin         100
        #                │  └─ b/
        #                │     ├─ mid.bin      8000
        #                │     └─ c/
        #                │        └─ deep.bin  5000
        #                ├─ other/ o.bin      30000
        #                ├─ small/ s.bin       1500
        #                ├─ empty/
        #                └─ lnk → junction → outside
        a = os.path.join(self.root, "a")
        b = os.path.join(a, "b")
        c = os.path.join(b, "c")
        for d in (a, b, c,
                  os.path.join(self.root, "other"),
                  os.path.join(self.root, "small"),
                  os.path.join(self.root, "empty")):
            os.makedirs(d)
        write(os.path.join(a, "big.bin"), 20000)
        write(os.path.join(a, "tiny.bin"), 100)
        write(os.path.join(b, "mid.bin"), 8000)
        write(os.path.join(c, "deep.bin"), 5000)
        write(os.path.join(self.root, "other", "o.bin"), 30000)
        write(os.path.join(self.root, "small", "s.bin"), 1500)
        write(os.path.join(self.outside, "outside.bin"), 777)
        write(os.path.join(self.outside, "sub", "o1.bin"), 4000)
        self.links = []
        self.junction(os.path.join(self.root, "lnk"), self.outside)
        self.a = a

    def tearDown(self):
        for link in self.links:
            if os.path.exists(link):
                os.rmdir(link)
        shutil.rmtree(self.top, ignore_errors=True)

    def junction(self, link, target):
        make_junction(link, target)
        self.links.append(link)

    @staticmethod
    def strip_deep(payload):
        """深索引是主扫描专属产物，细化树的等价性比较不含它。"""
        p = dict(payload)
        p.pop("deepDirs", None)
        p.pop("deepFiles", None)
        return p

    def detail_of(self, root, a):
        return scan_tree.scan(
            root, depth=6, min_mb=0.001, file_min_mb=0.002,
            detail={"root": a, "depth": 1, "min_mb": 0.0005})

    def children(self, node):
        return {c["n"]: c for c in node.get("c", [])}

    # ---------------------------------------------------------- 基本树

    def test_counts_thresholds_and_prune(self):
        r = scan_tree.scan(self.root, depth=6, min_mb=0.001, file_min_mb=0.002)
        full = r["full"]

        # 64600 = 20000+100+8000+5000+30000+1500；junction 目标里的 777 不算
        self.assertEqual(full["totalBytes"], 64600.0)
        self.assertEqual(full["fileCount"], 6)
        self.assertEqual(full["dirCount"], 6)
        self.assertEqual(len(full["skipped"]), 1)
        self.assertTrue(full["skipped"][0].endswith("lnk"))
        self.assertEqual(full["root"], self.root)
        self.assertEqual(full["rootLabel"], self.root + "\\")
        self.assertEqual(full["minMB"], 0.001)
        self.assertEqual(full["fileMinMB"], 0.002)

        top = self.children(full["tree"])
        # empty（0 字节 < 门槛）被剪掉；junction 从不成为节点
        self.assertEqual(set(top), {"a", "other", "small"})
        self.assertEqual(top["other"]["b"], 30000.0)
        self.assertEqual(top["small"]["b"], 1500.0)

        kids_a = self.children(top["a"])
        self.assertEqual(top["a"]["b"], 33100.0)
        self.assertEqual(set(kids_a), {"b", "big.bin"})
        self.assertEqual(kids_a["big.bin"]["f"], 1)
        self.assertNotIn("tiny.bin", kids_a)      # 100 < fileMinMB

        kids_b = self.children(kids_a["b"])
        self.assertEqual(kids_a["b"]["b"], 13000.0)
        self.assertEqual(set(kids_b), {"c", "mid.bin"})
        self.assertEqual(self.children(kids_b["c"])["deep.bin"]["b"], 5000.0)

    def test_depth_caps_attribution_but_not_traversal(self):
        r = scan_tree.scan(self.root, depth=2, min_mb=0.001, file_min_mb=0.002)
        full = r["full"]

        # 遍历照常到底：体积、文件数不因 depth 变小
        self.assertEqual(full["totalBytes"], 64600.0)
        self.assertEqual(full["fileCount"], 6)

        top = self.children(full["tree"])
        kids_a = self.children(top["a"])
        self.assertEqual(set(kids_a), {"b", "big.bin"})
        # level 3 的 c 不再建节点；mid.bin 的父目录在 level 2，仍算文件节点
        self.assertEqual(set(self.children(kids_a["b"])), {"mid.bin"})
        # a\b 的体积仍含 deep.bin（归账封顶在 depth，遍历不封顶）
        self.assertEqual(kids_a["b"]["b"], 13000.0)

    def test_zero_min_mb_keeps_all_files(self):
        # 空目录不进树：没有文件归账就没有节点，与门槛无关
        r = scan_tree.scan(self.root, depth=6, min_mb=0, file_min_mb=0)
        top = self.children(r["full"]["tree"])
        self.assertEqual(set(top), {"a", "other", "small"})
        self.assertIn("tiny.bin", self.children(top["a"]))
        self.assertIn("tiny.bin", json.dumps(r["full"]))

    # ---------------------------------------------------------- 深索引

    def test_deep_index_tracks_beyond_depth(self):
        r = scan_tree.scan(self.root, depth=2, min_mb=0.001, file_min_mb=0.002)
        full = r["full"]
        dd = full["deepDirs"]
        # depth=2 的树里没有 c（level 3），深索引里必须有，b 是整棵子树总量
        c_path = os.path.join(self.root, "a", "b", "c")
        self.assertIn(c_path, dd)
        self.assertEqual(dd[c_path], 5000.0)
        self.assertIn(os.path.join(self.root, "other"), dd)
        # 低于门槛的目录不进深索引（与主树同一门槛）
        self.assertNotIn(os.path.join(self.root, "empty"), dd)
        df = full["deepFiles"]
        big = os.path.join(self.root, "a", "big.bin")
        self.assertIn(big, df)
        self.assertEqual(df[big], 20000.0)
        self.assertNotIn(os.path.join(self.root, "a", "tiny.bin"), df)

    def test_deep_index_depth_cap(self):
        p = self.root
        for i in range(1, 20):
            p = os.path.join(p, "d%d" % i)
        os.makedirs(p)
        write(os.path.join(p, "deep.bin"), 30000)
        r = scan_tree.scan(self.root, depth=6, min_mb=0.001, file_min_mb=0.002)
        full = r["full"]
        deepest = max(full["deepDirs"], key=lambda k: k.count("\\"))
        self.assertLessEqual(deepest.count("\\") - self.root.count("\\"), 16)
        # 第 16 层以深的文件不进深文件索引（注意别误伤夹具里 a/b/c/deep.bin）
        self.assertNotIn(os.path.join(p, "deep.bin"), full["deepFiles"])
        # 但它的体积仍归账到深索引里的第 16 层祖先
        lv16 = os.path.join(self.root, *[("d%d" % i) for i in range(1, 17)])
        self.assertGreaterEqual(full["deepDirs"][lv16], 30000.0)
        # 主树口径不受影响：still capped at depth
        self.assertEqual(full["totalBytes"], 64600.0 + 30000.0)

    def test_detail_payload_has_no_deep(self):
        merged = self.detail_of(self.root, self.a)
        self.assertNotIn("deepDirs", merged["detail"])

    # ---------------------------------------------------------- junction

    def test_junction_target_never_counted(self):
        r = scan_tree.scan(self.root, depth=6, min_mb=0, file_min_mb=0)
        self.assertEqual(r["full"]["totalBytes"], 64600.0)
        text = json.dumps(r["full"]["tree"])
        self.assertNotIn("outside.bin", text)
        self.assertNotIn("o1.bin", text)

    # ---------------------------------------------------------- 细化树

    def test_merged_detail_equals_standalone_scan(self):
        merged = self.detail_of(self.root, self.a)
        alone = scan_tree.scan(self.a, depth=1, min_mb=0.0005, file_min_mb=0.002)
        self.assertEqual(merged["detail"], self.strip_deep(alone["full"]))

    def test_detail_payload_shape(self):
        merged = self.detail_of(self.root, self.a)
        det = merged["detail"]
        self.assertEqual(det["totalBytes"], 33100.0)
        self.assertEqual(det["fileCount"], 4)
        self.assertEqual(det["dirCount"], 2)
        self.assertEqual(det["skipped"], [])   # junction 在细化根之外
        kids = self.children(det["tree"])
        self.assertEqual(set(kids), {"b", "big.bin"})
        # depth=1：b 还在（mid.bin 的父目录恰在深度 1），c 不再建节点
        self.assertEqual(set(self.children(kids["b"])), {"mid.bin"})

    def test_detail_root_equals_scan_root(self):
        r = scan_tree.scan(
            self.root, depth=2, min_mb=0.001, file_min_mb=0.002,
            detail={"root": self.root, "depth": 2, "min_mb": 0.0025})
        full, det = r["full"], r["detail"]
        self.assertEqual(det["fileCount"], full["fileCount"])
        self.assertEqual(det["totalBytes"], full["totalBytes"])
        self.assertEqual(det["dirCount"], full["dirCount"])
        self.assertEqual(det["skipped"], full["skipped"])
        # 目录门槛不同：small（1500）在全盘树保留、在细化树被剪
        self.assertIn("small", self.children(full["tree"]))
        self.assertNotIn("small", self.children(det["tree"]))
        self.assertEqual(det["minMB"], 0.0025)

    def test_detail_root_outside_scan_root(self):
        r = scan_tree.scan(
            self.root, depth=6, min_mb=0.001, file_min_mb=0.002,
            detail={"root": self.outside, "depth": 2, "min_mb": 0.001})
        alone = scan_tree.scan(self.outside, depth=2, min_mb=0.001, file_min_mb=0.002)
        self.assertEqual(r["detail"], self.strip_deep(alone["full"]))
        self.assertEqual(r["detail"]["totalBytes"], 4777.0)

    def test_detail_root_as_junction_stays_empty(self):
        lnk = os.path.join(self.root, "lnk")
        r = scan_tree.scan(
            self.root, depth=6, min_mb=0.001, file_min_mb=0.002,
            detail={"root": lnk, "depth": 2, "min_mb": 0.001})
        det = r["detail"]
        self.assertEqual(det["totalBytes"], 0.0)
        self.assertEqual(det["fileCount"], 0)
        self.assertEqual(det["skipped"], [])
        self.assertEqual(det["tree"]["c"], [])

    def test_missing_detail_root_skips_detail(self):
        r = scan_tree.scan(
            self.root, depth=6, min_mb=0.001, file_min_mb=0.002,
            detail={"root": os.path.join(self.top, "nope"), "depth": 2, "min_mb": 0.001})
        self.assertIsNone(r["detail"])
        self.assertIsNotNone(r["full"])

    # ---------------------------------------------------------- CLI

    def test_cli_writes_both_trees_and_logs(self):
        out_full = os.path.join(self.top, "full.json")
        out_det = os.path.join(self.top, "detail.json")
        cmd = [sys.executable, os.path.join(HERE, "collect", "scan_tree.py"),
               "--root", self.root, "--depth", "2",
               "--min-mb", "0.001", "--file-min-mb", "0.002",
               "--out", out_full,
               "--log", os.path.join(self.top, "full.log"),
               "--detail-root", self.a, "--detail-depth", "1",
               "--detail-min-mb", "0.0005",
               "--detail-out", out_det,
               "--detail-log", os.path.join(self.top, "detail.log")]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0,
                         p.stderr.decode("mbcs", "replace"))

        inproc = scan_tree.scan(
            self.root, depth=2, min_mb=0.001, file_min_mb=0.002,
            detail={"root": self.a, "depth": 1, "min_mb": 0.0005})
        with open(out_full, encoding="utf-8") as f:
            self.assertEqual(json.load(f), inproc["full"])
        with open(out_det, encoding="utf-8") as f:
            self.assertEqual(json.load(f), inproc["detail"])

        with open(os.path.join(self.top, "full.log"), encoding="utf-8") as f:
            self.assertTrue(f.readline().startswith("root="))
        with open(os.path.join(self.top, "detail.log"), encoding="utf-8") as f:
            self.assertTrue(f.readline().startswith("root="))

    def test_cli_bad_root_fails_loudly(self):
        cmd = [sys.executable, os.path.join(HERE, "collect", "scan_tree.py"),
               "--root", os.path.join(self.top, "nope"),
               "--out", os.path.join(self.top, "x.json")]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.top, "x.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
