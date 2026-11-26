# -*- coding: utf-8 -*-
"""TokenPulse · 守护进程

职责：
  1. 常驻后台，轮询 WorkBuddy.exe 是否在运行
  2. WorkBuddy 启动 -> 自动拉起 HUD
  3. WorkBuddy 退出 -> HUD 自行退出（hud.py 内置判断），本进程继续等待下次启动
  4. HUD 被误杀 -> 自动重启

本身无窗口，不占任务栏，可放入启动文件夹随系统自启。
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
HUD = os.path.join(HERE, "hud.py")
HUD_MUTEX = "Local\\TPulse"
SELF_MUTEX = "Local\\TPulseGuard"
SELF_HANDLE = None
WB_EXE = "workbuddy.exe"

user32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi")

k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                           ctypes.POINTER(wt.DWORD)]
k32.QueryFullProcessImageNameW.restype = wt.BOOL
k32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
k32.CreateMutexW.restype = wt.HANDLE
k32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.OpenMutexW.restype = wt.HANDLE
psapi.EnumProcesses.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD,
                                ctypes.POINTER(wt.DWORD)]
psapi.EnumProcesses.restype = wt.BOOL

SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CREATE_NO_WINDOW = 0x08000000      # 原值是 0x08000000，混入了无关位，已改正
ERROR_ALREADY_EXISTS = 183

LOGFILE = os.path.join(HERE, "_guard.log")


def log(msg):
    """pythonw 没有控制台，启动失败时什么都看不到 —— 一律落盘"""
    try:
        line = "%s  %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg)
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line)
        if os.path.getsize(LOGFILE) > 64 * 1024:
            with open(LOGFILE, "r", encoding="utf-8", errors="ignore") as f:
                keep = f.readlines()[-200:]
            with open(LOGFILE, "w", encoding="utf-8") as f:
                f.writelines(keep)
    except Exception:
        pass


def wb_running():
    """WorkBuddy.exe 是否有任何一个实例存活"""
    arr = (wt.DWORD * 4096)()
    need = wt.DWORD()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(need)):
        return False
    n = need.value // ctypes.sizeof(wt.DWORD)
    for i in range(n):
        pid = arr[i]
        if not pid:
            continue
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                if os.path.basename(buf.value).lower() == WB_EXE:
                    return True
        finally:
            k32.CloseHandle(h)
    return False


def hud_alive():
    h = k32.OpenMutexW(SYNCHRONIZE, False, HUD_MUTEX)
    if h:
        k32.CloseHandle(h)
        return True
    return False


def pythonw_path():
    exe = sys.executable
    base = os.path.dirname(exe)
    for name in ("pythonw.exe", "python.exe"):
        cand = os.path.join(base, name)
        if os.path.isfile(cand):
            return cand
    return exe


def launch_hud():
    try:
        subprocess.Popen([pythonw_path(), HUD], cwd=HERE,
                         creationflags=CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
        return True
    except Exception:
        return False


def acquire_singleton(poll=3.0, max_wait=15.0):
    """已有实例在跑就先等一会儿：
    - 旧实例若已退出（含被回收），立刻接管
    - 旧实例健康存活则超时退出，避免双击两次就永久残留一个空转进程
    """
    waited = 0.0
    while True:
        ctypes.set_last_error(0)
        h = k32.CreateMutexW(None, False, SELF_MUTEX)
        if h and ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            return h
        if h:
            k32.CloseHandle(h)
        if max_wait is not None and waited >= max_wait:
            return None
        time.sleep(poll)
        waited += poll


def main():
    log("启动：python=%s" % sys.executable)
    if not os.path.isfile(HUD):
        log("异常：找不到 %s" % HUD)
        return
    global SELF_HANDLE
    SELF_HANDLE = acquire_singleton()
    if SELF_HANDLE is None:
        log("已有守护进程在运行，本实例退出")
        return
    log("已取得单例，进入守护循环")

    idle = 0
    last_state = None
    while True:
        try:
            running = wb_running()
            if running:
                idle = 0
                if not hud_alive():
                    ok = launch_hud()
                    log("WorkBuddy 在运行，拉起状态条: %s" % ("成功" if ok else "失败"))
                    time.sleep(2.5)
                    log("状态条互斥体: %s" % ("已建立" if hud_alive() else "仍未建立"))
            else:
                idle += 1
                if idle > 7200:      # 约 6 小时无 WorkBuddy，自行退出省资源
                    log("6 小时未检测到 WorkBuddy，退出")
                    return
            if running != last_state:
                log("WorkBuddy 运行状态 -> %s" % running)
                last_state = running
        except Exception as e:
            log("循环异常: %r" % e)
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("崩溃退出: " + traceback.format_exc().replace("\n", " | "))
