# -*- coding: utf-8 -*-
"""
把 web/index.html 构建成「单文件离线版」。

产物把**每个已扫描的盘**的数据都内嵌进去、echarts 内联，可以脱离本地服务、
直接双击（file://）打开；顶上那颗磁盘按钮照常能切盘，切换只是换一份内嵌数据
重新加载，不读磁盘、也不能扫描。实时下钻只在 server 版可用。

两个模式的差别只有数据来源和能力：
    server 模式   数据槽为空，页面去 /api/payload 取 data/ 里的数据源，可调 /api/drill
    离线单文件   drivesData 槽被填满（每盘五件套 + 历史包），只能静态浏览

用法:
    python scripts/build.py
    python scripts/build.py -o "D:\\磁盘空间分析报告.html"
默认输出: dist/磁盘空间分析报告.html

update.bat 刷新完成后默认在后台调用本脚本（update.bat --no-build 可跳过）。
"""
import argparse
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 与服务端、ticket 对账共用 scripts/pathvars.py 这一套占位符展开逻辑
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import pathvars  # noqa: E402
import history   # noqa: E402
import storage   # noqa: E402
import volumes   # noqa: E402

TPL = os.path.join(ROOT, "web", "index.html")
ECHARTS = os.path.join(ROOT, "assets", "echarts.min.js")

_DATA_ROOT = os.path.join(ROOT, "data")
_CONTENT_ROOT = os.path.join(ROOT, "content")

ECHARTS_TAG = '<script src="assets/echarts.min.js"></script>'


def read_json(p):
    """读不到 / 解析失败都返回 None。

    report.json 是可选语义层，缺了页面会按用途库自动生成；links/meta 也可能
    因为留档不完整缺席。缺哪个都不该让整个构建抛异常。
    """
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def history_dir(data_root, cur):
    """某个盘的 history/ 所在目录：data/drives/<KEY>/。

    传 data/ 根进去不会报错，只会静默拿到空包 —— 这个函数就是为了让
    「传哪一层」只有一种写法。
    """
    return storage.data_store(data_root, cur)["dir"]


def embed(obj):
    """序列化进 <script type="application/json">。
    把 < 转成 \\u003c，杜绝 JSON 内容里出现 </script 提前闭合脚本。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def fill(html, slot, payload):
    """往空数据槽里塞内容。用 lambda 是为了躲开 re.sub 对 \\ 的二次解释。"""
    pat = re.compile(r'(<script id="%s" type="application/json">)</script>' % slot)
    if not pat.search(html):
        raise RuntimeError("模板里找不到空数据槽 %s" % slot)
    return pat.sub(lambda m: m.group(1) + payload + "</script>", html, count=1)


_RE_TAG = re.compile(r"<(script|link|img|iframe|source|video|audio|embed|object|track)\b[^>]*>", re.I)
_RE_ATTR = re.compile(r"\b(?:src|href|poster|data|srcset)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_RE_BODY = re.compile(r"(<script\b[^>]*>).*?</script>", re.S | re.I)
_RE_CSSURL = re.compile(r"url\(\s*['\"]?([^)'\"\s]+)", re.I)
_RE_AHREF = re.compile(r"<a\b[^>]*>", re.I)


def audit_selfcontained(html):
    """产物必须是自包含的：没有任何指向外部或本地文件的资源引用。

    只查「会触发一次加载」的东西（script/link/img/iframe/... 的 src/href、
    CSS 的 @import 与 url()），以及指向 http(s) 的 <a>。脚本正文整段剔掉再查：
    echarts 里就有 '<img src="' + ... + '"' 这种字符串拼接（导出图表用），
    不剔会误判成外链。
    """
    shell = _RE_BODY.sub(lambda m: m.group(1) + "</script>", html)
    bad = []
    for tag in _RE_TAG.finditer(shell):
        for a in _RE_ATTR.finditer(tag.group(0)):
            v = a.group(1).strip("\"'").strip()
            if v and not v.startswith(("#", "data:")):
                bad.append("<%s %s>" % (tag.group(1).lower(), v[:70]))
    if "@import" in shell.lower():
        bad.append("css @import")
    for m in _RE_CSSURL.finditer(shell):
        v = m.group(1)
        if not v.startswith(("#", "data:")):
            bad.append("css url(%s)" % v[:60])
    for m in _RE_AHREF.finditer(shell):
        for a in _RE_ATTR.finditer(m.group(0)):
            v = a.group(1).strip("\"'").strip()
            if v.startswith(("http://", "https://", "//")):
                bad.append("<a %s>" % v[:70])
    return bad


def collect_drives(data_root=None, content_root=None):
    """把每个「有树」的盘收成一份内嵌数据。

    返回 {"v":1, "default":"", "order":[KEY...], "drives":{KEY: {...}}}。
    每个盘都带五件套 + 历史包，页面切盘时直接换这一份，不需要再读任何文件。
    没扫描过的盘（没有 tree.json）不收 —— 收进来也是个空壳。
    """
    data_root = data_root or _DATA_ROOT
    content_root = content_root or _CONTENT_ROOT
    vols = {}
    try:
        for v in volumes.list_volumes():
            vols[storage.norm(v.get("root"))] = v
    except Exception as e:
        print("  [warn] 读不到本机磁盘信息：%s" % e)

    out = {"v": 1, "default": "", "order": [], "drives": {}}
    for root in storage.archived(data_root):
        if not storage.has_tree(data_root, root):
            continue
        ds = storage.data_store(data_root, root)
        cs = storage.content_store(content_root, root)
        tree = read_json(ds["tree"])
        if not tree:
            continue
        r = storage.norm(tree.get("root") or root)
        k = storage.key(r)
        # 占位符按这个盘自己的家目录/盘符展开，跨盘不会串味
        ctx = pathvars.context(pathvars.home_of(tree), tree.get("root") or "C:",
                               pathvars.sysdrive_of(tree))
        meta = read_json(ds["meta"]) or {}
        summary = read_json(ds["summary"]) or {}
        vol = vols.get(r) or {}
        item = {
            "key": k,
            "root": r,
            "label": vol.get("label") or "",
            "kind": vol.get("kind") or "fixed",
            "system": bool(vol.get("system")) or
                      storage.norm(meta.get("systemDrive")) == r,
            "at": meta.get("scannedAt") or summary.get("at") or "",
            "capGB": meta.get("capGB") or vol.get("capGB") or 0,
            "usedPct": meta.get("usedPct") or 0,
            "freeGB": meta.get("freeGB") or 0,
            "files": meta.get("fileCount") or 0,
            "dirs": meta.get("dirCount") or 0,
            "tree": tree,
            "links": read_json(ds["links"]),
            "meta": meta,
            "purpose": pathvars.expand(read_json(cs["purpose"]), ctx),
            "report": pathvars.expand(read_json(cs["report"]), ctx),
        }
        try:
            item["hist"] = history.offline_bundle(
                history_dir(data_root, r))
        except Exception as e:
            print("  [warn] %s 的离线历史包生成失败：%s" % (r, e))
            item["hist"] = {"snapshots": [], "ids": [], "diff": None,
                            "totals": [], "movers": {}, "matrix": None}
        out["drives"][k] = item
        out["order"].append(k)
    return out


def main():
    ap = argparse.ArgumentParser(description="构建单文件离线版")
    ap.add_argument("-o", "--out", default=None, help="输出路径，默认 dist\\磁盘空间分析报告.html")
    a = ap.parse_args()

    # 默认显示哪个盘：界面停在哪个就用哪个，其次上次扫描的盘
    cur = storage.read_active(_DATA_ROOT) or storage.read_current(_DATA_ROOT)
    if not cur:
        print("[fatal] 还没有任何盘的留档可以打包")
        print("        先跑一次 update.bat（或 start.bat）把数据扫出来")
        return 1

    # ---- 收全部盘的数据 ----
    pack = collect_drives(_DATA_ROOT, _CONTENT_ROOT)
    if not pack["drives"]:
        print("[fatal] data/drives/ 下没有任何可用的盘（tree.json 一个都没有）")
        print("        跑一次 start.bat —— 启动时会自动扫描、建库、出 ticket")
        return 1
    ck = storage.key(cur)
    pack["default"] = ck if ck in pack["drives"] else pack["order"][0]

    html = io.open(TPL, encoding="utf-8").read()

    # ---- 数据内嵌（所有盘）----
    payload = embed(pack)
    html = fill(html, "drivesData", payload)
    print("  [data] %-10s %6.1f KB（%d 个盘）"
          % ("drivesData", len(payload) / 1024.0, len(pack["order"])))
    for k in pack["order"]:
        d = pack["drives"][k]
        print("         %-4s %-12s 树 %5.1f KB · 用途 %6.1f KB · 快照 %d 张%s"
              % (d["root"], d["label"] or "",
                 len(embed(d["tree"])) / 1024.0,
                 len(embed(d["purpose"])) / 1024.0,
                 len((d.get("hist") or {}).get("snapshots") or []),
                 "（默认）" if k == pack["default"] else ""))

    # ---- echarts 内联 ----
    if ECHARTS_TAG in html:
        if not os.path.exists(ECHARTS):
            print("[fatal] 缺 %s，无法内联图表库" % ECHARTS)
            return 1
        ec = io.open(ECHARTS, encoding="utf-8").read()
        for bad in ("</script", "<!--", "<script"):
            if bad in ec.lower():
                print("[fatal] echarts.min.js 含 %r，内联会破坏页面" % bad)
                return 1
        html = html.replace(ECHARTS_TAG, "<script>" + ec + "</script>", 1)
        print("  [lib]  echarts 内联 %.0f KB" % (len(ec) / 1024.0))
    else:
        print("  [warn] 模板里没有 echarts 外链标签，图表库不会内联")

    # ---- 输出 ----
    out = a.out or os.path.join(ROOT, "dist", "磁盘空间分析报告.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    # 兜底：万一槽没填上，说明模板和脚本对不上
    if re.search(r'<script id="drivesData" type="application/json"></script>', html):
        print("[fatal] drivesData 槽没填上，产物会是空的")
        return 1

    # 兜底：单文件版必须是自包含的（换台机器、断网双击都要能看）
    bad = audit_selfcontained(html)
    if bad:
        print("[fatal] 产物不是自包含的，发现 %d 处外部引用：" % len(bad))
        for x in bad[:8]:
            print("        %s" % x)
        return 1
    print("  [ok]   自包含校验通过：0 处外部引用（脚本、样式、图片全部内联）")

    tmp = out + ".new"
    io.open(tmp, "w", encoding="utf-8").write(html)
    os.replace(tmp, out)
    print("")
    print("  [out]  %s" % out)
    print("         %.0f KB · 双击即可离线打开（file://），顶上磁盘按钮可切盘"
          % (os.path.getsize(out) / 1024.0))
    print("  [warn] 离线单文件不含实时下钻与扫描；需要按目录深挖请用 start.bat 起本地服务")
    print("         生成于 %s" % time.strftime("%Y-%m-%d %H:%M"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
