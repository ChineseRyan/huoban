"""EPUB 导入（G6）：把一本现成的 EPUB 拆回章节树（中间态）+ 图片目录。

为什么要做这条线（项目铁律 4 的延伸）：清洗与打包只通过章节树对话，
所以"导入一本 EPUB"最自然的落点就是**产出同一份章节树**。之后的流程
（改字、调排版、重新打包）与 TXT 那条线完全一样，不需要两套渲染器。

设计边界（docs/gui-design.md §12）：
- **重排，不是转换**：走章节树重新决定每段是什么（正文/标题/图说/脚注），
  字符级格式（加粗、斜体、ruby 注音、字号）一律丢弃；
- **章节边界以 NCX/nav 为准**，不信正文里的大字标题 —— 盗版书的"标题"
  经常是整段加粗正文；
- **图片落盘不落内存**：一本 54 张图的画册全塞进内存再写盘，峰值会翻倍；
- 不支持 DRM（内容被加密就报错，不猜密钥）；固定版式（pre-paginated）报错。

三条"宁缺勿滥"的纪律：
1. 图片文件名一律由我们自己生成（img001.jpg…），不用原书名字 ——
   真实样本里出现过 `**::**::::…` 这种带 `*` 与 `:` 的混淆文件名，
   在 Windows 上根本写不出来；
2. 装饰图（同一张图全书引用超过阈值）与封面图不进正文；
3. 读不出来的东西（尺寸、锚点、缺图）只降级 + 记警告，不让整本书导入失败。
"""

from __future__ import annotations

import html.parser
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from .clean import _is_separator_line
from .model import Book, Chapter, Paragraph, parse_heading

# 图片扩展名与 media-type。与 epub.py 的 _IMAGE_TYPES 保持一致（那边是打包侧）。
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif")
_MEDIA_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}

# 各图片格式的文件头。只用来判"这到底是不是一张图"与决定落盘扩展名 ——
# 扩展名是原书随口写的，字节才是事实（真实样本里有 .jpg 里装 PNG 的）。
_MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
_MAGIC_JPG = b"\xff\xd8\xff"
_MAGIC_GIF = b"GIF8"

# 装饰图阈值（§12.5）：同一张图全书被引用超过这么多次，就认为它不是内容，
# 而是"每章顶部的 logo""脚注引用图标"这类排版装饰。
#
# 这个数 2026-10-28 用 349 本真 EPUB（其中 91 本带图，共 3,157 张图次引用）实测过：
# 引用 1 次的 2,951 张（93.5%）、2 次 107 张，3–8 次合计只有 35 张，
# **9 与 10 是空的**，家具图从 11 次起跳（11/15/16/18…直到 2,241 与 3,386）。
# 所以 10 落在真正的空带上：观察到的每一张家具图都还能被丢掉，而 3–8 次那一小撮
# 分不清是插图还是纹样的，一律按"是内容"处理。
# 原来写 5 的依据是"装饰图 79~2241 次、插图 1 次，5 是很宽的缝"—— 那句已被这批
# 样本证伪：家具图的下限其实低到 11 次，5 落在 3–10 这条模糊带里。
# 留下装饰图的代价是一本书里多几张废图，误删插图的代价是内容没了 —— 不对等。
DECORATIVE_THRESHOLD = 10

# 前言区的标题关键词（§12.7 前附）。只在前缀位置生效：一旦切出了第一
# 个正文章节，后面再出现"序"字样的标题就一律当章节（番外里常有"序"）。
_FRONT_WORDS = (
    "版权", "扉页", "制作说明", "内容简介", "简介", "前言",
    "序言", "自序", "代序", "译序", "出版说明", "编者的话",
)
# 目录页：整页都是链接，重排后没有意义，直接丢弃（不占章节、不进前言）。
_TOC_WORDS = ("目录", "目次", "contents", "tableofcontents")

# 分片合并时允许剥掉的尾部标记（§12.7）：「第二章 xxx（上）」与「（中）」
# 属于同一章。只认括号里是这些字/数字的，正文里合法的括号（如"（笑）"）不剥。
_PIECE_SUFFIX = re.compile(
    r"[（(]\s*(?:[一二三四五六七八九十百零〇0-9]{1,3}|上|中|下|全|续|完|终)\s*[）)]\s*$"
)

# 章节树里 title 可能带零宽字符（盗版书常见），比标题前一律清掉。
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff\u2060]")


class EpubReadError(ValueError):
    """EPUB 读不进来（不是合法 ZIP / 没有 OPF / DRM 加密 / 固定版式）。

    为什么继承 ValueError：CLI 层已经在用 `except ValueError` 兜打包期的
    元数据错误，导入期"这本书根本读不了"属于同一类"用户得改输入"的错，
    不需要再教它认识一个新异常类型。
    """


# ---------- 小型数据结构 ----------


@dataclass
class _ManifestItem:
    id: str
    href: str
    media: str
    properties: str


@dataclass
class _Opf:
    base: str  # OPF 所在目录（包内路径），manifest href 都相对它
    version: str
    title: str
    author: str
    lang: str
    items: dict[str, _ManifestItem]
    spine: list[str]
    cover_id: str | None
    cover_page_href: str | None  # guide 里 type="cover" 指向的页面
    meta: dict[str, str]


@dataclass
class _NavNode:
    """目录树的一个节点（NCX navPoint 或 nav.xhtml 的 <li>）。"""

    title: str
    doc: str = ""       # 已解析成 zip 条目名的目标文档（找不到就是空串）
    fragment: str = ""  # #anchor 部分（文档内切分用）
    children: list["_NavNode"] = field(default_factory=list)
    order: int = 0      # 先序遍历序号（位置回退时保持目录顺序）


@dataclass
class EpubImport:
    """一次导入的结果：章节树 + 一份给人看的报告。

    为什么报告与 Book 分开返回：Book 是给下游（打包/编辑）用的，
    报告是给人核对"这本书拆得对不对"用的 —— 混在一起会让章节树里
    塞满统计字段。
    """

    book: Book
    report: dict


# ---------- 文本小工具 ----------


def _clean_text(text: str) -> str:
    """段落/标题文本归一：去零宽、压 ASCII 空白、去首尾空白。

    为什么**保留**内部的 \\u3000（全角空格）：它是中文排版的一部分
    （"第一章　开端"里那个间隔、对话里的对齐空白），压成半角空格会让
    回环后的标题与原文逐字不一致。首尾的 \\u3000 由 strip() 去掉 ——
    盗版书常用"　　正文"表示缩进，缩进交给 CSS text-indent，不能留。
    """
    if not text:
        return ""
    s = _ZERO_WIDTH.sub("", text).replace("\xa0", " ")
    s = re.sub(r"[ \t\r\n\f\v]+", " ", s)
    return s.strip()


def _norm_key(text: str) -> str:
    """比较用的强归一：去掉所有空白。"""
    return re.sub(r"\s+", "", _ZERO_WIDTH.sub("", text or ""))


# 行内引用（正文里的 [1] 这类上标）在解析期间用哨兵占位：
# 解析时还不知道这段文字最后会被清洗成什么样（空白会被压缩、首尾会被 strip），
# 直接记字符偏移一定对不上。先塞一个不会被 _clean_text 动到的哨兵
# （\x01 和 \x02 都不属于它压缩的空白字符集），清洗完再按哨兵的真实位置
# 反算偏移，才不会把链接插到错的地方。
_REF_SENTINEL = re.compile("\x01(\\d+)\x02")


def _ext_from_magic(data: bytes) -> str | None:
    if data.startswith(_MAGIC_PNG):
        return ".png"
    if data.startswith(_MAGIC_JPG):
        return ".jpg"
    if data.startswith(_MAGIC_GIF):
        return ".gif"
    return None


def _image_size(data: bytes) -> tuple[int, int]:
    """从图片字节里读宽高（§12.4）。读不出来返回 (0, 0)，绝不抛异常。

    为什么不用 Pillow：它是可选依赖，打包/导入这条主路径不能因为
    "没装 Pillow"就整体不可用。三种格式的尺寸都在文件头里，标准库够了：
    - PNG：IHDR 块，偏移 16–19 宽、20–23 高，big-endian；
    - JPEG：扫段，遇到 SOF0/SOF1/SOF2… 时段内偏移 5–6 高、7–8 宽；
    - GIF：逻辑屏幕描述符，偏移 6–7 宽、8–9 高，little-endian。
    """
    if data.startswith(_MAGIC_PNG) and data[12:16] == b"IHDR" and len(data) >= 24:
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if data.startswith(_MAGIC_GIF) and len(data) >= 10:
        return (
            int.from_bytes(data[6:8], "little"),
            int.from_bytes(data[8:10], "little"),
        )
    if data.startswith(_MAGIC_JPG):
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            # 填充字节（连续的 0xFF）与无长度的标记：跳过
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if seg_len < 2:
                break
            # SOF0/SOF1/SOF2/SOF3/SOF5/SOF6/SOF7/SOF9…SOF15（除 DHT/JPG/DAC）
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height = int.from_bytes(data[i + 5:i + 7], "big")
                width = int.from_bytes(data[i + 7:i + 9], "big")
                return width, height
            i += 2 + seg_len
    return (0, 0)


def _warn_once(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


# ---------- 包内路径解析 ----------


def _make_resolver(names: list[str]):
    """造一个"包内 href → zip 条目名"的解析器。

    为什么不能直接 endswith 比对：真实样本（缝隙）的 href 是百分号编码的
    （`%2A` = `*`、`%3A` = `:`），zip 条目名里却是字面字符；另外 OPF 可能
    不在包根（OEBPS/content.opf），href 相对 OPF 目录。两层都要处理。
    """
    exact: dict[str, str] = {}
    folded: dict[str, str] = {}
    for name in names:
        decoded = unquote(name)
        exact[decoded] = name
        folded.setdefault(decoded.lower(), name)

    def resolve(href: str | None, base: str = "") -> str | None:
        if not href:
            return None
        raw = unquote(str(href).split("#")[0]).strip()
        if not raw:
            return None
        if raw.startswith(("http://", "https://", "data:", "//")):
            return None
        if raw.startswith("/"):
            cand = posixpath.normpath(raw.lstrip("/"))
        else:
            cand = posixpath.normpath(posixpath.join(base, raw)) if base else posixpath.normpath(raw)
        if cand in exact:
            return exact[cand]
        if cand.lower() in folded:
            return folded[cand.lower()]
        # 兜底：有的包 base 目录写错（href 已经带了 OEBPS/），按后缀唯一匹配。
        hits = [n for decoded, n in exact.items() if decoded.endswith("/" + raw)]
        if len(hits) == 1:
            return hits[0]
        return None

    return resolve


def _find_opf(zf: zipfile.ZipFile, names: list[str], warnings: list[str]) -> str:
    """找 OPF：先信 container.xml，其次在包里扫 *.opf。"""
    if "META-INF/container.xml" in names:
        try:
            root = ET.fromstring(zf.read("META-INF/container.xml"))
        except ET.ParseError:
            root = None
        if root is not None:
            for el in root.iter():
                if el.tag.split("}")[-1] == "rootfile":
                    path = el.get("full-path")
                    if path and unquote(path) in {unquote(n) for n in names}:
                        return path
                    if path:
                        warnings.append(f"container.xml 指向的 {path} 在包里不存在，改为扫描 *.opf")
    cands = [n for n in names if n.lower().endswith(".opf")]
    if not cands:
        raise EpubReadError("这个包里没有 OPF（content.opf）：不是一本完整的 EPUB")
    if len(cands) > 1:
        warnings.append(f"包里有 {len(cands)} 个 .opf，取第一个：{cands[0]}")
    return cands[0]


def _parse_opf(zf: zipfile.ZipFile, opf_path: str, warnings: list[str]) -> _Opf:
    try:
        root = ET.fromstring(zf.read(opf_path))
    except ET.ParseError as exc:
        raise EpubReadError(f"OPF 不是合法的 XML（{opf_path}）：{exc}") from exc

    def local(tag: str) -> str:
        return tag.split("}")[-1].lower()

    items: dict[str, _ManifestItem] = {}
    spine: list[str] = []
    meta: dict[str, str] = {}
    title = author = lang = ""
    cover_id = None
    cover_page = None
    for el in root.iter():
        name = local(el.tag)
        if name == "item":
            iid = el.get("id") or ""
            if iid:
                items[iid] = _ManifestItem(
                    id=iid,
                    href=el.get("href") or "",
                    media=(el.get("media-type") or "").lower(),
                    properties=(el.get("properties") or "").lower(),
                )
        elif name == "itemref":
            idref = el.get("idref")
            if idref:
                spine.append(idref)
        elif name == "meta":
            key = el.get("property") or el.get("name") or ""
            # EPUB3 的 <meta property="...">pre-paginated</meta> 把值写成元素文本，
            # EPUB2 的 <meta name="..." content="..."/> 把值写成属性 —— 两种都要认。
            value = el.get("content")
            if value is None:
                value = (el.text or "").strip()
            if key:
                meta[key.lower()] = value
            if (el.get("name") or "").lower() == "cover" and value:
                cover_id = value
        elif name == "reference":
            if (el.get("type") or "").lower() == "cover" and el.get("href"):
                cover_page = el.get("href")
        elif name == "title" and not title:
            title = _clean_text("".join(el.itertext()))
        elif name == "creator" and not author:
            author = _clean_text("".join(el.itertext()))
        elif name == "language" and not lang:
            lang = _clean_text("".join(el.itertext()))

    # cover 的两条来源：EPUB2 的 <meta name="cover">，EPUB3 的 properties="cover-image"。
    if not cover_id:
        for item in items.values():
            if "cover-image" in item.properties:
                cover_id = item.id
                break
    return _Opf(
        base=posixpath.dirname(opf_path),
        version=(root.get("version") or "").strip(),
        title=title,
        author=author,
        lang=lang,
        items=items,
        spine=spine,
        cover_id=cover_id,
        cover_page_href=cover_page,
        meta=meta,
    )


def _check_fixed_layout(opf: _Opf, warnings: list[str] | None = None) -> None:
    """固定版式（pre-paginated）直接报错：它每一页都是设计好的版心，
    重排等于把漫画拆成一堆散图，不如明说做不了。

    EPUB3 声明固定版式有两个地方，两个都得认：
    - 全局：`<meta property="rendition:layout">pre-paginated</meta>`；
    - 按条目覆盖：manifest 里 `properties="rendition:layout-pre-paginated"`
      （或省略前缀的 `page-layout` 变体）。
    只查全局 meta 会漏掉「整本漫画只在每个正文条目上标了固定版式」这种包 ——
    那会把设计好的整页当普通正文重排，出来的书全乱。

    部分条目才是固定版式时不整本报错（混排的包里其余章节仍然可读），
    改成一条警告点名数量，让用户自己判断要不要继续用这份产物。
    """
    layout = (opf.meta.get("rendition:layout") or "").strip().lower()
    fixed = (opf.meta.get("fixed-layout") or "").strip().lower()
    if layout == "pre-paginated" or fixed in ("true", "yes", "1"):
        raise EpubReadError("这本是固定版式（pre-paginated），无法重排")
    # spine 才是"正文条目"：图片、字体等资源也可能在 properties 里带别的标记，
    # 只按 spine 数，避免把一本正常重排书里的某张图误判成固定版式整本。
    spine_items = [opf.items[idref] for idref in opf.spine if idref in opf.items]
    fixed_items = [it for it in spine_items if "pre-paginated" in it.properties]
    if not fixed_items:
        return
    if len(fixed_items) == len(spine_items):
        raise EpubReadError("这本是固定版式（pre-paginated），无法重排")
    if warnings is not None:
        warnings.append(
            f"spine 里有 {len(fixed_items)}/{len(spine_items)} 个文档声明了固定版式"
            "（pre-paginated），这几篇重排后会变形"
        )


def _check_drm(
    zf: zipfile.ZipFile,
    names: list[str],
    resolve,
    warnings: list[str],
    opf: _Opf | None = None,
) -> list[str]:
    """检查 META-INF/encryption.xml：正文被加密就报错，其余资源分别说清楚。

    为什么不一见 encryption.xml 就拒绝：Adobe 的字体混淆（idpf.org/2008/embedding）
    与"只加密一个 CSS"在真实样本里都出现过（缝隙那本），正文完全可读，
    拒掉等于白白丢掉一本能导入的书。真正读不了的只有正文被加密这一种。

    分类不能只看扩展名：加密条目的 URI 写成 `Text/ch` 这种没扩展名的形式就绕过去了。
    所以"是不是正文"优先按 manifest 的 media-type 判，扩展名只作兜底。
    各类资源的后果也不一样，得分别说——原来那句「已跳过，不影响正文导入」是空话：
    代码里没有任何跳过逻辑，被加密的图片会和正常图片一样落盘（只是内容是乱码）。
    """
    if "META-INF/encryption.xml" not in names:
        return []
    try:
        root = ET.fromstring(zf.read("META-INF/encryption.xml"))
    except ET.ParseError:
        warnings.append("META-INF/encryption.xml 不是合法 XML，按没有加密处理")
        return []
    encrypted: list[str] = []
    for el in root.iter():
        if el.tag.split("}")[-1] == "CipherReference":
            uri = el.get("URI")
            if uri:
                encrypted.append(unquote(uri))
    if not encrypted:
        return []
    # 按 media-type 认正文（比扩展名可靠）；解析不出来的条目再看扩展名。
    doc_paths: set[str] = set()
    img_paths: set[str] = set()
    if opf is not None:
        for item in opf.items.values():
            resolved = resolve(item.href, opf.base)
            if not resolved:
                continue
            media = item.media.lower()
            if media.endswith("xhtml+xml") or media.endswith("html") or media.endswith("xml"):
                doc_paths.add(resolved)
            elif media.startswith("image/"):
                img_paths.add(resolved)
    blocking = [
        u for u in encrypted
        if u in doc_paths
        or (u not in img_paths and u.lower().endswith((".xhtml", ".html", ".htm", ".opf", ".ncx", ".xml")))
    ]
    if blocking:
        raise EpubReadError(
            f"这本 EPUB 的正文有 DRM 加密（{len(blocking)} 个文档被加密，例如 "
            f"{posixpath.basename(blocking[0])}），无法导入"
        )
    imgs = [u for u in encrypted if u in img_paths or u.lower().endswith(IMAGE_EXTS)]
    others = [u for u in encrypted if u not in imgs]
    if imgs:
        warnings.append(
            f"包里有 {len(imgs)} 张图片被加密（DRM），无法解密：导入后这些图会缺失或变成乱码"
        )
    if others:
        warnings.append(
            f"包里有 {len(others)} 个非正文资源被加密（样式/字体），一律忽略：正文文字不受影响，"
            "但排版样式与内嵌字体会丢失"
        )
    return encrypted


# ---------- 目录（NCX / nav.xhtml） ----------


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _parse_ncx(zf: zipfile.ZipFile, name: str, base: str, resolve, warnings: list[str]) -> list[_NavNode]:
    try:
        root = ET.fromstring(zf.read(name))
    except ET.ParseError:
        warnings.append(f"{posixpath.basename(name)} 不是合法 XML：当作没有 NCX 目录")
        return []
    navmap = None
    for el in root.iter():
        if _local(el.tag) == "navMap":
            navmap = el
            break
    if navmap is None:
        return []
    counter = [0]

    def build(node) -> list[_NavNode]:
        out: list[_NavNode] = []
        for child in node:
            if _local(child.tag) != "navPoint":
                continue
            label = ""
            src = ""
            for sub in child:
                if _local(sub.tag) == "navLabel":
                    label = _clean_text("".join(sub.itertext()))
                elif _local(sub.tag) == "content":
                    src = sub.get("src") or ""
            raw, frag = (src.split("#", 1) + [""])[:2] if "#" in src else (src, "")
            doc = resolve(raw, base) or ""
            counter[0] += 1
            out.append(
                _NavNode(
                    title=label,
                    doc=doc,
                    fragment=frag,
                    children=build(child),
                    order=counter[0],
                )
            )
        return out

    return build(navmap)


class _NavParser(html.parser.HTMLParser):
    """解析 EPUB3 nav.xhtml 里的 <nav epub:type="toc"><ol> 结构。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.roots: list[_NavNode] = []
        self._lists: list[list[_NavNode]] = [[]]
        self._li: list[_NavNode] = []
        self._in_a = False
        self._a_text: list[str] = []
        self._a_href = ""
        self._in_toc_nav = 0
        self._nav_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "nav":
            etype = (a.get("epub:type") or a.get("type") or "").lower()
            if "toc" in etype:
                self._in_toc_nav = 1
            elif self._in_toc_nav:
                self._in_toc_nav += 1
            self._nav_depth += 1
            return
        if not self._in_toc_nav:
            return
        if tag == "li":
            self._li.append(_NavNode(title=""))
        elif tag == "ol":
            self._lists.append([])
        elif tag == "a" and self._li:
            self._in_a = True
            self._a_text = []
            self._a_href = a.get("href") or ""

    def handle_data(self, data):
        if self._in_a:
            self._a_text.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "nav":
            if self._in_toc_nav:
                self._in_toc_nav -= 1
            self._nav_depth = max(0, self._nav_depth - 1)
            return
        if not self._in_toc_nav:
            return
        if tag == "a" and self._in_a:
            self._in_a = False
            node = self._li[-1]
            node.title = _clean_text("".join(self._a_text))
            href = self._a_href
            if "#" in href:
                raw, frag = href.split("#", 1)
            else:
                raw, frag = href, ""
            node.fragment = frag
            node.doc = raw  # 暂存 href 原文，调用方统一 resolve
            self._a_href = ""
        elif tag == "ol" and len(self._lists) > 1:
            kids = self._lists.pop()
            if self._li:
                self._li[-1].children.extend(kids)
            else:
                self._lists[-1].extend(kids)
        elif tag == "li" and self._li:
            node = self._li.pop()
            self._lists[-1].append(node)


def _parse_nav(zf: zipfile.ZipFile, name: str, base: str, resolve, warnings: list[str]) -> list[_NavNode]:
    parser = _NavParser()
    try:
        parser.feed(zf.read(name).decode("utf-8", "replace"))
        parser.close()
    except Exception:  # HTMLParser 对畸形输入理论上不抛，兜底不阻断导入
        warnings.append(f"{posixpath.basename(name)} 解析失败：当作没有 nav 目录")
        return []
    roots = parser._lists[0]
    # href 在 <a> 里，解析时暂存的是原文；统一补 resolve（相对 nav 文档目录）。
    def fix(nodes: list[_NavNode]) -> None:
        for node in nodes:
            if node.doc:
                node.doc = resolve(node.doc, base) or ""
            fix(node.children)

    fix(roots)
    return roots


def _flatten(nodes: list[_NavNode]) -> list[_NavNode]:
    """先序遍历，顺便打上连续序号（位置回退与"未消费节点"统计都靠它）。"""
    out: list[_NavNode] = []

    def walk(items: list[_NavNode]) -> None:
        for node in items:
            node.order = len(out) + 1
            out.append(node)
            walk(node.children)

    walk(nodes)
    return out


# ---------- 正文解析（html.parser，不用 ElementTree） ----------

# 整棵子树都不要的标签：脚本/样式/文档头，以及 nav（正文文档里偶尔夹目录块）。
_SKIP_TAGS = {"script", "style", "head", "title", "meta", "link", "nav"}

# 块级标签：每个块是"一个段落候选"。真实 EPUB 里 div/p 混用极其常见。
_BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
    "pre", "td", "dd", "dt", "figcaption", "aside", "section", "article",
    "header", "footer", "center", "address", "caption", "th",
}

# 图说常见的 class 关键词（多看系：duokan-image-maintitle；其它书：caption 等）。
_CAPTION_WORDS = ("maintitle", "caption", "imgtitle", "img-title", "img_caption")

# 自家 EPUB 产物（epub.py 渲出）用 class 标记段落语义：verse/note/sep 全都渲成
# <p class="...">，只看 tag 全是 p，必须靠 class 才分得回来。这是"自家产物回环
# 保真"的关键判据 —— class 命中优先于 blockquote/分隔线这类形态猜测。
# 注意只认整词："notes"（注释区容器）不在表里，容器本身也不该变成一个段落。
_CLASS_KINDS = {
    "note": "note",
    "verse": "verse",
    "sep": "sep",
    "separator": "sep",
}

# 自家信息页的指纹（书名/作者/来源/章节数/生成时间/由谁生成）。重排时整页跳过：
# 那是打包器自己写的元数据，不是书的内容；不跳过的话每过一遍重排就多出一段
# "章节数：N"，重排几遍书里就攒一堆自家统计数字。
_GENERATED_PAGE_MARK = "由 txt2epub 生成"

# 封底页的指纹。封底页整页只有一张图、一个字都没有，_GENERATED_PAGE_MARK 那条
# 「靠可见文字认」的路走不通。打包层（epub.py 的 _back_doc）在页面里留了一条
# XHTML 注释 `<!-- back-cover -->`，这里解析成 ["marker", "back-cover"] 并整页跳过。
#
# 为什么不复用 _GENERATED_PAGE_MARK：那条是"由 txt2epub 生成"，而生成物里刻意
# 不留这句自带广告词（tests/test_epub.py 有断言）；而且封底页没有文字可写。
# 为什么不能只靠 guide 的 <reference type="back">：实测带不带 guide，封底页都会
# 被当成"目录里没有的文档"续接进最后一章，每导一次多两段 —— 必须靠页面自身的标记。
_BACK_COVER_MARK = "back-cover"


def _is_foreign_note(attrs: dict) -> bool:
    """这条 note 是"别人的"脚注条目（多看 li / epub:type=footnote）吗？

    只有别人的脚注才需要剥开头的编号（"1 注释文字"）；自家产物的 note 文本
    就是章节树里的原文，剥了就不保真。
    """
    cls = (attrs.get("class") or "").lower()
    if "duokan-footnote" in cls:
        return True
    return any(word in _type_words(attrs) for word in _NOTE_WORDS)


# 注释块/注释引用的语义关键词。真实书里三种写法都要认：
# - epub:type="footnote"（标准 EPUB3）
# - type="footnote"（**Calibre 转换出来的 EPUB2 就这么写**，没有 epub: 前缀）
# - class="duokan-footnote-item"（多看）
_NOTE_WORDS = ("footnote", "endnote", "rearnote", "endnote-ref")


def _type_words(attrs: dict) -> str:
    """取"语义类型"属性：epub:type 优先，其次无前缀的 type。

    为什么必须认无前缀的 type：Calibre 把带脚注的书转成 EPUB2 时写的是
    `<a type="noteref">` / `<aside type="rearnote">`（EPUB2 没有 epub:type）。
    只认 epub:type 的话，这类书的注释会整批退化成普通正文 —— 实测的
    "弹注全部失效"就是这个（三国演义那本 7104 个 noteref 一个都没认出来）。
    """
    return ((attrs.get("epub:type") or "") + " " + (attrs.get("type") or "")).lower()


# 没有 type="noteref" 的注释引用，靠"显示文字是不是一个注码"来认。
# 《三国演义》那本就是这样：正文里是 <a id="p1" href="#pnote1">〚1〛</a>，
# 注释本体在每回末尾，也是同样形状的 <a id="pnote1" href="#p1">〚1〛</a>。
# 两个都没有 type 属性 —— 只认 type 的话，5322 个注码全部退化成正文。
_NOTE_MARKER = re.compile(
    r"^[〚\[【〔（(]\s*[0-9０-９一二三四五六七八九十百千零〇]+\s*[〛\]】〕）)]$"
    r"|^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]+$"
    r"|^注\s*[0-9０-９一二三四五六七八九十百]+$"
)


# 注释正文开头那个"〚3〛"：不是 href 链接时（有的书条目上只有 id），它就是一串
# 死文字，得在转成 note 时剥掉 —— 否则每一条注释前面都顶着一个注码。
_NOTE_LEAD = re.compile(
    r"^\s*(?:[〚\[【〔（(]\s*[0-9０-９一二三四五六七八九十百千零〇]+\s*[〛\]】〕）)]"
    r"|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]"
    r"|注\s*[0-9０-９一二三四五六七八九十百]+)\s*"
)


def _is_note_marker(label: str) -> bool:
    """这串显示文字是不是一个注码（〚1〛、【2】、①、注 3）？

    为什么拿显示文字当判据：没有 type 属性的 <a href="#pnote1"> 和目录里的
    <a href="#ch3">第三章</a> 在结构上完全一样，唯一能分开的就是"里面写着
    什么"。注码的形态很窄（括号包一个数字、圈号、注N），目录标题不会长这样。
    """
    return bool(_NOTE_MARKER.match((label or "").strip()))


def _is_note_box(attrs: dict) -> bool:
    """这个块元素是不是"注释容器"（它的子段落都算注释）？

    区分"容器"和"注释本身"没有意义 —— 两种都会让里面的文字变成 note，
    区别只在容器自己没有文字、由子元素认领。
    """
    if "duokan-footnote" in (attrs.get("class") or "").lower():
        return True
    return any(word in _type_words(attrs) for word in _NOTE_WORDS)


def _is_generated_page(items: list[list]) -> bool:
    """这一页是本工具自己加的、不该进正文的页吗？

    两种：文字版信息页（见 _GENERATED_PAGE_MARK，靠可见文字认）与封底页
    （见 _BACK_COVER_MARK，靠页面里的注释标记认 —— 它没有文字可认）。
    """
    return any(
        (item[0] == "text" and _GENERATED_PAGE_MARK in item[1])
        or (item[0] == "marker" and item[1] == _BACK_COVER_MARK)
        for item in items
    )


class _DocParser(html.parser.HTMLParser):
    """把一个正文 xhtml 解析成"块序列"。

    产出 items 是三种东西的有序列表：
    - ["anchor", id]            文档内锚点（目录片段切分用）
    - ["text", text, kind]      一个块级元素归一后的文字
    - ["image", src, caption]   一张图（src 还是相对路径，调用方 resolve）

    带行内引用的文本块多一个第四项：`["text", text, kind, refs]`，
    refs 是 `[(哨兵号, 目标 id, 显示文字)]`，text 里仍留着 `\\x01N\\x02` 哨兵，
    由 _resolve_refs 在整篇解析完之后统一定位、去掉哨兵。

    为什么用 html.parser 而不是 ElementTree：真实 EPUB 大量不规范
    （未闭合的 <p>、&nbsp;、重复属性、大小写混乱），ElementTree 直接
    ParseError；html.parser 是容错的，坏文档也能尽量抠出内容。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[list] = []
        # 每帧：[tag, attrs, 文本缓冲, 有子块, 有图, 行内引用, 是注释容器]
        self._frames: list[list] = []
        # 待认领的锚点：块元素上的 id 往往在容器上（<aside id="note1">），
        # 文字却在子元素里 —— 所以不能把 id 记在帧上，要挂到"下一段落地的
        # 文字"上。容器没有文字时它会顺延给后面第一段，总比丢掉强。
        self._pending_anchor = ""
        self._body_buf: list[str] = []
        self._body_refs: list[tuple] = []
        self._skip: list | None = None  # [tag, 深度]
        self.last_image_idx: int | None = None
        self.text_since_image = False
        # 正在收集的注释引用：{"frag": 目标 id, "buf": [显示文字]}
        self._ref: dict | None = None
        self._ref_seq = 0

    # -- 内部工具 --

    def _flush_body(self) -> None:
        if not self._body_buf:
            return
        text = _clean_text("".join(self._body_buf))
        refs = self._body_refs
        self._body_buf = []
        self._body_refs = []
        if text:
            if self._pending_anchor:
                self.items.append(["anchor", self._pending_anchor])
                self._pending_anchor = ""
            item = ["text", text, "body"]
            if refs:
                item.append(refs)
            self.items.append(item)
            self.text_since_image = True

    def _append(self, data: str) -> None:
        if self._skip is not None or not data:
            return
        if self._ref is not None:
            # 注释引用的显示文字（"[1]"）单独收，不混进正文缓冲 ——
            # 它要以链接的形式回到原位，直接写进正文就成了死文字。
            self._ref["buf"].append(data)
            return
        if self._frames:
            self._frames[-1][2].append(data)
        else:
            self._body_buf.append(data)

    def _append_raw(self, data: str) -> None:
        """把哨兵写进当前缓冲，绕开引用收集（哨兵不是正文）。"""
        if self._frames:
            self._frames[-1][2].append(data)
        else:
            self._body_buf.append(data)

    def _on_image(self, src: str, attrs: dict) -> None:
        src = (src or "").strip()
        if not src:
            return
        if self._frames:
            self._frames[-1][4] = True
        self.items.append(["image", src, ""])
        self.last_image_idx = len(self.items) - 1
        self.text_since_image = False

    def _close_frame(self, frame: list) -> None:
        tag, attrs, buf, had_child, had_image, refs, note_box = frame
        text = _clean_text("".join(buf))
        cls = (attrs.get("class") or "").lower()
        # 图说：紧跟在图片后面、中间没夹正文的 caption 类块，挂到上一张图上。
        if tag == "figcaption" or any(word in cls for word in _CAPTION_WORDS):
            if text and self.last_image_idx is not None and not self.text_since_image:
                item = self.items[self.last_image_idx]
                if item[0] == "image" and not item[2]:
                    item[2] = text
                    return
        if not text:
            # 空块：只把"真正的空段落"记成空行（场景分隔），嵌套容器的空壳不记。
            if tag in ("p", "div") and not had_child and not had_image:
                self.items.append(["text", "", "blank"])
            return
        # 段落自己的 id 要排在文本块之前：目录片段靠 ["anchor", id] 定位切分点，
        # 排在后面会让章节从"下一段"才开始，章首那一段掉进上一章。
        if self._pending_anchor:
            self.items.append(["anchor", self._pending_anchor])
            self._pending_anchor = ""
        kind = self._kind_of(tag, attrs, text)
        # 注释的"语义"常写在容器上、文字在子元素里：
        # `<section type="rearnotes"><aside id=..><p>注文</p></aside></section>`
        # 里面那个 <p> 自己什么标记都没有。只看本帧的属性，这类书的注释
        # 会整批退化成正文 —— 所以只要祖先帧（含本帧）被标成注释容器，
        # 里面的文字就是注释。
        in_note_box = note_box or any(f[6] for f in self._frames)
        if kind == "body" and in_note_box:
            kind = "note"
        if kind == "note" and (_is_foreign_note(attrs) or in_note_box):
            # 脚注条目常以编号开头（"1 注释文字"），编号对重排没有意义。
            # 只对"别人的"脚注做这一步：自家产物里的 note 文本是章节树原文，
            # 动它就不是保真了（自家 note 的 class 恰好是 "note"，见 _CLASS_KINDS）。
            text = re.sub(r"^[\[（(]?\s*\d{1,3}\s*(?:[\]）)\.、:：]\s*|\s+)", "", text) or text
        item = ["text", text, kind]
        if refs:
            item.append(refs)
        self.items.append(item)
        self.text_since_image = True

    @staticmethod
    def _kind_of(tag: str, attrs: dict, text: str) -> str:
        types = _type_words(attrs)
        cls = (attrs.get("class") or "").lower()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            return "heading"
        if any(word in types for word in _NOTE_WORDS):
            return "note"
        if "duokan-footnote-item" in cls:
            return "note"
        if "duokan-footnote-content" in cls:
            return "note"
        # 自家产物的 class 标记（见 _CLASS_KINDS）：<p class="verse"> 等。
        for token in cls.split():
            kind = _CLASS_KINDS.get(token)
            if kind:
                return kind
        if tag == "blockquote":
            # 引文/诗词：模型里最接近的是 verse（居中楷体、与正文拉开距离）。
            return "verse"
        if _is_separator_line(text):
            return "sep"
        return "body"

    # -- HTMLParser 回调 --

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        if self._skip is not None:
            if tag == self._skip[0]:
                self._skip[1] += 1
            return
        if tag in _SKIP_TAGS:
            self._skip = [tag, 1]
            return
        style = (a.get("style") or "").replace(" ", "").lower()
        if "display:none" in style:
            # 隐藏标题（盗版书用来藏"书名/作者"或重复标题）不算内容。
            self._skip = [tag, 1]
            return
        cls = (a.get("class") or "").lower()
        if tag in ("a", "sup", "span", "img") and "duokan-footnote" in cls:
            # 脚注引用处的图标/编号：图标本身由装饰图阈值挡掉，编号文字没意义。
            self._skip = [tag, 1]
            return
        # 正文里的注释引用：<a type="noteref" href="#note1">[1]</a>。
        # 只认带 #片段 的 href：跨文件的引用（注释块回指正文那种）在本篇里
        # 没有落点，交给 _resolve_refs 还原成纯文字，不硬造死链接。
        if tag == "a":
            href = (a.get("href") or "").strip()
            frag = href.split("#", 1)[1].strip() if "#" in href else ""
            if "noteref" in _type_words(a):
                self._ref = {"frag": frag, "buf": [], "cand": False}
                return
            if frag:
                # 可能是"没有 type 属性的注释引用"（见 _NOTE_MARKER）：先把
                # 显示文字收进候选缓冲，结束标签时看它是不是注码再定。
                # 不是注码就把文字原样放回正文 —— 目录链接走的正是这条路。
                aid = a.get("id") or a.get("name")
                self._ref = {"frag": frag, "buf": [], "cand": True, "aid": aid}
                # 注释条目自己的 id 也挂在这类 <a> 上（<a id="pnote1">〚1〛</a>
                # 注文…）。注释正文要靠它才能被正文里的注码指到，所以先登记
                # 待认领锚点；结束标签时若发现它根本不是注码，再撤回去 ——
                # 不然每一条目录链接都会给所在段落平白添一个锚点。
                if aid and not self._pending_anchor:
                    self._pending_anchor = aid
                return
        anchor_id = a.get("id") or a.get("name")
        if anchor_id and tag not in _BLOCK_TAGS:
            # 段内的 id（脚注锚点几乎都在段内的 <a>/<sup> 上）：挂到待认领，
            # 由这一段的收尾认领。直接往 items 里插 ["anchor", id]
            # 会把一段正文从中间劈成两段。
            if not self._pending_anchor:
                self._pending_anchor = anchor_id
        if tag == "img":
            self._on_image(a.get("src") or a.get("data-src") or "", a)
            return
        if tag == "image":  # SVG 里的位图（<svg><image xlink:href="..."/></svg>）
            self._on_image(a.get("xlink:href") or a.get("href") or "", a)
            return
        if tag == "hr":
            # 自家产物把场景分隔线渲成 <hr class="sep"/>。重排时要认回来，
            # 否则回环一遍分隔线就退化成普通空行（回环不动点当场破掉）。
            # 别的 <hr>（精排书里当分节线用的那种）仍按空行处理。
            self.items.append(["text", "", "sep" if "sep" in cls else "blank"])
            return
        if tag == "br":
            self._append(" ")
            return
        if tag in _BLOCK_TAGS:
            self._flush_body()
            if self._frames:
                self._frames[-1][3] = True
            if anchor_id and not self._pending_anchor:
                # 容器的 id（<aside id="note1"> 这种）由它内部的第一个文字块认领：
                # 文字在子元素里，直接记在容器帧上会在容器"没有自己的文字"
                # 时丢掉 —— 注释块恰好全是这种形态。
                self._pending_anchor = anchor_id
            self._frames.append(
                [tag, a, [], False, False, [], _is_note_box(a)]
            )

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip is not None:
            if tag == self._skip[0]:
                self._skip[1] -= 1
                if self._skip[1] <= 0:
                    self._skip = None
            return
        if tag == "a" and self._ref is not None:
            label = _clean_text("".join(self._ref["buf"]))
            frag = self._ref["frag"]
            cand = bool(self._ref.get("cand"))
            aid = self._ref.get("aid") or ""
            self._ref = None
            # 候选引用只有"显示文字是注码"时才算数；否则把文字放回正文，
            # 连同刚才替它登记的锚点一起撤销。
            if frag and cand and not _is_note_marker(label):
                frag = ""
                if aid and self._pending_anchor == aid:
                    self._pending_anchor = ""
                if label:
                    self._append_raw(label)
                return
            if frag:
                self._ref_seq += 1
                seq = self._ref_seq
                self._append_raw("\x01%d\x02" % seq)
                entry = (seq, frag, label)
                if self._frames:
                    self._frames[-1][5].append(entry)
                else:
                    self._body_refs.append(entry)
            elif label:
                # 没有 #片段：不是可跳转的引用，把显示文字原样放回正文。
                self._append_raw(label)
            return
        if tag not in _BLOCK_TAGS:
            return
        idx = None
        for i in range(len(self._frames) - 1, -1, -1):
            if self._frames[i][0] == tag:
                idx = i
                break
        if idx is None:
            return
        # 未闭合的内层帧一并按"内→外"收掉（容错），不能只弹最上面一个。
        while len(self._frames) > idx:
            self._close_frame(self._frames.pop())

    def handle_data(self, data):
        self._append(data)

    def handle_comment(self, data):
        """只认自家封底页那条标记注释，别的一律丢掉。

        为什么封底要靠注释认：那一页整页只有一张图，没有任何文字，没法像
        信息页那样靠"由 txt2epub 生成"这句可见文字认出来；而它只要没被认出来，
        就会掉进"目录里没有的文档"分支被续接进最后一章 —— 每导一次多两段，
        回环不动点当场破掉（实测 2→4→6→8）。

        为什么不写成可见的水印文字：封底上不该有字（用户定的规格是"只有纸纹
        和一枚印"），而且任何写进正文的字都会真的被读者看见。
        """
        mark = (data or "").strip()
        if mark == _BACK_COVER_MARK:
            self.items.append(["marker", mark])

    def close(self) -> None:
        super().close()
        if self._ref is not None:
            # 未闭合的 <a>（真实 EPUB 里并不罕见）：把显示文字放回正文，
            # 不然这一段末尾的字会整块消失。
            self._append_raw(_clean_text("".join(self._ref["buf"])))
            self._ref = None
        while self._frames:
            self._close_frame(self._frames.pop())
        self._flush_body()


def _resolve_refs(items: list[list]) -> None:
    """把哨兵定位成真实字符偏移，记下每条引用的落点与显示文字。

    分两趟的原因：解析一个块的时候还不知道整篇有哪些 id（注释块可能排在
    引用之后），所以先记哨兵、整篇解析完再定。

    本文档里有落点的引用直接定下来；没有落点的**先保留**（note 段开头的
    反向链接除外 —— 那是注释自己的编号，正文那边已经有指过来的链接了）。
    跨文件的落点要靠全书锚点表才能判，见 _finalize_refs。
    """
    anchors = {item[1] for item in items if item[0] == "anchor"}
    for item in items:
        if item[0] != "text" or len(item) < 4:
            continue
        text = item[1]
        raw_refs = item[3]
        by_seq = {seq: (frag, label) for seq, frag, label in raw_refs}
        if "\x01" not in text:
            item[3] = []
            continue
        pieces: list[str] = []
        kept: list[dict] = []
        pos = 0
        cur = 0
        for m in _REF_SENTINEL.finditer(text):
            chunk = text[pos:m.start()]
            pieces.append(chunk)
            cur += len(chunk)
            info = by_seq.get(int(m.group(1)))
            if info is None:
                pass  # 找不到对应记录（不该发生）：哨兵直接消失
            elif item[2] == "note" and cur == 0:
                # 注释段开头那个 "[1]" 是"回到正文"的反向链接（几乎总是跨
                # 文件，本篇里没有落点）。还原成文字只会变成注释开头的
                # 多余编号 —— 正文那边已经有指过来的链接了，这里直接丢掉。
                # 这条判断必须排在"目标存在就保留"之前：反向链接指向的是
                # 正文里的锚点，全书锚点表里通常真能找到它。
                pass
            else:
                # 目标不在本文档也先留着：Calibre 这类书把正文和注释
                # 分成两个 xhtml 文件，注释的落点在别的文件里。真正的
                # 有效性由全书锚点表在 _finalize_refs 里统一裁决。
                kept.append({"at": cur, "note": info[0], "label": info[1]})
            pos = m.end()
        pieces.append(text[pos:])
        item[1] = "".join(pieces)
        item[3] = kept


def _iter_paragraphs(book):
    """遍历整本书的所有段落（前言、各章、后记）。"""
    yield from book.front_matter
    for ch in book.chapters:
        yield from ch.paragraphs
    yield from book.tail_matter


def _finalize_refs(book) -> None:
    """全书锚点表建好之后，把指向不存在锚点的引用还原成纯文字。

    为什么要等全书解析完：Calibre 把正文和注释拆成两个 xhtml，正文里的
    `<a type="noteref" href="part0178.html#rearnote_1">` 目标根本不在
    同一个文件里。单篇解析时看不到那个 id，只能先留着；整本解析完才知道
    哪些落点真实存在。

    落点不存在的引用（残缺书、被删掉的注释页）一律还原成它的显示文字：
    留一条点了没反应的死链接，比不链接更让人困惑。
    """
    anchors = {p.anchor for p in _iter_paragraphs(book) if p.anchor}
    for para in _iter_paragraphs(book):
        if not para.refs:
            continue
        text = para.text or ""
        kept: list[dict] = []
        shift = 0
        for ref in sorted(para.refs, key=lambda r: r.get("at", 0)):
            at = ref.get("at")
            if not isinstance(at, int) or isinstance(at, bool):
                continue
            pos = at + shift
            if pos < 0 or pos > len(text):
                continue
            note = ref.get("note") or ""
            label = ref.get("label") or ""
            if note in anchors:
                kept.append({"at": pos, "note": note, "label": label})
            else:
                text = text[:pos] + label + text[pos:]
                shift += len(label)
        para.text = text
        para.refs = kept

    # 被注释引用指到的段落就是注释本体。Calibre 和毛宗岗评本这类书，注释条目
    # 没有任何 type 属性（<p><a id="pnote1">〚1〛</a>注文…</p>），唯一的线索
    # 就是"正文里有一个注码指着它"。不转成 note 的话，正文里点得开、注释区
    # 却还是普通正文 —— 阅读器不会弹窗，等于白链。
    # 只认"注码落在段中间"的引用（at > 0）。注释段自己开头那个注码是指回
    # 正文的反向链接（at == 0），拿它当判据会把整段正文转成注释 —— 实测
    # 那样一本来会多出 3000 多个假注释段。
    referenced = {
        r.get("note")
        for para in _iter_paragraphs(book)
        for r in (para.refs or [])
        if isinstance(r, dict) and isinstance(r.get("at"), int) and r["at"] > 0
    }
    for para in _iter_paragraphs(book):
        if para.kind != "body" or not para.anchor or para.anchor not in referenced:
            continue
        para.kind = "note"
        # 开头那个指回正文的注码是反向链接：正文那边已经有一个指过来的了，
        # 留着会在注释开头多印一个 〚1〛。
        para.refs = [r for r in (para.refs or []) if r.get("at") != 0]
        # 条目上只有 id、没有 href 时，注码根本不成链接，只是一串死文字 ——
        # 这里把它剥掉，剩下的才是注释正文。剥掉的长度要从 refs 的偏移里减掉。
        lead = _NOTE_LEAD.match(para.text or "")
        if lead:
            cut = lead.end()
            para.text = (para.text or "")[cut:]
            if para.refs:
                para.refs = [
                    {**r, "at": max(0, int(r.get("at", 0)) - cut)} for r in para.refs
                ]


def _parse_doc(data: bytes) -> list[list]:
    parser = _DocParser()
    parser.feed(data.decode("utf-8", "replace"))
    parser.close()
    items = parser.items
    _resolve_refs(items)
    return items


# ---------- 章节切分辅助 ----------


def _stem_title(title: str) -> str:
    """去掉分片尾标（（上）（中）…（十一）），得到"主干"标题。"""
    out = title.strip()
    prev = None
    while out and prev != out:
        prev = out
        out = _PIECE_SUFFIX.sub("", out).rstrip()
    return out


def _is_front_title(title: str) -> bool:
    """这个目录标题像不像"前附"（版权页/扉页/序/译序/制作说明…）。"""
    key = _norm_key(title).lower()
    if not key:
        return False
    if any(word in key for word in _FRONT_WORDS):
        return True
    # 单独的"序"/"序一"/"序二"：只认极短且不是"序章/序幕"（那是正文）。
    if key.startswith("序") and len(key) <= 3 and not key.startswith(("序章", "序幕")):
        return True
    return False


def _is_toc_title(title: str) -> bool:
    key = _norm_key(title).lower()
    return bool(key) and (key in _TOC_WORDS or (key.startswith("目录") and len(key) <= 4))


def _split_positions(items: list[list], nodes: list[_NavNode]) -> list[int]:
    """算出每个目录节点在 items 里的起始下标（单调不减）。

    正常情况靠 #anchor 定位；锚点丢了（破损/改名）就在相邻已知锚点之间
    均分 —— 宁可切得粗糙，也不能让第一个节点吞掉整篇、其余全是空章。
    """
    # 一个文档只对应一个目录节点、又没有 #fragment（绝大多数"一文件一章"的书
    # 都是这样，包括本工具自己打出来的 EPUB）：整篇文档都属于它，从 0 开始。
    # 不特殊处理的话下面的均分会把它从中间劈开，前半截被塞给上一章。
    if len(nodes) == 1 and not nodes[0].fragment:
        return [0]
    anchor_at: dict[str, int] = {}
    for idx, item in enumerate(items):
        if item[0] == "anchor":
            anchor_at.setdefault(item[1], idx)
    pos: list[int | None] = [None] * len(nodes)
    prev = 0
    for i, node in enumerate(nodes):
        if node.fragment and node.fragment in anchor_at and anchor_at[node.fragment] >= prev:
            pos[i] = anchor_at[node.fragment]
            prev = pos[i]
    i = 0
    while i < len(nodes):
        if pos[i] is not None:
            i += 1
            continue
        j = i
        while j < len(nodes) and pos[j] is None:
            j += 1
        start = pos[i - 1] if i > 0 and pos[i - 1] is not None else 0
        end = pos[j] if j < len(nodes) else len(items)
        run = j - i
        if i == 0:
            # 这一段是文档的**开头**：前一个节点都没有，就没有"让出第一段"的理由。
            # 按 run 段分而不是 run+1 段 —— 原来多留的那一段会掉到**上一章**的尾巴上。
            # 真池 194 本里命中 414 次，绝大多数只漏 1 段，而那一段通常是章标题。
            step = (end - start) / run
            for k in range(run):
                pos[i + k] = int(start + step * k)
        else:
            step = (end - start) / (run + 1)
            for k in range(run):
                pos[i + k] = int(start + step * (k + 1))
        i = j
    return [int(p or 0) for p in pos]


def _drop_duplicate_heading(items: list[list], title: str) -> list[list]:
    """去掉章节开头那个"和目录标题重复"的标题块。

    真实样本里几乎每章正文开头都重复一遍章标题（宰执天下是
    `<h2 class="head"><b>第一章</b><br/> 劫后梦醒世事更</h2>`），
    不去掉的话 EPUB 里会出现"h1 章标题 + h2 同标题"上下贴在一起。

    两条规则，宽严不同：
    1. **章首第一个非空文本块逐字等于目录标题 → 删**。TXT 改出来的 EPUB 十有
       八九把章标题写成 `<p>` 而不是 `<h1>`（多样本矩阵里 28 本合成样本全中），
       规则 2 的 heading 匹配对这种包完全不生效，成品里就是"标题 + 一模一样的
       一段正文"。这条要求**完全相等且只看开头**，所以吃不掉正常句子。
    2. 前 3 个标题块里允许互相包含（`第一章` 与 `第一章　劫后梦醒` 是同一章）。
    """
    key = _norm_key(title)
    if not key:
        return items
    for idx, item in enumerate(items):
        if item[0] != "text" or item[2] == "blank":
            continue
        if item[2] != "note" and _norm_key(item[1]) == key:
            return items[:idx] + items[idx + 1:]
        break
    # 扫描范围：正文块不限个数 —— 本工具自己打出的 EPUB 会把"前言"折进第一个
    # 叶子章的章标题**之前**，前言可以很长（死灵之书 53 段，其中还有一个自己的
    # 小标题）。改为限制"最多看前 3 个标题块"：章首重复标题基本都在这个窗口里，
    # 而章内后段再出现的同名标题是作者的分节写法，不该删。
    headings_seen = 0
    for idx, item in enumerate(items):
        if item[0] != "text" or item[2] == "blank":
            continue
        if item[2] == "heading":
            head = _norm_key(item[1])
            if head == key or (len(head) >= 4 and (head in key or key in head)):
                return items[:idx] + items[idx + 1:]
            headings_seen += 1
            if headings_seen >= 3:
                break
    return items


def _merge_pieces(chapters: list[Chapter]) -> tuple[list[Chapter], int, int]:
    """相邻同主干的分片合并（§12.7），返回 (合并后的章节, 组合数, 合并前片数)。

    为什么要求"相邻"：不相邻的同名章（"第一章"出现在两卷里）合并会把
    两段隔了几百章的内容缝到一起。相邻 + 同主干 + 同层级（都是章）才合。
    """
    pieces = sum(1 for ch in chapters if ch.level == 1)
    merged: list[Chapter] = []
    groups = 0
    in_group = False
    for ch in chapters:
        stem = _stem_title(ch.title)
        if (
            merged
            and ch.level == 1
            and merged[-1].level == 1
            and stem
            and stem == _stem_title(merged[-1].title)
        ):
            if not in_group:
                groups += 1
                in_group = True
            merged[-1].paragraphs.extend(ch.paragraphs)
            merged[-1].title = stem
            continue
        in_group = False
        merged.append(ch)
    return merged, groups, pieces


# ---------- 主流程 ----------


def read_epub(path, image_dir=None) -> Book:
    """把一本 EPUB 读成章节树（图片按需落盘到 image_dir）。

    这是对外的主入口：只返回 Book，报告用 read_epub_with_report 取。
    不 print、不写日志 —— 业务结果由 CLI 层负责展示（项目代码约定）。
    """
    return read_epub_with_report(path, image_dir=image_dir).book


def read_epub_with_report(path, image_dir=None) -> EpubImport:
    """同 read_epub，另外返回一份导入报告（章节数/合并组数/图片统计/警告）。

    **所有解析期异常都在这里收成 EpubReadError**（文件坏、章节 CRC 坏、加密 zip、
    deflate 流损坏、EPUB3 的压缩算法不支持）。理由：调用方（CLI / 界面）只有
    "这个文件不能处理"与"我们自己的 bug"两种话可说，把 `BadZipFile` / `zlib.error`
    的英文原文连同包内路径透到界面上，既看不懂也暴露了不该看的内部结构。
    """
    src = Path(path)
    try:
        zf = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as exc:
        raise EpubReadError(
            f"打不开 {src.name}：它不是一个合法的 ZIP/EPUB（{exc.__class__.__name__}）"
        ) from exc
    with zf:
        try:
            return _import(zf, src, image_dir)
        except EpubReadError:
            raise
        except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError) as exc:
            # 这些不是"文件不够规范"而是"文件已损坏/不支持"：章节条目 CRC 坏、
            # 压缩流被截断、加密 zip 需要口令、EPUB3 声明了我们不实现的压缩算法。
            #
            # 括号里的类名是给排查用的，但**不能直接写 `exc.__class__.__name__`**：
            # `zlib.error` 的类名就是 "error"，用户会看到「包里有损坏或不支持的内容
            # （error）」—— 一个叫 error 的东西是什么？实测踩过（截断的压缩流
            # 抛的正是 zlib.error）。所以要给这几类补一句人话。
            raise EpubReadError(
                f"读不下去 {src.name}：包里有损坏或不支持的内容（{_digest_exc(exc)}）"
                "——文件可能没下完整，或者带了 DRM"
            ) from exc


# 异常类名对用户没有信息量的时候，补一句人话。
# 键是"类名"，值是"这是怎么回事"。只用类名不用类型对象，
# 是为了不把 zipfile/zlib 的类都 import 到模块级（也方便这张表被测试直接查）。
_EXC_HINTS: dict[str, str] = {
    "BadZipFile": "ZIP 结构坏了",
    "error": "压缩流解不开",  # zlib.error 的类名就叫 error
    "RuntimeError": "包里有加密或需要口令的内容",
    "NotImplementedError": "用了我们还不支持的压缩算法",
    "EOFError": "文件被截断了",
}


def _digest_exc(exc: BaseException) -> str:
    """把异常翻成「类名：人话」，认不出来就只给类名。

    为什么不用 `isinstance` 分支：`zlib.error` 是内建 `Exception` 的别名式异常，
    和别的异常没有共同的基类可挂；按类名查表最直白，也不会漏。
    """
    name = exc.__class__.__name__
    hint = _EXC_HINTS.get(name)
    return f"{name}：{hint}" if hint else name


def _import(zf: zipfile.ZipFile, src: Path, image_dir) -> EpubImport:
    warnings: list[str] = []
    names = zf.namelist()
    if "mimetype" in names:
        try:
            if zf.read("mimetype").strip() != b"application/epub+zip":
                warnings.append("mimetype 不是 application/epub+zip：仍按 EPUB 尝试读取")
        except Exception:
            pass
    else:
        warnings.append("包里没有 mimetype：仍按 EPUB 尝试读取")

    resolve = _make_resolver(names)
    opf_path = _find_opf(zf, names, warnings)
    opf = _parse_opf(zf, opf_path, warnings)
    _check_fixed_layout(opf, warnings)
    _check_drm(zf, names, resolve, warnings, opf)

    # 图片目录：**先不建**，等 spine 校验通过再建（M3）。
    # 早先在这里直接 mkdir，结果是"文件解析到一半就失败"（空 spine、DRM、章节坏）
    # 也会在用户的输入目录旁边留下一个 `<文件名>_gui\images` 空目录 —— 界面上表现为
    # "报错了但多了个空文件夹"，用户不知道要不要删。目录建在确定要继续读之后。
    out_dir = Path(image_dir) if image_dir is not None else None

    # --- 封面图 ---
    cover_name = ""
    if opf.cover_id:
        item = opf.items.get(opf.cover_id)
        if item:
            resolved = resolve(item.href, opf.base)
            if resolved and item.media.startswith("image/"):
                cover_name = resolved
            elif resolved and (item.media.endswith("xhtml+xml") or resolved.lower().endswith((".xhtml", ".html", ".htm"))):
                # 封面指向一个页面：从页面里剥出第一张位图（§12.5 的 SVG 包位图写法）。
                cover_name = _first_image_in_doc(zf, resolved, opf.base, resolve) or ""
    if not cover_name:
        # 兜底：manifest 里 properties="cover-image" 的那张
        for item in opf.items.values():
            if "cover-image" in item.properties:
                resolved = resolve(item.href, opf.base)
                if resolved:
                    cover_name = resolved
                    break
    if not cover_name:
        warnings.append("这本 EPUB 没找到封面图：重排后不会有封面")

    # --- spine 文档顺序 ---
    spine_docs: list[str] = []
    skipped_media: Counter[str] = Counter()
    for idref in opf.spine:
        item = opf.items.get(idref)
        if item is None:
            warnings.append(f"spine 里的 {idref} 在 manifest 里找不到，已跳过")
            continue
        if not (item.media.endswith("xhtml+xml") or item.media.endswith("html")):
            # 非 XHTML 的 spine 文档（text/xml 正文、SVG 漫画整本等）不能按
            # XHTML 解析，只能跳过——但"整本无声丢光"不可接受，聚合点名。
            skipped_media[item.media or "（无 media-type）"] += 1
            continue
        resolved = resolve(item.href, opf.base)
        if resolved is None:
            warnings.append(f"spine 文档 {item.href} 在包里找不到，已跳过")
            continue
        if resolved not in spine_docs:
            spine_docs.append(resolved)
    if skipped_media:
        _skipped_total = sum(skipped_media.values())
        _skipped_kinds = "、".join(
            f"{m}×{c}" for m, c in sorted(skipped_media.items(), key=lambda kv: -kv[1])
        )
        warnings.append(
            f"spine 里有 {_skipped_total} 个非 XHTML 文档（{_skipped_kinds}），"
            "不能重排，已整批跳过——正文可能缺章"
        )
    if not spine_docs:
        # 没有 spine 的包（不规范）：按 manifest 里 xhtml 的声明顺序排。
        warnings.append("OPF 里没有可用的 spine：按 manifest 顺序读正文")
        for item in opf.items.values():
            if item.media.endswith("xhtml+xml"):
                resolved = resolve(item.href, opf.base)
                if resolved and resolved not in spine_docs:
                    spine_docs.append(resolved)
    if not spine_docs:
        raise EpubReadError("这本 EPUB 里没有任何正文文档（spine 与 manifest 都是空的）")

    # spine 校验通过 = 确定要往下读了，这时候才建图片目录（见上面 out_dir 的注释）。
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    # --- 要整篇跳过的文档：目录页、封面页 ---
    skip_docs: set[str] = set()
    for item in opf.items.values():
        if "nav" in item.properties:
            resolved = resolve(item.href, opf.base)
            if resolved:
                skip_docs.add(resolved)
    if opf.cover_page_href:
        resolved = resolve(opf.cover_page_href, opf.base)
        if resolved:
            skip_docs.add(resolved)

    # --- 目录 ---
    nav_nodes: list[_NavNode] = []
    ncx_item = next(
        (i for i in opf.items.values() if i.media == "application/x-dtbncx+xml"), None
    )
    if ncx_item is not None:
        ncx_name = resolve(ncx_item.href, opf.base)
        if ncx_name:
            nav_nodes = _parse_ncx(
                zf, ncx_name, posixpath.dirname(ncx_name), resolve, warnings
            )
    if not nav_nodes:
        nav_item = next((i for i in opf.items.values() if "nav" in i.properties), None)
        if nav_item is not None:
            nav_name = resolve(nav_item.href, opf.base)
            if nav_name:
                nav_nodes = _parse_nav(
                    zf, nav_name, posixpath.dirname(nav_name), resolve, warnings
                )
    if not nav_nodes:
        warnings.append("这本书没有可用的 NCX/nav 目录：按 spine 顺序一章一文件切分")

    flat = _flatten(nav_nodes)
    nodes_by_doc: dict[str, list[_NavNode]] = {}
    for node in flat:
        if node.doc:
            nodes_by_doc.setdefault(node.doc, []).append(node)

    # --- 第一遍：统计图片引用（装饰图/孤儿图判定要用全书计数） ---
    img_refs: Counter[str] = Counter()
    css_refs: Counter[str] = Counter()
    img_re = re.compile(
        r"""(?:src|data-src|xlink:href|href)\s*=\s*["']([^"']+)["']""", re.I
    )
    for name in spine_docs:
        if name in skip_docs:
            continue
        try:
            data = zf.read(name)
        except KeyError:
            continue
        text = data.decode("utf-8", "replace")
        for m in img_re.finditer(text):
            target = _resolve_ref(m.group(1), posixpath.dirname(name), resolve)
            if target and target.lower().endswith(IMAGE_EXTS):
                img_refs[target] += 1
    for item in opf.items.values():
        if item.media != "text/css":
            continue
        css_name = resolve(item.href, opf.base)
        if not css_name:
            continue
        try:
            css_text = zf.read(css_name).decode("utf-8", "replace")
        except (KeyError, UnicodeDecodeError):
            continue
        for m in re.finditer(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css_text, re.I):
            target = _resolve_ref(m.group(1), posixpath.dirname(css_name), resolve)
            if target and target.lower().endswith(IMAGE_EXTS):
                css_refs[target] += 1

    manifest_images = {
        resolved
        for item in opf.items.values()
        if item.media.startswith("image/")
        for resolved in [resolve(item.href, opf.base)]
        if resolved
    }
    decorative = sorted(
        n for n, count in img_refs.items() if count > DECORATIVE_THRESHOLD
    )
    orphans = sorted(
        n for n in manifest_images if n not in img_refs and n not in css_refs and n != cover_name
    )

    # --- 第二遍：切章节、落图片 ---
    front: list[Paragraph] = []
    chapters: list[Chapter] = []
    name_map: dict[str, str] = {}   # zip 内图片名 → 落盘文件名
    size_cache: dict[str, tuple[int, int]] = {}
    current: list[Paragraph] = front   # 当前"续写目标"（前言或某章的段落表）
    chapters_started = False
    front_entries = 0      # 被归进前言的目录条目数（版权/序/制作说明…）
    toc_dropped = 0        # 被当成目录页丢掉的条目数
    continuation_docs = 0  # 目录里没有、续接到当前章节的文档数
    generated_pages = 0    # 自家信息页（打包器写的元数据页），整页跳过
    doc_dir = [""]  # 当前文档目录（_emit 闭包用）

    def _resolve_ref_local(ref: str) -> str | None:
        return _resolve_ref(ref, doc_dir[0], resolve)

    def _emit(items: list[list]) -> None:
        pending_anchor = ""
        for item in items:
            if item[0] == "anchor":
                # 锚点先记下，交给紧跟着的那个文本块当它自己的 id ——
                # 段落自己的 id 在阅读器里就是跳转落点（注释块靠它被点开）。
                pending_anchor = item[1]
                continue
            if item[0] == "text":
                refs = item[3] if len(item) > 3 else []
                current.append(
                    Paragraph(
                        text=item[1],
                        kind=item[2],
                        anchor=pending_anchor,
                        refs=refs,
                    )
                )
                pending_anchor = ""
            elif item[0] == "image":
                _emit_image(item[1], item[2])

    def _emit_image(ref: str, caption: str) -> None:
        target = _resolve_ref_local(ref)
        if target is None:
            _warn_once(warnings, f"图片引用 {posixpath.basename(ref)} 在包里找不到，已跳过")
            return
        if target == cover_name:
            return  # 封面单独处理，不进正文
        if img_refs.get(target, 0) > DECORATIVE_THRESHOLD:
            return  # 装饰图：报告里已经点名
        if out_dir is None:
            _warn_once(warnings, "书里有插图，但没给图片目录（image_dir）：图片段已跳过")
            return
        if target in name_map:
            out_name = name_map[target]
        else:
            try:
                data = zf.read(target)
            except KeyError:
                _warn_once(warnings, f"图片 {posixpath.basename(target)} 在包里读不出来，已跳过")
                return
            ext = _ext_from_magic(data)
            if ext is None:
                _warn_once(warnings, f"图片 {posixpath.basename(target)} 的格式不认识（只支持 jpg/png/gif），已跳过")
                return
            out_name = f"img{len(name_map) + 1:03d}{ext}"
            (out_dir / out_name).write_bytes(data)
            name_map[target] = out_name
            size_cache[target] = _image_size(data)
        width, height = size_cache.get(target, (0, 0))
        current.append(
            Paragraph(
                text=out_name,
                kind="image",
                caption=caption or "",
                width=width,
                height=height,
            )
        )

    def _new_chapter(title: str, level: int, items: list[list]) -> Chapter:
        nonlocal current
        ch = Chapter(title=title, level=level, start_line=0)
        heading = parse_heading(title)
        ch.number = heading.number if heading is not None else None
        chapters.append(ch)
        current = ch.paragraphs
        _emit(_drop_duplicate_heading(items, title))
        return ch

    if not nav_nodes:
        # 没有目录：spine 一文档一章，标题取正文里第一个标题块（没有就用文件名）。
        for name in spine_docs:
            if name in skip_docs:
                continue
            doc_dir[0] = posixpath.dirname(name)
            try:
                items = _parse_doc(zf.read(name))
            except KeyError:
                continue
            if _is_generated_page(items):
                # 自家信息页（书名/来源/章节数/生成时间）：打包器自己写的元数据，
                # 不是书的内容。跳过，否则重排一遍就多攒一段"章节数：N"。
                generated_pages += 1
                continue
            title = ""
            for item in items:
                if item[0] == "text" and item[2] == "heading":
                    title = item[1]
                    break
            if not title:
                title = posixpath.splitext(posixpath.basename(name))[0]
            if not chapters:
                # 第一页之前若有零散文字（版权/扉页），先留在前言里
                pass
            _new_chapter(title, 1, items)
            chapters_started = True
    else:
        leftover: set[int] = set(node.order for node in flat)
        for name in spine_docs:
            if name in skip_docs:
                continue
            doc_dir[0] = posixpath.dirname(name)
            try:
                items = _parse_doc(zf.read(name))
            except KeyError:
                continue
            nodes = nodes_by_doc.get(name, [])
            if not nodes:
                if _is_generated_page(items):
                    # 自家信息页：见上面的说明，整页跳过。
                    generated_pages += 1
                    continue
                # 目录里没有的文档：续接到当前章节（分片续页）或前言。
                continuation_docs += 1
                _emit(items)
                continue
            positions = _split_positions(items, nodes)
            if positions and positions[0] > 0:
                _emit(items[: positions[0]])
            for i, node in enumerate(nodes):
                end = positions[i + 1] if i + 1 < len(nodes) else len(items)
                seg = items[positions[i]:end]
                leftover.discard(node.order)
                title = _clean_text(node.title)
                if _is_toc_title(title):
                    toc_dropped += 1
                    continue  # 目录页：丢弃
                if not chapters_started and _is_front_title(title):
                    # 前缀位置的"序/版权/制作说明"归前言：不建章节。
                    front_entries += 1
                    current = front
                    _emit(_drop_duplicate_heading(seg, title))
                    continue
                if not title:
                    title = posixpath.splitext(posixpath.basename(name))[0]
                level = 2 if node.children else 1
                _new_chapter(title, level, seg)
                chapters_started = True
        if leftover:
            warnings.append(f"目录里有 {len(leftover)} 个条目指向的文档不在 spine 里，已跳过")

    # --- 分片合并 ---
    merged, groups, pieces = _merge_pieces(chapters)

    # --- 组装 Book ---
    book_title = opf.title or src.stem
    book = Book(
        title=book_title,
        author=opf.author,
        encoding="utf-8",
        source=str(src),
        front_matter=front,
        chapters=merged,
        tail_matter=[],
    )
    # 全书都解析完了，现在才有资格判"这个引用指向的注释到底在不在"。
    _finalize_refs(book)

    fonts = sum(
        1 for item in opf.items.values()
        if "font" in item.media or item.media.endswith((".ttf", ".otf", ".woff"))
    )

    # --- 封面落盘 ---
    # 封面不进正文（`_emit_image` 里显式跳过），但"重新打包时想沿用原封面"
    # 需要一个真实文件路径。所以在给了 image_dir 时把它单独写出去，
    # 文件名固定成 cover<ext>：它和 img001.png 那种正文插图不是一类东西，
    # 混在同一个编号序列里会让人以为它也是正文图。
    cover_file = ""
    if cover_name and out_dir is not None:
        try:
            data = zf.read(cover_name)
        except KeyError:
            _warn_once(warnings, "封面在包里读不出来，已跳过")
            data = b""
        if data:
            ext = _ext_from_magic(data)
            if ext is None:
                _warn_once(warnings, "封面的格式不认识（只支持 jpg/png/gif），已跳过")
            else:
                cover_file = f"cover{ext}"
                (out_dir / cover_file).write_bytes(data)

    report = {
        "source": str(src),
        "title": book_title,
        "author": opf.author,
        "epub_version": opf.version,
        "nav_entries": len(flat),
        "chapters": len(merged),
        "volumes": sum(1 for ch in merged if ch.level == 2),
        "merged_groups": groups,
        "merged_pieces": pieces,
        "front_matter_entries": front_entries,
        "toc_dropped": toc_dropped,
        "continuation_docs": continuation_docs,
        "generated_pages": generated_pages,
        "empty_chapters": sum(1 for ch in merged if not ch.paragraphs),
        "front_matter_paragraphs": len(front),
        "images_kept": len(name_map),
        "images_kept_origin": [posixpath.basename(n) for n in name_map],
        "images_decorative": [posixpath.basename(n) for n in decorative],
        "images_orphan": [posixpath.basename(n) for n in orphans],
        "cover": posixpath.basename(cover_name) if cover_name else None,
        "cover_file": cover_file,
        "fonts": fonts,
        "warnings": warnings,
    }
    return EpubImport(book=book, report=report)


def _resolve_ref(ref: str, base: str, resolve) -> str | None:
    """把文档里的相对引用（src/href/url()）解析成 zip 条目名。"""
    if not ref:
        return None
    raw = ref.strip()
    if raw.startswith(("http://", "https://", "data:", "//", "#")):
        return None
    return resolve(raw, base)


def _first_image_in_doc(zf: zipfile.ZipFile, doc: str, opf_base: str, resolve) -> str | None:
    """从封面页里剥出第一张位图（含 <svg><image xlink:href=...> 的包壳写法）。"""
    try:
        data = zf.read(doc).decode("utf-8", "replace")
    except KeyError:
        return None
    for m in re.finditer(
        r"""<(?:img|image)\b[^>]*?(?:src|xlink:href|href)\s*=\s*["']([^"']+)["']""",
        data,
        re.I,
    ):
        target = _resolve_ref(m.group(1), posixpath.dirname(doc), resolve)
        if target and target.lower().endswith(IMAGE_EXTS):
            return target
    return None
