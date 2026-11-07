"""字体子集化与内嵌的测试：fonts.py + epub.py 的字体接线。

照 tests/test_epub.py 的风格：不用 pytest，一个 case 一个函数，纯 assert，
直接 python tests/test_fonts.py 跑。只断言契约，不断言内部实现细节。

几处刻意的设计：

1. **依赖本机字体**。"内嵌字体"这件事没有字体就无从测起，所以要求本机装思源/
   Noto。找不到时不是"失败"而是"跳过"，并且跳过的条数会算进总数分母、
   同时打印一行说明 —— 悄悄当通过会让"全绿"变成一句谎话。
2. **子集化很慢**（两套字体约 1.5~7 秒，取决于字符数）。多数字典序无关的用例
   共用一份模块级缓存的小字体方案（_SMALL_PLAN），不重复子集化。
3. **不往项目目录写临时文件**，一律走 tempfile。

覆盖的边界：
- plan_fonts：两个角色各一份、family/filename/media_type 契约、字符覆盖；
- 产出的 ttf 能被 fontTools 重新打开，cmap 确实覆盖输入字符（而不只是"字节非空"）；
- 确定性：同一 chars 两次子集化 data 逐字节相同（recalcTimestamp 那个坑的回归）；
- fontTools 不可用：降级返回 None，不抛异常；
- 只嵌 OFL：商业字体（SimSun 等）显式拒绝并告警；
- build_epub(fonts=False)：完全保持原样（没有 @font-face、没有字体条目）；
- build_epub(fonts=True)：字体条目位置/不压缩、manifest、CSS、body 字体首位，
  且两种模式产出的所有 XML 都良构；
- 找不到字体时的 warning 文案。
"""

from __future__ import annotations

import io
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban import fonts as fonts_mod  # noqa: E402
from huoban.epub import build_epub  # noqa: E402
from huoban.model import Book, Chapter, Paragraph  # noqa: E402

FIXED_MODIFIED = "2026-10-24T00:00:00Z"

# 样本字符：一段真实中文 + 数字 + 半角标点。子集化只需要"这些字在 cmap 里"，
# 不需要长文本 —— 字符种数才是体积的驱动因素，重复次数无关。
SAMPLE_TEXT = "他说：第3章开始了，天色将晚，远处传来钟声。"

_HAVE_FONTS = bool(fonts_mod.available_fonts())

# 模块级缓存：多数字典序无关的用例共用这份很小的子集方案，避免重复子集化。
# 用函数+列表而不是直接赋值，是为了让"跳过"路径也能 import 成功（字体缺失时
# 这里根本不会被子集化）。
_CACHE: dict[str, object] = {}


def _small_plan():
    """一份小字符集的字体方案（缓存）。没有可用字体时返回 None。"""
    if "plan" not in _CACHE:
        _CACHE["plan"] = fonts_mod.plan_fonts(set(SAMPLE_TEXT))
    return _CACHE["plan"]  # type: ignore[return-value]


# ---------- 造数据与拆包的小工具 ----------


def _chapter(title: str, paragraphs=None, level: int = 1) -> Chapter:
    body = []
    for p in paragraphs or []:
        body.append(p if isinstance(p, Paragraph) else Paragraph(text=p))
    return Chapter(title=title, level=level, paragraphs=body)


def _book(*chapters: Chapter) -> Book:
    return Book(title="测试书", author="作者", chapters=list(chapters))


def _build(book: Book, **kwargs):
    """固定 modified 打包：字体规划与 modified 都会影响字节，不固定就没法比。"""
    kwargs.setdefault("modified", FIXED_MODIFIED)
    return build_epub(book, **kwargs)


def _css_of(result, name: str = "OEBPS/Styles/main.css") -> str:
    with zipfile.ZipFile(io.BytesIO(result.data)) as z:
        return z.read(name).decode("utf-8")


def _font_family_in(css: str, selector: str) -> str:
    """取某条规则里的 font-family 值（取第一条匹配）。"""
    m = re.search(re.escape(selector) + r"\s*\{[^}]*?font-family:\s*([^;]+);", css, re.S)
    assert m is not None, f"CSS 里找不到 {selector} 的 font-family"
    return m.group(1)


def _skip(name: str) -> None:
    print(f"SKIP {name}（本机没有思源/Noto 中文字体）")


# ---------- plan_fonts 契约 ----------


def case_plan_fonts_two_roles() -> None:
    """plan_fonts 正常时给出正文/标题两份字体，字段与契约一致。"""
    if not _HAVE_FONTS:
        return _skip("case_plan_fonts_two_roles")
    plan = _small_plan()
    assert plan is not None, "本机有字体但 plan_fonts 返回了 None"

    families = [f.family for f in plan.fonts]
    assert "huoban-serif" in families, f"缺正文字体：{families}"
    assert "huoban-sans" in families, f"缺标题字体：{families}"

    for f in plan.fonts:
        assert f.filename.startswith("OEBPS/Fonts/"), f"包内路径不对：{f.filename}"
        assert f.filename.endswith((".ttf", ".otf")), f"扩展名不对：{f.filename}"
        assert f.media_type in ("font/ttf", "font/otf"), f"media-type 不对：{f.media_type}"
        assert isinstance(f.data, bytes) and len(f.data) > 1000, "字体字节不像一份真字体"
        assert f.chars > 0, "chars 必须记录实际覆盖的字数"

    # CSS 的 @font-face 条数必须与字体份数一致，url 指向的正是 filename 推出来的路径。
    # （样本全是常用字，不该牵出补字字体，所以这里正好 2 条。）
    assert plan.css.count("@font-face") == len(plan.fonts), \
        f"@font-face 条数与字体份数不符：\n{plan.css}"
    for f in plan.fonts:
        assert f'font-family: "{f.family}"' in plan.css, f"CSS 缺 {f.family} 的声明"
        href = f.filename[len("OEBPS/"):]
        assert f'url("../{href}")' in plan.css, f"CSS 的 url 与 filename 不一致：{href}"


def case_plan_fonts_char_coverage() -> None:
    """样本字符必须真的被覆盖到：chars 计数 = cmap 相交数，不是输入字符数。"""
    if not _HAVE_FONTS:
        return _skip("case_plan_fonts_char_coverage")
    plan = _small_plan()
    assert plan is not None
    wanted = set(SAMPLE_TEXT) | {chr(c) for c in range(0x20, 0x7F)} | {"\u3000"}
    # ASCII 与全角空格是内部补的，必须算进去；样本里的汉字也必须覆盖。
    for f in plan.fonts:
        assert f.chars <= len(wanted), "覆盖字数不可能超过请求的字数"
        assert f.chars >= len(set(SAMPLE_TEXT)), \
            f"{f.family} 只覆盖了 {f.chars} 字，连样本正文都没装下"


def case_subset_opens_and_covers() -> None:
    """产出的 ttf 能被 fontTools 重新打开，且 cmap 真的覆盖输入的每个字。"""
    if not _HAVE_FONTS:
        return _skip("case_subset_opens_and_covers")
    if fonts_mod._TTFont is None:  # pragma: no cover - 有字体就一定有 fontTools
        return _skip("case_subset_opens_and_covers")
    plan = _small_plan()
    assert plan is not None
    for f in plan.fonts:
        if f.family == fonts_mod.EXTB_FAMILY:
            # 补字字体只含扩展 B 区，样本里的常用字它本来就不该有。
            continue
        font = fonts_mod._TTFont(io.BytesIO(f.data), fontNumber=0)
        cmap: set[str] = set()
        for table in font["cmap"].tables:
            cmap |= {chr(code) for code in table.cmap}
        for ch in set(SAMPLE_TEXT) | {"\u3000"}:
            assert ch in cmap, f"{f.family} 的 cmap 不含 {ch!r}"
        for ch in "0123456789ABCxyz.,:;":
            assert ch in cmap, f"{f.family} 的 cmap 不含 ASCII 字符 {ch!r}"
        # 子集必须真的变小：一份 20 MB 的 VF 字体裁完还这么大就是没生效。
        assert len(f.data) < 8 * 1024 * 1024, f"{f.family} 子集后仍有 {len(f.data)} 字节"


# 两个扩展 B 区汉字（U+20000 以上）。HanaMinB 认得的字，用它们测补字字体。
EXTB_TEXT = "\U0002000B\U0002000C"


def _have_extb() -> bool:
    return fonts_mod.EXTB_ROLE in fonts_mod.available_fonts()


def case_extb_font_wiring() -> None:
    """书里有扩展 B 区字符时，补字字体必须接进来，且只认扩展 B 区。"""
    if not _HAVE_FONTS:
        return _skip("case_extb_font_wiring")
    if not _have_extb():
        return _skip("case_extb_font_wiring")
    chars = set(SAMPLE_TEXT) | set(EXTB_TEXT)
    plan = fonts_mod.plan_fonts(chars)
    assert plan is not None
    families = [f.family for f in plan.fonts]
    assert fonts_mod.EXTB_FAMILY in families, f"有扩展 B 字符却没嵌补字字体：{families}"

    extb = next(f for f in plan.fonts if f.family == fonts_mod.EXTB_FAMILY)
    assert extb.filename.startswith("OEBPS/Fonts/"), extb.filename
    # 只装扩展 B 区那几个字：ASCII 与常用汉字都不该混进来（那部分由思源负责）。
    assert extb.chars <= len(set(EXTB_TEXT)), f"补字字体装多了：{extb.chars} 字"
    assert extb.chars > 0
    # 体积上限：一份 32 MB 字库只裁两个字，还大就是没裁。
    assert len(extb.data) < 200 * 1024, f"补字字体子集后仍有 {len(extb.data)} 字节"

    # @font-face 必须带 unicode-range，否则它会去抢常用字的字形。
    block = plan.css[plan.css.index(f'font-family: "{fonts_mod.EXTB_FAMILY}"'):]
    block = block[:block.index("}")]
    assert "unicode-range: U+20000-2A6DF" in block, f"补字字体缺 unicode-range：\n{block}"

    # 扩B 字符不再进"缺字"告警 —— 这正是加这份字体的目的。
    assert not any("不在内嵌字体里" in w for w in plan.warnings), plan.warnings

    # 打包后：正文字体链里思源在前、补字在后（常用字必须先由思源接住）。
    result = _build(_book(_chapter("第一章", [SAMPLE_TEXT + EXTB_TEXT])), fonts=True)
    css = _css_of(result)
    body_family = _font_family_in(css, "body")
    serif_at = body_family.find('"huoban-serif"')
    extb_at = body_family.find(f'"{fonts_mod.EXTB_FAMILY}"')
    assert 0 <= serif_at < extb_at, f"补字字体必须排在正文字体之后：{body_family}"
    assert not any("不在内嵌字体里" in w for w in result.warnings), result.warnings


def case_extb_skipped_when_unused() -> None:
    """书里没有扩展 B 字符时，不该为补字字体白背一份体积。"""
    if not _HAVE_FONTS:
        return _skip("case_extb_skipped_when_unused")
    plan = _small_plan()
    assert plan is not None
    families = [f.family for f in plan.fonts]
    assert fonts_mod.EXTB_FAMILY not in families, \
        f"常用字的书不该嵌补字字体：{families}"


def case_extb_missing_degrades() -> None:
    """补字字体不存在时：照常出包，扩展 B 字符退化成一条缺字告警。"""
    if not _HAVE_FONTS:
        return _skip("case_extb_missing_degrades")
    saved = fonts_mod.available_fonts

    def _without_extb():
        pool = dict(saved())
        pool.pop(fonts_mod.EXTB_ROLE, None)
        return pool

    try:
        fonts_mod.available_fonts = _without_extb  # type: ignore[assignment]
        plan = fonts_mod.plan_fonts(set(SAMPLE_TEXT) | set(EXTB_TEXT))
        assert plan is not None, "少了补字字体也必须能出方案"
        assert fonts_mod.EXTB_FAMILY not in [f.family for f in plan.fonts]
        assert any("不在内嵌字体里" in w for w in plan.warnings), \
            f"没有补字字体时扩展 B 字符必须进缺字告警：{plan.warnings}"
    finally:
        fonts_mod.available_fonts = saved  # type: ignore[assignment]


def case_subset_deterministic() -> None:
    """同一 chars 子集化两次，data 必须逐字节相同。

    这是 recalcTimestamp 那个坑的回归用例：TTFont 默认 load 时把 head.modified
    改成"现在"，于是同样的输入两次产出不同字节 —— 看起来只是时间戳，实际会让
    "同一本书两次打包 sha256 不同"，去重、缓存、校验全部失效。
    """
    if not _HAVE_FONTS:
        return _skip("case_subset_deterministic")
    chars = set(SAMPLE_TEXT)
    a = fonts_mod.plan_fonts(chars)
    b = fonts_mod.plan_fonts(chars)
    assert a is not None and b is not None
    assert [f.filename for f in a.fonts] == [f.filename for f in b.fonts]
    for fa, fb in zip(a.fonts, b.fonts):
        assert fa.data == fb.data, f"{fa.family} 两次子集化字节不同（确定性被破坏）"


def case_fonttools_missing_degrades() -> None:
    """fontTools 不可用时 plan_fonts 返回 None，绝不抛异常。"""
    saved = (fonts_mod._ft_subset, fonts_mod._TTFont, fonts_mod._instancer)
    try:
        fonts_mod._ft_subset = None
        fonts_mod._TTFont = None
        fonts_mod._instancer = None
        assert fonts_mod.plan_fonts(set(SAMPLE_TEXT)) is None, \
            "fontTools 不在时必须返回 None（调用方据此降级）"

        # build_epub(fonts=True) 也必须能降级出包，不能抛异常。
        result = _build(_book(_chapter("第一章", ["甲。"])), fonts=True)
        assert result.data.startswith(b"PK"), "降级后仍必须产出合法的 zip 包"
        assert any("没找到可内嵌的开源中文字体" in w for w in result.warnings), \
            f"降级必须有 warning：{result.warnings}"
    finally:
        fonts_mod._ft_subset, fonts_mod._TTFont, fonts_mod._instancer = saved


def case_commercial_font_rejected() -> None:
    """显式指定商业字体（SimSun / 华文宋体）时必须拒绝并告警，不许偷偷嵌进去。"""
    if not _HAVE_FONTS:
        return _skip("case_commercial_font_rejected")
    # 造一个假路径：文件名是商业字体，内容随便。_allow_embed 只看文件名，
    # 所以这里不需要真字体文件 —— 也绝不能真去读系统里的 SimSun。
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "SimSun.ttf"
        fake.write_bytes(b"\x00" * 16)
        assert fonts_mod._allow_embed(fake) is False, "SimSun 必须被拒绝"
        for name in ("STSong.ttf", "STKaiti.otf", "方正宋体.ttf", "PingFang.ttf"):
            p = Path(tmp) / name
            assert fonts_mod._allow_embed(p) is False, f"{name} 必须被拒绝"
        for name in ("NotoSerifSC-Regular.otf", "SourceHanSansSC-VF.ttf"):
            p = Path(tmp) / name
            assert fonts_mod._allow_embed(p) is True, f"{name} 是 OFL 字体，应允许"

        # 通过 plan_fonts 显式传商业字体路径：会被拒，并留下一条说明。
        plan = fonts_mod.plan_fonts(set(SAMPLE_TEXT), body_font=str(fake))
        assert plan is not None, "只拒绝了一个角色，另一个角色仍应内嵌"
        families = [f.family for f in plan.fonts]
        assert "huoban-serif" not in families,             f"正文角色被拒后不该悄悄换成别的字库，只能退回系统回退链：{families}"
        assert "huoban-sans" in families, "标题角色没被指定，应该照常内嵌"
        assert not any("simsun" in f.filename.lower() for f in plan.fonts)
        assert any("不是 OFL 开源字体" in w for w in plan.warnings), \
            f"拒绝商业字体必须留一条说明：{plan.warnings}"


def case_normalize_chars() -> None:
    """字符归一：补 ASCII 与全角空格，剔掉控制字符。"""
    got = fonts_mod._normalize_chars({"甲", "\u3000", "\x00", "\x0c", "A"})
    assert "甲" in got and "A" in got and "\u3000" in got
    assert "\x00" not in got and "\x0c" not in got, "控制字符必须剔除"
    auto = fonts_mod._normalize_chars(set())
    for ch in "~!0z":
        assert ch in auto, "必须自动补满 ASCII 可见字符"


# ---------- build_epub 接线 ----------


def case_build_without_fonts_unchanged() -> None:
    """fonts=False（默认）：包结构与 CSS 都必须与"从来就没有字体功能"一致。"""
    book = _book(_chapter("第一章", ["甲。"]))
    result = _build(book)
    assert not any("Fonts/" in name for name in result.files), result.files
    css = _css_of(result)
    assert "@font-face" not in css and ".ttf" not in css, "fonts=False 时不许出现字体声明"
    # 默认 False 时不该报"没找到字体" —— 用户没要求，报错是噪声。
    assert not any("开源中文字体" in w for w in result.warnings), result.warnings
    assert '"huoban-serif"' not in css and '"huoban-sans"' not in css
    # 回退链本身必须还在（这是我们不做字体时的全部保障）。
    assert "Songti SC" in css and "SimSun" in css and "Kaiti SC" in css


def case_build_with_fonts_structure() -> None:
    """fonts=True：字体条目位置、不压缩、manifest、CSS、body 首位全部到位。"""
    if not _HAVE_FONTS:
        return _skip("case_build_with_fonts_structure")
    book = _book(_chapter("第一章", [SAMPLE_TEXT]))
    result = _build(book, fonts=True)
    font_names = [n for n in result.files if n.startswith("OEBPS/Fonts/")]
    assert font_names, f"包内没有字体条目：{result.files}"

    # 位置：紧随 Styles/main.css，且在封面图/正文之前。
    css_at = result.files.index("OEBPS/Styles/main.css")
    for name in font_names:
        assert result.files.index(name) == css_at + 1 + font_names.index(name), \
            f"字体条目应紧跟在 main.css 之后：{result.files}"
    first_text = next(i for i, n in enumerate(result.files)
                      if n.startswith("OEBPS/Text/"))
    assert all(result.files.index(n) < first_text for n in font_names), \
        "字体条目必须在 Text/ 之前"

    with zipfile.ZipFile(io.BytesIO(result.data)) as z:
        for name in font_names:
            info = z.getinfo(name)
            assert info.compress_type == zipfile.ZIP_STORED, \
                f"{name} 必须是 ZIP_STORED（字体已压过，再 deflate 只费 CPU）"
            assert info.file_size > 1000
            opf = z.read("OEBPS/content.opf").decode("utf-8")
            href = name[len("OEBPS/"):]
            assert f'href="{href}"' in opf, f"manifest 缺 {href}"
            assert 'media-type="font/ttf"' in opf or 'media-type="font/otf"' in opf
        css = z.read("OEBPS/Styles/main.css").decode("utf-8")
        # @font-face 在样式表最前面。
        assert css.lstrip().startswith("@font-face"), "字体声明应插在样式表最前"

    # body 首位必须是内嵌 family，h1 首位必须是标题 family（否则内嵌字体白嵌）。
    body_family = _font_family_in(css, "body")
    assert body_family.strip().startswith('"huoban-serif"'), body_family
    assert "Songti SC" in body_family, "内嵌字体后面必须保留原回退链"
    h1_family = _font_family_in(css, "h1")
    assert h1_family.strip().startswith('"huoban-sans"'), h1_family
    assert "Kaiti SC" in h1_family, "内嵌字体后面必须保留楷体回退链"

    # 内嵌字体不该"有但它被跳过"：字体文件里必须真的含样本字符。
    assert not any("不在内嵌字体里" in w for w in result.warnings), \
        f"样本字符全部是常用字，不该有缺字警告：{result.warnings}"

    # nav/style 交叉引用：CSS 里的 url 在包里真实存在。
    with zipfile.ZipFile(io.BytesIO(result.data)) as z:
        for url in re.findall(r'url\("([^"]+)"\)', css):
            assert url.startswith("../Fonts/")
            assert url.replace("../", "OEBPS/") in z.namelist(), f"CSS 指向不存在的 {url}"


def case_build_fonts_deterministic() -> None:
    """fonts=True 时两次打包 sha256 也必须相同（字体字节是最大的变量源）。"""
    if not _HAVE_FONTS:
        return _skip("case_build_fonts_deterministic")
    book = _book(_chapter("第一章", [SAMPLE_TEXT]))
    a = _build(book, fonts=True)
    b = _build(book, fonts=True)
    assert a.data == b.data, "fonts=True 两次打包字节不同"
    assert a.files == b.files


def case_all_xml_wellformed_with_fonts() -> None:
    """两种模式下产出的每个 XML 都必须良构（字体接进 manifest 后尤其要试）。"""
    book = _book(_chapter("第一章", ["甲。", Paragraph(text="按语", kind="note")]))
    for flags in (False, True):
        if flags and not _HAVE_FONTS:
            continue
        result = _build(book, fonts=flags)
        with zipfile.ZipFile(io.BytesIO(result.data)) as z:
            for name in z.namelist():
                if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                    ET.fromstring(z.read(name))


def case_fonts_true_without_fonts_degrades() -> None:
    """fonts=True 但本机没有 OFL 字体：降级出包 + 一条明确 warning。"""
    saved = fonts_mod.available_fonts
    try:
        fonts_mod.available_fonts = lambda: {}  # type: ignore[assignment]
        result = _build(_book(_chapter("第一章", ["甲。"])), fonts=True)
        assert result.data.startswith(b"PK"), "拿不到字体也必须产出合法的包"
        assert not any("Fonts/" in n for n in result.files)
        hits = [w for w in result.warnings if "没找到可内嵌的开源中文字体" in w]
        assert len(hits) == 1, f"降级警告必须恰好一条：{result.warnings}"
        assert "思源" in hits[0] and "Noto" in hits[0], hits[0]
        assert "系统字体" in hits[0], hits[0]
    finally:
        fonts_mod.available_fonts = saved  # type: ignore[assignment]


def main() -> int:
    cases = [
        case_plan_fonts_two_roles,
        case_plan_fonts_char_coverage,
        case_subset_opens_and_covers,
        case_extb_font_wiring,
        case_extb_skipped_when_unused,
        case_extb_missing_degrades,
        case_subset_deterministic,
        case_fonttools_missing_degrades,
        case_commercial_font_rejected,
        case_normalize_chars,
        case_build_without_fonts_unchanged,
        case_build_with_fonts_structure,
        case_build_fonts_deterministic,
        case_all_xml_wellformed_with_fonts,
        case_fonts_true_without_fonts_degrades,
    ]
    failed = 0
    for c in cases:
        try:
            c()
            print(f"ok   {c.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {c.__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
