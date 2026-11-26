# -*- coding: utf-8 -*-
"""TokenPulse · 对话用量仪表（主程序）

每个 WorkBuddy 主窗口各挂一条浮字条，显示该窗口当前会话的用量；
只有窗口停在对话页时才出现（专家 / 技能 / 连接器 / 定时任务 / 资料库等入口
页自动隐藏）。侧边栏底部另有三列「今日」KPI，口径见 references/ARCHITECTURE.md。

位置：与底部那句「内容由 AI 生成，请核实重要信息」同一行，字号比它大半号
（13 vs 12 DIP），字色更深一档（#4C4C4C），像是那句提示的延续。
左段：对话N轮 · 消耗X.XX分；右段：Token总计X.XXM · 缓存命中XX.X%。

数据来源（全部本地，不需要 API key）：
  · 会话日志 ~/.workbuddy/projects/*/<sessionId>.jsonl  —— 积分、token、命中率、完成状态
  · ~/.workbuddy/logs/daemon.log / renderer.log          —— 界面此刻显示哪个会话
  · workbuddy.db 的 sessions 表                          —— 兜底信号
"""
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from ctypes import wintypes as wt
from datetime import datetime

# ---------------------------------------------------------------- 基础常量
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

MUTEX_NAME = os.environ.get("TP_MUTEX") or "Local\\TPulse"
WND_CLASS = os.environ.get("TP_CLASS") or "TPulseWnd"
HOST_CLASS = WND_CLASS + "Host"
PANEL_CLASS = WND_CLASS + "T"    # 今日面板单独一个类名：和状态条同类别名会被按类枚举的人抓错
OWNER_TITLE = os.environ.get("TP_OWNER_TITLE") or "WorkBuddy"
# 自测模式：test_visibility 用 Python 进程开一个假 WorkBuddy 窗口跑回归，
# 进程名不是 workbuddy.exe、也抢不到系统前台。这个开关只放行这两道校验，
# 正常运行时永远为 False。
SELFTEST = os.environ.get("TP_SELFTEST") == "1"
ERROR_ALREADY_EXISTS = 183

FOLLOW_MS = 2                      # 位移期间的跟随间隔
try:
    FOLLOW_MS = max(1, int(os.environ.get("TP_FOLLOW_MS", FOLLOW_MS)))
except Exception:
    pass

HOME = os.environ.get("USERPROFILE") or os.path.expanduser("~")
WB_ROOT = os.path.join(HOME, ".workbuddy")
LOG_ROOT = os.environ.get("TP_LOG_ROOT") or os.path.join(WB_ROOT, "projects")
if not os.path.isdir(LOG_ROOT):
    for cand in (r"D:\.workbuddy\projects",):
        if os.path.isdir(cand):
            LOG_ROOT = cand
            break
DB_PATH = os.path.join(WB_ROOT, "workbuddy.db")
# 运行期产物（截图、今日账本）统一放这里，脚本目录只放代码
TMP_DIR = os.environ.get("TP_TMP_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_tmp")

# ---------------------------------------------------------------- 外观
# 说明：这里全部是 DIP（逻辑像素），运行时乘以屏幕缩放 S。写成物理像素会在
# 150% 屏上把字缩到 8 DIP 那么小，务必按 DIP 取值。
SS = 4                              # 超采样倍数：4 倍渲染再盒式降采样，消掉马赛克感
FONT_DIP = 13.0                     # 比「内容由 AI 生成…」（12）大半号，仍小于聊天正文（14）
BAR_H_DIP = 20.0
PAD_L_DIP = 4.0
PAD_R_DIP = 3.0
GAP_L_DIP = 300.0                   # 兜底起点（探测失败时才用）：侧边栏右缘 280 + 20 DIP
SIDE_YS_DIP = (180.0, 480.0)        # 两条横带，相距 300 DIP，用来验证阶跃是竖直贯穿的
SIDE_MIN_DIP = 120.0                # 把窗口左边框/阴影排除在外
SIDE_MAX_DIP = 700.0                # 边界最右可能位置
SIDE_GAP_DIP = 7.0                  # 左段文字距侧边栏右缘的空隙（约一个字符宽）
OCCLUDE_DIP = 22.0                  # 压在我们上面的窗口，按矩形外扩这么多 DIP 整段排除（吃掉 DWM 阴影）
EDGE_SHARP = 7                      # 分界线必须是"硬边"：相邻两像素的绿通道差 ≥ 这个值（阴影是软渐变，过不了）
GAP_R_DIP = 6.0                     # 距窗口右边（右段贴右对齐用）
GAP_B_DIP = 7.3                     # 兜底：距窗口底边。正常情况下按提示文案的墨迹中心现算
NOTICE_H_DIP = 44.0                 # 竖直探测带高度：从窗口底往上，盖住提示文案那一行
NOTICE_W_DIP = 110.0                # 只取提示文案中间一段，够判断竖直范围即可
GEO_POLL_S = 0.06                   # 拖动时两次抓屏的最小间隔
SIZE_POLL_S = 0.05                  # 窗口在前台时的 rect 轮询间隔（尺寸一变 50 ms 内就重排）
SIZE_IDLE_S = 0.20                  # 窗口不在前台时的轮询间隔（省电，肉眼看不出来）
GEO_WARM_S = 0.18                   # 刚变完 1.2 秒内的抓屏间隔
GEO_IDLE_S = 0.90                   # 静止时的抓屏间隔
MIN_W_DIP = 150.0

# 0.70 → 白底上压出 #4C4C4C(76,76,76)，与对话页顶部「共消耗 17.02 …」那行的字色同深
# （该处实测最深处 RGB 76；更浅的档位压出来是 #777777，明显发虚）
INK_ALPHA_DEFAULT = 0.70
try:
    INK_ALPHA = float(os.environ.get("TP_INK", str(INK_ALPHA_DEFAULT)))   # 文字透明度（黑字压底）
except Exception:
    INK_ALPHA = INK_ALPHA_DEFAULT
try:
    INK_GAMMA = float(os.environ.get("TP_GAMMA", "0.90"))  # <1 略加粗，抵消灰度抗锯齿的发虚
except Exception:
    INK_GAMMA = 0.90

OPACITY = 255
try:
    OPACITY = max(60, min(255, int(os.environ.get("TP_OPACITY", 255))))
except Exception:
    pass

ONLY_CHAT = (os.environ.get("TP_ONLY_CHAT") or "1").strip().lower() not in ("0", "off", "false", "no")
SESSION_LOCK = (os.environ.get("TP_SESSION") or "").strip()
DISP_W_DIP = 190.0                  # 「内容由 AI 生成，请核实重要信息」在 13 DIP 下实测约 190 DIP
SB_DIP = 23.0                       # 内容区右侧滚动条留白（决定提示文案的居中位置）
CLEAR_DIP = 16.0                    # 与提示文案之间最少留白
# ---- 侧边栏"选中态高亮"扫描区（整条侧边栏宽度 × 入口区到历史列表前段）
#
# 侧边栏只有一条规矩：**任何时刻恰好有一行是选中的**。
#   · 停在「新建任务 / 助理 / 专家·技能·连接器 / 定时任务 / 资料库 / 发现」
#     → 高亮在那六个入口行之一（在「最近」上方）；
#   · 停了某个对话 → 高亮在「最近」列表里那个会话上（在「最近」下方）。
# 所以判断"是不是对话页"只需要一件事：**高亮块在「最近」上面还是下面**。
# 不再猜"这块是悬停还是选中"，也不用记点击——重启、切页都不会漏。
#
# 真机实测（S=1.5，窗口 1156×772 DIP，侧边栏右缘 304 DIP）：
#   六个入口行 96~130 / 132~166 / 168~202 / 204~238 / 240~274 / 276~310
#   「最近」标题 ~348，历史第一行的选中块 364~398，底色 247、高亮 235（差 12）
SB_NAV_TOP_DIP = 84.0          # 扫描上沿：留 12 DIP 余量，别让「新建任务」那块贴边被当成"被切过"
SB_NAV_SPLIT_DIP = 340.0       # 入口区 / 历史区分界（「最近」标题在 348，两边都不碰）
SB_SCAN_BOT_DIP = 620.0        # 扫描下沿：历史列表前几行够认出"选中的那个对话"
# 横向只扫侧边栏靠左这一窄条（8~70 DIP）：入口行的图标就在这段里，一行高亮是
# "整行铺满"的，所以窄条一样认得出；而这一段紧贴窗口左边，别的窗口（记事本、
# 浏览器…）几乎不会压到这里——扫整条 300 DIP 时，随便一个窗口盖住侧边栏右半，
# 整块就"看不懂"，条子会停在旧状态（用户在新建任务页看到条子还在，就是这么来的）。
SB_SCAN_X0_DIP = 8.0
SB_SCAN_W_DIP = 62.0
SB_ROW_MIN_DIP = 18.0          # 一行高亮的合理高度（实测 34 DIP）
SB_ROW_MAX_DIP = 52.0
# 灰阶差门槛：底色 247 / 高亮 235 差 12，弹层白面板 255 差 8。
# 取 7 既认得出高亮（含主题对比更弱的机器），又不至于把白面板当高亮——
# 白面板那种几百 DIP 高的整块会被高度上限挡掉。
SB_DEV = 7
# 唯一含糊的情形：全带只有"鼠标正压着的那一块"（可能是路过悬停，也可能刚点完）。
# 让它持续这么久再认定切页，避免从对话页扫过入口时闪一下。
SB_HOVER_HOLD_S = 0.45

WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000
WS_POPUP = 0x80000000
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_MINIMIZEBOX = 0x00020000
GWLP_HWNDPARENT = -8
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_SHOWNOACTIVATE = 0x0004
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
TRANSPARENT = 1
HTTRANSPARENT = -1
GA_ROOT = 2
GW_HWNDNEXT = 2
SRCCOPY = 0x00CC0020
HALFTONE = 4
HWND_MESSAGE = -3

# ---------------------------------------------------------------- 结构体
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte),
                ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


DWMWA_EXTENDED_FRAME_BOUNDS = 9     # DWM 可见外框（不含最大化时多出来的隐形边框）
DWMWA_CLOAKED = 14                  # 窗口被 DWM 临时隐藏（虚拟桌面切换等）


# ---------------------------------------------------------------- API 签名
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.LoadCursorW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.LoadCursorW.restype = wt.HANDLE
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                       ctypes.POINTER(wt.SIZE), wt.HDC,
                                       ctypes.POINTER(wt.POINT), wt.DWORD,
                                       ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.SetProcessDPIAware.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetTopWindow.argtypes = [wt.HWND]
user32.GetTopWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetAncestor.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.DestroyWindow.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
user32.FillRect.restype = ctypes.c_int
user32.SetCursor.argtypes = [ctypes.c_void_p]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
gdi32.CreateSolidBrush.argtypes = [wt.DWORD]
gdi32.CreateSolidBrush.restype = wt.HBRUSH
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.PeekMessageW.restype = wt.BOOL
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
user32.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]

gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.CreateSolidBrush.argtypes = [wt.DWORD]
gdi32.CreateSolidBrush.restype = wt.HBRUSH
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wt.DWORD] * 8 + [wt.LPCWSTR]
gdi32.CreateFontW.restype = wt.HFONT
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wt.HGDIOBJ
gdi32.Ellipse.argtypes = [wt.HDC] + [ctypes.c_int] * 4
gdi32.SetTextColor.argtypes = [wt.HDC, wt.DWORD]
gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.GetTextExtentPoint32W.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int,
                                        ctypes.POINTER(wt.SIZE)]
gdi32.GetTextExtentPoint32W.restype = wt.BOOL
gdi32.TextOutW.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.LPCWSTR, ctypes.c_int]
gdi32.TextOutW.restype = wt.BOOL
gdi32.StretchBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wt.DWORD]
gdi32.StretchBlt.restype = wt.BOOL
gdi32.SetStretchBltMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.SetBrushOrgEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
kernel32.OpenMutexW.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]

SYNCHRONIZE = 0x00100000
GUARD_MUTEX = "Local\\TPulseGuard"


LOG_FILE = os.environ.get("TP_LOG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_hud.log")
_TRACE_T = [0.0]


def _trace_exists():
    return os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "_trace.flag"))


def _traw(msg):
    """排障用：不加去重，原样写一行（仅在 _trace.flag 存在时被调用）。"""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "_trace.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _trace_on():
    """排障开关：脚本目录下放一个 _trace.flag 就开，最多每 0.5 秒一条。"""
    if not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "_trace.flag")):
        return False
    now = time.time()
    if now - _TRACE_T[0] < 0.5:
        return False
    _TRACE_T[0] = now
    return True


CLICK_T = [0.0]        # 最近一次"按下去"的时刻（全局，谁读到都记）
_DOWN = [False]        # 上一次读到的"正按着"状态，用来自己找上升沿


def lbtn():
    """左键：(正按着, 最近 0.4 秒内按过)。

    "自上次查询以来按过"那一位（`ks & 1`）**不可靠**：本机实测注入点击根本不置位，
    几何线程每 20~60ms 也读这个键，轮到路由判定时经常已经被清掉。所以改成自己
    找上升沿：任何线程读到"按着"就记时间戳，判定线程只看这个时间戳。
    几何线程 20ms 一轮，人手一次点击 80~120ms，稳拿。
    """
    ks = user32.GetAsyncKeyState(1)
    down = bool(ks & 0x8000)
    now = time.time()
    if (ks & 0x0001) or (down and not _DOWN[0]):
        CLICK_T[0] = now
        if _trace_exists():
            _traw("LBUTTON edge tid=%d" % threading.get_ident())
    _DOWN[0] = down
    return down, (now - CLICK_T[0] < 0.4)

_log_last = [0.0, ""]


def log_err(tag, exc):
    """异常落盘（每秒最多一条同类），pythonw 下没有控制台可看"""
    msg = "%s: %s" % (tag, exc)
    now = time.time()
    if msg == _log_last[1] and now - _log_last[0] < 30:
        return
    _log_last[0], _log_last[1] = now, msg
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
        if os.path.getsize(LOG_FILE) > 64 * 1024:
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                keep = f.readlines()[-200:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(keep)
    except Exception:
        pass


def rc(r, g, b):
    return r | (g << 8) | (b << 16)


WHITE = rc(255, 255, 255)

# ---------------------------------------------------------------- 全局状态
S = 1.0
FONT_K = None            # 超采样用的字号（12 DIP × S × SS）
MEAS_DC = None
INK_TABLE = bytes(range(256))
PANEL_TABLE = bytes(range(256))    # 今日面板专用：满墨比状态条更深一档
QUIT = False
POS_LOCK = threading.Lock()
DRAW_LOCK = threading.RLock()      # 排版/绘制/定位：tick 线程与几何线程共用，避免抢 DIB
BARS = []
BARS_LOCK = threading.Lock()
LAST_FG = {}
DAY = {"today": 0.0, "total": 0.0, "ready": False}
DAY_LOCK = threading.Lock()


def sc(v):
    return int(round(v * S))


def fmt_k(n):
    """按规模换单位：万以内给数字，10K–999K 用 K，百万以上用 M。

    状态条窄，三位有效数字够看；真正需要精确值的场合不在这里。
    单位切换与拆分版共用 split_k，避免两处阈值漂移。
    """
    v, u = split_k(n)
    return v + u


# ---------------------------------------------------------------- DIB 工具
def new_dib(w, h):
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    ptr = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(dc, ctypes.byref(bmi), 0,
                                 ctypes.byref(ptr), None, 0)
    gdi32.SelectObject(dc, bmp)
    user32.ReleaseDC(None, screen)
    return dc, ptr, bmp


def drop_dib(dc, bmp):
    try:
        if bmp:
            gdi32.DeleteObject(bmp)
        if dc:
            gdi32.DeleteDC(dc)
    except Exception:
        pass


def rebuild_ink_table():
    """覆盖率 -> 最终 alpha 的查找表（含 gamma 加粗）"""
    global INK_TABLE, PANEL_TABLE

    def _tbl(alpha):
        t = bytearray(256)
        for i in range(256):
            v = (i / 255.0) ** INK_GAMMA
            a = int(round(255.0 * v * alpha))
            t[i] = 255 if a > 255 else (0 if a < 0 else a)
        return bytes(t)

    INK_TABLE = _tbl(INK_ALPHA)
    PANEL_TABLE = _tbl(TODAY_INK_ALPHA)


# ---------------------------------------------------------------- 会话库
_db_cache = [0.0, [], {}]
DB_GAP = 2.0


def db_sessions():
    """最近的会话（按"最后一次被写入"倒序）。只读打开，失败就返回上次结果。

    排序键取 updated_at 与 last_activity_at 的较大值：正在流式输出的会话
    last_activity_at 不一定最新，但 updated_at 一直在走，用它能认准"当前这个"。
    """
    now = time.time()
    if now - _db_cache[0] < DB_GAP:
        return _db_cache[1]
    rows = []
    used = {}
    try:
        uri = "file:%s?mode=ro" % DB_PATH.replace("\\", "/")
        conn = sqlite3.connect(uri, uri=True, timeout=0.4)
        try:
            cur = conn.execute(
                "select s.id, coalesce(s.custom_title,s.title), s.cwd, s.status, "
                "case when coalesce(s.last_activity_at,0) > s.updated_at "
                "     then s.last_activity_at else s.updated_at end as act, u.used "
                "from sessions s left join session_usage u on u.session_id = s.id "
                "where s.deleted_at is null "
                "and (s.is_background_automation is null or s.is_background_automation=0) "
                "order by act desc limit 24")
            for sid, title, cwd, status, act, u in cur:
                rows.append({"id": sid, "title": title or "", "cwd": cwd or "",
                             "status": status or "", "act": (act or 0) / 1000.0})
                if u:
                    used[sid] = int(u)
        finally:
            conn.close()
    except Exception:
        rows = _db_cache[1]
        used = _db_cache[2]
    _db_cache[0] = now
    _db_cache[1] = rows
    _db_cache[2] = used
    return rows


def session_used(sid):
    """该会话当前的上下文体量（会话表里的权威值，跟界面提示一致）"""
    if not sid:
        return None
    return _db_cache[2].get(sid)


_file_cache = [0.0, {}]
FILE_GAP = 15.0


def log_index():
    """sessionId -> jsonl 路径"""
    now = time.time()
    if _file_cache[1] and now - _file_cache[0] < FILE_GAP:
        return _file_cache[1]
    out = {}
    try:
        for proj in os.scandir(LOG_ROOT):
            if not proj.is_dir():
                continue
            try:
                for e in os.scandir(proj.path):
                    if e.name.endswith(".jsonl"):
                        out[e.name[:-6]] = e.path
            except OSError:
                pass
    except OSError:
        pass
    _file_cache[0] = now
    _file_cache[1] = out
    return out


def log_files():
    return list(log_index().values())


def session_log(sid):
    """sid -> 该会话的 jsonl 路径。新建任务刚发第一句话时日志才落盘，
    索引缓存（15 秒）还没刷新，这里直接到目录里找一次，免得显示成 0。"""
    if not sid:
        return None
    p = log_index().get(sid)
    if p:
        return p
    now = time.time()
    if now - _miss.get(sid, 0.0) < 1.0:
        return None
    _miss[sid] = now
    name = sid + ".jsonl"
    try:
        for proj in os.scandir(LOG_ROOT):
            if not proj.is_dir():
                continue
            fp = os.path.join(proj.path, name)
            if os.path.isfile(fp):
                _file_cache[1][sid] = fp
                return fp
    except OSError:
        pass
    return None


_miss = {}


# ---------------------------------------------------------------- 界面当前会话
# 界面每显示一个对话，都会去探这个对话的历史，daemon.log 里留下：
#   [LocalConversationHistorySource] history-probe {"conversationId":"<uuid>",...}
# 这行只在「该对话正被显示」时出现——打开、切换、刷新都有，和你发不发消息无关。
# 所以它是「用户眼睛现在看的是哪个对话」，比"最后被写入的那份日志"准得多：
# 切到旧对话不发消息也能立刻跟过去。
#
# 备用信号（probe 读不到时，比如日志轮转或版本改格式）：
#   renderer.log 的 [perf] [heartbeat] appState {..."currentConversationId":"..."}
# 每 5 秒一条，慢但稳。两个都没有才退回"数据库里最后被写入的会话"。
DAEMON_LOG = os.environ.get("TP_DAEMON_LOG") or os.path.join(WB_ROOT, "logs", "daemon.log")
RENDERER_LOG = os.environ.get("TP_RENDERER_LOG") or os.path.join(WB_ROOT, "logs", "renderer.log")

_ui = {"off": None, "sid": None, "persisted": None}
_hb = {"t": 0.0, "sid": None}


def uuid_after(txt, pos, key="conversationId"):
    """从 pos 起找到 key，再取出紧随其后的 36 位 uuid（容忍 JSON 套 JSON 的反斜杠）"""
    i = txt.find(key, pos)
    if i < 0:
        return None
    j = i + len(key)
    n = len(txt)
    while j < n and txt[j] in ':\\" \t':
        j += 1
    k = j
    while k < n and (txt[k].isalnum() or txt[k] == "-"):
        k += 1
    v = txt[j:k]
    return v if len(v) == 36 and v.count("-") == 4 else None


def ui_session():
    """界面此刻显示的是哪个会话（增量扫 daemon.log，几乎零成本）"""
    try:
        size = os.path.getsize(DAEMON_LOG)
    except OSError:
        return _hb_session()
    off = _ui["off"]
    if off is None or size < off:          # 首次 / 日志轮转
        off = max(0, size - 768 * 1024)    # 首次只看尾部，够找到当前显示的对话
    if size - off > 256 * 1024:            # 一次只吃尾部 256KB，够找最新那条 probe
        off = size - 256 * 1024
    _ui["off"] = off
    if size > off:
        try:
            with open(DAEMON_LOG, "rb") as f:
                f.seek(off)
                chunk = f.read(size - off)
                end = f.tell()
        except OSError:
            chunk = b""
            end = off
        if chunk:
            cut = chunk.rfind(b"\n")       # 半行留到下次
            if cut >= 0:
                txt = chunk[:cut].decode("utf-8", "ignore")
                _ui["off"] = end - (len(chunk) - cut - 1)
                i = txt.rfind("history-probe")
                if i >= 0:
                    sid = uuid_after(txt, i)
                    if sid:
                        _ui["sid"] = sid
                        _ui["persisted"] = bool_after(txt, i, "persisted")
            else:
                _ui["off"] = end
    return _ui["sid"] or _hb_session()


def bool_after(txt, pos, key):
    """取 pos 之后同一行的 "key":true/false，取不到返回 None"""
    i = txt.find(key, pos)
    if i < 0 or i - pos > 160:
        return None
    seg = txt[i:i + 40]
    j = seg.find("true")
    k = seg.find("false")
    if j < 0 and k < 0:
        return None
    if j >= 0 and (k < 0 or j < k):
        return True
    return False


def ui_blank():
    """界面是不是停在「新建任务」这种空白页。

    新建任务时 WorkBuddy 会先摊一个临时会话（probe 里 persisted=false），
    它的日志文件还不存在——这种页面就是"还没开始聊"，条上不该留着上一个
    对话的数字。一旦发出第一条消息，同一个会话会被持久化，probe 变
    persisted=true、日志落盘，条就自动回来接着算。
    """
    sid = _ui.get("sid")
    if not sid or _ui.get("persisted") is not False:
        return False
    return session_log(sid) is None


def _hb_session():
    """备用：renderer 心跳里的 currentConversationId，5 秒一条"""
    now = time.perf_counter()
    if now - _hb["t"] < 1.0:
        return _hb["sid"]
    _hb["t"] = now
    try:
        size = os.path.getsize(RENDERER_LOG)
        with open(RENDERER_LOG, "rb") as f:
            f.seek(max(0, size - 512 * 1024))
            txt = f.read().decode("utf-8", "ignore")
    except OSError:
        return _hb["sid"]
    i = txt.rfind("heartbeat] appState")
    if i >= 0:
        sid = uuid_after(txt, i, "currentConversationId") or uuid_after(txt, i, "onversationId")
        if sid:
            _hb["sid"] = sid
    return _hb["sid"]



# ---------------------------------------------------------------- 今日 / 累计
TAIL_BYTES = 24 * 1024 * 1024
FULL_MAX = 64 * 1024 * 1024      # 小于这个体积就整份读，全对话统计才准
_day_files = {}      # path -> (offset, today_sum, total_sum)
_day_bounds = [0.0, 0.0]


def period_bounds():
    lt = time.localtime()
    start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    lt2 = time.localtime(start + 90000)
    end = int(time.mktime((lt2.tm_year, lt2.tm_mon, lt2.tm_mday, 0, 0, 0, 0, 0, -1)))
    return float(start), float(end)


def scan_usage():
    """一次增量扫描，同时算出「今日」和「累计」。

    只解析含 credit 的行，5MB 日志全程不到 0.3s；之后按偏移增量读。
    """
    start, end = period_bounds()
    if start != _day_bounds[0]:
        _day_bounds[0], _day_bounds[1] = start, end
        # 只把「今日」清零，偏移和「累计」保留，免得跨零点后重扫一遍全量
        _day_files.update({p: (v[0], 0.0, v[2]) for p, v in _day_files.items()})
    today = 0.0
    total = 0.0
    keep = {}
    for p in log_files():
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        off, ds, ts = _day_files.get(p, (0, 0.0, 0.0))
        if size < off:
            off, ds, ts = 0, 0.0, 0.0
        if size > off:
            try:
                with open(p, "rb") as f:
                    f.seek(off)
                    chunk = f.read()
            except OSError:
                keep[p] = (off, ds, ts)
                today += ds
                total += ts
                continue
            cut = chunk.rfind(b"\n")
            if cut < 0:
                chunk, newoff = b"", off
            else:
                newoff = off + cut + 1
                chunk = chunk[:cut]
            for raw in chunk.split(b"\n"):
                if b'"credit"' not in raw:
                    continue
                try:
                    o = json.loads(raw)
                except Exception:
                    continue
                pd = o.get("providerData")
                if not isinstance(pd, dict):
                    continue
                ru = pd.get("rawUsage")
                if not isinstance(ru, dict):
                    continue
                c = ru.get("credit")
                if not isinstance(c, (int, float)) or isinstance(c, bool):
                    continue
                ts2 = parse_ts(o.get("timestamp"))
                ts += float(c)
                if ts2 is not None and start <= ts2 < end:
                    ds += float(c)
            off = newoff
        keep[p] = (off, ds, ts)
        today += ds
        total += ts
    with DAY_LOCK:
        _day_files.clear()
        _day_files.update(keep)
        DAY["today"] = today
        DAY["total"] = total
        DAY["ready"] = True


def parse_ts(ts):
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        v = float(ts)
        return v / 1000.0 if v > 1e11 else v
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            try:
                return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                return None
    return None


# ---------------------------------------------------------------- 单会话日志
class Reader:
    """增量读某个会话的 jsonl，按「请求」聚合出条上要的四个数。

    判据全部来自日志字段，不读界面：
      · 完成一轮：该请求最后一条记录是 assistant 消息，且带 usage、无 error
      · 中断    ：providerData.error 非空（Interrupted by user）
      · 回退    ：resend-fork-notice.editedUserItemId 指向的用户消息，
                  它对应的整个请求作废，无论有没有答完
      · 消耗 / Token / 缓存命中：所有调用累加（中断、回退的也算，钱确实花了）
    """

    def __init__(self):
        self.path = None
        self.off = 0
        self.turns = []          # (ts, inp, out, cache) 最近调用，用来判断是否在跑
        self.reqs = {}           # reqId -> {ok, err, cr, tok, pin, phit, t}
        self.killed = set()      # 被回退作废的 reqId
        self.user2req = {}       # user 消息 id -> reqId
        self.pending = None      # 刚读到、还没配上请求的 user 消息 id
        self.credit_total = 0.0  # 全部调用的积分（含中断、回退）
        self.req_credit = {}
        self.last_req = ""
        self.reset_all()

    def reset_all(self):
        self.turns = []
        self.reqs = {}
        self.killed = set()
        self.user2req = {}
        self.pending = None
        self.credit_total = 0.0
        self.req_credit = {}
        self.last_req = ""

    def set_path(self, path):
        if path == self.path:
            return
        self.path = path
        self.reset_all()
        try:
            size = os.path.getsize(path) if path else 0
        except OSError:
            size = 0
        # 小日志整份读（全对话统计才准），超大日志才退化成只读尾部
        self.off = 0 if size <= FULL_MAX else max(0, size - TAIL_BYTES)

    def poll(self):
        """吃新内容；返回 True 表示数字可能变了（外面据此决定要不要重排重画）"""
        if not self.path:
            return False
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self.off:              # 文件被重写/压缩，退回全量
            self.off = 0
            self.reset_all()
        if size == self.off:
            return False
        try:
            with open(self.path, "rb") as f:
                f.seek(self.off)
                chunk = f.read()
                end = f.tell()
        except OSError:
            return False
        cut = chunk.rfind(b"\n")         # 只吃到最后一个完整行，半行留到下次
        if cut < 0:
            return False
        data = chunk[:cut]
        self.off = end - (len(chunk) - cut - 1)

        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                o = json.loads(raw.decode("utf-8", "ignore"))
            except Exception:
                continue
            typ = o.get("type")
            pd = o.get("providerData")
            if not isinstance(pd, dict):
                pd = {}

            # ---- 回退：把被编辑消息对应的请求作废
            if typ == "resend-fork-notice":
                eid = o.get("editedUserItemId")
                rid = self.user2req.get(eid) if isinstance(eid, str) else None
                if rid:
                    self.killed.add(rid)
                continue

            if typ == "message" and o.get("role") == "user":
                uid = o.get("id")
                if isinstance(uid, str):
                    self.pending = uid
                continue

            rid = pd.get("conversationRequestId")
            if not isinstance(rid, str) or not rid:
                continue
            if self.pending:
                self.user2req[self.pending] = rid
                self.pending = None

            e = self.reqs.get(rid)
            if e is None:
                e = {"ok": False, "err": False, "cr": 0.0, "tok": 0,
                     "pin": 0, "phit": 0, "t": 0.0}
                self.reqs[rid] = e
            ts = parse_ts(o.get("timestamp"))
            if ts and (not e["t"] or ts < e["t"]):
                e["t"] = ts

            ru = pd.get("rawUsage")
            if isinstance(ru, dict):
                c = ru.get("credit")
                if isinstance(c, (int, float)) and not isinstance(c, bool):
                    e["cr"] += float(c)
                    self.credit_total += float(c)
                tk = ru.get("total_tokens")
                if isinstance(tk, (int, float)) and not isinstance(tk, bool):
                    e["tok"] += int(tk)
                pin = ru.get("prompt_tokens")
                hit = ru.get("prompt_cache_hit_tokens")
                if isinstance(pin, (int, float)) and not isinstance(pin, bool):
                    e["pin"] += int(pin)
                if isinstance(hit, (int, float)) and not isinstance(hit, bool):
                    e["phit"] += int(hit)
                if ts:
                    self.turns.append(
                        (ts, int(pin or 0), int(tk or 0), int(hit or 0)))

            # 完成判定只看该请求「最后一条记录」：后续若还有工具调用，ok 自动回落
            err = pd.get("error")
            e["err"] = bool(err)
            e["ok"] = (typ == "message" and o.get("role") == "assistant"
                       and isinstance(pd.get("usage"), dict) and not e["err"])

        if len(self.turns) > 600:
            del self.turns[:len(self.turns) - 600]
        return True

    # ---- 指标
    @property
    def n_reply(self):
        """有效轮数 = 答完的请求 − 被回退作废的"""
        return sum(1 for rid, e in self.reqs.items()
                   if e["ok"] and rid not in self.killed)

    @property
    def cr_total(self):
        """本窗口全部消耗（含中断、回退、压缩、取标题）"""
        return sum(e["cr"] for e in self.reqs.values())

    @property
    def tok_total(self):
        """本窗口全部模型调用的 token 合计"""
        return sum(e["tok"] for e in self.reqs.values())

    def hit_rate(self):
        recent = self.turns[-20:]
        if not recent:
            return None
        tin = sum(t[1] for t in recent)
        tc = sum(t[3] for t in recent)
        return (tc / tin * 100.0) if tin else 0.0

    def rate(self):
        tail = self.turns[-3:]
        if len(tail) >= 2:
            dt = tail[-1][0] - tail[0][0]
            if 0.4 < dt < 120:
                return sum(t[2] for t in tail) / dt
        return None

    def context(self):
        return self.turns[-1][1] if self.turns else None

    def running(self):
        return bool(self.turns) and (time.time() - self.turns[-1][0]) < 180

    def turn_credit(self):
        if not self.last_req:
            return None
        return self.req_credit.get(self.last_req, 0.0)

    def hit_all(self):
        pin = sum(e["pin"] for e in self.reqs.values())
        hit = sum(e["phit"] for e in self.reqs.values())
        return (hit / pin * 100.0) if pin else None


# ---------------------------------------------------------------- 文案
def split_k(n):
    """fmt_k 的拆分版：数值与小一号的单位分开给，方便把单位画小半档

    阈值 999950 而不是 1000000：999,999 按 K 四舍五入会变成「1000.0K」，
    得让它提档显示成「1.00M」。
    """
    n = float(n or 0)
    if n >= 999950:
        return ("%.2f" % (n / 1000000.0), "M")
    if n >= 10000:
        return ("%.1f" % (n / 1000.0), "K")
    return ("{:,}".format(int(n)), "")


def today_empty(vals):
    """空态判据：今天一笔都还没落（或首批扫描还没回来）→ 面板不露脸。

    宁可什么都没有，也不要显示「0轮 0.00分 0.00M」——那看着像坏了。
    """
    if not vals:
        return True
    return not (vals.get("n") or vals.get("credit") or vals.get("tok"))


def today_anchors(w):
    """三列锚点（像素）：首列贴左 0、中列精确居中 w/2、尾列贴右（右缘留 1 DIP）"""
    return (0.0, w / 2.0, float(w - max(1, sc(1.0))))


def today_panel_w(bar=None):
    """面板宽度（像素）= 侧边栏宽 - 左右各 12 DIP，跟着拖动实时变。

    侧边栏宽度只有状态条量得准（它一直在跟侧边栏右缘，`left_dip` 就是
    右缘 + 7 DIP）。写死 256 会让拖窄后第三列跑到对话区上、拖宽后三列挤在
    左边一小块——面板宽必须跟侧边栏同宽。
    """
    edge_dip = None
    if bar is not None:
        ld = getattr(bar, "left_dip", None)
        # 读数太小说明还没测到侧边栏（左段起点还停在默认值），别拿它算宽度
        if ld and ld > SIDE_GAP_DIP + 80:
            edge_dip = ld - SIDE_GAP_DIP
    if not edge_dip:
        edge_dip = TODAY_W_DIP + 2 * TODAY_GAP_L_DIP
    w_dip = edge_dip - 2 * TODAY_GAP_L_DIP
    return sc(max(TODAY_MIN_W_DIP, min(w_dip, TODAY_MAX_W_DIP)))


def valid_sep(dip):
    """灰线候选必须落在账号行分隔线的合理区间，否则是动画/弹层的假线。"""
    return TODAY_SEP_MIN_DIP <= dip <= TODAY_SEP_MAX_DIP


def obsc_step(state, hit, clear_n=6):
    """遮挡滞回：命中立刻藏；要连续 clear_n 帧没命中才恢复。

    滚轮划过列表时行与行之间的空白会让单帧判定"没被挡"，没有滞回就会
    一帧现一帧隐地闪。state 是 {"on":bool, "clean":int}，可直接单测。
    """
    if hit:
        state["on"] = True
        state["clean"] = 0
    elif state["on"]:
        state["clean"] += 1
        if state["clean"] >= clear_n:
            state["on"] = False
            state["clean"] = 0
    return state["on"]


def obsc_band_ranges(guard):
    """把 (起点DIP, 高度DIP) 的横带定义换成像素区间 [(ya, yb)]。"""
    out = []
    for a_dip, dh_dip in OBSC_BANDS_DIP:
        ya = guard + sc(a_dip)
        yb = ya + sc(dh_dip)
        if yb > ya:
            out.append((ya, yb))
    return out


def rects_overlap(x0, y0, x1, y1, rects):
    """扫描区 (x0,y0,x1,y1) 是否与任一窗口矩形相交。"""
    for l, t, r, b in rects:
        if l < x1 and r > x0 and t < y1 and b > y0:
            return True
    return False


# 外部程序窗口缓存：30ms 级别的循环不必每轮 EnumWindows。
EXT_WND_MARGIN_DIP = 12.0
# 这些是 Windows 自己的全屏壳层（桌面、输入法、托盘），它们永远"盖着"一切，
# 把它们算进遮挡名单会让面板永远不藏——账号菜单就盖不住了。
EXT_WND_SKIP_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                        "Windows.UI.Core.CoreWindow", "ThumbnailDeviceHelperWnd")
_EXT_WND = [0.0, []]


def external_window_rects(owner=0, ttl=0.2):
    """其它程序（不含 WorkBuddy、自己、系统壳层）的可见顶层窗口矩形。

    WorkBuddy 的侧边栏有透明区，后面的记事本/浏览器会从那里透出来；这些像素
    不能当成"展开了最近列表"。账号菜单、最近列表都属 WorkBuddy 自己，不在
    名单里，照旧触发隐藏。
    """
    now = time.time()
    if now - _EXT_WND[0] < ttl:
        return _EXT_WND[1]
    me = kernel32.GetCurrentProcessId()
    wb_pid = 0
    h = owner
    if not h or not user32.IsWindow(h):
        ws = wb_windows_cached()
        h = ws[0] if ws else 0
    if h:
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        wb_pid = p.value
    out = []
    margin = sc(EXT_WND_MARGIN_DIP)

    def cb(h, l):
        if not user32.IsWindowVisible(h) or user32.IsIconic(h):
            return True
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(h, cls, 128)
        # 自己的面板/状态条（可能有多个实例、不同 pid）也不能算外部窗口
        if cls.value.startswith(WND_CLASS):
            return True
        if cls.value in EXT_WND_SKIP_CLASSES:
            return True
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value in (me, wb_pid) or not p.value:
            return True
        rr = wt.RECT()
        if not user32.GetWindowRect(h, ctypes.byref(rr)):
            return True
        if rr.right - rr.left < 80 or rr.bottom - rr.top < 80:
            return True
        out.append((rr.left - margin, rr.top - margin,
                    rr.right + margin, rr.bottom + margin))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    _EXT_WND[0] = now
    _EXT_WND[1] = out
    return out


def obsc_foreign(g, bg, alpha=None):
    """一条灰度带里"非底色"像素数 / 总采样数。

    g 是 8 位灰度字节，bg 是本带底色众数；alpha 同长，>0 表示那一点是
    面板自己画的字（屏幕截图里能看到，但不该当成遮挡物），统计时挖掉。
    底色按众数算而不是写死 247：全屏/不同主题的白底不会再被误判成弹层。
    """
    if not g:
        return 0, 0
    table = bytes(1 if abs(i - bg) >= OBSC_DEV else 0 for i in range(256))
    if alpha:
        masked = bytearray(g)
        for i, a in enumerate(alpha):
            if a:
                masked[i] = bg
        return sum(masked.translate(table)), len(g)
    return sum(g.translate(table)), len(g)


def align_x(align, left, tw):
    """把一个宽 tw 的「数值+单位」按对齐方式摆到基准位 left 上

    align: 0 左对齐 / 1 居中 / 2 右对齐。抽出来纯粹为了能纯逻辑测。
    """
    if align == 1:
        return left - tw / 2.0
    if align == 2:
        return left - tw
    return left


def compose_left(n, total, tight, with_total):
    """左下：对话n轮 · 消耗n.分（消耗=本对话全部轮次的积分合计）"""
    parts = ["对话%d轮" % (n or 0)]
    if with_total and total is not None:
        parts.append("消耗%.2f分" % total)
    return ("·" if tight else " · ").join(parts)


def compose_right(tok, hit, short):
    """右下：Token总计53.12M · 缓存命中99.3%"""
    if not tok:
        return ""
    s = "Token总计%s" % fmt_k(tok)
    if hit is not None and not short:
        s += " · 缓存命中%.1f%%" % hit
    return s


def sidebar_bands(buf, sw, sh):
    """在侧边栏这条带子里找所有"整行铺满高亮"的块，返回 [(y0, y1)]（带内像素）。

    两个要点，都是被真机教训出来的：
      · **按"行"判，不按某个窄条判**：一行高亮是整行铺满的，遮住一部分也认得出；
      · **按"够不够多列都变暗"判，不按中位数判**：文字行只有少量列是墨色，
        占比上不去，不会被当成高亮。

    高度窗口 18~52 DIP 用来挡弹层：左上角头像弹出的面板是几百 DIP 高的整块，
    整块都超出窗口，不算高亮——否则弹层一开就会被误判成"某个入口被选中"。
    """
    step = max(1, sc(2))                        # 每 2 DIP 一行，34 DIP 的行切得出 17 行
    xs = list(range(0, sw, max(2, sw // 40)))   # 横向抽 40 列
    rows = []
    hist = {}
    for y in range(0, sh, step):
        base = y * sw * 4
        vals = []
        for x in xs:
            i = base + x * 4
            v = (buf[i + 2] * 299 + buf[i + 1] * 587 + buf[i] * 114) // 1000
            vals.append(v)
            hist[v] = hist.get(v, 0) + 1
        rows.append(vals)
    if not rows:
        return []
    bg = max(hist.items(), key=lambda kv: kv[1])[0]
    n = float(len(xs))
    hmin = sc(SB_ROW_MIN_DIP)
    hmax = sc(SB_ROW_MAX_DIP)
    need = max(1, sc(10) // step)               # 至少连续 10 DIP 才算一块
    out = []
    run = 0
    off = 0
    for i, vals in enumerate(rows):
        dark = sum(1 for v in vals if abs(v - bg) >= SB_DEV)
        if dark >= 0.6 * n:
            if run == 0:
                off = i * step
            run += 1
        else:
            if run >= need and hmin <= run * step <= hmax:
                out.append((off, off + run * step))
            run = 0
    if run >= need and hmin <= run * step <= hmax:
        out.append((off, off + run * step))
    return out


def nav_verdict(bands, split, cur, tol):
    """高亮块落在「最近」上面还是下面 → True=对话页 / False=入口页 / None=看不出。

    bands=[(y0,y1)]（带内像素）；split=入口区下沿（带内像素）；cur=光标带内 Y。
    鼠标压着的那一块可能是"路过悬停"，所以：只要有一块**不在**鼠标下面，
    它就是真正的选中行，直接下结论。全都在鼠标下面（刚点完还没松手）时返回
    保守结果，由调用方决定要不要等 SB_HOVER_HOLD_S 再认。
    """
    nav = [b for b in bands if b[0] < split]
    hist = [b for b in bands if b[0] >= split]

    def under(b):
        return b[0] - tol <= cur <= b[1] + tol

    nav_out = [b for b in nav if not under(b)]      # 没被鼠标压着 = 真正的选中行
    hist_out = [b for b in hist if not under(b)]
    if nav_out and hist_out:
        # 入口区和历史区各有一块都不在鼠标下面：正常界面不会这样（任何时刻只有
        # 一行是选中的），只可能是"鼠标刚从入口行划到中间，悬停高亮还没淡掉"的
        # 过渡帧。分不清就别动，免得条子闪一下。
        return None
    if nav_out:
        return False                    # 入口行选中 = 不在对话页
    if hist_out:
        return True                     # 历史项选中 = 正在看某个对话
    if nav and not hist:
        return False                    # 只有鼠标压着的入口块
    if hist:
        return True
    return None                         # 一块都没有：不猜


def only_under_cursor(bands, cur, tol):
    """带子里所有高亮块都压在鼠标下面（= 可能是路过悬停）。"""
    if not bands:
        return False
    return all(b[0] - tol <= cur <= b[1] + tol for b in bands)

class Bar:
    def __init__(self, owner):
        self.owner = owner
        self.hwnd = 0
        self.w = sc(MIN_W_DIP)
        self.h = sc(BAR_H_DIP)
        self.dc = self.ptr = self.bmp = None
        self.mdc = self.mptr = self.mbmp = None
        self.ddc = self.dptr = self.dbmp = None      # 截图用
        self.reader = Reader()
        self.sid = None
        self.vis = False
        self.chat = True
        self.miss = 0
        # 上一轮"这是不是对话页"判断不了（侧边栏被弹层压住等）。判断不了的时候
        # 顺便冻结横向几何：弹层会糊住侧边栏右缘，硬测会让左段凭空平移十几 DIP。
        self.unknown = False
        # 横向几何的"可信闸门"。侧边栏宽度只会在两种情况下变：用户拖着它，或
        # 窗口尺寸刚变。别的时候读数一变（点头像弹出的面板比侧边栏宽 15 DIP、
        # 被浮层糊住）就不认——否则条子会跟着弹层右移一下再弹回来。
        self.edge_locked = False           # 首次读到侧边栏位置后就锁住
        self.edge_relearn = True           # 允许重学一次（窗口尺寸变了）
        self.sig = None
        self.text = "对话0轮 · 消耗0.00分"
        self.text_r = ""
        self.tight = True
        self.with_total = True
        self.left_dip = GAP_L_DIP          # 左段起点：侧边栏右缘 + SIDE_GAP_DIP，实时跟着拖动走
        self.gap_b_dip = GAP_B_DIP         # 距窗口底边：按「内容由 AI 生成…」的墨迹中心现算
        self.nc_off = None                 # 提示文案墨迹中心距窗口底的距离（像素）
        self.pdc = self.pptr = self.pbmp = None   # 探测用的一行宽 DIB
        self.pw = 0
        self.ndc = self.nptr = self.nbmp = None   # 提示文案竖直探测用的窄带 DIB
        self.nw = self.nh = 0
        self.last_size = None              # 上次的窗口尺寸，变了才重新找提示文案
        self.geo_pending = True            # 强制下一轮几何探测
        self.nc_t = 0.0                    # 上次重测提示文案的时刻
        self.geo_hot = 0.0                 # 最近一次几何变化的时刻
        self._ar = None                    # 缓存：压在我们上面的窗口矩形
        self._ar_t = 0.0
        self.stop = False
        self.last_check = 0.0
        self.nav_click_t = 0.0             # 最近一次"点在入口行"的时刻（切页后立刻收起，不等重绘）
        self.nav_hold_t = 0.0              # 只有"鼠标压着的那块高亮"时的起始时刻（防悬停误判）
        self.dw = self.dh = 0
        self._create()
        self.mk_dibs()

    # ---- 窗口
    def _create(self):
        hinst = kernel32.GetModuleHandleW(None)
        ex = WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED
        self.hwnd = user32.CreateWindowExW(ex, WND_CLASS, "TPulse", WS_POPUP,
                                           -20000, -20000, self.w, self.h,
                                           None, None, hinst, None)
        if self.hwnd:
            user32.SetWindowLongPtrW(self.hwnd, GWLP_HWNDPARENT,
                                     ctypes.c_void_p(self.owner))
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def close(self):
        """只拆窗口；位图交给 tick 线程收尾，避免绘制中途被释放"""
        self.stop = True
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = 0

    def _free(self):
        drop_dib(self.dc, self.bmp)
        drop_dib(self.mdc, self.mbmp)
        drop_dib(self.ddc, self.dbmp)
        drop_dib(self.pdc, self.pbmp)
        drop_dib(self.ndc, self.nbmp)
        self.dc = self.mdc = self.ddc = None
        self.pdc = None
        self.pw = 0
        self.ndc = None
        self.nw = self.nh = 0

    def mk_dibs(self):
        drop_dib(self.dc, self.bmp)
        drop_dib(self.mdc, self.mbmp)
        self.dc, self.ptr, self.bmp = new_dib(max(1, self.w), max(1, self.h))
        self.mdc, self.mptr, self.mbmp = new_dib(max(1, self.w * SS), max(1, self.h * SS))

    # ---- 位置
    def rect(self):
        """窗口的**可见**外框（DWM extended frame bounds），拿不到再退回 GetWindowRect。

        普通窗口两者一致；最大化 / 全屏时 GetWindowRect 会比屏幕每边多出约
        11 px 的隐形 resize 边框（实测 (-11,-11,2571,1379)，可见区其实是
        (0,0,2560,1368)）。拿它当右边界，右段文字就会被推到屏幕上只剩 2 px，
        这就是"全屏时贴边"的真正原因 —— 换成可见外框，两种状态留白一致。
        """
        fb = wt.RECT()
        if dwmapi.DwmGetWindowAttribute(self.owner, DWMWA_EXTENDED_FRAME_BOUNDS,
                                        ctypes.byref(fb), ctypes.sizeof(fb)) == 0 \
                and fb.right > fb.left and fb.bottom > fb.top:
            return fb
        r = wt.RECT()
        if user32.GetWindowRect(self.owner, ctypes.byref(r)):
            return r
        return None

    def win_size(self):
        """窗口尺寸（极便宜）：用来 50 ms 级感知切全屏 / 拉窗口"""
        r = self.rect()
        if not r:
            return None
        return (r.right - r.left, r.bottom - r.top)

    # ---- 归属判断：抓到的像素必须是我们自己的界面
    def owns(self, x, y):
        """这个屏幕点是不是 WorkBuddy 自己的像素（被别的窗口盖住时为 False）"""
        h = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
        if not h:
            return False
        return h == self.owner or user32.GetAncestor(h, GA_ROOT) == self.owner

    def above_rects(self):
        """压在我们上面的那些窗口的矩形（按 z 序只取 owner 之上的，屏幕坐标）。

        为什么不能只靠 WindowFromPoint 逐点判断：DWM 的窗口阴影画在窗口矩形
        **外面**，那块像素系统认为属于下面的窗口（也就是我们），可画面上它
        明明是别人投上来的。而阴影本身就是一条柔和的亮度渐变，正好落进"阶跃"
        判据里 —— 这就是"别的窗口一置顶，左段就跟着它跑"的根子。
        窗口矩形外扩 OCCLUDE_DIP 整段排除，才是干净的做法。
        """
        now = time.perf_counter()
        if self._ar is not None and now - self._ar_t < 0.35:
            return self._ar
        skip = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                "Windows.UI.Core.CoreWindow", "EdgeUiInputTopWndClass",
                "ForegroundStaging", "MultitaskingViewFrame",
                "Windows.Internal.Shell.TabProxyWindow", "TaskListThumbnailWnd",
                "XamlExplorerHostIslandWindow")
        own = wt.DWORD()
        user32.GetWindowThreadProcessId(self.owner, ctypes.byref(own))
        out = []
        cloaked = wt.DWORD()
        h = user32.GetTopWindow(None)
        n = 0
        while h and h != self.owner and n < 400:      # 沿 z 序自上而下，到我们自己为止
            n += 1
            if user32.IsWindowVisible(h) and not user32.IsIconic(h):
                c = ctypes.create_unicode_buffer(96)
                user32.GetClassNameW(h, c, 96)
                if not c.value.startswith("TPulse") and c.value not in skip:
                    pid = wt.DWORD()
                    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                    # DWM 暂时隐藏（虚拟桌面切换等）的窗口不算遮挡，否则会白冻一片
                    cloaked.value = 0
                    dwmapi.DwmGetWindowAttribute(h, DWMWA_CLOAKED,
                                                 ctypes.byref(cloaked), ctypes.sizeof(cloaked))
                    if pid.value != own.value and not cloaked.value:
                        rr = wt.RECT()
                        if user32.GetWindowRect(h, ctypes.byref(rr)) \
                                and rr.right - rr.left > sc(8) \
                                and rr.bottom - rr.top > sc(8):
                            out.append((rr.left, rr.top, rr.right, rr.bottom))
            h = user32.GetWindow(h, GW_HWNDNEXT)
        self._ar, self._ar_t = out, now
        return out

    def blocked(self, y):
        """y 这一行上"不归我们"的 x 区间（屏幕坐标，已合并），含别人的阴影余量"""
        m = sc(OCCLUDE_DIP)
        spans = [(l - m, r_ + m) for l, t, r_, b in self.above_rects()
                 if t - m <= y <= b + m]
        if not spans:
            return ()
        spans.sort()
        out = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= out[-1][1] + 1:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return out

    def free_spans(self, y, x0, x1):
        """[x0, x1) 里还归我们的连续段（屏幕坐标）；太窄的段直接丢掉"""
        out = []
        cur = x0
        for a, b in self.blocked(y):
            if b < cur or a >= x1:
                continue
            if a > cur:
                out.append((cur, a))
            cur = max(cur, b + 1)
            if cur >= x1:
                break
        if cur < x1:
            out.append((cur, x1))
        return [(a, min(b, x1)) for a, b in out if min(b, x1) - a > sc(24)]

    def owns_band(self, x0, y0, w, h):
        """一条窄带是否整块都属于我们（抽查 5×3 个点，别的地方盖住就放弃这一帧）"""
        xs = (x0 + 2, x0 + w // 3, x0 + w // 2, x0 + 2 * w // 3, x0 + w - 3)
        ys = (y0 + 2, y0 + h // 2, y0 + h - 3)
        return all(self.owns(x, y) for y in ys for x in xs)

    # ---- 侧边栏右缘：用户拖动就会变，所以要现读
    def side_left(self):
        """左段起点 DIP，带"可信闸门"（见下）；拿不到返回 None。

        侧边栏宽度只有两种正当变化：用户正拖着那条边，或窗口尺寸刚变。
        其余时候读数一变就是有东西糊在侧边栏上（最典型：点左下角头像弹出的
        账号面板比侧边栏宽 15 DIP，两条横带都能测到它那条硬边），这时必须
        沿用旧值，不能跟着弹层平移。
        """
        raw = self.measure_side_left()
        if raw is None:
            return None
        if abs(raw - self.left_dip) <= 0.8:
            self.edge_relearn = False
            self.edge_locked = True
            return raw
        if not self.edge_locked or self.edge_relearn or self._dragging_edge(raw):
            self.edge_locked = True
            self.edge_relearn = False
            return raw
        return self.left_dip

    def _dragging_edge(self, raw):
        """鼠标按着左键、且指针压在候选边界上 → 用户正在拖侧边栏。"""
        if not lbtn()[0]:
            return False
        r = self.rect()
        if not r:
            return False
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return abs(pt.x - (r.left + raw * S)) <= sc(14)

    def measure_side_left(self):
        """返回左段起点 DIP（= 侧边栏右缘 + SIDE_GAP_DIP），拿不到返回 None。

        侧边栏能左右拖动改宽度，所以固定值必然对不上。判据不看颜色方向
        （侧边栏有时浅灰、有时纯白，内容区还可能被遮罩压暗），只看"竖直
        贯穿的亮度阶跃"：取相距 300 DIP 的两条横带，各自找阶跃列，两边都
        命中同一个 x 才算数——侧边栏分界是贯穿整个高度的，正文里的气泡、
        图片、文字都只占局部，命中不了第二次。
        """
        r = self.rect()
        if not r:
            return None
        cw = r.right - r.left
        maxw = min(cw, sc(SIDE_MAX_DIP))
        lo = sc(SIDE_MIN_DIP)
        if maxw < lo + 40:
            return None
        if self.pdc is None or self.pw != maxw:
            drop_dib(self.pdc, self.pbmp)
            self.pdc, self.pptr, self.pbmp = new_dib(maxw, 1)
            self.pw = maxw
        k = sc(4)                      # 阶跃判据的采样间距（DIP 的 4 倍像素）
        rows = []                      # [(y, 这一行上所有亮度阶跃的位置)]
        for ydip in SIDE_YS_DIP:
            y = r.bottom - sc(ydip)
            if y <= r.top + 6:
                continue
            # 这一行还归我们的那几段：被别的窗口（含它的阴影）压住的部分整段不看
            spans = self.free_spans(y, r.left + lo, r.left + maxw)
            if not spans:
                continue
            screen = user32.GetDC(None)
            ok = gdi32.BitBlt(self.pdc, 0, 0, maxw, 1, screen, r.left, y, SRCCOPY)
            user32.ReleaseDC(None, screen)
            if not ok:
                continue
            buf = (ctypes.c_ubyte * (maxw * 4)).from_address(self.pptr.value)
            # 只取绿通道：UI 底色都是中性色，绿通道占亮度 59%，够判阶跃，
            # 而且切片是 C 速度——整条 1000 多像素算下来不到 0.5 ms
            g = bytes(buf[1::4])
            cand = []
            for a, b in spans:
                a -= r.left
                b -= r.left
                for x in range(max(lo, a) + k, min(maxw, b) - k):
                    if abs(g[x + k] - g[x - k]) >= 8:
                        cand.append(x)
            rows.append((y, cand, g))
        if len(rows) < 2:
            return None
        tol = sc(3)
        agree = [x for x in rows[0][1]
                 if all(any(abs(x - y2) <= tol for y2 in cand) for _, cand, _ in rows[1:])]
        # 逐条验明正身：判阶跃用的那两点（x±k）都必须是我们自己的界面。
        # 上面已按窗口矩形排除，这里再按像素归属兜一道（同一进程的子窗口、洞等）
        good = [x for x in agree
                if all(self.owns(r.left + max(1, x - k), y)
                       and self.owns(r.left + x + k, y) for y, _, _ in rows)]
        if not good:
            return None
        # 再筛一道"硬边"：侧边栏分界是一个像素之内的硬跳变，而别的窗口的阴影、
        # 遮罩、渐变都是软坡（每像素只差 1~3 灰阶）。软坡在这一关全部出局。
        sharp = []
        for x in good:
            vals = []
            for _, _, gg in rows:
                vals.append(max(abs(gg[i + 1] - gg[i])
                                for i in range(max(0, x - k), min(len(gg) - 1, x + k))))
            if min(vals) >= EDGE_SHARP:
                sharp.append(x)
        if not sharp:
            return None
        edge = min(sharp) / S
        if not (SIDE_MIN_DIP <= edge <= SIDE_MAX_DIP):
            return None
        return edge + SIDE_GAP_DIP

    # ---- 提示文案的竖直位置：窗口模式一变（中等/最大化/全屏）它就换高度
    def notice_center(self):
        """返回「内容由 AI 生成，请核实重要信息」墨迹中心距窗口底的距离（像素）。

        在内容区中心取一条 110 DIP 宽、44 DIP 高的窄带，逐行数暗像素：
        连续 5 DIP 以上有墨的段才算文案，低于这个高度的一律当边框线丢掉，
        取最靠下的有效段，返回它的上下缘中点。拿不到返回 None。
        """
        r = self.rect()
        if not r:
            return None
        cw = r.right - r.left
        left = sc(self.left_dip)
        if cw - left < sc(240):
            return None
        cx = r.left + left + (cw - left - sc(SB_DIP)) // 2
        bw = sc(NOTICE_W_DIP)
        bh = min(sc(NOTICE_H_DIP), r.bottom - r.top)
        x0 = cx - bw // 2
        y0 = r.bottom - bh
        if x0 < r.left or x0 + bw > r.right or bh < sc(20):
            return None
        # 这条窄带被别人盖住时，截到的不是我们自己的界面，这一帧直接放弃（沿用上次的值）
        if not self.owns_band(x0, y0, bw, bh):
            return None
        if self.ndc is None or self.nw != bw or self.nh != bh:
            drop_dib(self.ndc, self.nbmp)
            self.ndc, self.nptr, self.nbmp = new_dib(bw, bh)
            self.nw, self.nh = bw, bh
        screen = user32.GetDC(None)
        ok = gdi32.BitBlt(self.ndc, 0, 0, bw, bh, screen, x0, y0, SRCCOPY)
        user32.ReleaseDC(None, screen)
        if not ok:
            return None
        buf = (ctypes.c_ubyte * (bw * bh * 4)).from_address(self.nptr.value)
        g = bytes(buf[1::4])          # 绿通道平面，切片是 C 速度
        dk = bytes(1 if i < 210 else 0 for i in range(256))   # 暗像素映射表
        ink = []
        for y in range(bh):
            seg = g[y * bw:(y + 1) * bw]
            ink.append(sum(seg[0::2].translate(dk)))
        minh = max(3, sc(5))
        bands = []
        y = 0
        while y < bh:
            if ink[y] >= 2:
                s = y
                while y < bh and ink[y] >= 2:
                    y += 1
                if y - s >= minh:
                    bands.append((s, y - 1))
            else:
                y += 1
        # 贴窗口底边的那几行是边框/阴影，不是文案；高过 20 DIP 的段也不是一行字
        bands = [b for b in bands
                 if b[1] < bh - max(2, sc(3)) and b[1] - b[0] <= sc(20)]
        if not bands:
            return None
        top, bot = bands[-1]
        return r.bottom - (y0 + (top + bot) // 2)

    def update_geo(self, force=False):
        """现读侧边栏右缘、是否全屏、提示文案的竖直位置，返回有没有变化。

        侧边栏能被拖动改宽，窗口模式（中等/最大化/全屏）会改底部输入区的高度，
        两件事都得实时跟，写死任何一个数都会跑位。
        force=True：窗口尺寸刚变过，不管读数有没有差都重排一次（位置立刻归位）。
        """
        r = self.rect()
        if not r:
            return False
        size = (r.right - r.left, r.bottom - r.top)
        if self.last_size is not None and size != self.last_size:
            self.edge_relearn = True       # 切全屏/最大化/拉窗口：允许重学一次
        changed = False
        edge = None if self.unknown else self.side_left()
        if edge is not None and abs(edge - self.left_dip) > 0.8:
            self.left_dip = edge
            changed = True

        now = time.time()
        # 尺寸刚变（切全屏/最大化/拉窗口）后 0.6 秒内连着重测，尽快收敛到最终位置
        hot = time.perf_counter() - self.geo_hot < 0.6
        forced = self.geo_pending or size != self.last_size
        if forced or hot or now - self.nc_t > 2.0:
            self.geo_pending = False
            self.last_size = size
            self.nc_t = now
            nc = self.notice_center()
            if nc is not None:
                gb_new = (nc - self.h // 2) / float(S)
                # 「内容由 AI 生成，请核实重要信息」永远落在窗口底往上 17~25 DIP。
                # 量出来偏太多，说明这一页根本没有那句话（定时任务/资料库这类没有
                # 输入框的页），抓到的是卡片文字；相对上次突变超过 12 DIP 也一样。
                # 两种都不许当基准——否则整条会凭空上移二十几 DIP，正压在页面字上。
                if 6.0 <= gb_new <= 34.0 and (forced or hot
                                              or abs(gb_new - self.gap_b_dip) <= 12.0):
                    self.nc_off = nc
        if self.nc_off:
            gb = (self.nc_off - self.h // 2) / float(S)
            if 0.0 <= gb <= 60.0 and abs(gb - self.gap_b_dip) > 0.6:
                self.gap_b_dip = gb
                changed = True
        return changed or force

    def geo(self):
        """几何线程：拖动侧边栏、切窗口模式时，左段和基线要跟着立刻动。

        抓一次屏要 4~6 ms（比读像素贵得多），所以不盲抓：
          * 每 20 ms 读一次窗口 rect（一次 GetWindowRect，微秒级），尺寸一变
            立刻抓屏重排 —— 切全屏时 20~50 ms 内就位，不再等慢半拍的定时轮询；
          * 鼠标左键按着时按 60 ms 一次抓（跟手拖侧边栏）；
          * 松手后 0.18 秒一次热一阵，彻底静下来降到 0.9 秒一次；
          * 窗口不在前台时没必要盯这么紧，退到 0.15 秒一次，省点电。
        """
        last = 0.0
        last_size = None
        while not self.stop:
            wait = SIZE_POLL_S
            try:
                now = time.perf_counter()
                size = self.win_size()
                resized = last_size is not None and size != last_size
                last_size = size
                drag = lbtn()[0]
                since = now - self.geo_hot
                due = GEO_WARM_S if since < 1.2 else GEO_IDLE_S
                if (resized or (drag and now - last >= GEO_POLL_S)
                        or now - last >= due) and self.vis \
                        and user32.IsWindow(self.owner):
                    last = now
                    with DRAW_LOCK:
                        if self.update_geo(force=resized):
                            self.geo_hot = time.perf_counter()
                            wdev = self.build()
                            if wdev != self.w:
                                self.w = wdev
                                self.mk_dibs()
                            self.paint()
                            self.place()
                            self.sig = (self.text, self.text_r,
                                        self.reader.running(), wdev)
                    wait = SIZE_POLL_S
                else:
                    fg = user32.GetForegroundWindow() == self.owner
                    wait = SIZE_POLL_S if (fg or drag or since < 1.2) else SIZE_IDLE_S
            except Exception as e:
                log_err("geo", e)
            time.sleep(wait)

    def width_cap(self):
        """左右两段各自的可用宽度：都在那句居中提示文案之外，绝不重叠"""
        r = self.rect()
        if not r:
            return sc(300), sc(200), sc(520)
        cw = r.right - r.left
        left = sc(self.left_dip)
        # 右段距窗口右边：全屏没有深色边框兜着，额外留半个字符
        right = cw - sc(GAP_R_DIP)
        # 提示文案按内容区（扣掉右侧滚动条）居中
        center = left + (cw - left - sc(SB_DIP)) // 2
        gap = sc(DISP_W_DIP / 2 + CLEAR_DIP)
        cap_l = center - gap - left
        cap_r = right - (center + gap)
        return max(sc(60), cap_l), max(sc(60), cap_r), right - left

    def place(self):
        r = self.rect()
        if not r:
            return
        x = r.left + sc(self.left_dip)
        y = r.bottom - self.h - sc(self.gap_b_dip)
        move_to(self, x, y)

    def park(self):
        move_to(self, -20000, -20000)

    # ---- 路由判断：窗口当前是不是对话页
    def tr(self, msg):
        if getattr(self, "trace_on", False):
            _traw(msg)


    def check_chat(self):
        """当前窗口是不是停在某个对话上（只有这时才显示数字）。

        判据只有一条：**侧边栏里那块"选中态高亮"在「最近」上面还是下面**。
        侧边栏任何时刻恰好有一行是高亮的——停在入口页就在六个入口行之一，
        停在对话上就在「最近」列表里那个会话上。所以扫整条侧边栏、找出所有
        "整行高亮"，看位置就够了：不需要记点击、不需要区分悬停，重启后第一
        帧就能判对。

        返回 True=对话页 / False=入口页 / None=判断不了（被遮挡、侧边栏被拉没…）。
        """
        self.trace_on = _trace_exists()
        if not ONLY_CHAT:
            return True
        r = self.rect()
        if not r:
            self.tr("no rect")
            return None
        sf = float(S or 1.0)
        # 横向：侧边栏靠左那一窄条（left_dip 是"侧边栏右缘 + 7 DIP 空隙"）
        side = self.left_dip - SIDE_GAP_DIP
        x0d = SB_SCAN_X0_DIP
        x1d = min(x0d + SB_SCAN_W_DIP, side - 8.0)
        if x1d - x0d < 30:
            self.tr("strip too narrow %.1f" % (x1d - x0d))
            return None                       # 侧边栏被折叠/宽度没测出来
        y0d = SB_NAV_TOP_DIP
        y1d = min(SB_SCAN_BOT_DIP, (r.bottom - r.top) / sf - 8.0)
        if y1d - y0d < 140:
            self.tr("strip too short %.1f" % (y1d - y0d))
            return None                       # 窗口太矮，看不清就没法定
        sx, sy = r.left + sc(x0d), r.top + sc(y0d)
        sw, sh = sc(x1d - x0d), sc(y1d - y0d)
        if sx + sw > r.right or sy + sh > r.bottom:
            return None
        # 这块还得是 WorkBuddy 自己的界面：被别的窗口/对话框压住就判断不了
        for py in (sy + 4, sy + sh // 2, sy + sh - 4):
            for px in (sx + 4, sx + sw // 2, sx + sw - 4):
                h = user32.WindowFromPoint(wt.POINT(px, py))
                if not h:
                    self.tr("no window at %d,%d" % (px, py))
                    return None
                if user32.GetAncestor(h, GA_ROOT) != self.owner and h != self.owner:
                    self.tr("covered at %d,%d by %d" % (px, py, h))
                    return None
        if self.ddc is None or self.dw != sw or self.dh != sh:
            drop_dib(self.ddc, self.dbmp)
            self.ddc, self.dptr, self.dbmp = new_dib(sw, sh)
            self.dw, self.dh = sw, sh
        screen = user32.GetDC(None)
        ok = gdi32.BitBlt(self.ddc, 0, 0, sw, sh, screen, sx, sy, SRCCOPY)
        user32.ReleaseDC(None, screen)
        if not ok:
            self.tr("bitblt failed")
            return None
        buf = (ctypes.c_ubyte * (sw * sh * 4)).from_address(self.dptr.value)
        bands = sidebar_bands(buf, sw, sh)

        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cur = pt.y - sy
        split = sc(SB_NAV_SPLIT_DIP - SB_NAV_TOP_DIP)
        tol = sc(6)
        now = time.time()

        # 点在入口区（横向在侧边栏内、纵向在入口行那一段）→ 用户刚切页，立刻算入口页。
        # 页面重绘之前那一帧还测不到高亮，有这个点击就不用等。
        down, clicked = lbtn()
        if down or clicked:
            xdip = (pt.x - r.left) / sf
            ydip = (pt.y - r.top) / sf
            if x0d - 4 <= xdip <= x1d + 4 and y0d - 4 <= ydip <= SB_NAV_SPLIT_DIP:
                self.nav_click_t = now
                if self.trace_on:
                    _traw("CLICK at %.0f,%.0f" % (xdip, ydip))

        # 刚点过入口 0.5 秒内一律按入口页处理（页面重绘/切换动画期间别闪回）
        if now - self.nav_click_t < 0.5:
            if _trace_on():
                log_err("chk", "click->entry bands=%s cur=%d" % (bands, cur))
            return False

        v = nav_verdict(bands, split, cur, tol)
        # 唯一要压一压的情形：带子里只有鼠标正压着的那一块。可能是从对话页扫过
        # 入口行带出来的悬停高亮，等它稳住 SB_HOVER_HOLD_S 再认，免得闪一下。
        if v is False and only_under_cursor(bands, cur, tol):
            if self.nav_hold_t <= 0:
                self.nav_hold_t = now
            if now - self.nav_hold_t < SB_HOVER_HOLD_S:
                if _trace_on():
                    log_err("chk", "hold bands=%s cur=%d" % (bands, cur))
                return None
        else:
            self.nav_hold_t = 0.0
        if self.trace_on:
            _traw("v=%s bands=%s cur=%d split=%d rect=%d,%d %dx%d left=%.1f click=%.1f"
                  % (v, bands, cur, split, sx, sy, sw, sh, self.left_dip,
                     now - self.nav_click_t))
        return v


    # ---- 绘制
    def measure(self, text):
        old = gdi32.SelectObject(MEAS_DC, FONT_K)
        sz = wt.SIZE()
        gdi32.GetTextExtentPoint32W(MEAS_DC, text, len(text), ctypes.byref(sz))
        gdi32.SelectObject(MEAS_DC, old)
        return sz.cx

    def layout_text(self, cap_l, cap_r, vals):
        """左段按可读性降级，右段按短版降级。

        宽度硬约束：左段右缘绝不能越过中间那句提示文案，否则会把
        check_chat 的路标（那块像素）盖住，导致页面判断失真。
        """
        variants = [(False, True), (True, True), (True, False)]
        best = (compose_left(vals["n"], None, True, False), True)
        for tight, wt_ in variants:
            txt = compose_left(vals["n"], vals["total"], tight, wt_)
            best = (txt, tight)
            w = self.measure(txt) // SS + sc(PAD_L_DIP) + sc(PAD_R_DIP)
            if w <= cap_l:
                break
        txt_r = ""
        for short in (False, True):
            cand = compose_right(vals["tok"], vals["hit"], short)
            if self.measure(cand) // SS <= cap_r:
                txt_r = cand
                break
        return best, txt_r

    def paint(self):
        w, h, K = self.w, self.h, SS
        mw, mh = w * K, h * K
        ctypes.memset(self.mptr, 0, mw * mh * 4)
        hdc = self.mdc
        gdi32.SetBkMode(hdc, TRANSPARENT)

        # 左段文字（不再带圆点，直接贴左）
        cy = mh // 2
        tx = sc(PAD_L_DIP) * K
        old = gdi32.SelectObject(hdc, FONT_K)
        gdi32.SetTextColor(hdc, WHITE)
        sz = wt.SIZE()
        gdi32.GetTextExtentPoint32W(hdc, self.text, len(self.text), ctypes.byref(sz))
        gdi32.TextOutW(hdc, tx, cy - sz.cy // 2, self.text, len(self.text))
        if self.text_r:
            sz2 = wt.SIZE()
            gdi32.GetTextExtentPoint32W(hdc, self.text_r, len(self.text_r), ctypes.byref(sz2))
            gdi32.TextOutW(hdc, mw - sz2.cx - sc(PAD_R_DIP) * K, cy - sz2.cy // 2,
                           self.text_r, len(self.text_r))
        gdi32.SelectObject(hdc, old)

        # 4 倍 -> 1 倍（HALFTONE 走加权平均，无抖动噪点）
        ctypes.memset(self.ptr, 0, w * h * 4)
        gdi32.SetStretchBltMode(self.dc, HALFTONE)
        gdi32.SetBrushOrgEx(self.dc, 0, 0, None)
        gdi32.StretchBlt(self.dc, 0, 0, w, h, self.mdc, 0, 0, mw, mh, SRCCOPY)

        # 覆盖率 -> 预乘 alpha（字色为黑，RGB 保持 0）
        mv = memoryview((ctypes.c_ubyte * (w * h * 4)).from_address(self.ptr.value))
        raw = mv[0::4].tobytes()
        out = bytearray(w * h * 4)
        out[3::4] = raw.translate(INK_TABLE)

        # 无圆点：整段文字统一用 disabled 墨色
        ctypes.memmove(self.ptr, bytes(out), len(out))
        commit(self)

    def build(self):
        r = self.reader
        vals = {"n": r.n_reply, "total": r.cr_total,
                "tok": r.tok_total, "hit": r.hit_all()}
        cap_l, cap_r, full = self.width_cap()
        (txt, tight), txt_r = self.layout_text(cap_l, cap_r, vals)
        self.text = txt if txt is not None else ""
        self.text_r = txt_r
        self.tight = tight
        return max(sc(MIN_W_DIP), full)

    # ---- 每秒动作
    def refresh_sid(self):
        """跟着界面走：界面显示哪个对话，条上就是哪个对话的数字。
        多窗口时只让最前面那个窗口跟随，其它窗口保持各自绑定的会话，免得两条条显示同一个。"""
        if SESSION_LOCK:
            if self.sid != SESSION_LOCK:
                self.sid = SESSION_LOCK
                self.sig = None
                return True
            return False
        with BARS_LOCK:
            multi = len([b for b in BARS if b.hwnd]) > 1
        if multi and self.sid and user32.GetForegroundWindow() != self.owner:
            return False
        sid = ui_session()
        if not sid:
            sess = db_sessions()
            sid = sess[0]["id"] if sess else None
        if sid and sid != self.sid:
            if self.sid and ui_blank():
                # 停在"新建任务"空白页：条马上要隐藏，没必要为那个没落盘的
                # 临时会话把 reader 重建一遍
                return False
            self.sid = sid
            self.sig = None
            return True
        return False

    def session_path(self):
        if not self.sid:
            return None
        if SESSION_LOCK and self.sid == SESSION_LOCK and os.path.isfile(self.sid):
            return self.sid
        return session_log(self.sid)

    def tick(self):
        while not self.stop:
            try:
                if not user32.IsWindow(self.owner):
                    self.stop = True
                    break
                now = time.time()
                # 前台 0.15 秒看一次侧边栏：切到助理/专家/资料库/新建任务这些页面时，
                # 选中高亮一铺出来就判定；不在前台就懒得勤看。
                # **不在前台干脆不看**：别的程序压上来时 BitBlt 抓到的是别人的界面，
                # 侧边栏高亮会判错，条子会莫名其妙消失（切回来也不回来）。
                front = wb_in_front(self.owner)
                chk = 0.15 if front else 0.6
                if front and now - self.last_check >= chk:
                    self.last_check = now
                    res = self.check_chat()
                    self.unknown = res is None
                    if res is None:
                        pass                       # 看不懂就保持现状，绝不猜
                    elif res:
                        # 停在"最近"列表里的某个对话上 → 显示。
                        # 例外：界面停在还没落盘的空白新任务（persisted=false）
                        self.chat = not ui_blank()
                        self.miss = 0
                    else:
                        # 高亮在六个入口行之一（新建任务/助理/专家/资料库/发现…）
                        # 连着两帧都这么说才收（0.15 秒），躲开切换动画/局部重绘的
                        # 单帧假象；刚点过入口则立刻收，不等。
                        self.miss += 1
                        if self.miss >= 2 or now - self.nav_click_t < 0.5:
                            self.chat = False

                sid_new = self.refresh_sid()
                path = self.session_path()
                self.reader.set_path(path)
                new_data = self.reader.poll()

                # 没事就不重排重画：会话没变、日志没长、几何没动就跳过（省 CPU）。
                # 0.5 秒兜底刷一次，宽度上限随侧边栏漂移也能跟上。
                pc = time.perf_counter()
                need_build = (sid_new or new_data or self.sig is None
                              or pc - getattr(self, "last_build", 0.0) >= 0.5)

                with DRAW_LOCK:
                    if need_build:
                        self.last_build = pc
                        wdev = self.build()
                        sig = (self.text, self.text_r, self.reader.running(), wdev)
                        if sig != self.sig:
                            self.sig = sig
                            if wdev != self.w:
                                self.w = wdev
                                self.mk_dibs()
                            self.paint()
                            # 标题里带上「当前会话 + 正在显示的文字」，外部脚本据此可直接核对，
                            # 不用截图；以 TPulse 开头，原有按前缀匹配的地方都不受影响。
                            try:
                                user32.SetWindowTextW(
                                    self.hwnd, "TPulse|%s|%s|%s"
                                    % ((self.sid or "")[:8], self.text, self.text_r))
                            except Exception:
                                pass

                    # 只在 WorkBuddy 自己在前台时露脸：切到浏览器等别的程序就收起，
                    # 不把数字画到别人的界面上。（自测模式下没有真前台，直接放行）
                    want = (front or SELFTEST) and self.chat and not ui_blank() \
                        and not user32.IsIconic(self.owner) \
                        and user32.IsWindowVisible(self.owner)
                    if want and not self.vis:
                        self.vis = True
                        self.geo_pending = True
                        self.paint()
                        self.place()
                    elif not want and self.vis:
                        self.vis = False
                        self.park()
            except Exception as e:
                log_err("tick", e)
            # 空闲 0.25 秒一轮：切到别的对话时，界面一显示它 daemon.log 就有 probe，
            # 这一轮就能发现并把数字换过去（不必等发消息）。回答期间再降到 0.15 秒。
            time.sleep(0.15 if self.reader.running() else 0.25)
        self._free()


# ---------------------------------------------------------------- 窗口操作
def commit(bar):
    with POS_LOCK:
        rw = wt.RECT()
        user32.GetWindowRect(bar.hwnd, ctypes.byref(rw))
        pt_dst = wt.POINT(rw.left, rw.top)
        sz = wt.SIZE(bar.w, bar.h)
        pt_src = wt.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, OPACITY, AC_SRC_ALPHA)
        screen = user32.GetDC(None)
        user32.UpdateLayeredWindow(bar.hwnd, screen, ctypes.byref(pt_dst),
                                   ctypes.byref(sz), bar.dc, ctypes.byref(pt_src),
                                   0, ctypes.byref(blend), ULW_ALPHA)
        user32.ReleaseDC(None, screen)


def move_to(bar, x, y):
    with POS_LOCK:
        user32.SetWindowPos(bar.hwnd, None, x, y, 0, 0,
                            SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOSIZE)


# ---------------------------------------------------------------- 窗口发现
OWNER_EXE = "workbuddy.exe"     # WorkBuddy 主程序名：同标题的浏览器窗口不能算


def proc_exe_name(h):
    """窗口所属进程的可执行文件名（小写）。拿不到返回空串。"""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    if not pid.value:
        return ""
    try:
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.OpenProcess.restype = wt.HANDLE
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                       ctypes.POINTER(wt.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    except Exception:
        pass
    hp = kernel32.OpenProcess(0x1000, False, pid.value)      # QUERY_LIMITED_INFORMATION
    if not hp:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(n)):
            return buf.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(hp)


def is_main_win(h):
    """这个顶层窗口该不该挂一条状态条（只认 WorkBuddy 主窗口）。

    三条同时满足才算：类名 Chrome_WidgetWin_1、标题含 "WorkBuddy"、有最小化
    按钮（设置面板那一类辅助窗口差这一位）。

    **必须再加一条进程名校验**：Edge/Chrome 打开 open.workbuddy.cn 时，标题
    正好是"WorkBuddy 开放平台 … - Microsoft Edge"，类名同样是
    Chrome_WidgetWin_1，最小化按钮也有——不加进程名，浏览器窗口会被当成
    WorkBuddy 主窗口，多挂一条状态条，还把侧边面板画到浏览器界面上。
    """
    if not user32.IsWindowVisible(h):
        return False
    cls = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(h, cls, 128)
    if cls.value != "Chrome_WidgetWin_1":
        return False
    n = user32.GetWindowTextLengthW(h)
    if n == 0:
        return False
    b = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, b, n + 1)
    if OWNER_TITLE.strip().lower() not in b.value.strip().lower():
        return False
    exe = proc_exe_name(h)
    if exe and not SELFTEST and exe != OWNER_EXE:   # 拿不到进程名时退回原逻辑，别整块失效
        return False
    if not (user32.GetWindowLongW(h, GWL_STYLE) & WS_MINIMIZEBOX):
        return False
    r = wt.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    if r.right - r.left < 400 or r.bottom - r.top < 300:
        return False
    return True


def wb_windows():
    """所有 WorkBuddy 主窗口，按 Z 序（最上面优先）"""
    out = []

    def cb(h, l):
        if is_main_win(h):
            out.append(h)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def bind_sessions():
    """多窗口时按最近激活顺序分配会话，免得两条状态条显示同一个对话。

    单窗口不在这里管：绑哪个会话由 Bar.refresh_sid 每轮跟着界面决定（读 daemon.log
    的 history-probe），切到旧对话不发消息也会立刻跟过去。
    """
    with BARS_LOCK:
        bars = [b for b in BARS if b.hwnd]
    if not bars:
        return
    if SESSION_LOCK:
        for b in bars:
            b.sid = SESSION_LOCK
        return
    if len(bars) < 2:
        return
    sess = db_sessions()
    idx = log_index()
    sess = [s for s in sess if s["id"] in idx] or sess
    ids = set(s["id"] for s in sess)
    bars.sort(key=lambda b: -LAST_FG.get(b.owner, 0))
    used = set()
    for b in bars:
        if b.sid in ids:
            used.add(b.sid)
    for b in bars:
        if b.sid in used:
            continue
        for s in sess:
            if s["id"] not in used:
                b.sid = s["id"]
                used.add(s["id"])
                b.sig = None
                break
        else:
            b.sid = None


PM_REMOVE = 0x0001


def pump_msgs():
    """抽干**当前线程**的消息队列。

    侧边栏「今日」面板的窗口由 today 线程创建，而窗口消息只由创建它的线程泵。
    Windows 判"窗口所属线程不取消息"就等于"程序未响应"，DWM 会给它盖一块
    **黑色幽灵窗口**（`Ghost` 类，挂在 dwm.exe 名下，标题还是 TPulseToday）——
    它压在真窗口上、点不掉，重启 WorkBuddy 也没用，因为幽灵跟着 python 那条
    窗口活，跟 WorkBuddy 无关。

    状态条本体的窗口建在主线程、由 main() 里的 GetMessage 泵驱动，所以从来没
    出过这个毛病。规矩定死：**谁建的窗口，谁负责抽消息。**
    """
    msg = wt.MSG()
    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def manager_step():
    """主线程定时器回调：建/拆状态条，更新会话绑定"""
    if QUIT:
        return
    wins = wb_windows()
    fg = user32.GetForegroundWindow()
    now = time.time()
    if fg in wins:
        LAST_FG[fg] = now

    with BARS_LOCK:
        alive, dead = [], []
        for b in BARS:
            if not user32.IsWindow(b.owner):
                dead.append(b)                       # 窗口已经关了
            elif user32.IsWindowVisible(b.owner) and not is_main_win(b.owner):
                # 这一条挂在了辅助窗口上（设置面板等）。设置面板与主窗口同进程、
                # 同类名、同标题，只靠 style 区分，这里补一刀防止多挂一条。
                dead.append(b)
            else:
                alive.append(b)
        owners = set(b.owner for b in alive)
        BARS[:] = alive
    for b in dead:
        b.close()
    for h in wins:
        if h not in owners:
            try:
                bar = Bar(h)
            except Exception:
                continue
            with BARS_LOCK:
                BARS.append(bar)
            threading.Thread(target=bar.tick, daemon=True).start()
            threading.Thread(target=bar.geo, daemon=True).start()
    bind_sessions()


# ---------------------------------------------------------------- 跟随
def follow_loop():
    last = {}
    hot_s = FOLLOW_MS / 1000.0
    idle_s = 0.012
    hot_until = 0.0
    while not QUIT:
        moved = False
        with BARS_LOCK:
            bars = list(BARS)
        for b in bars:
            try:
                if not (b.vis and user32.IsWindow(b.owner) and not user32.IsIconic(b.owner)
                        and user32.IsWindowVisible(b.owner)):
                    last.pop(b.hwnd, None)
                    continue
                r = b.rect()
                if not r:
                    continue
                pos = (r.left + sc(b.left_dip), r.bottom - b.h - sc(b.gap_b_dip))
                if last.get(b.hwnd) != pos:
                    moved = last.get(b.hwnd) is not None
                    last[b.hwnd] = pos
                    move_to(b, pos[0], pos[1])
            except Exception:
                last.pop(getattr(b, "hwnd", None), None)
        if moved:
            hot_until = time.perf_counter() + 0.6
        time.sleep(hot_s if time.perf_counter() < hot_until else idle_s)


# ================================================================ 今日面板
# 侧边栏底部（账号行上方）的一块浮字，显示「今日全部会话」的三项：
#   对话N轮 / 消耗X.XX分 / TokenX.XXM
# 口径与 probe.py 完全一致，只是改成增量扫、跨天自动清零。
# 版式固定一套：三列、标签在上、数字在下。不提供版式切换。
TODAY_W_DIP = 256.0          # 默认内容宽：侧边栏 280 - 左右各 12（实际宽度跟侧边栏实时变）
TODAY_MIN_W_DIP = 150.0      # 再窄三列就要互相压字：到这里就不再缩，宁可贴边
TODAY_MAX_W_DIP = 460.0      # 上限只是防异常读数，正常侧边栏到不了
TODAY_H_DIP = 62.0           # 面板高：顶部留白 + 标签 + 行距 + 数字 + 底部距灰线
TODAY_PAD_B_DIP = 10.0       # 数字距面板底（= 距下方那条灰线），不能贴死
TODAY_GAP_B_DIP = 57.0       # 兜底值：账号行上方灰线距窗口底。正式值由 find_account_sep() 动态测
TODAY_GAP_L_DIP = 12.0
TODAY_POLL_S = 5.0           # 今日汇总 5 秒一轮（扫全部会话，别和条子同频）

# 数字/单位同一档深灰，标签一档浅灰。深灰走单独一张表（满墨 0.90 ≈ #181818），
# 比状态条的 0.70（#4C4C4C）更深；标签落回 #545454 一带。
TODAY_V_DIP = 18.0           # 数字字号
TODAY_U_DIP = 11.0           # 单位字号（轮 / 分 / M）
TODAY_L_DIP = 13.0           # 标签字号（今日对话 / 今日消耗 / 今日Token）
TODAY_GAP_VU_DIP = 3.0       # 数字与拉丁单位（M / K）之间的间距
TODAY_GAP_VU_CJK_DIP = 2.5   # 数字与中文单位（轮 / 分）之间的间距：汉字字面自带左侧留白，
                             # 3.0 看着比 M 松；2.5 实测墨迹间隙与 M 完全一致（mass 口径均 6px），别再动
TODAY_GAP_VL_DIP = 6.0       # 标签与数字之间的行距
TODAY_INK_NUM = 255          # 数字/单位墨量（配合 PANEL_TABLE 满墨 = 深灰）
TODAY_INK_LBL = 180          # 标签墨量（比数字浅一档）
TODAY_INK_ALPHA = 0.90       # 面板深灰的满墨透明度

# 灰线（账号行分隔线）检测：只在合理区间里认，且要连续两次接近才采纳。
# 全屏/中等屏切换的动画帧里会冒出各种横条，不设防就会把面板钉在账号行上。
TODAY_SEP_MIN_DIP = 40.0
TODAY_SEP_MAX_DIP = 80.0
TODAY_SEP_SAMPLE_S = 0.05    # rect 刚变完：高频取两次，0.1 秒内定案
TODAY_SEP_IDLE_S = 2.0       # 静止后：2 秒复核一次（原逻辑）
TODAY_SEP_UNSTABLE_S = 1.2   # rect 变化后这么久内都按"动画期"处理
TODAY_OBSC_FREEZE_S = 0.6    # rect 变化后冻结遮挡判定，别把动画中的列表行当成弹层

# 遮挡检测：扫整块面板区域（外加顶上一条保护带），把自己的字从 alpha 里挖掉。
OBSC_GUARD_DIP = 8.0
OBSC_BANDS_DIP = ((-8.0, 6.0), (0.0, 3.0), (10.0, 3.0), (20.0, 3.0),
                  (30.0, 3.0), (40.0, 3.0), (50.0, 3.0), (54.0, 3.0))
OBSC_DEV = 7                 # 与侧边栏灰阶差同档：白弹层 255 / 底色 247 也认得出
OBSC_RATIO = 0.015           # 非底色像素超过 1.5% 就算被占
OBSC_CLEAR_FRAMES = 3        # 连续 3 帧（约 0.09 秒）干净就恢复，收起后不拖泥带水

_FONTS = {}
_TF = {}                     # path -> {"off","reqs","killed","u2r","pend"}
_TDAY = [0.0]
_TLEDGER = {}                # sessionId -> [n, credit, tok, pin, hit]，只存当天
_LDAY = [""]                 # 账本对应的日期
_LWRITE = [0.0]              # 上次落盘时间（节流用）
_TLOCK = threading.Lock()     # 只护 _TVALS 这一小撮结果，**不许**拿它裹住扫盘
_SLOCK = threading.Lock()     # 扫盘串行（只允许一个扫描线程）
_TVALS = {"n": 0, "credit": 0.0, "tok": 0}
_TREADY = [False]
PANEL = [None]


def font_dip(size_dip):
    key = int(round(size_dip * 10))
    f = _FONTS.get(key)
    if f is None:
        f = gdi32.CreateFontW(-sc(size_dip * SS), 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 4, 0,
                              "Microsoft YaHei UI")
        _FONTS[key] = f
    return f


def _today_paths(t0):
    """今天动过的会话 jsonl + 各自的子代理日志（只取 mtime 够新的，省 IO）

    目录是两层：`<项目>/<会话ID>.jsonl` 与 `<项目>/<会话ID>/subagents/agent-*.jsonl`。
    会话目录里还躺着 tool-results/，所以只往 subagents、agents 这两个名字里走，
    别的一律不下钻——子代理的花费只写在它自己的文件里，主会话文件里一条都没有，
    漏掉这一层，今日消耗就会比实际少掉整笔子代理开销。
    """
    out = []
    cutoff = t0 - 120.0
    SUB = ("subagents", "agents")

    def walk(d, depth, is_sub):
        try:
            for e in os.scandir(d):
                try:
                    if e.is_dir():
                        if depth == 0:                       # 项目目录
                            walk(e.path, 1, False)
                        elif depth == 1:
                            if e.name in SUB:                # 项目下直接挂着子代理目录
                                walk(e.path, 3, True)
                            else:                            # 会话目录
                                walk(e.path, 2, False)
                        elif depth == 2 and e.name in SUB:   # 会话 → subagents
                            walk(e.path, 3, True)
                        continue
                    if e.name.endswith(".jsonl") and e.stat().st_mtime >= cutoff:
                        out.append((e.path, is_sub))
                except OSError:
                    pass
        except OSError:
            pass

    walk(LOG_ROOT, 0, False)
    return out


def _sid_of(path):
    """文件 → 所属会话 ID；子代理文件取它上两层的会话目录名"""
    if (os.sep + "subagents" + os.sep) in path or (os.sep + "agents" + os.sep) in path:
        return os.path.basename(os.path.dirname(os.path.dirname(path)))
    b = os.path.basename(path)
    return b[:-6] if b.endswith(".jsonl") else b


def _ledger_file(day):
    return os.path.join(TMP_DIR, "spend-%s.json" % day)


def _ledger_prune(_today):
    """只留最近 7 天的账本（按修改时间判断），其余删掉；每天首次跑时做一次"""
    cutoff = time.time() - 7 * 86400
    try:
        for e in os.scandir(TMP_DIR):
            if not e.name.startswith("spend-"):
                continue
            try:
                if e.stat().st_mtime < cutoff:
                    os.remove(e.path)
            except OSError:
                pass
    except Exception:
        pass


def _ledger_merge(per, t0):
    """把这一轮的会话小计并进今日账本，并按需落盘

    账本的意义：会话在界面上被删掉后，日志文件跟着消失，但那笔钱是真花掉的，
    今日合计不该因此回退。所以按会话取逐项最大值——当天只会累加，不会减少。
    """
    day = time.strftime("%Y-%m-%d", time.localtime(t0))
    if day != _LDAY[0]:
        _LDAY[0] = day
        _TLEDGER.clear()
        try:
            with open(_ledger_file(day), "r", encoding="utf-8") as f:
                saved = json.load(f).get("s") or {}
            for k, v in saved.items():
                if isinstance(v, list) and len(v) == 5:
                    _TLEDGER[k] = [int(v[0]), float(v[1]), int(v[2]), int(v[3]), int(v[4])]
        except Exception:
            pass
        _ledger_prune(day)
    for sid, cur in per.items():
        old = _TLEDGER.get(sid)
        _TLEDGER[sid] = cur if old is None else [max(old[i], cur[i]) for i in range(5)]
    if time.time() - _LWRITE[0] < 8.0:
        return
    _LWRITE[0] = time.time()
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        p = _ledger_file(day)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"day": day, "s": _TLEDGER}, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)
    except Exception:
        pass


def _fh_req(reqs, rid):
    e = reqs.get(rid)
    if e is None:
        e = {"ok": False, "done": 0.0, "sub": True,
             "cr": 0.0, "tok": 0, "pin": 0, "hit": 0}
        reqs[rid] = e
    return e


def _scan_today_file(path, t0, end):
    """增量吃一个文件的尾巴；请求级状态留在 _TF 里，钱只累加今天那部分"""
    st = _TF.get(path)
    if st is None:
        st = {"off": 0, "reqs": {}, "killed": set(), "u2r": {}, "pend": None}
        _TF[path] = st
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < st["off"]:                 # 被重写/压缩：退回全量
        st["off"] = 0
        st["reqs"].clear()
        st["killed"].clear()
        st["u2r"].clear()
        st["pend"] = None
    if size == st["off"]:
        return
    try:
        with open(path, "rb") as f:
            f.seek(st["off"])
            chunk = f.read()
    except OSError:
        return
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return
    st["off"] += cut + 1

    for raw in chunk[:cut].split(b"\n"):
        if not raw.strip():
            continue
        try:
            o = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            continue
        typ = o.get("type")
        pd = o.get("providerData")
        if not isinstance(pd, dict):
            pd = {}

        if typ == "resend-fork-notice":
            eid = o.get("editedUserItemId")
            rid = st["u2r"].get(eid) if isinstance(eid, str) else None
            if rid:
                st["killed"].add(rid)
            continue

        if typ == "message" and o.get("role") == "user":
            uid = o.get("id")
            if isinstance(uid, str):
                st["pend"] = uid
            continue

        rid = pd.get("conversationRequestId")
        if not isinstance(rid, str) or not rid:
            continue
        if st["pend"]:
            st["u2r"][st["pend"]] = rid
            st["pend"] = None

        ts = parse_ts(o.get("timestamp"))
        if ts is None:
            ts = 0.0
        e = _fh_req(st["reqs"], rid)
        if not pd.get("isSubAgent"):
            e["sub"] = False

        ru = pd.get("rawUsage")
        if isinstance(ru, dict) and t0 <= ts < end:
            c = ru.get("credit")
            if isinstance(c, (int, float)) and not isinstance(c, bool):
                e["cr"] += float(c)
            for key, name in (("total_tokens", "tok"), ("prompt_tokens", "pin"),
                              ("prompt_cache_hit_tokens", "hit")):
                v = ru.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    e[name] += int(v)

        e["ok"] = (typ == "message" and o.get("role") == "assistant"
                   and isinstance(pd.get("usage"), dict) and not pd.get("error"))
        e["done"] = ts


def poll_today():
    """扫一轮今日汇总；跨天把"今天那部分"清零，请求状态保留（完成时间自己会过滤）

    扫描是重活（大会话冷启动第一轮要读几十 MB），所以扫描用 _SLOCK 串行，
    **只在最后写结果时**才碰 _TLOCK。整段裹在 _TLOCK 里的话，today_loop 每 tick
    都要 today_vals()，一压锁就是好几秒不响应——窗口被判"未响应"、DWM 盖黑框，
    就是这么来的。
    """
    t0, end = period_bounds()
    with _SLOCK:
        if t0 != _TDAY[0]:
            _TDAY[0] = t0
            for st in _TF.values():
                for e in st["reqs"].values():
                    e["cr"] = 0.0
                    e["tok"] = e["pin"] = e["hit"] = 0
                    if e["done"] < t0:
                        e["ok"] = False
        for path, _sub in _today_paths(t0):
            try:
                _scan_today_file(path, t0, end)
            except Exception:
                pass

        # 先按会话归拢，再并进今日账本：会话被删掉后账本里那一笔仍然留着
        per = {}
        for path, st in _TF.items():
            sid = _sid_of(path)
            acc = per.get(sid)
            if acc is None:
                acc = per[sid] = [0, 0.0, 0, 0, 0]      # 轮, 分, token, prompt, hit
            is_sub_file = (os.sep + "subagents" + os.sep) in path
            for rid, e in st["reqs"].items():
                acc[1] += e["cr"]
                acc[2] += e["tok"]
                acc[3] += e["pin"]
                acc[4] += e["hit"]
                if is_sub_file or rid in st["killed"]:
                    continue
                if not e["ok"] or e["sub"] or e["done"] < t0:
                    continue
                acc[0] += 1
        _ledger_merge(per, t0)

        n = 0
        cr = 0.0
        tk = pin = hit = 0
        for n_, cr_, tk_, pin_, hit_ in _TLEDGER.values():
            n += n_
            cr += cr_
            tk += tk_
            pin += pin_
            hit += hit_
        with _TLOCK:
            _TVALS["n"] = n
            _TVALS["credit"] = cr
            _TVALS["tok"] = tk
            _TVALS["pin"] = pin
            _TVALS["hit"] = hit
            _TREADY[0] = True


def today_vals():
    with _TLOCK:
        return dict(_TVALS)


class TodayPanel:
    """侧边栏底部那块浮字。只画字，不画底（与底部状态条同一套墨色管线）。"""

    def __init__(self):
        self.hwnd = 0
        self.w = sc(TODAY_W_DIP)
        self.h = sc(TODAY_H_DIP)
        self.dc = self.ptr = self.bmp = None
        self.mdc = self.mptr = self.mbmp = None
        self.sig = None
        self.shown = False
        self.painted = False
        self.pos = None
        self._z_t = 0.0          # 上次把窗口提到最顶层的时刻（定期重提，别被别的程序压住）
        self.owner = 0
        self._create()
        self.mk_dibs()

    def _create(self):
        hinst = kernel32.GetModuleHandleW(None)
        ex = WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED
        self.hwnd = user32.CreateWindowExW(ex, PANEL_CLASS, "TPulseToday", WS_POPUP,
                                           -20000, -20000, self.w, self.h,
                                           None, None, hinst, None)
        if self.hwnd:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def mk_dibs(self):
        drop_dib(self.dc, self.bmp)
        drop_dib(self.mdc, self.mbmp)
        self.dc, self.ptr, self.bmp = new_dib(max(1, self.w), max(1, self.h))
        self.mdc, self.mptr, self.mbmp = new_dib(max(1, self.w * SS), max(1, self.h * SS))

    def resize(self, w, h=None):
        """宽度/高度变了就重建位图并重画：三列锚点按新宽度重新分配。

        侧边栏拖动时这里会被持续调用（宽度每变 1.5 px 一次），只做必要的事：
        重建 DIB、清掉旧签名逼下一轮重绘，位置交给 place 重算。
        """
        w = max(1, int(w))
        h = max(1, int(h)) if h else self.h
        if w == self.w and h == self.h:
            return
        self.w, self.h = w, h
        self.mk_dibs()
        self.painted = False
        self.sig = None
        self.pos = None

    # ---- 排版
    def _extent(self, text, size):
        """量一段字在 1 倍像素下的 (宽, 高)——高用来做垂直排版"""
        old = gdi32.SelectObject(MEAS_DC, font_dip(size))
        sz = wt.SIZE()
        gdi32.GetTextExtentPoint32W(MEAS_DC, text, len(text), ctypes.byref(sz))
        gdi32.SelectObject(MEAS_DC, old)
        return sz.cx / float(SS), sz.cy / float(SS)

    def runs(self, vals):
        """返回 [(文字, 字号DIP, x像素, y像素, 对齐, 墨量)]

        坐标一律用「面板像素」（= DIP × 屏幕缩放），字号才用 DIP —— 与状态条
        的绘制管线一致：先按 SS 倍画进大 DIB，再缩回 1 倍。

        版式固定：三列，标签在上、数字在下。每列的标签与数字共用一个左起点
        （「今」对准数字首位），整组再按 左/中/右 锚定（右缘留 1 DIP 抗锯齿余量）。
        数字与单位同档深灰、标签浅灰一档。
        """
        n = str(vals.get("n") or 0)
        cr = "%.2f" % (vals.get("credit") or 0.0)
        tk, tu = split_k(vals.get("tok") or 0)
        W = self.w
        out = []

        _, vh = self._extent("0", TODAY_V_DIP)
        _, lh = self._extent("今", TODAY_L_DIP)
        bottom = self.h - sc(TODAY_PAD_B_DIP)
        vy = bottom - vh
        ly = vy - sc(TODAY_GAP_VL_DIP) - lh

        cols = ((n, "轮", "今日对话"), (cr, "分", "今日消耗"), (tk, tu, "今日Token"))
        anchors = today_anchors(W)
        modes = (0, 1, 2)
        for i, (v, u, k) in enumerate(cols):
            # 标签与数字共用一个左起点（「今」对准数字首位），整组再按 左/中/右 锚定。
            wv, _ = self._extent(v, TODAY_V_DIP)
            wu = uh = 0.0
            if u:
                wu, uh = self._extent(u, TODAY_U_DIP)
            # 中文单位自带左侧字肩，缩进 1 DIP，视觉留白才和拉丁单位 M 一致
            gap = TODAY_GAP_VU_CJK_DIP if u in ("轮", "分") else TODAY_GAP_VU_DIP
            nw = wv + sc(gap) + wu
            lw, _ = self._extent(k, TODAY_L_DIP)
            gw = max(nw, lw)
            x0 = align_x(modes[i], anchors[i], gw)
            out.append((v, TODAY_V_DIP, x0, vy, 0, TODAY_INK_NUM))
            if u:
                out.append((u, TODAY_U_DIP, x0 + wv + sc(gap),
                            vy + vh - uh, 0, TODAY_INK_NUM))
            out.append((k, TODAY_L_DIP, x0, ly, 0, TODAY_INK_LBL))
        return out

    def paint(self, vals):
        w, h, K = self.w, self.h, SS
        mw, mh = w * K, h * K
        ctypes.memset(self.mptr, 0, mw * mh * 4)
        hdc = self.mdc
        gdi32.SetBkMode(hdc, TRANSPARENT)
        for text, size, x, y, align, ink in self.runs(vals):
            if not text:
                continue
            f = font_dip(size)
            old = gdi32.SelectObject(hdc, f)
            sz = wt.SIZE()
            gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz))
            px = int(round(x * K))
            if align == 1:
                px -= sz.cx // 2
            elif align == 2:
                px -= sz.cx
            gdi32.SetTextColor(hdc, rc(ink, ink, ink))
            gdi32.TextOutW(hdc, px, int(round(y * K)), text, len(text))
            gdi32.SelectObject(hdc, old)

        ctypes.memset(self.ptr, 0, w * h * 4)
        gdi32.SetStretchBltMode(self.dc, HALFTONE)
        gdi32.SetBrushOrgEx(self.dc, 0, 0, None)
        gdi32.StretchBlt(self.dc, 0, 0, w, h, self.mdc, 0, 0, mw, mh, SRCCOPY)

        mv = memoryview((ctypes.c_ubyte * (w * h * 4)).from_address(self.ptr.value))
        raw = mv[0::4].tobytes()
        out = bytearray(w * h * 4)
        out[3::4] = raw.translate(PANEL_TABLE)
        ctypes.memmove(self.ptr, bytes(out), len(out))
        commit(self)
        self.painted = True        # 有内容了才允许露脸

    # ---- 显隐与位置
    def park(self):
        if self.shown:
            user32.SetWindowPos(self.hwnd, None, -20000, -20000, 0, 0,
                                SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOSIZE)
            self.shown = False

    def place(self, owner, r):
        """贴着侧边栏底部放好，并保证在 WorkBuddy 窗口之前不被自己的窗口挡住。

        调用方只在 WorkBuddy 处在前台时才调它：面板是"贴在 WorkBuddy 界面上"
        的字，不能压到浏览器等别的程序上。z 序用 HWND_TOP 并且定期重提——这个
        窗口是 WorkBuddy 的附属窗口，一旦被别的窗口压下去，位置没变就不会再调
        SetWindowPos，于是切回来也看不见（历史现象：切走再切回，左下面板一直
        不出来）。
        """
        if self.owner != owner:
            self.owner = owner
            user32.SetWindowLongPtrW(self.hwnd, GWLP_HWNDPARENT, ctypes.c_void_p(owner))
        x = r.left + sc(TODAY_GAP_L_DIP)
        y = r.bottom - sc(_SEP["dip"]) - self.h
        now = time.time()
        need_z = now - self._z_t >= 0.25
        if not self.shown or self.pos != (x, y) or need_z:
            if not self.shown and not self.painted:
                return             # 还没画出内容就露脸 = 露一整块黑
            user32.SetWindowPos(self.hwnd, wt.HWND(0), x, y, 0, 0,
                                SWP_NOACTIVATE | SWP_NOSIZE)
            self.pos = (x, y)
            self._z_t = now
            if not self.shown:
                user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
                self.shown = True


def today_poll_loop():
    """数据线程：只算数，一根手指都不碰窗口。

    扫盘与跟手必须分线：两者挤在同一个循环里，扫盘一慢（尤其冷启动第一轮要读
    几十 MB）整块面板就僵住——面板窗口因此被判"未响应"、被 DWM 盖黑框。重活
    切出去，窗口线程只做窗口的事。
    """
    while not QUIT:
        try:
            poll_today()
        except Exception as e:
            log_err("today-poll", e)
        time.sleep(TODAY_POLL_S)


_WINS = [0.0, []]


def wb_windows_cached(ttl=0.3):
    """wb_windows() 每轮都枚举一遍太浪费，30ms 级的跟手循环用 0.3 秒缓存够了"""
    now = time.time()
    if now - _WINS[0] > ttl:
        _WINS[0] = now
        _WINS[1] = wb_windows()
    return _WINS[1]


def wb_in_front(owner):
    """前台窗口是不是 WorkBuddy 自己的（主窗口或同进程的辅助窗口）。

    所有"看屏幕"的判定（侧边栏高亮、账号灰线、面板遮挡）都只能信 WorkBuddy
    在前台时的画面：别的程序压上来时，BitBlt 抓到的是别人的界面像素，据此
    判断会得出完全错误的位置和结论。这些判定必须先问一句"现在能看吗"。
    """
    fg = user32.GetForegroundWindow()
    if not fg or not owner:
        return False
    a, b = wt.DWORD(), wt.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(a))
    user32.GetWindowThreadProcessId(owner, ctypes.byref(b))
    return bool(a.value) and a.value == b.value


# 今日面板遮挡探测用的截图缓存：整块面板区域（外加顶部保护带）只抓一次屏，
# 复用同一块 DIB，别每轮新建。
_TSCAN = {"dc": None, "ptr": None, "bmp": None, "w": 0, "h": 0}

# 灰线（账号行分隔线）动态检测缓存。写死 46 在「全屏/最大化」下会跑位——灰线
# 实际距窗口底约 57 DIP，且随窗口模式浮动。rect 一动先进"动画期"：只测量、
# 不采纳，连续两次测到接近的合理值才更新，避免切换尺寸时面板钉在账号行上。
_SEP = {"dip": TODAY_GAP_B_DIP, "rect": None, "t": 0.0, "changed": 0.0,
        "last": None, "found": False,
        "dc": None, "ptr": None, "bmp": None, "w": 0, "h": 0}

# 遮挡滞回状态。滚轮滚动时列表行间空白会造成单帧"没被挡"，必须有滞回。
_OBSC = {"on": False, "clean": 0}

# 窗口 rect 最近一次变化时间：面板照常跟 rect 走，但动画期冻结遮挡判定，
# 免得全屏切换时滑过面板区的列表行被当成账号菜单，把面板误收起来。
_GEO = {"sig": None, "t": 0.0}


def find_account_sep(r, width=None):
    """动态找「账号行上方那条水平灰线」距窗口底的距离（DIP）。

    灰线是一条横贯侧边栏宽度的浅灰线（灰度 195~240），在账号行（头像+手机号）
    之上。从窗口底往上扫侧边栏左侧那块、110 DIP 高，找最先命中的「灰像素占比
    > 55%」且落在 40~80 DIP 合理区间的行。

    扫描宽度必须跟侧边栏当前宽度一致（width）：写死 256 DIP，侧边栏拖窄后
    右侧会扫进对话区的白底，灰线占比永远不够，落点就一直停在旧值上。

    - rect 刚变化：进入 1.2 秒动画期，每 0.15 秒测一次，但不立刻采用；
    - 连续两次读数差 <= 1.2 DIP 才更新面板落点；
    - 静止后 2 秒复核一次；扫不到/读数离谱就维持原值。

    返回距窗口底的 DIP 数（像素 / S）。
    """
    now = time.time()
    sig = (r.left, r.top, r.right, r.bottom)
    if _SEP["rect"] != sig:
        _SEP["rect"] = sig
        _SEP["t"] = now
        _SEP["changed"] = now
        _SEP["last"] = None
        return _SEP["dip"]                 # 尺寸动画期先不动，面板跟着窗口底走即可

    hot = now - _SEP["changed"] < TODAY_SEP_UNSTABLE_S
    interval = TODAY_SEP_SAMPLE_S if hot else TODAY_SEP_IDLE_S
    if now - _SEP["t"] < interval:
        return _SEP["dip"]
    _SEP["t"] = now

    x0 = r.left + sc(TODAY_GAP_L_DIP)
    w = int(width) if width else sc(TODAY_W_DIP)
    h = sc(110)
    y0 = r.bottom - h
    if w <= 0 or h <= 0:
        return _SEP["dip"]
    if _SEP["w"] != w or _SEP["h"] != h:
        drop_dib(_SEP["dc"], _SEP["bmp"])
        _SEP["dc"], _SEP["ptr"], _SEP["bmp"] = new_dib(w, h)
        _SEP["w"], _SEP["h"] = w, h
    screen = user32.GetDC(None)
    ok = gdi32.BitBlt(_SEP["dc"], 0, 0, w, h, screen, x0, y0, SRCCOPY)
    user32.ReleaseDC(None, screen)
    if not ok:
        return _SEP["dip"]
    buf = (ctypes.c_ubyte * (w * h * 4)).from_address(_SEP["ptr"].value)
    g = bytes(buf[1::4])                          # 绿通道：UI 底色中性，绿≈亮度
    grey = bytes(1 if 195 <= i <= 240 else 0 for i in range(256))
    need = w * 0.55
    found = None
    for yy in range(h - 1, -1, -1):               # 从下往上，第一条合理命中即灰线
        if sum(g[yy * w:(yy + 1) * w].translate(grey)) >= need:
            dip = (h - yy) / float(S)
            if valid_sep(dip):
                found = dip
                break
    if found is None:
        _SEP["last"] = None
        return _SEP["dip"]
    if _SEP["last"] is not None and abs(_SEP["last"] - found) <= 1.2:
        _SEP["dip"] = found
        _SEP["found"] = True
    _SEP["last"] = found
    return _SEP["dip"]


def today_obscured(r, panel=None):
    """今日面板那块区域是不是被别的 UI（账号菜单 / 展开的最近列表）占了。

    判据是整块面板区，不能只看灰线上方 2~8 DIP 的一条窄缝：展开列表后那条缝
    恰好落在行间空白上，会判成"没挡"，滚轮一滚文字扫过去就藏，一帧一帧地闪。
    做法：

      · 一次抓下整块面板区域，外加顶上 8 DIP 保护带；
      · 面板自己画的字用它的 alpha 通道挖掉（否则会把自己当成列表文字）；
      · 底色按像素众数现算，不写死底色值、也不用"够白就算弹层"这种单阈值——
        全屏下白底不会被误判成账号菜单而整块消失；
      · 在面板区、数字区、保护带各取几条细横带统计非底像素；
      · 命中立刻藏，连续 3 帧干净就恢复（obsc_step 滞回）。

    返回 True = 被占，要把面板藏起来。
    """
    guard = sc(OBSC_GUARD_DIP)
    pw = panel.w if panel is not None else sc(TODAY_W_DIP)
    ph = panel.h if panel is not None else sc(TODAY_H_DIP)
    x0 = r.left + sc(TODAY_GAP_L_DIP)
    y0 = r.bottom - sc(_SEP["dip"]) - ph - guard
    sw, sh = pw, ph + guard
    if sw <= 0 or sh <= 0:
        return False
    if (x0 < 0 or y0 < 0 or x0 + sw > user32.GetSystemMetrics(0)
            or y0 + sh > user32.GetSystemMetrics(1)):
        return False

    if _TSCAN["w"] != sw or _TSCAN["h"] != sh:
        drop_dib(_TSCAN["dc"], _TSCAN["bmp"])
        _TSCAN["dc"], _TSCAN["ptr"], _TSCAN["bmp"] = new_dib(sw, sh)
        _TSCAN["w"], _TSCAN["h"] = sw, sh
    screen = user32.GetDC(None)
    ok = gdi32.BitBlt(_TSCAN["dc"], 0, 0, sw, sh, screen, x0, y0, SRCCOPY)
    user32.ReleaseDC(None, screen)
    if not ok:
        return False
    # 别的程序（记事本等）从 WorkBuddy 透明侧边栏后面透出来时，屏幕像素会被它的
    # 文字/阴影污染，判不出 WorkBuddy 自己的菜单。这种时候直接不判：面板本来就
    # 被那个窗口挡住一部分，等它挪开再说。账号菜单、最近列表都在 WorkBuddy 自己
    # 的进程里，不会进 external 名单，照旧能立刻收起。
    if rects_overlap(x0, y0, x0 + sw, y0 + sh,
                     external_window_rects(panel.owner if panel else 0)):
        return obsc_step(_OBSC, False, OBSC_CLEAR_FRAMES)

    buf = (ctypes.c_ubyte * (sw * sh * 4)).from_address(_TSCAN["ptr"].value)
    gfull = bytes(buf[1::4])                      # 绿通道 ≈ 灰度
    if not gfull:
        return False

    alpha_full = None
    if panel is not None and panel.ptr and panel.painted and panel.w == sw and panel.h == ph:
        pbuf = (ctypes.c_ubyte * (pw * ph * 4)).from_address(panel.ptr.value)
        alpha_full = bytes(pbuf[3::4])            # >0 的点是面板自己的字

    bg = Counter(gfull).most_common(1)[0][0]
    foreign = total = 0
    for ya, yb in obsc_band_ranges(guard):
        ya, yb = max(0, ya), min(sh, yb)
        if yb <= ya:
            continue
        alpha = None
        if alpha_full is not None and ya >= guard:
            alpha = alpha_full[(ya - guard) * sw:(yb - guard) * sw]
        f, t = obsc_foreign(gfull[ya * sw:yb * sw], bg, alpha)
        foreign += f
        total += t
    hit = total > 0 and foreign > total * OBSC_RATIO
    return obsc_step(_OBSC, hit, OBSC_CLEAR_FRAMES)


def today_loop():
    """窗口线程：建窗口 + 抽消息 + 跟手 + 按需重绘，30ms 一轮。

    **自己建的窗口自己抽消息**（pump_msgs），这一条是这次死机的教训：不抽消息
    的线程建出来的窗口会被 Windows 判成"未响应"，DWM 盖一块黑色幽灵窗口，
    点不掉、重启 WorkBuddy 也没用。
    """
    panel = None
    while not QUIT:
        try:
            pump_msgs()
            if panel is None or not user32.IsWindow(panel.hwnd):
                panel = TodayPanel()
                PANEL[0] = panel

            owner = None
            fg = user32.GetForegroundWindow()
            wins = [h for h in wb_windows_cached()
                    if user32.IsWindowVisible(h) and not user32.IsIconic(h)]
            if fg in wins:
                owner = fg
            elif wins:
                owner = wins[0]
            if not owner:
                panel.park()
                time.sleep(0.3)
                continue
            with BARS_LOCK:
                bar = next((b for b in BARS if b.owner == owner), None)
            # 用可见外框（DWM）：全屏时 GetWindowRect 会比屏幕每边多 11 px 的
            # 隐形拖动边框，拿它算面板落点会整块往下压，正好压到账号行上。
            r = bar.rect() if bar is not None else None
            if not r:
                r = wt.RECT()
                if not user32.GetWindowRect(owner, ctypes.byref(r)):
                    panel.park()
                    time.sleep(0.3)
                    continue
            # 面板宽度跟着侧边栏走：拖动侧边栏时三列要按新宽度重新分配、整体居中
            pw = today_panel_w(bar)
            if abs(pw - panel.w) >= 2:
                panel.resize(pw)
            # 只在 WorkBuddy 自己在前台时才露脸：面板是贴在它界面上的字，
            # 切到浏览器或任何别的程序时一律收起，绝不压到别人界面上。
            # 也正因为"看不见就别测"，所有抓屏判定都集中在这条分支之后。
            front = wb_in_front(owner)
            if not front and not SELFTEST:
                panel.park()
                time.sleep(0.05)
                continue
            find_account_sep(r, panel.w)   # 更新灰线位置（动画期只测量不采纳）

            # 尺寸切换期间面板照常跟 rect 走（place 每轮都会重算），不再强制收起；
            # 只冻结遮挡判定：全屏动画中列表行会暂时滑进面板区，不能把它当弹层。
            rsig = (r.left, r.top, r.right, r.bottom)
            if _GEO["sig"] != rsig:
                _GEO["sig"] = rsig
                _GEO["t"] = time.time()

            vals = today_vals()
            # 空态：今天一笔都没有（或首批扫描还没回来）就不露脸，别画一排 0
            if not _TREADY[0] or today_empty(vals):
                panel.sig = None
                panel.park()
                time.sleep(0.2)
                continue
            # 面板那块区域被账号菜单 / 展开的最近列表占了 → 立刻收起，别浮在它们上面。
            # rect 刚变完的 0.6 秒内冻结判定，沿用上一次结论：动画中的列表行不算遮挡。
            if time.time() - _GEO["t"] < TODAY_OBSC_FREEZE_S:
                obsc = _OBSC["on"]
            else:
                obsc = today_obscured(r, panel)
            if obsc:
                panel.park()
                time.sleep(0.03)
                continue
            sig = (vals.get("n"), round(vals.get("credit") or 0.0, 2),
                   vals.get("tok"), panel.w, panel.h)
            if sig != panel.sig:
                panel.sig = sig
                panel.paint(vals)
            panel.place(owner, r)
        except Exception as e:
            log_err("today", e)
        time.sleep(0.03)


# ---------------------------------------------------------------- 主循环
CALLBACK = None


def bar_proc(hwnd, msg, wp, lp):
    if msg == 0x0084:      # WM_NCHITTEST -> 点击穿透
        return HTTRANSPARENT
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def panel_proc(hwnd, msg, wp, lp):
    """今日面板的窗口过程：纯展示，整块点击穿透（没有交互元素了）"""
    if msg == 0x0084:      # WM_NCHITTEST -> 点击穿透
        return HTTRANSPARENT
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def host_proc(hwnd, msg, wp, lp):
    if msg == 0x0113:      # WM_TIMER
        try:
            manager_step()
        except Exception as e:
            log_err("manager", e)
        return 0
    if msg == 0x0002:      # WM_DESTROY
        global QUIT
        QUIT = True
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def register_classes():
    hinst = kernel32.GetModuleHandleW(None)
    global CALLBACK
    CALLBACK = (WNDPROC(bar_proc), WNDPROC(host_proc), WNDPROC(panel_proc))
    for name, proc in ((WND_CLASS, CALLBACK[0]), (HOST_CLASS, CALLBACK[1]),
                       (PANEL_CLASS, CALLBACK[2])):
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, wt.LPCWSTR(32512))
        wc.lpszClassName = name
        user32.RegisterClassW(ctypes.byref(wc))
    return hinst


def init_env():
    global S, GAP_L_DIP, GAP_B_DIP, GAP_R_DIP
    S = 1.0
    try:
        S = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
    except Exception:
        pass
    if S < 1:
        S = 1.0
    for key, name in (("TP_LEFT", "GAP_L_DIP"), ("TP_RIGHT", "GAP_R_DIP"),
                      ("TP_BOTTOM", "GAP_B_DIP")):
        try:
            v = os.environ.get(key)
            if v:
                globals()[name] = float(v)
        except Exception:
            pass


def worker():
    """后台：扫描全机今日/累计。

    口径是「每窗口只看自己那个对话」，全机汇总不上界面，
    所以默认不启动这个线程；留着是为了以后要加全机口径时直接用。
    """
    while not QUIT:
        try:
            scan_usage()
        except Exception:
            pass
        time.sleep(8.0)


def guard_alive():
    """守护进程在不在：看它的单例互斥体是否已被创建"""
    h = kernel32.OpenMutexW(SYNCHRONIZE, False, GUARD_MUTEX)
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


def respawn_self(delay):
    """没有守护进程时，先拉一个替身再退出（替身延迟启动，避开自己的互斥体）"""
    exe = sys.executable
    base = os.path.dirname(exe)
    for nm in ("pythonw.exe", "python.exe"):
        cand = os.path.join(base, nm)
        if os.path.isfile(cand):
            exe = cand
            break
    here = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ)
    env["TP_START_DELAY"] = str(delay)
    subprocess.Popen([exe, os.path.abspath(__file__)], cwd=here, env=env,
                     creationflags=0x08000000,          # CREATE_NO_WINDOW
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)


def reload_watch():
    """hud.py 被改动后自己换掉自己 —— 改样式/逻辑不必再让用户手动重启。

    盯住自己的源文件：大小和 mtime 都稳定 0.7 秒（说明写完了）就退出，
    守护进程 1 秒内会拉起新实例；没有守护进程就先拉替身再退。
    设 TP_NORELOAD=1 可关掉这个行为。
    """
    if os.environ.get("TP_NORELOAD"):
        return
    path = os.path.abspath(__file__)
    try:
        st = os.stat(path)
        sig = (st.st_mtime, st.st_size)
    except OSError:
        return
    time.sleep(2.5)                      # 启动稳定后再开始盯
    while not QUIT:
        time.sleep(1.0)
        try:
            st = os.stat(path)
        except OSError:
            continue
        cur = (st.st_mtime, st.st_size)
        if cur == sig:
            continue
        time.sleep(0.7)                  # 等写完
        try:
            st2 = os.stat(path)
        except OSError:
            continue
        if (st2.st_mtime, st2.st_size) != cur:
            sig = (st2.st_mtime, st2.st_size)
            continue
        sig = cur
        log_err("reload", "检测到 hud.py 更新，自行重启")
        if not guard_alive():
            try:
                respawn_self(1.5)
            except Exception as e:
                log_err("respawn", e)
        os._exit(0)


def main():
    global FONT_K, MEAS_DC, QUIT
    d = os.environ.get("TP_START_DELAY")
    if d:                                # 替身专用：等旧实例把互斥体放掉
        try:
            time.sleep(max(0.0, min(10.0, float(d))))
        except Exception:
            pass
    user32.SetProcessDPIAware()
    init_env()
    rebuild_ink_table()

    ctypes.set_last_error(0)
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return

    hinst = register_classes()
    FONT_K = gdi32.CreateFontW(-sc(FONT_DIP * SS), 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 4, 0,
                               "Microsoft YaHei UI")
    MEAS_DC, mptr, mbmp = new_dib(1, 1)

    host = user32.CreateWindowExW(0, HOST_CLASS, "TPulseHost", WS_POPUP,
                                  0, 0, 0, 0, wt.HWND(HWND_MESSAGE), None, hinst, None)
    if not host:
        return

    threading.Thread(target=follow_loop, daemon=True).start()
    threading.Thread(target=today_poll_loop, daemon=True).start()
    threading.Thread(target=today_loop, daemon=True).start()
    threading.Thread(target=reload_watch, daemon=True).start()
    user32.SetTimer(host, 1, 700, None)
    manager_step()

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
