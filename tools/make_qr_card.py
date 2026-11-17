"""生成关于页「请喝杯茶」浮层用的爱发电二维码成品。

母版不是图片，是下面这份 37×37 的模组网格：2026-11-14 从开发者提供的
爱发电官方卡片里逐格提取（原图二维码区域 148×148、模组约 4px），
逐模组比对零差异，OpenCV 解码与原图一致：
    https://afdian.com/u/e36080ca51d411e9900752540025c377?utm_source=qrpic&utm_medium=link

产出（写进 gui/assets/，先备份旧文件到 backups/qr-card-<日期>/）：
  about_qr_light.png  浅色用：透明底 + 印色 #A83A2E 模块，2× 尺寸（148 → 296），
                      四周留 4 模组透明静区（成品 360），静区透出页面纸底 ——
                      浅色下二维码直接印在纸上，不套白牌。
  about_qr_dark.png   深色用：纸签 #C9C3B6 底 + 墨色 #1B1D1E 模块，2× 尺寸（180 → 360），
                      同样留 4 模组静区。深色底上二维码必须有一块浅底才扫得动：
                      反白（浅色模块压深底）实测解不出，这不是排版取舍（2026-11-14 实测）。

用法：
    python tools/make_qr_card.py            # 生成并覆盖 gui/assets/
    python tools/make_qr_card.py --verify   # 只做解码核验，不写文件

注意：二维码图案就是链接，别改网格、别做颜色替换之外的"美化"；
深浅两版的模块尺寸都按整数倍绘制，避免模组宽窄不一。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "gui" / "assets"

SEAL = (0xA8, 0x3A, 0x2E, 255)   # 浅色印色
INK = (0x1B, 0x1D, 0x1E)          # 墨色
SLIP = (0xC9, 0xC3, 0xB6)         # 深色纸签底色（2026-11-14 开发者定：压到不刺眼的暖灰）

GRID = (
    "1111111011101010011000111101001111111",
    "1000001011100000101111000100001000001",
    "1011101010110100111001000001001011101",
    "1011101011010010101010110101101011101",
    "1011101000000111101000110101101011101",
    "1000001010011110011111100110101000001",
    "1111111010101010101010101010101111111",
    "0000000001001010011101011011100000000",
    "1100111000010111011101000010100101111",
    "1111010111101001101000111101000110110",
    "1100001001011111001010010001001101010",
    "1100000001110101110001000011001111101",
    "0000011110101101010101000000111100101",
    "1010100110100111100001111111010011110",
    "0110011101100001111010010011000110000",
    "1100100100101011011011010011100111110",
    "1010011010010111010001100010001100101",
    "0110010100101001100010111001100011110",
    "0000101000011111011011010001001110000",
    "0011100101010101010011010010101000110",
    "1111101110101101110111100010011100111",
    "0000110111000111011000111001100010010",
    "1001011010000001011011010001100010100",
    "1101000100101011011101111011100110110",
    "1100111111110111000111100010101100111",
    "1010010110101001110001011101010111010",
    "0000011011111111001001011001110011000",
    "0000100010110101011101011001101110110",
    "1101011010101100110100100011111110100",
    "0000000011100111111001111101100011000",
    "1111111001100001011010010000101011100",
    "1000001010001011111011000001100010111",
    "1011101011010111010101100010111110111",
    "1011101000001001101000111100001100111",
    "1011101001011111111010111111101100100",
    "1000001010010101011111011010000100110",
    "1111111010101100010011100010000110111",
)

N = 37
SCALE = 8          # 每个模组 8px（成品为 2× 显示尺寸）
QUIET = 4          # 深色纸签四周留 4 模组静区


def render(fg: tuple[int, ...], bg: tuple[int, ...] | None, quiet_modules: int) -> Image.Image:
    """按网格画二维码；bg 为 None 时输出透明底。"""
    quiet = quiet_modules * SCALE
    size = N * SCALE + 2 * quiet
    if bg is None:
        im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        color = fg
    else:
        im = Image.new("RGB", (size, size), bg)
        color = fg
    px = im.load()
    for r, row in enumerate(GRID):
        for c, cell in enumerate(row):
            if cell != "1":
                continue
            for y in range(quiet + r * SCALE, quiet + (r + 1) * SCALE):
                for x in range(quiet + c * SCALE, quiet + (c + 1) * SCALE):
                    px[x, y] = color
    return im


def verify(path: Path, composited_on: tuple[int, int, int] | None = None) -> str:
    """用 OpenCV 解码核验；装了 cv2 才做，没装就明说没核。

    透明底的成品不能直接交给 cv2（它会把 alpha 当黑），先按 alpha 铺到指定底色上。
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return "未核验（本机没有 cv2）"
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return "读不出来"
    if img.ndim == 3 and img.shape[2] == 4:
        alpha = (img[:, :, 3:4].astype(np.float32)) / 255.0
        base = np.zeros_like(img[:, :, :3], dtype=np.uint8)
        if composited_on is not None:
            base[:] = composited_on[::-1]
        img = (img[:, :, :3].astype(np.float32) * alpha + base.astype(np.float32) * (1 - alpha)).astype(np.uint8)
    pad = 40
    canvas = np.full((img.shape[0] + pad * 2, img.shape[1] + pad * 2, 3),
                     0xF1 if composited_on is None else composited_on[::-1], dtype=np.uint8)
    canvas[pad:pad + img.shape[0], pad:pad + img.shape[1]] = img
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(canvas)
    return "OK " + text if text else "FAIL：解不出"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="只核验现有成品，不写文件")
    args = ap.parse_args()

    light = ASSETS / "about_qr_light.png"
    dark = ASSETS / "about_qr_dark.png"

    if args.verify:
        print("light:", verify(light, composited_on=(0xF1, 0xEF, 0xE8)))
        print("dark :", verify(dark))
        return 0

    from datetime import date
    backup = ROOT / "backups" / f"qr-card-{date.today():%Y%m%d}"
    backup.mkdir(parents=True, exist_ok=True)
    for old in (light, dark):
        if old.is_file() and not (backup / old.name).exists():
            shutil.copy2(old, backup / old.name)
    print(f"旧资产已备份到 {backup}")

    render(SEAL, None, QUIET).save(light)
    render(INK, SLIP, QUIET).save(dark)
    print(f"写入 {light}  {light.stat().st_size} 字节")
    print(f"写入 {dark}  {dark.stat().st_size} 字节")
    print("light:", verify(light, composited_on=(0xF1, 0xEF, 0xE8)))
    print("dark :", verify(dark))
    print("sha256 light:", sha256(light))
    print("sha256 dark :", sha256(dark))
    return 0


if __name__ == "__main__":
    sys.exit(main())
