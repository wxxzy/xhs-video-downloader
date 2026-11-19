# -*- coding: utf-8 -*-
"""
make_icon.py — 生成应用图标（小红书风格原创设计）

设计说明
--------
不使用小红书官方 logo（避免商标风险），而是采用"小红书风格"视觉语言：
- 正红色底 #ff2442（与小红书品牌色一致）
- 白色几何符号，简洁、在大尺寸和小尺寸下都清晰
- 圆角矩形，符合移动端图标习惯

两个方案：
  A（推荐）：红底 + 白色下载箭头（"下载器"语义最直白）
  B：白底 + 红色下载箭头（浅色系统下更协调）

输出：
  assets/icon.ico          Windows 多尺寸图标
  assets/icon.png          1024×1024 预览
  assets/icon.icns         macOS 图标（若装了 iconutil 才能用，否则只留 png 供手动转）
  assets/icon-preview.png  方案对比图（供挑选）
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT = Path(__file__).resolve().parent.parent
ASSETS = PROJECT / "assets"
ASSETS.mkdir(exist_ok=True)

# 小红书品牌红
XHS_RED = (255, 36, 66, 255)
WHITE = (255, 255, 255, 255)

# 超采样倍数（先大后缩，得到抗锯齿边缘）
SS = 4


def _rounded_mask(size: int, radius_ratio: float) -> Image.Image:
    """生成圆角矩形遮罩（L 模式）"""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    r = int(size * radius_ratio)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    return m


def _draw_download_arrow(draw: ImageDraw.ImageDraw, size: int,
                         color, on_white: bool = False) -> None:
    """
    在 size×size 画布上绘制下载箭头（含托盘）。
    坐标用归一化比例，保证任意尺寸下比例一致。

    布局（自上而下）：
      0.155  竖杆起点
      0.385  竖杆终点
      0.370  箭头三角顶边
      0.545  箭头尖
      0.645  托盘顶（与箭头尖留 0.10 间隙，避免糊在一起）
      0.838  托盘底
    """
    S = size
    w = S * 0.078          # 线条粗细
    half = w / 2

    # --- 箭头竖杆 ---
    shaft_top, shaft_bottom = S * 0.155, S * 0.385
    draw.line([(S * 0.5, shaft_top), (S * 0.5, shaft_bottom)],
              fill=color, width=int(w))
    draw.ellipse([S * 0.5 - half, shaft_top - half, S * 0.5 + half, shaft_top + half],
                 fill=color)

    # --- 箭头三角 ---
    head = [
        (S * 0.320, S * 0.370),
        (S * 0.680, S * 0.370),
        (S * 0.5,   S * 0.545),
    ]
    draw.polygon(head, fill=color)

    # --- 托盘（U 形）---
    tray_top, tray_bottom = S * 0.645, S * 0.838
    tray_left, tray_right = S * 0.245, S * 0.755
    draw.line(
        [(tray_left, tray_top), (tray_left, tray_bottom - half),
         (tray_right, tray_bottom - half), (tray_right, tray_top)],
        fill=color, width=int(w), joint="curve",
    )
    # 三个端点补圆头（左上、右上、底部两角）
    for x, y in ((tray_left, tray_top), (tray_right, tray_top),
                 (tray_left, tray_bottom - half), (tray_right, tray_bottom - half)):
        draw.ellipse([x - half, y - half, x + half, y + half], fill=color)


def make_icon(variant: str = "A", size: int = 1024) -> Image.Image:
    """生成指定方案的图标（RGBA）"""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    if variant == "A":
        # 红底 + 白箭头
        bg = Image.new("RGBA", (S, S), XHS_RED)
        img.paste(bg, (0, 0), _rounded_mask(S, 0.225))
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        _draw_download_arrow(ImageDraw.Draw(layer), S, WHITE)
        img = Image.alpha_composite(img, layer)
    else:
        # 白底 + 红箭头 + 细红边
        bg = Image.new("RGBA", (S, S), WHITE)
        img.paste(bg, (0, 0), _rounded_mask(S, 0.225))
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        _draw_download_arrow(d, S, XHS_RED)
        img = Image.alpha_composite(img, layer)
        # 外描边（红色细边，增强浅色背景下的辨识度）
        edge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(edge).rounded_rectangle(
            [0, 0, S - 1, S - 1], radius=int(S * 0.225),
            outline=XHS_RED, width=max(2, int(S * 0.012)),
        )
        img = Image.alpha_composite(img, edge)

    return img.resize((size, size), Image.LANCZOS)


def save_ico(img: Image.Image, path: Path) -> None:
    """保存多尺寸 .ico（Windows 图标）"""
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(path, format="ICO", sizes=sizes)


def _load_cjk_font(size: int):
    """加载支持中文的字体（不同平台路径不同）；失败则返回 None 走默认字体"""
    from PIL import ImageFont
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",      # Windows 微软雅黑
        r"C:\Windows\Fonts\simhei.ttf",    # Windows 黑体
        "/System/Library/Fonts/PingFang.ttc",           # macOS 苹方
        "/System/Library/Fonts/STHeiti Medium.ttc",     # macOS 华文黑体
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",  # Linux 文泉驿
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    return None


def make_preview() -> Image.Image:
    """生成方案对比图：两方案 + 小尺寸效果"""
    cell = 200
    gap = 24
    pad = 28
    label_h = 30
    W = pad * 2 + cell * 2 + gap
    H = pad * 2 + cell + label_h + 110
    canvas = Image.new("RGBA", (W, H), (245, 245, 245, 255))
    d = ImageDraw.Draw(canvas)

    font = _load_cjk_font(15)
    font_small = _load_cjk_font(13)

    a = make_icon("A", cell)
    b = make_icon("B", cell)
    canvas.alpha_composite(a, (pad, pad + label_h))
    canvas.alpha_composite(b, (pad + cell + gap, pad + label_h))

    d.text((pad, pad + 4), "方案 A：红底 + 白箭头（推荐）",
           fill=(40, 40, 40, 255), font=font)
    d.text((pad + cell + gap, pad + 4), "方案 B：白底 + 红箭头",
           fill=(40, 40, 40, 255), font=font)

    # 小尺寸预览（任务栏/文件夹实际观感）
    y = pad + label_h + cell + 42
    d.text((pad, y - 24), "小尺寸实际效果：", fill=(90, 90, 90, 255), font=font_small)
    x = pad
    for s in (16, 24, 32, 48, 64):
        small = make_icon("A", s)
        canvas.alpha_composite(small, (x, y))
        d.rectangle([x, y, x + s, y + s], outline=(205, 205, 205, 255))
        x += s + 18
    return canvas


def main() -> int:
    variant = "A"
    for arg in sys.argv[1:]:
        if arg in ("A", "B", "--a", "--b"):
            variant = arg.strip("-").upper()

    img = make_icon(variant, 1024)
    png = ASSETS / "icon.png"
    img.save(png)
    print(f"PNG 已生成：{png}（1024×1024）")

    ico = ASSETS / "icon.ico"
    save_ico(img, ico)
    print(f"ICO 已生成：{ico}（含 16/24/32/48/64/128/256 多尺寸）")

    preview = make_preview()
    pp = ASSETS / "icon-preview.png"
    preview.save(pp)
    print(f"方案对比图：{pp}")

    # macOS icns 提示（icns 需 iconutil，仅 macOS 可用）
    iconset = ASSETS / "icon.iconset"
    if not iconset.exists():
        iconset.mkdir()
    for s in (16, 32, 64, 128, 256, 512, 1024):
        make_icon(variant, s).save(iconset / f"icon_{s}x{s}.png")
        if s <= 512:
            make_icon(variant, s * 2).save(iconset / f"icon_{s}x{s}@2x.png")
    print(f"macOS iconset 已生成：{iconset}")
    print("  在 macOS 上执行以下命令生成 .icns：")
    print(f"    iconutil -c icns {iconset} -o {ASSETS / 'icon.icns'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
