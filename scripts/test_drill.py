# -*- coding: utf-8 -*-
"""drill.py 的单元测试：用临时目录验证动态下钻，不碰真实磁盘。"""
import email.message
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app
import drill


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


class DrillTest(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp(prefix="diskaudit-drill-")
        self.root = os.path.join(self.top, "root")
        os.makedirs(self.root)
        self.outside = os.path.join(self.top, "outside")
        os.makedirs(self.outside)
        write(os.path.join(self.outside, "outside.bin"), 12345)
        self.links = []

    def tearDown(self):
        for link in self.links:
            if os.path.exists(link):
                os.rmdir(link)
        shutil.rmtree(self.top, ignore_errors=True)

    def junction(self, link, target):
        make_junction(link, target)
        self.links.append(link)

    def test_nested_tree_and_small_files(self):
        p = self.root
        for i in range(1, 9):
            p = os.path.join(p, "d%d" % i)
            os.makedirs(p)
        write(os.path.join(p, "deep.bin"), 2048)
        write(os.path.join(self.root, "big.bin"), 4096)
        write(os.path.join(self.root, "small.bin"), 512)   # <1KB：不单独成行
        write(os.path.join(self.root, "zero.bin"), 0)
        os.makedirs(os.path.join(self.root, "empty"))

        out = drill.scan(self.root, self.root)
        names = {c["n"]: c for c in out["children"]}

        self.assertIn("d1", names)
        self.assertEqual(names["d1"]["b"], 2048)
        self.assertEqual(names["big.bin"]["b"], 4096)
        self.assertEqual(names["empty"]["b"], 0)           # 目录不过滤
        self.assertNotIn("small.bin", names)               # <1KB 不单独成行
        self.assertNotIn("zero.bin", names)
        self.assertEqual(len(out["children"]), 3)
        self.assertEqual(out["bytes"], 2048 + 4096 + 512)  # 体积仍全额计入

    def test_root_and_children_share_one_path(self):
        child = os.path.join(self.root, "a")
        os.makedirs(child)
        write(os.path.join(child, "f.bin"), 2048)

        as_root = drill.scan(self.root, self.root)
        as_child = drill.scan(child, self.root)
        self.assertEqual(as_root["children"][0]["b"], 2048)
        self.assertEqual(as_child["children"][0]["b"], 2048)
        self.assertEqual(as_child["path"], os.path.normpath(child))

    def test_junction_is_skipped_and_not_counted(self):
        write(os.path.join(self.root, "real.bin"), 10)
        self.junction(os.path.join(self.root, "jump"), self.outside)

        out = drill.scan(self.root, self.root)
        names = {c["n"]: c for c in out["children"]}
        self.assertNotIn("jump", names)
        self.assertNotIn("real.bin", names)     # <1KB 不成行……
        self.assertEqual(out["bytes"], 10)      # ……但体积已计入
        self.assertEqual(out["skipped"], 1)

    def test_file_rows_capped_at_500(self):
        sub = os.path.join(self.root, "many")
        os.makedirs(sub)
        for i in range(600):
            write(os.path.join(sub, "f%03d.bin" % i), 1024)
        write(os.path.join(self.root, "tiny.txt"), 3)

        out = drill.scan(sub, self.root)
        rows = out["children"]
        self.assertEqual(len(rows), 500)                       # 文件行封顶
        self.assertTrue(all(r["f"] == 1 for r in rows))
        self.assertEqual(rows[0]["n"], "f000.bin")             # 等体积按名字序，截掉最小的
        self.assertEqual(rows[-1]["n"], "f499.bin")
        self.assertEqual(out["bytes"], 600 * 1024)             # 体积全额计入
        self.assertEqual(out["truncated"], 100)                # 截断数如实上报

        root_out = drill.scan(self.root, self.root)
        self.assertEqual(root_out["children"][0]["n"], "many")
        self.assertEqual(root_out["children"][0]["b"], 600 * 1024)

    def test_nested_junction_is_skipped(self):
        sub = os.path.join(self.root, "sub")
        os.makedirs(sub)
        write(os.path.join(sub, "real.bin"), 10)
        self.junction(os.path.join(sub, "jump"), self.outside)

        out = drill.scan(self.root, self.root)
        self.assertEqual(out["bytes"], 10)
        self.assertEqual(out["skipped"], 1)

    def test_junction_target_outside_root_is_denied(self):
        link = os.path.join(self.root, "jump")
        self.junction(link, self.outside)
        with self.assertRaises(drill.DrillError) as cm:
            drill.scan(link, self.root)
        self.assertEqual(cm.exception.code, 403)

    def test_resolve_rejects_outside_sibling_and_bad_paths(self):
        sibling = self.root + "extra"
        os.makedirs(sibling)

        for path, code in [
            (sibling, 403),
            (self.outside, 403),
            (os.path.join(self.root, "missing"), 404),
            ("relative", 400),
            ("", 400),
            ("C:relative", 400),
        ]:
            with self.assertRaises(drill.DrillError) as cm:
                drill.scan(path, self.root)
            self.assertEqual(cm.exception.code, code, path)

    def test_file_target_is_rejected(self):
        p = os.path.join(self.root, "f.bin")
        write(p, 1)
        with self.assertRaises(drill.DrillError) as cm:
            drill.scan(p, self.root)
        self.assertEqual(cm.exception.code, 400)

    def test_root_canonicalizes_drive_and_trailing_separator(self):
        with_sep = drill.canonical_root(self.root + "\\")
        without_sep = drill.canonical_root(self.root)
        self.assertEqual(os.path.normcase(with_sep), os.path.normcase(self.root + "\\"))
        self.assertEqual(os.path.normcase(without_sep), os.path.normcase(self.root + "\\"))

    def test_case_insensitive_root(self):
        if os.name != "nt":
            self.skipTest("Windows 路径才不区分大小写")
        out = drill.scan(self.root.upper(), self.root.lower())
        self.assertEqual(os.path.normcase(out["path"]), os.path.normcase(self.root))


class LocalOnlyTest(unittest.TestCase):
    def handler(self, client, host, referer=None):
        h = object.__new__(app.Handler)
        h.client_address = (client, 50000)
        h.headers = email.message.Message()
        if host is not None:
            h.headers["Host"] = host
        if referer is not None:
            h.headers["Referer"] = referer
        return h

    def test_loopback_and_allowed_hosts(self):
        self.assertTrue(self.handler("127.0.0.1", "127.0.0.1:8765")._local_only())
        self.assertTrue(self.handler("::1", "[::1]:8765")._local_only())
        self.assertTrue(self.handler("::ffff:127.0.0.1", "localhost")._local_only())
        self.assertTrue(self.handler("127.0.0.1", "localhost",
                                     "http://localhost:8765/")._local_only())

    def test_rejects_remote_client(self):
        self.assertFalse(self.handler("192.168.1.20", "127.0.0.1:8765")._local_only())

    def test_rejects_bad_or_missing_host(self):
        self.assertFalse(self.handler("127.0.0.1", "evil.example:8765")._local_only())
        self.assertFalse(self.handler("127.0.0.1", None)._local_only())

    def test_rejects_cross_site_referer(self):
        self.assertFalse(self.handler("127.0.0.1", "127.0.0.1:8765",
                                      "http://evil.example/")._local_only())
        self.assertFalse(self.handler("127.0.0.1", "127.0.0.1:8765",
                                      "not-a-url")._local_only())


class ScanOptionsTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(app.scan_options(), [])

    def test_drive_and_overrides(self):
        self.assertEqual(
            app.scan_options(r"D:", r"D:\Work", 4, 80, 50, 20, True),
            ["-Drive", r"D:", "-DetailRoot", r"D:\Work", "-Depth", "4",
             "-MinMB", "80", "-FileMinMB", "50", "-DetailMinMB", "20",
             "-SkipLinks"],
        )


class DrivePickTest(unittest.TestCase):
    """扫描前要不要问盘：问错了会卡住计划任务，不问又回到「只能扫系统盘」。"""

    def test_explicit_drive(self):
        self.assertFalse(app.need_drive_pick(True, False, False, "D:", False, True, True))

    def test_non_tty(self):
        self.assertFalse(app.need_drive_pick(True, False, False, None, False, True, False))

    def test_skip_scan(self):
        self.assertFalse(app.need_drive_pick(False, False, True, None, False, False, True))

    def test_no_ask(self):
        self.assertFalse(app.need_drive_pick(True, False, False, None, True, True, True))

    def test_update_asks(self):
        self.assertTrue(app.need_drive_pick(True, False, False, None, False, True, True))

    def test_rescan_asks(self):
        self.assertTrue(app.need_drive_pick(False, True, False, None, False, True, True))

    def test_first_run_asks(self):
        self.assertTrue(app.need_drive_pick(False, False, False, None, False, False, True))

    def test_plain_start_does_not_ask(self):
        self.assertFalse(app.need_drive_pick(False, False, False, None, False, True, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
