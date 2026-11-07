# -*- coding: utf-8 -*-
"""目录树采集：一次 os.scandir 流式遍历，产出全盘树与（可选）细化树。

快的两个来源：

    - entry.stat(follow_symlinks=False) 复用目录枚举时缓存的元数据，
      每个条目只有一次系统调用，属性查询与文件信息不会各走一遍；
    - 归账走「祖先链元组」，不对每个文件 Split('\\') 再逐级拼路径。

细化根在扫描根内时，全盘树与细化树共用同一次遍历，细化子树的文件不会被
重访；不在扫描根内时单独再扫一遍。

输出字段：

    root rootLabel depth minMB fileMinMB totalBytes fileCount dirCount skipped tree

不跟随 reparse point（Windows junction 不是 symlink，必须看文件属性），
遇到的全部 reparse point 进 skipped[]，供 linktargets.ps1 挑选测量。
遍历本身不受深度限制 —— depth 只决定「账记到哪一层、输出保留到哪一层」，
收紧 depth / minMB 省的是输出和归账，省不了磁盘遍历本身。
"""
import argparse
import io
import json
import os
import re
import stat
import sys
import time

MB = 1048576.0
GB = MB * 1024.0
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# 控制台编码（中文 Windows 上是 cp936）装不下某些字符时，宁可降级成 "?",
# 也别让整个扫描因为一个文件名崩掉。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# 深索引的最深层数：目录树输出封在 depth（默认 6），但「定位深层变化」需要
# 更深的目录与文件档案 —— 封在 16 层，够深到能指认，又不至于索引爆炸。
DEEP_DEPTH = 16


def _root_parts(root):
    s = (root or "").strip().strip('"')
    if re.match(r"^[A-Za-z]:$", s):
        s += "\\"
    p = os.path.normpath(os.path.abspath(s))
    if not p.endswith(("\\", "/")):
        p += "\\"
    base = p.rstrip("\\/")
    return p, base or p


def _error_text(path, exc):
    return path + "  ::  " + type(exc).__name__ + " : " + str(exc)


def _under(path, root_path):
    return os.path.normcase(path).startswith(os.path.normcase(root_path))


def _inside(base, root_base):
    """base 是否等于或位于 root_base 之内（盘符大小写不敏感）。"""
    nb = os.path.normcase(base)
    nr = os.path.normcase(root_base)
    return nb == nr or nb.startswith(nr + "\\")


def _prune(node, min_bytes):
    keep = []
    for child in node.get("c", []):
        if child.get("f") == 1 or child["b"] >= min_bytes:
            _prune(child, min_bytes)
            keep.append(child)
    node["c"] = keep


class Ledger(object):
    """一次遍历里的一本账：全盘树与细化树各记各的，互不干扰。

    enter 为 None 表示从遍历种子起就活跃（全盘账本）；否则是自身根目录
    的 normcase 值，遍历走到那个目录时开始记账（细化账本）。
    """

    def __init__(self, root_path, base, depth, min_mb, file_min_mb, enter=None):
        self.root_path = root_path          # 带尾反斜杠，作 rootLabel
        self.base = base                    # 不带尾反斜杠，作树根键
        self.depth = max(0, int(depth))
        self.min_mb = float(min_mb)
        self.file_min_mb = float(file_min_mb)
        self.min_bytes = self.min_mb * MB
        self.file_min_bytes = self.file_min_mb * MB
        self.enter = os.path.normcase(enter) if enter else None
        self.dir_acc = {}
        self.file_nodes = []
        self.skipped = []
        self.total = 0.0
        self.file_count = 0
        self.dir_count = 0


def _walk(root_path, main, detail, errors, deep=None, progress=None):
    """一次 DFS。main 从种子开始记账；detail 在走到它的根目录后开始记账。

    栈条目：(path, level, chain, dstate)
      chain  —— 祖先目录绝对路径元组（levels 1..level，封顶在 max(depth, DEEP_DEPTH)）；
                主树归账取 chain[:main.depth]，深索引归账取整条
      dstate —— None，或 (drel, dchain)：detail 视角的相对深度与祖先元组

    某棵子树内部的遍历次序由「后进先出 + 目录项顺序」唯一决定，与栈里
    是否穿插其他子树无关，所以两本账的 skipped[] 顺序与单独扫描一致。

    progress(ledger, cur_dir) 每两秒最多调一次 —— 扫描动辄几分钟，
    界面上至少要能看出还在动。
    """
    dstate = None
    if detail is not None and detail.enter == os.path.normcase(root_path.rstrip("\\/")):
        dstate = (0, ())        # 细化根就是扫描根：种子即激活
    # 链的封顶取两者较大：主树要 depth 层，深索引要 DEEP_DEPTH 层，都不能缺
    cap = main.depth if main.depth > DEEP_DEPTH else DEEP_DEPTH
    stack = [(root_path, 0, (), dstate)]
    tick = 0
    last_tick = 0.0         # 第一个 tick 立刻报一行，小盘也能看到「在动」
    while stack:
        path, level, chain, dstate = stack.pop()
        try:
            entries = os.scandir(path)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                    if getattr(st, "st_file_attributes", 0) & REPARSE or stat.S_ISLNK(st.st_mode):
                        main.skipped.append(entry.path)
                        if dstate is not None:
                            detail.skipped.append(entry.path)
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        main.dir_count += 1
                        child_level = level + 1
                        child_chain = chain + (entry.path,) if child_level <= cap else chain
                        if dstate is None:
                            if detail is not None and os.path.normcase(entry.path) == detail.enter:
                                stack.append((entry.path, child_level, child_chain, (0, ())))
                            else:
                                stack.append((entry.path, child_level, child_chain, None))
                            continue
                        drel, dchain = dstate
                        cdrel = drel + 1
                        cdchain = dchain + (entry.path,) if cdrel <= detail.depth else dchain
                        detail.dir_count += 1
                        stack.append((entry.path, child_level, child_chain, (cdrel, cdchain)))
                        continue
                    size = float(st.st_size)
                    main.total += size
                    main.file_count += 1
                    # 单趟归账：前 main.depth 个祖先进主树，整条进深索引
                    i = 0
                    for key in chain:
                        if i >= main.depth:
                            if deep is None:
                                break       # 主树已记完，深索引又不要 —— 提前收工
                        else:
                            main.dir_acc[key] = main.dir_acc.get(key, 0.0) + size
                        if deep is not None:
                            deep["acc"][key] = deep["acc"].get(key, 0.0) + size
                        i += 1
                    if size >= main.file_min_bytes and level <= main.depth:
                        main.file_nodes.append(
                            (chain[-1] if chain else main.base, entry.name, size))
                    if deep is not None and size >= main.file_min_bytes and level <= DEEP_DEPTH:
                        # 用 entry.path 存真实完整路径；超过 DEEP_DEPTH 的文件
                        # 不进深索引（其祖先目录已不在索引里，路径会拼错）
                        deep["files"][entry.path] = size
                    if dstate is not None:
                        drel, dchain = dstate
                        detail.total += size
                        detail.file_count += 1
                        for key in dchain:
                            detail.dir_acc[key] = detail.dir_acc.get(key, 0.0) + size
                        if size >= detail.file_min_bytes and drel <= detail.depth:
                            detail.file_nodes.append(
                                (dchain[-1] if dchain else detail.base, entry.name, size))
                except OSError as exc:
                    errors.append((entry.path, exc))

                if progress is not None:
                    tick += 1
                    if not (tick & 0x3FF):
                        now = time.time()
                        if now - last_tick >= 2.0:
                            last_tick = now
                            progress(main, path)


def _build_tree(ledger):
    root_node = {"n": ledger.root_path, "b": ledger.total, "c": []}
    nodes = {ledger.base: root_node}
    for key, size in ledger.dir_acc.items():
        nodes[key] = {"n": key.rsplit("\\", 1)[-1], "b": size, "c": []}

    for parent, name, size in ledger.file_nodes:
        node = nodes.get(parent)
        if node is not None:
            node["c"].append({"n": name, "b": size, "f": 1, "c": []})

    for key, node in nodes.items():
        if key == ledger.base:
            continue
        # 盘根键是 "C:" 而它的父路径 "C:\" 不成立，所以手工截父路径。
        cut = key.rfind("\\")
        parent = key[:cut] if cut > 1 else ledger.base
        pnode = nodes.get(parent)
        if pnode is not None:
            pnode["c"].append(node)

    _prune(root_node, ledger.min_bytes)
    return root_node


def _deep_maps(deep, min_bytes):
    """深索引 → {目录: b, 文件: b}。目录门槛与主树一致。"""
    dirs = {}
    for key, size in deep["acc"].items():
        if size >= min_bytes:
            dirs[key] = size
    return dirs, deep["files"]


def _payload(ledger):
    return {
        "root": ledger.base,
        "rootLabel": ledger.root_path,
        "depth": ledger.depth,
        "minMB": ledger.min_mb,
        "fileMinMB": ledger.file_min_mb,
        "totalBytes": ledger.total,
        "fileCount": ledger.file_count,
        "dirCount": ledger.dir_count,
        "skipped": ledger.skipped,
        "tree": _build_tree(ledger),
    }


def scan(root, depth=6, min_mb=40, file_min_mb=20, detail=None, progress=None):
    """扫描 root，可选同时产出细化树。返回：

        {"full": payload, "detail": payload|None,
         "full_errors": [(path, exc)], "detail_errors": [(path, exc)]}

    detail = {"root": 细化根, "depth": 相对深度, "min_mb": 目录门槛}，
    depth / min_mb 缺省沿用全盘参数；fileMinMB 两棵树共用。
    细化根不存在时跳过细化（stderr 提示）；细化根是 reparse point 时
    遍历不会进入它，细化树只剩根节点，合并时按「分支缺失」打警告跳过。

    progress 是进度回调，见 _walk。
    """
    root_path, base = _root_parts(root)
    errors = []
    main = Ledger(root_path, base, depth, min_mb, file_min_mb)

    det_ledger = None
    det_errors = None
    if detail and (detail.get("root") or "").strip():
        d_path, d_base = _root_parts(detail["root"])
        d_depth = detail.get("depth")
        d_depth = main.depth if d_depth is None else int(d_depth)
        d_min = detail.get("min_mb")
        d_min = main.min_mb if d_min is None else float(d_min)
        if os.path.isdir(d_path):
            if _inside(d_base, base):
                # 细化根在扫描根内：同一次遍历记两本账
                det_ledger = Ledger(d_path, d_base, d_depth, d_min, file_min_mb,
                                    enter=d_base)
            else:
                # 细化根不在扫描根内：另起一本账，单独扫一遍
                det_ledger = Ledger(d_path, d_base, d_depth, d_min, file_min_mb)
                det_errors = []
                _walk(d_path, det_ledger, None, det_errors)
        else:
            print("  [warn] 细化根不存在，跳过细化：%s" % d_path, file=sys.stderr)

    merged = det_ledger if det_ledger is not None and det_ledger.enter is not None else None
    deep = {"acc": {}, "files": {}}
    _walk(root_path, main, merged, errors, deep=deep, progress=progress)

    full = _payload(main)
    # 深索引只属于主扫描：全深度、超门槛的目录与文件，供快照间的深层对比。
    # 挂在 payload 上随 tree-full.json 走，assemble 会摘出来只放进快照。
    dd, df = _deep_maps(deep, main.min_bytes)
    full["deepDirs"] = dd
    full["deepFiles"] = df

    result = {
        "full": full,
        "detail": _payload(det_ledger) if det_ledger is not None else None,
        "full_errors": errors,
        "detail_errors": det_errors,
    }
    if det_ledger is not None and det_errors is None:
        # 合并模式下细化账本与全盘共用错误列表，按细化根前缀摘出来
        result["detail_errors"] = [
            (p, e) for p, e in errors if _under(p, det_ledger.root_path)]
    return result


def _summary_line(payload, error_count):
    return "root=%s total=%.3fGB files=%d dirs=%d links=%d errors=%d" % (
        payload["rootLabel"], payload["totalBytes"] / GB,
        payload["fileCount"], payload["dirCount"],
        len(payload["skipped"]), error_count)


def write_output(payload, errors, out, log=""):
    parent = os.path.dirname(os.path.abspath(out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with io.open(out, "w", encoding="utf-8", newline="") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    if log:
        parent = os.path.dirname(os.path.abspath(log))
        if parent:
            os.makedirs(parent, exist_ok=True)
        lines = [_summary_line(payload, len(errors))]
        lines.extend("ERR " + _error_text(p, e) for p, e in errors)
        with io.open(log, "w", encoding="utf-8", newline="") as f:
            f.write("\r\n".join(lines))


def _say_progress(ledger, cur):
    """进度行：文件数、累计体积、当前目录。一行一个时间点，日志里也看得懂。"""
    sys.stdout.write("  [scan] %s 文件 · %.2f GB · %s\n"
                     % (format(ledger.file_count, ","), ledger.total / GB, cur))
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("SystemDrive", "C:") + "\\")
    ap.add_argument("--depth", type=int, default=6)
    ap.add_argument("--min-mb", type=float, default=40)
    ap.add_argument("--file-min-mb", type=float, default=20)
    ap.add_argument("--out", default="tree.json")
    ap.add_argument("--log", default="")
    ap.add_argument("--detail-root", default="")
    ap.add_argument("--detail-depth", type=int, default=None)
    ap.add_argument("--detail-min-mb", type=float, default=None)
    ap.add_argument("--detail-out", default="")
    ap.add_argument("--detail-log", default="")
    ap.add_argument("--quiet", action="store_true", help="不打印扫描进度")
    a = ap.parse_args(argv)

    root_path, _ = _root_parts(a.root)
    if not os.path.isdir(root_path):
        print("scan root is not a directory: %s" % root_path, file=sys.stderr)
        return 2
    if (a.detail_root or "").strip() and not a.detail_out:
        print("--detail-root 需要 --detail-out 才会输出细化树", file=sys.stderr)

    detail = None
    if a.detail_out and (a.detail_root or "").strip():
        detail = {"root": a.detail_root, "depth": a.detail_depth, "min_mb": a.detail_min_mb}

    r = scan(a.root, a.depth, a.min_mb, a.file_min_mb, detail,
             progress=None if a.quiet else _say_progress)
    write_output(r["full"], r["full_errors"], a.out, a.log)
    if r["detail"] is not None:
        write_output(r["detail"], r["detail_errors"], a.detail_out, a.detail_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
