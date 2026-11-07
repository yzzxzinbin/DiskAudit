# -*- coding: utf-8 -*-
"""DiskAudit 应用入口：启动编排 + 本地服务。

启动时按顺序做四件事，然后才起服务：
    ① 没有扫描数据 → 初始化知识库、执行扫描（首次运行）
    ② 知识库 / 报告骨架缺失 → 补上
    ③ ticket（ticket.md）被改过 → 解析并导入 content/
    ④ 还有缺口 → 重新生成 ticket

服务本身只做几件事：
    /            → web/index.html（页面骨架，数据另外取）
    /api/payload → 一次取齐 tree / links / meta / purpose / report
    /api/drill  → 按目录实时下钻一层（只读，不落盘）
    /api/volumes → 本机磁盘清单，页面据此选盘
    /api/scan    → 起一次后台扫描（POST），查进度（GET）
数据每次请求现读磁盘，所以补完 ticket 重启一次就能看到新内容。

用法:
    python scripts/app.py                 # 默认 127.0.0.1:8765，自动开浏览器
    python scripts/app.py --update        # 刷新数据/文字层，先问扫哪个盘
    python scripts/app.py --update --drive D:    # 指定盘，不问
    python scripts/app.py --update --skip-scan   # 只刷新文字层
    python scripts/app.py --update --no-build    # 刷新但不生成离线版
    python scripts/app.py --rescan        # 先重扫再起服务（同样会问盘）
    python scripts/app.py --port 9000
    python scripts/app.py --update --no-ask      # 脚本里调用：不问，用默认盘
"""
import argparse
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))     # scripts/
ROOT = os.path.dirname(HERE)                          # 产品根目录
WEB = os.path.join(ROOT, "web")
ASSETS = os.path.join(ROOT, "assets")
DATA = os.path.join(ROOT, "data")
CONTENT = os.path.join(ROOT, "content")

sys.path.insert(0, HERE)
import drill         # noqa: E402
import history       # noqa: E402
import pathvars      # noqa: E402
import purpose       # noqa: E402
import report as report_mod   # noqa: E402
import storage       # noqa: E402
import ticket        # noqa: E402
import volumes       # noqa: E402

MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".map": "application/json; charset=utf-8",
}

# 数据源清单：接口名 -> 文件路径。
# 每个盘各自一份（data/drives/<盘>/ 与 content/drives/<盘>/），切盘时由
# use_drive() 重新填这张表。这里没有令牌，也没有「根目录那套」，只有一份真源。
SOURCES = {}


def use_drive(root=None):
    """把全局读路径切到某个盘，返回 (数据目录, 内容目录, 盘根)。

    下游模块（report / ticket / history）都按目录拼路径，所以把
    data/drives/<盘> 当成 data 传下去，它们一行都不用改就自动按盘了。
    root 为空时取 current.json 记录的盘 —— 即上次扫完的那个。

    整表替换而不是逐项 update：刷新文字层是在后台线程里调的，逐项写会让
    正在读的请求拼出「tree 是 A 盘、purpose 是 B 盘」的混合视图。替换之后
    已经取到旧表的请求继续用旧表，自始至终看到的都是同一个盘。
    """
    global SOURCES
    r = storage.norm(root) or storage.read_current(DATA)
    ds = storage.data_store(DATA, r)
    cs = storage.content_store(CONTENT, r)
    SOURCES = {
        "tree": ds["tree"],
        "links": ds["links"],
        "meta": ds["meta"],
        "purpose": cs["purpose"],
        "report": cs["report"],
    }
    return ds["dir"], cs["dir"], r

# 页面渲染必需的四项；缺任意一项都算「数据源没准备好」
CORE_KEYS = ("tree", "links", "meta", "purpose")

# report 是语义层（描述 / 评级 / 步骤），缺失不算错误 —— 页面会按用途库自动生成
PAYLOAD_KEYS = CORE_KEYS + ("report",)

# ---- 扫描任务 ----
# 同一时刻只允许一个：全盘采集本身就是满负荷 I/O，排队只会让两个任务都变慢。
# busy 覆盖「扫描 + 刷新文字层」整段，页面据此禁用按钮；phase 由 worker 维护，
# 扫描进行中的细分阶段则从日志尾实时推（scan_phase）。
SCAN_STATE = {
    "proc": None,
    "busy": False,
    "drive": "",
    "detail": "",
    "log": "",
    "started": 0.0,
    "ended": 0.0,
    "rc": None,
    "phase": "idle",
    "aborted": False,
    "error": "",
}
SCAN_LOCK = threading.Lock()
SCAN_LOG_DIR = os.path.join(DATA, ".log")


def _console_bytes(text):
    """日志要和控制台输出保持同一种编码，否则一份文件里两种编码，
    解码时必然有一段是乱的。"""
    if os.name == "nt":
        return text.encode("mbcs", "replace")
    return text.encode("utf-8", "replace")


def _console_text(raw):
    """采集脚本的输出可能是 UTF-8，也可能跟着控制台代码页走。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", "replace")


def _log_tail(path, nbytes=6000, max_lines=14):
    """日志尾若干行，够看出卡在哪一步就行。"""
    if not path or not os.path.exists(path):
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
            raw = f.read()
    except OSError:
        return ""
    lines = [l.rstrip() for l in _console_text(raw).splitlines() if l.strip()]
    return "\n".join(lines[-max_lines:])


def scan_view():
    """给页面的任务快照。"""
    with SCAN_LOCK:
        st = dict(SCAN_STATE)
    busy = bool(st["busy"])
    tail = _log_tail(st["log"])
    if busy and st["phase"] == "scan":
        phase = volumes.scan_phase(tail)
    else:
        phase = st["phase"]
    end = time.time() if busy else (st["ended"] or 0.0)
    return {
        "running": busy,
        "drive": st["drive"],
        "detailRoot": st["detail"],
        "phase": phase,
        "phaseLabel": volumes.PHASE_LABEL.get(phase, phase),
        "elapsed": round(end - st["started"], 1) if st["started"] and end else 0.0,
        "startedAt": (time.strftime("%H:%M:%S", time.localtime(st["started"]))
                      if st["started"] else ""),
        "rc": None if busy else st["rc"],
        "aborted": bool(st["aborted"]),
        "error": st["error"],
        "log": tail,
    }


def kill_scan():
    """中止扫描。采集本身是只读的，中途结束只会在 data/.tmp 留下中间产物，
    下一轮开头会清掉；data/ 里的正式数据直到整形成功才会被替换。"""
    with SCAN_LOCK:
        # 只在采集进程还活着时允许中止。整形之后数据已经落位，此时杀掉自己
        # 反而会留下「数据换了、文字层没刷」的半成品；PID 也可能已经被复用。
        if not SCAN_STATE["busy"] or SCAN_STATE["phase"] != "scan" \
                or SCAN_STATE["proc"] is None:
            return False
        SCAN_STATE["aborted"] = True
        proc = SCAN_STATE["proc"]
    if os.name == "nt":
        # powershell 下面还挂着 python，terminate 只杀外层，必须连树一起
        subprocess.call(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.terminate()
    return True


def _scan_finish(proc, logf):
    """采集进程结束后的收尾：刷新文字层，落终态。"""
    rc = proc.wait()
    try:
        logf.write(_console_bytes("===== 退出码 %d =====\n" % rc))
        logf.close()
    except OSError:
        pass

    with SCAN_LOCK:
        aborted = SCAN_STATE["aborted"]
        SCAN_STATE["rc"] = rc
        SCAN_STATE["phase"] = "aborted" if aborted else ("post" if rc == 0 else "failed")

    if rc == 0 and not aborted:
        try:
            ensure(True, quiet=True)
        except Exception as e:
            with SCAN_LOCK:
                SCAN_STATE["error"] = "文字层刷新失败：%s" % e
        # 刚扫完的盘就是用户此刻该看的盘：活动盘指针跟着挪过去，
        # 否则下次打开首页还停在扫描前停留的那个盘。
        with SCAN_LOCK:
            just = SCAN_STATE["drive"]
        if just:
            try:
                storage.write_active(DATA, just)
            except OSError as e:
                sys.stderr.write("[warn] 记活动盘失败：%s\n" % e)

    with SCAN_LOCK:
        SCAN_STATE["ended"] = time.time()
        SCAN_STATE["busy"] = False
        if SCAN_STATE["phase"] == "post":
            SCAN_STATE["phase"] = "done"


def read_json(path):
    """读不到 / 解析失败都返回 None，让页面自己决定怎么提示。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        sys.stderr.write("[warn] 读不了 %s：%s\n" % (path, e))
        return None


def _norm_root(r):
    """盘根归一：去空白与冒号/斜杠、统一大写 —— 查询参数与留档键的比较基准。"""
    return re.sub(r"[\s:/\\]+", "", (r or "").strip()).upper()


def view_root(rq=""):
    """这个请求要看哪个盘：URL 指定的 → 界面停的那个 → 上次扫描的。

    中间那层（active.json）不能省：current.json 记「扫描过哪个盘」，只由扫描
    改写，切盘不动它。两个指针合成一个，切盘后关掉页面再打开就会跳回上次扫描
    的盘 —— 用户看到的就是「切盘没记住」。
    """
    r = storage.norm(rq)
    if r:
        return r
    return storage.read_active(DATA) or storage.read_current(DATA)


def sources_state():
    """数据源新鲜度快照，用于 /api/health 和启动横幅。"""
    out = {}
    for k, p in SOURCES.items():
        if os.path.exists(p):
            st = os.stat(p)
            out[k] = {
                "ok": True,
                "kb": round(st.st_size / 1024.0, 1),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            }
        else:
            out[k] = {"ok": False, "kb": 0, "mtime": ""}
    return out


def drives_payload(active=None):
    """data/drives/ 里每个盘的留档摘要 + 当前盘标记。

    留档是 assemble.py 扫描时落下的，所以「列出来的盘」比本机实际盘少 ——
    页面和命令行选盘都要再和 volumes.list_volumes() 对一次。

    active 是「调用方正在查看的盘」，必须按请求算（?root= → active.json）。
    不能拿进程启动时的 SOURCES 顶替：切盘只跳 ?root=、SOURCES 不动，混用会
    让页面明明在看 K 盘，响应却把 D 标成「当前」，连带放出 K 的删除按钮。
    """
    out = []
    droot = os.path.join(DATA, "drives")
    if os.path.isdir(droot):
        for san in sorted(os.listdir(droot)):
            j = read_json(os.path.join(droot, san, "drive.json"))
            if not j or not j.get("root"):
                continue
            # 只剩 drive.json、没有 tree.json 的残档（删到一半 / 手工动过目录）
            # 不能当可切换的留档：点进去必然 404。
            j["broken"] = not os.path.exists(
                os.path.join(droot, san, "tree.json"))
            out.append(j)
    if active is None:
        src = SOURCES
        atree = read_json(src.get("tree"))
        active = (atree or {}).get("root") or ""
    for j in out:
        j["current"] = _norm_root(j.get("root")) == _norm_root(active)
    out.sort(key=lambda j: _norm_root(j.get("root")))
    return {"drives": out, "active": active}


def delete_block_reason(target, viewed, default):
    """某个盘的留档能不能删。返回 None 表示可以，否则是拒绝理由。

    viewed  —— 发起这次删除的页面正在看哪个盘（前端带 view=）
    default —— 服务端默认盘（current.json 那个）

    两个都不许删：删 viewed 会让那个页面下一次取数就 404；删 default 会让
    重启后的默认视图没有数据。只校验 default 不够 —— default 往往不是
    用户正在看的那块盘。
    """
    t = _norm_root(target)
    if not t:
        return "缺少 root 参数"
    if t == _norm_root(viewed):
        return "这是当前正在查看的盘，先切到别的盘再来删它的留档"
    if t == _norm_root(default):
        return "这是默认盘的留档（current.json 记的那个），删了下次启动就没有数据可看"
    return None



class Handler(BaseHTTPRequestHandler):
    server_version = "DiskAudit/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础工具 ----

    def _send(self, code, body, ctype="application/json; charset=utf-8", head_only=False,
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, obj, code=200, head_only=False):
        self._send(code, json.dumps(obj, ensure_ascii=False), head_only=head_only)

    def _err(self, code, msg, head_only=False, extra_headers=None):
        self._send(code, json.dumps({"error": msg}, ensure_ascii=False), head_only=head_only,
                   extra_headers=extra_headers)

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    def _body(self, limit=8192):
        """读 JSON 请求体。无论大小都要把 body 读干净，否则 keep-alive 下一条会串包。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return None
        raw = self.rfile.read(n)
        if n > limit:
            return None
        try:
            v = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        return v if isinstance(v, dict) else None

    # ---- 路由 ----

    def do_GET(self):
        self._route(head_only=False)

    def do_HEAD(self):
        self._route(head_only=True)

    def do_POST(self):
        self._route(head_only=False)

    def _route(self, head_only):
        path = unquote(urlparse(self.path).path)
        if path.startswith("/api/"):
            name = path[len("/api/"):].strip("/")
            if self.command == "POST" and not name.startswith("scan"):
                return self._err(405, "接口 /api/%s 不接受 POST" % name, head_only,
                                 extra_headers={"Allow": "GET"})
            return self._api(name, head_only)
        if self.command == "POST":
            return self._err(405, "只接受 GET", head_only, extra_headers={"Allow": "GET"})
        return self._static(path, head_only)

    # ---- 动态下钻的本地来源校验 ----

    @staticmethod
    def _loopback(addr):
        try:
            ip = ipaddress.ip_address(addr)
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            return ip.is_loopback
        except Exception:
            return False

    @staticmethod
    def _host_name(value):
        value = (value or "").strip()
        if not value:
            return ""
        if value.startswith("["):
            end = value.find("]")
            return value[1:end].lower() if end > 0 else value.lower()
        return value.rsplit(":", 1)[0].lower() if ":" in value else value.lower()

    def _local_only(self):
        if not self._loopback(self.client_address[0]):
            return False
        host = self._host_name(self.headers.get("Host"))
        if host not in ("localhost", "127.0.0.1", "::1"):
            return False
        ref = self.headers.get("Referer") or ""
        if ref:
            refhost = (urlparse(ref).hostname or "").lower()
            if not refhost or refhost != host:
                return False
        return True

    # ---- 数据接口 ----

    def _api(self, name, head_only):
        name = name.strip("/")
        # 取一次快照，整个请求只用这一份 —— 后台刷新文字层会整表换掉 SOURCES
        src = SOURCES

        if name in ("health", "status"):
            st = sources_state()
            return self._json({
                "ok": True,
                "service": "DiskAudit",
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "dataDir": DATA,
                "sources": st,
                "ready": all(st[k]["ok"] for k in CORE_KEYS),
            }, head_only=head_only)

        if name == "payload":
            q = parse_qs(urlparse(self.path).query)
            rq = (q.get("root") or [""])[0].strip()
            pack = {k: read_json(src.get(k)) for k in ("tree", "links", "meta")}
            cur = storage.norm((pack["tree"] or {}).get("root"))
            # 没带 ?root= 就看「界面上次停在哪个盘」，而不是「上次扫描的盘」。
            # 带 ?root= 指向别的盘：五件套全部换成那个盘目录里的，并把这个
            # 选择记进 active.json —— 下次直接打开首页还停在这儿。
            want = storage.norm(rq) or view_root()
            if want and want != cur:
                ds = storage.data_store(DATA, want)
                cs = storage.content_store(CONTENT, want)
                t2 = read_json(ds["tree"])
                if t2 is None or storage.norm(t2.get("root")) != want:
                    if rq:
                        return self._err(404, "没有这个盘的留档数据：%s" % want, head_only)
                    want = cur            # 活动盘指针失效：退回默认盘，别让页面打不开
                else:
                    pack["tree"] = t2
                    pack["links"] = read_json(ds["links"])
                    pack["meta"] = read_json(ds["meta"])
                    pack["purpose"] = read_json(cs["purpose"])
                    pack["report"] = read_json(cs["report"])
            if not want or want == cur:
                pack["purpose"] = read_json(src.get("purpose"))
                pack["report"] = read_json(src.get("report"))
            if storage.norm(rq):
                storage.write_active(DATA, rq)
            missing = [k for k in CORE_KEYS if pack[k] is None]
            if pack["tree"] is None:
                return self._err(503, "数据源还没生成：%s 不存在。"
                                      "跑一次 update.bat 重扫。" % src.get("tree"),
                                 head_only)
            if missing:
                sys.stderr.write("[warn] payload 里缺 %s\n" % ", ".join(missing))
            return self._json(pack, head_only=head_only)

        if name in src:
            # 单文件接口只有「默认盘」一套语义：要按盘取，走 /api/payload?root=
            v = read_json(src.get(name))
            if v is None:
                return self._err(404, "没有数据源 %s，跑一次 update.bat" % name, head_only)
            return self._json(v, head_only=head_only)

        if name == "drill":
            return self._drill(head_only)

        if name == "open":
            return self._open_dir(head_only)

        if name == "drives":
            return self._drives(head_only)

        if name == "drives/delete":
            return self._drives_delete(head_only)

        if name == "volumes":
            return self._volumes(head_only)

        if name == "scan":
            if self.command == "POST":
                return self._scan_start(head_only)
            return self._json(scan_view(), head_only=head_only)

        if name == "scan/abort":
            return self._scan_abort(head_only)

        if name == "history" or name.startswith("history/"):
            return self._history(name[len("history"):].strip("/"), head_only)

        return self._err(404, "未知接口 /api/%s" % name, head_only)

    # ---- 多盘数据源 ----

    def _drives_payload(self, active=None):
        return drives_payload(active)

    def _drives(self, head_only):
        """/api/drives —— data/drives/ 里每个盘的留档摘要 + 当前盘标记。

        ?root= 指定「页面正在看哪个盘」，缺省才用服务端默认盘。
        与历史接口同一道 _local_only 校验（外层已做）。"""
        q = parse_qs(urlparse(self.path).query)
        rq = storage.norm((q.get("root") or [""])[0].strip())
        return self._json(self._drives_payload(rq or view_root() or None),
                          head_only=head_only)

    def _drives_delete(self, head_only):
        """/api/drives/delete?root=<要删的盘>&view=<页面正在看的盘> → 删留档（数字层）。

        破坏性接口，三道闸：只绑 127.0.0.1 + _local_only（本机页面）+
        同源 Referer（与 /api/open 同级）。只动 data/ 下的留档，
        永不碰被扫描的盘本身。

        只删 tree / links / meta / drive 四个文件，history/ 原样保留：
        快照是不可再生的历史记录，删留档（重扫即可再生成）不该把它连坐。
        不能 rmtree 整个目录：那样会把 forget() 刚移进 _trash 的快照一起抹掉。

        view 是「页面正在看哪个盘」，要按请求算：切盘只跳 ?root=、SOURCES
        不动，拿 SOURCES 当「正在查看」会让正在看的那个盘的删除按钮照常放行，
        删完页面立刻 404。
        """
        if head_only:
            return self._json({"ok": True}, head_only=True)
        if not self._local_only():
            return self._err(403, "只允许本机页面调用删除接口")
        ref = self.headers.get("Referer") or ""
        if ref:
            host = urlparse(ref).netloc.split("@")[-1]
            if host and host != self.headers.get("Host"):
                return self._err(403, "跨站调用被拒绝")

        q = parse_qs(urlparse(self.path).query)
        rq = (q.get("root") or [""])[0].strip()
        view = (q.get("view") or [""])[0].strip()
        if not rq:
            return self._err(400, "缺少 root 参数")

        st = history.drive_store(DATA, rq)
        if not os.path.isdir(st["dir"]):
            return self._err(404, "没有这个盘的留档：%s" % rq)

        src = SOURCES
        atree = read_json(src.get("tree"))
        default = (atree or {}).get("root") or ""
        reason = delete_block_reason(rq, view or view_root() or default, default)
        if reason:
            return self._err(400, reason)

        for name in ("tree.json", "links.json", "meta.json", "drive.json"):
            p = os.path.join(st["dir"], name)
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError as e:
                return self._err(500, "删留档失败：%s" % e)
        has_hist = os.path.isdir(history.hdir(st["dir"]))
        if not has_hist:
            try:
                os.rmdir(st["dir"])
            except OSError:
                pass
        p = self._drives_payload(view or None)
        return self._json({"ok": True, "keptHistory": has_hist,
                           "drives": p["drives"], "active": p["active"]},
                          head_only=head_only)

    # ---- 选盘与扫描 ----

    def _volumes(self, head_only):
        """/api/volumes —— 本机磁盘 + 每个盘的留档情况 + 当前任务。

        页面上的「选择磁盘」全靠它：本机有哪些盘、扫过没有、正在看哪个。
        留档信息来自 data/drives/，扫过才有；没扫过的盘 archive 为 null。

        ?root= 是「页面正在看哪个盘」，前端切盘时带上。缺省才回落到服务端
        默认盘 —— 否则在看 K 盘的页面里，弹窗会把 D 盘标成「当前」。
        """
        q = parse_qs(urlparse(self.path).query)
        rq = storage.norm((q.get("root") or [""])[0].strip())
        # 一次取表算出 active 与 default：分两次读 SOURCES 的话，后台换表插在
        # 中间就会给出「active 是 A 盘、default 是 B 盘」的混搭响应。
        src = SOURCES
        default = (read_json(src.get("tree")) or {}).get("root") or ""
        payload = self._drives_payload(rq or view_root() or default)
        arch = {}
        for j in payload["drives"]:
            # 残档（只剩 drive.json）不给「切换」入口，点进去必然 404
            arch[_norm_root(j.get("root"))] = None if j.get("broken") else j
        active = payload["active"]
        out = []
        for v in volumes.list_volumes():
            item = dict(v)
            item["archive"] = arch.get(_norm_root(v["root"])) or None
            item["current"] = _norm_root(v["root"]) == _norm_root(active)
            # 默认盘（current.json 那个）删了下次启动就没数据，界面上不给这颗按钮
            item["default"] = _norm_root(v["root"]) == _norm_root(default)
            out.append(item)
        return self._json({"volumes": out, "active": active, "default": default,
                           "scan": scan_view()},
                          head_only=head_only)

    def _scan_start(self, head_only):
        """POST /api/scan {"drive":"D:"} → 后台起一次扫描。

        只认本机真实存在的固定盘，不接受路径 —— 这个接口的语义是「换个盘重扫」，
        不是「扫任意目录」。扫完接着刷新文字层：换盘后语义层里的路径全指向旧盘，
        不重建的话 A 类清单会被算成 0。
        """
        if head_only:
            return self._json({"ok": True}, head_only=True)
        if not self._local_only():
            return self._err(403, "只允许本机页面发起扫描")
        ref = self.headers.get("Referer") or ""
        if ref:
            host = urlparse(ref).netloc.split("@")[-1]
            if host and host != self.headers.get("Host"):
                return self._err(403, "跨站调用被拒绝")

        drive = volumes.norm_drive((self._body() or {}).get("drive"))
        if not drive:
            return self._err(400, "缺少有效的盘符")
        vol = None
        for v in volumes.list_volumes():
            if v["root"] == drive:
                vol = v
                break
        if vol is None:
            return self._err(404, "本机没有这个盘：%s" % drive)
        if vol["kind"] != "fixed":
            return self._err(400, "%s 是可移动盘，插拔会中断扫描" % drive)

        with SCAN_LOCK:
            if SCAN_STATE["busy"]:
                return self._err(409, "已有扫描在跑：%s" % SCAN_STATE["drive"])
            SCAN_STATE["busy"] = True
            SCAN_STATE["drive"] = drive
            SCAN_STATE["phase"] = "scan"

        detail = volumes.detail_root_for(drive)
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", os.path.join(HERE, "collect", "update-data.ps1"),
               "-Drive", drive]
        if detail:
            cmd += ["-DetailRoot", detail]
        else:
            # 别的盘没有天然候选：不给 -NoDetail，脚本会拿家目录当细化根，
            # 而家目录不在这个盘上，白扫一遍再丢掉。
            cmd.append("-NoDetail")

        os.makedirs(SCAN_LOG_DIR, exist_ok=True)
        log_p = os.path.join(SCAN_LOG_DIR, "scan-%s.log" % time.strftime("%Y%m%d_%H%M%S"))
        logf = None
        try:
            logf = open(log_p, "ab", buffering=0)
            logf.write(_console_bytes("\n===== %s  扫描 %s =====\n"
                                      % (time.strftime("%Y-%m-%d %H:%M:%S"), drive)))
            proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                                    stdout=logf, stderr=subprocess.STDOUT)
        except Exception as e:
            if logf:
                logf.close()
            with SCAN_LOCK:
                SCAN_STATE["busy"] = False
                SCAN_STATE["phase"] = "idle"
            return self._err(500, "启动扫描失败：%s" % e)

        with SCAN_LOCK:
            SCAN_STATE.update({
                "proc": proc, "detail": detail, "log": log_p,
                "started": time.time(), "ended": 0.0, "rc": None,
                "aborted": False, "error": "",
            })
        threading.Thread(target=_scan_finish, args=(proc, logf), daemon=True).start()
        return self._json({"ok": True, "scan": scan_view()}, code=202,
                          head_only=head_only)

    def _scan_abort(self, head_only):
        """/api/scan/abort —— 中止正在跑的扫描。"""
        if head_only:
            return self._json({"ok": True}, head_only=True)
        if not self._local_only():
            return self._err(403, "只允许本机页面中止扫描")
        ref = self.headers.get("Referer") or ""
        if ref:
            host = urlparse(ref).netloc.split("@")[-1]
            if host and host != self.headers.get("Host"):
                return self._err(403, "跨站调用被拒绝")
        if not kill_scan():
            return self._err(409, "当前没有正在跑的扫描")
        return self._json({"ok": True, "scan": scan_view()})

    # ---- 快照历史 ----

    def _history(self, action, head_only):
        """/api/history[/diff|/trend] —— 全部只读按盘的 history/ 目录，与
        /api/drill 同一道 _local_only 校验；快照 id 先过 ID_RE 再拼路径。"""
        if not self._local_only():
            return self._err(403, "只允许本机页面调用历史接口")

        # 快照按盘存放，这一整个接口只读 ?root= 指定那个盘的 history/。
        q = parse_qs(urlparse(self.path).query)
        rq = (q.get("root") or [""])[0].strip()
        d_dir = storage.data_store(DATA, storage.norm(rq) or view_root())["dir"]
        # 这个盘连留档目录都没有（名字打错 / 盘已经从 data/ 里清掉）：
        # 直接说清楚，别让下游抛「快照不足两张」这种误导文案。
        if rq and not os.path.isdir(d_dir):
            return self._err(404, "没有这个盘的留档：%s" % rq, head_only)

        if action == "":
            # snapshots 给全集（各盘 history 合在一起），前端按当前树的 root
            # 过滤 —— 只发当前盘的话，切到刚扫过的盘会只剩一两张，用户会
            # 以为历史被删了。count / latest / prev 仍然是本盘口径。
            mine = history.summaries(d_dir)
            return self._json({
                "count": len(mine),
                "snapshots": history.all_summaries(DATA),
                "latest": mine[-1] if mine else None,
                "prev": mine[-2] if len(mine) > 1 else None,
            }, head_only=head_only)

        if action == "forget":
            """/api/history/forget?sid=<快照id> —— 删掉单张快照。

            破坏性接口，与 /api/drives/delete 同一道同源 Referer 闸；
            快照 id 过 ID_RE；同一盘删到只剩两张就拒绝 —— 两版对比
            是这个面板的地基，不能拆穿。
            """
            if head_only:
                return self._json({"ok": True}, head_only=True)
            ref = self.headers.get("Referer") or ""
            if ref:
                host = urlparse(ref).netloc.split("@")[-1]
                if host and host != self.headers.get("Host"):
                    return self._err(403, "跨站调用被拒绝")
            sid = (q.get("sid") or [""])[0].strip()
            if not sid:
                return self._err(400, "缺少 sid 参数")
            try:
                history.snap_path(d_dir, sid)      # ID_RE 校验，挡路径注入
            except history.DiffError as e:
                return self._err(400, str(e))
            target = history.load(d_dir, sid)
            if target is None:
                return self._err(404, "快照不存在：%s" % sid)
            rn = _norm_root(((target.get("tree") or {}).get("root") or ""))
            same = 0
            for s in history.snapshots(d_dir):
                sp = history.load(d_dir, s)
                if sp and _norm_root(((sp.get("tree") or {}).get("root") or "")) == rn:
                    same += 1
            if same <= 2:
                return self._err(400, "这个盘只剩 %d 张快照，再删对比就没得做了" % same)
            try:
                history.forget_one(d_dir, sid)
            except history.DiffError as e:
                return self._err(400, str(e))
            return self._json({"ok": True, "left": same - 1}, head_only=head_only)

        if action == "diff":
            sids = history.snapshots(d_dir)
            if len(sids) < 2:
                return self._err(404, "历史快照不足两张，刷新一次数据就有了")
            rq = (q.get("root") or [""])[0].strip()
            pool = sids
            if rq:
                # ?root= 指向别的盘：默认对只在该盘的快照里取
                rn = _norm_root(rq)
                pool = []
                for s in sids:
                    snap = history.load(d_dir, s)
                    if snap and _norm_root(((snap.get("tree") or {}).get("root") or "")) == rn:
                        pool.append(s)
            if len(pool) < 2:
                return self._err(404, "该盘的历史快照不足两张：%s" % (rq or "当前数据"))
            from_id = (q.get("from") or [""])[0] or pool[-2]
            to_id = (q.get("to") or [""])[0] or pool[-1]
            try:
                d = history.diff_ids(d_dir, from_id, to_id)
            except history.DiffError as e:
                return self._err(400, str(e))
            return self._json(d, head_only=head_only)

        if action == "deepdiff":
            sids = history.snapshots(d_dir)
            if len(sids) < 2:
                return self._err(404, "历史快照不足两张，刷新一次数据就有了")
            from_id = (q.get("from") or [""])[0] or sids[-2]
            to_id = (q.get("to") or [""])[0] or sids[-1]
            raw = (q.get("path") or [""])[0].strip().strip('"')
            if not raw:
                return self._err(400, "缺少 path 参数")
            try:
                d = history.deep_diff_ids(d_dir, from_id, to_id, raw)
            except history.DiffError as e:
                return self._err(400, str(e))
            return self._json(d, head_only=head_only)

        if action == "trend":
            raw = (q.get("path") or [""])[0].strip()
            if not raw:
                return self._err(400, "缺少 path 参数")
            rq = (q.get("root") or [""])[0].strip()
            t = history.trend(d_dir, raw, root=rq or None)
            if not any(p["b"] is not None for p in t["points"]):
                return self._err(404, "该路径不在任何已保留的快照里：%s" % raw)
            return self._json(t, head_only=head_only)

        if action == "matrix":
            rq = (q.get("root") or [""])[0].strip()
            tp = None
            if rq:
                st = history.drive_store(DATA, rq)
                if st and os.path.exists(st["tree"]):
                    tp = st["tree"]
            return self._json(history.trend_matrix(d_dir, tree_path=tp),
                              head_only=head_only)

        return self._err(404, "未知接口 /api/history/%s" % action, head_only)

    # ---- 动态下钻 ----

    def _drill(self, head_only):
        """GET /api/drill?path=<绝对路径> → 实时返回该目录的一层子项。"""
        if head_only:
            return self._err(405, "动态下钻只支持 GET", head_only=True,
                             extra_headers={"Allow": "GET"})
        if not self._local_only():
            return self._err(403, "只允许本机页面调用动态下钻")

        q = parse_qs(urlparse(self.path).query)
        raw = (q.get("path") or [""])[0]
        # 没带 ?root= 就跟界面停的那个盘（不是扫描盘）—— 否则切盘之后
        # 下钻仍以原盘的树为边界，跨盘的路径会被判成「不在任何留档里」。
        rq = (q.get("root") or [""])[0].strip() or view_root()
        src = SOURCES
        tree = read_json(src.get("tree"))
        # ?root= 指向别的盘：下钻以那个盘的留档树为边界（盘得挂在本机上）。
        # 那个盘没有留档就如实 404 —— 退回默认盘的树会让边界落到另一块盘上。
        if rq and tree is not None and _norm_root(rq) != _norm_root(tree.get("root")):
            st = history.drive_store(DATA, rq)
            t2 = read_json(st["tree"]) if st else None
            if t2 is None:
                return self._err(404, "没有这个盘的留档数据：%s" % rq)
            tree = t2
        if not isinstance(tree, dict) or not tree:
            return self._err(503, "数据源还没生成：%s 不存在" % src.get("tree"))
        try:
            out = drill.scan(raw, tree.get("root"))
        except drill.DrillError as e:
            return self._err(e.code, e.message)
        except Exception as e:
            sys.stderr.write("[warn] 动态下钻失败：%s\n" % e)
            return self._err(500, "动态下钻失败：%s" % e)
        return self._json(out)

    # ---- 打开资源管理器 ----

    def _open_dir(self, head_only):
        """/api/open?path=<绝对路径> → 用资源管理器打开该目录。

        服务只绑 127.0.0.1，本来就只有本人能调；再加一道 Referer 校验，
        免得用户浏览器里某个无关页面拿这个接口当跳板。
        路径必须真实存在才动手，不存在就如实回错，不猜、不创建。
        """
        if head_only:
            return self._json({"ok": True}, head_only=True)

        q = parse_qs(urlparse(self.path).query)
        raw = (q.get("path") or [""])[0].strip().strip('"')
        if not raw:
            return self._err(400, "缺少 path 参数")

        # 同源校验：Referer 必须是本服务，或者干脆没有（直接敲地址 / 探针）
        ref = self.headers.get("Referer") or ""
        if ref:
            host = urlparse(ref).netloc.split("@")[-1]
            if host and host != self.headers.get("Host"):
                return self._err(403, "跨站调用被拒绝")

        p = os.path.abspath(os.path.expanduser(raw))
        if not os.path.exists(p):
            return self._err(404, "路径不存在：%s" % p)
        if len(p) < 3 or p[1] != ":":
            return self._err(400, "只支持 Windows 绝对路径")

        try:
            # 目录直接开；文件用 /select 定位到它并选中
            if os.path.isdir(p):
                subprocess.Popen(["explorer", p])
            else:
                subprocess.Popen(["explorer", "/select," + p])
        except Exception as e:
            return self._err(500, "调起资源管理器失败：%s" % e)
        return self._json({"ok": True, "path": p})

    # ---- 静态资源 ----

    def _static(self, path, head_only):
        if path in ("/", "", "/index.html"):
            return self._file(os.path.join(WEB, "index.html"), head_only)

        if path.startswith("/assets/"):
            return self._file(self._safe(ASSETS, path[len("/assets/"):]), head_only)

        # 其余按 web/ 下的相对路径找（css/js/图片等）
        rel = path.lstrip("/")
        f = self._safe(WEB, rel)
        if f and os.path.isfile(f):
            return self._file(f, head_only)

        return self._err(404, "没有这个文件：%s" % path, head_only)

    @staticmethod
    def _safe(base, rel):
        """把 rel 解析到 base 里，挡掉 ../ 穿越。越界返回 None。"""
        rel = rel.replace("\\", "/").lstrip("/")
        full = os.path.realpath(os.path.join(base, rel))
        root = os.path.realpath(base)
        if full != root and not full.startswith(root + os.sep):
            return None
        return full

    def _file(self, full, head_only):
        if not full or not os.path.isfile(full):
            return self._err(404, "找不到文件", head_only)
        ctype = MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        return self._send(200, body, ctype, head_only)

    # ---- 连接收尾 ----
    # protocol_version 是 HTTP/1.1，连接默认 keep-alive：响应写完不等于线程结束，
    # 它还要在同一个 socket 上读下一行请求。浏览器关标签页 / 进程被强杀时会把池子里
    # 闲置的连接直接 RST 掉，那次 readline 就抛 ConnectionResetError。响应早已发出，
    # 服务端无事可做，但 ThreadingHTTPServer 的默认 handle_error 会往 stderr 糊一份
    # 全 stdlib 的 traceback，看着像崩了。这里只接连接类异常 —— 真正的代码错误
    # 仍然照常抛，不会被吞掉。

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError):
            # 响应写到一半对端就跑了，wfile.flush() 会抛。同上，无事可做。
            pass


def port_free(port, host="127.0.0.1"):
    s = socket.socket()
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def probe_running(port):
    """端口被占了 —— 问一句是谁。是本服务就返回它的 /api/health，否则 None。"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=1.5) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    return d if d.get("service") == "DiskAudit" else None


def need_drive_pick(update, rescan, skip_scan, drive, no_ask, has_tree, tty):
    """扫描前要不要问一句「扫哪个盘」。

    显式给了 --drive 就不问；这次根本不会扫描也不问；非交互环境（管道、
    计划任务）必须不问 —— 否则 input() 会一直等在那里。
    """
    if drive or no_ask or skip_scan or not tty:
        return False
    if update or rescan:
        return True
    return not has_tree      # 已经有数据、又没要求重扫：只起服务，不扫描


def ask_drive():
    """交互式选盘：列出本机磁盘，返回盘符；用户退出返回 None。

    默认项按「上次扫过的盘 → 系统盘 → 列表第一个」定，回车即接受。
    只在交互终端里调用 —— 管道里 input() 拿不到输入。
    """
    vols = volumes.list_volumes()
    if not vols:
        return volumes.norm_drive(os.environ.get("SystemDrive") or "C:")

    arch = {_norm_root(j["root"]): j for j in drives_payload()["drives"]}
    sysd = volumes.norm_drive(os.environ.get("SystemDrive") or "C:")
    last = storage.read_current(DATA)
    roots = [v["root"] for v in vols]
    dflt = last if last in roots else (sysd if sysd in roots else roots[0])

    print("")
    print("  选择要刷新数据的磁盘（本机 %d 个）：" % len(vols))
    print("")
    for i, v in enumerate(vols, 1):
        a = arch.get(_norm_root(v["root"]))
        parts = ["%d) %s" % (i, v["root"])]
        if v["label"]:
            parts.append(v["label"])
        parts.append("%.2f GB" % v["capGB"])
        parts.append("已用 %.1f%%" % v["usedPct"])
        parts.append("剩 %.2f GB" % v["freeGB"])
        parts.append(("上次扫描 " + (a.get("at") or "")) if a else "尚未扫描")
        marks = []
        if v["root"] == dflt:
            marks.append("默认")
        if v["root"] == sysd:
            marks.append("系统盘")
        line = "  " + " · ".join(parts)
        if marks:
            line += "  [" + " · ".join(marks) + "]"
        print(line)
    print("")
    sys.stdout.flush()       # 菜单必须先落屏，别让后面的输出抢在前面

    while True:
        try:
            s = input("  输入序号或盘符（回车 = %s，q 退出）：" % dflt).strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return None
        if not s:
            return dflt
        if s.lower() in ("q", "quit", "exit"):
            return None
        if s.isdigit():
            n = int(s)
            if 1 <= n <= len(vols):
                return vols[n - 1]["root"]
        d = volumes.norm_drive(s)
        if d in roots:
            return d
        print("  没有这个选项：%s" % s)


def scan_options(drive=None, detail_root=None, depth=None, min_mb=None,
                 file_min_mb=None, detail_min_mb=None, skip_links=False,
                 keep_snapshots=None):
    """把 app 的参数翻译成 update-data.ps1 的参数。"""
    out = []
    if drive:
        out += ["-Drive", str(drive)]
    if detail_root:
        out += ["-DetailRoot", str(detail_root)]
    if depth is not None:
        out += ["-Depth", str(depth)]
    if min_mb is not None:
        out += ["-MinMB", str(min_mb)]
    if file_min_mb is not None:
        out += ["-FileMinMB", str(file_min_mb)]
    if detail_min_mb is not None:
        out += ["-DetailMinMB", str(detail_min_mb)]
    if skip_links:
        out.append("-SkipLinks")
    if keep_snapshots is not None:
        out += ["-KeepSnapshots", str(keep_snapshots)]
    return out


def run_scan(extra_args=None):
    """调用采集脚本并返回退出码。"""
    ps1 = os.path.join(HERE, "collect", "update-data.ps1")
    if not os.path.exists(ps1):
        print("  [x] 找不到采集脚本：%s" % ps1)
        return 1
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1]
    cmd += list(extra_args or [])
    return subprocess.call(cmd)


def ensure(skip_scan=False, quiet=False, limit=ticket.LIMIT, scan_args=None):
    """启动前的准备。返回 True 表示可以起服务。

    顺序不能换：扫描 → 知识库 → 报告骨架 → ticket。
    ticket 要靠知识库评级挑候选，知识库要靠扫描数据才知道有哪些目录。
    skip_scan 为真时整段扫描都不碰，只跑文字层。
    """
    # 当前盘 = current.json 记录的那个（上次扫完的盘）。一张留档都没有时先扫再定。
    root = storage.read_current(DATA)
    if not root and skip_scan:
        print("  [x] 还没有任何盘的扫描数据，而本次跳过了扫描")
        print("      去掉 --skip-scan 再启动，数据会被采出来")
        return False
    if not root:
        print("")
        print("  首次运行：先扫描一遍磁盘（取决于文件数量，通常几分钟）")
        print("  ------------------------------------------------------------")
        rc = run_scan(scan_args)
        root = storage.read_current(DATA)
        if rc != 0 or not root:
            print("")
            print("  [x] 扫描没有完成（退出码 %s），没有产出可用数据" % rc)
            return False
        print("  ------------------------------------------------------------")
        print("")

    d_dir, c_dir, root = use_drive(root)
    tree_p = os.path.join(d_dir, "tree.json")
    purpose_p = os.path.join(c_dir, "purpose.json")
    report_p = os.path.join(c_dir, "report.json")
    ticket_p = ticket.adopt(os.path.join(c_dir, ticket.DEFAULT_NAME))

    # ---- ② 知识库兜底（这个盘第一次扫，还没有用途库）----
    if purpose.ensure(purpose_p, tree=read_json(tree_p)) and not quiet:
        print("  [新建] content\\drives\\%s\\purpose.json —— 通用基础库" % storage.key(root))

    # ---- ③ 报告骨架 ----
    # 每个盘各自一份 report.json，换盘不会覆盖别人的语义层，所以没有「换盘重建」。
    if not os.path.exists(report_p):
        try:
            report_mod.seed(d_dir, c_dir, quiet=True)
            if not quiet:
                print("  [新建] content\\drives\\%s\\report.json —— 骨架，文字等 AI 补"
                      % storage.key(root))
        except Exception as e:
            print("  [warn] 生成报告骨架失败：%s" % e)

    # ---- ④ ticket：改过就导入，然后重新出单 ----
    try:
        n, newp, cnt = ticket.sync(d_dir, c_dir, ticket_p, limit=limit, quiet=quiet)
    except Exception as e:
        print("  [warn] ticket 处理失败：%s" % e)
        n, newp, cnt = 0, None, 0

    if cnt and not quiet:
        print("")
        print("  待完善：%d 条目录说明 → %s" % (cnt, ticket_p))
        print("  把这个文件发给 AI，让它按里面的说明填写，")
        print("  填完覆盖回原位置，重启本程序即可自动导入。")
        print("")
    elif n and not quiet:
        print("  ticket 已导入 %d 条，暂时没有新的缺口。" % n)

    return True


def start_offline_build(quiet=False):
    """后台启动 build.py，不阻塞 update；日志写到 data\\.build.log。"""
    build_py = os.path.join(HERE, "build.py")
    if not os.path.exists(build_py):
        print("  [warn] 找不到 scripts/build.py，跳过离线版构建")
        return False
    log_p = os.path.join(DATA, ".build.log")
    try:
        os.makedirs(DATA, exist_ok=True)
        log = open(log_p, "a", encoding="utf-8")
    except OSError as e:
        print("  [warn] 打不开离线版构建日志 %s：%s" % (log_p, e))
        return False

    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        log.write("\n===== build %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        log.flush()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [sys.executable, build_py],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    except Exception as e:
        log.close()
        print("  [warn] 离线版后台构建启动失败：%s" % e)
        return False
    log.close()
    if not quiet:
        print("  [build] 离线版后台生成中（pid %d，日志 data\\.build.log）" % proc.pid)
    return True


def run_update(skip_scan=False, quiet=False, limit=ticket.LIMIT, scan_args=None,
               build_offline=True):
    """只刷新：扫描（除非跳过）+ 文字层 + ticket + 后台离线版。不起服务、不开浏览器。

    扫描失败不连坐 —— 旧数据是原子替换的，没被破坏，那就先按旧数据把
    文字层和 ticket 刷出来，最后用非零码告诉调用方「扫描这步没成」。
    """
    ok = True
    if skip_scan:
        print("  跳过扫描（--skip-scan）：只刷新文字层")
    else:
        print("  重新扫描 …")
        rc = run_scan(scan_args)
        if rc != 0:
            ok = False
            print("")
            print("  [x] 扫描没有完成（退出码 %s）" % rc)
            _c = storage.read_current(DATA)
            if not (_c and os.path.exists(storage.data_store(DATA, _c)["tree"])):
                print("      没有可用的旧数据，先解决扫描问题再试")
                return 1
            print("      data\\ 里的旧数据没被改动，先按它刷新文字层")

    if not ensure(True, quiet, limit):
        return 1
    if build_offline:
        start_offline_build(quiet)
    elif not quiet:
        print("  跳过离线版构建（--no-build）")
    return 0 if ok else 2


def main():
    ap = argparse.ArgumentParser(description="DiskAudit：启动编排 + 本地服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--update", action="store_true",
                    help="只刷新数据与文字层，不起服务（会先问扫哪个盘）")
    ap.add_argument("--skip-scan", "--no-scan", dest="skip_scan", action="store_true",
                    help="跳过整个扫描阶段（全盘 / 细化 / 链接都不跑）")
    ap.add_argument("--no-build", action="store_true",
                    help="刷新后不后台生成 dist 离线版")
    ap.add_argument("--rescan", action="store_true", help="起服务前先重扫一遍")
    ap.add_argument("--no-ask", action="store_true",
                    help="不交互选盘，直接用 --drive 或默认盘（脚本里调用时加）")
    ap.add_argument("--drive", metavar="PATH",
                    help="扫描哪个盘或根目录，如 D:；默认系统盘")
    ap.add_argument("--detail-root", metavar="PATH",
                    help="细化扫描根目录；默认当前用户家目录")
    ap.add_argument("--depth", type=int, metavar="N",
                    help="静态树保留深度；默认 6")
    ap.add_argument("--min-mb", type=float, metavar="MB",
                    help="目录收录门槛；默认 40")
    ap.add_argument("--file-min-mb", type=float, metavar="MB",
                    help="单文件收录门槛；默认 20")
    ap.add_argument("--detail-min-mb", type=float, metavar="MB",
                    help="细化目录门槛；默认 12")
    ap.add_argument("--skip-links", action="store_true",
                    help="不测量 junction / 符号链接目标体积，扫描更快")
    ap.add_argument("--keep-snapshots", type=int, metavar="N",
                    help="每个盘的 history/ 保留多少张扫描快照，0 = 不限；默认 64")
    ap.add_argument("--limit", type=int, default=ticket.LIMIT, metavar="N",
                    help="ticket 一次放多少条，0 = 全量导出（默认）")
    ap.add_argument("-q", "--quiet", action="store_true", help="少打印")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每个请求")
    a = ap.parse_args()

    # --drive 只认盘符：传目录会扫出一棵「D:\Work」的树，落进
    # data/drives/DWORK/ —— 而选盘弹窗只列本机真实盘符，那份留档在界面上
    # 永远看不见、切不到、也删不掉。与 /api/scan 用同一条规则。
    if a.drive:
        d = volumes.norm_drive(a.drive)
        if not d:
            print("  [x] --drive 只接受盘符（如 D:），收到的是：%s" % a.drive)
            print("      要细化某个目录，用 --detail-root；整盘扫描用盘符。")
            return 1
        a.drive = d

    # ---- 选盘：双击 update.bat 的人不会记得加 --drive，那就先问一句 ----
    cur = storage.read_current(DATA)
    has_tree = bool(cur and os.path.exists(storage.data_store(DATA, cur)["tree"]))
    if need_drive_pick(a.update, a.rescan, a.skip_scan, a.drive, a.no_ask,
                       has_tree, sys.stdin.isatty()):
        picked = ask_drive()
        if picked is None:
            print("  已取消，没有改动任何数据")
            return 0
        a.drive = picked
    elif not a.drive and (a.update or a.rescan):
        # 不问就得自己定：用上次扫过的盘，而不是落到 ps1 的 $env:SystemDrive。
        # 否则 --no-ask 在非系统盘上刷新时，会静默改扫系统盘。
        a.drive = cur or None

    # 指定了盘、但那个盘还没有留档：ensure() 只在「一棵树都没有」时才真扫，
    # 这里补一次。判据必须落在 a.drive 自己身上 —— 用 has_tree（= 有没有任意
    # 一个盘有树）会让 `start.bat --drive C`（C 从没扫过、D 有留档）什么都不做，
    # 用户选了 C 却看到 D 的数据。
    if a.drive and not a.skip_scan and not storage.has_tree(DATA, a.drive):
        a.rescan = True

    scan_args = scan_options(a.drive, a.detail_root, a.depth, a.min_mb,
                             a.file_min_mb, a.detail_min_mb, a.skip_links,
                             a.keep_snapshots)

    if not os.path.exists(os.path.join(WEB, "index.html")):
        print("[fatal] 找不到 web/index.html，请确认目录结构完整")
        return 1

    # ---- 刷新模式：干完就退，不占端口、不开浏览器 ----
    if a.update:
        return run_update(a.skip_scan, a.quiet, a.limit, scan_args,
                          not a.no_build)

    # 端口先查 —— 别扫了十分钟才发现端口被占
    if not port_free(a.port, a.host):
        info = probe_running(a.port)
        if info:
            other = info.get("dataDir") or ""
            if other and os.path.normcase(os.path.abspath(other)) != os.path.normcase(DATA):
                print("  端口 %d 上跑着另一个 DiskAudit：%s" % (a.port, other))
                print("  换一个端口再启动：start.bat --port 8766")
                return 1
            url = "http://%s:%d/" % (a.host, a.port)
            print("服务已经在跑：%s" % url)
            if not a.no_browser:
                webbrowser.open(url)
            return 0
        print("[fatal] 端口 %d 被别的程序占用了，换一个：--port 8766" % a.port)
        return 1

    if a.rescan and not a.skip_scan:
        print("  重新扫描 …")
        rc = run_scan(scan_args)
        if rc != 0:
            print("  [x] 扫描失败（退出码 %s）" % rc)
            _c = storage.read_current(DATA)
            if not (_c and os.path.exists(storage.data_store(DATA, _c)["tree"])):
                return rc
            print("      先用旧数据起服务")

    if not ensure(a.skip_scan, a.quiet, a.limit, scan_args):
        return 1

    # --drive 是「扫哪个盘」，不是「打开哪个盘」：打开的是 current.json 记的那个。
    # 两者不一致时说一句，否则用户会以为 --drive 没生效。
    served = storage.read_current(DATA)
    want = volumes.norm_drive(a.drive)
    if want and want != volumes.norm_drive(served):
        print("  提示：--drive %s 是扫描目标，默认打开的是 %s；"
              "要看 %s 用页面右上角的磁盘按钮切换。" % (want, served or "—", want))

    st = sources_state()
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    httpd.daemon_threads = True
    httpd.verbose = a.verbose
    url = "http://%s:%d/" % (a.host, a.port)

    print("  DiskAudit · %s" % url)
    print("  数据 %s" % DATA)
    for k in ("tree", "links", "meta", "purpose", "report"):
        v = st[k]
        print("    %-8s %s" % (k, ("%s KB" % v["kb"]) if v["ok"] else "缺失"))
    print("")
    print("  按 Ctrl+C 停止")
    print("")

    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
