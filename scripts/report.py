# -*- coding: utf-8 -*-
"""生成 content/report.json 的骨架 —— 报表「语义层」的入口。

分工：
    数据层  data/*.json        体积、占比、条数、盘容量 —— 程序算得出来
    语义层  content/report.json 描述、说明、评级、执行步骤 —— 程序算不出来

本模块只做前者：把扫描结果里值得说的目录挑出来（按体积、按用途库评级），
把 path / 数字 / 结构摆好，文字位置留空或先填用途库里的一句话。
剩下的交给用户或 AI 补 —— 补完页面就变详细，不补也能跑（页面自动回落）。

对外接口：
    seed(data, content)        生成/更新骨架（已有文字按路径复用）
    fill_dirs(data, content)   补齐空着的 dsc、把截断的说明换回全文，不动结构
"""
import io
import json
import os
import re
import sys

import pathvars
import purpose as pur

BS = "\\"
MB = 1024.0 ** 2
GB = MB * 1024.0

# 挑候选的门槛（字节）
MIN_CLEAN = 20 * MB
MIN_REVIEW = 500 * MB
MIN_FORBID = 100 * MB
MIN_MIGRATE = 300 * MB

TOP_DIRS = 6
TOP_PARTS = 8


def rd(p):
    if not os.path.exists(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def wr(p, obj):
    tmp = p + ".new"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def walk(node, path, out, depth=0):
    for k in node.get("c", []):
        p = path + ("" if path.endswith(BS) else BS) + k.get("n", "")
        out.append({
            "p": p, "n": k.get("n", ""), "b": float(k.get("b", 0)),
            "file": k.get("f") == 1, "d": depth + 1, "node": k,
        })
        walk(k, p, out, depth + 1)


def resolve(path, name, exact, names, paths):
    """和页面 purposeOf() 同序：精确 → 名字规则 → 路径规则 → 祖先继承。"""
    self = path
    if len(self) == 2 and self[1] == ":":
        self += BS
    if self in exact:
        return exact[self]
    for rx, o in names:
        if rx.search(name):
            return o
    for rx, o in paths:
        if rx.search(self):
            return o
    p = self
    for _ in range(40):
        j = p.rfind(BS)
        if j <= 0:
            break
        q = p[:j]
        if len(q) == 2 and q[1] == ":":
            q += BS
        p = q
        if p in exact:
            return exact[p]
    return None


def short(text, n=40):
    """用途库里的说明可能很长，骨架里先截一句，人工再润色。"""
    t = (text or "").strip()
    if len(t) <= n:
        return t
    m = re.split(r"[。；;]", t)
    return m[0] + "。" if m and len(m[0]) <= n else t[:n] + "…"


def _untruncate(old, full):
    """说明被截成「前缀 + 省略号」时，能确认只是前缀就换回完整用途。"""
    old = (old or "").strip()
    full = (full or "").strip()
    if not old:
        return full
    if full and re.search(r"(?:…|\.{3,})\s*$", old):
        stem = re.sub(r"(?:…|\.{3,})\s*$", "", old).strip()
        if stem and full.startswith(stem):
            return full
    return old


def _seed_steps(clean, limit=4):
    """没写过执行顺序时，拿 A 类清单的前几项起个草。

    每步都挂 valuePaths，页面按实测体积现算 —— 这样换台机器不需要改一个数字。
    """
    out = []
    for it in clean[:limit]:
        paths = it.get("paths") or ([it["path"]] if it.get("path") else [])
        if not paths:
            continue
        out.append({
            "text": it.get("title") or paths[0],
            "em": it.get("how") or "",
            "valuePaths": paths,
            "prefix": "约 ",
        })
    return out


def _seed_total(steps):
    """累计行的默认值：取前 N 步的现算合计，没有可算的步就不给。"""
    n = len([s for s in steps if s.get("valuePaths")])
    if not n:
        return {}
    return {"label": "前 %d 步累计" % n, "upto": n}


def seed(data, content, out=None, force=False, quiet=False):
    """按扫描结果生成/更新 content/report.json 的骨架。

    返回 (path, todo)：path 是写出的文件（没写返回 None），todo 是待补文字清单。
    文字按路径复用 —— 人工润色过的内容不会被本次重排冲掉。
    """
    def say(*a, **k):
        if not quiet:
            print(*a, **k)

    tree_p = os.path.join(data, "tree.json")
    links_p = os.path.join(data, "links.json")
    purp_p = os.path.join(content, "purpose.json")
    for p in (tree_p, links_p, purp_p):
        if not os.path.exists(p):
            say("[fatal] 缺少 %s" % p)
            say("        跑一次 start.bat，启动时会自动补齐并生成 ticket")
            return None, []

    T = rd(tree_p)
    LK = rd(links_p) or {}
    home = pathvars.home_of(T)
    focus = pathvars.focus_of(T)
    ctx = pathvars.context(home, T.get("root", "C:"), pathvars.sysdrive_of(T))
    P = pathvars.expand(rd(purp_p), ctx)
    _P, exact, names, paths = pur.load_index(P)

    nodes = []
    walk(T["tree"], T["root"].rstrip(BS) or T["root"], nodes)
    for n in nodes:
        n["pu"] = resolve(n["p"], n["n"], exact, names, paths)

    def tok(s):
        """骨架里的路径就是真实绝对路径，和用途库、ticket 同一套口径。

        report.json 按盘各存一份，换盘不会互相覆盖，也就没有「一份文案适配
        多个盘」的需求 —— 占位符在这里只会让路径看不出属于哪个盘。
        """
        return s

    old = None if force else rd(os.path.join(content, "report.json"))
    old = old or {}

    # ---------- 索引旧文字（按路径复用） ----------
    def idx(key, items, fields):
        d = {}
        for it in items or []:
            k = it.get(key)
            if k:
                d[k.lower()] = dict((f, it.get(f)) for f in fields if it.get(f) is not None)
        return d

    old_dirs = idx("path", (old.get("dirs") or {}).get("items"), ["dsc", "note", "tag", "kind"])
    # A 类里有一项是「多个目录合并成一行」（*-updater），没有单一路径，按标题复用
    old_clean = {}
    for it in old.get("clean") or []:
        k = (it.get("path") or it.get("title") or "").lower()
        if k:
            old_clean[k] = dict((f, it.get(f)) for f in ("title", "how", "tag", "kind", "on")
                                if it.get(f) is not None)
    old_review = idx("path", old.get("review"), ["dsc", "tag", "kind"])
    old_forbid = idx("path", old.get("forbid"), ["why"])
    old_migrate = idx("path", old.get("migrate"), ["note"])

    # ---------- 六大占用目录 ----------
    # 根目录下的 Users 对读者没有信息量（真正占地方的是账户目录本身），
    # 所以遇到「细化根的父目录」时，直接把细化根顶上来。
    kids = sorted(T["tree"].get("c", []), key=lambda k: -k.get("b", 0))
    dir_items = []
    for k in kids[:TOP_DIRS]:
        p = T["root"].rstrip(BS) + BS + k.get("n", "")
        if p.rstrip(BS).lower() == os.path.dirname(focus).lower() and focus:
            p = focus
            k = next((n["node"] for n in nodes if n["p"].lower() == focus.lower()), k)
        pu = resolve(p, k.get("n", ""), exact, names, paths)
        parts = []
        for c in sorted(k.get("c", []), key=lambda x: -x.get("b", 0))[:TOP_PARTS]:
            parts.append({"name": c.get("n", ""), "path": tok(p + BS + c.get("n", ""))})
        od = old_dirs.get(tok(p).lower()) or {}
        it = {
            "path": tok(p),
            "dsc": _untruncate(od.get("dsc"), pu["t"] if pu else ""),
            "tag": od.get("tag") or "",
            "kind": od.get("kind") or (pu or {}).get("g") or "info",
            "parts": parts,
        }
        note = od.get("note")
        if note:
            it["note"] = note
        dir_items.append(it)

    # ---------- 重点目录明细：本次细化根 + 它下面最大的一个目录 ----------
    focus_node = None
    for n in nodes:
        if n["p"].lower() == focus.lower():
            focus_node = n
            break
    details = []
    if focus_node:
        sub = sorted([c for c in focus_node["node"].get("c", []) if c.get("f") != 1],
                     key=lambda x: -x.get("b", 0))
        targets = [focus_node]
        if sub:
            # 往下钻一层：如果某个子目录占了父目录六成以上（AppData → Local 这种），
            # 那说明真正值得看的是它，而不是父目录本身。
            cur = sub[0]
            cur_p = focus_node["p"] + BS + cur.get("n", "")
            for _ in range(2):
                kids2 = sorted([c for c in cur.get("c", []) if c.get("f") != 1],
                               key=lambda x: -x.get("b", 0))
                if not kids2 or not cur.get("b"):
                    break
                if kids2[0].get("b", 0) / float(cur["b"]) < 0.6:
                    break
                cur = kids2[0]
                cur_p = cur_p + BS + cur.get("n", "")
            targets.append({"p": cur_p, "n": cur.get("n", ""), "node": cur})
        old_details = {}
        for d in (old.get("dirs") or {}).get("details") or []:
            if d.get("path"):
                old_details[d["path"].lower()] = d
        for tgt in targets:
            key = tok(tgt["p"]).lower()
            od = old_details.get(key) or {}
            rows = []
            cands = [c for c in tgt["node"].get("c", [])
                     if not c.get("n", "").lower().endswith("-updater")]
            for c in sorted(cands, key=lambda x: -x.get("b", 0))[:9]:
                rp = tok(tgt["p"] + BS + c.get("n", ""))
                rows.append({"path": rp, "dsc": ""})
            # 旧文字按路径回填
            old_rows = dict((r.get("path", "").lower(), r.get("dsc")) for r in (od.get("rows") or []))
            for r in rows:
                if old_rows.get(r["path"].lower()):
                    r["dsc"] = old_rows[r["path"].lower()]
            details.append({
                "path": tok(tgt["p"]),
                "title": od.get("title") or tok(tgt["p"]),
                "rows": rows,
            })

    # ---------- 可清理：用途库判 ok 的节点，按体积取前 20 ----------
    clean, taken = [], []
    up_dirs = [n for n in nodes if n["n"].lower().endswith("-updater") and n["d"] >= 3]
    up_sum = sum(n["b"] for n in up_dirs)
    if len(up_dirs) >= 3:
        title = "桌面应用更新包残留（%d 个目录）" % len(up_dirs)
        od = old_clean.get(title.lower()) or {}
        clean.append({
            "paths": [tok(n["p"]) for n in sorted(up_dirs, key=lambda x: -x["b"])],
            "title": od.get("title") or title,
            "how": od.get("how") or "AppData\\Local\\*-updater：安装完就没用了",
            "tag": od.get("tag") or "安全",
            "kind": od.get("kind") or "ok",
            "on": od.get("on", True),
            "_b": up_sum,
        })
        taken.extend(n["p"] for n in up_dirs)

    ok_nodes = [n for n in nodes if n["pu"] and n["pu"].get("g") == "ok" and n["b"] >= MIN_CLEAN]
    ok_nodes.sort(key=lambda x: -x["b"])
    for n in ok_nodes:
        if len(clean) >= 12:
            break
        np_ = n["p"].lower()
        if any(np_ == t.lower() or np_.startswith(t.lower() + BS) for t in taken):
            continue
        taken.append(n["p"])
        key = tok(n["p"])
        od = old_clean.get(key.lower()) or {}
        clean.append({
            "path": key,
            "title": od.get("title") or n["n"],
            "how": od.get("how") or "",
            "tag": od.get("tag") or "可清理",
            "kind": od.get("kind") or "ok",
            "on": od.get("on", True),
            "_b": n["b"],
        })
    # 语义层里手工加的「扫不到但要保留」条目（回收站、Windows 更新缓存…）：
    # 这些路径普通权限永远扫不到，数据里查不到，重生成时必须原样带回来，
    # 否则每次跑一遍骨架就被洗掉。判定依据是 keep / size 字段。
    have = set()
    for c in clean:
        have.add((c.get("path") or c.get("title") or "").lower())
    for it in old.get("clean") or []:
        if not (it.get("keep") or it.get("size")):
            continue
        k = (it.get("path") or it.get("title") or "").lower()
        if k and k not in have:
            it2 = dict(it)
            it2.pop("_b", None)
            clean.append(it2)
            have.add(k)

    clean.sort(key=lambda x: -x.get("_b", 0))

    # ---------- 禁区 / 需评估 / 建议迁移 ----------
    def pick(grade, minimum, limit, inside_home=None):
        out = []
        for n in sorted(nodes, key=lambda x: -x["b"]):
            if n["b"] < minimum or not n["pu"] or n["pu"].get("g") != grade:
                continue
            if inside_home is True and not n["p"].lower().startswith(focus.lower()):
                continue
            if inside_home is False and n["p"].lower().startswith(focus.lower()):
                continue
            low = n["p"].lower()
            # 已经被选中的祖先目录覆盖了就不重复列（Windows Kits 与 Windows Kits\10 这种）
            if any(low == o["p"].lower() or low.startswith(o["p"].lower() + BS) for o in out):
                continue
            out.append(n)
            if len(out) >= limit:
                break
        return out

    review = []
    for n in pick("warn", MIN_REVIEW, 6, inside_home=False):
        key = tok(n["p"])
        od = old_review.get(key.lower()) or {}
        review.append({
            "path": key,
            "dsc": od.get("dsc") or short(n["pu"]["t"], 30),
            "kind": od.get("kind") or "warn",
            "tag": od.get("tag") or "按需",
        })

    forbid = []
    for n in pick("no", MIN_FORBID, 10):
        key = tok(n["p"])
        od = old_forbid.get(key.lower()) or {}
        forbid.append({"path": key, "why": od.get("why") or short(n["pu"]["t"], 34)})

    linked = set(x.get("l", "").lower() for x in (LK.get("items") or []))
    migrate = []
    for n in pick("warn", MIN_MIGRATE, 7, inside_home=True):
        if n["p"].lower() in linked:
            continue
        key = tok(n["p"])
        od = old_migrate.get(key.lower()) or {}
        migrate.append({"path": key, "note": od.get("note") or short(n["pu"]["t"], 24)})

    # ---------- 执行顺序：没写过就按 A 类清单起个草 ----------
    # 每一步挂 valuePaths，页面上就会按实测体积现算，清理完数字自己会缩。
    steps = old.get("steps") or _seed_steps(clean)

    # ---------- 组装 ----------
    rep = {
        "version": 1,
        "drive": T.get("root", "C:"),
        "note": old.get("note") or (
            "语义层：只写文字与路径，数字由页面从 data/*.json 现算。"
            "可用事实占位符 {links.size} {links.count} {links.pct} {drive} {disk.used} "
            "{disk.free} {disk.usedPct} {ram} {hiber} {hiber.pct} {clean.total} {clean.count} "
            "{top.name} {top.size} {top.pct}；路径写真实绝对路径，不要用占位符。"
        ),
        "overview": old.get("overview") or {
            "conclusion": [
                {"title": "已迁出 {links.count} 个目录", "metric": "{links.size}", "done": True,
                 "text": "这些路径已是联接点 / 符号链接，实体在其他盘，<b>不再占用 {drive} 盘</b>，占总量 <b>{links.pct}%</b>。"},
                {"title": "可安全回收 {clean.total}", "metric": "{clean.total}",
                 "text": "按「清理指引」A 类逐项确认即可回收。"},
                {"title": "最大占用：{top.name}", "metric": "{top.size}",
                 "text": "占全盘 <b>{top.pct}</b>，是下一步最值得拆开看的一块。"},
            ]
        },
        "dirs": {"items": dir_items, "details": details},
        "clean": clean,
        "systemClean": old.get("systemClean") or {
            "total": "3–9 GB",
            "items": [
                {"name": "WinSxS 组件清理", "note": "删除被取代的旧组件版本", "value": "2–6 GB"},
                {"name": "磁盘清理", "note": "Windows 更新清理 / 投递优化 / 缩略图", "value": "1–3 GB"},
                {"name": "系统还原点", "note": "卷影副本占用上限收敛", "value": "视配置"},
            ],
            "commands": [
                "Dism /Online /Cleanup-Image /StartComponentCleanup",
                "cleanmgr → 清理系统文件 → 勾选「Windows 更新清理」「投递优化文件」",
            ],
            "hint": "/ResetBase 更彻底但会锁定当前更新包、无法回滚，建议先不加。",
        },
        "review": review,
        "forbid": forbid,
        "traps": old.get("traps") or [],
        "steps": steps,
        "stepsTotal": old.get("stepsTotal") or _seed_total(steps),
        "migrate": migrate,
        "migrateNote": old.get("migrateNote") or {
            "text": "参考命令（示例，需管理员）：先移动实体，再建链接，<目标盘> 换成自己的迁移盘 ——",
            "commands": ["robocopy \"<路径>\" \"<目标盘>:\\Links\\<名字>\" /E /MOVE",
                         "mklink /J \"<路径>\" \"<目标盘>:\\Links\\<名字>\""],
            "hint": "移动前先退出正在使用该目录的程序，否则会因文件占用失败。",
        },
    }
    clean_total = sum(it.get("_b", 0) for it in rep["clean"])

    # ---------- 缺文字统计 ----------
    todo = []

    def need(cond, where):
        if cond:
            todo.append(where)

    for it in dir_items:
        need(not it.get("dsc"), "dirs.items[%s].dsc" % it["path"])
        need(not it.get("tag"), "dirs.items[%s].tag" % it["path"])
    for d in details:
        for r in d["rows"]:
            need(not r.get("dsc"), "dirs.details[%s].rows[%s]" % (d["path"], r["path"]))
    for it in clean:
        need(not it.get("how"), "clean[%s].how" % it.get("title"))
    for it in review:
        need(not it.get("dsc"), "review[%s].dsc" % it["path"])
    for it in forbid:
        need(not it.get("why"), "forbid[%s].why" % it["path"])
    for it in migrate:
        need(not it.get("note"), "migrate[%s].note" % it["path"])
    need(not rep["traps"], "traps（误删陷阱，纯文案）")
    need(not rep["steps"], "steps（执行顺序，纯文案）")

    say("  [tree]    节点 %d · 家目录 %s" % (len(nodes), home))
    say("  [dirs]    主要目录 %d 项 · 明细 %d 组" % (len(dir_items), len(details)))
    say("  [clean]   A 类 %d 项 · 合计 %.2f GB" % (len(clean), clean_total / GB))
    say("  [review]  C 类 %d 项 · [forbid] 禁区 %d 项 · [migrate] 建议迁移 %d 项"
          % (len(review), len(forbid), len(migrate)))
    say("  [todo]    待补文字 %d 处" % len(todo))
    for t in todo[:12]:
        say("              - %s" % t)
    if len(todo) > 12:
        say("              …… 另有 %d 处" % (len(todo) - 12))

    if old.get("clean") or (old.get("dirs") or {}).get("details"):
        say("  [warn]    已有 report.json：条目按本次扫描重排，文字按路径复用；"
              "路径对不上的条目会留空 —— 人工润色过的文件建议先备份")

    for it in rep["clean"]:
        it.pop("_b", None)

    out = out or os.path.join(content, "report.json")
    wr(out, rep)
    say("  [out]     %s (%.1f KB)" % (out, os.path.getsize(out) / 1024.0))
    return out, todo


def fill_dirs(data, content):
    """把用途库里的说明填进 report.json 里空着的目录描述。

    和 seed() 的区别：**不动结构**。不重排、不增删条目，只修说明文字。
    以省略号结尾、且能确认只是用途库文本前缀的描述，换回完整文本。
    导入 ticket 后调这个，人工润色过的 report.json 一个字节都不会被挪位置。
    返回补上或修好的条数。
    """
    rep_p = os.path.join(content, "report.json")
    R = rd(rep_p)
    if not R:
        return 0
    T = rd(os.path.join(data, "tree.json"))
    if not T:
        return 0
    P = pathvars.expand(rd(os.path.join(content, "purpose.json")), 
                        pathvars.context(pathvars.home_of(T), T.get("root", "C:"),
                                         pathvars.sysdrive_of(T)))
    if not P:
        return 0
    _P, exact, names, paths = pur.load_index(P)
    ctx = pathvars.context(pathvars.home_of(T), T.get("root", "C:"),
                           pathvars.sysdrive_of(T))

    def look(key):
        """key 是 %HOME% 形式的路径，先展开成真实路径再查库。"""
        if not key:
            return None
        real = pathvars.expand_str(key, ctx)
        name = real.rstrip(BS).rsplit(BS, 1)[-1] or real
        return resolve(real, name, exact, names, paths)

    def fill(row):
        old = row.get("dsc") or ""
        pu = look(row.get("path"))
        new = _untruncate(old, pu.get("t") if pu else "")
        if new and new != old:
            row["dsc"] = new
            return 1
        return 0

    n = 0
    for it in (R.get("dirs") or {}).get("items") or []:
        n += fill(it)

    for d in (R.get("dirs") or {}).get("details") or []:
        for row in d.get("rows") or []:
            n += fill(row)

    if n:
        wr(rep_p, R)
    return n

