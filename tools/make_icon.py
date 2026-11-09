"""从"活板"白文印蒙版生成应用图标。

蒙版（gui/assets/icon_mask_264.png，原始出处 _shot/about_mock/mask_264.png）
是界面"关于"页那枚印章的 alpha 通道：
白色字形处 alpha 高，印面其余部分 alpha 低。界面上靠 CSS 把印面染成
var(--seal)（浅色 #A83A2E / 深色 #E07A6C），这里做的是同一件事的位图版。

产出（写进 gui/assets/，先备份旧文件到 backups/icon-<日期>/）：
  huoban.ico        16/20/24/32/48/64/128/256 多尺寸，给窗口与 exe 用
  icon_light_1024.png 浅色主题印色 #A83A2E
  icon_dark_1024.png  深色主题印色 #E07A6C
  icon_tile_1024.png  与浅色同版（打包商店图用的方版）

用法：
    python tools/make_icon.py                # 生成并覆盖 gui/assets/
    python tools/make_icon.py --preview      # 只出 _shot/icon_strip.png 供肉眼检查

蒙版只有 264px，256 及以下是从原尺寸缩的（清晰），1024 是放大（会软），
这是源图分辨率决定的，不是参数问题。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MASK = ROOT / "gui" / "assets" / "icon_mask_264.png"
ASSETS = ROOT / "gui" / "assets"
SEAL_LIGHT = (0xA8, 0x3A, 0x2E, 255)
SEAL_DARK = (0xE0, 0x7A, 0x6C, 255)
ICO_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def compose(mask: Image.Image, color: tuple[int, int, int, int], size: int) -> Image.Image:
    """印面铺 color，字形留白，输出 size×size。"""
    alpha = mask.split()[3]
    if size != mask.width:
        alpha = alpha.resize((size, size), Image.LANCZOS)
    plate = Image.new("RGBA", (size, size), color)
    glyph = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    plate.paste(glyph, (0, 0), alpha)
    return plate


def strip(mask: Image.Image) -> Image.Image:
    """把所有尺寸并排拼一张，检查小尺寸是否糊成一团。"""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    pad = 16
    w = sum(sizes) + pad * (len(sizes) + 1)
    h = max(sizes) + pad * 2
    canvas = Image.new("RGB", (w, h), (250, 248, 244))
    x = pad
    for s in sizes:
        img = compose(mask, SEAL_LIGHT, s)
        canvas.paste(img, (x, pad + (max(sizes) - s)), img)
        x += s + pad
    return canvas


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="只生成 _shot/icon_strip.png")
    ap.add_argument("--mask", default=str(MASK))
    args = ap.parse_args()

    mask = Image.open(args.mask).convert("RGBA")
    if mask.width != mask.height:
        print(f"蒙版必须是正方形，实际 {mask.size}")
        return 2

    if args.preview:
        out = ROOT / "_shot" / "icon_strip.png"
        strip(mask).save(out)
        print(out)
        return 0

    backup = ROOT / "backups" / f"icon-{date.today():%Y%m%d}"
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("huoban.ico", "icon_light_1024.png", "icon_dark_1024.png", "icon_tile_1024.png"):
        old = ASSETS / name
        if old.is_file() and not (backup / name).exists():
            shutil.copy2(old, backup / name)
    print(f"旧资产已备份到 {backup}")

    # ico：Pillow 用最大尺寸那张 + sizes 参数自己缩，但那样小尺寸是双三次，
    # 这里逐尺寸手工 LANCZOS 再 append，边缘更干净。
    frames = [compose(mask, SEAL_LIGHT, s) for s in ICO_SIZES]
    frames[-1].save(ASSETS / "huoban.ico", format="ICO", sizes=[(s, s) for s in ICO_SIZES],
                    append_images=frames[:-1])
    print(f"写入 {ASSETS / 'huoban.ico'}  {len(ICO_SIZES)} 档")

    compose(mask, SEAL_LIGHT, 1024).save(ASSETS / "icon_light_1024.png")
    compose(mask, SEAL_DARK, 1024).save(ASSETS / "icon_dark_1024.png")
    compose(mask, SEAL_LIGHT, 1024).save(ASSETS / "icon_tile_1024.png")
    print("写入 3 张 1024 png（由 264 放大，边缘偏软）")

    chk = Image.open(ASSETS / "huoban.ico")
    print("回读 ico 尺寸:", sorted(chk.info.get("sizes", [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
