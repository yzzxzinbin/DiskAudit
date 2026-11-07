# -*- coding: utf-8 -*-
"""目录树动态下钻：路径校验 + 单层实时扫描。

这个模块只读，不依赖 HTTP，也不读写 data/。app.py 负责取 tree.root
和做请求来源校验，具体扫描放在这里，方便用临时目录直接做单元测试。

扫描规则：
    - 只返回目标目录的直接子项；
    - 目录的 b 是整棵子树的文件体积总和；
    - 小于 1KB 的文件不单独成行（体积仍计入 bytes 合计，前端以
      「其余零散项」行交代差额）——与静态树的门槛思路一致：碎项
      不值得一行，但不许从账上消失；
    - 单目录文件行最多 500 个（按体积从大到小截断，truncated 字段如实
      上报截断数）——文件爆炸的目录（node_modules 之类）会拖垮前端；
    - 目录子项不过滤（空目录也返回）；
    - 不跟随 reparse point（Windows junction 不是 symlink，必须看文件属性）。
"""
import os
import re
import stat

REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
DRIVE_ABS = re.compile(r"^[A-Za-z]:[\\/]")
FILE_ROW_MIN = 1024
MAX_FILE_ROWS = 500
FILE_ROW_MIN = 1024


class DrillError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def canonical_root(root):
    """把 tree.root 规范成带一个尾分隔符的盘根/目录路径。"""
    s = (root or "").strip().strip('"')
    if not s:
        raise DrillError(503, "扫描根路径为空")
    if re.match(r"^[A-Za-z]:$", s):
        s += "\\"
    if not DRIVE_ABS.match(s):
        raise DrillError(503, "扫描根不是盘符绝对路径：%s" % s)
    try:
        s = os.path.realpath(os.path.abspath(s))
    except OSError as e:
        raise DrillError(503, "扫描根无法解析：%s" % e)
    if not s.endswith(("\\", "/")):
        s += "\\"
    return s


def _norm_target(raw):
    s = (raw or "").strip().strip('"')
    if not s:
        raise DrillError(400, "缺少 path 参数")
    if not DRIVE_ABS.match(s):
        raise DrillError(400, "只支持盘符绝对路径")
    try:
        return os.path.realpath(os.path.abspath(s))
    except OSError as e:
        raise DrillError(400, "路径无法解析：%s" % e)


def resolve_dir(raw, root):
    root = canonical_root(root)
    p = _norm_target(raw)
    root_cmp = os.path.normcase(root).rstrip("\\/")
    p_cmp = os.path.normcase(p).rstrip("\\/")
    if p_cmp != root_cmp and not p_cmp.startswith(root_cmp + "\\"):
        raise DrillError(403, "路径不在扫描根内")
    if not os.path.exists(p):
        raise DrillError(404, "路径不存在：%s" % p)
    if not os.path.isdir(p):
        raise DrillError(400, "目标不是目录：%s" % p)
    return p


def _attrs(entry):
    st = entry.stat(follow_symlinks=False)
    return st, getattr(st, "st_file_attributes", 0)


def _sum_tree(path):
    """汇总 path 下所有后代文件体积，跳过 reparse point。"""
    total = 0.0
    errors = 0
    skipped = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            entries = os.scandir(d)
        except OSError:
            errors += 1
            continue
        try:
            for e in entries:
                try:
                    st, attrs = _attrs(e)
                except OSError:
                    errors += 1
                    continue
                if attrs & REPARSE:
                    skipped += 1
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        total += float(st.st_size)
                except OSError:
                    errors += 1
        finally:
            entries.close()
    return total, errors, skipped


def scan_dir(path):
    """返回 path 的直接子项；每个目录子项的 b 都是完整递归体积。
    小于 FILE_ROW_MIN 的文件不单独成行；文件行超过 MAX_FILE_ROWS 的按体积
    截断——两者的体积都留在 bytes 合计里，由前端零散项吸收。"""
    total = 0.0
    errors = 0
    skipped = 0
    children = []
    try:
        entries = os.scandir(path)
    except OSError as e:
        raise DrillError(500, "无法读取目录：%s" % e)
    try:
        for e in entries:
            try:
                st, attrs = _attrs(e)
            except OSError:
                errors += 1
                continue
            if attrs & REPARSE:
                skipped += 1
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    b, err, skip = _sum_tree(e.path)
                    errors += err
                    skipped += skip
                    children.append({"n": e.name, "b": b, "f": 0})
                    total += b
                else:
                    b = float(st.st_size)
                    total += b
                    if b >= FILE_ROW_MIN:
                        children.append({"n": e.name, "b": b, "f": 1})
            except OSError:
                errors += 1
    finally:
        entries.close()

    children.sort(key=lambda c: (-c["b"], (c["n"] or "").lower()))
    kept = []
    truncated = 0
    files = 0
    for c in children:
        if c["f"] == 1:
            files += 1
            if files > MAX_FILE_ROWS:
                truncated += 1
                continue
        kept.append(c)
    return {
        "path": os.path.normpath(path),
        "bytes": total,
        "children": kept,
        "truncated": truncated,
        "errors": errors,
        "skipped": skipped,
    }


def scan(raw_path, root):
    return scan_dir(resolve_dir(raw_path, root))
