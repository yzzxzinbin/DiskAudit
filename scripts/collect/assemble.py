# -*- coding: utf-8 -*-
"""
把采集脚本的原始产物整形为「页面直接可用」的数据源文件。

输入（data/.tmp/，由 update-data.ps1 产出）：
    tree-full.json      全盘扫描
    tree-detail.json    指定子目录的细化扫描（可选）
    links-raw.json      链接目标实测（可选）
输出（data/drives/<盘>/，先写 .new 再原子替换，中途失败不动旧数据）：
    tree.json           目录树（全盘 + 细化子树合并后）
    links.json          迁移链接（已剔掉包级 junction）
    meta.json           盘容量 / 扫描时间 / 计数
    drive.json          留档摘要（选盘弹窗读它）

每个盘各写各的目录，换盘扫描互不覆盖；data/current.json 只记「上次扫的是
哪个盘」。全盘扫描最慢，所以 update-data.ps1 每轮都必须产出一份 tree-full.json
（没有增量复用这条路了）。

用法:
    python scripts/collect/assemble.py [--tmp data/.tmp] [--data data] [--drive C:]
"""
import argparse
import io
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import history  # noqa: E402
import storage  # noqa: E402

# 控制台编码（中文 Windows 上是 cp936）装不下某些字符时，宁可降级成 "?"，
# 也别让整个脚本因为一个字符崩掉。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

MB = 1024.0 ** 2
GB = MB * 1024.0


def rd(p):
    if not os.path.exists(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def wr_atomic(path, obj):
    """先写同目录的 .new，再 os.replace（同盘 rename 是原子的）。"""
    tmp = path + ".new"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def fmt(b):
    return ("%.2f GB" % (b / GB)) if b >= GB else ("%d MB" % round(b / MB))


# ---------------------------------------------------------------- 树合并

def build_index(node, path):
    """给每个节点挂上完整路径，返回 path -> node。"""
    idx = {}
    stack = [(node, path)]
    while stack:
        cur, p = stack.pop()
        cur["_p"] = p
        idx[p] = cur
        for k in cur.get("c", []):
            stack.append((k, p + "\\" + k.get("n", "")))
    return idx


def merge_detail(full, detail, detail_root):
    """用更细的子树扫描替换全盘树里对应的那一支。"""
    idx = build_index(full["tree"], full["root"])
    tgt = idx.get(detail_root)
    if tgt is None:
        print("  [warn] 全盘树里没有 %s，跳过细化替换" % detail_root)
        return False
    d_root = detail["tree"]
    d_root["n"] = detail_root.rstrip("\\").split("\\")[-1] or d_root.get("n")
    tgt["b"] = d_root["b"]
    tgt["c"] = d_root.get("c", [])
    print("  [ok] 用细化扫描替换 %s（%s）" % (detail_root, fmt(d_root["b"])))
    return True


def strip_aux(node):
    node.pop("_p", None)
    node.pop("_kids", None)
    for k in node.get("c", []):
        strip_aux(k)


def norm_flags(node):
    """补齐 f=0，前端统一按 f===1 判文件。"""
    node.setdefault("f", 0)
    for k in node.get("c", []):
        norm_flags(k)


# ---------------------------------------------------------------- 链接

def clean_links(raw):
    """剔掉「目标嵌在另一个目标内部」的包级 junction（pnpm/npm 那种），
    只留下真正独立的迁移项。"""
    items = (raw or {}).get("items", [])
    if not items:
        return [], ""

    def norm(s):
        return (s or "").rstrip("\\").lower()

    tgts = [(norm(it.get("target")), it) for it in items]
    kept = []
    for t, it in tgts:
        nested = False
        for t2, _ in tgts:
            if t2 and t2 != t and t.startswith(t2 + "\\"):
                nested = True
                break
        if not nested:
            kept.append(it)

    seen, out = set(), []
    for it in sorted(kept, key=lambda x: -x.get("bytes", 0)):
        k = norm(it.get("link"))
        if k in seen:
            continue
        seen.add(k)
        out.append({
            "l": it.get("link"),
            "k": it.get("kind"),
            "t": it.get("target"),
            "b": float(it.get("bytes", 0)),
            "d": it.get("drive") or "",
        })
    return out, (raw or {}).get("measured", "")


# ---------------------------------------------------------------- 磁盘容量

def total_ram():
    """物理内存总量（字节）。走 kernel32.GlobalMemoryStatusEx，非 Windows 返回 0。

    报告里的「休眠文件 ≈ 物理内存的 40%」这类判断需要它，而它不属于文件扫描结果，
    所以单独取一次 —— 不依赖 WMI / PowerShell，纯系统 API。
    """
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return float(st.ullTotalPhys)
    except Exception:
        pass
    return 0.0


def scan_stamp(tmp_dir):
    """扫描时间 = 中间产物的最晚修改时间。

    取整形那一刻的时钟会让这个字段随重跑漂移：数据还是几天前采的，报告上却
    写着今天。中间产物的 mtime 才是采集时刻。
    """
    best = 0.0
    for f in ("tree-full.json", "tree-detail.json", "links-raw.json"):
        p = os.path.join(tmp_dir, f)
        if os.path.exists(p):
            best = max(best, os.path.getmtime(p))
    if best > 0:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(best))
    return time.strftime("%Y-%m-%d %H:%M")


def disk_usage(drive):
    """返回 (总, 已用, 剩余) 字节数。"""
    root = drive if drive.endswith("\\") else drive + "\\"
    try:
        t, u, f = shutil.disk_usage(root)
        return float(t), float(u), float(f)
    except Exception:
        return 0.0, 0.0, 0.0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--tmp", default=os.path.join(here, "data", ".tmp"))
    ap.add_argument("--data", default=os.path.join(here, "data"))
    ap.add_argument("--drive", default=os.environ.get("SystemDrive") or "C:")
    # 空串表示这次没做细化扫描。PS 5.1 会把空的字符串参数整个吞掉，
    # 调用方（update-data.ps1）在没细化时压根不传这个参数。
    ap.add_argument("--detail-root", default="")
    ap.add_argument("--home", default=os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    ap.add_argument("--system-drive", default=os.environ.get("SystemDrive") or "C:")
    ap.add_argument("--elapsed", type=float, default=0.0)
    ap.add_argument("--keep-snapshots", type=int, default=history.DEFAULT_KEEP,
                    help="每个盘的 history/ 里保留多少张快照，0 = 不限")
    a = ap.parse_args()

    # 参数归一化：盘符去掉尾部分隔符，细化根统一成反斜杠（树里的路径都是反斜杠拼接的）。
    # 这样从命令行、环境变量、或别的脚本传进来的写法不一致也不会静默失配。
    a.drive = a.drive.rstrip("\\/") or "C:"
    a.detail_root = (a.detail_root or "").replace("/", "\\").rstrip("\\")
    a.home = (a.home or "").replace("/", "\\").rstrip("\\")
    a.system_drive = (a.system_drive or "C:").strip().rstrip("\\/") or "C:"

    t0 = time.time()
    full = rd(os.path.join(a.tmp, "tree-full.json"))
    if full is None:
        print("[fatal] 缺少 tree-full.json，采集没跑成功")
        return 1
    detail = rd(os.path.join(a.tmp, "tree-detail.json"))
    links_raw = rd(os.path.join(a.tmp, "links-raw.json"))

    # --- 树 ---
    if detail:
        merge_detail(full, detail, a.detail_root)
    strip_aux(full["tree"])
    norm_flags(full["tree"])

    tree_payload = {
        "root": full["root"],
        "rootLabel": full.get("rootLabel", full["root"] + "\\"),
        "depth": full.get("depth"),
        "fileMinMB": full.get("fileMinMB"),
        "minMB": full["minMB"],
        "minMBSub": (detail or full)["minMB"],
        "subRoot": a.detail_root,
        "home": a.home,
        "systemDrive": a.system_drive,
        "totalBytes": full["totalBytes"],
        "fileCount": full["fileCount"],
        "dirCount": full["dirCount"],
        "skipped": full.get("skipped", []),
        "tree": full["tree"],
    }

    # --- 链接 ---
    links, measured = clean_links(links_raw)
    cap, used, free = disk_usage(a.drive)
    cap_gb = round(cap / GB, 2) if cap else 0.0
    ram = total_ram()
    link_payload = {
        "capGB": cap_gb,
        "measured": measured,
        "items": links,
    }

    # --- meta ---
    scanned = scan_stamp(a.tmp)
    meta = {
        "scannedAt": scanned,
        "scanDate": scanned[:10],
        "drive": a.drive,
        "systemDrive": a.system_drive,
        "capGB": cap_gb,
        "usedGB": round(used / GB, 2) if cap else 0.0,
        "freeGB": round(free / GB, 2) if cap else 0.0,
        "usedPct": round(used / cap * 100, 1) if cap else 0.0,
        "freePct": round(free / cap * 100, 1) if cap else 0.0,
        "fileCount": full["fileCount"],
        "dirCount": full["dirCount"],
        "linkCount": len(links),
        "scanSec": round(a.elapsed, 1),
        "ramGB": round(ram / GB, 2) if ram else 0.0,
    }

    # ---- 落盘前最后一道：扫描根必须是盘符 ----
    # 扫目录（-Drive D://Work）会落出 data/drives/DWORK/，而选盘弹窗只列本机
    # 真实盘符 —— 这份留档在界面上永远看不见、切不到、也删不掉，事后只能手工清。
    # 闸门放在这里而不是 app.main：ps1、assemble 都能被直接调用。
    root_name = tree_payload.get("root") or ""
    if not re.match(r"^[A-Za-z]:$", root_name):
        print("[fatal] 扫描根不是盘符：%r —— 只支持整盘扫描（-Drive D:），"
              "要细化子目录用 -DetailRoot" % (root_name,))
        return 1

    # ---- 落盘：只写这个盘自己的目录 ----
    # 三件套按盘各存一份（data/drives/<盘>/），data/ 根下不放：
    # 换盘扫描互不覆盖；current.json 只记「上次扫的是哪个盘」。
    st = history.drive_store(a.data, tree_payload.get("root"))
    os.makedirs(st["dir"], exist_ok=True)
    wr_atomic(st["tree"], tree_payload)
    wr_atomic(st["links"], link_payload)
    wr_atomic(st["meta"], meta)
    wr_atomic(st["summary"], {
        "root": tree_payload.get("root"),
        "drive": meta.get("drive"),
        "at": meta.get("scannedAt"),
        "total": tree_payload.get("totalBytes"),
        "files": meta.get("fileCount"),
        "dirs": meta.get("dirCount"),
    })
    storage.write_current(a.data, tree_payload.get("root"))

    # ---- 快照归档 ----
    # 数字层已经原子落位，这里把同一份 payload 记进该盘的 history/。
    # 快照失败只警告：档案是锦上添花，不能连坐一次成功的刷新。
    try:
        deep = {"d": full.get("deepDirs") or {}, "f": full.get("deepFiles") or {}}
        sid = history.record(st["dir"], tree_payload, link_payload, meta,
                             deep=deep, keep=a.keep_snapshots)
        print("  [snap]  快照 %s 已归档（现存 %d 张，drives\\%s\\history\\，深索引 %d 目录 / %d 文件）"
              % (sid, len(history.snapshots(st["dir"])), storage.key(tree_payload.get("root")),
                 len(deep["d"]), len(deep["f"])))
    except Exception as e:
        print("  [warn] 快照归档失败（不影响本次数据）：%s" % e)

    off = [x for x in links if (x["d"] or "").upper() != a.drive.rstrip(":\\/").upper()]
    print("  [tree]  顶层 %d 项 · 总计 %s · %d 文件 / %d 目录"
          % (len(full["tree"].get("c", [])), fmt(full["totalBytes"]),
             full["fileCount"], full["dirCount"]))
    print("  [links] %d 条（迁出 %d 条，%s）"
          % (len(links), len(off), fmt(sum(x["b"] for x in off))))
    print("  [meta]  %s %.2f GB / 总 %.2f GB（已用 %.1f%%）"
          % (a.drive, used / GB, cap / GB, meta["usedPct"]))
    print("  [time]  整形 %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
