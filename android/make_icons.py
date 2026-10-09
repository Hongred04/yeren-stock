# -*- coding: utf-8 -*-
"""生成安卓启动图标（纯标准库，不依赖 Pillow）。

画一个深色圆角方块 + 三根 K 线（红涨绿跌，A 股配色），
输出各密度 mipmap PNG。改配色/形状改这里的常量即可。
"""
from __future__ import annotations

import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "app", "src", "main", "res")

BG = (22, 35, 63)          # 深海军蓝
UP = (229, 57, 53)         # 红 = 涨
DOWN = (38, 166, 154)      # 绿 = 跌
WICK = (222, 231, 245)     # 影线

DENSITIES = {
    "mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192,
}


def write_png(path: str, px: list[list[tuple[int, int, int, int]]]) -> None:
    h = len(px)
    w = len(px[0])
    raw = bytearray()
    for row in px:
        raw.append(0)                      # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)


def make(size: int) -> list[list[tuple[int, int, int, int]]]:
    """画一张 size×size 的图。"""
    s = size
    canvas = [[(0, 0, 0, 0)] * s for _ in range(s)]

    # ── 圆角方块背景（留 4% 边距）──
    m = max(1, round(s * 0.04))
    rad = max(2, round(s * 0.22))
    for y in range(m, s - m):
        for x in range(m, s - m):
            # 四角做圆角裁切
            cx = min(max(x, m + rad), s - m - 1 - rad)
            cy = min(max(y, m + rad), s - m - 1 - rad)
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy > rad * rad:
                continue
            canvas[y][x] = (*BG, 255)

    # ── 三根 K 线 ──
    # (中心x比例, 实体半宽比例, 影线上下比例, 是否红)
    candles = [
        (0.30, 0.075, 0.20, 0.62, True),    # 红，实体偏低
        (0.50, 0.075, 0.13, 0.50, False),   # 绿，实体居中
        (0.70, 0.075, 0.28, 0.80, True),    # 红，实体偏高
    ]
    wick_w = max(1, round(s * 0.018))
    for cxr, hwr, top_r, bot_r, is_up in candles:
        cx = round(s * cxr)
        half = max(1, round(s * hwr))
        y0 = round(s * top_r)
        y1 = round(s * bot_r)
        col = UP if is_up else DOWN

        # 影线（细线贯穿）
        for y in range(y0, y1 + 1):
            for x in range(cx - wick_w // 2, cx + wick_w // 2 + 1):
                if m <= x < s - m and m <= y < s - m:
                    canvas[y][x] = (*WICK, 255)
        # 实体（居中、高度 55%）
        span = y1 - y0
        by0 = y0 + round(span * 0.22)
        by1 = y1 - round(span * 0.22)
        for y in range(by0, by1 + 1):
            for x in range(cx - half, cx + half + 1):
                if m <= x < s - m and m <= y < s - m:
                    canvas[y][x] = (*col, 255)
    return canvas


def main() -> None:
    for name, size in DENSITIES.items():
        p = os.path.join(RES, f"mipmap-{name}", "ic_launcher.png")
        write_png(p, make(size))
        print(f"  生成 {p}  ({size}x{size})")

    # 自适应图标前景（v26+）：矢量画同一组 K 线，落在中间安全区内
    # （自适应图标会被系统裁成各种形状，只有中间 ~66% 是安全区）
    fg = os.path.join(RES, "drawable", "ic_launcher_foreground.xml")
    os.makedirs(os.path.dirname(fg), exist_ok=True)
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<vector xmlns:android="http://schemas.android.com/apk/res/android"',
             '    android:width="108dp" android:height="108dp"',
             '    android:viewportWidth="108" android:viewportHeight="108">']
    # (中心x, 影线起, 影线止, 实体起, 实体止, 是否红)
    for cx, wy0, wy1, by0, by1, up in [
        (39, 30, 78, 41, 68, True),
        (54, 24, 70, 36, 58, False),
        (69, 34, 86, 45, 76, True),
    ]:
        col = "#E53935" if up else "#26A69A"
        lines.append(f'    <!-- {"涨" if up else "跌"} -->')
        lines.append(f'    <path android:fillColor="#DEE7F5" '
                     f'android:pathData="M{cx - 1.5},{wy0}h3v{wy1 - wy0}h-3z"/>')
        lines.append(f'    <path android:fillColor="{col}" '
                     f'android:pathData="M{cx - 6},{by0}h12v{by1 - by0}h-12z"/>')
    lines.append('</vector>')
    lines.append('')
    with open(fg, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    ad = os.path.join(RES, "mipmap-anydpi-v26", "ic_launcher.xml")
    os.makedirs(os.path.dirname(ad), exist_ok=True)
    with open(ad, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write('<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n')
        f.write('    <background android:drawable="@color/ic_launcher_background"/>\n')
        f.write('    <foreground android:drawable="@drawable/ic_launcher_foreground"/>\n')
        f.write('</adaptive-icon>\n')

    with open(os.path.join(RES, "values", "ic_launcher_background.xml"),
              "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n<resources>\n')
        f.write('    <color name="ic_launcher_background">#16233F</color>\n</resources>\n')
    print("  生成自适应图标 XML")


if __name__ == "__main__":
    main()
