# -*- coding: utf-8 -*-
"""TokenPulse 安装 / 卸载工具

用法：
    python tpctl.py install     安装到启动文件夹（开机静默自启，随 WorkBuddy 启动）
    python tpctl.py remove      移除自启并停止所有相关进程
    python tpctl.py status      查看当前状态
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "guard.py")
HUD = os.path.join(HERE, "hud.py")
VBS_NAME = "TPulse.vbs"
CREATE_NO_WINDOW = 0x08000000


def startup_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    return p if os.path.isdir(p) else None


def pythonw_path():
    exe = sys.executable
    base = os.path.dirname(exe)
    for name in ("pythonw.exe", "python.exe"):
        cand = os.path.join(base, name)
        if os.path.isfile(cand):
            return cand
    return exe


def ps(cmd):
    try:
        subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cmd],
                       creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=25)
    except Exception:
        pass


def kill_all():
    # 只匹配 python 进程，避免 powershell 因命令行含关键字而误杀自身
    ps("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
       "($_.CommandLine -like '*hud.py*' -or $_.CommandLine -like '*guard.py*') } | "
       "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")


def start_guard():
    """启动守护进程；必须脱离当前进程树，否则会随父进程一起被回收"""
    pyw = pythonw_path()
    BREAKAWAY = 0x01000000
    NO_WINDOW = 0x08000000
    DETACHED = 0x00000008
    for flags in (BREAKAWAY | NO_WINDOW, BREAKAWAY | DETACHED, 0):
        try:
            subprocess.Popen([pyw, GUARD], cwd=HERE, creationflags=flags,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True)
            time.sleep(1.0)
            return True
        except OSError:
            continue
    return False


def _hud_mutex():
    try:
        sys.path.insert(0, HERE)
        import guard as w
        return w.hud_alive()
    except Exception:
        return False


def write_vbs(path):
    pyw = pythonw_path()
    body = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        'sh.CurrentDirectory = "%s"\r\n'
        'sh.Run """%s"" ""%s""", 0, False\r\n' % (HERE, pyw, GUARD)
    )
    # newline="" 必须加：否则 Windows 文本模式会把 body 里的 \r\n 再转一次，
    # 写成 \r\r\n，VBScript 解析就会把两行粘在一起，开机自启直接失效
    with open(path, "w", encoding="utf-16", newline="") as f:
        f.write(body)


def do_install():
    sd = startup_dir()
    if not sd:
        print("找不到启动文件夹，安装中止")
        return 1
    if not os.path.isfile(GUARD) or not os.path.isfile(HUD):
        print("缺少 guard.py 或 hud.py，安装中止")
        return 1
    vbs = os.path.join(sd, VBS_NAME)
    write_vbs(vbs)
    print("已写入开机自启：%s" % vbs)
    kill_all()
    ok = start_guard()
    if ok:
        print("守护进程已启动，状态条会随 WorkBuddy 自动出现")
    return 0 if ok else 1


def do_remove():
    sd = startup_dir()
    if sd:
        vbs = os.path.join(sd, VBS_NAME)
        if os.path.isfile(vbs):
            try:
                os.remove(vbs)
                print("已移除开机自启：%s" % vbs)
            except OSError as exc:
                print("移除自启失败：%r" % (exc,))
        else:
            print("开机自启未安装")
    kill_all()
    print("已停止守护进程与状态条")
    return 0


def do_status():
    sd = startup_dir()
    vbs = os.path.join(sd, VBS_NAME) if sd else None
    print("自启文件：%s" % (vbs if vbs and os.path.isfile(vbs) else "未安装"))
    print("程序目录：%s" % HERE)
    print("pythonw：%s" % pythonw_path())
    sys.path.insert(0, HERE)
    try:
        import guard as w
        print("WorkBuddy 在运行：%s" % w.wb_running())
        print("状态条在运行：%s" % w.hud_alive())
    except Exception as exc:
        print("状态检查失败：%r" % (exc,))
    return 0


if __name__ == "__main__":
    act = sys.argv[1] if len(sys.argv) > 1 else "status"
    if act == "install":
        sys.exit(do_install())
    if act == "remove":
        sys.exit(do_remove())
    sys.exit(do_status())
