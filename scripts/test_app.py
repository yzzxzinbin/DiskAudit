# -*- coding: utf-8 -*-
"""app.py 里和分盘有关的纯逻辑：留档能不能删、哪个盘算「当前」。

这两件事都是「看错盘就出事」的类型：守卫必须认「页面正在看哪个盘」，而切盘
只跳 ?root=、进程里的 SOURCES 不动 —— 拿 SOURCES 顶替，页面明明在看 K 盘，
K 的删除按钮也会照样放行。所以把它们从 handler 里拆出来单独钉住。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import email.message

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app
import storage


def wr(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


class DeleteGuardTest(unittest.TestCase):
    def test_viewed_drive_is_refused(self):
        self.assertTrue(app.delete_block_reason("K:", "K:", "D:"))

    def test_default_drive_is_refused(self):
        self.assertTrue(app.delete_block_reason("D:", "K:", "D:"))

    def test_other_drive_is_allowed(self):
        self.assertIsNone(app.delete_block_reason("K:", "C:", "D:"))

    def test_forms_do_not_matter(self):
        """K / K: / K:\\ / k: 都是同一个盘。"""
        for target in ("K", "K:", "K:\\", "k:", "K:/"):
            self.assertTrue(app.delete_block_reason(target, "K:\\", "D:"), target)
        self.assertIsNone(app.delete_block_reason("k:/", "C:", "D:"))

    def test_missing_target(self):
        self.assertTrue(app.delete_block_reason("", "K:", "D:"))

    def test_no_default_yet(self):
        """还没有任何数据时 default 是空串，不能把任何盘都挡掉。"""
        self.assertIsNone(app.delete_block_reason("K:", "", ""))


class DrivesPayloadTest(unittest.TestCase):
    """active 必须跟着调用方传进来的盘，而不是全局 SOURCES。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="diskaudit-app-")
        self.old_data, self.old_sources = app.DATA, app.SOURCES
        app.DATA = self.d
        for root in ("C:", "K:"):
            ds = storage.data_store(self.d, root)
            wr(ds["summary"], {"root": root, "drive": root, "at": "2026-10-24 05:00"})
            wr(ds["tree"], {"root": root, "tree": {"n": root, "c": []}})
        # 服务端默认盘（相当于 current.json 记的那个）
        app.SOURCES = {
            "tree": storage.data_store(self.d, "C:")["tree"],
            "links": "", "meta": "", "purpose": "", "report": "",
        }

    def tearDown(self):
        app.DATA, app.SOURCES = self.old_data, self.old_sources
        shutil.rmtree(self.d, ignore_errors=True)

    def test_active_follows_the_query(self):
        p = app.drives_payload("K:")
        self.assertEqual(p["active"], "K:")
        cur = {j["root"]: j["current"] for j in p["drives"]}
        self.assertTrue(cur["K:"])
        self.assertFalse(cur["C:"])

    def test_active_defaults_to_sources(self):
        p = app.drives_payload()
        self.assertEqual(p["active"], "C:")
        cur = {j["root"]: j["current"] for j in p["drives"]}
        self.assertTrue(cur["C:"])
        self.assertFalse(cur["K:"])

    def test_broken_archive_is_flagged(self):
        """只剩 drive.json、没有 tree.json 的残档要能被识别出来。"""
        os.remove(storage.data_store(self.d, "K:")["tree"])
        p = app.drives_payload("C:")
        k = [j for j in p["drives"] if j["root"] == "K:"][0]
        self.assertTrue(k.get("broken"))
        c = [j for j in p["drives"] if j["root"] == "C:"][0]
        self.assertFalse(c.get("broken"))


class DeleteHandlerTest(unittest.TestCase):
    """Handler 有没有把 view= 真的传进守卫 —— 只测纯函数抓不到这一段。

    这一段最容易漏：守卫函数本身写得再对，只要 handler 拿 SOURCES 当
    「正在看的盘」，「删掉正在看的那个盘」照样会被放行。
    """

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="diskaudit-del-")
        self.old_data, self.old_sources = app.DATA, app.SOURCES
        app.DATA = self.d
        for root in ("C:", "D:", "K:"):
            ds = storage.data_store(self.d, root)
            wr(ds["summary"], {"root": root, "drive": root, "at": "2026-10-24 05:00"})
            wr(ds["tree"], {"root": root, "tree": {"n": root, "c": []}})
            os.makedirs(ds["history"], exist_ok=True)
            wr(os.path.join(ds["history"], "index.json"), [])
        # 默认盘 = D（相当于 current.json 记的那个）
        app.SOURCES = {
            "tree": storage.data_store(self.d, "D:")["tree"],
            "links": "", "meta": "", "purpose": "", "report": "",
        }

    def tearDown(self):
        app.DATA, app.SOURCES = self.old_data, self.old_sources
        shutil.rmtree(self.d, ignore_errors=True)

    def handler(self, query):
        h = object.__new__(app.Handler)
        h.client_address = ("127.0.0.1", 50000)
        h.headers = email.message.Message()
        h.headers["Host"] = "127.0.0.1:8765"
        h.path = "/api/drives/delete?" + query
        h.got = {}
        h._json = lambda obj, code=200, head_only=False: h.got.update(obj=obj, code=code)
        h._err = (lambda code, msg, head_only=False, extra_headers=None:
                  h.got.update(err=msg, code=code))
        return h

    def test_viewed_drive_is_refused(self):
        h = self.handler("root=K&view=K")
        h._drives_delete(False)
        self.assertEqual(h.got["code"], 400)
        self.assertIn("正在查看", h.got["err"])
        self.assertTrue(os.path.exists(storage.data_store(self.d, "K:")["tree"]))

    def test_default_drive_is_refused(self):
        h = self.handler("root=D&view=K")
        h._drives_delete(False)
        self.assertEqual(h.got["code"], 400)
        self.assertIn("默认盘", h.got["err"])
        self.assertTrue(os.path.exists(storage.data_store(self.d, "D:")["tree"]))

    def test_other_drive_is_deleted_but_history_kept(self):
        h = self.handler("root=C&view=K")
        h._drives_delete(False)
        self.assertEqual(h.got["code"], 200)
        ds = storage.data_store(self.d, "C:")
        for name in ("tree.json", "links.json", "meta.json", "drive.json"):
            self.assertFalse(os.path.exists(os.path.join(ds["dir"], name)), name)
        self.assertTrue(os.path.isdir(ds["history"]))


class ActivePayloadTest(unittest.TestCase):
    """切过盘要记住：active.json 记界面停在哪，current.json 只记扫描过哪。

    两个指针合成一个，切盘后关掉页面再打开就会跳回上次扫描的盘 —— 用户
    看到的就是「切盘没记住」。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="diskaudit-act-")
        self.old_data, self.old_sources = app.DATA, app.SOURCES
        app.DATA = self.d
        for root in ("C:", "D:"):
            ds = storage.data_store(self.d, root)
            wr(ds["tree"], {"root": root, "tree": {"n": root, "c": []}})
            wr(ds["links"], {"items": []})
            wr(ds["meta"], {"drive": root})
            cs = storage.content_store(self.d, root)
            wr(cs["purpose"], {"exact": [], "names": [], "paths": []})
        # 默认盘（current.json 记的那个）= D
        app.SOURCES = {
            "tree": storage.data_store(self.d, "D:")["tree"],
            "links": storage.data_store(self.d, "D:")["links"],
            "meta": storage.data_store(self.d, "D:")["meta"],
            "purpose": storage.content_store(self.d, "D:")["purpose"],
            "report": "",
        }

    def tearDown(self):
        app.DATA, app.SOURCES = self.old_data, self.old_sources
        shutil.rmtree(self.d, ignore_errors=True)

    def handler(self, path):
        h = object.__new__(app.Handler)
        h.client_address = ("127.0.0.1", 50000)
        h.headers = email.message.Message()
        h.headers["Host"] = "127.0.0.1:8765"
        h.path = path
        h.got = {}
        h._json = lambda obj, code=200, head_only=False: h.got.update(obj=obj, code=code)
        h._err = (lambda code, msg, head_only=False, extra_headers=None:
                  h.got.update(err=msg, code=code))
        return h

    def payload(self, query=""):
        h = self.handler("/api/payload" + (("?" + query) if query else ""))
        h._api("payload", False)
        self.assertEqual(h.got.get("code"), 200, h.got.get("err"))
        return h.got["obj"]["tree"]["root"]

    def test_first_boot_uses_default_drive(self):
        self.assertEqual(self.payload(), "D:")
        self.assertFalse(os.path.exists(os.path.join(self.d, "active.json")))

    def test_switch_is_remembered(self):
        self.assertEqual(self.payload("root=C"), "C:")
        self.assertEqual(storage.read_active(self.d), "C:")
        self.assertEqual(self.payload(), "C:")      # 模拟重开页面：不带 ?root=

    def test_stale_active_falls_back(self):
        """活动盘留档没了：指针要失效，页面不能因此打不开。"""
        self.payload("root=C")
        os.remove(storage.data_store(self.d, "C:")["tree"])
        self.assertEqual(self.payload(), "D:")


class ActiveViewRootTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="diskaudit-vr-")
        self.old_data = app.DATA
        app.DATA = self.d

    def tearDown(self):
        app.DATA = self.old_data
        shutil.rmtree(self.d, ignore_errors=True)

    def test_query_wins(self):
        wr(storage.data_store(self.d, "C:")["tree"], {"root": "C:"})
        storage.write_active(self.d, "C:")
        self.assertEqual(app.view_root("K:"), "K:")
        self.assertEqual(app.view_root("k"), "K:")

    def test_falls_back_to_active_then_current(self):
        wr(storage.data_store(self.d, "C:")["tree"], {"root": "C:"})
        self.assertEqual(app.view_root(), "")        # 两个指针都没有
        storage.write_current(self.d, "C:")
        self.assertEqual(app.view_root(), "C:")      # 只有 current
        wr(storage.data_store(self.d, "K:")["tree"], {"root": "K:"})
        storage.write_active(self.d, "K:")
        self.assertEqual(app.view_root(), "K:")      # active 优先


class BuildHistoryDirTest(unittest.TestCase):
    """离线历史包必须读某个盘自己的目录，不能读 data/ 根。"""

    def test_points_at_drive_store(self):
        import build
        tmp = tempfile.mkdtemp(prefix="diskaudit-build-")
        try:
            self.assertEqual(build.history_dir(tmp, "K:"),
                             os.path.join(tmp, "drives", "K"))
            self.assertEqual(build.history_dir(tmp, "k"), build.history_dir(tmp, "K:"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BuildDrivesTest(unittest.TestCase):
    """单文件版收的是「每个有树的盘」，不是只收默认盘。"""

    def setUp(self):
        import build
        self.b = build
        self.d = tempfile.mkdtemp(prefix="diskaudit-bd-")
        self.c = tempfile.mkdtemp(prefix="diskaudit-bc-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
        shutil.rmtree(self.c, ignore_errors=True)

    def test_collects_every_drive_with_tree(self):
        for root, cap in (("C:", 100.0), ("K:", 200.0)):
            ds = storage.data_store(self.d, root)
            wr(ds["tree"], {"root": root, "tree": {"n": root, "c": []}})
            wr(ds["links"], {"items": []})
            wr(ds["meta"], {"drive": root, "capGB": cap, "scannedAt": "2026-10-24 05:00"})
            wr(ds["summary"], {"root": root, "at": "2026-10-24 05:00"})
            wr(storage.content_store(self.c, root)["purpose"],
               {"exact": [{"p": root + "\\x", "t": "%DRIVE% 盘"}], "names": [], "paths": []})
        # 只有摘要、没有 tree 的盘不算有留档
        wr(storage.data_store(self.d, "D:")["summary"], {"root": "D:", "at": "2026-10-25 05:00"})

        pack = self.b.collect_drives(self.d, self.c)
        self.assertEqual(pack["order"], ["C", "K"])
        self.assertEqual(sorted(pack["drives"]), ["C", "K"])
        self.assertEqual(pack["drives"]["C"]["capGB"], 100.0)
        # 占位符按各盘自己的盘符展开，不串味
        self.assertEqual(pack["drives"]["K"]["purpose"]["exact"][0]["t"], "K 盘")
        self.assertIn("hist", pack["drives"]["C"])


class BuildSelfContainedTest(unittest.TestCase):
    """单文件版必须自包含：外链一个都不许有，data: 与锚点不算。"""

    def setUp(self):
        import build
        self.b = build

    def test_clean_page_passes(self):
        html = ('<style>.a{color:red}</style>'
                '<img src="data:image/png;base64,AA"><a href="#x">锚点</a>'
                '<script>var t=\'<img src="\'+u+\'">\';</script>')
        self.assertEqual(self.b.audit_selfcontained(html), [])

    def test_external_resources_are_caught(self):
        bad = [
            '<script src="https://cdn.example.com/e.js"></script>',
            '<script src="assets/e.js"></script>',
            '<link rel="stylesheet" href="//fonts.example.com/x.css">',
            '<img src="http://a.example.com/x.png">',
            '<style>@import url("http://a.example.com/x.css");</style>',
            '<style>.a{background:url(../img/bg.png)}</style>',
            '<a href="https://example.com">外链</a>',
        ]
        for frag in bad:
            self.assertTrue(self.b.audit_selfcontained(frag), frag)


class HintLayerTest(unittest.TestCase):
    """悬停提示的层级必须压在弹窗遮罩之上。

    选盘 / 确认弹窗里的图标按钮（重扫、删留档）也带 data-hint；遮罩 .mmask
    有半透明底和 backdrop-filter，层级一倒，气泡就被变暗 + 模糊整个吃掉 ——
    只在弹窗打开时才看得出，靠肉眼回归很容易漏。这里直接读 CSS 钉住。
    """

    def setUp(self):
        self.html = io.open(os.path.join(HERE, "..", "web", "index.html"),
                            encoding="utf-8").read()

    def zindex(self, sel):
        m = re.search(r"\n%s\s*\{[^}]*?z-index\s*:\s*(\d+)" % re.escape(sel), self.html)
        self.assertIsNotNone(m, "web/index.html 里找不到 %s 的 z-index" % sel)
        return int(m.group(1))

    def test_hint_above_modal_mask(self):
        self.assertGreater(self.zindex(".hint"), self.zindex(".mmask"))

    def test_hint_above_rich_tip(self):
        """停靠卡片里按钮的提示要压在卡片之上（.tip 是树行的富卡片）。"""
        self.assertGreater(self.zindex(".hint"), self.zindex(".tip"))


class PortChoiceTest(unittest.TestCase):
    """端口占用时的决策：默认端口顺延、显式端口不让步、自己的实例不重复起。"""

    def free_except(self, *busy):
        return lambda p, h: p not in busy

    def test_free_port_is_used_as_is(self):
        d = app.choose_port(8765, "127.0.0.1", free=lambda p, h: True)
        self.assertEqual(("ok", 8765), (d["action"], d["port"]))

    def test_default_port_occupied_moves_on(self):
        d = app.choose_port(8765, "127.0.0.1",
                            free=self.free_except(8765, 8766),
                            probe=lambda p: None)
        self.assertEqual(("moved", 8767), (d["action"], d["port"]))
        self.assertEqual(8765, d["from"])

    def test_explicit_port_occupied_is_refused(self):
        d = app.choose_port(9000, "127.0.0.1", strict=True,
                            free=self.free_except(9000),
                            probe=lambda p: None)
        self.assertEqual("fail", d["action"])
        self.assertEqual(9000, d["port"])

    def test_own_running_instance_is_reused_not_duplicated(self):
        d = app.choose_port(8765, "127.0.0.1",
                            free=self.free_except(8765),
                            probe=lambda p: {"service": "DiskAudit", "dataDir": app.DATA},
                            data_dir=app.DATA)
        self.assertEqual("reuse", d["action"])

    def test_other_datadir_instance_moves_on(self):
        d = app.choose_port(8765, "127.0.0.1",
                            free=self.free_except(8765),
                            probe=lambda p: {"service": "DiskAudit",
                                             "dataDir": r"X:\somewhere\else"},
                            data_dir=app.DATA)
        self.assertEqual(("moved", 8766), (d["action"], d["port"]))

    def test_own_instance_on_fallback_port_is_reused(self):
        """默认端口被别的程序占、自己跑在 8766：再双击不该在 8767 起第三个进程。"""
        def probe(p):
            return {"service": "DiskAudit", "dataDir": app.DATA} if p == 8766 else None
        d = app.choose_port(8765, "127.0.0.1", tries=4,
                            free=lambda p, h: p >= 8767, probe=probe,
                            data_dir=app.DATA)
        self.assertEqual(("reuse", 8766), (d["action"], d["port"]))

    def test_all_ports_busy_fails(self):
        free = lambda p, h: False
        d = app.choose_port(8765, "127.0.0.1", tries=3,
                            free=free, probe=lambda p: None)
        self.assertEqual("fail", d["action"])
        self.assertIn("8767", d["why"])

    def test_first_free_port_respects_tries(self):
        self.assertEqual(3, app.first_free_port(1, "h", tries=5,
                                                free=lambda p, h: p == 3))
        self.assertIsNone(app.first_free_port(1, "h", tries=5,
                                              free=lambda p, h: False))


class BindRetryTest(unittest.TestCase):
    """准备阶段跑了很久之后，原本选好的端口可能已经被抢走。"""

    def test_skips_ports_taken_in_between(self):
        def factory(addr, handler):
            return "server@%d" % addr[1]
        srv, port = app.bind_server("h", 8765, tries=4, factory=factory,
                                    free=lambda p, h: p != 8765)
        self.assertEqual(("server@8766", 8766), (srv, port))

    def test_constructor_failure_falls_through(self):
        calls = []

        def factory(addr, handler):
            calls.append(addr[1])
            if addr[1] == 8765:
                raise OSError("bind race")
            return "ok"
        srv, port = app.bind_server("h", 8765, tries=3, factory=factory,
                                    free=lambda p, h: True)
        self.assertEqual(("ok", 8766), (srv, port))
        self.assertEqual([8765, 8766], calls)

    def test_all_busy_returns_none(self):
        srv, port = app.bind_server("h", 8765, tries=3,
                                    factory=lambda a, h: "never",
                                    free=lambda p, h: False)
        self.assertEqual((None, None), (srv, port))


if __name__ == "__main__":
    unittest.main(verbosity=2)
