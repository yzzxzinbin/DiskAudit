# -*- coding: utf-8 -*-
"""本机磁盘枚举与选盘参数推导。

页面上的「选择磁盘」需要两样东西：本机有哪些盘、每个盘扫过没有。
前者走 kernel32 的三个 API（GetLogicalDriveStrings / GetDriveType /
GetDiskFreeSpaceEx），和 assemble.total_ram() 同一思路 —— 不依赖 WMI 或 PowerShell。
后者由调用方从 data/drives/ 的留档补齐。

只列固定盘与可移动盘。网络盘扫一遍可能几小时，光驱没有文件系统，都不在
「磁盘分析」的适用范围内。
"""
import os
import re
import sys

GB = 1024.0 ** 3

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3

# 采集脚本会按阶段打印这些标记，UI 据此显示当前进行到哪一步。
# 同一个标记可能反复出现（细化树单独扫时），最后出现的那条才算数。
PHASE_MARKS = (
    ("[1/3]", "tree"),
    ("[2/3]", "links"),
    ("[整形]", "merge"),
    ("[snap]", "merge"),
)

PHASE_LABEL = {
    "idle": "空闲",
    "start": "准备中",
    "tree": "扫描目录树",
    "links": "测量链接目标",
    "merge": "整形数据",
    "post": "刷新文字层",
    "done": "完成",
    "failed": "失败",
    "aborted": "已中止",
}


def norm_drive(s):
    """"d" / "D:" / "D:\\" 一律归一成 "D:"；不是盘符则返回空串。"""
    m = re.match(r"^\s*([A-Za-z])\s*:?\s*[\\/]*\s*$", s or "")
    return (m.group(1).upper() + ":") if m else ""


def same_drive(a, b):
    na, nb = norm_drive(a), norm_drive(b)
    return bool(na) and na == nb


def detail_root_for(drive, home=None, system_drive=None):
    """细化根只在扫描盘内才有意义 —— 盘外扫出来的枝接不进全盘树。

    系统盘用当前账户家目录（碎文件集中在 AppData）；别的盘没有天然候选，
    返回空串，表示这次不做细化。默认值和 update-data.ps1 保持一致。
    """
    d = norm_drive(drive)
    if not d:
        return ""
    h = home if home is not None else (os.environ.get("USERPROFILE") or "")
    h = h.replace("/", "\\").rstrip("\\")
    if not h or norm_drive(h[:2]) != d:
        return ""
    return h


def scan_phase(text):
    """从采集脚本的输出里认出当前阶段，认不出就是 start。"""
    cur = "start"
    for line in (text or "").splitlines():
        for mark, phase in PHASE_MARKS:
            if mark in line:
                cur = phase
                break
    return cur


def _win_volumes():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetLogicalDriveStringsW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    k32.GetLogicalDriveStringsW.restype = wintypes.DWORD
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT
    k32.GetDiskFreeSpaceExW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    ]
    k32.GetDiskFreeSpaceExW.restype = wintypes.BOOL
    k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    k32.GetVolumeInformationW.restype = wintypes.BOOL

    buf = ctypes.create_unicode_buffer(512)
    n = k32.GetLogicalDriveStringsW(len(buf), buf)
    if not n:
        return []
    # 这里是 \0 分隔的多字符串，切片取值会被第一个 \0 截断，按字符数整段读
    raw = ctypes.wstring_at(ctypes.addressof(buf), n * ctypes.sizeof(ctypes.c_wchar))

    sysdrive = norm_drive(os.environ.get("SystemDrive") or "C:")
    out = []
    for root in [s for s in raw.split("\x00") if s]:
        kind = k32.GetDriveTypeW(root)
        if kind not in (DRIVE_FIXED, DRIVE_REMOVABLE):
            continue

        total = ctypes.c_ulonglong()
        free = ctypes.c_ulonglong()
        _avail = ctypes.c_ulonglong()
        if not k32.GetDiskFreeSpaceExW(root, ctypes.byref(_avail),
                                       ctypes.byref(total), ctypes.byref(free)):
            continue
        cap = float(total.value)
        if cap <= 0:
            continue

        vol = ctypes.create_unicode_buffer(261)
        fsname = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        maxlen = wintypes.DWORD()
        flags = wintypes.DWORD()
        k32.GetVolumeInformationW(root, vol, len(vol), ctypes.byref(serial),
                                  ctypes.byref(maxlen), ctypes.byref(flags),
                                  fsname, len(fsname))

        letter = norm_drive(root)
        out.append({
            "root": letter,
            "label": vol.value.strip(),
            "fs": fsname.value.strip(),
            "kind": "removable" if kind == DRIVE_REMOVABLE else "fixed",
            "capGB": round(cap / GB, 2),
            "freeGB": round(free.value / GB, 2),
            "usedPct": round((cap - free.value) / cap * 100, 1),
            "system": letter == sysdrive,
        })
    out.sort(key=lambda v: v["root"])
    return out


def list_volumes():
    """本机磁盘列表；非 Windows 返回空列表。"""
    if os.name != "nt":
        return []
    return _win_volumes()


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    for v in list_volumes():
        print("%-3s %-12s %-6s %8.2f GB 已用 %5.1f%% 剩 %7.2f GB%s"
              % (v["root"], v["label"] or "-", v["fs"] or "-", v["capGB"],
                 v["usedPct"], v["freeGB"], "  [系统]" if v["system"] else ""))
