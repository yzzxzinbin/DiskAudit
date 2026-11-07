# -*- coding: utf-8 -*-
"""按盘存储的路径层：每个盘各成一份，切盘只换路径，不写任何文件。

    data/
      current.json                    上次扫描的盘（只有扫描会改写它）
      active.json                     界面正在看哪个盘（切盘写它）
      drives/<KEY>/tree.json          目录树
      drives/<KEY>/links.json         迁移链接
      drives/<KEY>/meta.json          盘容量与计数
      drives/<KEY>/drive.json         留档摘要（列表页读它）
      drives/<KEY>/history/           快照
    content/
      linkextra.json                  链接例外，跨盘通用
      drives/<KEY>/purpose.json       目录用途
      drives/<KEY>/report.json        报表语义
      drives/<KEY>/ticket.md         待补说明

<KEY> 由盘根去掉非字母数字字符后转大写得到（C: → C）。

「扫描」和「在看」是两件事，所以有两个指针：current.json 记上次扫的是谁
（决定默认读路径、build 默认盘、--no-ask 的扫描目标），active.json 记界面
停在哪个盘（切盘写它，下次打开就还停在那儿）。切盘不碰任何数据文件，
只更新这一个小指针；history 索引的自愈重写是另一处例外。
"""
import json
import os
import re


def norm(root):
    """盘根归一：c、c:、C:\\ 一律成 C:。非盘符路径原样保留（去尾斜杠）。"""
    s = (root or "").strip().replace("/", "\\")
    m = re.match(r"^([A-Za-z]):?$", s.rstrip("\\"))
    if m:
        return m.group(1).upper() + ":"
    return s.rstrip("\\")


def key(root):
    """盘根 → 目录名。只留字母数字下划线并统一大写，杜绝路径注入。"""
    k = re.sub(r"[^A-Za-z0-9_]", "", (root or "")).upper()
    return k or "ROOT"


def data_store(data_root, root):
    """data/drives/<KEY>/ 下的全部文件路径。"""
    d = os.path.join(data_root, "drives", key(root))
    return {
        "dir": d,
        "tree": os.path.join(d, "tree.json"),
        "links": os.path.join(d, "links.json"),
        "meta": os.path.join(d, "meta.json"),
        "summary": os.path.join(d, "drive.json"),
        "history": os.path.join(d, "history"),
    }


def content_store(content_root, root):
    """content/drives/<KEY>/ 下的全部文件路径。"""
    d = os.path.join(content_root, "drives", key(root))
    return {
        "dir": d,
        "purpose": os.path.join(d, "purpose.json"),
        "report": os.path.join(d, "report.json"),
        "ticket": os.path.join(d, "ticket.md"),
    }


# ---------------------------------------------------------------- 当前盘指针

def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def archived(data_root):
    """有留档的盘根列表，按盘符排序。"""
    d = os.path.join(data_root, "drives")
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        j = _read_json(os.path.join(d, name, "drive.json"))
        if j and j.get("root"):
            out.append(j["root"])
    return out


def has_tree(data_root, root):
    """这个盘的留档树在不在 —— 判断「有没有可用数据」的唯一口径。

    只认 tree.json：drive.json 是摘要，删到一半会剩它；history/ 更是可以
    在留档删掉之后继续存在。两者都不能当「有数据可看」。
    """
    return os.path.isfile(data_store(data_root, root)["tree"])


def read_current(data_root):
    """上次扫描的盘根。

    记的那个盘必须真有 tree.json 才算数。current.json 只是「上次扫的是谁」，
    目录被手工搬走、删掉，或者留档被清过一次，照旧返回它只会让启动流程
    指着一堆不存在的文件，页面永久 503 还找不到原因。
    依次退：current.json → 扫描时间最新且树还在的盘 → 空串（当作首次运行）。
    """
    j = _read_json(os.path.join(data_root, "current.json"))
    if j and j.get("root") and has_tree(data_root, j["root"]):
        return norm(j["root"])
    best, at = "", ""
    d = os.path.join(data_root, "drives")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            s = _read_json(os.path.join(d, name, "drive.json"))
            if not s or not s.get("root") or not has_tree(data_root, s["root"]):
                continue
            t = str(s.get("at") or "")
            if t >= at:
                best, at = norm(s["root"]), t
    return best


def write_current(data_root, root):
    """记录刚扫完的盘。切盘查看不会调它。"""
    p = os.path.join(data_root, "current.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"root": norm(root)}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ---------------------------------------------------------------- 活动盘指针

def read_active(data_root, fallback=""):
    """界面正在看哪个盘；没有记录或留档已经不在时返回 fallback。

    和 read_current 一样要验 tree：留档被删掉之后指针必须自己失效，
    否则下次打开页面就是一个指向空目录的 404。
    """
    j = _read_json(os.path.join(data_root, "active.json"))
    if j and j.get("root") and has_tree(data_root, j["root"]):
        return norm(j["root"])
    return norm(fallback)


def write_active(data_root, root):
    """记下界面切到了哪个盘。没有树的盘不记 —— 记了下次也打不开。"""
    r = norm(root)
    if not r or not has_tree(data_root, r):
        return False
    p = os.path.join(data_root, "active.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"root": r}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return True
