# -*- coding: utf-8 -*-
"""快照历史：落档、索引、对比、趋势、首次见于。

每次 assemble.py 刷新数字层后，把 tree / links / meta 三件套原样内嵌成一张
快照存进 data\\drives\\<盘>\\history\\，累积出「这台机器的磁盘时间线」。对比与
趋势都在这份档案上现算，不碰文件系统、不写任何缓存。

口径：
    - 目录的 b 是整棵子树的文件体积总和，与门槛无关，跨快照直接可比；
    - 「新增」= 上一张没有的节点（新建或首次突破门槛），文案由前端如实标注；
    - diff 只在两快照 root 一致时给出，趋势只连接 root 相同的点；
    - 快照 id 取自 meta.scannedAt，同一分钟重复刷新 = 覆盖。

这个模块只读档案 + 写自己的目录，失败一律向上抛 —— 由调用方（assemble）
决定「快照失败不连坐刷新」。
"""
import io
import json
import os
import re
import time

import storage

HISTORY_DIRNAME = "history"
SNAP_PREFIX = "snap-"
SNAP_SUFFIX = ".json"
INDEX_NAME = "index.json"
TRASH_NAME = "_trash"      # 删除的快照移到这里，移回 history/ 即可找回
TRASH_KEEP = 24
DEFAULT_KEEP = 64

# 快照 id 只允许这个形态 —— 它会拼进文件路径，必须挡掉 ../ 之类的注入
ID_RE = re.compile(r"^\d{8}_\d{4}$")
AT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})")

_CACHE = {}          # (normcase(path), mtime) -> 解析结果，读多写少的小档案
_CACHE_MAX = 8


class DiffError(Exception):
    """两张快照没法比（root 不一致 / 缺 tree）。"""


def hdir(data_dir):
    return os.path.join(data_dir, HISTORY_DIRNAME)


def snap_path(data_dir, sid):
    if not ID_RE.match(sid or ""):
        raise DiffError("快照 id 不合法：%r" % (sid,))
    return os.path.join(hdir(data_dir), SNAP_PREFIX + sid + SNAP_SUFFIX)


def _wr_atomic(path, obj):
    tmp = path + ".new"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _read(path):
    if not os.path.exists(path):
        return None
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- 落档

def _entry(sid, tree, links, meta):
    """索引条目：列表页要用的轻量摘要，从内存 payload 直接取。"""
    drive = (meta.get("drive") or "").upper()
    off = 0.0
    for it in (links or {}).get("items") or []:
        if (it.get("d") or "").upper() != drive:
            off += float(it.get("b") or 0)
    return {
        "id": sid,
        "at": meta.get("scannedAt") or "",
        "drive": meta.get("drive") or "",
        "root": (tree or {}).get("root") or "",
        "total": float((tree or {}).get("totalBytes") or 0),
        "files": meta.get("fileCount") or 0,
        "dirs": meta.get("dirCount") or 0,
        "capGB": meta.get("capGB") or 0,
        "usedGB": meta.get("usedGB") or 0,
        "links": len((links or {}).get("items") or []),
        "offsite": off,
    }


def _rebuild_index(data_dir, sids):
    out = []
    for sid in sids:
        snap = _read(snap_path(data_dir, sid))
        if not snap:
            continue
        out.append(_entry(snap["id"], snap.get("tree"), snap.get("links"),
                          snap.get("meta") or {}))
    return out


def _write_index(data_dir, entries):
    _wr_atomic(os.path.join(hdir(data_dir), INDEX_NAME), entries)


def _prune(data_dir, keep):
    """按 id 升序裁掉最老的，返回被删的 id。keep<=0 表示不限。"""
    if keep is None or keep <= 0:
        return []
    sids = snapshots(data_dir)
    gone = []
    for sid in sids[:max(0, len(sids) - keep)]:
        try:
            os.remove(snap_path(data_dir, sid))
            gone.append(sid)
        except OSError:
            pass
    return gone


def record(data_dir, tree, links, meta, keep=DEFAULT_KEEP, deep=None):
    """把当前数字层记成一张快照。返回快照 id。

    deep = {"d": {目录路径: b}, "f": {文件路径: b}} —— 全深度、超门槛的
    深索引，供快照间「深层到底哪变了」的下钻对比；不传则快照无深索引
    （早于该功能的旧档），deepdiff 对它们如实报错。
    """
    if not isinstance(meta, dict) or not (tree or {}).get("root"):
        raise DiffError("快照缺 meta 或 tree.root，不落档")
    os.makedirs(hdir(data_dir), exist_ok=True)

    at = meta.get("scannedAt") or ""
    m = AT_RE.match(at.strip())
    sid = ("%s%s%s_%s%s" % m.groups()) if m else time.strftime("%Y%m%d_%H%M")
    _wr_atomic(snap_path(data_dir, sid),
               {"id": sid, "meta": meta, "tree": tree, "links": links,
                "deep": deep if isinstance(deep, dict) else None})

    # 索引：坏了自己重建；同 id 覆盖
    idx = _read(os.path.join(hdir(data_dir), INDEX_NAME))
    entries = idx if isinstance(idx, list) else []
    known = {e.get("id") for e in entries if isinstance(e, dict)}
    if not known:
        entries = _rebuild_index(data_dir, snapshots(data_dir))
    entries = [e for e in entries
               if isinstance(e, dict)
               and ID_RE.match(str(e.get("id") or ""))
               and os.path.exists(snap_path(data_dir, e["id"]))
               and e.get("id") != sid]
    entries.append(_entry(sid, tree, links, meta))
    entries.sort(key=lambda e: e.get("id") or "")
    _write_index(data_dir, entries)

    gone = _prune(data_dir, keep)
    if gone:
        entries = [e for e in entries if e.get("id") not in set(gone)]
        _write_index(data_dir, entries)
    return sid


# ---------------------------------------------------------------- 读取

def snapshots(data_dir):
    """全部快照 id，按时间升序。目录不存在 = 空历史。"""
    hd = hdir(data_dir)
    try:
        names = os.listdir(hd)
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith(SNAP_PREFIX) and n.endswith(SNAP_SUFFIX):
            sid = n[len(SNAP_PREFIX):-len(SNAP_SUFFIX)]
            if ID_RE.match(sid):
                out.append(sid)
    out.sort()
    return out


def summaries(data_dir):
    """索引条目列表（升序）。索引坏/丢时从快照文件重建。"""
    idx = _read(os.path.join(hdir(data_dir), INDEX_NAME))
    if isinstance(idx, list) and idx:
        sids = snapshots(data_dir)
        if [e.get("id") for e in idx if isinstance(e, dict)] == sids:
            return [e for e in idx if isinstance(e, dict)]
    entries = _rebuild_index(data_dir, snapshots(data_dir))
    if entries:
        try:
            _write_index(data_dir, entries)
        except OSError:
            pass
    return entries


def all_summaries(data_root):
    """data/drives/*/history/ 下全部盘的快照条目，按时间升序。

    快照按盘各存一份（不同盘的 tree 不可比），查看时却要能看到全貌：
    只给当前盘的话，切到一个刚扫过的盘会只剩一两张，看着像历史被删了。
    每条都带 root / drive 字段，调用方按它挑自己要看的那一盘。
    """
    out = []
    d = os.path.join(data_root, "drives")
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        sub = os.path.join(d, name)
        if os.path.isdir(hdir(sub)):
            out.extend(summaries(sub))
    out.sort(key=lambda e: (e.get("id") or "", e.get("root") or ""))
    return out


def load(data_dir, sid):
    """读一张快照；带小缓存（档案只增不改，mtime 变了才重读）。"""
    p = snap_path(data_dir, sid)
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    key = (os.path.normcase(p), mt)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    snap = _read(p)
    if snap is None:
        return None
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = snap
    return snap


# ---------------------------------------------------------------- 对比

def _flatten(tree_payload):
    """树 → {绝对路径: (b, f, n)}。根也在内。"""
    out = {}
    node = (tree_payload or {}).get("tree")
    root = (tree_payload or {}).get("root") or ""
    if not isinstance(node, dict):
        return out, root
    stack = [(root, node)]
    while stack:
        p, nd = stack.pop()
        out[p] = (float(nd.get("b") or 0), 1 if nd.get("f") == 1 else 0,
                  nd.get("n") or "")
        for ch in nd.get("c") or []:
            stack.append((p + "\\" + (ch.get("n") or ""), ch))
    return out, root


def _depth(root, p):
    rd = root.count("\\")
    return max(0, p.count("\\") - rd)


def diff(old, new):
    """两张快照的结构化对比。root 不一致抛 DiffError。"""
    fo, oroot = _flatten(old.get("tree"))
    fn, nroot = _flatten(new.get("tree"))
    if not fo or not fn:
        raise DiffError("快照缺树，没法比")
    if oroot != nroot:
        raise DiffError("两次扫描的不是同一棵树（%s vs %s），不能对比" % (oroot, nroot))

    added, removed, changed = [], [], []
    for p, (b, f, n) in fn.items():
        if p == nroot:
            continue
        o = fo.get(p)
        if o is None:
            added.append({"p": p, "n": n, "f": f, "d": _depth(nroot, p), "b": b})
        elif o[0] != b:
            d = b - o[0]
            changed.append({"p": p, "n": n, "f": f, "d": _depth(nroot, p),
                            "ob": o[0], "nb": b, "delta": d,
                            "pct": round(d / o[0] * 100, 1) if o[0] else None})
    for p, (b, f, n) in fo.items():
        if p != nroot and p not in fn:
            removed.append({"p": p, "n": n, "f": f, "d": _depth(nroot, p), "b": b})

    added.sort(key=lambda r: -r["b"])
    removed.sort(key=lambda r: -r["b"])
    changed.sort(key=lambda r: -abs(r["delta"]))

    # 级联归并：一个深层变化（比如某个大文件）会让它的每个 ≥门槛 祖先目录
    # 都留下一行同量变化 —— 涨跌榜里全是同一条新闻的转载。给「几乎被某个
    # 更深层条目完全解释」的目录行打 cascade 标记，前端排名表把它们归并掉，
    # 只登原创新闻。阈值 max(4KB, 2%)：残差在阈值内的父行视为传导管道。
    by_abs = sorted(changed, key=lambda r: -abs(r["delta"]))
    for r in changed:
        if r["f"] != 0:
            continue
        rd = abs(r["delta"])
        tol = max(4096.0, rd * 0.02)
        floor = rd - tol
        pref = r["p"] + "\\"
        for s in by_abs:
            sd = abs(s["delta"])
            if sd < floor:
                break            # 按 |delta| 降序，后面只会更小
            if s is not r and s["p"].startswith(pref):
                r["cascade"] = s["p"]
                break

    om, nm = old.get("meta") or {}, new.get("meta") or {}
    return {
        "from": {"id": old.get("id"), "at": om.get("scannedAt") or "",
                 "total": fo[nroot][0], "drive": om.get("drive") or ""},
        "to": {"id": new.get("id"), "at": nm.get("scannedAt") or "",
               "total": fn[nroot][0], "drive": nm.get("drive") or ""},
        "root": nroot,
        "summary": {
            "delta": fn[nroot][0] - fo[nroot][0],
            "added": len(added), "removed": len(removed), "changed": len(changed),
        },
        "added": added,
        "removed": removed,
        "changed": changed,
    }


# ---------------------------------------------------------------- 深层对比

DEEPDIFF_MAX_ROWS = 500


def _deep_maps_of(snap):
    deep = snap.get("deep") or {}
    return deep.get("d") or {}, deep.get("f") or {}


def _kids_index(dd, ff, root_dir):
    """深索引 → {父路径: [子路径...]}，只收 root_dir 子树内的。"""
    idx = {}
    pref = root_dir + "\\"
    for m in (dd, ff):
        for p in m:
            if not p.startswith(pref):
                continue
            cut = p.rfind("\\")
            idx.setdefault(p[:cut], []).append(p)
    return idx


def deep_diff(old, new, root_dir):
    """在 root_dir 子树内逐层下钻，给到深索引能解释的最深层变化。

    深索引收录全深度（≤16 层）、超门槛（目录 MinMB / 文件 FileMinMB）的
    路径，所以「目录变了」能一路指认到最深的目录/文件；再往下的碎项
    算进每层的 residual（自身变化 − 可解释子项变化之和），顶层 residual
    同理对着 root_dir 自身的 b 变化，如实标注而不是装作看不见。
    快照没有深索引（早于该功能的旧档）抛 DiffError。
    """
    if old.get("deep") is None or new.get("deep") is None:
        sid = old.get("id") if old.get("deep") is None else new.get("id")
        raise DiffError("快照 %s 没有深索引（早于该功能），无法下钻" % sid)
    root_dir = (root_dir or "").rstrip("\\")
    if not root_dir:
        raise DiffError("缺少要下钻的目录")

    odd, off = _deep_maps_of(old)
    ndd, nff = _deep_maps_of(new)
    okids = _kids_index(odd, off, root_dir)
    nkids = _kids_index(ndd, nff, root_dir)

    rows_left = [DEEPDIFF_MAX_ROWS]
    truncated = [False]

    def bval(dd, ff, p):
        v = dd.get(p)
        if v is not None:
            return v, 0
        return ff.get(p), 1

    def descend(cur):
        keys = set(okids.get(cur) or []) | set(nkids.get(cur) or [])
        rows = []
        tot = 0.0
        for p in keys:
            ob, of_ = bval(odd, off, p)
            nb, nf_ = bval(ndd, nff, p)
            if ob is None and nb is None:
                continue
            obv = ob if ob is not None else 0.0
            nbv = nb if nb is not None else 0.0
            delta = nbv - obv
            if delta == 0:
                continue
            is_dir = (p in ndd) or (p in odd)
            row = {"p": p, "n": p.rsplit("\\", 1)[-1], "f": 0 if is_dir else 1,
                   "ob": ob, "nb": nb, "delta": delta,
                   "pct": round(delta / ob * 100, 1) if ob else None}
            if is_dir and rows_left[0] > 0:
                sub_rows, sub_tot = descend(p)
                if sub_rows:
                    row["kids"] = sub_rows
                    row["residual"] = round(delta - sub_tot, 2)
            rows.append(row)
            tot += delta
            if rows_left[0] <= 0:
                truncated[0] = True
            rows_left[0] -= 1
        rows.sort(key=lambda r: -abs(r["delta"]))
        return rows, tot

    rows, tot = descend(root_dir)
    orb, _ = bval(odd, off, root_dir)
    nrb, _ = bval(ndd, nff, root_dir)
    root_delta = (nrb or 0.0) - (orb or 0.0)
    return {"root": root_dir,
            "delta": root_delta,
            "residual": round(root_delta - tot, 2),
            "rows": rows, "truncated": truncated[0]}


def deep_diff_ids(data_dir, from_id, to_id, path):
    old = load(data_dir, from_id)
    new = load(data_dir, to_id)
    if old is None or new is None:
        raise DiffError("快照不存在：%s" % (from_id if old is None else to_id))
    if ((old.get("tree") or {}).get("root") or "") != ((new.get("tree") or {}).get("root") or ""):
        raise DiffError("两次扫描的不是同一棵树，不能对比")
    return deep_diff(old, new, path)


def first_seen(data_dir, paths):
    """{path: 最早包含它的快照 id}。在已保留的档案里找，裁掉的追溯不到。"""
    want = set(paths)
    out = {}
    for sid in snapshots(data_dir):
        snap = load(data_dir, sid)
        if not snap:
            continue
        fl, _ = _flatten(snap.get("tree"))
        for p in list(want):
            if p in fl:
                out[p] = sid
                want.discard(p)
        if not want:
            break
    return out


def diff_ids(data_dir, from_id, to_id, with_fs=True):
    """按 id 取两张快照做对比；added 附上首次见于。"""
    old = load(data_dir, from_id)
    new = load(data_dir, to_id)
    if old is None or new is None:
        raise DiffError("快照不存在：%s" % (from_id if old is None else to_id))
    d = diff(old, new)
    if with_fs and d["added"]:
        fs = first_seen(data_dir, [r["p"] for r in d["added"]])
        for r in d["added"]:
            r["fs"] = fs.get(r["p"])
    return d


# ---------------------------------------------------------------- 趋势

def _tree_paths(tree_payload):
    """tree.json 里全部节点的绝对路径（含文件、含根），与前端 prep() 的拼法一致。"""
    root = (tree_payload or {}).get("root") or ""
    out = []
    node = (tree_payload or {}).get("tree")
    if not isinstance(node, dict):
        return out, root
    stack = [(root, node)]
    while stack:
        p, nd = stack.pop()
        out.append(p)
        for ch in nd.get("c") or []:
            stack.append((p + "\\" + (ch.get("n") or ""), ch))
    return out, root


_MATRIX_CACHE = {"key": None, "val": None}


# ---------------------------------------------------------------- 按盘留档

def _norm_root(r):
    """与 app.py 的 _norm_root 同一套口径：去掉空白/冒号/斜杠再比。"""
    return re.sub(r"[\s:/\\]+", "", r or "").upper()


def forget(data_dir, root):
    """删掉某个根（盘）的全部快照并重建索引，返回被删的快照 id 列表。

    root 按规范化比较："C:" 与 "C:\\" 是同一个盘；快照文件缺 tree/root
    的残档不误删。只动这个盘的 history/，留档目录由调用方自行清理。
    """
    if not root:
        raise DiffError("缺 root")
    key = _norm_root(root)
    gone = []
    for sid in snapshots(data_dir):
        snap = _read(snap_path(data_dir, sid))
        r = ((snap or {}).get("tree") or {}).get("root") or ""
        if not r or _norm_root(r) != key:
            continue
        p = snap_path(data_dir, sid)
        if os.path.exists(p):
            _trash_push(data_dir, p)
            gone.append(sid)
    if gone:
        try:
            _write_index(data_dir, _rebuild_index(data_dir, snapshots(data_dir)))
        except OSError:
            pass
        _CACHE.clear()
        _MATRIX_CACHE["key"] = None
    return gone


def _trash_push(data_dir, path):
    """删除 = 移入 history/_trash/（上限 TRASH_KEEP 张，超出清最旧）。

    回收站里就是原快照文件：把它移回 data\\history\\，索引会自动重建。
    移不动（跨盘等）才退回真删。
    """
    td = os.path.join(hdir(data_dir), TRASH_NAME)
    try:
        os.makedirs(td, exist_ok=True)
        os.replace(path, os.path.join(td, os.path.basename(path)))
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    try:
        entries = [os.path.join(td, n) for n in os.listdir(td)
                   if n.startswith(SNAP_PREFIX)]
        if len(entries) > TRASH_KEEP:
            entries.sort(key=lambda p: os.path.getmtime(p))
            for p in entries[:len(entries) - TRASH_KEEP]:
                os.remove(p)
    except OSError:
        pass


def forget_one(data_dir, sid):
    """删掉指定的一张快照（移入 _trash/）并重建索引。

    sid 先过 ID_RE 校验（snap_path 会挡）；不存在时如实抛 DiffError。
    """
    p = snap_path(data_dir, sid)
    if not os.path.exists(p):
        raise DiffError("快照不存在：%s" % sid)
    _trash_push(data_dir, p)
    try:
        _write_index(data_dir, _rebuild_index(data_dir, snapshots(data_dir)))
    except OSError:
        pass
    _CACHE.clear()
    _MATRIX_CACHE["key"] = None


def store_name(root):
    """盘根 → 留档目录名。只留字母数字下划线并统一大写，杜绝路径注入。"""
    return storage.key(root)


def drive_store(data_dir, root):
    """按盘留档路径：data/drives/<盘>/。换盘扫描互不覆盖，
    每个盘保留自己最新一份三件套，前端据此提供多盘数据源切换。"""
    return storage.data_store(data_dir, root)


def trend_matrix(data_dir, limit=None, tree_path=None):
    """当前树里全部路径的跨快照体积矩阵（稀疏）。

    返回 {"ids": [快照id...], "root": ..., "paths": {路径: [b|null, ...]}}，
    只覆盖 data/tree.json 里存在的路径 —— 正好是页面上能悬停到的全部节点
    （下钻看到的更深目录从未单独入档，本来就没有逐路径历史）。

    序列「有故事」才返回：全程都在且体积不变的路径不占字节，前端拿它
    当前的 b 自补恒定线；全程缺席的路径同理跳过。limit 截断返回条数
    （离线内嵌控体积用），按最新体积从大到小保留。

    结果进程内缓存；快照集合、任一快照文件或 tree.json 变了才重算。
    tree_path 可指向某个盘的留档树（多盘查看），缺省用 data/tree.json。
    """
    tree_file = tree_path or os.path.join(data_dir, "tree.json")
    tree_payload = _read(tree_file)
    sids = snapshots(data_dir)
    if not isinstance(tree_payload, dict) or len(sids) < 2:
        return {"ids": [], "root": (tree_payload or {}).get("root") or "", "paths": {}}
    root = tree_payload.get("root") or ""

    try:
        tree_key = os.stat(tree_file).st_mtime_ns
    except OSError:
        tree_key = 0
    snap_key = 0
    for sid in sids:
        try:
            snap_key = max(snap_key, int(os.path.getmtime(snap_path(data_dir, sid)) * 1000))
        except OSError:
            pass
    key = (tree_file, tuple(sids), tree_key, snap_key, limit)
    if _MATRIX_CACHE["key"] == key:
        return _MATRIX_CACHE["val"]

    # 与趋势图同一条铁律：只连 root 相同的快照，换过盘的历史不混入
    ordered = []
    for sid in sids:
        s = load(data_dir, sid)
        if s and ((s.get("tree") or {}).get("root") or "") == root:
            ordered.append(sid)

    paths, _ = _tree_paths(tree_payload)
    series = {}
    for sid in ordered:
        s = load(data_dir, sid)
        fl, _ = _flatten(s.get("tree"))
        for p in paths:
            n = fl.get(p)
            if p not in series:
                series[p] = []
            series[p].append(n[0] if n else None)

    out = {}
    for p in paths:
        vals = series.get(p) or []
        if not vals:
            continue
        first = vals[0]
        constant = True
        for v in vals:
            if v != first:
                constant = False
                break
        if constant:
            continue            # 恒定（含全程缺席）：没有可画的变化
        out[p] = tuple(vals)    # 不可变：缓存与调用方隔离

    if limit is not None and len(out) > limit:
        def latest_b(p):
            for v in reversed(out[p]):
                if v is not None:
                    return v
            return 0.0
        keep = sorted(out, key=latest_b, reverse=True)[:limit]
        out = {p: out[p] for p in keep}

    val = {"ids": ordered, "root": root, "paths": out}
    _MATRIX_CACHE["key"] = key
    _MATRIX_CACHE["val"] = val
    # 每次返回浅拷贝：调用方改字典不影响缓存（序列本身是不可变元组）
    return {"ids": list(ordered), "root": root, "paths": dict(out)}


def trend(data_dir, path, root=None):
    """路径在快照时间线上的体积序列。

    root 缺省取最新一张快照的 root；root 不同的旧快照（换过盘）不混入，
    序列里节点缺席 = None（当时还没被收录/不存在）。

    root 先归一化再比：调用方可能传 "D:" 也可能传 "D:\\"（前端 DATA.root
    会补尾部反斜杠），字面比较会让整条序列被当成别的盘全部跳过。
    """
    sids = snapshots(data_dir)
    if not sids:
        return {"path": path, "root": root, "points": []}
    if root is None:
        last = load(data_dir, sids[-1])
        root = (last.get("tree") or {}).get("root") if last else None
    want = _norm_root(root)
    points = []
    for sid in sids:
        snap = load(data_dir, sid)
        if not snap:
            continue
        if _norm_root((snap.get("tree") or {}).get("root") or "") != want:
            continue
        fl, _ = _flatten(snap.get("tree"))
        node = fl.get(path)
        points.append({"id": sid, "b": node[0] if node else None})
    return {"path": path, "root": root, "points": points}


def offline_bundle(data_dir, max_movers=40, matrix_limit=1200):
    """离线单文件内嵌包：摘要 + 最近一次对比 + 总量/Top 变动趋势 + 稀疏矩阵。

    matrix 是 trend_matrix 的截断版（按最新体积取前 matrix_limit 条），
    离线版的悬停趋势小图只对它覆盖的路径可用。
    """
    sids = snapshots(data_dir)
    sums = summaries(data_dir)
    bundle = {"snapshots": sums, "ids": [], "diff": None, "totals": [],
              "movers": {}, "matrix": {}}
    if not sids:
        return bundle
    last = load(data_dir, sids[-1])
    root = (last.get("tree") or {}).get("root") if last else None

    # 与 trend 同一条铁律：只画 root 相同的序列
    ordered = []
    for sid in sids:
        s = load(data_dir, sid)
        if s and ((s.get("tree") or {}).get("root") or "") == root:
            ordered.append(sid)
    bundle["ids"] = ordered
    if ordered:
        series = []
        for sid in ordered:
            s = load(data_dir, sid)
            series.append(float((s.get("tree") or {}).get("totalBytes") or 0))
        bundle["totals"] = series

    if len(ordered) >= 2:
        d = diff_ids(data_dir, ordered[-2], ordered[-1])
        bundle["diff"] = d
        real = [r for r in d["changed"] if not r.get("cascade")]
        paths = [r["p"] for r in real[:max_movers]]
        for r in d["added"]:
            if len(paths) >= max_movers:
                break
            if r["p"] not in paths:
                paths.append(r["p"])
        for p in paths:
            bundle["movers"][p] = [
                pt["b"] for pt in trend(data_dir, p, root=root)["points"]]
        try:
            mat = trend_matrix(data_dir, limit=matrix_limit)
            if mat["ids"] == ordered:
                bundle["matrix"] = mat["paths"]
        except Exception:
            pass
    return bundle
