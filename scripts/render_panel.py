# -*- coding: utf-8 -*-
"""离线渲染今日面板，存 PNG + 报墨量。

不依赖截屏（锁屏/远控下 BitBlt 拿不到画面），直接把面板画进它自己的 DIB，
按 alpha 合成到白底上看效果。用于改版式后的自查和给用户的对比图。

    python render_panel.py                # 存到 _tmp/panel.png
    python render_panel.py out.png        # 指定输出路径

面板的绘制管线是"先按 SS 倍画进大 DIB，再缩回 1 倍"，本体是纯 alpha 的墨迹，
所以白底合成 = 255 - alpha，和真机叠在侧边栏上的观感一致。
"""
import ctypes
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hud as B  # noqa: E402

B.init_env()          # 拿真实缩放（SS），否则画面是 1 倍的假比例
# MEAS_DC 是 main() 里建的（量字宽用），离线渲染得自己补一份，
# 否则 SelectObject(None) 失败、所有文字宽度量成 0 → 右对齐列会画到画布外。
B.MEAS_DC, _mptr, _mbmp = B.new_dib(1, 1)

SAMPLE = {"n": 10, "credit": 215.89, "tok": 65820000}


def write_png(path, rows, w):
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, len(rows), 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def render(vals):
    panel = B.TodayPanel()
    B.pump_msgs()                     # 自己建的窗口自己抽消息，别留幽灵窗口
    try:
        panel.paint(vals)
        w, h = panel.w, panel.h
        mv = memoryview((ctypes.c_ubyte * (w * h * 4)).from_address(panel.ptr.value))
        alpha = bytes(mv[3::4])
        ink = sum(1 for a in alpha if a > 24)
        xs = [x for y in range(h) for x in range(w) if alpha[y * w + x] > 24]
        ys = [y for y in range(h) if any(alpha[y * w + x] > 24 for x in range(w))]
        bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None
        rows = []
        for yy in range(h):               # new_dib 是自顶向下的 DIB，别翻
            row = bytearray()
            for xx in range(w):
                a = alpha[yy * w + xx]
                row += bytes((255 - a, 255 - a, 255 - a))
            rows.append(bytes(row))
        return rows, w, h, ink, alpha, bbox
    finally:
        try:
            B.user32.DestroyWindow(panel.hwnd)
        except Exception:
            pass


def main():
    args = sys.argv[1:]
    out_png = next((a for a in args if a.lower().endswith(".png")), None)
    here = os.path.dirname(os.path.abspath(__file__))
    tmp = os.path.join(os.path.dirname(here), "_tmp")
    rows, w, h, ink, alpha, bbox = render(SAMPLE)
    path = out_png or os.path.join(tmp, "panel.png")
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    write_png(path, rows, w)
    pct = ink * 100.0 / (w * h)
    # 墨点是文字核心，本来就该有 alpha>0；坏的样子是"整块全实"或"一片空白"
    broken = pct > 60.0 or ink == 0
    print("%s  %dx%d  墨点 %d (%.1f%%)  墨迹框 %s  %s"
          % ("[FAIL]" if broken else "[ OK ]", w, h, ink, pct, bbox, path))
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
