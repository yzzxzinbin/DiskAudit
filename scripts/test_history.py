# -*- coding: utf-8 -*-
"""history.py 的单元测试：临时目录里合成快照，验证落档/清理/对比/趋势。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import history


def node(name, b=0.0, kids=None, f=0):
    d = {"n": name, "b": float(b), "c": kids or []}
    if f:
        d["f"] = 1
    return d


def tpay(root, kids, total=None):
    if total is None:
        total = sum(k["b"] for k in kids)
    return {"root": root, "rootLabel": root + "\\", "totalBytes": float(total),
            "fileCount": 0, "dirCount": 0, "skipped": [], "tree": {"n": root, "b": float(total), "c": kids}}


def meta(at, drive="C:"):
    return {"scannedAt": at, "scanDate": at[:10], "drive": drive, "capGB": 476.93,
            "usedGB": 400.0, "fileCount": 10, "dirCount": 5}


def links(drive="C:", items=None):
    return {"capGB": 476.93, "measured": "", "items": items or []}


def kids_a():
    """两张快照共有的骨架：Users(1000) + Games(800) + Logs(500)"""
    return [
        node("Users", 1000, [node("alice", 1000, [node("big.bin", 1000, f=1)])]),
        node("Games", 800),
        node("Logs", 500),
    ]


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="diskaudit-hist-")

    def tearDown(self):
        shutil.rmtree(self.data, ignore_errors=True)

    def put(self, at, kids, root="C:", total=None, keep=history.DEFAULT_KEEP):
        return history.record(self.data, tpay(root, kids, total), links(), meta(at), keep=keep)

    # ------------------------------------------------------------ 落档

    def test_record_list_and_overwrite_same_minute(self):
        self.put("2026-10-24 05:43", kids_a())
        self.put("2026-10-24 06:30", kids_a())
        self.assertEqual(history.snapshots(self.data),
                         ["20261024_0543", "20261024_0630"])
        # 同一分钟再记一次 = 覆盖，不产生新档
        self.put("2026-10-24 06:30", kids_a())
        self.assertEqual(history.snapshots(self.data),
                         ["20261024_0543", "20261024_0630"])
        sums = history.summaries(self.data)
        self.assertEqual(len(sums), 2)
        self.assertEqual(sums[0]["id"], "20261024_0543")
        self.assertEqual(sums[0]["total"], 2300.0)
        self.assertEqual(sums[0]["drive"], "C:")

    def test_index_survives_rebuild(self):
        self.put("2026-10-24 05:43", kids_a())
        self.put("2026-10-24 06:30", kids_a())
        os.remove(os.path.join(self.data, "history", "index.json"))
        sums = history.summaries(self.data)
        self.assertEqual([s["id"] for s in sums],
                         ["20261024_0543", "20261024_0630"])
        # 坏索引也一样重建
        with open(os.path.join(self.data, "history", "index.json"), "w") as f:
            f.write("{broken")
        sums = history.summaries(self.data)
        self.assertEqual(len(sums), 2)

    def test_prune_keeps_newest(self):
        for h, m in (("01", "00"), ("02", "00"), ("03", "00"), ("04", "00"), ("05", "00")):
            self.put("2026-10-24 %s:%s" % (h, m), kids_a(), keep=3)
        self.assertEqual(history.snapshots(self.data),
                         ["20261024_0300", "20261024_0400", "20261024_0500"])
        # 索引同步裁掉
        self.assertEqual([s["id"] for s in history.summaries(self.data)],
                         history.snapshots(self.data))

    def test_forget_one_removes_single_snapshot(self):
        self.put("2026-10-24 05:43", kids_a(), root="C:")
        self.put("2026-10-24 06:30", kids_a(), root="C:")
        self.put("2026-10-24 07:30", kids_a(), root="C:")
        history.forget_one(self.data, "20261024_0630")
        self.assertEqual(history.snapshots(self.data),
                         ["20261024_0543", "20261024_0730"])
        # 索引同步重建
        self.assertEqual([s["id"] for s in history.summaries(self.data)],
                         ["20261024_0543", "20261024_0730"])
        # 删除 = 移入 _trash/ 回收站（可找回），不是真删
        trash = os.path.join(self.data, "history", "_trash")
        self.assertTrue(os.path.exists(
            os.path.join(trash, "snap-20261024_0630.json")))
        self.assertEqual(os.listdir(trash), ["snap-20261024_0630.json"])
        # 再删同一张 / 删不存在的 id，都如实报错
        with self.assertRaises(history.DiffError):
            history.forget_one(self.data, "20261024_0630")
        with self.assertRaises(history.DiffError):
            history.forget_one(self.data, "99999999_9999")

    def test_record_requires_root(self):
        with self.assertRaises(history.DiffError):
            history.record(self.data, {"root": ""}, links(), meta("2026-10-24 05:43"))

    def test_forget_removes_root_and_rebuilds_index(self):
        self.put("2026-10-24 05:43", kids_a(), root="C:")
        self.put("2026-10-24 06:30", kids_a(), root="C:")
        self.put("2026-10-24 07:30", kids_a(), root="C:\\Program Files\\SomeApp")
        self.put("2026-10-24 08:30", kids_a(), root="E:")
        # 规范化匹配："C:\Program Files\SomeApp" 与不带反斜杠的写法是同一个根
        self.assertEqual(history.forget(self.data, "c:/program files/someapp"),
                         ["20261024_0730"])
        self.assertEqual(history.forget(self.data, "C:\\"),
                         ["20261024_0543", "20261024_0630"])
        self.assertEqual(history.snapshots(self.data), ["20261024_0830"])
        # C 盘那两张也进了回收站，可找回
        trash = os.path.join(self.data, "history", "_trash")
        self.assertEqual(sorted(os.listdir(trash)),
                         ["snap-20261024_0543.json", "snap-20261024_0630.json",
                          "snap-20261024_0730.json"])
        # 索引跟着重建，只剩没删的
        self.assertEqual([s["id"] for s in history.summaries(self.data)],
                         ["20261024_0830"])
        # 不匹配的根原样返回空；缺 root 拒绝
        self.assertEqual(history.forget(self.data, "D:"), [])
        with self.assertRaises(history.DiffError):
            history.forget(self.data, "")

    def test_sid_is_validated(self):
        with self.assertRaises(history.DiffError):
            history.snap_path(self.data, "..\\evil")
        with self.assertRaises(history.DiffError):
            history.snap_path(self.data, "20261024_0543 ")
        self.assertIsNone(history.load(self.data, "20261024_0543"))

    def test_empty_history(self):
        self.assertEqual(history.snapshots(self.data), [])
        self.assertEqual(history.summaries(self.data), [])

    # ------------------------------------------------------------ 对比

    def test_diff_added_removed_changed(self):
        self.put("2026-10-24 05:43", kids_a())
        new_kids = [
            node("Users", 1500, [node("alice", 1500, [node("big.bin", 1500, f=1)])]),
            node("Logs", 500),                                    # 不变
            node("NewStuff", 600),                                # 只在新快照
        ]                                                          # Games 消失
        self.put("2026-10-24 06:30", new_kids, total=2600)

        d = history.diff_ids(self.data, "20261024_0543", "20261024_0630")
        self.assertEqual(d["summary"]["added"], 1)
        self.assertEqual(d["summary"]["removed"], 1)
        self.assertEqual(d["summary"]["changed"], 3)   # Users、alice、big.bin
        self.assertEqual(d["summary"]["delta"], 2600.0 - 2300.0)

        self.assertEqual(d["added"][0]["p"], "C:\\NewStuff")
        self.assertEqual(d["added"][0]["b"], 600.0)
        self.assertEqual(d["added"][0]["fs"], "20261024_0630")

        self.assertEqual(d["removed"][0]["p"], "C:\\Games")
        self.assertEqual(d["removed"][0]["b"], 800.0)

        ups = {r["p"]: r for r in d["changed"]}
        self.assertEqual(ups["C:\\Users"]["ob"], 1000.0)
        self.assertEqual(ups["C:\\Users"]["nb"], 1500.0)
        self.assertEqual(ups["C:\\Users"]["delta"], 500.0)
        self.assertEqual(ups["C:\\Users"]["pct"], 50.0)
        self.assertEqual(ups["C:\\Users\\alice\\big.bin"]["f"], 1)
        # 排序按 |delta| 降序
        deltas = [abs(r["delta"]) for r in d["changed"]]
        self.assertEqual(deltas, sorted(deltas, reverse=True))

    def test_diff_root_mismatch(self):
        self.put("2026-10-24 05:43", kids_a(), root="C:")
        self.put("2026-10-24 06:30", kids_a(), root="D:")
        with self.assertRaises(history.DiffError):
            history.diff_ids(self.data, "20261024_0543", "20261024_0630")

    def test_diff_missing_snapshot(self):
        self.put("2026-10-24 05:43", kids_a())
        with self.assertRaises(history.DiffError):
            history.diff_ids(self.data, "20261024_0543", "20261024_9999")

    # ------------------------------------------------------------ 首见与趋势

    def test_first_seen(self):
        self.put("2026-10-24 05:00", kids_a())
        self.put("2026-10-24 06:00", kids_a() + [node("NewStuff", 600)])
        self.put("2026-10-24 07:00", kids_a() + [node("NewStuff", 700)])
        fs = history.first_seen(self.data, ["C:\\NewStuff", "C:\\Users", "C:\\Nope"])
        self.assertEqual(fs["C:\\NewStuff"], "20261024_0600")
        self.assertEqual(fs["C:\\Users"], "20261024_0500")
        self.assertNotIn("C:\\Nope", fs)

    def test_trend_includes_missing_as_null(self):
        self.put("2026-10-24 05:00", kids_a())
        self.put("2026-10-24 06:00", kids_a() + [node("NewStuff", 600)])
        self.put("2026-10-24 07:00", kids_a() + [node("NewStuff", 700)])
        t = history.trend(self.data, "C:\\NewStuff")
        self.assertEqual([p["b"] for p in t["points"]], [None, 600.0, 700.0])
        t2 = history.trend(self.data, "C:\\Users")
        self.assertEqual([p["b"] for p in t2["points"]], [1000.0, 1000.0, 1000.0])

    def test_trend_filters_other_roots(self):
        self.put("2026-10-24 05:00", kids_a(), root="C:")
        self.put("2026-10-24 06:00", [node("Elsewhere", 123)], root="D:")
        self.put("2026-10-24 07:00", kids_a(), root="C:")
        # 缺省 root = 最新快照的 root（C:），D: 那张不混入
        t = history.trend(self.data, "C:\\Users")
        self.assertEqual(len(t["points"]), 2)
        self.assertEqual([p["id"] for p in t["points"]],
                         ["20261024_0500", "20261024_0700"])

    # ------------------------------------------------------------ 趋势矩阵

    def write_tree_json(self, kids, root="C:", total=None):
        with open(os.path.join(self.data, "tree.json"), "w", encoding="utf-8") as f:
            json.dump(tpay(root, kids, total), f, ensure_ascii=False)

    def test_trend_matrix_sparse_and_cache(self):
        self.write_tree_json(kids_a() + [node("Ghost", 0)])
        self.put("2026-10-24 05:00", kids_a())
        new_kids = [
            node("Users", 1100, [node("alice", 1100, [node("big.bin", 1100, f=1)])]),
            node("Games", 800),
            node("Logs", 500),
        ]
        self.put("2026-10-24 06:00", new_kids, total=2400)
        self.write_tree_json(new_kids + [node("Ghost", 0)])

        m = history.trend_matrix(self.data)
        self.assertEqual(m["root"], "C:")
        self.assertEqual(m["ids"], ["20261024_0500", "20261024_0600"])
        # 变了的留下（含文件），恒定的不占字节
        self.assertEqual(m["paths"]["C:\\Users"], (1000.0, 1100.0))
        self.assertEqual(m["paths"]["C:\\Users\\alice\\big.bin"], (1000.0, 1100.0))
        self.assertNotIn("C:\\Games", m["paths"])
        self.assertNotIn("C:\\Logs", m["paths"])
        self.assertNotIn("C:\\Ghost", m["paths"])          # 全程缺席

        # 进程内缓存：调用方怎么改都污染不了缓存
        m["paths"]["C:\\Users"] = "corrupted"
        m["paths"]["junk"] = (1.0,)
        m2 = history.trend_matrix(self.data)
        self.assertEqual(m2["paths"]["C:\\Users"], (1000.0, 1100.0))
        self.assertNotIn("junk", m2["paths"])

        # 新快照落档 → 缓存失效 → 序列变长
        self.put("2026-10-24 07:00", new_kids, total=2400)
        m3 = history.trend_matrix(self.data)
        self.assertEqual(len(m3["paths"]["C:\\Users"]), 3)

    def test_trend_matrix_skips_single_snapshot(self):
        self.write_tree_json(kids_a())
        self.put("2026-10-24 05:00", kids_a())
        self.assertEqual(history.trend_matrix(self.data)["paths"], {})

    def test_trend_matrix_limit_keeps_largest(self):
        kids = kids_a()
        self.write_tree_json(kids)
        self.put("2026-10-24 05:00", kids)
        kids2 = [
            node("Users", 1400, [node("alice", 1400, [node("big.bin", 1400, f=1)])]),
            node("Games", 500),
            node("Logs", 400),
        ]
        self.put("2026-10-24 06:00", kids2, total=2300)
        self.write_tree_json(kids2)
        m = history.trend_matrix(self.data, limit=1)
        self.assertEqual(list(m["paths"]), ["C:\\Users"])   # 最新体积最大者胜

    # ------------------------------------------------------------ 深层对比

    def put_deep(self, at, kids, deep, total=None):
        return history.record(self.data, tpay("C:", kids, total), links(), meta(at),
                              deep=deep)

    def test_diff_cascade_marking(self):
        # 一个深层文件的涨跌会级联到每个 ≥门槛 的祖先目录：祖先行标 cascade
        # （标出解释它的子行），文件行不标；自有变化超过阈值(4KB/2%)的不归并
        b = "C:"
        old_kids = [node("System", 1000, [node("Logs", 1000, [node("qq.tlg", 1000, f=1)])])]
        new_kids = [node("System", 1512, [node("Logs", 1512, [node("qq.tlg", 1512, f=1)])])]
        self.put("2026-10-24 05:00", old_kids)
        self.put("2026-10-24 06:00", new_kids, total=1512)
        d = history.diff_ids(self.data, "20261024_0500", "20261024_0600")
        rows = {r["p"]: r for r in d["changed"]}
        self.assertEqual(rows[b + "\\System"].get("cascade"), b + "\\System\\Logs")
        self.assertEqual(rows[b + "\\System\\Logs"].get("cascade"),
                         b + "\\System\\Logs\\qq.tlg")
        self.assertNotIn("cascade", rows[b + "\\System\\Logs\\qq.tlg"])

        # A 比 B 多涨 200KB（远超 4KB/2% 噪声阈值）→ A 有自己的故事，不归并
        old2 = [node("A", 1000000, [node("B", 1000000, [node("f.bin", 1000000, f=1)])])]
        new2 = [node("A", 1712000, [node("B", 1512000, [node("f.bin", 1512000, f=1)])])]
        self.put("2026-10-24 07:00", old2)
        self.put("2026-10-24 08:00", new2, total=1712000)
        d2 = history.diff_ids(self.data, "20261024_0700", "20261024_0800")
        rows2 = {r["p"]: r for r in d2["changed"]}
        self.assertNotIn("cascade", rows2[b + "\\A"])
        self.assertEqual(rows2[b + "\\A\\B"].get("cascade"), b + "\\A\\B\\f.bin")

    def test_deep_diff_descends_to_leaves(self):
        b = "C:"
        old_kids = [node("Users", 1000, [node("alice", 1000, [node("big.bin", 1000, f=1)])])]
        new_kids = [node("Users", 1600, [node("alice", 1500,
                    [node("big.bin", 600, f=1), node("huge.bin", 900, f=1)])])]
        self.write_tree_json(new_kids)
        self.put_deep("2026-10-24 05:00", old_kids,
                      {"d": {b + "\\Users": 1000.0, b + "\\Users\\alice": 1000.0},
                       "f": {b + "\\Users\\alice\\big.bin": 1000.0}}, total=1000)
        self.put_deep("2026-10-24 06:00", new_kids,
                      {"d": {b + "\\Users": 1600.0, b + "\\Users\\alice": 1500.0},
                       "f": {b + "\\Users\\alice\\big.bin": 600.0,
                             b + "\\Users\\alice\\huge.bin": 900.0}}, total=1600)

        d = history.deep_diff_ids(self.data, "20261024_0500", "20261024_0600",
                                  b + "\\Users")
        self.assertEqual(d["delta"], 600.0)          # Users 自身 1000 → 1600
        # 子项只解释 500，剩下 100 在深索引之外
        self.assertEqual(d["residual"], 100.0)
        rows = {r["p"]: r for r in d["rows"]}
        usr = rows[b + "\\Users\\alice"]
        self.assertEqual(usr["delta"], 500.0)
        kidp = {k["p"]: k for k in usr["kids"]}
        self.assertEqual(kidp[b + "\\Users\\alice\\big.bin"]["delta"], -400.0)
        self.assertEqual(kidp[b + "\\Users\\alice\\big.bin"]["f"], 1)
        self.assertIsNone(kidp[b + "\\Users\\alice\\huge.bin"]["ob"])
        self.assertEqual(kidp[b + "\\Users\\alice\\huge.bin"]["nb"], 900.0)
        self.assertEqual(usr["residual"], 0)

    def test_deep_diff_leaf_dir_has_no_kids_key(self):
        b = "C:"
        deep = {"d": {b + "\\Games": 800.0}, "f": {}}
        self.put_deep("2026-10-24 05:00", [node("Games", 800)], deep, total=800)
        deep2 = {"d": {b + "\\Games": 300.0}, "f": {}}
        self.put_deep("2026-10-24 06:00", [node("Games", 300)], deep2, total=300)
        d = history.deep_diff_ids(self.data, "20261024_0500", "20261024_0600",
                                  b + "\\Games")
        # Games 在深索引里没有收录任何子项 → 下钻结果为空，UI 显示
        # 「深索引里没有更细的变化」；顶层 delta 仍是它自己的变化量
        self.assertEqual(d["rows"], [])
        self.assertEqual(d["delta"], -500.0)
        self.assertEqual(d["residual"], -500.0)

    def test_deep_diff_requires_deep_index(self):
        self.put("2026-10-24 05:00", kids_a())     # 无深索引的旧档
        self.put("2026-10-24 06:00", kids_a())
        with self.assertRaises(history.DiffError):
            history.deep_diff_ids(self.data, "20261024_0543", "20261024_0630", "C:\\Users")

    def test_deep_diff_missing_root_is_empty(self):
        b = "C:"
        deep = {"d": {b + "\\Users": 1000.0}, "f": {}}
        self.put_deep("2026-10-24 05:00", [node("Users", 1000)], deep, total=1000)
        self.put_deep("2026-10-24 06:00", [node("Users", 1000)],
                      {"d": {b + "\\Users": 1000.0}, "f": {}}, total=1000)
        d = history.deep_diff_ids(self.data, "20261024_0500", "20261024_0600",
                                  b + "\\Nope")
        self.assertEqual(d["rows"], [])
        self.assertEqual(d["delta"], 0.0)

    # ------------------------------------------------------------ 离线包

    def test_offline_bundle(self):
        self.write_tree_json(kids_a() + [node("NewStuff", 700)])
        self.put("2026-10-24 05:00", kids_a())
        self.put("2026-10-24 06:00", kids_a() + [node("NewStuff", 600)])
        self.put("2026-10-24 07:00", kids_a() + [node("NewStuff", 700)])
        b = history.offline_bundle(self.data)
        self.assertEqual(len(b["snapshots"]), 3)
        self.assertEqual(b["ids"], ["20261024_0500", "20261024_0600", "20261024_0700"])
        self.assertEqual(len(b["totals"]), 3)
        self.assertEqual(b["diff"]["to"]["id"], "20261024_0700")
        self.assertIn("C:\\NewStuff", b["movers"])
        self.assertEqual(b["movers"]["C:\\NewStuff"], [None, 600.0, 700.0])
        # 稀疏矩阵：变的在、恒定不在，且与 ids 对齐
        self.assertEqual(b["matrix"]["C:\\NewStuff"], (None, 600.0, 700.0))
        self.assertNotIn("C:\\Games", b["matrix"])

    def test_offline_bundle_empty(self):
        b = history.offline_bundle(self.data)
        self.assertEqual(b, {"snapshots": [], "ids": [], "diff": None, "totals": [],
                             "movers": {}, "matrix": {}})

    def test_all_summaries_merges_drives(self):
        """各盘 history 合在一起给前端，条目自带 root 才分得清归属。

        只发当前盘的话，切到一个刚扫过的盘会只剩一两张 —— 看着像历史被删了。
        """
        base = os.path.join(self.data, "drives")
        for key, drv, at in (("CD", "C:", "2026-10-24 05:00"),
                             ("DD", "D:", "2026-10-24 06:00"),
                             ("KD", "K:", "2026-10-23 09:00")):
            sub = os.path.join(base, key)
            os.makedirs(sub, exist_ok=True)
            history.record(sub, tpay(drv, kids_a()), links(drive=drv), meta(at, drv))

        alls = history.all_summaries(self.data)
        self.assertEqual([e["id"] for e in alls],
                         ["20261023_0900", "20261024_0500", "20261024_0600"])
        self.assertEqual([e["root"] for e in alls], ["K:", "C:", "D:"])
        # 单盘视图仍是本盘那一张，不会把别人的算进来
        self.assertEqual(history.summaries(os.path.join(base, "CD")),
                         [e for e in alls if e["root"] == "C:"])

    def test_all_summaries_no_drives(self):
        """还没分盘留档时返回空表，不抛错。"""
        self.assertEqual(history.all_summaries(self.data), [])

    # ------------------------------------------------------ 分盘后新踩过的坑

    def test_offline_bundle_on_drive_store(self):
        """离线包要吃「某个盘自己的目录」，不是 data/ 根。

        快照在 data/drives/<盘>/history/ 下；传 data/ 根只会拿到空包，而且
        不报错 —— 单文件版的历史面板就这么空着。
        """
        base = os.path.join(self.data, "drives", "CD")
        os.makedirs(base, exist_ok=True)
        history.record(base, tpay("C:", kids_a()), links(), meta("2026-10-24 05:00"))
        history.record(base, tpay("C:", kids_a() + [node("New", 100)]), links(),
                       meta("2026-10-24 06:00"))

        one = history.offline_bundle(base)
        self.assertEqual(len(one["snapshots"]), 2)
        self.assertEqual(one["ids"], ["20261024_0500", "20261024_0600"])
        self.assertTrue(one["diff"])
        # 快照只在 data/drives/<盘>/history/ 下：传错层就会静默得到空包
        self.assertEqual(history.offline_bundle(self.data)["snapshots"], [])

    def test_trend_root_with_trailing_backslash(self):
        """前端 DATA.root 会补尾部反斜杠，trend 不能按字面比 root。"""
        self.put("2026-10-24 05:00", kids_a())
        self.put("2026-10-24 06:00", kids_a())

        for root in ("C:", "C:\\", "c:"):
            t = history.trend(self.data, "C:\\Users", root=root)
            self.assertEqual(len(t["points"]), 2, root)
            self.assertTrue(all(p["b"] is not None for p in t["points"]), root)

    def test_trend_still_rejects_other_drive(self):
        """归一化不能把别的盘也放进来。"""
        self.put("2026-10-24 05:00", kids_a())
        t = history.trend(self.data, "C:\\Users", root="D:")
        self.assertEqual(t["points"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
