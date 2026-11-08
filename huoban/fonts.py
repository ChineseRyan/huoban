"""字体子集内嵌：把 OFL 中文字体裁成"只含本书用到的字"，打进 EPUB。

为什么现在要内嵌：只写系统字体回退链时，"宋体"在不同设备上是三种字形
（苹果 Songti SC、Windows SimSun、安卓 Noto Serif CJK），而 SimSun 在小字号下
走点阵渲染，观感与另外两个差得很远。内嵌一份子集字体，正文在哪台设备上都一样。

为什么是子集而不是整份字库：思源宋体 VF 原文件 23.97 MB，一本 500 KB 的小说
会被撑成 24 MB；手机阅读器加载这么大的字库还会卡。实测一本 2698 种字符的书，
子集后思源宋体约 1.1 MB、思源黑体约 0.8 MB —— 只随"这本书用了多少字"增长。

为什么只认 OFL（思源 / Noto / 花园明朝）：内嵌等于把字库数据放进包里分发。个人
自用没人管，但 OFL 字体在"能随便嵌"这件事上是零成本的正确选项，没有理由为省几
十 KB 去嵌 SimSun / 华文中宋这类商业字体。所以这里是**白名单**：文件名里没有
Noto / SourceHan / HanaMin 字样的一律不嵌（另有黑名单做双保险）。花园明朝是
OFL 1.1 与 Hanazono Font License 双许可（Debian 的 debian/copyright 里两种都
列了），能嵌。

为什么要第三份"补字"字体：思源宋体 SC 的 cmap 只覆盖 52 个扩展 B 区汉字
（U+20000 以上），而真实小说里偶尔会撞上这些字（异体名、古籍引文、作者自造
字），缺了就是豆腐块。本机 Windows 自带 SimSun-ExtB，能补全 42720 个，但那是
微软商业字体，按上面的白名单不能嵌；思源宋体完整版也只有 2057 个。花园明朝 B
（HanaMinB）覆盖 42711 个，是唯一能合法内嵌又基本补全的选择。

它只有扩展 B 区，正好用 unicode-range 限定在 U+20000-2A6DF：常用字仍走思源
（大陆字形），生僻字才落到花园明朝（日式字形 —— 那些字本来就没人认得，字形
无所谓）。补字字体子集化后通常只有几十 KB，装不下时静默跳过，不留告警。

fontTools 是可选依赖：本项目的 M1/M2 是纯标准库的，不能因为有人没装 fontTools
就让 import 崩掉。探测不到 fontTools 时 plan_fonts 直接返回 None，调用方退回
系统字体回退链并告警 —— 功能降级，但软件照常能用。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BODY_FAMILY",
    "HEADING_FAMILY",
    "EXTB_FAMILY",
    "BODY_ROLE",
    "HEADING_ROLE",
    "EXTB_ROLE",
    "EmbeddedFont",
    "FontPlan",
    "available_fonts",
    "missing_chars",
    "plan_fonts",
]

# fontTools 是可选依赖。除了 ImportError，还把任意异常一起吞掉：某些环境里
# fontTools 装了个半截（依赖缺失、ABI 不匹配）时抛的是别的异常，而这里的
# 语义只有一个 —— "用不了就降级"，不允许在 import 期把整个包带崩。
try:  # pragma: no cover - 取决于运行环境
    from fontTools import subset as _ft_subset
    from fontTools.ttLib import TTFont as _TTFont
    from fontTools.varLib import instancer as _instancer
except Exception:  # noqa: BLE001
    _ft_subset = None
    _TTFont = None
    _instancer = None

# 内嵌字体在 CSS 里的 family 名。带 huoban- 前缀是为了不可能与设备上任何
# 系统字体重名 —— 一旦重名，reader 可能拿系统字库顶替（甚至顶替成商业字体）。
BODY_FAMILY = "huoban-serif"
HEADING_FAMILY = "huoban-sans"
# 补字 family：只挂扩展 B 区，排在正文 family 之后当补丁。
EXTB_FAMILY = "huoban-extb"

# 角色名：正文用宋体、标题用黑体、扩展 B 区用花园明朝（契约固定）。
BODY_ROLE = "body"
HEADING_ROLE = "heading"
EXTB_ROLE = "extb"

# 补字字体管的码位范围。花园明朝 B 的 cmap 只有扩展 B 区（实测 42711 个，
# 基本区 / 扩 A / 扩 C 都是 0），所以限定在这一段，别让它去抢常用字的字形。
_EXTB_LO = 0x20000
_EXTB_HI = 0x2A6DF
_EXTB_UNICODE_RANGE = f"U+{_EXTB_LO:04X}-{_EXTB_HI:04X}"

# 为什么：见 docs/notes/variable-font-before-static.md
# 每个角色的候选字体：只列 OFL（思源 / Noto / 花园明朝）系列，顺序即优先级。
# 前两个是本机实测存在的可变字体（VF = 单个文件含全部字重，实例化到 400）。
_CANDIDATES: dict[str, tuple[str, ...]] = {
    BODY_ROLE: (
        "NotoSerifSC-VF.ttf",
        "NotoSerifSC-Regular.otf",
        "NotoSerifSC-Regular.ttf",
        "SourceHanSerifSC-VF.ttf",
        "SourceHanSerifSC-Regular.otf",
        "NotoSerifCJKsc-Regular.otf",
    ),
    HEADING_ROLE: (
        "NotoSansSC-VF.ttf",
        "NotoSansSC-Regular.otf",
        "NotoSansSC-Regular.ttf",
        "SourceHanSansSC-VF.ttf",
        "SourceHanSansSC-Regular.otf",
        "NotoSansCJKsc-Regular.otf",
    ),
    # 补字：只要 B 那份（A 只有基本区，用不上）。花园明朝的 B 是独立文件，
    # 不存在"选 A 还是选 B"的问题 —— 这里只可能落到 B。
    EXTB_ROLE: (
        "HanaMinB.otf",
        "HanaMinB.ttf",
        "HanaMinB.ttc",
    ),
}

# 白名单标记：文件名（小写）里含其中之一才允许内嵌。
_OFL_MARKERS = ("noto", "sourcehan", "source-serif", "source-sans", "hanamin", "hanazono")

# 黑名单标记：命中就不嵌，哪怕用户显式指了路径。白名单在先，这里只是双保险 ——
# 防止有人起个 "NotoSongti" 之类的名字把商业字体混进来。
_COMMERCIAL_MARKERS = (
    "simsun", "simhei", "simkai", "simfang", "msyh", "msmincho",
    "stsong", "stzhong", "stkaiti", "stfangso", "stxihei", "stheit",
    "songti", "kaiti", "fangsong", "pingfang", "hiragino", "mingliu", "yugothic",
)

# 角色 → (包内文件名主干, CSS 的 local() 回退链)。
# local() 只在 url() 取不到时才会被用（比如用户把 Fonts/ 目录解出来单看），
# 但留着不亏：一份坏字体会退化成"和以前一样，用系统字体"，而不是一屏方块。
_ROLE_SPEC: dict[str, tuple[str, tuple[tuple[str, ...], ...]]] = {
    BODY_ROLE: ("serif", (
        ("Noto Serif CJK SC",), ("Source Han Serif SC",),
        ("Songti SC",), ("SimSun",), ("宋体",),
    )),
    HEADING_ROLE: ("sans", (
        ("Noto Sans CJK SC",), ("Source Han Sans SC",),
        ("PingFang SC",), ("Microsoft YaHei",), ("微软雅黑",),
    )),
    # 补字字体基本没人装到系统里，local() 只是"用户自己装了花園明朝B"时的兜底。
    EXTB_ROLE: ("extb", (
        ("Hanazono Mincho B",), ("花園明朝B",),
    )),
}

# 字重：正文与标题都实例化到 400。h2.sub 上的 font-weight: bold 交给阅读器
# 合成（faux bold）—— 内嵌两套字重会再多一倍体积，收益不成比例。
_EMBED_WEIGHT = 400

# 扩展名 → (CSS src 里的 format() 关键字, manifest 的 media-type)。
# **format() 的取值不是扩展名**：真字体是 "truetype"/"opentype"，写成 "ttf"/"otf"
# 时严格一点的浏览器会整条 src 判无效，字体静默失效（退回 local() 甚至系统字体），
# 而且控制台不报错 —— 这种问题很难查。
_MEDIA_BY_EXT = {
    ".ttf": ("truetype", "font/ttf"),
    ".otf": ("opentype", "font/otf"),
}


@dataclass(frozen=True)
class EmbeddedFont:
    """一份要写进包的子集字体。

    filename 是**包内路径**（含 OEBPS/ 前缀），与 content.opf 的 href、CSS 的
    url 都能从它推出来：href 去掉 "OEBPS/"、url 相对 Styles/ 往上一级。
    chars 是这份子集实际装了多少种字符（= 请求字符里该字体 cmap 覆盖到的）。
    """

    family: str
    filename: str
    media_type: str
    data: bytes
    chars: int


@dataclass(frozen=True)
class FontPlan:
    """一次字体规划的结论：要嵌哪几份、CSS 头部长什么样、有什么要提醒的。"""

    fonts: list[EmbeddedFont]
    css: str
    warnings: list[str]


# ---------- 字体探测 ----------


def _font_dirs() -> list[Path]:
    """可能放字体的目录（按优先级）。只返回真实存在的。"""
    dirs: list[Path] = []
    # 项目自带的 fonts/ 排最前：花园明朝 B 不在任何系统的字体目录里，只能随项目
    # 走；思源宋体完整版也放这儿（系统里装的是 SC 可变字体，扩展 B 区少两千字）。
    # 目录名取"项目根/fonts"而不是包内：这两个文件加起来 57 MB，进不了 git，
    # 也不该跟着 pip 安装走。缺了就是功能降级（退回系统字体 + 一条缺字告警）。
    dirs.append(Path(__file__).resolve().parent.parent / "fonts")
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


def _index_dir(directory: Path, *, max_depth: int = 3) -> dict[str, Path]:
    """把目录里的文件名（小写）索引成 文件名 → 路径。

    为什么要走一趟 os.walk 而不是对每个候选名做 rglob：Linux 的 /usr/share/fonts
    底下是按厂商分的多层目录，对 6 个候选名各 rglob 一遍 = 把整棵树扫 6 遍。
    走一遍、限制深度，既够用也不至于在字体多的机器上卡住。
    """
    index: dict[str, Path] = {}
    base = len(directory.parts)
    try:
        for root, subdirs, files in os.walk(directory):
            depth = len(Path(root).parts) - base
            if depth >= max_depth:
                subdirs[:] = []
            for name in files:
                index.setdefault(name.lower(), Path(root) / name)
    except OSError:  # pragma: no cover - 权限不足的字体目录直接跳过
        return index
    return index


def _allow_embed(path: Path) -> bool:
    """这份字体能不能内嵌：只认 OFL（思源 / Noto / 花园明朝）白名单。

    白名单而不是黑名单，是因为"漏掉一个商业字体"的代价（包里塞了不该带的字库）
    远大于"少认一个开源字体"（退化回系统字体，用户几乎无感）。
    """
    name = path.name.lower()
    if path.suffix.lower() not in _MEDIA_BY_EXT:
        return False
    if any(bad in name for bad in _COMMERCIAL_MARKERS):
        return False
    return any(good in name for good in _OFL_MARKERS)


def available_fonts() -> dict[str, str]:
    """探测本机可用的 OFL 中文字体：{角色: 文件路径}；探测不到的角色不出现。"""
    found: dict[str, str] = {}
    for directory in _font_dirs():
        index = _index_dir(directory)
        for role, names in _CANDIDATES.items():
            if role in found:
                continue
            for name in names:
                path = index.get(name.lower())
                if path is not None and _allow_embed(path):
                    found[role] = str(path)
                    break
        if len(found) == len(_CANDIDATES):
            break
    return found


# ---------- 字符集归一 ----------


def _normalize_chars(chars) -> set[str]:
    """归一调用方给的字符集：补 ASCII 可见字符与全角空格，剔掉不可见垃圾。

    为什么必须补 ASCII：正文里一定有 "K"、"3"、半角标点、阿拉伯数字；只拿
    书里的汉字去子集，这些字符会在包里变成系统字体的方块或字体跳变
    （一行里"12"和汉字字形不一致，非常显眼）。
    为什么补 U+3000（全角空格）：中文缩进、对话里的间隔号都可能用它，
    而且清洗层产出的段落首部就是两个全角空格。
    """
    out: set[str] = set()
    for ch in chars or ():
        if isinstance(ch, str) and len(ch) == 1:
            out.add(ch)
    out |= {chr(c) for c in range(0x20, 0x7F)}
    out.add("\u3000")
    # 剔掉控制字符、孤立代理项、非字符码位：它们本来就不该出现在正文里
    # （epub 层会静默剔除），带进子集只会白白让 cmap 里多一条查不到的死记录。
    out = {
        ch
        for ch in out
        if (ord(ch) >= 0x20 or ch in "\t\n\r")
        and ord(ch) not in (0x7F, 0xFFFE, 0xFFFF)
        and not (0xD800 <= ord(ch) <= 0xDFFF)
    }
    out.discard("\t")
    out.discard("\n")
    out.discard("\r")
    return out


# ---------- 子集化 ----------


def _sfnt_meta(font) -> tuple[str, str]:
    """按 sfnt 版本给出 (扩展名, media-type)。CFF 字体存成 .otf，别改名叫 .ttf。"""
    try:
        version = font.sfntVersion
    except AttributeError:  # pragma: no cover - 理论上不会发生
        version = "\x00\x01\x00\x00"
    return (".otf", "font/otf") if version == "OTTO" else (".ttf", "font/ttf")


def _cmap_chars(font) -> set[str]:
    """字体 cmap 覆盖的全部字符。"""
    have: set[str] = set()
    try:
        for table in font["cmap"].tables:
            have |= {chr(code) for code in table.cmap}
    except KeyError:  # pragma: no cover - 没有 cmap 的坏字体
        return set()
    return have


# 读过的 cmap 按文件路径缓存：复核页要反复问"这本书缺哪些字"，
# 而读一次 25 MB 字库的 cmap 要几十毫秒，缓存之后第二次起是白捡。
_CMAP_CACHE: dict[str, set[str]] = {}


def _cmap_of(path: str) -> set[str]:
    """按路径缓存某个字体文件的 cmap 覆盖集。读不出来就当作"什么都没覆盖"。"""
    if path not in _CMAP_CACHE:
        try:
            font = _TTFont(str(path), fontNumber=0, recalcTimestamp=False, lazy=True)
            try:
                _CMAP_CACHE[path] = _cmap_chars(font)
            finally:
                font.close()
        except Exception:  # noqa: BLE001
            _CMAP_CACHE[path] = set()
    return _CMAP_CACHE[path]


def missing_chars(chars: set[str]) -> tuple[list[str], str]:
    """只算覆盖、不做子集化：返回 (内嵌字体里没有的字, 主字体文件名)。

    复核页要在**打包之前**说清"这本书有几个字不在字库里"。`plan_fonts` 会顺手
    把整本书子集化（一两秒），这里只读 cmap（几十毫秒），代价小到可以直接在
    复核页同步跑一次。

    没有可用字体时返回 `([], "")`：那种情况不是"缺字"，是"整套都用系统字体"，
    界面不该把它算成缺字 —— 那会把"没装思源"说成"这本书字不全"。
    """
    if _TTFont is None:
        return [], ""
    pool = available_fonts()
    if not pool:
        return [], ""
    source = _resolve_source(BODY_ROLE, None, pool, [])
    if not source:
        return [], ""
    covered = set(_cmap_of(source))
    extb = _resolve_source(EXTB_ROLE, None, pool, [])
    if extb:
        # 补字字体只挂扩展 B 区，但它认得的那些字确实不会缺 —— 这里按
        # "能覆盖就算有"来算，与实际打包时的字体链一致。
        covered |= _cmap_of(extb)
    wanted = _normalize_chars(chars)
    return sorted(wanted - covered), Path(source).name


def _subset_bytes(path: Path, chars: set[str], *, weight: int = _EMBED_WEIGHT):
    """把字体裁成只含 chars 的子集。

    返回 (字节, 扩展名, media-type, 实际覆盖到的字符集)。

    两个必须钉死的细节，否则"同样的书打两次包"会得到不同的字体字节：

    1. **recalcTimestamp=False**：TTFont 默认在 load 时把 head.modified 改成
       "现在"。这一条就足以让两次子集化的结果逐字节不同（实测差异全在 head）。
    2. **drop_tables += DSIG / meta**：DSIG 是签名表（子集后必然失效），meta 里
       常带厂商的构建时间。两者留着除了让字节不稳定，没有任何用处。

    顺序上先子集、后实例化（VF → 静态 400 字重）：反过来的话要先把 24 MB 的
    可变字体整体实例化并保存一遍，实测慢三倍多。
    """
    font = _TTFont(str(path), fontNumber=0, recalcTimestamp=False)
    covered = _cmap_chars(font)
    payload = "".join(sorted(chars & covered))

    options = _ft_subset.Options()
    options.drop_tables += ["DSIG", "meta"]
    options.notdef_outline = True
    subsetter = _ft_subset.Subsetter(options=options)
    subsetter.populate(text=payload)
    subsetter.subset(font)

    # 可变字体实例化到固定字重。没有 wght 轴就原样保留（静态字体走这条）。
    try:
        axes = {axis.axisTag for axis in font["fvar"].axes}
    except (KeyError, AttributeError):
        axes = set()
    if "wght" in axes and _instancer is not None:
        font = _instancer.instantiateVariableFont(font, {"wght": weight}, inplace=False)

    ext, media = _sfnt_meta(font)
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue(), ext, media, chars & covered


# ---------- CSS ----------


def _font_face_css(
    family: str, url: str, fmt: str, local_chain, *, unicode_range: str | None = None
) -> str:
    """拼一条 @font-face。

    src 里 url() 写在 local() 前面（契约约定）：url 是我们的子集字体，一定在包里，
    字形也一定是我们选的那份；local() 只是"包被拆开/字体损坏时不要变成方块"的
    兜底。顺序反过来的话，装了思源字体的设备会用系统那份（不同版本字形略有差异），
    "所有设备长得一样"这个目标就落空了。
    """
    parts = [f'url("{url}") format("{fmt}")']
    parts += [f'local("{name}")' for (name,) in local_chain]
    # unicode-range 只给补字字体用：让阅读器知道"这一份只管扩展 B 区"，
    # 常用字照旧走前面那份。没有它，某些引擎会拿补字字体去渲染它根本
    # 没有的字形（结果是豆腐），白白多一轮回退。
    range_line = f"    unicode-range: {unicode_range};\n" if unicode_range else ""
    return (
        "@font-face {\n"
        f'    font-family: "{family}";\n'
        "    src: " + ",\n         ".join(parts) + ";\n"
        "    font-weight: normal;\n"
        "    font-style: normal;\n"
        "    font-display: swap;\n"
        + range_line
        + "}\n"
    )


def _pkg_href(filename: str) -> str:
    """包内路径 → content.opf 的 href（相对 OEBPS/）。"""
    return filename[len("OEBPS/"):] if filename.startswith("OEBPS/") else filename


def _pkg_url(filename: str) -> str:
    """包内路径 → CSS 的 url()（样式表在 OEBPS/Styles/ 下，要往上一级）。"""
    return "../" + _pkg_href(filename)


# ---------- 主入口 ----------


def _resolve_source(role: str, override, pool: dict[str, str], warnings: list[str]):
    """定下这个角色到底用哪个字体文件；定不下来返回 None。

    override 允许三种写法：角色名（"body"/"heading"，取同角色那份）、
    字体文件的绝对/相对路径、空值（用该角色的默认候选）。

    **只有"没指定"才会退回默认候选**：显式指定了路径却不可用（不存在、
    不是 OFL）时返回 None，让这个角色老老实实走系统字体回退链 ——
    悄悄换成另一份字库，等于骗调用方说"你要的字形已经嵌进去了"。
    """
    default = pool.get(role)
    if override is None:
        return default
    text = str(override).strip()
    if not text:
        return default
    if text in pool:
        return pool[text]
    path = Path(text)
    if path.is_file():
        if _allow_embed(path):
            return str(path)
        # 显式点名了一个不许嵌的字体：**不换用默认字体**，这个角色就退系统回退链。
        # 悄悄换成另一份字库比不嵌更糟：调用方以为自己拿到了指定字形，
        # 实际拿到的却是别的东西，而且这在阅读器里完全看不出来。
        warnings.append(
            f"{path.name} 不是 OFL 开源字体（思源 / Noto / 花园明朝），拒绝内嵌：{role} 退回系统字体回退链"
        )
        return None
    warnings.append(f"指定的字体 {text} 不存在：{role} 退回系统字体回退链")
    return None


def plan_fonts(
    chars: set[str],
    *,
    body_font: str | None = None,
    heading_font: str | None = None,
) -> FontPlan | None:
    """规划本次要内嵌的字体；没有可用字体（或没装 fontTools）时返回 None。

    chars 是全书要显示的字符（调用方从章节树收集，加上元数据与自家文案）。
    内部会补上 ASCII 可见字符与全角空格 —— 调用方不必操心这两类。

    返回 None 的两种情况（都由调用方降级成"系统字体回退链 + 一条告警"）：
    - fontTools 不可用；
    - 本机没有任何 OFL 中文字体（没装思源 / Noto，项目里也没有 fonts/）。
    两种都不抛异常：字体是锦上添花，缺了不该让整本书打不出来。
    """
    if _ft_subset is None or _TTFont is None:
        return None
    pool = available_fonts()
    if not pool:
        return None

    wanted = _normalize_chars(chars)
    warnings: list[str] = []
    fonts: list[EmbeddedFont] = []
    css_blocks: list[str] = []
    covered_all: set[str] = set()
    # 同一个文件被两个角色选中时只子集化一次、只写进包一份，
    # 但 CSS 里仍给两个 family 各写一条 @font-face 指向它。
    cache: dict[str, tuple[bytes, str, str, set[str]]] = {}
    embedded_roles: dict[str, str] = {}  # 包内文件名 → 已用的 family（防重复条目）

    for role in (BODY_ROLE, HEADING_ROLE):
        override = body_font if role == BODY_ROLE else heading_font
        source = _resolve_source(role, override, pool, warnings)
        if not source:
            continue
        family = BODY_FAMILY if role == BODY_ROLE else HEADING_FAMILY
        stem, local_chain = _ROLE_SPEC[role]
        try:
            if source not in cache:
                cache[source] = _subset_bytes(Path(source), wanted)
            data, ext, media, have = cache[source]
        except Exception as exc:  # noqa: BLE001
            # 字体文件损坏 / 格式不认识 / fontTools 内部报错：某个角色失败
            # 只影响它自己，另一个角色照旧内嵌。
            warnings.append(f"字体 {Path(source).name} 子集化失败（{exc}）：{role} 退回系统字体")
            continue

        filename = f"OEBPS/Fonts/{stem}{ext}"
        if filename not in embedded_roles:
            embedded_roles[filename] = family
            fonts.append(
                EmbeddedFont(
                    family=family,
                    filename=filename,
                    media_type=media,
                    data=data,
                    chars=len(have),
                )
            )
        css_blocks.append(
            _font_face_css(family, _pkg_url(filename), _MEDIA_BY_EXT[ext][0], local_chain)
        )
        covered_all |= have

    # 补字字体：不是"角色"，没有 override，也不参与正文/标题的 family 竞争。
    # 只有真的用得上（本书确实有扩展 B 区字符、而补字字体确实认得）才嵌 ——
    # 一本普通小说不该为一个没人用的补丁多背 30 KB。
    extb_source = _resolve_source(EXTB_ROLE, None, pool, warnings)
    # 只看本书里落在扩展 B 区的那些字。**不能拿整个 wanted 去求交集**：HanaMinB
    # 的 cmap 里也有 ASCII，那样每本普通书都会多背一份没人用的 16 KB 字体。
    extb_wanted = {ch for ch in wanted if _EXTB_LO <= ord(ch) <= _EXTB_HI}
    if extb_source and extb_wanted:
        # 先只读 cmap 探一下有没有交集：子集化一份 32 MB 的字库要一两秒，
        # 而绝大多数书一个扩展 B 字都没有，不值当为它付这个时间。
        overlap: set[str] = set()
        try:
            probe = _TTFont(str(extb_source), fontNumber=0, recalcTimestamp=False, lazy=True)
            overlap = extb_wanted & _cmap_chars(probe)
            probe.close()
        except Exception:  # noqa: BLE001
            overlap = set()
        if overlap:
            try:
                if extb_source not in cache:
                    # 只子集化真正用到的那些字：unicode-range 已经把这份字体锁在
                    # 扩展 B 区，带上 ASCII 只是白占体积。
                    cache[extb_source] = _subset_bytes(Path(extb_source), overlap)
                data, ext, media, have = cache[extb_source]
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"补字字体 {Path(extb_source).name} 子集化失败（{exc}）：缺字会落到系统回退字体"
                )
            else:
                filename = f"OEBPS/Fonts/{_ROLE_SPEC[EXTB_ROLE][0]}{ext}"
                if filename not in embedded_roles:
                    embedded_roles[filename] = EXTB_FAMILY
                    fonts.append(
                        EmbeddedFont(
                            family=EXTB_FAMILY,
                            filename=filename,
                            media_type=media,
                            data=data,
                            chars=len(have),
                        )
                    )
                css_blocks.append(
                    _font_face_css(
                        EXTB_FAMILY,
                        _pkg_url(filename),
                        _MEDIA_BY_EXT[ext][0],
                        _ROLE_SPEC[EXTB_ROLE][1],
                        unicode_range=_EXTB_UNICODE_RANGE,
                    )
                )
                covered_all |= have

    if not fonts:
        return None

    missing = sorted(wanted - covered_all)
    if missing:
        sample = "".join(missing[:8])
        warnings.append(
            f"有 {len(missing)} 种字符不在内嵌字体里（如 {sample}）：这些字会落到系统回退字体"
        )

    return FontPlan(fonts=fonts, css="\n".join(css_blocks), warnings=warnings)
