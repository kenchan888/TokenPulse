# -*- coding: utf-8 -*-
"""幽灵窗口自查。

判断 DWM 有没有给我们的窗口盖 `Ghost` 类幽灵窗口（建了窗口不抽消息就会出）。
同时把自家的 TPulse / TPulseToday 窗口列出来，方便核对"只挂了一条"。

退出码 0 = 无幽灵；1 = 有幽灵。
"""
import ctypes
import ctypes.wintypes as wt
import sys

u = ctypes.windll.user32

ghosts = []
ours = []


@ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
def cb(h, _l):
    cls = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(h, cls, 256)
    t = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(h, t, 512)
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    row = (hex(h), cls.value, t.value, pid.value,
           (r.left, r.top, r.right, r.bottom), bool(u.IsWindowVisible(h)))
    if cls.value == "Ghost":
        ghosts.append(row)
    elif "TPulse" in t.value:
        ours.append(row)
    return True


u.EnumWindows(cb, 0)

print("HUD windows (%d):" % len(ours))
for x in ours:
    print("  ", x)
print("Ghost windows (%d):" % len(ghosts))
for x in ghosts:
    print("  ", x)
print("VERDICT:", "OK" if not ghosts else "FOUND GHOST - kill pythonw to clear")
sys.exit(0 if not ghosts else 1)
