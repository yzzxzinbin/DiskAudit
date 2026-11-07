# -*- coding: utf-8 -*-
"""路径占位符：让**模板**不绑定某一台机器。

占位符只出现在源码里的模板（purpose.py 的 EXACT / NAMES / PATHS、web/index.html 的文案），
落盘的数据文件一律是真实绝对路径 —— 那些文件不进版本库、不对外发布，没有隐藏的必要，
而且令牌会让「这条属于哪个盘」看不出来，读的时候还得多解析一层。

    %HOME%    家目录，如 C:\\Users\\alice
    %USER%    账户名，如 alice
    %DRIVE%   被扫描的盘符（不带冒号），如 D
    %SYSDRIVE% Windows 系统盘（不带冒号），如 C

展开端是 scripts/purpose.py、scripts/ticket.py、scripts/report.py、scripts/app.py、
scripts/build.py —— 都走这里，保证「页面显示」与「ticket 对账」口径一致。

家目录取值顺序：tree.json 的 home（采集时记录的真实用户家目录）→
subRoot（兼容旧数据）→ 当前进程的 ~。subRoot 是本次扫描的细化根，
可能只是一个工作目录，不能再把它当成家目录。
"""
import os
import re

TOKEN_RE = re.compile(r"%(HOME|USER|DRIVE|SYSDRIVE)%")


def home_of(tree, fallback=None):
    """从数据源里取用户家目录；旧数据没有 home 时退回 subRoot / 当前进程的 ~。"""
    home = ((tree or {}).get("home") or "").strip()
    if home:
        return home.rstrip("\\/")
    sub = ((tree or {}).get("subRoot") or "").strip()
    if sub:
        return sub.rstrip("\\/")
    return (fallback or os.path.expanduser("~")).rstrip("\\/")


def focus_of(tree):
    """本次细化扫描的根目录；报告的「重点目录」按它找。"""
    focus = ((tree or {}).get("subRoot") or "").strip()
    return (focus or home_of(tree)).rstrip("\\/")


def sysdrive_of(tree):
    """系统盘盘符；优先用采集时记录的值，旧数据退回环境变量。"""
    drv = ((tree or {}).get("systemDrive") or "").strip()
    if not drv:
        drv = os.environ.get("SystemDrive") or "C:"
    return (drv.rstrip(":\\/") or "C")


def context(home, drive="C:", sysdrive=None):
    """拼出占位符 -> 实际值 的映射表。"""
    home = (home or "").rstrip("\\/")
    user = re.split(r"[\\/]", home)[-1] if home else ""
    drv = (drive or "C:").strip().rstrip(":\\/") or "C"
    sysdrv = (sysdrive or os.environ.get("SystemDrive") or "C").strip().rstrip(":\\/") or "C"
    return {"%HOME%": home, "%USER%": user, "%DRIVE%": drv, "%SYSDRIVE%": sysdrv}


def expand_str(s, ctx):
    if "%" not in s:
        return s
    return TOKEN_RE.sub(lambda m: ctx.get(m.group(0), m.group(0)), s)


def expand(obj, ctx):
    """递归展开 dict / list / str 里的占位符；其它类型原样返回。"""
    if isinstance(obj, str):
        return expand_str(obj, ctx)
    if isinstance(obj, list):
        return [expand(x, ctx) for x in obj]
    if isinstance(obj, dict):
        return dict((k, expand(v, ctx)) for k, v in obj.items())
    return obj


def count(obj):
    """还剩多少个占位符（自检用：展开后应当为 0）。"""
    if isinstance(obj, str):
        return len(TOKEN_RE.findall(obj))
    if isinstance(obj, list):
        return sum(count(x) for x in obj)
    if isinstance(obj, dict):
        return sum(count(v) for v in obj.values())
    return 0
