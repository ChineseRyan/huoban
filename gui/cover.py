"""程序化封面：九款版式 × 六套配色，1200×1600 PNG。

这是 `backups/_archive/20261025/cover_proto/cover_proto.py`（609 行原型，已验证
54 组确定性 + 对比度全过）搬进 GUI 的版本。搬的时候做了三件事：

1. **Pillow 变成可选依赖**。清洗层与打包层是纯标准库的（AGENTS.md 硬约束），
   GUI 侧才允许用 Pillow。这里照 `fonts.py:47-58` 的范式：探测不到就置 None，
   `make_cover()` 返回 None 让调用方回落"无封面"，绝不在 import 期崩掉整个界面。
2. **可变全局收进 make_cover 的锁里**。原型用模块级 W/H/M/BASE/CUR 传参，
   简洁但不可重入；GUI 的预览与"换一个"都可能在 worker 线程里跑。改成
   `_make_cover_locked()` 全程持一把 RLock —— 同进程串行、确定性不变。
3. **对比度审计从"事后跑脚本"变成"生成时顺手记"**。原型靠 `TEXT_LOG` 事后读；
   这里 `make_cover(..., audit=True)` 直接在同一趟里断言，失败就抛 ValueError，
   因为"封面字看不清"是必须当场发现的缺陷，不该等到跑评审脚本。

对外接口（§8.6）：
    TEMPLATES / THEMES / auto_pick / auto_look / make_cover / make_back_cover / next_choice

版式与配色的对应关系、池子约束都在 §8.4；这里只实现，不重新设计。
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

__all__ = [
    "TEMPLATES",
    "THEMES",
    "BACKGROUNDS",
    "CoverError",
    "auto_pick",
    "auto_look",
    "background_label",
    "background_names",
    "make_back_cover",
    "make_cover",
    "next_background",
    "next_choice",
    "pick_background",
    "available",
]

# --------------------------------------------------------------------------
# Pillow：可选依赖
# --------------------------------------------------------------------------
try:  # pragma: no cover - 取决于运行环境
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except Exception:  # noqa: BLE001
    Image = None
    ImageDraw = None
    ImageFilter = None
    ImageFont = None

# 尺寸：3:4 海报，1200×1600 是 §8.5 定的量级（单张 32–101KB、约 141ms）
W_DEFAULT, H_DEFAULT = 1200, 1600

# 页边距（占位按比例给，支持非默认画布）
_M_RATIO = 112 / 1200

# 字体：与 EPUB 内嵌的是同一套（思源宋/黑），封面和正文长得一样。
#
# 按「文件名」排优先级、按「目录」挨个找，两条轴分开：
#   - 找哪儿：项目自带 fonts/ 排最前，然后是系统字体目录（见 _font_dirs）。
#     自带的那份是发行包的 _internal/fonts/，一定跟包走 —— 用户没装字体也能用。
#   - 先找谁：可变字体（VF）在前，静态字体在后。**这不是排版偏好，是字重正确性**：
#     封面标题用 700、作者用 500，只有带 wght 轴的可变字体画得出真正的粗体；
#     静态字体（比如自带那份 NotoSerifCJKsc-Regular.otf）拿到 700 会被 Pillow
#     直接忽略，标题会明显变细（实测同一句话墨量 5730 vs 可变字体 8950）。
#     所以系统里装了 VF 时优先用系统的，和设计稿逐像素一致；没装才退回自带的
#     静态宋体 —— 字形族正确、粗细偏细，但不会崩、也不会掉成宋体以外的样子。
#
# 顺序里为什么还留着 simsun/msyh：那是最后的兜底。前两档都没了（精简过的 Windows
# 或没带 fonts/ 的源码树），好歹还能出一个封面，而不是直接报错。
_FONT_FALLBACKS = {
    "serif": (
        "NotoSerifSC-VF.ttf",
        "NotoSerifSC-Regular.otf",
        "NotoSerifSC-Regular.ttf",
        "SourceHanSerifSC-VF.ttf",
        "SourceHanSerifSC-Regular.otf",
        "NotoSerifCJKsc-Regular.otf",
        "simsun.ttc",
    ),
    "sans": (
        "NotoSansSC-VF.ttf",
        "NotoSansSC-Regular.otf",
        "NotoSansSC-Regular.ttf",
        "SourceHanSansSC-VF.ttf",
        "SourceHanSansSC-Regular.otf",
        "NotoSansCJKsc-Regular.otf",
        "msyh.ttc",
    ),
}


class CoverError(ValueError):
    """封面生成的参数或绘制缺陷。§8.6：非法 template/theme 直接抛，不做修补。"""


# --------------------------------------------------------------------------
# 纸质底纹（自带的 9 张纹理；入库形态就是下面 _TEXTURE_WORK 那个尺寸，
# 9 张合计 19KB。原始 1086×1448 在两处有备份，要重新导出随时可以）
# --------------------------------------------------------------------------
# 幅度按「sRGB 色阶步数」算，不是按相对亮度 sd —— 松墨底色 #15181D 的亮度只有
# 0.9%，同样 9% 的相对波动换算成 8-bit 色阶不足 1 级，深底上肉眼完全看不出来
# （v1 用相对 sd 就是这样：26 组合全"合格"，深底一片死黑）。
# ±8 是目视标定的结果：浅底颗粒清晰、大字更实；深底能看到纸的质感但不脏。
# 余量：六套配色下 ±8 色阶的最坏文字对比度仍 ≥ 11:1（要求 4.5:1）。
TEXTURE_AMP = 8

# 版式 → 纹理族。浅底用 *_light，深底用 *_dark。
# 不按"哪张最好看"挑，按"这张纸该配这款版式"挑：宣纸配书页感的章/框/碑/疏，
# 麻纸配极简的简，墨洇配沉闷的碑/夜。
# 注意「对」原本想用 cloth（蓝印花布），但 cloth_light 质检不合格已剔除
# （浅底文字带 p05=63，深色字压上去不到 4.5:1），所以浅底回落到 grain。
_TEXTURE_MAP = {
    "章": "paper", "简": "grain", "框": "paper", "对": "grain", "栏": "metal",
    "碑": "ink", "印": "cloth", "夜": "ink", "疏": "paper",
}

# 质检不合格、不入库的两张：cloth_light（浅底文字带 p05=63，深色字压上去不够
# 4.5:1）、light_03（景物压在上方 60%，与"大字落上方留白"冲突）。
# 族 B 那 10 张「万能封面预设」不是纹理，是整幅背景画，这里不接。
#
# 某张素材缺席时的回落顺序（按"纸感接近"排）：cloth 缺了就找 grain，
# 再缺找 paper，最后找 ink。实测触发过一次：cloth_light 被剔除而「印」还指着
# cloth，浅底「印」会一张纹理都拿不到。
_TEXTURE_FALLBACK = ("grain", "paper", "ink", "metal")
_TEXTURE_DIR = Path(__file__).resolve().parent / "assets" / "textures"
_TEXTURES = ({p.stem: p for p in sorted(_TEXTURE_DIR.glob("*.png"))}
             if Image is not None and _TEXTURE_DIR.is_dir() else {})
_TEXTURE_CACHE: dict[tuple[str, int, int], object] = {}
_TEXTURE_LOCK = threading.Lock()


# 纹理在入库图（1086×1448）之上再做一次「低分辨率 + 3 级量化 + 双线性上采样」，
# 这是为了 PNG 体积 —— 平滑渐变让 PNG 几乎压不动，实测同一张封面：
#   直接铺全分辨率纹理 1035 KB / 不铺纹理 66 KB（15 倍）
#   降到 600×800 再放大 992 KB，300×400 → 684 KB，150×200 → 438 KB（还是嫌大）
#   150×200 **量化成 3 级**再 BILINEAR 上采样 → 整张平均 97 KB，且单张只要 130ms
# 为什么可以这么狠地降：纹理只是「纸的质感」，它是低频的；高频细节由 grain()
# 程序化噪声负责（每张封面本来就在画），素材不需要承担高频。目视对比条上
# 150×200/3 级与原始全分辨率在 1200×1600 上几乎分不出，后者只是更"脏"。
# 为什么不用 JPEG：解码必须跨机器逐字节一致，JPEG 依赖 libjpeg 版本会让成品
# 哈希漂（§8.5 确定性是硬要求）。
_TEXTURE_WORK = (150, 200)   # 工作尺寸（按 1200×1600 画布定标，其它尺寸等比）
_TEXTURE_LEVELS = 3          # 量化级数


def _pick_texture(template: str, dark: bool) -> str | None:
    """版式 → 纹理名。族名对应的那张不在库里就按 _TEXTURE_FALLBACK 回落。

    为什么需要回落：cloth_light 质检不合格被剔除了，但「印」还指着 cloth ——
    浅底的「印」本来会一张纹理都拿不到（实测：`_pick_texture("印", False)` 返回
    None，那张封面就是纯色块）。回落而不是改映射表，是因为深底 cloth_dark 是好的，
    一张表同时服务明暗两侧，缺哪侧就在哪侧回落。
    """
    fam = _TEXTURE_MAP.get(template)
    if not fam:
        return None
    suffix = "dark" if dark else "light"
    name = f"{fam}_{suffix}"
    if name in _TEXTURES:
        return name
    for alt in _TEXTURE_FALLBACK:
        alt_name = f"{alt}_{suffix}"
        if alt_name in _TEXTURES:
            return alt_name
    return None


def _load_texture(name: str, w: int, h: int):
    """按目标尺寸缩放后缓存。封面一张只画一次，但「换一个」会连点，值这个钱。

    三步：缩到工作尺寸 → 量化到 _TEXTURE_LEVELS 级 → 双线性放大到画布。
    量化步在放大之前做，放大后是「大片平坦区 + 少量过渡带」，PNG 才压得下去；
    先放大再量化会得到满屏抖动点，PNG 反而压不动（实测 487 KB vs 97 KB）。
    """
    key = (name, w, h)
    hit = _TEXTURE_CACHE.get(key)
    if hit is not None:
        return hit
    path = _TEXTURES.get(name)
    if path is None:
        return None
    with _TEXTURE_LOCK:
        hit = _TEXTURE_CACHE.get(key)
        if hit is not None:
            return hit
        try:
            im = Image.open(path).convert("L")
            # 工作尺寸按画布宽度等比缩放 —— 支持非默认画布（如 600×800 的预览图）
            wk_w = max(16, int(round(_TEXTURE_WORK[0] * w / 1200.0)))
            wk_h = max(16, int(round(_TEXTURE_WORK[1] * h / 1600.0)))
            im = im.resize((wk_w, wk_h), Image.LANCZOS)
            step = 255 // (_TEXTURE_LEVELS - 1)
            im = Image.eval(im, lambda v: int(round(v / 255.0 * (_TEXTURE_LEVELS - 1))) * step)
            im = im.resize((w, h), Image.BILINEAR)
        except Exception:  # noqa: BLE001 - 素材坏了不该让整张封面失败
            return None
        _TEXTURE_CACHE[key] = im
        return im


# --------------------------------------------------------------------------
# 六套配色（§8.3，对比度已实测；改色必须重跑 audit）
# --------------------------------------------------------------------------
THEME_TABLE: dict[str, dict] = {
    # mu（次要信息色）按 WCAG 4.5:1 反推校准过，不是凭感觉取的灰
    "xuanzhi": dict(name="宣纸", bg="#F1EFE8", fg="#1B1D1E", ac="#A83A2E", mu="#6B675C", dark=False),
    "songmo": dict(name="松墨", bg="#15181D", fg="#EDEAE2", ac="#C24A38", mu="#8A93A2", dark=True),
    "qingci": dict(name="青瓷", bg="#E7EDE8", fg="#13302C", ac="#2C6C5F", mu="#4F6E66", dark=False),
    "dianlan": dict(name="靛蓝", bg="#182338", fg="#E9EEF7", ac="#C8A02E", mu="#8CA0BC", dark=True),
    # 后两套浅色是补的：只有两套浅色时 8 本书实测出现 5 张青瓷，书架糊成一片
    "zheshi": dict(name="赭石", bg="#EDE2D0", fg="#251C14", ac="#8E4B26", mu="#6B5F4E", dark=False),
    "daiqing": dict(name="黛青", bg="#E4E8EE", fg="#1B2430", ac="#35577D", mu="#5A6674", dark=False),
}
THEMES = ["xuanzhi", "songmo", "qingci", "dianlan", "zheshi", "daiqing"]

TEMPLATES = ["章", "简", "框", "对", "栏", "碑", "印", "夜", "疏"]

# §8.4：版式 → 允许的配色。纸面五款只配浅色（`对` 额外允许松墨）；
# `夜` 只配深色；`简` 任意；`印` 底色就是强调色，凭色相挑，不参与自动池。
ALLOWED: dict[str, list[str]] = {
    "章": ["xuanzhi", "qingci", "zheshi", "daiqing"],
    "简": list(THEMES),
    "框": ["xuanzhi", "qingci", "zheshi", "daiqing"],
    "对": ["xuanzhi", "qingci", "zheshi", "songmo"],   # 排除靛蓝：强调色是金色，铺成一整块太张扬
    "栏": ["xuanzhi", "qingci", "zheshi", "daiqing"],
    "碑": ["xuanzhi", "qingci", "zheshi", "daiqing"],
    "印": ["xuanzhi", "qingci", "zheshi", "daiqing"],
    "夜": ["songmo", "dianlan"],
    "疏": ["xuanzhi", "qingci", "zheshi", "daiqing"],
}

# §8.4：按书名长度筛版式。两条池子约束写在这里：
#   `夜` 只进 SHORT/MID（十几个字的大字距横排会糊成一片）
#   `章` 不进 LONG（学术单块版式折成三行就散了）
#   `印` 一个池都不进：存在感最强，只作手选的「签名款」
POOL_SHORT = ["章", "对", "框", "碑", "夜"]
POOL_MID = ["章", "简", "框", "碑", "疏", "夜", "对"]
POOL_LONG = ["简", "栏", "疏"]


# --------------------------------------------------------------------------
# 闸门：可用性
# --------------------------------------------------------------------------
def available() -> bool:
    """封面能不能画（Pillow 在不在）。界面据此决定「自动生成」按钮是否可点。"""
    return Image is not None


# --------------------------------------------------------------------------
# 自动挑选（§8.4：种子固定，同一本书永远同一个样子）
# --------------------------------------------------------------------------
def _pool_for(title: str) -> list[str]:
    n = len(title.replace(" ", ""))
    if n <= 5:
        return POOL_SHORT
    if n <= 9:
        return POOL_MID
    return POOL_LONG


def auto_pick(title: str) -> tuple[str, str]:
    """书名 → (template, theme)。纯函数、无 random、同一书名恒定。"""
    clean = (title or "").strip()
    if not clean:
        # 没有书名时给一个确定的兜底，而不是抛异常：封面是可选步骤，
        # 空书名不该阻断整个打包流程。
        clean = "未命名"
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    pool = _pool_for(clean)
    template = pool[int(digest[:8], 16) % len(pool)]
    allowed = ALLOWED[template]
    # 次 8 位定配色。乘一个与长度互质的奇数再取模，避免"同一本书
    # 换个书名长度就系统性偏向某个配色"的伪相关。
    theme = allowed[(int(digest[8:16], 16) * 7) % len(allowed)]
    return template, theme


def next_choice(title: str, template: str, theme: str, step: int = 1) -> tuple[str, str]:
    """「换一个」：先在当前版式的允许配色内循环，配色轮完再换池内下一款版式。

    §8.6 要求仍然确定性 —— 同一个起点连点两次拿到同一个下一个，所以这里
    不用 random，只做确定性的环形推进。
    """
    if template not in ALLOWED:
        return auto_pick(title)
    allowed = ALLOWED[template]
    if theme not in allowed:
        idx = -1
    else:
        idx = allowed.index(theme)
    idx = (idx + step) % len(allowed)
    if idx == 0 and step > 0:
        # 配色绕回起点 → 说明这款版式的配色轮完了，换池内下一款版式
        pool = _pool_for((title or "未命名").replace(" ", ""))
        if template in pool:
            t = (pool.index(template) + step) % len(pool)
        else:
            t = 0
        template = pool[t]
        allowed = ALLOWED[template]
        idx = 0
    return template, allowed[idx]


# --------------------------------------------------------------------------
# 绘制上下文：原型的模块级全局改成"每次调用重建的一组对象"
# --------------------------------------------------------------------------
class _Canvas:
    """一次绘制用到的全部可变状态。

    原型用模块级 `W/H/M/BASE/CUR` + `global` 语句，一行 draw_h 要读五个全局。
    搬进 GUI 后同进程可能有多个 worker（预览、换一个、打包），全局会被踩。
    这里收成一个对象，模板函数签名改成 `(spec, th, seed, cv)`，语义不变。
    """

    def __init__(self, w: int, h: int, theme: dict):
        self.W = w
        self.H = h
        self.M = int(round(w * _M_RATIO))
        # 设计尺度：所有"看起来该多大"的绝对像素（字号、留白、线宽、印章边长）
        # 都写在 1200×1600 这套设计坐标里，画的时候乘这个系数。
        # s=1 时恒等 —— 正式打包走 1200×1600，出图逐字节不变（有基线守着）。
        # 没有它，300×400 的缩略图会得到"150px 的字 + 56px 的框"，
        # 也就是一张与大图**排版不同**的封面（实测：书名在小图上折成两行）。
        self.s = w / float(W_DEFAULT)
        self.theme = theme
        self.base = _rgb(theme["bg"])
        self.dark = bool(theme.get("dark"))
        # (模板名, 字号, 文字色, 当时的底色)。审计读它，不读成品像素 ——
        # 在成品图上按众数猜底色会在「对」上量到空白区，报出 1.04:1 的假警报。
        self.text_log: list[tuple[str, int, tuple, tuple]] = []
        self.cur = ""
        # 底纹幅度（sRGB 色阶）。0 表示这一张不铺底纹。
        # 用色阶步数而不是"相对亮度 sd"：松墨底色亮度只有 0.9%，
        # 9% 的相对 sd 换算成绝对色阶不到 1 级 —— 深底上等于没铺（实测深底一片死黑）。
        self.texture_amp = TEXTURE_AMP

    def S(self, v: float) -> int:
        """设计坐标 → 本画布像素。字号/间距/线宽都要过这里，2 是常识下限。"""
        return max(1, int(round(v * self.s)))

    def Sf(self, v: float) -> float:
        """同上，但留小数（给 line-height 之类的等比量用）。"""
        return max(1.0, v * self.s)

    # ---- 颜色 ----
    def C(self, h, a: int = 255):
        """直接画在画布上的颜色：先按当前底色预混合成不透明色。

        关键点（§8.5）：PIL 的 ImageDraw 对 RGBA 画布传半透明色是「替换像素」
        而不是「叠加」，存 PNG 转 RGB 时会按黑色底压平，细线和淡色块会黑成
        一大块 —— 所以一律自己混合，画布始终保持 RGB。
        """
        r, g, b = _rgb(h) if isinstance(h, str) else h
        if a >= 255:
            return (r, g, b)
        t = a / 255.0
        base = self.base
        return (
            int(round(r * t + base[0] * (1 - t))),
            int(round(g * t + base[1] * (1 - t))),
            int(round(b * t + base[2] * (1 - t))),
        )

    def C4(self, h, a: int = 255):
        """给 RGBA 图层用的真透明色（只在不透明的实心块上小面积用）。"""
        r, g, b = _rgb(h) if isinstance(h, str) else h
        return (r, g, b, a)

    def set_base(self, rgb_tuple) -> None:
        """切底色。模板里「书名在纸上、作者压在色块上」需要中途换底。"""
        self.base = tuple(rgb_tuple[:3])

    def new(self):
        img = Image.new("RGB", (self.W, self.H), self.base)
        # 铺底纹放在这里：六款版式（章/简/框/栏/碑/疏）都从这里起手，
        # 一处即覆盖。剩下三款（对/夜/印）自己建画布，各自在合适的位置调
        # paint_base()。cur 已经由模板函数设置好，所以能查到对应纹理。
        self.paint_base(img)
        return img, ImageDraw.Draw(img)

    # ---- 底纹 ----
    def texture_source(self):
        """这一款版式该用的灰度纹理（已缩放到画布尺寸），没有就 None。"""
        name = _pick_texture(self.cur, self.dark)
        if not name:
            return None
        return _load_texture(name, self.W, self.H)

    def tint_texture(self, base_rgb, src=None):
        """灰度纹理 → 按 base_rgb 上色的 RGB 图。

        用的映射是 base + (g-128)*amp/128，逐通道 point() 后 merge。
        全程纯 PIL，不走 numpy —— PyInstaller 的 spec 里 numpy 在 excludes，
        一旦用了打包版封面就崩（开发期还偏偏是好的）。
        """
        src = self.texture_source() if src is None else src
        if src is None:
            return None
        amp = self.texture_amp
        if amp <= 0:
            return None
        s = amp / 128.0
        chans = [
            src.point(lambda v, c=c: max(0, min(255, int(round(c + (v - 128) * s)))))
            for c in tuple(base_rgb)[:3]
        ]
        return Image.merge("RGB", chans)

    def paint_base(self, img, base_rgb=None, region=None) -> bool:
        """把纸质底纹铺到 img 上（原地），返回是否真的铺了纹理。

        必须在「底色定下来、还没画内容」的位置调。**没有纹理时这里会填实色**
        —— 调用方可以无条件依赖它把这块区域变成 base_rgb；否则「对」的底部
        深色块会因为没有纹理而整块消失（实测过一次：canvas 全白，色块不见了）。

        为什么不直接塞进 `new()` 了事：九款版式里有三款不调 `new()` ——
        「对」自己建 RGBA 画布并中途换底两次，「夜」自己建 RGB 再叠竖向渐变，
        「印」整张就是强调色底。只改 `new()` 会漏掉这三款（实测「夜」的画面
        一丝不变，连调试字段都没被赋值）。所以底纹做成独立一步，谁建画布谁调。

        region=(x0, y0, x1, y1) 时只处理这一块 —— 「对」的上下两块底色不同，
        要分别按各自的底色上色。

        region 越界不报错：PIL 的 crop/paste 会自己裁到画布内（实测 region
        (-50,-50,100,100) 只铺 (0,0,100,100)、(1100,1500,1300,1700) 只铺
        (1100,1500,1200,1600)）。这是刻意的 —— 版式里 bh 是算出来的，
        画布尺寸又可以是 600×800 的预览，让越界静默裁剪比让整张封面失败好。
        """
        base = tuple(base_rgb if base_rgb is not None else self.base)[:3]
        tinted = self.tint_texture(base)
        if tinted is None:
            # 素材缺失/幅度为 0：填实色，保证这块区域至少是对的底色
            if region is None:
                img.paste(Image.new("RGB", img.size, base), (0, 0))
            else:
                x0, y0, x1, y1 = region
                img.paste(Image.new("RGB", (x1 - x0, y1 - y0), base), (x0, y0))
            return False
        if region is None:
            img.paste(tinted, (0, 0))
        else:
            x0, y0, x1, y1 = region
            img.paste(tinted.crop((x0, y0, x1, y1)), (x0, y0))
        return True


# --------------------------------------------------------------------------
# 颜色工具（模块级纯函数）
# --------------------------------------------------------------------------
def _rgb(h):
    if isinstance(h, tuple):
        return tuple(h[:3])
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _mix8(a, b, t: float):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _lum(c) -> float:
    def f(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def contrast(a, b) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _ensure(fg, bg, target: float = 4.5):
    """文字色 fg 在底色 bg 上对比不足时，把 fg 往黑或白推，直到达标。

    填色块类模板必须过这一关：强调色做底 + 纸白做字，遇到黄/青主题会糊成一片。
    """
    fg = tuple(fg[:3])
    if contrast(fg, bg) >= target:
        return fg
    toward = (255, 255, 255) if _lum(bg) < 0.35 else (18, 18, 18)
    for i in range(1, 101):
        t = i / 100.0
        c = tuple(int(round(fg[j] + (toward[j] - fg[j]) * t)) for j in range(3))
        if contrast(c, bg) >= target:
            return c
    return toward


# --------------------------------------------------------------------------
# 字体
# --------------------------------------------------------------------------
_FONT_CACHE: dict[tuple, object] = {}
_FONT_LOCK = threading.Lock()
_FONT_PATHS: dict[str, str | None] = {}


def _font_dirs() -> list[Path]:
    """可能放中文字体的目录（按优先级）。只返回真实存在的。

    第一项是项目自带的 fonts/：开发期是「项目根/fonts」，冻结后 PyInstaller 把它
    放在 _MEIPASS/fonts —— 两种情形下 `Path(__file__).parents[1]` 都指得到，
    因为 gui/ 与 fonts/ 在包内是平级的两层（见 build/gui.spec 的 datas）。

    这就是"用户没装字体也能出封面"的那一条：发行包自带字库，探测时它排在系统
    目录前面。系统目录只是补充 —— 系统里那份可变字体比自带的静态字体字重更全。
    """
    dirs: list[Path] = [
        # 项目根/fonts（源码树）或 _MEIPASS/fonts（打包后）
        Path(__file__).resolve().parents[1] / "fonts",
    ]
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # Windows 10 起"为当前用户安装"的字体落在这里，不在 C:\Windows\Fonts。
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    home = Path.home()
    dirs.extend([
        home / ".fonts",
        home / ".local" / "share" / "fonts",
        home / "Library" / "Fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ])
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        try:
            key = str(d.resolve()).lower()
        except OSError:  # pragma: no cover - 路径异常时退回原样
            key = str(d).lower()
        if key in seen or not d.is_dir():
            continue
        seen.add(key)
        out.append(d)
    return out


def _font_path(kind: str) -> str | None:
    """按 `_FONT_FALLBACKS[kind]` 的顺序，在 `_font_dirs()` 里找第一个存在的。

    循环顺序是「先按目录、再按文件名」还是反过来，结果不一样：这里按文件名在
    最外层，所以"系统里装了可变字体"会赢过"包里自带的静态字体"—— 正是想要的
    优先级（可变字体画得出真正的粗体，见 _FONT_FALLBACKS 上面的说明）。
    """
    if kind in _FONT_PATHS:
        return _FONT_PATHS[kind]
    found = _search_font(kind)
    if found is None and kind != "serif":
        # 退而求其次：宁可整张封面都用宋体，也不要一个字都画不出来。
        # sans 只用在副标题、作者、「著」这类小字上，换成宋体只是观感差异；
        # 而 F() 一旦拿不到字体就抛 CoverError，整张封面会降级成纯文字封面。
        found = _search_font("serif")
    _FONT_PATHS[kind] = found
    return found


def _search_font(kind: str) -> str | None:
    """按 `_FONT_FALLBACKS[kind]` 的顺序在各字体目录里找第一个存在的文件。"""
    dirs = _font_dirs()
    for name in _FONT_FALLBACKS[kind]:
        for directory in dirs:
            cand = directory / name
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:  # pragma: no cover - 权限不足的目录直接跳过
                continue
    return None


def F(kind: str, size: float, wght=None):
    """取字体。kind 是 "serif"/"sans"（原型的路径参数换成角色名）。"""
    path = _font_path(kind)
    if path is None:
        raise CoverError("找不到可用的中文字体（思源宋/黑 或 系统宋体/黑体）")
    key = (path, int(size), wght)
    f = _FONT_CACHE.get(key)
    if f is not None:
        return f
    with _FONT_LOCK:
        f = _FONT_CACHE.get(key)
        if f is not None:
            return f
        f = ImageFont.truetype(path, int(size))
        if wght is not None:
            try:
                f.set_variation_by_axes([wght])
            except Exception:  # noqa: BLE001 - 静态字库没有变体轴
                pass
        _FONT_CACHE[key] = f
    return f


# --------------------------------------------------------------------------
# 排版工具
# --------------------------------------------------------------------------
# 中文禁则：这些字符不许出现在行首（出现了就往上一行挤）
CLOSER = "。，、；：？！）』」》·—…"

# 英文/数字连写的词：折行时不能从中间断开（"shot_book" 断成 "shot_b/ook"）。
# 允许内部出现 ' - . 这些词内符号，它们不构成断点。
_WORD_CHARS = set("abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "0123456789_'-.")


def _tokens(text: str) -> list[str]:
    """把文本切成"不可再分的折行单元"。

    中文按单字切；连续的 ASCII 词字符算一个整体。这样 `书E vol.2` 会切成
    `道/诡/异/仙/ /vol.2`，折行只可能落在字间与空格处 —— "vol.2" 不会被劈开。
    单个词比整行还宽时（极端情况，比如 60 个字母连写的书名），这一整词会自己
    独占一行并溢出，由 `fit()` 继续降字号兜住，不在折行层丢掉。
    """
    out: list[str] = []
    buf = ""
    for ch in text:
        if ch in _WORD_CHARS:
            buf += ch
            continue
        if buf:
            out.append(buf)
            buf = ""
        out.append(ch)
    if buf:
        out.append(buf)
    return out


def wrap(text: str, f, maxw: float, tr: float = 0):
    """按像素宽度折行：英文词整体不断开，中文带禁则，末行单字回收。"""
    lines, cur, curw = [], "", 0.0
    for token in _tokens(text):
        if cur:
            w_join = f.getlength(token) + tr * max(0, len(token) - 1)
        else:
            w_join = tw(f, token, tr)
        if cur and curw + w_join > maxw:
            if len(token) == 1 and token in CLOSER:
                # 中文禁则第一档：收尾标点允许"悬挂"到边界外半个字宽以内
                if curw + w_join <= maxw + f.size * 0.55:
                    cur += token
                    lines.append(cur)
                    cur, curw = "", 0.0
                    continue
                # 第二档：悬挂也放不下，就"推一个字下来"给标点让位 ——
                # 直接另起一行会让「，」落在行首，这是中文排版里最扎眼的错。
                if len(cur) > 1 and cur[-1] not in _WORD_CHARS:
                    moved = cur[-1]
                    lines.append(cur[:-1])
                    cur = moved + token
                    curw = tw(f, cur, tr)
                    continue
            lines.append(cur)
            cur, curw = token, tw(f, token, tr)
        else:
            if cur:
                curw += w_join
            else:
                curw = tw(f, token, tr)
            cur += token
    if cur:
        lines.append(cur)
    # 末行只剩一个孤字就收进上一行（除非那是英文词的一部分，收进去会断词）
    if len(lines) > 1 and len(lines[-1]) == 1 and lines[-1] not in _WORD_CHARS:
        lines[-2] += lines[-1][0]
        lines[-1] = lines[-1][1:]
        lines = [ln for ln in lines if ln]
    return lines


def tw(f, s: str, tr: float = 0) -> float:
    return f.getlength(s) + tr * max(0, len(s) - 1)


def fit(text: str, kind: str, wght, maxw: float, maxh: float, size0: float,
        min0: float = 54, tr_ratio: float = 0.0, lead: float = 1.06):
    """逐档 0.94 降字号直到放得下。永不丢行（§8.5 硬约束）。"""
    size = size0
    while size >= min0:
        f = F(kind, size, wght)
        tr = size * tr_ratio
        ls = wrap(text, f, maxw, tr)
        if len(ls) * size * lead <= maxh and all(tw(f, l, tr) <= maxw + 1 for l in ls):
            return int(size), ls, tr
        size = int(size * 0.94)
    f = F(kind, min0, wght)
    return int(min0), wrap(text, f, maxw), min0 * tr_ratio


def fit_v(text: str, kind: str, wght, maxh: float, size0: float,
          min0: float = 36, step: float = 1.05) -> int:
    size = size0
    while size >= min0:
        if len(text) * size * step <= maxh:
            return int(size)
        size = int(size * 0.94)
    return int(min0)


def draw_h(d, cv: _Canvas, x, y, s, f, fill, tr: float = 0,
           align: str = "left", deco: bool = False):
    """横排一行。deco=True 表示这是装饰性重影，不进对比度审计。"""
    w = tw(f, s, tr)
    if align == "center":
        x -= w / 2
    elif align == "right":
        x -= w
    if not deco and isinstance(fill, tuple) and len(fill) == 3:
        cv.text_log.append((cv.cur, int(f.size), tuple(fill), tuple(cv.base)))
    if tr:
        cx = x
        for ch in s:
            d.text((cx, y), ch, font=f, fill=fill)
            cx += f.getlength(ch) + tr
    else:
        d.text((x, y), s, font=f, fill=fill)
    return w


def draw_v(d, cv: _Canvas, x, y, text: str, f, fill, step_ratio: float = 1.05):
    """竖排一列，返回总高。"""
    if isinstance(fill, tuple) and len(fill) == 3:
        cv.text_log.append((cv.cur, int(f.size), tuple(fill), tuple(cv.base)))
    st = f.size * step_ratio
    for i, ch in enumerate(text):
        d.text((x - f.getlength(ch) / 2, y + i * st), ch, font=f, fill=fill)
    return len(text) * st


# --------------------------------------------------------------------------
# 底纹与装饰
# --------------------------------------------------------------------------
def _over(img, layer):
    """把 RGBA 图层叠加到 RGB 画布上（原地）。"""
    buf = img.convert("RGBA")
    buf.alpha_composite(layer)
    img.paste(buf.convert("RGB"), (0, 0))


def grain(img, cv: _Canvas, seed: int, n: int = 26000, a: int = 7):
    """噪点：由 seed 派生、点数按面积定 —— 不引 random 状态，保证字节可复现。

    点数要跟着设计尺度走：n 是在 1200×1600 上数的，画布缩到四分之一还撒同样
    多点，噪点密度就变 16 倍 —— 缩略图会脏成一片，与大图不是同一个观感。
    """
    import random

    n = max(1, int(round(n * cv.s * cv.s)))
    rnd = random.Random(seed)
    px = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dd = ImageDraw.Draw(px)
    base = cv.C4(cv.theme["fg"], a) if not cv.dark else cv.C4(cv.theme["bg"], a)
    w, h = img.size
    for _ in range(n):
        dd.point((rnd.randrange(w), rnd.randrange(h)), fill=base)
    _over(img, px)


def vignette(img, cv: _Canvas, strength: int = 26):
    w, h = img.size
    m = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(m)
    md.ellipse([-w * 0.25, -h * 0.2, w * 1.25, h * 1.2], fill=255)
    # 模糊半径也按设计尺度走，否则小画布上这 160px 的模糊会把整张图抹平
    m = m.filter(ImageFilter.GaussianBlur(max(1.0, 160 * cv.s)))
    veil = Image.new("RGBA", (w, h), cv.C4(cv.theme["fg"], strength))
    img.paste(Image.composite(veil.convert("RGB"), img, Image.eval(m, lambda v: 255 - v)), (0, 0))


def stripes(img, cv: _Canvas, gap: int = 44, w: int = 1, a: int = 14,
            x0=None, x1=None, y0=0, y1=None):
    d = ImageDraw.Draw(img)
    W, H, M = cv.W, cv.H, cv.M
    x0 = M if x0 is None else x0
    x1 = W - M if x1 is None else x1
    y1 = H - M if y1 is None else y1
    col = cv.C(cv.theme["fg"], a) if not cv.dark else cv.C(cv.theme["bg"], a)
    w = cv.S(w)
    x = x0
    while x <= x1:
        d.line([x, y0, x, y1], fill=col, width=w)
        x += cv.S(gap)


def hair(d, cv: _Canvas, y, a: int = 90, x0=None, x1=None, w: int = 2):
    x0 = cv.M if x0 is None else x0
    x1 = cv.W - cv.M if x1 is None else x1
    d.line([x0, y, x1, y], fill=cv.C(cv.theme["fg"], a), width=cv.S(w))


def footer(d, cv: _Canvas, spec: dict, y):
    M = cv.M
    f = F("sans", cv.S(27), 400)
    txt = "  ·  ".join([s for s in [spec.get("author"), spec.get("source")] if s])
    if txt:
        draw_h(d, cv, M, y, txt, f, cv.C(cv.theme["mu"]), tr=cv.Sf(1.2))


# --------------------------------------------------------------------------
# 九款版式（§8.2）
# --------------------------------------------------------------------------
def t_zhang(spec, th, seed, cv):
    """章 · 学术横排：细线分区 + 大字书名"""
    cv.cur = "章"
    img, d = cv.new()
    grain(img, cv, seed, 9000, 6)
    W, H, M = cv.W, cv.H, cv.M
    size, ls, tr = fit(spec["title"], "serif", 700, W - 2 * M, H * 0.34,
                       cv.S(190), min0=cv.S(54), tr_ratio=0.035)
    top = H * 0.235
    y = top
    for line in ls:
        draw_h(d, cv, M, y, line, F("serif", size, 700), cv.C(th["fg"]), tr=tr)
        y += size * 1.14
    hair(d, cv, y + cv.S(26), a=70, w=2)
    hair(d, cv, y + cv.S(34), a=30, x1=W - M, w=1)
    sub = spec.get("subtitle") or ""
    if sub:
        draw_h(d, cv, M, y + cv.S(66), sub, F("sans", cv.S(34), 400),
               cv.C(th["mu"]), tr=cv.Sf(6))
    footer(d, cv, spec, H - M - cv.S(30))
    return img


def t_jian(spec, th, seed, cv):
    """简 · 单列竖排居中：一根细线 + 大字竖排，留白极大"""
    cv.cur = "简"
    img, d = cv.new()
    grain(img, cv, seed, 14000, 5)
    W, H = cv.W, cv.H
    title = spec["title"].replace(" ", "")
    cols = 1 if len(title) <= 6 else (2 if len(title) <= 13 else 3)
    per = -(-len(title) // cols)
    chunks = [title[i * per:(i + 1) * per] for i in range(cols)]
    chunks = [c for c in chunks if c]
    cols = len(chunks)
    maxh = H * 0.62
    size = fit_v(max(chunks, key=len), "serif", 500, maxh, cv.S(168),
                 min0=cv.S(64), step=1.12)
    f = F("serif", size, 500)
    step = size * 1.12
    colw = size * 1.34
    x0 = W * 0.5 + (cols - 1) * colw / 2
    top = (H - max(len(c) for c in chunks) * step) / 2
    # 竖排必须右起：第 0 块在最右，往左排。写成 x0 + ci*colw 会让两列以上书名
    # 被读成「后半段 + 前半段」（《书Y》读成「险屋的夏天我有一座冒」）
    for ci, c in enumerate(chunks):
        x = x0 - ci * colw
        draw_v(d, cv, x, top, c, f, cv.C(th["fg"]), 1.12)
    leftx = x0 - (cols - 1) * colw
    d.line([leftx - size * 1.05, top, leftx - size * 1.05, top + len(chunks[-1]) * step * 0.42],
           fill=cv.C(th["ac"], 220), width=cv.S(3))
    if spec.get("author"):
        fa = F("sans", cv.S(30), 400)
        # 作者列放在标题块左侧（右起读序里作者在书名之后）
        draw_v(d, cv, leftx - size * 2.32, top + maxh * 0.16, "著  " + spec["author"], fa,
               cv.C(th["mu"]), 1.30)
    return img


def t_kuang(spec, th, seed, cv):
    """框 · 民国出版风：双线内框 + 题签居中 + 底部横带"""
    cv.cur = "框"
    img, d = cv.new()
    grain(img, cv, seed, 22000, 6)
    W, H = cv.W, cv.H
    o = cv.S(56)
    inset = cv.S(12)
    d.rectangle([o, o, W - o, H - o], outline=cv.C(th["fg"], 130), width=cv.S(3))
    d.rectangle([o + inset, o + inset, W - o - inset, H - o - inset],
                outline=cv.C(th["fg"], 60), width=1)
    band_h = int(H * 0.30)
    d.rectangle([o + inset, o + inset, W - o - inset, o + inset + band_h],
                fill=cv.C(th["fg"], 26 if not cv.dark else 40))
    size, ls, tr = fit(spec["title"], "serif", 600, W - 2 * (o + cv.S(60)), band_h * 0.80,
                       cv.S(130), min0=cv.S(52), tr_ratio=0.06)
    y = o + inset + band_h * 0.5 - len(ls) * size * 1.12 / 2
    for line in ls:
        draw_h(d, cv, W / 2, y, line, F("serif", size, 600), cv.C(th["fg"]), tr=tr, align="center")
        y += size * 1.12
    hy = o + inset + band_h
    d.line([o + inset, hy, W - o - inset, hy], fill=cv.C(th["ac"], 210), width=cv.S(4))
    mid = spec.get("subtitle") or ""
    if mid:
        draw_h(d, cv, W / 2, hy + cv.S(66), mid, F("sans", cv.S(32), 400),
               cv.C(th["mu"]), tr=cv.Sf(8), align="center")
    if spec.get("author"):
        draw_h(d, cv, W / 2, H - o - cv.S(132), spec["author"], F("serif", cv.S(44), 500),
               cv.C(th["fg"]), tr=cv.Sf(6), align="center")
        d.text((W / 2, H - o - cv.S(62)), "著", font=F("sans", cv.S(26), 400),
               fill=cv.C(th["mu"]), anchor="mm")
    return img


def t_dui(spec, th, seed, cv):
    """对 · 色块对切：底部深色块压上来，书名跨界"""
    cv.cur = "对"
    W, H, M = cv.W, cv.H, cv.M
    base_rgb = tuple(cv.base)
    bh = int(H * 0.42)
    blk = _rgb(th["ac"])
    canvas = Image.new("RGBA", (W, H), cv.C(th["bg"]))
    # 上下两块各有自己的底色，纹理要分别按各自的底色上色，不能一张铺到底。
    # 所以底部色块不再用 rectangle 实心填充（那会把刚铺好的纹理抹掉），
    # 改为直接按 blk 上色铺纹理 —— 纹理本身就以 blk 为基准色。
    cv.paint_base(canvas, base_rgb, (0, 0, W, H - bh))
    cv.paint_base(canvas, blk, (0, H - bh, W, H))
    cv.set_base(blk)
    onblk = _ensure(_rgb(th["bg"]), blk, 5.0)
    d = ImageDraw.Draw(canvas)
    d.line([0, H - bh, W, H - bh], fill=cv.C(th["bg"], 90), width=cv.S(2))
    d.line([0, H - bh + cv.S(10), W, H - bh + cv.S(10)], fill=cv.C(th["bg"], 28), width=1)
    size, ls, tr = fit(spec["title"], "serif", 700, W - 2 * M, H * 0.36,
                       cv.S(200), min0=cv.S(54), tr_ratio=0.03)
    y = H * 0.20
    cv.set_base(base_rgb)          # 书名在纸面上
    for line in ls:
        draw_h(d, cv, M, y, line, F("serif", size, 700), cv.C(th["fg"]), tr=tr)
        y += size * 1.13
    cv.set_base(blk)               # 下面作者/出处压色块
    fy = H - bh + cv.S(52)
    if spec.get("author"):
        draw_h(d, cv, M, fy, spec["author"], F("serif", cv.S(52), 500), cv.C(onblk), tr=cv.Sf(4))
        fy += cv.S(74)
    if spec.get("source"):
        draw_h(d, cv, M, fy, spec["source"], F("sans", cv.S(28), 400), cv.C(onblk), tr=cv.Sf(3))
    cv.set_base(base_rgb)
    return canvas


def t_lao(spec, th, seed, cv):
    """栏 · 稿纸界栏：竖栏格做底，书名压在干净的白条上"""
    cv.cur = "栏"
    img, d = cv.new()
    W, H, M = cv.W, cv.H, cv.M
    stripes(img, cv, gap=68, w=1, a=26)
    d.line([M, H * 0.06, M, H * 0.94], fill=cv.C(th["fg"], 40), width=cv.S(2))
    d.line([W - M, H * 0.06, W - M, H * 0.94], fill=cv.C(th["fg"], 40), width=cv.S(2))
    bx0, by0 = M, H * 0.20
    bx1, by1 = W - M, H * 0.20 + H * 0.30
    d.rectangle([bx0 + cv.S(8), by0, bx1 - cv.S(8), by1], fill=cv.C(th["bg"]))
    d = ImageDraw.Draw(img)
    size, ls, tr = fit(spec["title"], "serif", 700, (bx1 - bx0) - cv.S(90),
                       (by1 - by0) - cv.S(40), cv.S(150),
                       min0=cv.S(54), tr_ratio=0.05)
    y = (by0 + by1) / 2 - len(ls) * size * 1.12 / 2
    for line in ls:
        draw_h(d, cv, W / 2, y, line, F("serif", size, 700), cv.C(th["fg"]), tr=tr, align="center")
        y += size * 1.12
    d.line([bx0 + cv.S(8), by1, bx1 - cv.S(8), by1], fill=cv.C(th["ac"], 220), width=cv.S(4))
    if spec.get("subtitle"):
        draw_h(d, cv, W / 2, by1 + cv.S(44), spec["subtitle"], F("sans", cv.S(30), 400),
               cv.C(th["mu"]), tr=cv.Sf(7), align="center")
    if spec.get("author"):
        draw_h(d, cv, W - M - cv.S(4), H * 0.60, spec["author"] + " 著",
               F("serif", cv.S(40), 500), cv.C(th["fg"]), tr=cv.Sf(2), align="right")
    footer(d, cv, spec, H - M - 6)
    return img


def t_bei(spec, th, seed, cv):
    """碑 · 拓片墨底：巨字压中"""
    cv.cur = "碑"
    img, d = cv.new()
    W, H, M = cv.W, cv.H, cv.M
    grain(img, cv, seed, 12000, 7)
    vignette(img, cv, strength=30)
    size, ls, tr = fit(spec["title"], "serif", 900, W - 2 * M, H * 0.40,
                       cv.S(210), min0=cv.S(54), tr_ratio=0.02)
    y = H * 0.30 - len(ls) * size * 1.08 / 2 + size * 0.1
    for line in ls:
        draw_h(d, cv, W / 2, y + 2, line, F("serif", size, 900), cv.C(th["bg"], 46), tr=tr,
               align="center", deco=True)
        draw_h(d, cv, W / 2, y, line, F("serif", size, 900), cv.C(th["fg"]), tr=tr, align="center")
        y += size * 1.08
    hair(d, cv, y + cv.S(22), a=60, x0=W * 0.30, x1=W * 0.70, w=2)
    if spec.get("subtitle"):
        draw_h(d, cv, W / 2, y + cv.S(58), spec["subtitle"], F("sans", cv.S(30), 400),
               cv.C(th["mu"]), tr=cv.Sf(10), align="center")
    if spec.get("author"):
        draw_h(d, cv, W / 2, H - M - cv.S(96), spec["author"], F("serif", cv.S(46), 400),
               cv.C(th["fg"]), tr=cv.Sf(14), align="center")
    return img


def t_ye(spec, th, seed, cv):
    """夜 · 墨底白字：永远深色底，色相跟着主题走，大字距横排"""
    cv.cur = "夜"
    W, H, M = cv.W, cv.H, cv.M
    bgc = _rgb(th["bg"])
    fgc = _rgb(th["fg"])
    acc = _rgb(th["ac"])
    # 「夜」永远要深底：亮主题从墨色起手，暗主题从底色起手 —— 都掺一点强调色提色相。
    # 早期版本在暗主题上拿 fg（纸白）去混，结果整张变成浅灰，等于把「夜」做成了「昼」。
    if th["dark"]:
        ink = tuple(int(round(bgc[i] * 0.80 + acc[i] * 0.20)) for i in range(3))
    else:
        ink = tuple(int(round(fgc[i] * 0.86 + acc[i] * 0.14)) for i in range(3))
    top = tuple(int(round(ink[i] * 0.82)) for i in range(3))
    img = Image.new("RGB", (W, H), ink)
    _vgrad(img, top, ink, H)
    cv.set_base(ink)
    # 纹理铺在渐变之上：渐变只负责上下明暗，纸的质感要压在最外层底。
    # 底色取 ink（不是主题 bg）—— 「夜」的实底是合成出来的那个深色，不是配色表里的 bg。
    cv.paint_base(img, ink)
    grain(img, cv, seed, 11000, 8)
    d = ImageDraw.Draw(img)
    paper = _ensure(bgc, ink, 6.0)
    title = spec["title"]
    maxw = W - 2 * M
    size = cv.S(132)
    trr = 0.16
    ls = []
    while size >= cv.S(44):
        tr = size * trr
        f = F("serif", size, 500)
        ls = wrap(title, f, maxw, tr)
        if len(ls) * size * 1.26 <= H * 0.36 and all(tw(f, l, tr) <= maxw for l in ls):
            break
        size = int(size * 0.94)
    tr = size * trr
    f = F("serif", size, 500)
    y = H * 0.28
    for line in ls:
        draw_h(d, cv, W / 2, y, line, f, cv.C(paper), tr=tr, align="center")
        y += size * 1.26
    d.line([W / 2 - cv.S(40), y + cv.S(14), W / 2 + cv.S(40), y + cv.S(14)],
           fill=acc, width=cv.S(4))
    if spec.get("subtitle"):
        draw_h(d, cv, W / 2, y + cv.S(52), spec["subtitle"], F("sans", cv.S(30), 400),
               cv.C(paper, 170), tr=cv.Sf(10), align="center")
    if spec.get("author"):
        draw_h(d, cv, W / 2, H - M - cv.S(66), spec["author"], F("serif", cv.S(42), 400),
               cv.C(paper, 235), tr=cv.Sf(16), align="center")
        d.text((W / 2, H - M - cv.S(6)), "著", font=F("sans", cv.S(25), 400),
               fill=cv.C(paper, 140), anchor="mm")
    return img


def t_yin(spec, th, seed, cv):
    """印 · 满版印章：整张封面就是一方印，书名即印文"""
    cv.cur = "印"
    W, H = cv.W, cv.H
    ac = _rgb(th["ac"])
    img = Image.new("RGB", (W, H), ac)
    # 满版平色（渐变会让最亮那端跌破 4.5:1）
    # 印文是 200px 级别的超大字，按 WCAG「大文本」3:1 判定，不必为了 4.5 把朱砂提亮成粉红
    cv.set_base(ac)
    ink = _ensure(_rgb(th["bg"]), ac, 3.4)
    # 满版朱砂底 + 织物肌理：免得整块纯色看着像塑料贴纸。
    # 幅度调小（±4）—— 「印」的整张就是一个色块，纹理太重会破坏"一方印"的观感。
    _prev_amp = cv.texture_amp
    cv.texture_amp = max(3, _prev_amp // 2)
    cv.paint_base(img, ac)
    cv.texture_amp = _prev_amp
    grain(img, cv, seed, 6000, 7)
    d = ImageDraw.Draw(img)
    m = cv.S(104)
    d.rectangle([m, m, W - m, H - m], outline=cv.C(ink), width=cv.S(7))
    d.rectangle([m + cv.S(16), m + cv.S(16), W - m - cv.S(16), H - m - cv.S(16)],
                outline=_mix8(ac, ink, 0.42), width=cv.S(2))
    title = spec["title"].replace(" ", "")
    n = len(title)
    cols = 1 if n <= 4 else (2 if n <= 9 else (3 if n <= 14 else 4))
    per = -(-n // cols)
    chunks = [title[i * per:(i + 1) * per] for i in range(cols)]
    chunks = [c for c in chunks if c]
    cols = len(chunks)
    inner_w = W - 2 * (m + cv.S(46))
    inner_h = H - 2 * (m + cv.S(46))
    step = 1.14
    size = int(min(inner_w / (cols * 1.28), inner_h / (max(len(c) for c in chunks) * step)))
    size = max(cv.S(40), min(size, cv.S(220)))
    f = F("serif", size, 700)
    colw = size * 1.28
    x0 = W / 2 + (cols - 1) * colw / 2
    for ci, c in enumerate(chunks):
        tot = len(c) * size * step
        draw_v(d, cv, x0 - ci * colw, (H - tot) / 2, c, f, cv.C(ink), step)
    return img


def t_shu(spec, th, seed, cv):
    """疏 · 左上角对齐 + 满幅界栏细线：像一页稿纸，极克制"""
    cv.cur = "疏"
    img, d = cv.new()
    W, H, M = cv.W, cv.H, cv.M
    for x in range(M, W - M + 1, cv.S(60)):
        d.line([x, H * 0.062, x, H * 0.938], fill=cv.C(th["fg"], 16), width=1)
    grain(img, cv, seed, 12000, 5)
    d = ImageDraw.Draw(img)
    size, ls, tr = fit(spec["title"], "serif", 600, W - 2 * M - cv.S(40), H * 0.30,
                       cv.S(150), min0=cv.S(48), tr_ratio=0.05)
    y = H * 0.20
    for line in ls:
        draw_h(d, cv, M, y, line, F("serif", size, 600), cv.C(th["fg"]), tr=tr)
        y += size * 1.16
    d.line([M, y + cv.S(8), M + cv.S(120), y + cv.S(8)], fill=_rgb(th["ac"]), width=cv.S(5))
    if spec.get("subtitle"):
        draw_h(d, cv, M, y + cv.S(44), spec["subtitle"], F("sans", cv.S(30), 400),
               cv.C(th["mu"]), tr=cv.Sf(7))
    meta = "  ".join([x for x in [spec.get("author") and (spec["author"] + " 著"), spec.get("source")] if x])
    if meta:
        draw_h(d, cv, M, H - M - cv.S(40), meta, F("sans", cv.S(28), 400),
               cv.C(th["mu"]), tr=cv.Sf(2))
    return img


def _vgrad(img, c0, c1, h):
    """竖向线性渐变，直接写 RGB 像素。"""
    d = ImageDraw.Draw(img)
    w = img.size[0]
    for y in range(h):
        t = y / max(1, h)
        col = tuple(int(round(c0[i] + (c1[i] - c0[i]) * t)) for i in range(3))
        d.line([(0, y), (w, y)], fill=col)


_TEMPLATE_FUNCS = {
    "章": t_zhang, "简": t_jian, "框": t_kuang, "对": t_dui, "栏": t_lao,
    "碑": t_bei, "印": t_yin, "夜": t_ye, "疏": t_shu,
}


# --------------------------------------------------------------------------
# 整幅背景（族 B：自带的"万能封面预设"，§8.10）
# --------------------------------------------------------------------------
# 这九张不是纹理，是**整幅背景画**：画作压在一条边上，其余是大片留白。
# 所以它们不能走底纹那条路（那是给纯色底加质感），得单独一套：底图铺满、
# 文字放进构图留白的那块地方。
#
# 每张的 zone/align/maxw 都是量出来的，不是猜的：把每张图切成 6×6 网格，
# 算每格的 mean/sd，sd 小的格就是"干净的留白区"（画作的笔触会让 sd 跳到
# 15 以上）。九张全部有干净的上方 50%；只有 dark_03 的松枝伸到右上角，
# 文字必须靠左并限宽到 55%。
#
# dark=True 表示这张图是深底（文字要用浅色）。文字色不另配：深底用宣纸白、
# 浅底用墨色 —— 与六套配色里的"墨/纸"同一对，跨主题一致。
# label 是给界面看的名字：素材文件名（light_01）是工程记号，不该出现在
# 用户眼前。名字只描述画面里有什么，不评价好坏。
BACKGROUNDS: dict[str, dict] = {
    "light_01": {"label": "远山", "dark": False, "zone": "top",    "align": "left",   "maxw": 0.62},
    "light_02": {"label": "海平线", "dark": False, "zone": "top",  "align": "center", "maxw": 0.68},
    "light_04": {"label": "梅枝", "dark": False, "zone": "top",    "align": "left",   "maxw": 0.60},
    "light_05": {"label": "云雾", "dark": False, "zone": "center", "align": "center", "maxw": 0.66},
    "dark_01":  {"label": "山影", "dark": True,  "zone": "top",    "align": "center", "maxw": 0.66},
    "dark_02":  {"label": "水波", "dark": True,  "zone": "top",    "align": "left",   "maxw": 0.62},
    "dark_03":  {"label": "松枝", "dark": True,  "zone": "top",    "align": "left",   "maxw": 0.55},
    "dark_04":  {"label": "夜梅", "dark": True,  "zone": "top",    "align": "center", "maxw": 0.62},
    "dark_05":  {"label": "暗云", "dark": True,  "zone": "center", "align": "center", "maxw": 0.66},
}

_BACKGROUND_DIR = Path(__file__).resolve().parent / "assets" / "backgrounds"
_BACKGROUNDS = ({p.stem: p for p in sorted(_BACKGROUND_DIR.glob("*.png"))}
                if _BACKGROUND_DIR.is_dir() else {})
_BG_CACHE: dict[tuple[str, int, int], object] = {}
_BG_LOCK = threading.Lock()
# 输出位深：5bit（32 级）。实测 4bit 会把暖奶油色量化成偏黄绿 —— 是色相偏移
# 不只是色带；5bit 与原图肉眼无差。代价是成品封面 350–450 KB（程序化封面约 99 KB），
# 对一本带插图的 EPUB 来说可以接受，对纯文字书是明显变胖 —— 所以这是**可选**模式。
_BG_LEVELS = 32
# 平滑半径（按 1200 宽定标）：底图有细噪点，噪点是 PNG 的头号敌人。
# 0.6 → 538/593/472 KB，1.0 → 368/453/346 KB，1.0 还顺手抹掉了 2× 放大的插值痕。
_BG_BLUR = 1.0


def background_names() -> list[str]:
    """这台机器上真正可用的背景（素材缺了就少一张，界面据此决定给不给按钮）。"""
    return [n for n in BACKGROUNDS if n in _BACKGROUNDS]


def _load_background(name: str, w: int, h: int):
    """载入并放大到画布尺寸。带缓存 —— 同一进程里"换一个"会连点很多次。

    **必须返回副本**：调用方会直接在这张图上画字，缓存对象被就地改掉的话，
    第二次画出来的是"上一张的字 + 这一张的字"，而且审计会量到被污染的区域
    （实测：连画两次字节不同，第三次直接抛对比度不足）。
    """
    src = _BACKGROUNDS.get(name)
    if src is None:
        return None
    key = (name, w, h)
    with _BG_LOCK:
        hit = _BG_CACHE.get(key)
        if hit is not None:
            return hit.copy()
    try:
        im = Image.open(src).convert("RGB").resize((w, h), Image.LANCZOS)
        if _BG_BLUR > 0:
            im = im.filter(ImageFilter.GaussianBlur(_BG_BLUR * w / 1200.0))
    except Exception:
        return None
    with _BG_LOCK:
        _BG_CACHE[key] = im
    return im.copy()


def pick_background(title: str, step: int = 0) -> str:
    """确定性起点：同一本书永远从同一张开始（§8.5 确定性）。"""
    names = background_names()
    if not names:
        raise CoverError("这台机器上没有背景素材（gui/assets/backgrounds/ 是空的）")
    seed = int(hashlib.sha256((title or "").encode("utf-8")).hexdigest()[:8], 16)
    return names[(seed + step) % len(names)]


def next_background(name: str, step: int = 1) -> str:
    """「换一个」：确定性轮转。非法起点回落到确定性起点而不是抛错 ——
    用户手上可能是一个已经消失的素材名（换了素材包），不该因此画不出封面。"""
    names = background_names()
    if not names:
        raise CoverError("这台机器上没有背景素材（gui/assets/backgrounds/ 是空的）")
    if name not in names:
        return names[0]
    return names[(names.index(name) + step) % len(names)]


def background_label(name: str) -> str:
    """给界面看的名字。认不出来就回原名 —— 界面上宁可见一个工程记号，
    也不要空着一块让人以为加载失败。"""
    info = BACKGROUNDS.get(name)
    return (info or {}).get("label") or name


# --------------------------------------------------------------------------
# 「换一个」的统一队列（2026-10-28 按试用反馈重做）
# --------------------------------------------------------------------------
#
# 原来的界面把封面分成两条互斥的路：程序化版式（九款×配色）和整幅背景
# （九张画），还要使用者先选"用哪条路"。试用反馈里这条最直接：选项不该
# 让人疑惑，「换一个」应当把两种混在一个队列里。
#
# 这个判断是对的：对使用者来说这根本不是两种东西，都是"这本书能长的样子"。
# 分路只是我们实现上的区别，不该漏到界面上 —— 用户不该为了换一张封面
# 先理解我们的代码结构。
#
# 所以这里给出**一个**确定性队列：九张背景 + 九款版式×各允许配色，按
# 1:6 的比例交错排（九张背景 vs 五十几种版式组合，均匀交错才不会
# 连着给九次背景、也不会连着给几十次版式）。
# 「优先给用户没选过的」：前端记着这次会话里看过哪些，要"下一个"时传回来，
# 这里跳过；全看过的就按队列顺序接着转。

_LOOK_SEP = ":"


def _look_key(look: dict) -> str:
    """队列项的稳定标识。前端只把它当字符串存进 seen 列表，不用解析。"""
    if look["kind"] == "bg":
        return "bg" + _LOOK_SEP + look["background"]
    return "tpl" + _LOOK_SEP + look["template"] + _LOOK_SEP + look["theme"]


def look_key(look: dict) -> str:
    """给界面用的公开版本。"""
    return _look_key(look)


def _rotate(items: list, seed: int) -> list:
    """按种子转一下再取，避免每本书的队列都从同一款版式开头。"""
    if not items:
        return []
    n = seed % len(items)
    return items[n:] + items[:n]


def all_looks(title: str) -> list[dict]:
    """这本书能长成的全部样子，按**确定性**顺序排好（同一书名永远同一队列）。

    每项：`{"kind": "bg"|"tpl", "label": 给人看的名字, ...}`，
    整幅背景带 `background`，版式带 `template` 与 `theme`。
    素材或 Pillow 缺失时对应那一半自然为空，队列照样可用。
    """
    clean = (title or "").strip() or "未命名"
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    seed = int(digest[:8], 16)

    bgs: list[dict] = [
        {"kind": "bg", "background": name, "label": "整幅画「%s」" % background_label(name)}
        for name in background_names()
    ]
    tpls: list[dict] = [
        {"kind": "tpl", "template": t, "theme": th,
         "label": "版式「%s」· 配色「%s」" % (t, THEME_TABLE[th]["name"])}
        for t in TEMPLATES for th in ALLOWED.get(t, [])
    ]

    # 两半用**不同**的种子转：只用一个种子会让两者的相对位置固定，
    # 每本书都是"第 1 张背景永远跟着第 1 款版式"。
    bgs = _rotate(bgs, seed)
    tpls = _rotate(tpls, int(digest[8:16], 16))

    # 交错：每 gap 款版式之间夹一张背景。
    # gap 按实际数量算（36 款版式 / 9 张画 = 每 4 款夹一张），不写死 6 ——
    # 写死会在素材数量变化时留下"末尾连着三张背景"这种难看的收尾（实测过）。
    # 用整数比较推进，不用浮点累加：浮点会让同一份数据在不同机器上排出
    # 不同顺序，而"同一本书永远同一起点"是硬要求（§8.5）。
    out: list[dict] = []
    nb, nt = len(bgs), len(tpls)
    gap = max(1, (nt + nb - 1) // nb) if nb else 0
    bi = ti = 0
    while bi < nb or ti < nt:
        want_bg = nb > 0 and (nt == 0 or bi * gap <= ti)
        if want_bg and bi < nb:
            out.append(bgs[bi]); bi += 1
            continue
        if ti < nt:
            out.append(tpls[ti]); ti += 1
        elif bi < nb:
            out.append(bgs[bi]); bi += 1
    return out


def first_look(title: str) -> dict:
    """确定性起点：`auto_look` 选中的那一项，从队列里取出同一项返回。

    为什么绕一圈回队列取：队列里的项带 `label`，而且队列是"这本书能长的
    全部样子"的唯一真相 —— 自动档只负责挑，不该自己现造一项（造出来的
    项一旦与队列里的字段对不上，`look_key` 就对不上，「换一个」会跳过它）。
    """
    queue = all_looks(title)
    if not queue:
        raise CoverError("这台机器上没有可用的封面素材（背景素材与 Pillow 都缺）")
    want = _look_key(auto_look(title))
    for look in queue:
        if _look_key(look) == want:
            return look
    return queue[0]


def next_look(title: str, current: str | None = None, seen: list[str] | None = None) -> dict:
    """「换一个」：在统一队列里往前找一个**这次会话没给过的**。

    `current` 是当前项的 `_look_key`，`seen` 是这次已经给过的全部标识。
    全都看过之后按队列顺序接着转 —— 用户要求「选完了按已有队列顺序再来」，
    也就是不要卡在原地。

    任何异常输入（旧素材名、空 seen、认不出的 current）都不抛错：
    封面是可选步骤，为一次"换一个"失败而不给封面是本末倒置。
    """
    queue = all_looks(title)
    if not queue:
        raise CoverError("这台机器上没有可用的封面素材（背景素材与 Pillow 都缺）")
    keys = [_look_key(x) for x in queue]
    seen_set = set(seen or ())

    start = 0
    if current and current in keys:
        start = keys.index(current) + 1

    for i in range(len(queue)):
        idx = (start + i) % len(queue)
        if keys[idx] not in seen_set:
            return queue[idx]
    # 全看过了：按顺序给下一个，让队列继续转
    return queue[start % len(queue)]


def make_look(look: dict, title: str, author: str = "", *,
              width: int = 1200, height: int = 1600, audit: bool = True):
    """按队列项画封面。`look` 是 `all_looks()` 里的一项。

    这里只做分派，两边原有的绘制实现与配比完全不变 ——
    统一队列改的是"怎么挑"，不是"怎么画"。
    """
    if not look:
        return None
    if look.get("kind") == "bg":
        return make_cover(title, author, background=look["background"],
                          width=width, height=height, audit=audit)
    return make_cover(title, author, template=look.get("template"),
                      theme=look.get("theme"), width=width, height=height, audit=audit)


def _bg_worst_pixel(img, box, dark_text: bool):
    """文字块所在区域里"最不利"的那个像素。

    审计不能拿一个常数底色糊弄：背景画在同一块区域里可能有明暗过渡。
    深色字最怕最暗的像素，浅色字最怕最亮的像素。
    """
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.size[0], x1), min(img.size[1], y1)
    if x1 <= x0 or y1 <= y0:
        return (0, 0, 0) if dark_text else (255, 255, 255)
    region = img.crop((x0, y0, x1, y1))
    lo, hi = region.convert("L").getextrema()
    # 取该灰度对应的实际 RGB（避免把灰色当成彩色去算亮度）
    target = lo if dark_text else hi
    px = region.convert("L").point(lambda v: 255 if v == target else 0)
    bbox = px.getbbox()
    if bbox:
        cx = (bbox[0] + bbox[2]) // 2
        cy = (bbox[1] + bbox[3]) // 2
        return region.getpixel((min(cx, region.size[0] - 1), min(cy, region.size[1] - 1)))
    return (target, target, target)


def _draw_background_cover(spec: dict, bg_name: str, cv: _Canvas):
    """底图铺满 + 文字放进留白区。返回成品图（尚未量化）。"""
    img = _load_background(bg_name, cv.W, cv.H)
    if img is None:
        raise CoverError(f"背景素材载入失败：{bg_name}")
    meta = BACKGROUNDS.get(bg_name, {"dark": False, "zone": "top",
                                     "align": "left", "maxw": 0.62})
    W, H, M = cv.W, cv.H, cv.M
    is_dark = bool(meta["dark"])
    ink = (241, 239, 232) if is_dark else (27, 29, 30)   # 宣纸白 / 墨
    cv.set_base(_bg_worst_pixel(img, (0, 0, W, H), not is_dark))

    d = ImageDraw.Draw(img)
    maxw = W * float(meta["maxw"])
    x = M if meta["align"] == "left" else W / 2
    align = "left" if meta["align"] == "left" else "center"

    title = spec.get("title") or "未命名"
    author = spec.get("author") or ""

    # 标题字号：从大往小试。**先定行数，再定字号** —— 只要求"≤3 行"会挑到
    # 150px 那种把 "The Great Gatsby" 拆成三行、每行一个词的尺寸（实测）。
    # 正确做法：所有候选里取最少的行数，再在这一档里挑最大的字号。
    # 候选字号同样按设计尺度走：它是"在 1200 宽的画面里该多大"，
    # 不随画布缩的话，300×400 的缩略图会一路降到最小档仍放不下、被迫折行 ——
    # 同一本书的缩略图与大图就成了两张不同的设计（实测过，中位数差 16/255）。
    cands = []
    for size in (150, 132, 116, 100, 88, 76):
        f = F("serif", cv.S(size), 700)
        cand = wrap(title, f, maxw)
        if not cand or any(tw(f, s) > maxw + 2 for s in cand):
            continue
        cands.append((size, f, cand))
    if cands:
        fewest = min(len(c[2]) for c in cands)
        f_title, lines = next((f, ln) for _s, f, ln in cands if len(ln) == fewest)
    else:
        f_title = F("serif", cv.S(76), 700)
        lines = wrap(title, f_title, maxw)[:3]

    lh = f_title.size * 1.18
    block_h = lh * len(lines) + (f_title.size * 0.55 if author else 0)
    if meta["zone"] == "center":
        y = (H - block_h) / 2
    else:
        y = H * 0.11

    # 文字块矩形：审计用（把每一行都算进去，取最不利像素）
    block_box = (x - (maxw / 2 if align == "center" else 0),
                 y, x + (maxw / 2 if align == "center" else maxw), y + block_h)
    cv.text_log.append((f"背景 {bg_name}", int(f_title.size), ink,
                        _bg_worst_pixel(img, block_box, not is_dark)))

    for i, s in enumerate(lines):
        draw_h(d, cv, x, y + i * lh, s, f_title, ink, align=align, deco=True)

    if author:
        f_a = F("serif", cv.S(40), 400)
        ay = y + lh * len(lines) + f_title.size * 0.18
        draw_h(d, cv, x, ay, author, f_a, ink, tr=cv.Sf(2.0), align=align, deco=True)

    return img


# 全局锁：_Canvas 已经是每调用独立的，但字体缓存与 Pillow 内部有共享状态，
# 同一进程里多 worker 同时画要串行。代价是封面生成排队，收益是不会画出半张图。
_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# 对比度审计（§8.5：读绘制现场，不读成品像素）
# --------------------------------------------------------------------------
def _audit(text_log, template: str) -> None:
    """大文本（≥24px）3:1，正文 4.5:1。任一不达标抛 CoverError。"""
    for name, size, fg, bg in text_log:
        need = 3.0 if size >= 24 else 4.5
        got = contrast(fg, bg)
        if got + 1e-9 < need:
            raise CoverError(
                f"封面「{template}」的字看不清：{size}px 文字与底色对比 {got:.2f}:1，"
                f"低于 {need}:1（模板 {name}）"
            )


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def available() -> bool:
    """封面能不能画（Pillow 在不在）。界面据此决定「自动生成」按钮是否可点。"""
    return Image is not None


def auto_pick(title: str) -> tuple[str, str]:
    """书名 → (template, theme)。纯函数、无 random、同一书名恒定。"""
    clean = (title or "").strip() or "未命名"
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    pool = _pool_for(clean)
    template = pool[int(digest[:8], 16) % len(pool)]
    allowed = ALLOWED[template]
    # 次 8 位定配色。乘一个与长度互质的奇数再取模，避免"同一本书
    # 换个书名长度就系统性偏向某个配色"的伪相关。
    theme = allowed[(int(digest[8:16], 16) * 7) % len(allowed)]
    return template, theme


def auto_look(title: str) -> dict:
    """书名 → 队列里的**一项**（版式或整幅画）。纯函数、同一书名恒定。

    ★ 为什么要有这个（2026-10-31，规格允许「让自动档也能选底图」）：
      原来的自动档只在九款版式里挑，九张整幅画**永远进不了自动档** ——
      用户不点「换一个」就一辈子看不到它们，45 种样子实际只用得到一半。
      更糟的是长书名：POOL_LONG 只有 简/栏/疏 三款，各配四五套配色，
      十本长名书里六本撞成同一张图（实测 46 本真实感书名，长书名那一档
      只有 6 种不同样子）。画的构图与版式完全不同，并进来是零素材成本
      的扩容。

    候选 = 池内版式（各自允许的配色全部展开）+ 全部整幅画。两个半区都
    按**固定顺序**拼，不借 all_looks 的旋转顺序 —— 那个顺序依赖书名与
    素材数量，素材缺一张就整体位移，"同一本书永远同一个样子"跟着破。
    这里素材只影响画那半区的长度，版式那半区永远稳定。

    画自然占到 24%–43%（长书名最高，正好补它最窄的池子），不再另设
    概率阈值：多一个"按哈希位决定要不要走画"的开关，只是把随机藏到暗处。
    """
    clean = (title or "").strip() or "未命名"
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    pool = _pool_for(clean)
    bgs = [
        {"kind": "bg", "background": name, "label": "整幅画「%s」" % background_label(name)}
        for name in background_names()
    ]
    tpls = [
        {"kind": "tpl", "template": t, "theme": th,
         "label": "版式「%s」· 配色「%s」" % (t, THEME_TABLE[th]["name"])}
        for t in pool for th in ALLOWED.get(t, [])
    ]
    cands = bgs + tpls
    if not cands:
        raise CoverError("这台机器上没有可用的封面素材（背景素材与 Pillow 都缺）")
    # 用第 16–24 位：前面 16 位已经被 auto_pick 用掉了，复用会让
    # "自动选中的版式"与"自动选中的这一项"系统性相关。
    return cands[int(digest[16:24], 16) % len(cands)]


def next_choice(title: str, template: str, theme: str, step: int = 1) -> tuple[str, str]:
    """「换一个」：先在当前版式的允许配色内循环，配色轮完再换池内下一款版式。

    §8.6 要求仍然确定性 —— 同一个起点连点两次拿到同一个下一个，所以这里
    不用 random，只做确定性的环形推进。
    """
    if template not in ALLOWED:
        return auto_pick(title)
    allowed = ALLOWED[template]
    idx = -1 if theme not in allowed else allowed.index(theme)
    idx = (idx + step) % len(allowed)
    if idx == 0 and step > 0:
        # 配色绕回起点 → 说明这款版式的配色轮完了，换池内下一款版式
        pool = _pool_for((title or "未命名").replace(" ", ""))
        t = (pool.index(template) + step) % len(pool) if template in pool else 0
        template = pool[t]
        idx = 0
    return template, ALLOWED[template][idx]


def _make_cover_locked(title: str, author: str, subtitle: str, source: str,
                       template: str, theme: str, width: int, height: int,
                       audit: bool, background: str | None = None) -> bytes:
    th = THEME_TABLE[theme]
    cv = _Canvas(width, height, th)
    spec = {"title": title, "author": author, "subtitle": subtitle, "source": source}
    # seed 由书名派生：噪点纹理逐张不同，但同一本书恒定（§8.5 确定性）
    seed = int(hashlib.sha256((title + "|" + author).encode("utf-8")).hexdigest()[:8], 16)
    if background:
        img = _draw_background_cover(spec, background, cv)
        if audit:
            _audit(cv.text_log, f"背景 {background}")
        # 5bit 量化：底图本来就是 5bit，但 2× 放大与文字抗锯齿会插出新值。
        # 量化把「插值出来的渐变」压回 32 级，体积从 1.9 MB 降到 ~400 KB；
        # 4bit 会让暖奶油偏黄绿（实测），所以只能停在 5bit。
        step = 256 // _BG_LEVELS
        img = img.point(lambda v: (v // step) * step)
    else:
        img = _TEMPLATE_FUNCS[template](spec, th, seed, cv)
        if audit:
            _audit(cv.text_log, template)
    import io

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def make_cover(title: str, author: str = "", subtitle: str = "", source: str = "",
               *, template: str | None = None, theme: str | None = None,
               width: int = W_DEFAULT, height: int = H_DEFAULT,
               audit: bool = True, background: str | None = None) -> bytes | None:
    """画一张封面 PNG。Pillow 不在时返回 None（调用方回落"无封面"）。

    template / theme 传 None 走 auto_pick；传值即手选，非法值直接 ValueError
    （§8.6：不做修补 —— 悄悄换成另一款版式会让用户以为自己点错了按钮）。

    `background` 是另一条路：给一张"整幅背景画"的名字（`BACKGROUNDS` 的键），
    底图铺满、文字放进留白区，模板与配色都不参与（配色由底图自己决定）。
    两条路互斥，都给了就按 background 走。
    """
    if not available():
        return None
    clean_title = (title or "").strip() or "未命名"
    # 全自动（template 与 theme 都没给、也没给背景）：走 `auto_look`，
    # 与界面首屏 `first_look` 完全同一条路。原来这里走的是 `auto_pick`，
    # 只会在九款版式里挑，于是「不指定参数画出来的那一张」与「界面自动
    # 给出的那一张」可能是两张不同的图 —— 同一件事有两个答案，迟早出事。
    if background is None and template is None and theme is None:
        auto_item = auto_look(clean_title)
        if auto_item["kind"] == "bg":
            background = auto_item["background"]
        else:
            template, theme = auto_item["template"], auto_item["theme"]
    if background is not None:
        if background not in _BACKGROUNDS:
            raise CoverError(
                f"不认识的背景 {background!r}；只有 {'、'.join(background_names()) or '（空）'}"
            )
        if width < 200 or height < 200:
            raise CoverError(f"封面太小（{width}×{height}），至少 200×200")
        with _LOCK:
            return _make_cover_locked(clean_title, (author or "").strip(),
                                      (subtitle or "").strip(), (source or "").strip(),
                                      "章", "xuanzhi", int(width), int(height),
                                      audit, background=background)
    if template is None or theme is None:
        auto_t, auto_th = auto_pick(clean_title)
        template = auto_t if template is None else template
        theme = auto_th if theme is None else theme
    if template not in _TEMPLATE_FUNCS:
        raise CoverError(f"不认识的版式 {template!r}；只有 {'、'.join(TEMPLATES)}")
    if theme not in THEME_TABLE:
        raise CoverError(f"不认识的配色 {theme!r}；只有 {'、'.join(THEMES)}")
    if width < 200 or height < 200:
        raise CoverError(f"封面太小（{width}×{height}），至少 200×200")
    with _LOCK:
        return _make_cover_locked(clean_title, (author or "").strip(),
                                  (subtitle or "").strip(), (source or "").strip(),
                                  template, theme, int(width), int(height), audit)

# --------------------------------------------------------------------------
# 封底（2026-11-06，TODO 第 2 组）
# --------------------------------------------------------------------------
#
# 定稿规格（覆盖此前所有封底讨论）：
# - 封底上只有纸纹 + 一枚印：不落款、不画线、不画框、不居中；
# - 印落右下角，占画面宽 30%，右边距与下边距各 7%；
# - 印面比例 615:576，贴图按比例缩放，**不许压成正方形**（压了字会扁）；
# - 45 款 = 36 个版式×配色 + 9 张整幅背景，每款一张；
# - 封底没有自己的设计：纸纹与底色全跟封面走。

# 印面模板：从印章原图抠出的紧裁 alpha（灰度 L，615×576）。存 alpha 不存彩色图 ——
# 程序上色才能保证「印色跟配色走」，而且体积只有 6.6 KB。
_BACK_PLATE = Path(__file__).resolve().parent / "assets" / "seal_plate_615x576.png"
_BACK_W_RATIO = 0.30    # 印宽 / 画面宽
_BACK_M_RATIO = 0.07    # 右、下边距 / 画面宽
_BACK_DARK_LIFT = 0.30  # 深底印色往白提亮一档（定稿样张深底用的是亮朱）
# 整幅画那 9 款的封底底图：把画往纸色调压掉 82%，只留色调气质。
# 0 = 原样铺满（封底等于把封面再印一遍，印压在画上抢视线）；
# 1 = 纯纸色（跟封面看不出关系）。0.82 是拿 light_01 / dark_04 出对比图选的。
_BACK_BG_BLEND = 0.82

_PLATE_CACHE = None


def _seal_plate():
    """印章模板（灰度 L）。**亮部就是印泥，不要取反。**

    定稿样张是白文大印（红底米白镂空字），所以上色时 alpha 直接用原值；
    取反得到的是朱文，跟样张正好相反（2026-11-06 拿两版对照图确认过）。
    """
    global _PLATE_CACHE
    if not available():
        return None
    with _LOCK:
        if _PLATE_CACHE is None:
            try:
                _PLATE_CACHE = Image.open(_BACK_PLATE).convert("L")
            except Exception:  # noqa: BLE001 - 素材缺失不炸流程，由调用方决定退成"没有封底"
                return None
        return _PLATE_CACHE


def _seal_color(ac: str, dark: bool):
    """印色跟封面配色走；深底单独提亮一档（定稿样张深底是亮朱 #E07A6C 那一路）。

    不写死一个常量，是为了让六套配色各自保留自己的色相：
    松墨的朱、靛蓝的金，提亮之后仍然是它们自己，而不是统一变成样张那支红。
    """
    rgb = _rgb(ac)
    return _mix8(rgb, (255, 255, 255), _BACK_DARK_LIFT) if dark else rgb


def _paste_seal(img, color, w_ratio: float = _BACK_W_RATIO,
                m_ratio: float = _BACK_M_RATIO) -> bool:
    """把印贴到右下角。返回 False = 模板缺失，**这一页就不该算封底**（不抛错）。

    PIL 的 paste-with-mask 就是「color*alpha + 底*(1-alpha)」，与半透明合成等价，
    不需要自己做 RGBA 运算 —— 也正因为如此，先把印色做成纯色图再贴。
    """
    plate = _seal_plate()
    if plate is None:
        return False
    w, h = img.size
    sw = max(1, int(round(w * w_ratio)))
    sh = max(1, int(round(sw * plate.size[1] / float(plate.size[0]))))
    m = max(1, int(round(w * m_ratio)))
    alpha = plate.resize((sw, sh), Image.LANCZOS)
    img.paste(Image.new("RGB", (sw, sh), tuple(color)[:3]),
              (w - m - sw, h - m - sh), alpha)
    return True


def _back_paper(look: dict, width: int, height: int):
    """封底的纸面（**不含印**）+ 这一款该用的印色。返回 (img, color, dark)。

    单独拆出来是为了让"纸纹底色跟封面走"这件事可被测试量：把纸面与成品做差集，
    差集之外的部分多画出任何东西（落款/细线/边框）都该被抓住。

    版式项：把 cv.cur 设成版式名再 cv.new()，就得到与封面**同源**的纸纹与底色。
    整幅画项：整幅画没有配色表，按画的明暗取纸色/墨色（宣纸/松墨那一对），
    底图压到 18% 铺上去 —— 只留色调，不把封面再印一遍。
    """
    if look.get("kind") == "bg":
        name = look.get("background")
        dark = bool((BACKGROUNDS.get(name) or {}).get("dark"))
        tone = _rgb(THEME_TABLE["songmo" if dark else "xuanzhi"]["bg"])
        base = _load_background(name, width, height) if name in _BACKGROUNDS else None
        if base is None:
            img = Image.new("RGB", (width, height), tone)
        else:
            img = Image.blend(base, Image.new("RGB", (width, height), tone), _BACK_BG_BLEND)
        # 印色：浅底用宣纸朱、深底用定稿样张里那支亮朱（2026-11-06 实测像素值）。
        color = (224, 122, 108) if dark else (168, 58, 46)
        return img, color, dark

    template = look.get("template")
    theme = look.get("theme")
    if template not in _TEMPLATE_FUNCS:
        raise CoverError(f"不认识的版式 {template!r}；只有 {'、'.join(TEMPLATES)}")
    if theme not in THEME_TABLE:
        raise CoverError(f"不认识的配色 {theme!r}；只有 {'、'.join(THEMES)}")
    th = THEME_TABLE[theme]
    cv = _Canvas(width, height, th)
    cv.cur = template
    img, _draw = cv.new()
    return img, _seal_color(th["ac"], cv.dark), cv.dark


def make_back_cover(look: dict, *, width: int = W_DEFAULT, height: int = H_DEFAULT):
    """画一张封底 PNG。look 与 make_look 收同一种队列项（all_looks() 的一项）。

    纸面怎么来见 _back_paper；这里只做三件事：贴印、量化、编码。

    Pillow 不在返回 None（调用方回落"没有封底"），与 make_cover 同口径。
    印面模板没了也返回 None —— 封底的全部内容就是"纸纹 + 一枚印"，少掉印只剩
    一张白纸，那不算封底，宁可不出这一页。整幅画的素材缺了退成纯纸色不抛错
    （印还在，页面仍成立）—— 封底是锦上添花，不该拦住整本书的导出。
    但**版式名/配色名非法要抛**：那是调用方写错了，悄悄换一款会让人以为点错了按钮。
    """
    if not available() or not look:
        return None
    width, height = int(width), int(height)
    if width < 200 or height < 200:
        raise CoverError(f"封底太小（{width}×{height}），至少 200×200")

    with _LOCK:
        img, color, _dark = _back_paper(look, width, height)
        if not _paste_seal(img, color):
            return None  # 拿不到印：白纸不算封底，宁可不产这一页
        if look.get("kind") == "bg":
            # 5bit 量化：底图是 5bit 的，混合与贴图又会插出新值。不量化单张 1 MB 起，
            # 而这张图要随书进 EPUB。与封面的量化口径一致（step = 256//_BG_LEVELS）。
            step = 256 // _BG_LEVELS
            img = img.point(lambda v: (v // step) * step)

        import io

        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", optimize=True)
        return buf.getvalue()

