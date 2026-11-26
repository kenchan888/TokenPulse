# -*- coding: utf-8 -*-
"""今日面板的纯逻辑回归：单位换档、空态、三列锚点、对齐基准。

不建窗口、不碰屏幕，跑完即退。改动 split_k / today_empty / today_anchors
/ align_x / valid_sep / obsc_step / obsc_foreign / rects_overlap 之后必跑这个。

    python test_metrics.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hud as B  # noqa: E402

PASS = [0]
FAIL = [0]


def eq(name, got, want):
    if got == want:
        PASS[0] += 1
        print("  ok   %-34s %r" % (name, got))
    else:
        FAIL[0] += 1
        print("  FAIL %-34s got %r want %r" % (name, got, want))


def main():
    print("[A] split_k 单位换档")
    cases = [
        (0, ("0", "")),
        (842, ("842", "")),
        (8420, ("8,420", "")),
        (9999, ("9,999", "")),
        (10000, ("10.0", "K")),
        (128400, ("128.4", "K")),
        (999949, ("999.9", "K")),
        (999999, ("1.00", "M")),      # 别显示 1000.0K
        (1000000, ("1.00", "M")),
        (132820000, ("132.82", "M")),
    ]
    for n, want in cases:
        eq("split_k(%d)" % n, B.split_k(n), want)

    print("[B] fmt_k 与 split_k 同源")
    for n in (0, 8420, 10000, 999999, 132820000):
        v, u = B.split_k(n)
        eq("fmt_k(%d)" % n, B.fmt_k(n), v + u)

    print("[C] 空态判据")
    eq("today_empty({})", B.today_empty({}), True)
    eq("today_empty(None)", B.today_empty(None), True)
    eq("全 0 → 不显示", B.today_empty({"n": 0, "credit": 0.0, "tok": 0}), True)
    eq("只有轮", B.today_empty({"n": 2, "credit": 0.0, "tok": 0}), False)
    eq("只有钱", B.today_empty({"n": 0, "credit": 0.31, "tok": 0}), False)
    eq("只有 token", B.today_empty({"n": 0, "credit": 0.0, "tok": 1200}), False)

    print("[D] 三列锚点")
    eq("today_anchors(383)", B.today_anchors(383), (0.0, 191.5, 382.0))
    eq("today_anchors(256)", B.today_anchors(256), (0.0, 128.0, 255.0))
    for w in (239, 383, 1000):
        a = B.today_anchors(w)
        ok = a[0] == 0.0 and a[1] == w / 2.0 and a[2] == float(w - 1)
        eq("锚点居中/贴右 %d" % w, ok, True)

    print("[E] 对齐基准")
    eq("左对齐 x=left", B.align_x(0, 100.0, 40.0), 100.0)
    eq("居中 x=left-tw/2", B.align_x(1, 100.0, 40.0), 80.0)
    eq("右对齐 x=left-tw", B.align_x(2, 100.0, 40.0), 60.0)
    # 尾列右对齐后，右缘必须正好落在 W 上（W 已留 1 DIP 防抗锯齿被裁）
    for w in (239, 383, 1000):
        for tw in (30.0, 75.5, 140.0):
            x = B.align_x(2, float(w), tw)
            eq("尾列右缘贴 %d/%.1f" % (w, tw), round(x + tw, 6), float(w))
    # 中列居中：左右留白相等
    for w in (239, 383, 1000):
        tw = 88.0
        x = B.align_x(1, float(w // 2), tw)
        eq("中列居中等距 %d" % w, round(x, 6), round(w // 2 - tw / 2.0, 6))

    print("[F] 面板常量自洽")
    eq("内容宽 256 < 侧边栏 280", B.TODAY_W_DIP < 280, True)
    eq("面板高正数", B.TODAY_H_DIP > 0, True)
    eq("底距 >= 账号行 46", B.TODAY_GAP_B_DIP >= 46, True)
    eq("内容底边在面板内", B.TODAY_PAD_B_DIP < B.TODAY_H_DIP, True)
    eq("数字 > 标签字号", B.TODAY_V_DIP > B.TODAY_L_DIP, True)

    print("[G] 灰线候选合理性")
    for dip in (40.0, 56.7, 80.0):
        eq("valid_sep(%s)" % dip, B.valid_sep(dip), True)
    for dip in (0.0, 12.0, 39.9, 80.1, 120.0):
        eq("valid_sep(%s) 拒绝" % dip, B.valid_sep(dip), False)

    print("[H] 遮挡滞回")
    st = {"on": False, "clean": 0}
    eq("命中立刻藏", B.obsc_step(st, True, 3), True)
    eq("单帧空白不恢复", B.obsc_step(st, False, 3), True)
    eq("两帧空白不恢复", B.obsc_step(st, False, 3), True)
    eq("第三帧空白才恢复", B.obsc_step(st, False, 3), False)
    eq("恢复后再命中", B.obsc_step(st, True, 3), True)

    print("[I] 非底色统计（白底/深字/自身字）")
    plain = bytes([247] * 100)
    eq("纯底色 foreign=0", B.obsc_foreign(plain, 247), (0, 100))
    white = bytes([255] * 100)
    eq("白弹层差 8 也认", B.obsc_foreign(white, 247), (100, 100))
    dark = bytes([30] * 20 + [247] * 80)
    eq("深色列表文字", B.obsc_foreign(dark, 247), (20, 100))
    alpha = bytes([255] * 20 + [0] * 80)
    eq("面板自身字挖掉", B.obsc_foreign(dark, 247, alpha), (0, 100))

    print("[J] 外部程序窗口相交判定")
    # 扫描区 (100,200)-(106,202)
    eq("外部窗口压在扫描区", B.rects_overlap(100, 200, 106, 202,
                                       [(102, 199, 105, 202)]), True)
    eq("不相交的窗口忽略", B.rects_overlap(100, 200, 106, 202,
                                      [(500, 500, 600, 600)]), False)
    eq("仅贴边不算相交", B.rects_overlap(100, 200, 106, 202,
                                     [(106, 200, 200, 300)]), False)
    eq("内部小窗口也算", B.rects_overlap(100, 200, 106, 202,
                                     [(102, 201, 103, 202)]), True)

    print("[K] 遮挡横带区间")
    guard = B.sc(B.OBSC_GUARD_DIP)
    ranges = B.obsc_band_ranges(guard)
    eq("横带条数", len(ranges), len(B.OBSC_BANDS_DIP))
    eq("每条都有高度", all(b > a for a, b in ranges), True)
    eq("横带覆盖到面板下部", ranges[-1][1] > guard + B.sc(B.TODAY_H_DIP) - 12, True)

    print("\n%d passed, %d failed" % (PASS[0], FAIL[0]))
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
