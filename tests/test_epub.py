"""epub.py 的测试：章节树 → EPUB3 包。

照 tests/test_render.py 的风格：不用 pytest，一个 case 一个函数，纯 assert，
直接 python tests/test_epub.py 跑。只断言契约（包结构、OPF 元数据、目录形态、
正文元素、确定性），不断言内部实现细节。

覆盖的边界：
- 包结构：mimetype 第一且 ZIP_STORED、内容逐字节精确、container 指向 content.opf；
- content.opf：dc 元数据、manifest 与 spine 双向一致、nav 的 properties；
- 目录：nav.xhtml 与 toc.ncx 条目数与标题一致，有卷时嵌套一级；
- 正文：一章一文件、段落顺序、heading/note 的元素形态；
- 脏文本：& < > 与控制字符、段内换行、空段、连续空行折叠、首尾空行丢弃；
- 封面：有图 / 无图 / 扩展名不认识 三种；
- 封底：有图才产一页（放在正文之后）、图坏了整页不产、注释标记给重排认；
- 确定性：两次打包字节完全相同（zip 时间戳与打包平台都固定）；
- 退化输入：0 章节、正文全空、单段 10000 字；
- write_epub：落盘后能被 zipfile 回读。

临时文件一律走 tempfile，不往项目目录里写东西。
"""

from __future__ import annotations

import re
import sys
import tempfile
import uuid
import zipfile
import io
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban import epub  # noqa: E402
from huoban.epub import build_epub, write_epub  # noqa: E402
from huoban.model import Book, Chapter, Paragraph  # noqa: E402

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "epub": "http://www.idpf.org/2007/ops",
}

FIXED_MODIFIED = "2026-10-24T00:00:00Z"


# ---------- 造数据与拆包的小工具 ----------


def _chapter(title: str, paragraphs=None, level: int = 1) -> Chapter:
    """造一章：paragraphs 里可以直接给字符串（默认正文段）。"""
    body = []
    for p in paragraphs or []:
        body.append(p if isinstance(p, Paragraph) else Paragraph(text=p))
    return Chapter(title=title, level=level, paragraphs=body)


def _book(*chapters: Chapter, title="测试书", author="某作者",
          source="X:/样本/测试书.txt", front=None, tail=None) -> Book:
    return Book(
        title=title,
        author=author,
        source=source,
        front_matter=list(front or []),
        chapters=list(chapters),
        tail_matter=list(tail or []),
    )


def _build(book: Book, **kwargs):
    """打包时一律钉死 modified，否则"确定性"根本无从谈起。"""
    kwargs.setdefault("modified", FIXED_MODIFIED)
    return build_epub(book, **kwargs)


def _zip(result) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(result.data))


def _read(result, name: str) -> bytes:
    with _zip(result) as zf:
        return zf.read(name)


def _xml(result, name: str):
    """把包内某个 XML 条目解析成 ElementTree 根元素。"""
    return ET.fromstring(_read(result, name))


def _text(result, name: str) -> str:
    return _read(result, name).decode("utf-8")


def _chapter_roots(result) -> list:
    names = [n for n in result.files if n.startswith("OEBPS/Text/chap")]
    return [ET.fromstring(_read(result, n)) for n in names]


def _tags(root, find="xhtml:body") -> list[str]:
    """body 的直接子元素标签名（去掉命名空间前缀）。"""
    body = root.find(find, NS)
    assert body is not None, "找不到 body"
    return [c.tag.split("}")[1] for c in body]


def _paras(root) -> list[str]:
    return ["".join(p.itertext()) for p in root.findall(".//xhtml:p", NS)]


def _seps(root) -> list:
    return [d for d in root.findall(".//xhtml:div", NS) if d.get("class") == "sep"]


def _metas(result) -> dict:
    """content.opf 的 <meta>：property 或 name 作键，content 或文本作值。"""
    opf = _xml(result, "OEBPS/content.opf")
    meta = opf.find("opf:metadata", NS)
    assert meta is not None
    out = {}
    for m in meta.findall("opf:meta", NS):
        key = m.get("property") or m.get("name")
        if not key:
            continue
        out[key] = m.get("content") if m.get("content") is not None else (m.text or "")
    return out


def _ident(result) -> str:
    opf = _xml(result, "OEBPS/content.opf")
    el = opf.find("opf:metadata/dc:identifier", NS)
    assert el is not None, "缺少 dc:identifier"
    return el.text or ""


def _manifest(result) -> list:
    opf = _xml(result, "OEBPS/content.opf")
    assert opf.find("opf:manifest", NS) is not None
    return opf.findall("opf:manifest/opf:item", NS)


def _spine_idrefs(result) -> list[str]:
    opf = _xml(result, "OEBPS/content.opf")
    spine = opf.find("opf:spine", NS)
    assert spine is not None, "缺少 spine"
    return [r.get("idref") for r in spine.findall("opf:itemref", NS)]


# ---------- a) 包结构 ----------


def case_package_structure() -> None:
    """mimetype 第一个、未压缩、内容逐字节精确；条目顺序就是契约里的包结构。"""
    result = _build(_book(_chapter("第一章 起", ["甲。"])))
    assert result.files == [
        "mimetype",
        "META-INF/container.xml",
        "OEBPS/content.opf",
        "OEBPS/nav.xhtml",
        "OEBPS/toc.ncx",
        "OEBPS/Styles/main.css",
        "OEBPS/Text/cover.xhtml",
        "OEBPS/Text/chap0001.xhtml",
    ], result.files
    with _zip(result) as zf:
        infos = zf.infolist()
        assert [i.filename for i in infos] == result.files
        assert infos[0].filename == "mimetype", "mimetype 必须是第一个条目"
        assert infos[0].compress_type == zipfile.ZIP_STORED, "mimetype 必须不压缩"
        assert zf.read("mimetype") == b"application/epub+zip", "mimetype 内容必须精确且不带换行"
        assert zf.testzip() is None

    container = _xml(result, "META-INF/container.xml")
    rootfile = container.find(".//c:rootfile", NS)
    assert rootfile is not None, "container.xml 里没有 rootfile"
    assert rootfile.get("full-path") == "OEBPS/content.opf"
    assert rootfile.get("media-type") == "application/oebps-package+xml"


# ---------- b) content.opf ----------


def case_opf_metadata() -> None:
    """dc:title / dc:creator / dc:language / dc:identifier 与 generator 都写对了。"""
    result = _build(_book(_chapter("第一章", ["甲"]), title="测试书", author="某作者"))
    opf = _xml(result, "OEBPS/content.opf")
    assert opf.get("version") == "3.0", "必须是 EPUB3"
    assert opf.get("unique-identifier") == "book-id"

    meta = opf.find("opf:metadata", NS)
    assert meta is not None
    assert meta.find("dc:title", NS).text == "测试书"
    assert meta.find("dc:creator", NS).text == "某作者"
    assert meta.find("dc:language", NS).text == "zh-CN"

    ident = meta.find("dc:identifier", NS)
    assert ident.get("id") == "book-id", "dc:identifier 的 id 必须等于 unique-identifier"
    assert ident.text.startswith("urn:uuid:"), ident.text

    metas = _metas(result)
    assert metas.get("dcterms:modified") == FIXED_MODIFIED
    assert metas.get("generator") == "huoban"

    # spine 必须带 toc="ncx"、guide 得有封面/目录/正文三条
    spine = opf.find("opf:spine", NS)
    assert spine.get("toc") == "ncx"
    guide = opf.find("opf:guide", NS)
    assert guide is not None, "老阅读器要靠 guide 找到封面与第一页正文"
    types = [r.get("type") for r in guide.findall("opf:reference", NS)]
    assert "cover" in types and "toc" in types and "text" in types, types


def case_manifest_spine_consistent() -> None:
    """manifest 与 spine 双向一致：spine 里的 idref 都在 manifest、xhtml 都在 spine 里。

    例外是导航文档：nav.xhtml 是 xhtml，但它按设计不进 spine（见 case_nav_not_in_spine），
    所以这里按 properties="nav" 豁免。
    """
    result = _build(_book(*[_chapter(f"第{i}章", ["正文"]) for i in (1, 2, 3)]))
    items = _manifest(result)
    by_id = {i.get("id"): i for i in items}
    assert len(by_id) == len(items), "manifest 里的 id 必须唯一"

    idrefs = _spine_idrefs(result)
    for idref in idrefs:
        assert idref in by_id, f"spine 里的 {idref} 不在 manifest"

    for item in items:
        if item.get("media-type") == "application/xhtml+xml" and "nav" not in (item.get("properties") or ""):
            assert item.get("id") in idrefs, f"{item.get('href')} 不在 spine 里"
        assert f"OEBPS/{item.get('href')}" in result.files, f"manifest 里的 {item.get('href')} 包里没有"

    navs = [i for i in items if i.get("properties") == "nav"]
    assert len(navs) == 1 and navs[0].get("href") == "nav.xhtml", "nav 条目必须带 properties=nav"
    assert idrefs == ["cover", "chap0001", "chap0002", "chap0003"], idrefs


def case_nav_not_in_spine() -> None:
    """导航文档不进 spine：读者翻页时不该先过一页纯链接的目录（2026-11-06 试用反馈）。

    三条一起钉住，少一条就会改错方向：
    1. spine 里没有 nav，且第一页就是封面；
    2. nav.xhtml 仍然在包里、manifest 里带 properties="nav" —— 去掉的是"占一页正文"，
       不是"去掉导航文档"（EPUB3 要求包内有且仅有一份导航文档）；
    3. guide 的 type="toc" 仍然指向 nav.xhtml —— 老阅读器靠它找到目录。
    """
    result = _build(_book(*[_chapter(f"第{i}章", ["正文"]) for i in (1, 2, 3)]))
    idrefs = _spine_idrefs(result)
    assert "nav" not in idrefs, f"导航文档不能出现在 spine 里：{idrefs}"
    assert idrefs[0] == "cover", f"第一页必须是封面：{idrefs}"
    assert "OEBPS/nav.xhtml" in result.files, "nav.xhtml 必须仍在包里"
    navs = [i for i in _manifest(result) if i.get("properties") == "nav"]
    assert len(navs) == 1 and navs[0].get("href") == "nav.xhtml", navs

    opf = _xml(result, "OEBPS/content.opf")
    guide = opf.find("opf:guide", NS)
    toc_ref = [r for r in guide.findall("opf:reference", NS) if r.get("type") == "toc"]
    assert toc_ref and toc_ref[0].get("href") == "nav.xhtml", "guide 的目录引用不能跟着删"


# ---------- c) 目录 ----------


def case_nav_and_ncx_flat() -> None:
    """没有卷的书：nav 与 ncx 的条目数都等于章节数、标题一致、目录平铺。"""
    titles = ["第一章 起", "第二章 承", "第三章 转"]
    result = _build(_book(*[_chapter(t, ["正文"]) for t in titles]))

    nav = _xml(result, "OEBPS/nav.xhtml")
    nav_el = nav.find(".//xhtml:nav", NS)
    assert nav_el is not None, "nav.xhtml 里没有 <nav>"
    assert nav_el.get("{http://www.idpf.org/2007/ops}type") == "toc"
    anchors = nav_el.findall(".//xhtml:a", NS)
    assert ["".join(a.itertext()) for a in anchors] == titles
    assert [a.get("href") for a in anchors] == ["Text/chap0001.xhtml",
                                                "Text/chap0002.xhtml",
                                                "Text/chap0003.xhtml"]
    assert len(nav_el.findall(".//xhtml:ol", NS)) == 1, "没有卷时目录必须平铺，不能有多余嵌套"

    ncx = _xml(result, "OEBPS/toc.ncx")
    labels = ["".join(t.itertext()) for t in ncx.findall(".//ncx:navLabel/ncx:text", NS)]
    assert labels == titles
    points = ncx.findall(".//ncx:navPoint", NS)
    assert len(points) == len(titles), f"ncx 条目数 {len(points)} 与章节数 {len(titles)} 不一致"
    for i, p in enumerate(points, start=1):
        assert p.get("playOrder") == str(i), "playOrder 必须按文档顺序连续递增"
        assert p.find("ncx:content", NS).get("src") == f"Text/chap{i:04d}.xhtml"
    head = {m.get("name"): m.get("content") for m in ncx.findall(".//ncx:meta", NS)}
    assert head.get("dtb:uid") == _ident(result)
    assert head.get("dtb:depth") == "1", "平铺目录的 depth 是 1"


def case_toc_nested_with_volume() -> None:
    """有卷的书：level=2 的卷做父节点，卷下的章嵌套一级；卷标题用 h1.vol。"""
    book = _book(
        _chapter("楔子", ["开场。"]),
        _chapter("第一卷 上", [], level=2),
        _chapter("第一章 一", ["甲"]),
        _chapter("第二章 二", ["乙"]),
        _chapter("第二卷 下", [], level=2),
        _chapter("第三章 三", ["丙"]),
    )
    result = _build(book)
    order = ["楔子", "第一卷 上", "第一章 一", "第二章 二", "第二卷 下", "第三章 三"]

    nav = _xml(result, "OEBPS/nav.xhtml")
    nav_el = nav.find(".//xhtml:nav", NS)
    anchors = nav_el.findall(".//xhtml:a", NS)
    assert len(anchors) == len(order), "目录条目数必须等于章节数"
    assert ["".join(a.itertext()) for a in anchors] == order

    top = nav_el.find("xhtml:ol", NS)
    lis = top.findall("xhtml:li", NS)
    assert len(lis) == 3, "顶层：楔子 + 两卷"
    nested = lis[1].findall("xhtml:ol", NS)
    assert len(nested) == 1, "卷下的章必须是子 ol"
    assert len(nested[0].findall("xhtml:li", NS)) == 2
    # 卷行带 class="vol"（CSS 里加粗 + 上方留白），章行不带 —— 目录里
    # "卷是结构、章是内容"这件事必须能被样式区分开，不能只靠缩进。
    assert [li.get("class") for li in lis] == [None, "vol", "vol"]

    ncx = _xml(result, "OEBPS/toc.ncx")
    points = ncx.findall(".//ncx:navPoint", NS)
    assert len(points) == len(order)
    tops = ncx.findall(".//ncx:navMap/ncx:navPoint", NS)
    assert len(tops) == 3
    assert len(tops[1].findall("ncx:navPoint", NS)) == 2, "ncx 里卷下的章要嵌成子 navPoint"
    assert [t.get("playOrder") for t in points] == [str(i) for i in range(1, 7)], "playOrder 不能按层级重置"

    vol = ET.fromstring(_read(result, "OEBPS/Text/chap0002.xhtml"))
    h1 = vol.find(".//xhtml:h1", NS)
    assert (h1.get("class") or "") == "vol", "卷标题必须用 h1 class=vol"


# ---------- d) 正文 ----------


def case_chapter_files() -> None:
    """一章一个文件、四位数编号从 1 开始、段落数与顺序和章节树一致。"""
    book = _book(
        _chapter("第一章 甲", ["一。", "二。", "三。"]),
        _chapter("第二章 乙", ["四。"]),
    )
    result = _build(book)
    assert result.chapters == 2
    assert result.paragraphs == 4
    chaps = [n for n in result.files if n.startswith("OEBPS/Text/chap")]
    assert chaps == ["OEBPS/Text/chap0001.xhtml", "OEBPS/Text/chap0002.xhtml"]

    roots = _chapter_roots(result)
    for root, title, expect in zip(roots, ["第一章 甲", "第二章 乙"],
                                  [["一。", "二。", "三。"], ["四。"]]):
        h1 = root.find(".//xhtml:h1", NS)
        assert h1 is not None, "章节文件里必须有 <h1> 标题"
        assert "".join(h1.itertext()) == title, "h1 文本必须等于 Chapter.title"
        assert _paras(root) == expect, "段落顺序/数量不对"


def case_heading_and_note() -> None:
    """章内小标题 → h2.sub，注释 → p.note，正文 → p。"""
    book = _book(_chapter("第一章", [
        Paragraph(text="正文一。"),
        Paragraph(text="小节标题", kind="heading"),
        Paragraph(text="按语。", kind="note"),
    ]))
    result = _build(book)
    assert result.paragraphs == 3
    root = _chapter_roots(result)[0]
    h2 = root.find(".//xhtml:h2", NS)
    assert h2 is not None, "heading 段必须渲染成 h2"
    assert (h2.get("class") or "") == "sub"
    assert "".join(h2.itertext()) == "小节标题"
    notes = [p for p in root.findall(".//xhtml:p", NS) if p.get("class") == "note"]
    assert len(notes) == 1 and "".join(notes[0].itertext()) == "按语。"
    plain = [p for p in root.findall(".//xhtml:p", NS) if p.get("class") is None]
    assert ["".join(p.itertext()) for p in plain] == ["正文一。"]


def case_verse_paragraph() -> None:
    """诗词/韵文 → p.verse（居中、不缩进、楷体），与正文 p 区分开。

    为什么要有独立元素而不是靠正文段落里塞样式：韵文的排版语义与正文
    相反（正文首行缩进两端对齐，韵文居中不缩进），同一类元素上共用
    text-indent 会让两者的 CSS 互相打架。
    """
    book = _book(_chapter("第一章", [
        Paragraph(text="正文一。"),
        Paragraph(text="黑发难留，朱颜易变。", kind="verse"),
        Paragraph(text="名消利息，一派落花风。", kind="verse"),
    ]))
    result = _build(book)
    assert result.paragraphs == 3
    root = _chapter_roots(result)[0]
    verses = [p for p in root.findall(".//xhtml:p", NS) if p.get("class") == "verse"]
    assert len(verses) == 2, f"应有两个 p.verse，实际 {len(verses)}"
    assert "".join(verses[0].itertext()) == "黑发难留，朱颜易变。"
    plain = [p for p in root.findall(".//xhtml:p", NS) if p.get("class") is None]
    assert ["".join(p.itertext()) for p in plain] == ["正文一。"]


def case_sep_paragraph() -> None:
    """场景分隔线 → 一条 <hr>，原来的装饰字符一个字都不印。

    真实来源：一本 44 章的小说里有 61 行"＊＊＊＊＊＊"。它们是原作者的场景
    分隔，但 match_chapter 的"分隔线"家族只认半角 *，于是整批落进正文，
    在阅读器里变成"缩进 + 两端对齐的一串星号"，看着像乱码。

    用户后来提的判据：成品里不该留"纯文本分割线" —— 参考精排书（《死灵之书》
    用 hr + border-top:1px）的做法，分隔线统一渲成一条细横线。
    """
    book = _book(_chapter("第一章", [
        Paragraph(text="正文一。"),
        Paragraph(text="＊＊＊＊＊＊", kind="sep"),
        Paragraph(text="正文二。"),
    ]))
    result = _build(book)
    root = _chapter_roots(result)[0]
    hrs = [h for h in root.findall(".//xhtml:hr", NS) if h.get("class") == "sep"]
    assert len(hrs) == 1, f"应有一个 hr.sep，实际 {len(hrs)}"
    # 字符不许印出来
    assert "＊＊＊＊＊＊" not in _read(result, "OEBPS/Text/chap0001.xhtml").decode("utf-8")
    plain = [p for p in root.findall(".//xhtml:p", NS) if p.get("class") is None]
    assert ["".join(p.itertext()) for p in plain] == ["正文一。", "正文二。"]


def case_dark_mode_no_hardcoded_color() -> None:
    """深色模式回归（用户看 epub 发现的问题）。

    真实 bug：p.verse 写死 color: #33302e，深色模式下阅读器注入深色前景，
    写死的深色字配深色底，整块诗词糊成一片。

    契约：**浅色媒体查询之前**不允许出现任何 color 声明；诗词、章标题、
    注释、目录链接全都不能写死颜色，一切跟随阅读器。
    """
    result = _build(_book(_chapter("第一章", [
        Paragraph(text="正文一。"),
        Paragraph(text="黑发难留，朱颜易变。", kind="verse"),
    ])))
    css = _text(result, "OEBPS/Styles/main.css")
    light_at = css.index("@media (prefers-color-scheme: light)")
    head = css[:light_at]
    assert "color:" not in head, "常规规则里写死了颜色，深色模式会盖过阅读器注入的前景色"
    # 浅色块里必须还能找到那些层次色（浅色下排版层次不变）
    tail = css[light_at:]
    for needle in ("#2f5d62", "#33302e", "#6b6b6b"):
        assert needle in tail, f"浅色块缺少 {needle}"
    # 章节页里诗词段不能带 style 属性（内联样式同样会盖过深色模式）
    root = _chapter_roots(result)[0]
    for p in root.findall(".//xhtml:p", NS):
        assert p.get("style") is None, "段落上不允许有内联 style"


def case_css_contract() -> None:
    """样式表：契约点名的规则都在，而且绝不内嵌字体。"""
    result = _build(_book(_chapter("第一章", ["甲。"])))
    css = _text(result, "OEBPS/Styles/main.css")
    required = [
        "html, body", "margin: 0", "padding: 0",
        "Songti SC", "STSong", "宋体", "SimSun", "Noto Serif CJK SC", "serif",
        "Kaiti SC", "STKaiti", "楷体", "KaiTi",
        "text-indent: 2em", "text-align: justify", "text-justify: inter-ideograph",
        "line-height: 1.8", "font-size: 1.35em", "margin: 2.6em 0 2em",
        "h1.vol", "font-size: 1.6em", "margin: 4em 0 3em",
        "h2.sub", "p.note", "font-size: 0.9em",
        "p.verse", "line-height: 1.55",
        "hr.sep", "border-top: 1px solid",
        "div.sep", "height: 1.1em",
    ]
    for needle in required:
        assert needle in css, f"CSS 缺少 {needle!r}"
    assert "@font-face" not in css and ".ttf" not in css, "不允许内嵌字体（只引用系统字体）"
    assert "page-break-after" not in css and "页眉" not in css, "不做页眉页脚式的装饰"
    # 深色模式契约：层次色只许出现在 prefers-color-scheme: light 块里，
    # 常规规则里一个 color 都不许写 —— 否则会盖过阅读器注入的前景色，
    # 在深色底上变成黑字配黑底（诗词 p.verse 写死 #33302e 那一版就是这样）。
    light_at = css.index("@media (prefers-color-scheme: light)")
    head = css[:light_at]
    assert "color:" not in head, (
        f"浅色块之前的常规规则里有写死的 color，深色模式会糊：{head[head.index('color:') - 80:head.index('color:') + 40]!r}"
    )
    for needle in ("#2f5d62", "#33302e", "#6b6b6b", "#5a676b", "#1a1a1a"):
        assert needle in css[light_at:], f"浅色块缺少层次色 {needle}"


# ---------- 脏文本 ----------


def case_escaping_and_control_chars() -> None:
    """& < > 必须转义；XML 非法控制字符必须剔除，否则整章打不开。"""
    book = _book(_chapter("第一章 & <标题>", [
        Paragraph(text="甲 & 乙 <丙> 丁"),
        Paragraph(text="坏字符\x00\x07\x0b\x0c\x1f之后"),
    ]))
    result = _build(book)
    assert result.warnings == [], result.warnings
    raw = _read(result, "OEBPS/Text/chap0001.xhtml")
    text = raw.decode("utf-8")
    for bad in ("\x00", "\x07", "\x0b", "\x0c", "\x1f"):
        assert bad not in text, f"非法控制字符 {bad!r} 漏进了 xhtml"
    assert "&amp;" in text and "&lt;丙&gt;" in text, "转义必须真的写进字节，不能靠解析器猜"

    root = ET.fromstring(raw)  # 解析不过就是 ParseError
    assert "".join(root.find(".//xhtml:h1", NS).itertext()) == "第一章 & <标题>"
    ps = _paras(root)
    assert ps[0] == "甲 & 乙 <丙> 丁"
    assert ps[1].startswith("坏字符") and ps[1].endswith("之后")


def case_multiline_paragraph_split() -> None:
    """段落 text 里有换行：按行拆成多个 <p>，绝不留裸换行在文本节点里。"""
    book = _book(_chapter("第一章", [Paragraph(text="第一行\n第二行\n\n第四行")]))
    result = _build(book)
    assert result.paragraphs == 3, "三行有内容的文字要拆成三个段落"
    root = _chapter_roots(result)[0]
    assert _paras(root) == ["第一行", "第二行", "第四行"]
    assert len(_seps(root)) == 1, "中间那行空行要变成 sep"
    for p in root.findall(".//xhtml:p", NS):
        assert "\n" not in (p.text or ""), "段落文本节点里不能有裸换行"


def case_blank_folding() -> None:
    """连续空行只出一个 sep；章节首尾空行直接丢；空白正文段当空行。"""
    book = _book(_chapter("第一章", [
        Paragraph(text="", kind="blank"),
        Paragraph(text="  ", kind="blank"),
        Paragraph(text="甲。"),
        Paragraph(text="", kind="blank"),
        Paragraph(text="", kind="blank"),
        Paragraph(text="   "),
        Paragraph(text="乙。"),
        Paragraph(text="", kind="blank"),
    ]))
    result = _build(book)
    assert result.paragraphs == 2, "空行不算段落"
    root = _chapter_roots(result)[0]
    assert len(_seps(root)) == 1, "连续空行必须折叠成一个 sep"
    assert _tags(root) == ["h1", "p", "div", "p"], "首尾空行不能输出"
    assert _paras(root) == ["甲。", "乙。"]


# ---------- e) 封面 ----------


def case_cover_image() -> None:
    """给了 cover_path：图片进包、manifest 标 cover-image、cover.xhtml 引用它。"""
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "封面.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
        result = _build(_book(_chapter("第一章", ["甲。"]), title="有封面的书"),
                        cover_path=str(img))

        assert "OEBPS/Images/cover.png" in result.files
        assert result.files.index("OEBPS/Images/cover.png") < result.files.index("OEBPS/Text/cover.xhtml")

        images = [i for i in _manifest(result) if i.get("properties") == "cover-image"]
        assert len(images) == 1, "manifest 里必须恰好一条 cover-image"
        assert images[0].get("href") == "Images/cover.png"
        assert images[0].get("media-type") == "image/png"
        assert _metas(result).get("cover") == "cover-image", "老阅读器靠 <meta name=cover> 找封面"

        cover = _xml(result, "OEBPS/Text/cover.xhtml")
        image = cover.find(".//xhtml:img", NS)
        assert image is not None, "有封面时封面页必须是 <img>"
        assert image.get("src") == "../Images/cover.png", "封面页在 Text/ 下，路径要往上一级"
        # EPUB3 里 img 的 width 必须是整数像素；百分比是 HTML5 写法，epubcheck
        # 判 RSC-005。撑满页面交给 CSS 的 .cover img { width: 100% }。
        assert image.get("width") is None, "EPUB3 不允许 <img width='100%'>"
        body_el = cover.find(".//xhtml:body", NS)
        assert body_el is not None
        assert body_el.get("{%s}type" % NS["epub"]) == "cover", "封面页要标 epub:type=cover"
        with _zip(result) as zf:
            # 封面图一律 deflate：未压缩的 PNG 能压掉一大半，已压过的 jpg 只多几十字节。
            assert zf.getinfo("OEBPS/Images/cover.png").compress_type == zipfile.ZIP_DEFLATED

    # .jpeg 归一成 .jpg（阅读器对 .jpeg 的支持没那么稳）
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "a.jpeg"
        img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 40)
        result = _build(_book(_chapter("第一章", ["甲。"])), cover_path=str(img))
        assert "OEBPS/Images/cover.jpg" in result.files
        jpg = [i for i in _manifest(result) if i.get("properties") == "cover-image"][0]
        assert jpg.get("media-type") == "image/jpeg"


def case_cover_text_fallback() -> None:
    """没给 cover_path：不出图片，封面页用文字书名 + 作者。"""
    result = _build(_book(_chapter("第一章", ["甲。"]), title="无封面的书", author="某作者"))
    assert not [n for n in result.files if n.startswith("OEBPS/Images/")]
    assert not [i for i in _manifest(result) if i.get("properties") == "cover-image"]
    assert "cover" not in _metas(result)

    cover = _xml(result, "OEBPS/Text/cover.xhtml")
    title_el = cover.find(".//xhtml:h1[@class='cover-title']", NS)
    assert title_el is not None and "".join(title_el.itertext()) == "无封面的书"
    author_el = cover.find(".//xhtml:p[@class='cover-author']", NS)
    assert author_el is not None and "".join(author_el.itertext()) == "某作者"
    assert cover.find(".//xhtml:img", NS) is None


def case_cover_unusable_falls_back() -> None:
    """封面不可用（扩展名不认识 / 文件不存在）只报警告，不中断打包。"""
    with tempfile.TemporaryDirectory() as tmp:
        bmp = Path(tmp) / "cover.bmp"
        bmp.write_bytes(b"BM" + b"\x00" * 40)
        result = _build(_book(_chapter("第一章", ["甲。"])), cover_path=str(bmp))
        assert result.chapters == 1
        assert any("封面" in w for w in result.warnings), result.warnings
        assert not [n for n in result.files if n.startswith("OEBPS/Images/")]
        cover = _xml(result, "OEBPS/Text/cover.xhtml")
        assert cover.find(".//xhtml:h1[@class='cover-title']", NS) is not None

        missing = Path(tmp) / "没有这个文件.jpg"
        result2 = _build(_book(_chapter("第一章", ["甲。"])), cover_path=str(missing))
        assert result2.chapters == 1
        assert any("封面" in w for w in result2.warnings), result2.warnings


def case_back_cover_page() -> None:
    """给了 back_cover：封底页排在正文之后，封底图进包但**不**挂 cover-image。

    EPUB 里"封面图"只能有一张：封底图要是也标 properties="cover-image"，
    阅读器会在书架上把封底挑出来当封面。它只是一张普通图片。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cover = Path(tmp) / "封面.png"
        cover.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
        back = Path(tmp) / "封底.png"
        back.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x11" * 40)
        book = _book(_chapter("第一章", ["甲。"]), _chapter("第二章", ["乙。"]))
        result = _build(book, cover_path=str(cover), back_cover=str(back))

        # 1) 页与图都在包里；封底页是关键最后一条（翻页顺序 封面 → 正文 → 封底）
        assert "OEBPS/Images/back.png" in result.files, result.files
        assert result.files[-1] == "OEBPS/Text/back.xhtml", result.files[-1]
        assert result.files.index("OEBPS/Images/back.png") > result.files.index(
            "OEBPS/Images/cover.png"
        ), "契约顺序：封面图 → 封底图 → 章节内图片"

        # 2) spine 末尾多一个 back，封面仍在第一位
        idrefs = _spine_idrefs(result)
        assert idrefs == ["cover", "chap0001", "chap0002", "back"], idrefs

        # 3) manifest：back 页与 back-image 都在；cover-image 只有一条且属于封面
        items = {i.get("id"): i for i in _manifest(result)}
        assert items["back"].get("href") == "Text/back.xhtml"
        assert items["back"].get("media-type") == "application/xhtml+xml"
        assert items["back-image"].get("href") == "Images/back.png"
        assert items["back-image"].get("media-type") == "image/png"
        assert items["back-image"].get("properties") is None, "封底图不能标 cover-image"
        covers = [i for i in _manifest(result) if i.get("properties") == "cover-image"]
        assert [c.get("href") for c in covers] == ["Images/cover.png"], covers

        # 4) guide 里多了 type="back"（老阅读器靠它从书末定位）
        opf = _xml(result, "OEBPS/content.opf")
        refs = {
            r.get("type"): r.get("href")
            for r in opf.findall("opf:guide/opf:reference", NS)
        }
        assert refs.get("back") == "Text/back.xhtml", refs
        assert refs.get("cover") == "Text/cover.xhtml", refs

        # 5) 封底页本身：整页一张图 + 一条给重排认的注释标记，不带 epub:type
        back_xhtml = _xml(result, "OEBPS/Text/back.xhtml")
        img = back_xhtml.find(".//xhtml:img", NS)
        assert img is not None, "封底页必须引用那张图（否则重排会把它当孤儿图）"
        assert img.get("src") == "../Images/back.png", img.get("src")
        assert img.get("width") is None, "EPUB3 不允许 <img width='100%'>"
        body_el = back_xhtml.find(".//xhtml:body", NS)
        assert body_el is not None and body_el.get("class") == "back-body"
        assert body_el.get("{%s}type" % NS["epub"]) is None, "EPUB3 没有「封底」这个 epub:type"
        raw = _text(result, "OEBPS/Text/back.xhtml")
        assert "<!-- back-cover -->" in raw, "重排靠这条注释认封底页"
        # 封底上不许出现"由 txt2epub 生成"那句广告词（生成物刻意不留）
        assert "由 txt2epub 生成" not in raw

        # 6) CSS 里封底整页占满，且不引入字体文件
        css = _text(result, "OEBPS/Styles/main.css")
        assert "body.back-body" in css and ".back img" in css, css[-400:]

        # 7) 确定性：同参两遍逐字节相同（封底不能引入时间戳/随机）
        assert result.data == _build(book, cover_path=str(cover), back_cover=str(back)).data

        # 8) 不传 back_cover 时，包里一条 back 相关条目都没有（老调用方产物不变）
        plain = _build(book, cover_path=str(cover))
        assert not [n for n in plain.files if "back" in n], plain.files
        assert _spine_idrefs(plain) == ["cover", "chap0001", "chap0002"], plain.files

        # 9) 封底样式也得跟着走：不产封底页的书，main.css 里一行 .back 都不许有。
        #    这条不是洁癖 —— 封底是"可加可不加的一维"，样式表多 20 行死规则，
        #    不产封底的那一支就没法再跟自己的历史产物逐字节回归。
        plain_css = _text(plain, "OEBPS/Styles/main.css")
        for needle in ("body.back-body", ".back img", "\n.back {"):
            assert needle not in plain_css, f"没有封底页却带着封底样式 {needle!r}"
        assert css != plain_css, "有封底与没封底的样式表不该完全相同"
        # 占位符被替换成空串后不许把空行留在原地（不留痕才算真的"没加过这一维"）
        assert "\n\n\n" not in plain_css, "封底样式占位符留下了空行"


def case_back_cover_unusable_produces_no_page() -> None:
    """封底图坏了：整页不产（白纸不算封底），且警告说的是"封底"不是"封面"。

    与封面刻意相反：封面没图还能退回"书名 + 作者"的文字版，封底退回文字版
    就是一张白纸 —— 那不是封底，是多出来的一次翻页。所以这里断言产物与
    "根本没传 back_cover"逐字节相同，而不是"退回文字封底"。
    """
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "封底.png"
        fake.write_bytes("这不是图片".encode("utf-8"))
        book = _book(_chapter("第一章", ["甲。"]))
        result = _build(book, back_cover=str(fake))

        assert "OEBPS/Text/back.xhtml" not in result.files, result.files
        assert not [n for n in result.files if "back" in n], result.files
        assert any("封底" in w and "文件头对不上" in w for w in result.warnings), result.warnings
        assert any("这一版不生成封底页" in w for w in result.warnings), result.warnings
        # 降级方向：封底不出页，封面那条警告不该被子串"封面"误命中
        assert not any("封面" in w for w in result.warnings), result.warnings
        assert result.data == _build(book).data, "封底坏了必须与没传封底完全一致"

        # 文件不存在同样只警告
        result2 = _build(book, back_cover=str(Path(tmp) / "没有这个文件.jpg"))
        assert "OEBPS/Text/back.xhtml" not in result2.files
        assert any("封底" in w for w in result2.warnings), result2.warnings


# ---------- f) 元数据兜底 ----------


def case_title_author_fallback() -> None:
    """书名兜底成"未命名"；作者为空时整段不输出 dc:creator；信息页只写自家文案。"""
    result = _build(_book(_chapter("第一章", ["甲"]), title="", author="", source="X:/a.txt"))
    opf = _xml(result, "OEBPS/content.opf")
    meta = opf.find("opf:metadata", NS)
    assert meta.find("dc:title", NS).text == "未命名"
    assert meta.find("dc:creator", NS) is None, "作者为空时不能输出空的 dc:creator"
    assert "未命名" in "".join(_xml(result, "OEBPS/Text/cover.xhtml").itertext())

    # 显式传参覆盖 book 里的字段
    result2 = _build(_book(_chapter("第一章", ["甲"]), title="书内标题", author="书内作者"),
                     title="外部标题", author="外部作者")
    meta2 = _xml(result2, "OEBPS/content.opf").find("opf:metadata", NS)
    assert meta2.find("dc:title", NS).text == "外部标题"
    assert meta2.find("dc:creator", NS).text == "外部作者"

    # 2026-10-28 按试用反馈删掉了信息页（"生成的 EPUB 里不要有推广页"）。
    # 这条用例改成反向守卫：包里不能再出现 info.xhtml，也不能出现我们的落款。
    assert not [p for p in result2.files if p.endswith("info.xhtml")], \
        f"不该再有信息页：{result2.files}"
    for name in result2.files:
        if not name.endswith((".xhtml", ".ncx", ".opf", ".css")):
            continue
        text = _read(result2, name).decode("utf-8")
        assert "由 txt2epub 生成" not in text, f"{name} 里不该有落款"
        assert "来源文件：" not in text, f"{name} 里不该有来源文件行"


def case_identifier_and_modified() -> None:
    """identifier 用 uuid5 且可复算；modified 三种输入都归一成 YYYY-MM-DDTHH:MM:SSZ。"""
    book = _book(_chapter("第一章", ["甲"]), title="书A", source="D:/样本/书A.txt")
    first = _build(book)
    second = _build(book)
    assert _ident(first) == _ident(second), "同样的输入必须得到同样的 identifier"

    seed = "D:/样本/书A.txt\n书A"
    assert _ident(first) == "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, seed)), "identifier 种子变了"

    explicit = _build(book, identifier="urn:isbn:1234567890")
    assert _ident(explicit) == "urn:isbn:1234567890"

    other = _build(_book(_chapter("第一章", ["甲"]), title="书A", source="D:/样本/书B.txt"))
    assert _ident(other) != _ident(first), "来源路径必须参与种子"

    aware = _build(book, modified=datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc))
    assert _metas(aware).get("dcterms:modified") == "2026-10-24T12:00:00Z"
    naive = _build(book, modified=datetime(2026, 10, 24, 12, 0, 0))
    assert _metas(naive).get("dcterms:modified") == "2026-10-24T12:00:00Z", "无时区的 datetime 按 UTC 理解"
    auto = build_epub(book)  # 不传 modified：必须也是同一形状
    stamp = _metas(auto).get("dcterms:modified")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp or ""), stamp


# ---------- g) 确定性 ----------


def case_deterministic() -> None:
    """同样输入打两次，字节完全相同 —— zip 时间戳与打包平台都钉死了。"""
    book = _book(
        _chapter("第一章 甲", [Paragraph(text="甲 & 乙"), Paragraph(text="", kind="blank"),
                             Paragraph(text="丙")]),
        _chapter("第二章 乙", ["丁。"]),
    )
    a = _build(book)
    b = _build(book)
    assert a.data == b.data, "两次打包的字节必须完全相等"
    assert a.files == b.files
    assert a.warnings == b.warnings

    with _zip(a) as zf:
        for info in zf.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0), f"{info.filename} 的时间戳没固定"
            assert info.create_system == 0, f"{info.filename} 的 create_system 没固定"

    changed = build_epub(book, modified="2026-10-25T00:00:00Z")
    assert changed.data != a.data, "换了 modified 字节却没变，说明它没写进去"


# ---------- h) 退化输入 ----------


def case_degenerate_books() -> None:
    """0 章节、正文全空、单段 10000 字都不抛异常，且包仍然可用。"""
    empty = _build(Book(title="空书"))
    assert empty.chapters == 0 and empty.paragraphs == 0
    assert empty.warnings, "0 章节必须在 warnings 里说明"
    assert not [n for n in empty.files if "chap" in n]
    nav = _xml(empty, "OEBPS/nav.xhtml")
    assert nav.find(".//xhtml:nav", NS) is not None, "空目录页也要在"
    # EPUB3 要求导航的 <ol> 里至少一个 <li>，空 <ol></ol> 是硬性违规。
    # 所以没有章节时放一条占位项（信息页删掉后改指封面页），而不是留空。
    links = nav.findall(".//xhtml:a", NS)
    assert len(links) == 1, "空目录必须有一个占位项（ol 不能为空）"
    assert links[0].get("href") == "Text/cover.xhtml"
    ncx = _xml(empty, "OEBPS/toc.ncx")
    assert ncx.findall(".//ncx:navPoint", NS) == []
    assert _spine_idrefs(empty) == ["cover"], "0 章节时 spine 只剩封面"

    blank = _build(_book(_chapter("第一章", [Paragraph(text="", kind="blank"),
                                            Paragraph(text="   ")])))
    assert blank.chapters == 1 and blank.paragraphs == 0
    blank_root = _chapter_roots(blank)[0]
    assert blank_root.find(".//xhtml:h1", NS) is not None, "正文全空也要有标题"
    assert _paras(blank_root) == []

    big = _build(_book(_chapter("第一章", ["长" * 10000])))
    assert big.paragraphs == 1
    big_root = _chapter_roots(big)[0]
    assert len(_paras(big_root)[0]) == 10000

    minimal = _build(Book())
    assert minimal.chapters == 0


def case_front_tail_matter() -> None:
    """前言折进第一章标题之前、后记折进最后一章末尾，不额外造文件。"""
    book = _book(
        _chapter("第一章", ["甲。"]),
        _chapter("第二章", ["乙。"]),
        front=[Paragraph(text="前言。")],
        tail=[Paragraph(text="后记。")],
    )
    result = _build(book)
    assert not [n for n in result.files if "front" in n or "tail" in n]

    first = _chapter_roots(result)[0]
    assert _tags(first) == ["p", "h1", "p"], "前言必须在章标题之前"
    assert _paras(first) == ["前言。", "甲。"]

    last = _chapter_roots(result)[1]
    assert _paras(last) == ["乙。", "后记。"]
    assert result.paragraphs == 4


# ---------- i) 落盘 ----------


def case_write_epub_roundtrip() -> None:
    """write_epub 落盘后能被 zipfile 回读，父目录不存在时自动建。"""
    book = _book(_chapter("第一章", ["甲 & 乙"]))
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "新目录" / "书.epub"
        result = write_epub(book, target, modified=FIXED_MODIFIED)
        assert target.is_file()
        assert target.read_bytes() == result.data, "落盘内容必须与返回值一致"
        with zipfile.ZipFile(target) as zf:
            assert zf.namelist() == result.files
            assert zf.read("mimetype") == b"application/epub+zip"
            assert zf.testzip() is None
            for name in zf.namelist():
                if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                    ET.fromstring(zf.read(name))

    with tempfile.TemporaryDirectory() as tmp:
        written = write_epub(book, Path(tmp) / "a.epub", modified=FIXED_MODIFIED)
        built = build_epub(book, modified=FIXED_MODIFIED)
        assert written.data == built.data
        assert written.files == built.files
        assert written.chapters == built.chapters
        assert written.paragraphs == built.paragraphs
        assert written.warnings == built.warnings


def case_all_xml_wellformed() -> None:
    """包里每个 xhtml / opf / ncx / container 都是格式良好的 XML —— 能干开靠它。"""
    book = _book(
        _chapter("第一章 & <测试>", [
            Paragraph(text="正文 \x00 & <>\x0c"),
            Paragraph(text="  小节", kind="heading"),
            Paragraph(text="注", kind="note"),
        ]),
        _chapter("第一卷 卷名", [], level=2),
    )
    result = _build(book)
    assert result.warnings == [], result.warnings
    with _zip(result) as zf:
        for name in zf.namelist():
            if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ET.fromstring(zf.read(name))
    # 每章标题（去首尾空白后）必须与 Chapter.title 一致
    roots = _chapter_roots(result)
    for root, ch in zip(roots, book.chapters):
        h1 = root.find(".//xhtml:h1", NS)
        assert "".join(h1.itertext()) == ch.title.strip()




# ---------- j) 复核提出的脏元数据与层级越界 ----------


def case_metadata_dirty_chars() -> None:
    """书名/作者里的非法 XML 字符必须被剔除，且不许因孤立代理项崩掉。

    这条来自只读复核：章节正文早就走 _sanitize 了，元数据却只过 _text_of。
    encoding.py 用 errors="replace" 解码，NUL 不是解码错误，会原样留在
    书名里 —— 于是 content.opf 不良构，阅读器直接判"文件已损坏"。
    """
    book = Book(
        title="书\x00名",
        author="某\x07作者",
        source="X:/样本/\x01脏书名.txt",
        chapters=[_chapter("第一章", ["甲。"])],
    )
    result = _build(book)
    assert result.warnings == [], result.warnings

    with _zip(result) as zf:
        for name in zf.namelist():
            if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ET.fromstring(zf.read(name))  # 骨架必须都能解析

    opf = _xml(result, "OEBPS/content.opf")
    title_el = opf.find("opf:metadata/dc:title", NS)
    assert (title_el.text or "") == "书名", "非法字符要剔除，不能原样写进 opf"
    creator = opf.find("opf:metadata/dc:creator", NS)
    assert (creator.text or "") == "某作者"
    # 孤立代理项进 uuid5 会抛 UnicodeEncodeError —— 这里必须能算出一个合法 id。
    assert _ident(result).startswith("urn:uuid:")
    cover = _xml(result, "OEBPS/Text/cover.xhtml")
    assert "书名" in "".join(cover.itertext())


def case_level_three_treated_as_volume() -> None:
    """level>=3 的章节按卷处理，目录与正文用同一个判据。

    上一版的 bug：目录用 >=2 判卷、正文只认 1/2，level=3 的章节在目录里
    是容器、在正文里是叶子 —— 它后面的 C 章会被挂到它底下，点目录跳层级就错。
    """
    book = _book(
        _chapter("甲章", ["一。"]),
        _chapter("怪章", ["二。"], level=3),
        _chapter("丙章", ["三。"]),
    )
    result = _build(book)
    # 正文：level=3 用 h1.vol（卷级样式）
    roots = _chapter_roots(result)
    assert roots[1].find(".//xhtml:h1[@class='vol']", NS) is not None, "level=3 要按卷排版"
    assert roots[2].find(".//xhtml:h1[@class='vol']", NS) is None, "level=1 不能带 vol"

    # 目录：顶层 2 项（甲章 + 怪章），丙章挂在怪章下面
    nav = _xml(result, "OEBPS/nav.xhtml")
    top = nav.find(".//xhtml:nav/xhtml:ol", NS)
    lis = top.findall("xhtml:li", NS)
    assert len(lis) == 2, "丙章不该再当顶层项"
    nested = lis[1].findall("xhtml:ol", NS)
    assert len(nested) == 1 and len(nested[0].findall("xhtml:li", NS)) == 1
    assert "丙章" in "".join(nested[0].itertext())

    # 必须明确警告过层级越界，不许静默归并
    assert any("level=3" in w for w in result.warnings), result.warnings


def case_no_chapters_front_matter_kept() -> None:
    """0 章节但有前言后记：正文必须渲成一页，绝不能只报警告就丢掉。

    上一版直接 return，包里只剩 8 个壳条目 —— 用户拿到一本翻不开的白书。
    """
    book = Book(
        title="无章节",
        front_matter=[Paragraph(text="前言第一段。"), Paragraph(text="前言第二段。")],
        tail_matter=[Paragraph(text="后记。")],
    )
    result = _build(book)
    assert result.chapters == 1, "前言后记要合成一页正文"
    assert result.paragraphs == 3
    roots = _chapter_roots(result)
    assert len(roots) == 1
    assert _paras(roots[0]) == ["前言第一段。", "前言第二段。", "后记。"]
    assert any("没有切出章节" in w for w in result.warnings), result.warnings
    spine = _spine_idrefs(result)
    assert len(spine) == 2, f"封面/正文两页，实际 {spine}"


def case_cover_bytes_validated() -> None:
    """扩展名说是 png、内容却是文本：退回纯文字封面，别把裂图塞进书里。"""
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "封面.png"
        fake.write_bytes("这不是图片".encode("utf-8"))
        result = _build(_book(_chapter("第一章", ["甲。"])), cover_path=str(fake))
        assert not [n for n in result.files if n.startswith("OEBPS/Images/")], "坏图不能进包"
        assert not [i for i in _manifest(result) if i.get("properties") == "cover-image"]
        assert any("文件头对不上" in w for w in result.warnings), result.warnings
        cover = _xml(result, "OEBPS/Text/cover.xhtml")
        assert cover.find(".//xhtml:img", NS) is None
        assert cover.find(".//xhtml:h1[@class='cover-title']", NS) is not None


def case_chapter_images_are_read_whole_at_zip_time() -> None:
    """章节插图走"先验文件头、拼包时才读整张"这条路（G6 导入的书全靠它）。

    三件事都得钉住，任何一件写错都是静默坏包：
    - 进包的是**整张文件的字节**，不是验头时读过的那 8 个字节；
    - 只有文件头对得上的才进包，坏图与缺图汇总成一条警告，不让整本打包失败；
    - 读取时机推迟了，字节确定性不能跟着推迟 —— 同输入打两遍必须逐字节相同。
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "images"
        d.mkdir()
        good = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 20
        (d / "a.png").write_bytes(good)
        (d / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x11" * 500)
        (d / "lie.png").write_bytes(b"not an image at all")
        book = _book(
            _chapter(
                "第一章",
                [
                    "正文一段。",
                    Paragraph(text="a.png", kind="image"),
                    Paragraph(text="b.jpg", kind="image"),
                    Paragraph(text="lie.png", kind="image"),
                    Paragraph(text="gone.png", kind="image"),
                ],
            )
        )
        result = _build(book, image_dir=d)

        assert "OEBPS/Images/a.png" in result.files, result.files
        assert "OEBPS/Images/b.jpg" in result.files
        assert "OEBPS/Images/lie.png" not in result.files, "文件头不对的图不该进包"
        assert "OEBPS/Images/gone.png" not in result.files, "盘上没有的图不该进包"
        assert _read(result, "OEBPS/Images/a.png") == good, "进包的必须是整张图的字节"

        joined = " ".join(result.warnings)
        assert "2 张插图没能读进来" in joined, result.warnings
        assert joined.count("没能读进来") == 1, "坏图该汇总成一条，不该一人一行"

        # 图片段不该在正文里留下裂图：引用的名字必须是真正进了包的那张
        chap = [n for n in result.files if n.startswith("OEBPS/Text/chap")][0]
        body = _text(result, chap)
        assert "lie.png" not in body and "gone.png" not in body, "没进包的图不该被引用"

        assert _build(book, image_dir=d).data == result.data, "推迟读取不能破坏确定性"


def case_modified_string_and_invalid() -> None:
    """modified 是字符串时：日期补时间、"T"分隔归一成 UTC；认不出来就直接报错。"""
    book = _book(_chapter("第一章", ["甲。"]))
    a = build_epub(book, modified="2026-10-24")
    assert b"2026-10-24T00:00:00Z" in _read(a, "OEBPS/content.opf")

    b = build_epub(book, modified="2026-10-24 08:30:00")
    assert b"2026-10-24T08:30:00Z" in _read(b, "OEBPS/content.opf")

    for bad in ("昨天", "2026/10/24", ""):
        try:
            build_epub(book, modified=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"modified={bad!r} 应该当场报错，不该悄悄写出坏元数据")

    # 固定 modified 时两次打包逐字节相同（确定性）
    assert build_epub(book, modified=FIXED_MODIFIED).data == \
           build_epub(book, modified=FIXED_MODIFIED).data


# ---------- k) 成品观感：分页、留白、注释区、卷标题页 ----------


def case_page_break_and_spacing() -> None:
    """分页与留白：章标题另起一页、首个元素不重复分页、章末留白、目录卷行加粗。

    为什么"首个元素不重复分页"要单独立一条：每章已经是独立的 xhtml 文件，
    多数阅读器会在 spine 项之间自动分页。若再对文档第一个 h1 声明
    page-break-before: always，WebKit 系的引擎会先吐一张空白页 ——
    每章前面多一张白纸，这正是"不像正经电子书"最刺眼的一种。
    """
    result = _build(_book(
        _chapter("第一章", ["甲。"]),
        _chapter("第一卷 卷名", [], level=2),
    ))
    css = _text(result, "OEBPS/Styles/main.css")

    # 章标题另起一页（两种写法都写，老引擎认 page-break-*）
    assert "break-before: page" in css, "章标题必须另起一页"
    assert "page-break-before: always" in css, "老引擎只认 page-break-before"

    # 但文档里的第一个元素要覆盖成 auto
    reset_at = css.index("h1:first-child")
    reset = css[reset_at:reset_at + 320]
    assert "break-before: auto" in reset, "首个元素不许再强制分页（会多出空白页）"
    assert "page-break-before: auto" in reset
    # 卷标题页自己也是文档首个元素 —— 这条覆盖必须比 h1.vol 更具体才生效
    assert "h1.vol:first-child" in css

    # 章末留白：正文最后一页不能顶着屏幕底边
    assert "padding-bottom: 2.5em" in css, "章末要留白"

    # 目录：卷行加粗 + 上方留白（缩进只说明层级，加粗才看得出"这是卷"）
    assert "nav li.vol" in css
    vol_at = css.index("nav li.vol")
    assert "font-weight: bold" in css[vol_at:vol_at + 200]

    # 韵文块与正文之间拉开一档，块内仍紧
    assert "p:not(.verse) + p.verse" in css and "p.verse + p:not(.verse)" in css

    # 契约没破：仍然不做页眉页脚式的装饰
    assert "page-break-after" not in css


def case_notes_grouped() -> None:
    """连续的 note 段成组渲进 div.notes；中间隔正文就断组。"""
    book = _book(_chapter("第一章", [
        Paragraph(text="正文一。"),
        Paragraph(text="注一。", kind="note"),
        Paragraph(text="注二。", kind="note"),
        Paragraph(text="正文二。"),
        Paragraph(text="注三。", kind="note"),
    ]))
    result = _build(book)
    assert result.paragraphs == 5
    root = _chapter_roots(result)[0]

    blocks = [d for d in root.findall(".//xhtml:div", NS) if d.get("class") == "notes"]
    assert len(blocks) == 2, f"两处注释应成两组，实际 {len(blocks)}"
    assert ["".join(p.itertext()) for p in blocks[0].findall("xhtml:p", NS)] == ["注一。", "注二。"]
    assert ["".join(p.itertext()) for p in blocks[1].findall("xhtml:p", NS)] == ["注三。"]
    # body 的直接子元素里不该再出现裸的 p.note：它们必须都在 div.notes 里
    body = root.find("xhtml:body", NS)
    direct = [c for c in body if c.tag.split("}")[1] == "p"]
    assert [c.get("class") for c in direct] == [None, None], "note 段不许散在 body 下"

    css = _text(result, "OEBPS/Styles/main.css")
    assert ".notes {" in css and "border-top: 1px solid rgba(128, 128, 128, 0.45)" in css
    assert ".notes p.note:first-child" in css, "组内第一条注释不该再吃上边距"


def case_noteref_links_and_footnote_type() -> None:
    """正文里的引用渲成可点开的 noteref；注释段带 epub:type="footnote"。

    弹注靠两样东西：正文 `<a epub:type="noteref" href="#id">`，
    以及落点元素上的 `epub:type="footnote"`。缺后者时阅读器只当普通跳转，
    不会弹窗 —— 实测的"弹注全部失效"就是这两层同时缺。
    """
    book = _book(_chapter("第一章", [
        Paragraph(text="正文开始继续。", refs=[{"at": 4, "note": "n1", "label": "[1]"}],
                  anchor="nr1"),
        Paragraph(text="注一。", kind="note", anchor="n1"),
    ]))
    result = _build(book)
    root = _chapter_roots(result)[0]
    links = root.findall(".//xhtml:a", NS)
    assert len(links) == 1, links
    link = links[0]
    assert link.get("class") == "noteref"
    assert link.get("href") == "#n1"
    assert link.get("epub:type") or link.get(
        "{http://www.idpf.org/2007/ops}type"
    ) == "noteref", dict(link.attrib)
    assert "".join(link.itertext()) == "[1]"

    notes = [p for p in root.findall(".//xhtml:p", NS) if p.get("class") == "note"]
    assert len(notes) == 1
    assert notes[0].get("id") == "n1"
    assert notes[0].get("{http://www.idpf.org/2007/ops}type") == "footnote", dict(notes[0].attrib)


def case_noteref_cross_chapter_href() -> None:
    """注释在另一章时，链接必须写成跨文件路径，否则跳过去是个空锚点。"""
    book = _book(
        _chapter("第一章", [
            Paragraph(text="正文里引一处。", refs=[{"at": 2, "note": "n9", "label": "[9]"}])
        ]),
        _chapter("注释", [
            Paragraph(text="第九条注。", kind="note", anchor="n9"),
        ]),
    )
    result = _build(book)
    roots = _chapter_roots(result)
    link = roots[0].findall(".//xhtml:a", NS)[0]
    assert link.get("href") == "chap0002.xhtml#n9", link.get("href")


def case_noteref_dangling_becomes_plain_text() -> None:
    """落点全书都找不到时只留文字：一条点了没反应的死链接比不链接更糟。"""
    book = _book(_chapter("第一章", [
        Paragraph(text="正文里引一处。", refs=[{"at": 2, "note": "ghost", "label": "[7]"}])
    ]))
    result = _build(book)
    root = _chapter_roots(result)[0]
    assert root.findall(".//xhtml:a", NS) == []
    body_text = "".join(root.find("xhtml:body", NS).itertext())
    assert "[7]" in body_text, body_text


def case_volume_title_page_standalone() -> None:
    """第 1 章就是卷时：卷标题页只放卷标题，前言折进第一个叶子章。"""
    book = _book(
        _chapter("第一卷 卷名", [], level=2),
        _chapter("第一章", ["甲。"]),
        front=[Paragraph(text="前言。")],
    )
    result = _build(book)
    roots = _chapter_roots(result)
    vol = roots[0]
    assert _tags(vol) == ["h1"], f"卷标题页只该有标题，实际 {_tags(vol)}"
    assert (vol.find(".//xhtml:h1", NS).get("class") or "") == "vol"

    first = roots[1]
    assert _tags(first) == ["p", "h1", "p"], "前言要折进第一个叶子章，且在标题之前"
    assert _paras(first) == ["前言。", "甲。"]
    assert result.paragraphs == 2

    # 全书只有卷、没有叶子章时，前言退回第 1 章 —— 内容绝不能因为找不到落点而丢
    only_vol = _build(_book(_chapter("卷一", [], level=2), front=[Paragraph(text="前言。")]))
    assert _tags(_chapter_roots(only_vol)[0]) == ["p", "h1"]


# ---------- f) 2026-11-09 用户反馈：诗词块行距 / 字体子集 ----------


def case_verse_gap_absorbed() -> None:
    """诗词块内部的空行不再渲成 <div class="sep">，诗块每行等距。

    真实来源（2026-11-09 用户反馈的《西游记》）：诗节之间原书插着空行，
    旧实现把空行一律渲成 div.sep（高 1.1em），于是同一块诗里有的行距
    是 0.28em、有的是 1.1em+0.28em，看着像排版坏了。用户原话：
    "要是每行的间距都一样，那我也认了"。
    """
    book = _book(_chapter("第一章", [
        Paragraph(text="诗曰："),
        Paragraph(text="混沌未分天地乱，茫茫渺渺无人见。", kind="verse"),
        Paragraph(text="", kind="blank"),
        Paragraph(text="自从盘古破鸿蒙，开辟从兹清浊辨。", kind="verse"),
        Paragraph(text="覆载群生仰至仁，发明万物皆成善。", kind="verse"),
        Paragraph(text="", kind="blank"),
        Paragraph(text="欲知造化会元功，须看西游释厄传。", kind="verse"),
        Paragraph(text="正文一。"),
    ]))
    result = _build(book)
    root = _chapter_roots(result)[0]
    # 两块 verse 之间的空行（第 3、6 段）都被吞掉：诗块内 4 行紧邻
    assert _tags(root) == ["h1", "p", "p", "p", "p", "p", "p"], _tags(root)
    verses = [p for p in root.findall(".//xhtml:p", NS) if p.get("class") == "verse"]
    assert len(verses) == 4, f"应有 4 个 p.verse，实际 {len(verses)}"
    assert len(_seps(root)) == 0, "诗块内部不应再出现 div.sep"

    # 但诗与正文之间的空行还是正常分隔：诗块之后接正文时空行照旧渲染
    book2 = _book(_chapter("第一章", [
        Paragraph(text="混沌未分天地乱。", kind="verse"),
        Paragraph(text="", kind="blank"),
        Paragraph(text="正文一。"),
    ]))
    root2 = _chapter_roots(_build(book2))[0]
    assert _tags(root2) == ["h1", "p", "div", "p"], _tags(root2)
    assert len(_seps(root2)) == 1, "诗与正文之间的空行仍应保留"


def case_font_subset_includes_front_matter() -> None:
    """内嵌字体的子集必须收前言/后记（front_matter / tail_matter）里的字。

    真实来源（2026-11-09 用户反馈的《西游记》）：子集只收章节正文与目录，
    第一章前那段"简介"被漏掉，简介里的 30 多个字不在子集里，阅读器逐字
    退回系统字体，看起来就是"字体不一"。
    """
    rarities = "奘佚典浒奘"      # 只出现在前言里的字
    book = _book(
        _chapter("第一章", [Paragraph(text="正文甲。")]),
        front=[Paragraph(text=f"简介：{rarities}")],
        tail=[Paragraph(text="后记乙。")],
    )
    chars = epub._collect_font_chars(book, "", "", [])
    missing_front = [ch for ch in rarities if ch not in chars]
    assert not missing_front, f"前言里的字没进子集：{missing_front}"
    assert "乙" in chars, "后记里的字也要进子集"
    assert "甲" in chars and "正" in chars and "文" in chars, "章节正文的字不能丢"


def main() -> int:
    cases = [
        case_package_structure,
        case_opf_metadata,
        case_manifest_spine_consistent,
        case_nav_not_in_spine,
        case_nav_and_ncx_flat,
        case_toc_nested_with_volume,
        case_chapter_files,
        case_heading_and_note,
        case_verse_paragraph,
        case_sep_paragraph,
        case_css_contract,
        case_dark_mode_no_hardcoded_color,
        case_escaping_and_control_chars,
        case_multiline_paragraph_split,
        case_blank_folding,
        case_cover_image,
        case_cover_text_fallback,
        case_cover_unusable_falls_back,
        case_title_author_fallback,
        case_identifier_and_modified,
        case_deterministic,
        case_degenerate_books,
        case_front_tail_matter,
        case_write_epub_roundtrip,
        case_all_xml_wellformed,
        # 只读复核提出的回归用例（脏元数据 / level>=3 / 0 章前言 / 封面字节）
        case_metadata_dirty_chars,
        case_level_three_treated_as_volume,
        case_no_chapters_front_matter_kept,
        case_cover_bytes_validated,
        case_chapter_images_are_read_whole_at_zip_time,
        case_modified_string_and_invalid,
        # 成品观感：分页留白 / 注释区 / 卷标题页
        case_page_break_and_spacing,
        case_noteref_links_and_footnote_type,
        case_noteref_cross_chapter_href,
        case_noteref_dangling_becomes_plain_text,
        case_notes_grouped,
        case_volume_title_page_standalone,
        # 封底（2026-11-06）
        case_back_cover_page,
        case_back_cover_unusable_produces_no_page,
        # 用户反馈（2026-11-09）：诗词块行距 / 字体子集收前言
        case_verse_gap_absorbed,
        case_font_subset_includes_front_matter,
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
