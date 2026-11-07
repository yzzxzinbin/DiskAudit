# -*- coding: utf-8 -*-
r"""路径形态自检：ticket 导入时的规范化 + 模板占位符展开。

夹具一律用虚构的账户名（C:\Users\User）—— 这个文件要进仓库，
写死本机账户名等于把「谁在跑」泄出去。

数据文件里存的是真实绝对路径，所以这里防两件事：
    · 真实路径过一遍 heal 不能被改坏（导入时 AI 填的路径要走这一道）
    · 模板里的占位符仍要能展开（purpose.py 的规则库、web/index.html 的文案还在用）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket, pathvars

FAKE_HOME = r"C:\Users\User"
ctx = pathvars.context(FAKE_HOME, "C:")
bad = 0


def check(label, got, want):
    global bad
    ok = got == want
    bad += 0 if ok else 1
    print("  %-40s -> %-36s %s"
          % (label, repr(got), "OK" if ok else "FAIL want " + repr(want)))


print("旧 ticket 残留的占位符仍要规范化（%DRIVE%\\X 缺冒号）")
for src, want in [
    (r"%DRIVE%\Program Files", r"%DRIVE%:\Program Files"),
    (r"%DRIVE%\Program Files (x86)\X", r"%DRIVE%:\Program Files (x86)\X"),
    (r"C\Program Files", r"C:\Program Files"),
    (r"%DRIVE%:\Program Files", r"%DRIVE%:\Program Files"),      # 已正确，不动
    (r"%HOME%\AppData\Local", r"%HOME%\AppData\Local"),
    (r"%HOME%", r"%HOME%"),
    (r"%DRIVE%:", r"%DRIVE%:"),
    ("", ""),
    (None, None),
]:
    check(repr(src), ticket.heal(src), want)

print()
print("真实绝对路径过 heal 必须原样返回")
for real in [
    "C:",
    "C:\\",
    r"C:\Program Files",
    r"C:\Program Files (x86)\Some App",
    FAKE_HOME,
    FAKE_HOME + r"\AppData\Local",
    r"D:\SomeDir\Nested Folder",
    r"E:\SomeRepo\content\drives\D\purpose.json",
]:
    check(real, ticket.heal(real), real)

print()
print("模板占位符展开：%DRIVE% / %SYSDRIVE% / %HOME% / %USER%")
d_ctx = pathvars.context(FAKE_HOME, "D:", "C:")
for src, want in [
    ("%DRIVE% 盘顶层", "D 盘顶层"),
    ("%SYSDRIVE%:\\Windows", r"C:\Windows"),
    ("%DRIVE%:\\SomeDir", r"D:\SomeDir"),
    ("%HOME%\\AppData", FAKE_HOME + r"\AppData"),
    ("%USER% 的配置", "User 的配置"),
    ("没有占位符的文本", "没有占位符的文本"),
]:
    check(src, pathvars.expand_str(src, d_ctx), want)

print()
print("家目录 / 细化根 / 系统盘 三者不能混")
fake_tree = {"home": FAKE_HOME, "subRoot": r"D:\Work", "systemDrive": "C:"}
for label, got, want in [
    ("home_of", pathvars.home_of(fake_tree), FAKE_HOME),
    ("focus_of", pathvars.focus_of(fake_tree), r"D:\Work"),
    ("sysdrive_of", pathvars.sysdrive_of(fake_tree), "C"),
]:
    check(label, got, want)

print()
print("失败 %d 项" % bad)
sys.exit(1 if bad else 0)
