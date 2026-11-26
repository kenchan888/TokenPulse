# -*- coding: utf-8 -*-
"""自检：状态条是否真的在跑、窗口在哪。退出码 0=正常，1=异常"""
import ctypes
import os
import sys
from ctypes import wintypes as wt

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:                      # 不声明 DPI 感知，读到的坐标会被系统按 150% 缩小 1.5 倍
    u32.SetProcessDPIAware()
except Exception:
    pass
k32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.OpenMutexW.restype = wt.HANDLE

ok = True

h = k32.OpenMutexW(0x00100000, False, r"Local\TPulse")
if h:
    k32.CloseHandle(h)
    print("[OK] 状态条进程已建立运行标记")
else:
    print("[失败] 状态条进程没有起来")
    ok = False

# 守护进程
found_wd = False
CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
u32.GetWindowTextLengthW.argtypes = [wt.HWND]
u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u32.IsWindowVisible.argtypes = [wt.HWND]

hits = []


def cb(hwnd, l):
    n = u32.GetWindowTextLengthW(hwnd)
    if n:
        b = ctypes.create_unicode_buffer(n + 3)
        u32.GetWindowTextW(hwnd, b, n + 1)
        if b.value.startswith("TPulse"):
            r = wt.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            hits.append((b.value, r.left, r.top, r.right - r.left,
                         r.bottom - r.top, bool(u32.IsWindowVisible(hwnd))))
    return True


u32.EnumWindows(CB(cb), 0)
if hits:
    for t, x, y, w, hh, vis in hits:
        onscreen = -10000 < x < 20000
        flag = "可见" if vis else "隐藏"
        print("[OK] 状态条窗口 %s 位置=(%d,%d) 尺寸=%dx%d %s%s"
              % (t, x, y, w, hh, flag, "" if onscreen or not vis else " <- 在屏幕外！"))
else:
    print("[失败] 找不到状态条窗口")

h = k32.OpenMutexW(0x00100000, False, r"Local\TPulseGuard")
if h:
    k32.CloseHandle(h)
    print("[OK] 守护进程在运行")
else:
    print("[提示] 守护进程未运行（状态条本身仍可独立工作，只是不会随 WorkBuddy 自动启停）")

sys.exit(0 if ok else 1)
