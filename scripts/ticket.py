# -*- coding: utf-8 -*-
"""ticket：报告文字的唯一载体。

一份 md 文件，同时承担三个角色：
    ① 说明书 —— 提示词、字段含义、规则都写在文件里，AI 拿到就知道要干什么
    ② 表单   —— 每个目录一节，AI 直接在冒号后面填
    ③ 载荷   —— 用户把改好的文件放回来，程序解析并导入

指纹机制判断「有没有被改过」：生成时把正文的 md5 写进一行注释，
下次启动比对；不一致就说明用户 / AI 动过，于是导入。

对外接口：
    gen(data, content, path, limit)  生成 ticket，返回 (path, 条数)
    dirty(path)                      ticket 是否被改过
    parse(text)                      解析成 {"items": [...], "report": [...]}
    apply(parsed, content)           导入 content/purpose.json 与 report.json
    sync(data, content, path)        编排：改过就导入，然后重新出单
    adopt(path)                      旧版文件名自动改名，返回可用的路径
"""
import hashlib
import io
import os
import re
import sys

import pathvars
import purpose as pur
import report as rep

BS = "\\"
MB = 1024.0 ** 2
GB = MB * 1024.0

DEFAULT_NAME = "ticket.md"
# v1 写下的文件用的是中文名，升级后首次启动自动改名。用转义写，源码里不再出现该词。
LEGACY_NAME = "\u5de5\u5355.md"


def adopt(path):
    """把旧文件名的 ticket 改成新名，返回实际可用的路径。

    改名是 rename 不是重写：用户填的正文一个字不动。两种例外都保留原状 ——
    新文件已经在了就以新的为准（旧的原样留着，不替用户做删除决定）；
    改不动（被占用 / 只读）就退回旧路径，本轮照常读写，内容不丢。
    """
    old = os.path.join(os.path.dirname(path), LEGACY_NAME)
    if not os.path.exists(old) or os.path.exists(path):
        return path
    try:
        os.replace(old, path)
    except OSError as e:
        sys.stderr.write("[warn] 旧版 ticket 改名失败，继续用原文件：%s\n" % e)
        return old
    print("  [改名] %s → %s" % (LEGACY_NAME, DEFAULT_NAME))
    return path

# 一次 ticket 放多少条。0 = 不限量，把当前所有缺口一次全导出。
# 全量导出的好处是「一次交差」，代价是文件大、AI 容易偷懒；
# 想分批就传 --limit N，app.py 按同样的排序切前 N 条。
LIMIT = 0
# 小于这个体积的目录不值得单独补说明 —— 靠规则兜底够了
MIN_B = 8 * MB

FP_RE = re.compile(r"<!--\s*fingerprint:\s*([0-9a-f]*)\s*-->")
FP_BLANK = "<!-- fingerprint: -->"

H2_RE = re.compile(r"^###\s+(\d+)\.\s+`(.+?)`\s*$")
# 前缀放宽：AI 可能把 "- 说明：" 写成 "说明：" 或 "* 说明："，都要认
FIELD_NOTE = re.compile(r"^\s*[-*+]?\s*说明\s*[：:]\s*(.*)$")
FIELD_GRADE = re.compile(r"^\s*[-*+]?\s*评级\s*[：:]\s*(.*)$")
CARD_RE = re.compile(r"^###\s+结论\s*(\d+)\s*$")
CARD_TITLE = re.compile(r"^\s*[-*+]?\s*标题\s*[：:]\s*(.*)$")
CARD_TEXT = re.compile(r"^\s*[-*+]?\s*一句话\s*[：:]\s*(.*)$")

GRADES = ("ok", "warn", "no", "info")
GRADE_LABEL = {"ok": "可以清理", "warn": "谨慎", "no": "别动", "info": "了解即可"}
WHY_LABEL = {
    "rule-name": "名字规则命中",
    "rule-path": "路径规则命中",
    "inherited": "继承祖先说明",
    "none": "无任何说明",
}

# 兜底档次的权重：完全没有说明的最该补，靠规则兜底的次之
WHY_WEIGHT = {"none": 3.0, "inherited": 2.0, "rule-name": 1.2, "rule-path": 1.2}

# 随机命名的目录不值得补说明 —— 换个名字就是另一条，规则兜底足够
SKIP_NAME = re.compile(
    r"^(sess_[0-9a-f-]{20,}"
    r"|call_[0-9a-f]{10,}.*"
    r"|[0-9a-f]{10,}"
    r"|[A-Za-z0-9]{20,}"                      # 随机串（临时目录、检查点）
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\{[0-9A-Fa-f-]{36}\}"
    r"|[0-9a-f]{5,8}\.msi"
    r"|\{[0-9A-Fa-f-]{36}\}\.msi"
    r"|chromium-\d+"                          # 下载的浏览器内核，按版本命名
    r")$", re.I)

# 系统目录 / 临时目录的内部条目：父级已经有说明，逐条补没有意义
# （WinSxS 每个组件都不同、Installer 全是哈希命名的补丁包、Temp 里都是随机名）
SKIP_PATH = re.compile(
    r"\\(WinSxS|Installer|SoftwareDistribution|assembly|servicing|Prefetch|Temp|tmp)\\", re.I)


def tok(p, home, drive, sysdrive=None):
    """ticket 里的路径原样写真实绝对路径。

    这些是数据文件，不进版本库、不对外发布。令牌带不来任何保护，只会让
    「这条属于哪个盘」看不出来，导入时还要再用一套上下文解析回去。
    参数保留是为了调用方签名不变。
    """
    return p


def heal(p):
    r"""把缺了冒号的盘符路径补回来。

    %DRIVE% 展开成不带冒号的盘符，所以历史上有一批 ticket 把路径写成了
    `%DRIVE%\Program Files`（少了冒号），展开得到 `C\Program Files`，谁也命不中。
    这条就是用来在导入时顺手修对的 —— 用户手里的旧 ticket 不该因为一个冒号作废。

        %DRIVE%\Program Files  →  %DRIVE%:\Program Files
        C\Program Files        →  C:\Program Files
    已经正确的形态原样返回。
    """
    m = re.match(r"^((?:%DRIVE%|%SYSDRIVE%)|[A-Za-z])(\\.*)$", p or "")
    if not m:
        return p
    return m.group(1) + ":" + m.group(2)


def norm_grade(s):
    """把 AI 可能写的各种形式归一到 ok / warn / no / info。"""
    t = (s or "").strip().lower()
    if not t:
        return ""
    if t in GRADES:
        return t
    m = re.match(r"^[`*_（(]*\s*(ok|warn|no|info)\b", t)
    if m:
        return m.group(1)
    for g in GRADES:
        if re.search(r"(?<![a-z])%s(?![a-z])" % g, t):
            return g
    return ""


def md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def pick_candidates(data, content, limit=LIMIT):
    """挑出该补的目录：没有精确说明、体积够大、不是随机命名 / 系统内部。

    返回 (picked, snap, skipped)。skipped 是被 SKIP_* 规则挡掉的条目，
    数量会写进 ticket 头部 —— 过滤规则必须让人看见，不能悄悄吞掉。

    limit <= 0 表示全量导出。全量时不做父子去重：每个目录都该有自己的一条说明，
    去重只是「名额有限时别把两份名额花在同一件事上」的省名额手段。
    """
    tree = pur.rd(os.path.join(data, "tree.json"))
    if not tree:
        return [], None, []
    purpose_p = os.path.join(content, "purpose.json")
    P, _e, _n, _p = pur.load(purpose_p, tree)
    if P is None:
        return [], None, []
    snap = pur.snapshot(tree, P)

    home = pathvars.home_of(tree)
    drive = (tree.get("root") or "C:").rstrip("\\/:")
    sysdrive = pathvars.sysdrive_of(tree)
    picked = []

    # 先按「体积 × 兜底权重」重排：完全没说明的、靠祖先继承的优先，
    # 否则前几条会被「体积大但已有通用规则」的目录占满。
    cands, skipped = [], []
    for g in snap["gaps"]:
        if g["b"] < MIN_B:
            continue
        g["q"] = tok(g["p"], home, drive, sysdrive)
        if SKIP_NAME.match(g["n"] or "") or SKIP_PATH.search(g["p"] or ""):
            skipped.append(g)
            continue
        cands.append(g)
    cands.sort(key=lambda g: -(g["b"] * WHY_WEIGHT.get(g["why"], 1.0)))
    skipped.sort(key=lambda g: -g["b"])

    for g in cands:
        if limit and limit > 0 and len(picked) >= limit:
            break
        if limit and limit > 0:
            # 只有限名额时才做父子去重
            low = g["p"].lower()
            dup = False
            for q in picked:
                ql = q["p"].lower()
                if low.startswith(ql + BS) or ql.startswith(low + BS):
                    dup = True
                    break
            if dup:
                continue
        picked.append({
            "p": g["p"],
            "q": tok(g["p"], home, drive, sysdrive),
            "b": g["b"],
            "why": g["why"],
        })
    return picked, snap, skipped


def fmt_size(b):
    if b >= GB:
        return "%.2f GB" % (b / GB)
    return "%d MB" % max(1, round(b / MB))


def cur_cards(content):
    """取 report.json 里现有的三张结论卡，作为「现用文案」给 AI 参考。

    不预填进可编辑字段 —— 预填的话每轮都会被原样回写，日志里永远是「导入 3 条」，
    而 AI 也容易顺手改掉本来就不错的文案。
    """
    R = pur.rd(os.path.join(content or "", "report.json")) or {}
    out = []
    for c in ((R.get("overview") or {}).get("conclusion") or [])[:3]:
        t = re.sub(r"\s+", " ", (c.get("title") or "")).strip()
        x = re.sub(r"<[^>]+>", "", c.get("text") or "")
        x = re.sub(r"\s+", " ", x).strip()
        out.append((t, x))
    return out


def render(items, snap, content, skipped=None, limit=None):
    """拼出 ticket 正文。指纹行先留空占位，调用方再填。"""
    total = (snap or {}).get("stats", {}).get("total", 0)
    exact = (snap or {}).get("stats", {}).get("exact", 0)
    n = len(items)
    skipped = skipped or []
    cards = cur_cards(content)

    L = []
    A = L.append
    A("# 磁盘空间分析 · 待完善 ticket")
    A("")
    if limit and limit > 0:
        A("全盘 %d 个目录，其中 %d 个已有精确说明。本单先取**前 %d 条**"
          "（按「体积 × 缺少程度」排序），剩下的下一批继续。" % (total, exact, n))
    else:
        A("全盘 %d 个目录，其中 %d 个已有精确说明，还剩 %d 个没有。"
          "本单把**全部 %d 条**都列出来了（按「体积 × 缺少程度」排序）。" % (total, exact, n, n))
    if skipped:
        A("")
        A("另有 %d 条未列入（文末有清单）：随机命名的临时目录（`sess_*` / GUID / 哈希）"
          "和系统内部目录（WinSxS、Installer、Prefetch、Temp 等）—— 换个名字就是另一条，"
          "逐条补说明没有意义。" % len(skipped))
    A("")
    A("**怎么用**")
    A("")
    A("1. 把这个文件整个发给 AI（Claude / ChatGPT / 任意对话模型都行）")
    A("2. 让它按下面「给 AI 的说明」填写每一节")
    A("3. 保存，覆盖回原位置（还是这个文件名、这个目录）")
    A("4. 双击 `start.bat` —— 会自动导入，并生成下一批 ticket")
    A("")
    A("> 不补也没关系：报告照样能出，只是这些目录显示的是通用说明。")
    A("")
    A("> 条目多的话不必一次填完。填多少导多少，剩下的会出现在下一份 ticket 里。")
    A("")
    A(FP_BLANK)
    A("")
    A("---")
    A("")
    A("## 给 AI 的说明")
    A("")
    A("你在帮一个 Windows 磁盘空间分析工具补目录说明。下面每一节是一个真实存在的目录，")
    A("请为每一节补两行，写在原来的冒号后面。")
    A("")
    A("**`- 说明：`** —— 40~80 字，说清三件事：")
    A("这是什么程序 / 数据的目录、大概占多少、删了会怎样。")
    A("不要写「这是系统目录」这种空话，要写具体用途。")
    A("不确定这是什么，并且你能联网搜索，就先去查资料再写。")
    A("")
    A("**`- 评级：`** —— 从下面四个里选一个，只写单词：")
    A("")
    A("| 评级 | 含义 |")
    A("|---|---|")
    A("| `ok` | 可以清理，删了不影响功能 |")
    A("| `warn` | 谨慎，可能有用，先确认 |")
    A("| `no` | 别动，删了会出问题 |")
    A("| `info` | 了解即可，不用管 |")
    A("")
    A("**规则**")
    A("")
    A("1. 只改 `- 说明：` 和 `- 评级：` 冒号后面的内容，**其他一个字都不要动**")
    A("2. 标题里的路径是这台机器上的真实绝对路径，原样保留，不要改写盘符或目录层级")
    A("3. 拿不准就填 `- 评级：info`，说明里写「用途不明确，建议先确认」")
    A("4. 不要新增、删除或重排小节")
    A("5. 全部填完后返回整个文件")
    A("")
    A("---")
    A("")
    A("## 待补目录（%d 条）" % n)
    A("")

    for i, it in enumerate(items, 1):
        A("### %d. `%s`" % (i, it["q"]))
        A("")
        A("- 体积：%s" % fmt_size(it["b"]))
        A("- 当前兜底：%s" % WHY_LABEL.get(it["why"], it["why"]))
        A("- 说明：")
        A("- 评级：")
        A("")

    A("---")
    A("")
    A("## 报告结论（可选）")
    A("")
    A("概览页顶部有三张结论卡，各需要一句标题和一句话。")
    A("**留空就保留现用文案** —— 只有你觉得现用文案不够好时才填。")
    A("")
    A("**规则**：只改冒号后面的内容；标题 8~14 字，一句话 20~40 字；")
    A("数字不要写死 —— 要引用就用 `{links.size}`、`{disk.free}` 这类占位符。")
    A("")
    for i in (1, 2, 3):
        A("### 结论 %d" % i)
        A("")
        cur = cards[i - 1] if i - 1 < len(cards) else ("", "")
        if cur[0] or cur[1]:
            A("- 现用：%s —— %s" % (cur[0] or "（无标题）", cur[1] or "（无说明）"))
        else:
            A("- 现用：（空）")
        A("- 标题：")
        A("- 一句话：")
        A("")

    if skipped:
        A("---")
        A("")
        A("## 未列入的目录（%d 条，不需要填）" % len(skipped))
        A("")
        A("这些目录有两条以上的理由不值得逐条写说明：名字是随机的（会话 ID、GUID、"
          "哈希、`chromium-*`、`*.msi`），或者位于系统内部（WinSxS / Installer / "
          "SoftwareDistribution / assembly / servicing / Prefetch / Temp）—— "
          "父级已经有说明，换个名字就是另一条。列在这里只是为了让「缺口」这件事透明。")
        A("")
        for g in skipped:
            A("- `%s` · %s" % (g.get("q") or g["p"], fmt_size(g["b"])))
        A("")

    return "\n".join(L) + "\n"


def gen(data, content, path, limit=LIMIT):
    """生成 ticket。返回 (path, 条数)；没有要补的就返回 (None, 0)。

    「没有候选」不等于「缺口清零」：树读不到、用途库损坏时，pick_candidates
    同样返回空表。那时删掉 ticket 等于把用户填了一半的内容静默抹掉 ——
    数据读不全就只报错，一个字节都不动。
    """
    items, snap, skipped = pick_candidates(data, content, limit)
    if snap is None:
        sys.stderr.write("[warn] 数据读不全（缺 tree.json 或 purpose.json 读不出来），"
                         "本轮不重出 ticket：%s\n" % path)
        return None, 0
    if not items:
        if os.path.exists(path):
            os.remove(path)
        return None, 0

    body = render(items, snap, content, skipped, limit)
    fp = md5(body)
    text = body.replace(FP_BLANK, "<!-- fingerprint: %s -->" % fp, 1)

    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".new"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    return path, len(items)


# ---------------------------------------------------------------------------
# 解析与导入
# ---------------------------------------------------------------------------

def read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def dirty(path):
    """ticket 是否被改过。没指纹（用户手写的）也算改过。"""
    if not os.path.exists(path):
        return False
    try:
        text = read(path)
    except Exception:
        return False
    m = FP_RE.search(text)
    if not m or not m.group(1):
        return True
    body = FP_RE.sub(FP_BLANK, text, count=1)
    return md5(body) != m.group(1)


def parse(text):
    """解析 ticket。返回 {"items": [...], "report": [...]}，只收填了内容的。"""
    items, cards = [], []
    cur = None
    card = None
    for line in text.splitlines():
        m = H2_RE.match(line)
        if m:
            cur = {"path": m.group(2), "note": "", "grade": ""}
            items.append(cur)
            card = None
            continue
        m = CARD_RE.match(line)
        if m:
            card = {"idx": int(m.group(1)), "title": "", "text": ""}
            cards.append(card)
            cur = None
            continue
        if cur is not None:
            m = FIELD_NOTE.match(line)
            if m:
                cur["note"] = m.group(1).strip()
                continue
            m = FIELD_GRADE.match(line)
            if m:
                cur["grade"] = m.group(1).strip()
                continue
        elif card is not None:
            m = CARD_TITLE.match(line)
            if m:
                card["title"] = m.group(1).strip()
                continue
            m = CARD_TEXT.match(line)
            if m:
                card["text"] = m.group(1).strip()
                continue

    good = []
    for it in items:
        g = norm_grade(it["grade"])
        note = it["note"]
        # AI 可能把提示词原样留着，或者写「（待补）」，这些都不算填了
        if not note or not g:
            continue
        if note in ("（待补）", "(待补)", "待补", "...", "…"):
            continue
        good.append({"path": it["path"], "note": note, "grade": g})

    good_cards = [c for c in cards if c["title"] or c["text"]]
    return {"items": good, "report": good_cards}


def apply(parsed, content, tree=None, quiet=False):
    """把解析结果写进 content/。返回 (目录条数, 结论条数)。

    导入时会顺手把历史 ticket 里缺冒号的盘符路径补对（见 heal），
    命中的旧条目就地改 key，不会新插一份死的。
    """
    purpose_p = os.path.join(content, "purpose.json")
    P = pur.rd(purpose_p) or pur.build()
    exact = P.setdefault("exact", [])
    idx = {o.get("p"): o for o in exact}

    home = pathvars.home_of(tree) if tree else ""
    drive = ((tree or {}).get("root") or "C:").rstrip("\\/:")
    sysdrive = pathvars.sysdrive_of(tree) if tree else None
    ctx = pathvars.context(home, (tree or {}).get("root") or "C:", sysdrive) if home else None

    # ---- 本次扫描真实存在的路径，用来判定导入结果有没有生效 ----
    nodes = set()
    if tree:
        def _walk(n, p):
            for k in n.get("c", []):
                cp = p + BS + k.get("n", "")
                nodes.add(cp.lower())
                _walk(k, cp)
        _walk(tree.get("tree") or {}, (tree.get("root") or "").rstrip(BS))
        nodes.add((tree.get("root") or "").rstrip(BS).lower())

    def lookup(key):
        """按「原样 → 补冒号 → 各自展开」四种形态找已有条目。

        返回 (命中的 key, 规范化 key, 条目)。缺冒号的条目会被就地改对，
        而不是新插一条 —— 否则知识库里同一个目录会躺着两份，一份死的。
        """
        canon = heal(key)
        for c in (key, canon):
            hit = idx.get(c)
            if hit is not None:
                return c, canon, hit
            if ctx:
                real = pathvars.expand_str(c, ctx)
                hit = idx.get(real)
                if hit is not None:
                    return real, canon, hit
        return None, canon, None

    n_dir = 0
    migrated = 0
    for it in parsed["items"]:
        fk, key, hit = lookup(it["path"])
        if hit is None:
            hit = {"p": key, "t": "", "g": "info", "he": True}
            exact.append(hit)
            idx[key] = hit
        elif hit.get("p") != key:
            # 命中的是不带冒号的写法，把它的 key 改成规范形态
            old = hit.get("p")
            hit["p"] = key
            idx.pop(fk, None)
            idx.pop(old, None)
            idx[key] = hit
            migrated += 1
        hit["t"] = it["note"]
        hit["g"] = it["grade"]
        n_dir += 1

    pur.wr(purpose_p, P)

    if migrated and not quiet:
        print("  [修正] ticket 里有 %d 条盘符路径缺冒号，已就地补上" % migrated)

    # ---- 自检：导入的路径必须在本次扫描数据里真实存在 ----
    # 占位符少写一个冒号（`%DRIVE%` 展开成 `C\Program Files`）时，条目照样写进
    # 知识库，却一条也命不中目标目录，下一轮 ticket 会把同样的目录重新列一遍。
    # 对不上必须报出来，否则「填了却没生效」没有任何线索可查。
    if tree:
        dead = []
        for it in parsed["items"]:
            real = pathvars.expand_str(heal(it["path"]), ctx) if ctx else heal(it["path"])
            if real.rstrip(BS).lower() not in nodes:
                dead.append(it["path"])
        if dead:
            sys.stderr.write("[warn] 导入的 %d 条路径在当前扫描数据里找不到，"
                             "写了也命不中（换了机器 / 重扫过 / 路径被改过）：\n"
                             % len(dead))
            for d in dead[:5]:
                sys.stderr.write("       - %s\n" % d)
            if len(dead) > 5:
                sys.stderr.write("       …… 另有 %d 条\n" % (len(dead) - 5))

    n_card = 0
    cards = parsed.get("report") or []
    if cards:
        report_p = os.path.join(content, "report.json")
        R = pur.rd(report_p)
        if R is not None:
            concl = ((R.get("overview") or {}).get("conclusion")) or []
            for c in cards:
                i = c["idx"] - 1
                if not (0 <= i < len(concl)):
                    continue
                if c["title"]:
                    concl[i]["title"] = c["title"]
                if c["text"]:
                    concl[i]["text"] = c["text"]
                n_card += 1
            if n_card:
                pur.wr(report_p, R)

    return n_dir, n_card


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def sync(data, content, path, limit=LIMIT, quiet=False):
    """启动时调用：ticket 被改过就导入，然后重新出一份（如果还有缺口）。

    返回 (导入条数, 新 ticket 路径, 新 ticket 条数)。
    """
    path = adopt(path)
    tree = pur.rd(os.path.join(data, "tree.json"))
    n_dir = n_card = 0

    if os.path.exists(path) and dirty(path):
        try:
            parsed = parse(read(path))
        except Exception as e:
            sys.stderr.write("[warn] ticket 解析失败：%s\n" % e)
            parsed = {"items": [], "report": []}
        n_dir, n_card = apply(parsed, content, tree, quiet=quiet)
        if not quiet and (n_dir or n_card):
            print("  [导入] ticket：目录说明 %d 条 · 结论 %d 条" % (n_dir, n_card))
        # 新补的说明顺带填进 report.json 里空着的描述位。
        # 只填空、不重排 —— 人工润色过的文件不会被挪动。
        if n_dir and tree:
            try:
                rep.fill_dirs(data, content)
            except Exception as e:
                sys.stderr.write("[warn] 回填 report.json 失败：%s\n" % e)

    newp, cnt = gen(data, content, path, limit)
    return n_dir + n_card, newp, cnt
