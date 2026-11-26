# -*- coding: utf-8 -*-
"""不靠人点：验证"切到非对话页，状态条多快消失"。

自建一个假的 WorkBuddy 窗口当 owner，自己画侧边栏底色；改一改"入口行有没有
选中高亮"，就等价于你在界面里点了「新建任务 / 助理 / 资料库…」。

用例：
  A. 入口行亮起（= 切到新建任务/助理…）→ 状态条要在 0.6 秒内收起
  B. 亮块撤掉（= 切回对话页）→ 0.6 秒内回来
  C. probe 报 persisted=false 的空白新任务 → 立刻收起（不等像素）
  D. 空白页发出第一条消息（会话被持久化）→ 条回来并显示 1 轮 5 分
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes as wt

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)
try:
    u32.SetProcessDPIAware()          # 与真实 bar 进程同一坐标体系（DIP×S）
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TP_SELFTEST", "1")   # 假窗口进程不是 workbuddy.exe，放行进程名校验
import hud as B  # noqa: E402
B.init_env()
S = B.S

u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u32.FindWindowW.restype = wt.HWND
u32.GetWindowTextLengthW.argtypes = [wt.HWND]
u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT),
                ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
u32.CreateWindowExW.restype = wt.HWND
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.DefWindowProcW.restype = ctypes.c_ssize_t
u32.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
u32.BeginPaint.restype = wt.HDC
u32.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
u32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
g32.CreateCompatibleDC.argtypes = [wt.HDC]
g32.CreateCompatibleDC.restype = wt.HDC
g32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
g32.CreateCompatibleBitmap.restype = wt.HBITMAP
g32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
g32.SelectObject.restype = wt.HGDIOBJ
g32.DeleteDC.argtypes = [wt.HDC]
g32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
g32.BitBlt.restype = wt.BOOL
SRCCOPY = 0x00CC0020
g32.CreateSolidBrush.argtypes = [wt.DWORD]
g32.CreateSolidBrush.restype = wt.HBRUSH
g32.DeleteObject.argtypes = [wt.HANDLE]

TITLE = "WBBarHideTest"
TEST_CLASS = "TPulseHideTest"
SID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SID_BLANK = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"

WIN_W, WIN_H = 1600, 820                       # px（= 1066 x 546 DIP）
SIDE_DIP = 280.0                               # 假窗口的侧边栏宽度（DIP）
# 入口行的高亮：真机实测「新建任务」96~130 DIP，行高 34、步距 36
HILITE = {"on": False,
          "rect": (B.sc(11), B.sc(96), B.sc(275), B.sc(130))}
# 「最近」列表里那个正在看的会话：真机实测第一项 364~398 DIP。
# 它的有无就是"在不在对话页"的分水岭——入口页时这块不存在。
HIST = {"on": True,
        "rect": (B.sc(11), B.sc(364), B.sc(275), B.sc(398))}
# 模拟左上角点头像弹出的那块面板：一张很高的浅灰卡片压在侧边栏下半部分。
# 曾经它会被当成"某个入口被选中"，于是打开它条子就消失。高度 106 DIP，
# 超出"一行高亮 18~52 DIP"的窗口，所以永远不算高亮。
CARD = {"on": False,
        "rect": (B.sc(5), B.sc(200), B.sc(280), B.sc(306))}


def rec_user(uid, ts):
    return {"type": "message", "role": "user", "id": uid, "timestamp": ts,
            "content": [{"type": "text", "text": "hi"}]}


def rec_asst(rid, ts, credit, tok, pin, hit):
    return {"type": "message", "role": "assistant", "timestamp": ts,
            "content": [{"type": "text", "text": "ok"}],
            "providerData": {
                "conversationRequestId": rid,
                "usage": {"input_tokens": pin, "output_tokens": tok - pin,
                          "total_tokens": tok},
                "rawUsage": {"credit": credit, "total_tokens": tok,
                             "prompt_tokens": pin, "prompt_cache_hit_tokens": hit}}}


def wndproc(h, m, w, l):
    if m == 0x000F:                                   # WM_PAINT
        ps = PAINTSTRUCT()
        wdc = u32.BeginPaint(h, ctypes.byref(ps))
        # 双缓冲：整幅画好再一次性贴上去。以前直接画到窗口 DC，bar 那条线程有机会
        # 在"底色已擦、高亮还没画"的中间时刻截图，偶发地只看到半幅画面，判定就
        # 抖一下——真机 renderer 是原子合成的，不会有这一帧。
        hdc = g32.CreateCompatibleDC(wdc)
        bmp = g32.CreateCompatibleBitmap(wdc, WIN_W, WIN_H)
        old = g32.SelectObject(hdc, bmp)
        br = g32.CreateSolidBrush(0x00FFFFFF)          # 内容区纯白
        u32.FillRect(hdc, ctypes.byref(wt.RECT(0, 0, WIN_W, WIN_H)), br)
        g32.DeleteObject(br)
        brsb = g32.CreateSolidBrush(0x00DBDBDB)        # 侧边栏 219
        u32.FillRect(hdc, ctypes.byref(wt.RECT(0, 0, B.sc(SIDE_DIP), WIN_H)), brsb)
        g32.DeleteObject(brsb)
        for flag, color in ((HILITE, 0x00D0D0D0), (HIST, 0x00D0D0D0),
                            (CARD, 0x00D3D3D3)):
            if flag["on"]:
                x0, y0, x1, y1 = flag["rect"]
                b2 = g32.CreateSolidBrush(color)
                u32.FillRect(hdc, ctypes.byref(wt.RECT(x0, y0, x1, y1)), b2)
                g32.DeleteObject(b2)
        g32.BitBlt(wdc, 0, 0, WIN_W, WIN_H, hdc, 0, 0, SRCCOPY)
        g32.SelectObject(hdc, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(hdc)
        u32.EndPaint(h, ctypes.byref(ps))
        return 0
    return u32.DefWindowProcW(h, m, w, l)


def title_of(hwnd):
    n = u32.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    b = ctypes.create_unicode_buffer(n + 3)
    u32.GetWindowTextW(hwnd, b, n + 1)
    return b.value


def visible_bar(hwnd):
    """park() 把它挪到 -20000，所以用位置判断有没有收起来"""
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left > -10000


def repaint(hwnd):
    """强制立刻重画：InvalidateRect+UpdateWindow 在假窗口上偶尔不落地，
    用 RedrawWindow(RDW_UPDATENOW) 才稳。"""
    u32.RedrawWindow(hwnd, None, None, 0x0001 | 0x0004 | 0x0100 | 0x0080)
    time.sleep(0.35)


def logic_cases():
    """直接喂像素：整条侧边栏扫出来的高亮块位置对不对，以及据此下的结论。

    画面用真机实测的数值造：底色 247、高亮 235（差 12）、入口行 96~130 /
    132~166 / … / 276~310 DIP、历史第一行 364~398 DIP、「最近」标题 348。
    """
    sw = B.sc(270)
    sh = B.sc(456)                         # 相当于 84~540 DIP 这一段
    split = B.sc(B.SB_NAV_SPLIT_DIP - B.SB_NAV_TOP_DIP)
    tol = B.sc(6)

    def mk(bands):
        buf = (ctypes.c_ubyte * (sw * sh * 4))()
        for i in range(0, len(buf), 4):
            buf[i] = buf[i + 1] = buf[i + 2] = 247
            buf[i + 3] = 255
        for d0, d1, v in bands:                        # 用"带内 DIP"给
            row = bytes([v, v, v, 255]) * sw
            y0 = max(0, B.sc(d0))
            y1 = min(sh, B.sc(d1))
            for y in range(y0, y1):
                buf[y * sw * 4:(y + 1) * sw * 4] = row
        return buf

    nav1 = (96 - 84, 130 - 84)                         # 新建任务
    nav6 = (276 - 84, 310 - 84)                        # 发现
    hist = (364 - 84, 398 - 84)                        # 最近列表第一项

    cases = [
        # 名字, 画哪些块, 光标带内 Y, 期望结论
        ("对话页：只有历史项高亮", [(hist[0], hist[1], 235)], B.sc(300), True),
        ("入口页：新建任务高亮", [(nav1[0], nav1[1], 235)], B.sc(300), False),
        ("入口页：发现高亮", [(nav6[0], nav6[1], 235)], B.sc(200), False),
        ("对话页 + 鼠标悬停入口行", [(nav1[0], nav1[1], 235), (hist[0], hist[1], 235)],
         B.sc(30), True),
        ("入口页 + 鼠标悬停另一入口行", [(nav1[0], nav1[1], 235), (nav6[0], nav6[1], 235)],
         B.sc(200), False),
        ("什么高亮都没有（缩略视图/被遮挡）", [], B.sc(300), None),
        ("只压着一块入口高亮（刚点完）", [(nav1[0], nav1[1], 235)], B.sc(30), False),
        ("深色主题：底色 30 / 高亮 45", [(nav1[0], nav1[1], 45)], B.sc(300), False),
    ]
    good = True
    for name, shapes, cur, want in cases:
        if name.startswith("深色主题"):
            base = 30
        else:
            base = 247
        buf = mk([(a, b, v) for a, b, v in shapes]) if base == 247 else None
        if buf is None:                                # 深色主题单独造
            buf = (ctypes.c_ubyte * (sw * sh * 4))()
            for k in range(0, len(buf), 4):
                buf[k] = buf[k + 1] = buf[k + 2] = 30
                buf[k + 3] = 255
            row = bytes([45, 45, 45, 255]) * sw
            for y in range(B.sc(nav1[0]), B.sc(nav1[1])):
                buf[y * sw * 4:(y + 1) * sw * 4] = row
        bands = B.sidebar_bands(buf, sw, sh)
        got = B.nav_verdict(bands, split, cur, tol)
        if got != want:
            good = False
            print("[FAIL] G 像素逻辑 %-26s 期望 %r 实得 %r (bands=%s)" % (name, want, got, bands))
        else:
            print("[ OK ] G 像素逻辑 %-26s -> %r (bands=%s)" % (name, got, bands))
    return good


def gate_cases():
    """横向几何闸门：弹层打开时读数变宽 15 DIP，不许跟着平移。"""
    b = B.Bar.__new__(B.Bar)
    b.rect = lambda: type("R", (), {"left": 0, "top": 0,
                                    "right": 2600, "bottom": 1700})()
    real_key = B.user32.GetAsyncKeyState
    real_cpos = B.user32.GetCursorPos
    good = True

    def one(name, raw, cur_dip, down, relearn, want):
        b.left_dip = 213.0
        b.edge_locked = True
        b.edge_relearn = relearn
        b.measure_side_left = lambda: raw
        B.user32.GetAsyncKeyState = lambda v: 0x8000 if down else 0

        def cpos(p):
            o = ctypes.cast(p, ctypes.POINTER(B.wt.POINT)).contents
            o.x = int((raw + cur_dip) * B.S)      # cur_dip：指针相对候选边界的偏移
            o.y = 0
            return 1
        B.user32.GetCursorPos = cpos
        got = b.side_left()
        nonlocal good
        if abs(got - want) > 0.8:
            good = False
            print("[FAIL] I 几何闸门 %-28s 期望 %.1f 实得 %.1f" % (name, want, got))
        else:
            print("[ OK ] I 几何闸门 %-28s -> %.1f" % (name, got))

    one("弹层打开（读数 228）", 228.0, 60, False, False, 213.0)
    one("读数没变（213）", 213.0, 60, False, False, 213.0)
    one("窗口尺寸刚变，允许重学", 228.0, 60, False, True, 228.0)
    one("用户拖着边界（指针压线）", 228.0, 0, True, False, 228.0)
    one("鼠标按着但离线很远", 228.0, 60, True, False, 213.0)
    B.user32.GetAsyncKeyState = real_key
    B.user32.GetCursorPos = real_cpos
    return good


def aux_win_cases():
    """窗口筛选：设置面板这类辅助窗口不该再挂一条状态条。

    真机实测两个窗口同进程、同类名（Chrome_WidgetWin_1）、同标题（WorkBuddy）、
    都没有 owner，唯一区别是主窗口有最小化按钮：
        主窗口   style=0x14C70000
        设置窗口 style=0x14C50000   ← 少了 WS_MINIMIZEBOX
    这里造两个假窗口，只差这一位，看筛选结果。放在屏幕外，不闪。
    """
    WS_MAIN = 0x14C70000
    WS_AUX = 0x14C50000
    h_main = u32.CreateWindowExW(0x00000100, "Chrome_WidgetWin_1", "WorkBuddy",
                                 WS_MAIN, 4000, 3000, 800, 600, None, None, None, None)
    h_aux = u32.CreateWindowExW(0x00000100, "Chrome_WidgetWin_1", "WorkBuddy",
                                WS_AUX, 4000, 3000, 800, 600, None, None, None, None)
    good = True
    try:
        if not h_main or not h_aux:
            print("[FAIL] K 窗口筛选 建不出假窗口 %r %r" % (h_main, h_aux))
            return False
        for name, h, want in (("主窗口（有最小化按钮）", h_main, True),
                              ("设置面板（没有最小化按钮）", h_aux, False)):
            got = B.is_main_win(h)
            ok = (got == want)
            good = good and ok
            print("[%s] K 窗口筛选 %-22s 该挂条=%r" % (" OK " if ok else "FAIL", name, got))
        wins = B.wb_windows()
        ok = (h_main in wins) and (h_aux not in wins)
        good = good and ok
        print("[%s] K 窗口筛选 wb_windows 只收主窗口" % (" OK " if ok else "FAIL"))
    finally:
        for h in (h_main, h_aux):
            if h:
                u32.DestroyWindow(h)
    return good


def pick_cases():
    """nav_verdict：只看高亮块在「最近」上面还是下面，以及鼠标压在哪。

    这是整套判定的核心——不再区分"悬停还是选中"，因为侧边栏任何时刻恰好有
    一行是选中的：入口页的选中行在「最近」上方，对话页的选中行在「最近」里。
    """
    split = B.sc(340 - 84)                     # 带内 256 DIP
    tol = B.sc(6)
    nav = (B.sc(12), B.sc(46))                 # 新建任务那一行（带内）
    hist = (B.sc(280), B.sc(314))              # 历史第一项（带内）
    cases = [
        ("只有入口行亮着、鼠标在别处", [nav], B.sc(300), False),
        ("只有入口行亮着、鼠标正压着它", [nav], B.sc(30), False),
        ("只有历史项亮着、鼠标在别处", [hist], B.sc(30), True),
        ("历史项 + 悬停入口行（鼠标在入口行）", [nav, hist], B.sc(30), True),
        ("历史项 + 悬停历史项（鼠标在历史项）", [hist], B.sc(300), True),
        ("两块入口行（悬停 + 选中），鼠标压一块", [nav, (B.sc(192), B.sc(226))],
         B.sc(30), False),
        ("入口块 + 历史块都不在鼠标下（过渡帧）", [nav, hist], B.sc(150), None),
        ("一块都没有（看不清）", [], B.sc(300), None),
    ]
    good = True
    for name, bands, cur, want in cases:
        got = B.nav_verdict(bands, split, cur, tol)
        if got != want:
            good = False
            print("[FAIL] J 选中/悬停 %-40s 期望 %r 实得 %r" % (name, want, got))
        else:
            print("[ OK ] J 选中/悬停 %-40s -> %r" % (name, got))
    return good


def hover_cases():
    """only_under_cursor：判断"唯一那块高亮是否正被鼠标压着"（决定要不要等一会儿）。

    从对话页把鼠标扫过「发现」，会凭空多出一块悬停高亮；它就在鼠标下面。
    这种情况不能立刻判成切页，否则条子会闪一下（用户报过）。
    """
    tol = 9
    cases = [
        ("单块、鼠标正压着 → True", [(100, 136)], 110, True),
        ("单块、鼠标在别处 → False", [(100, 136)], 400, False),
        ("两块、鼠标压着上面那块 → False", [(100, 136), (300, 336)], 110, False),
        ("空 → False", [], 110, False),
    ]
    good = True
    for name, bands, cur, want in cases:
        got = B.only_under_cursor(bands, cur, tol)
        if got != want:
            good = False
            print("[FAIL] L 悬停保护 %-34s 期望 %r 实得 %r" % (name, want, got))
        else:
            print("[ OK ] L 悬停保护 %-34s -> %r" % (name, got))
    return good


def main():
    tmp = tempfile.mkdtemp(prefix="wbhide_")
    proj = os.path.join(tmp, "projects", "p1")
    os.makedirs(proj)
    now = int(time.time() * 1000)
    with open(os.path.join(proj, SID_A + ".jsonl"), "w", encoding="utf-8") as f:
        for r in (rec_user("ua1", now - 9000),
                  rec_asst("ra1", now - 8000, 1.5, 1000, 800, 700),
                  rec_user("ua2", now - 7000),
                  rec_asst("ra2", now - 6000, 2.5, 2000, 1600, 1500)):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    dlog = os.path.join(tmp, "daemon.log")
    rlog = os.path.join(tmp, "renderer.log")
    open(dlog, "w", encoding="utf-8").close()
    open(rlog, "w", encoding="utf-8").close()

    proc_cb = WNDPROC(wndproc)
    hinst = ctypes.WinDLL("kernel32").GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(proc_cb, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = "Chrome_WidgetWin_1"
    u32.RegisterClassW(ctypes.byref(wc))
    owner = u32.CreateWindowExW(0x00000008 | 0x00000080, "Chrome_WidgetWin_1", TITLE,
                                # 带上 WS_MINIMIZEBOX：真主窗口 style=0x14C70000，
                                # 没有这一位的窗口会被当成设置面板一类的辅助窗口
                                0x80000000 | 0x10000000 | 0x00020000, 4, 4, WIN_W, WIN_H,
                                None, None, hinst, None)
    if not owner:
        print("创建测试窗口失败")
        return 1

    def probe(sid, persisted=True):
        with open(dlog, "a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "t", "scope": "daemon",
                                "message": ['[LocalConversationHistorySource] history-probe',
                                            {"conversationId": sid,
                                             "persisted": persisted}]},
                               ensure_ascii=False) + "\n")

    env = dict(os.environ)
    env["TP_MUTEX"] = r"Local\TPulseHideTest"
    env["TP_CLASS"] = TEST_CLASS
    env["TP_OWNER_TITLE"] = TITLE
    env["TP_LOG_ROOT"] = os.path.join(tmp, "projects")
    env["TP_DAEMON_LOG"] = dlog
    env["TP_RENDERER_LOG"] = rlog
    env["TP_LOG"] = os.path.join(tmp, "_test_hud.log")
    env["TP_SELFTEST"] = "1"        # 假窗口进程不是 workbuddy.exe，放行进程名/前台校验
    bar_dir = os.path.dirname(os.path.abspath(__file__))
    logf = open(os.path.join(bar_dir, "_barhide.log"), "wb")
    p = subprocess.Popen([sys.executable, "hud.py"], cwd=bar_dir, env=env,
                         stdout=logf, stderr=subprocess.STDOUT)

    ok = True

    def wait_state(want_visible, limit=3.0):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < limit:
            if visible_bar(bar) == want_visible:
                return (time.perf_counter() - t0) * 1000
            time.sleep(0.01)
        return None

    def verdict(name, dt, note=""):
        nonlocal ok
        if dt is None:
            ok = False
            print("[FAIL] %-30s 超时 %s" % (name, note))
        else:
            print("[ OK ] %-30s %6.0f ms %s" % (name, dt, note))

    try:
        cur = wt.POINT()
        u32.GetCursorPos(ctypes.byref(cur))
        u32.SetCursorPos(1600, 950)                 # 鼠标别停在扫描区里

        probe(SID_A)
        bar = 0
        for _ in range(200):
            bar = u32.FindWindowW(TEST_CLASS, None)
            if bar:
                break
            time.sleep(0.05)
        if not bar:
            print("状态条进程未起来")
            return 1
        for _ in range(200):
            if visible_bar(bar) and "对话2轮" in title_of(bar):
                break
            time.sleep(0.02)
        print("初始：", title_of(bar))

        # A. 入口行亮起 + 历史项不再高亮 = 真的切到新建任务/助理…
        #    （真机上就是这样：入口页的选中行是入口，对话项的选中块同时消失）
        HILITE["on"] = True
        HIST["on"] = False
        repaint(owner)
        verdict("A 切到非对话页→收起", wait_state(False))

        # A2. 鼠标停在同一块上（只是路过悬停，没点）→ 不该隐藏
        # 先回到对话页：历史项高亮、入口行不亮
        HIST["on"] = True
        HILITE["on"] = False
        repaint(owner)
        wait_state(True)
        # 再从对话页把鼠标停到入口行上：只多出一块"悬停高亮"，页面没变
        HILITE["on"] = True
        repaint(owner)
        rr = wt.RECT()
        u32.GetWindowRect(owner, ctypes.byref(rr))
        cx = HILITE["rect"][0] + 20
        cy = HILITE["rect"][1] + 8
        # 每分钟都有人（用户）在动鼠标，所以每 0.1 秒把光标按回入口行再判
        bad = False
        for _ in range(10):
            u32.SetCursorPos(rr.left + cx, rr.top + cy)
            time.sleep(0.1)
            if not visible_bar(bar):
                bad = True
        if bad:
            ok = False
            print("[FAIL] A2 只是悬停就收起了（hover 被当成选中）")
        else:
            print("[ OK ] A2 悬停不误判              仍在显示")
        u32.SetCursorPos(1600, 950)

        # B. 鼠标离开入口行（悬停块消失）= 只剩历史项 = 对话页
        HILITE["on"] = False
        repaint(owner)
        verdict("B 切回对话页→回来", wait_state(True))
        print("     回来时的显示：", title_of(bar))

        # C. probe 报空白新任务（persisted=false，没有日志）
        probe(SID_BLANK, persisted=False)
        verdict("C 空白新任务→立刻收起", wait_state(False))

        # D. 从空白页发出第一条消息：同一会话被持久化，条回来并显示 1 轮
        with open(os.path.join(proj, SID_BLANK + ".jsonl"), "w", encoding="utf-8") as f:
            for r in (rec_user("ub1", now),
                      rec_asst("rb1", now + 500, 5.0, 600, 500, 400)):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        probe(SID_BLANK, persisted=True)
        verdict("D 空白页发首条→回来", wait_state(True))
        t = title_of(bar)
        for _ in range(80):                        # session_log 有 1 秒 miss 缓存
            if "对话1轮" in t and "5.00分" in t:
                break
            time.sleep(0.05)
            t = title_of(bar)
        print("     首条之后的显示：", t)
        if "对话1轮" not in t or "5.00分" not in t:
            ok = False
            print("[FAIL] D 应显示 对话1轮 · 消耗5.00分")

        # E. 点在头像上弹出那块面板（高灰卡片压住侧边栏下半）→ 不能因此收起
        CARD["on"] = True
        repaint(owner)
        time.sleep(1.2)
        if not visible_bar(bar):
            ok = False
            print("[FAIL] E 弹层压住侧边栏就消失了（这就是用户报的问题）")
        else:
            print("[ OK ] E 弹层压住侧边栏          仍在显示")

        # F. 关掉弹层 → 还是对话页，继续显示
        CARD["on"] = False
        repaint(owner)
        time.sleep(0.8)
        if not visible_bar(bar):
            ok = False
            print("[FAIL] F 关掉弹层后反而收起了")
        else:
            print("[ OK ] F 关掉弹层                仍在显示")

        # G. 纯像素逻辑：整条侧边栏扫出的高亮块位置 + 结论
        #    （假窗口一次 WM_PAINT 里同时画两块，重绘不稳定，所以这一段直接喂像素）
        verdict_logic = logic_cases()
        if not verdict_logic:
            ok = False
        # I. 横向几何闸门：弹层把读数改宽 15 DIP 时不许跟着平移
        if not gate_cases():
            ok = False
        # J. 高亮块：选中 vs 路过悬停
        if not pick_cases():
            ok = False
        # K. 窗口筛选：设置面板这类辅助窗口不该挂条
        if not aux_win_cases():
            ok = False
        # L. 悬停保护：唯一那块高亮在鼠标下面时要等一会儿再判
        if not hover_cases():
            ok = False
        verdict("H 恢复对话页→回来", wait_state(True))

        u32.SetCursorPos(cur.x, cur.y)
    finally:
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            pass
        logf.close()
        try:
            shutil.copy(os.path.join(tmp, "_test_hud.log"),
                        os.path.join(bar_dir, "_barhide.log"))
        except Exception:
            pass
        try:
            u32.DestroyWindow(owner)
        except Exception:
            pass

    print("结论：" + ("全部通过" if ok else "有失败项，见上"))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
