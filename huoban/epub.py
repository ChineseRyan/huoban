"""章节树 → EPUB3 包（纯标准库 zipfile 手写，不引 ebooklib）。

为什么自己拼 zip：ebooklib 是 AGPL，一旦链接它的代码，整个项目就被拖进 AGPL；
而 EPUB 的容器格式本身只是"一个固定顺序的 zip + 几份 XML"，手写完全可控，
出兼容问题时还能逐字节对着规范查。

本模块只认章节树（model.Book）：不碰原文、不碰编码、不碰文件名。清洗与打包
通过章节树解耦（项目铁律 4），以后加 AZW3 渲染器时，清洗逻辑一行都不用改。

几条在实现里反复出现的取舍：

1. **两套目录（nav.xhtml + toc.ncx）**：EPUB3 阅读器看 nav，老阅读器（含部分
   国产阅读 App）只看 ncx。两份都由同一个章节树生成、结构同构 —— 只写一份的
   下场是"在新阅读器里能跳转、在老阅读器里目录是空的"。
   nav.xhtml 只当导航文档用，**不进 spine**：两种阅读器都自带目录入口，
   正文前再摆一页纯链接的目录，读者翻页时得先过一页空壳（2026-11-06 按用户
   反馈去掉，与 2026-10-28 删 info.xhtml 是同一类冗余）。
2. **mimetype 必须是第一个条目且 ZIP_STORED**：这是 OCF 规范唯一的硬性布局
   要求，不少阅读器/校验工具靠"读 zip 头部固定偏移"判定这是不是 EPUB，
   压缩过的 mimetype 会被直接判成"不是电子书"。
3. **确定性**：同样的输入 + 同样的 modified/identifier 必须产出逐字节相同的包。
   所以每个 ZipInfo 的 date_time 钉死成 (1980,1,1,0,0,0)、create_system 钉死，
   不把本机时间、绝对路径、随机数写进任何条目。否则两份"同一本书"的 epub
   在 diff 里全是噪声，也没法用哈希做缓存。
4. **控制字符必须剔除**：XML 1.0 不允许 \x00-\x08、\x0b、\x0c、\x0e-\x1f，
   脏 TXT 里这些字符极常见（尤其 \x0c 分页符）。带一个进包，整章 xhtml 就是
   "格式不良"，阅读器报"文件已损坏"或干脆白屏，而且报错只说第几行，
   根本查不出是哪个字符。宁可静默剔除，也不能让它进包。
5. **一章一个文件**：不把整本拼成一个巨大 xhtml，是因为手机阅读器渲染几 MB 的
   单个 xhtml 会卡，而且导航跳转粒度会退化成"整本一节"。
6. **字体默认不内嵌**：中文字库动辄 10~20 MB，一本 500 KB 的小说会胖成几十 MB；
   而宋体/楷体在苹果、Windows、安卓上基本都有。所以默认只写系统字体 + 长回退链。
   要内嵌时传 fonts=True，由 fonts.plan_fonts 把字库裁成"只含本书用到的字"
   （实测一本 2698 字种的书，思源宋体子集约 1.1 MB），代价可控。
7. **内嵌字体条目必须 STORED 且不参与压缩**：字体文件本身是二进制表，deflate
   只能压掉 3~5%，却会让 zip 目录里出现"声明不压缩、实际压缩"的不一致；
   更实际的原因是部分阅读器对字体条目按偏移直接映射，压缩过的要额外解压一次。

包里不再有"业务外"的页面：曾经有一页 info.xhtml（书籍信息页）写来源文件、章节数、
生成时间与「由 txt2epub 生成」，2026-10-28 按试用反馈（"生成的 EPUB 里不要有推广页"）
整页删掉了。现在包内只有封面、正文、封底、样式与（可选）字体/图片；nav.xhtml 仍在
包里，但它只是一份导航文档（manifest 里带 properties="nav"），不再占一页正文。
封底（Text/back.xhtml）是 2026-11-06 加的：整页只有纸纹和右下角一枚印，落款、细线、
边框一律没有 —— 一本书翻到最后该有"到此为止"的收束，而不是翻完末章直接跳回书架。
它默认不产（build_epub 的 back_cover 默认为 None）：不传与传 None 完全一样，封底这一维
不影响老产物；但整包相对 0.1.2 已经变了（2026-11-06 nav.xhtml 移出 spine，OPF 少一行
itemref），逐字节回归基线以本版为准。
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from .fonts import BODY_FAMILY, EXTB_FAMILY, HEADING_FAMILY, FontPlan, plan_fonts
from .model import PARAGRAPH_KINDS, Book, Chapter, Paragraph

__all__ = ["EpubResult", "build_epub", "write_epub"]

# 封底页的指纹。封底页整页只有一张图、没有文字，重排时没法靠可见文字认出
# "这是打包器自己加的页"；改用一条 XHTML 注释当标记 —— 注释不进渲染、不会
# 被阅读器显示，epubread 解析时记成 ["marker", ...] 并整页跳过。
# 不复用 epubread 的 _GENERATED_PAGE_MARK（"由 txt2epub 生成"）：生成物里刻意
# 不留那句自带广告词（tests/test_epub.py 有断言），而且封底页上没有文字可写。
BACK_COVER_MARK = "back-cover"

# ---------- 常量 ----------

EPUB_MIMETYPE = b"application/epub+zip"
CONTAINER_PATH = "OEBPS/content.opf"

# zip 条目的时间戳固定成这个值：1980-01-01 是 ZIP 格式能表示的最早时间，
# 也是所有工具都接受的"零值"。固定它 = 同样的输入产出同样的字节。
ZIP_TIME = (1980, 1, 1, 0, 0, 0)

# 封面图扩展名 → (包内用的扩展名, media-type)。
# .jpeg 归一成 .jpg：阅读器对 .jpeg 的支持没有 .jpg 稳。
_IMAGE_TYPES = {
    ".jpg": ("jpg", "image/jpeg"),
    ".jpeg": ("jpg", "image/jpeg"),
    ".png": ("png", "image/png"),
    ".gif": ("gif", "image/gif"),
}

# XML 1.0 不允许出现的字符。除了规范点名的 \x00-\x08 / \x0b / \x0c / \x0e-\x1f，
# 这里额外剔掉三类"能让编码或解析直接失败"的字符：
#   \x7f        DEL，规范允许但阅读器普遍显示成方块乱码；
#   \ud800-\udfff 孤立代理项，.encode("utf-8") 时会抛 UnicodeEncodeError；
#   \ufffe/\uffff 规范明确禁止。
_ILLEGAL_XML_CHARS = frozenset(
    [chr(c) for c in range(0x00, 0x09)]
    + [chr(0x0B), chr(0x0C)]
    + [chr(c) for c in range(0x0E, 0x20)]
    + [chr(0x7F)]
    + [chr(c) for c in range(0xD800, 0xE000)]
    + [chr(0xFFFE), chr(0xFFFF)]
)

# 段落在 xhtml 里的元素形态：kind → (标签, class)。
# "blank" 不是元素而是分隔块，单独处理；契约外的 kind 由 _render_paragraphs
# 报一条警告后按正文处理（脏中间态优先"能出包"，而不是让整本书打包失败）。
_ELEMENT_BY_KIND = {
    "body": ("p", ""),
    "heading": ("h2", "sub"),
    "note": ("p", "note"),
    # 诗词/韵文：单独一类，样式在 CSS 的 p.verse 里（居中、不缩进、楷体）。
    "verse": ("p", "verse"),
    # 场景分隔线（整行只有"＊＊＊＊＊＊"这类装饰符号）：居中、不缩进。
    "sep": ("p", "sep"),
}


@dataclass(frozen=True)
class EpubResult:
    """一次打包的结果。

    为什么连 data 一起返回、而不是只写盘：GUI/CLI 可能需要先算哈希、先在内存里
    校验再决定落盘，只给路径的话这些都得再读一遍文件。data 就是最终字节，
    files 记录"按写入顺序"的条目名（测试和排查都靠它确认 mimetype 在第一）。
    """

    data: bytes          # 完整的 epub 字节流
    files: list[str]     # 包内条目名（按写入顺序）
    chapters: int        # 写进 spine 的章节文件数
    paragraphs: int      # 写进正文的段落数
    warnings: list[str]  # 打包期发现的问题（不中断）


# ---------- 文本清洗与转义 ----------


def _sanitize(text: str) -> str:
    """剔除 XML 非法字符；其余字符一个不动。

    为什么是"剔除"而不是"替换成空格"：这些字符在原文里都是排版噪声
    （分页符、空字节、乱码残留），塞个空格反而会在句子中间造出断裂的空白。
    """
    if not text:
        return ""
    return "".join(ch for ch in text if ch not in _ILLEGAL_XML_CHARS)


def _esc(text: str) -> str:
    """XML 文本转义。

    只转 & < > 三个：引号在文本节点里不是特殊字符，转多了会让 xhtml 读起来
    一堆 &quot;，反而难比对。属性值另有 _esc_attr。
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(text: str) -> str:
    return _esc(text).replace('"', "&quot;").replace("'", "&#39;")


def _text_of(value) -> str:
    """把任意值变成去首尾空白的字符串；None 变空串。"""
    if value is None:
        return ""
    return str(value).strip()


def _format_modified(modified) -> str:
    """把 modified 归一成 EPUB3 要求的 YYYY-MM-DDTHH:MM:SSZ。

    接受三种输入：None（取当前 UTC）、datetime（无时区的按 UTC 理解，
    不做"猜本机时区"这种事）、字符串。
    为什么不猜本机时区：同一本书在两个时区的机器上打包会得到不同的字节，
    确定性就没了；而 UTC 无歧义。

    字符串**不再原样透传**：dcterms:modified 的格式是 EPUB3 的硬性要求，
    传 "2026-10-24" 这种写法会被校验工具判不合规，而阅读器只会静默忽略
    这个字段。这里把最常见的两种简写补齐（纯日期补零点、缺 Z 的补 Z），
    实在认不出来就抛 ValueError —— 与其打出一个"元数据悄悄坏掉"的包，
    不如让调用方当场改对。
    """
    if modified is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(modified, datetime):
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=timezone.utc)
        return modified.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = _sanitize(_text_of(modified))
    # 只有 None 才代表"没指定、用当前时间"。空串是调用方给了个空值，
    # 那多半是上游字段没填对 —— 静默当成"当前时间"会把 bug 藏起来，
    # 而它表现出来的现象是"同样的输入两次打包字节不同"，极难查。
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        return f"{raw}T00:00:00Z"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", raw)
    if m:
        return f"{m.group(1)}T{m.group(2)}Z"
    raise ValueError(
        f"modified 不是合法的时间格式：{raw!r}（要 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SSZ）"
    )


def _meta_text(value) -> str:
    """元数据字段的归一：去空白 + 剔除 XML 非法字符。

    为什么元数据也必须过 _sanitize：书名/作者取自 TXT 首行，而 encoding.py
    解码用的是 errors="replace" —— \x00 这类字符不是"解码错误"，会原样留下。
    正文那边早就过了 _sanitize，元数据这边漏一次，content.opf 就是不良构 XML，
    而 OPF 坏掉的包阅读器直接判"文件已损坏"，比正文某一章坏严重得多。
    """
    return _sanitize(_text_of(value))


def _resolve_identifier(identifier, book: Book, title: str) -> str:
    """生成/采用书的唯一标识（dc:identifier）。

    没显式给时用 uuid5(NAMESPACE_URL, 来源路径 + 书名)：uuid5 是"同样的种子
    永远得到同样的 UUID"，所以同一本书打两次包 identifier 一致，阅读器不会
    把它当成两本不同的书（否则书架里会堆一串重复项）。
    种子用换行分隔而非直接拼接，避免 ("a","bc") 与 ("ab","c") 撞成同一个种子。
    """
    explicit = _meta_text(identifier)
    if explicit:
        return explicit
    # 种子也要剔掉非法字符：uuid5 内部会对字符串做 UTF-8 编码，
    # 一个孤立代理项就会抛 UnicodeEncodeError，把"只是书名脏了"变成"打不出包"。
    seed = f"{_meta_text(book.source)}\n{_meta_text(title)}"
    return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


# ---------- 正文渲染 ----------


def _inline_html(text: str, refs: list[dict], href_of=None) -> str:
    """把一段文字渲成 HTML，顺便把行内引用（[1] 这类）变成可点开的链接。

    为什么值得单独做：EPUB 的"弹注"不是靠一段注释文字，而是靠
    正文里的 `<a epub:type="noteref" href="#id">` 指向注释块的 id。
    没有这层链接，注释就只是一堆排在章末的普通段落，阅读器不会弹窗，
    读者也没法从正文跳过去 —— 实测的"弹注全部失效"就是这个原因。

    href_of 决定链接指向哪儿：注释在别的章节文件里时必须是
    `chap0007.xhtml#id`，只写 `#id` 会跳到当前章里一个不存在的锚点。
    它返回 None 表示"这个落点全书都找不到"，那就只留文字不留链接。

    refs 的 at 是"插在第 at 个字符之前"。越界/逆序的条目一律跳过而不是钳制：
    钳制会把链接插到错误的位置，读者点开看到的是别的注，比点不开更糟。
    """
    if not refs:
        return _esc(text)
    parts: list[str] = []
    pos = 0
    for ref in sorted(refs, key=lambda r: r.get("at", 0)):
        try:
            at = int(ref.get("at", 0))
        except (TypeError, ValueError):
            continue
        note = _text_of(ref.get("note"))
        if at < pos or at > len(text) or not note:
            continue
        parts.append(_esc(text[pos:at]))
        label = _text_of(ref.get("label")) or "·"
        href = f"#{note}" if href_of is None else href_of(note)
        if href:
            parts.append(
                f'<a class="noteref" epub:type="noteref" href="{_esc_attr(href)}">'
                f"{_esc(label)}</a>"
            )
        else:
            # 全书都找不到这条注的落点：只留文字，不留死链接。
            parts.append(_esc(label))
        pos = at
    parts.append(_esc(text[pos:]))
    return "".join(parts)


def _para_html(
    tag: str, text: str, para: "Paragraph | None", cls: str, href_of=None
) -> str:
    """渲一个段落元素，带上 id（被链接的落点）与行内引用。

    note 段额外带 epub:type="footnote"：这是阅读器判断"这是一条注"的依据，
    只有它存在时正文里的 noteref 才会被当成弹注而不是普通跳转。
    """
    attrs = f' class="{cls}"' if cls else ""
    anchor = _text_of(getattr(para, "anchor", "")) if para is not None else ""
    if anchor:
        attrs += f' id="{_esc_attr(anchor)}"'
        if cls == "note":
            attrs += ' epub:type="footnote"'
    refs = list(getattr(para, "refs", []) or []) if para is not None else []
    return f"<{tag}{attrs}>{_inline_html(text, refs, href_of)}</{tag}>\n"


def _render_paragraphs(
    paragraphs: list[Paragraph],
    warnings: list[str],
    where: str,
    *,
    front: bool = False,
    images_ok: set[str] | None = None,
    href_of=None,
) -> tuple[str, int]:
    """把一串段落渲染成 xhtml 片段，返回 (片段, 已写入的元素个数)。

    处理顺序有讲究：
    1. 先按段落类型分类。kind 优先于内容 —— 声明成 blank 的段即使带了字也当空行，
       因为"这句是空行"是清洗层的判断，渲染层不推翻它。
    2. 段内若含换行（脏中间态：清洗把碎行拼错、或人手改了 JSON），按物理行
       拆成多个元素。绝不能把裸换行塞进 xhtml 文本节点 —— 阅读器会把它折叠成
       一个空格，整段排版就散了。
    3. 空行折叠：连续多个只出一个 <div class="sep">，章节首尾的空行直接丢。
       首尾空行在视觉上毫无意义（章标题上下本来就有 margin），留着只会在
       每章开头凭空多出一块空白。
    4. 每行文本 strip：中文小说里的行首空白只可能是排版残留（清洗层归一后的
       两个全角空格），而缩进由 CSS 的 text-indent 负责，留着会变成四格缩进。

    front=True 时多一条排版规则：**短行**（不超过 24 字、且不以句末标点收尾）
    按"书名页"样式居中不缩进。理由是真实样本里书的前言区往往就是
    "《书名》" + "作者" 两行 —— 按正文段缩进两端对齐，看着像正文开头，
    不像一页书名；而真正的前言散文（长句、带句号）仍走正文样式。

    image 段（EPUB 导入的插图）单独渲成 <figure>：它不是文字，不能走 splitlines，
    也不能被空行折叠吞掉。images_ok 是 build_epub 从 image_dir 里实际读到的
    文件名集合；给了集合却不在集合里 = 图片文件缺失，跳过并警告（宁缺勿裂图）。
    images_ok=None 表示"不做存在性检查"（直接调用本函数的测试用）。

    **连续的 note 段成组渲进 `<div class="notes">`**：它们是同一处注释块
    （EPUB 导入的多看式脚注在章末就是这样一串 li.duokan-footnote-item），
    散着放时每条 p.note 与正文段落在阅读器里长得差不多，读者分不清
    "这句是作者注"还是"漏了缩进的正文"。成组之后由 CSS 画一条中灰分隔线，
    视觉上才是一块"注释区"。不加上标编号：章节树里已经丢了引用处的锚点，
    造不出"第 N 条注对应正文哪个位置"的对应关系，硬编号等于编数据。
    """
    # 契约外的 kind 只报一次，按正文处理：脏中间态优先能出包，让用户先看到书。
    bad_kinds = sorted({_text_of(p.kind) for p in paragraphs if _text_of(p.kind) not in PARAGRAPH_KINDS})
    if bad_kinds:
        warnings.append(f"{where}出现契约外的段落类型 {bad_kinds}（已按正文处理）")

    # (元素种类, 文本, 段落)。种类 "gap" 是本函数内部的空行哨兵，**不是段落类型** ——
    # 段落类型里也有一个 "sep"（场景分隔线），两者必须分开：混用的话
    # 分隔线会被当成空行直接折叠掉（实测就是这么丢了 61 行 ＊＊＊＊＊＊）。
    # "image" 的第二个分量是 Paragraph 本身（它带 caption/width/height）。
    # 第三个分量保留原段落对象：note 段要用它的 anchor 当被链接的落点，
    # 正文段要用它的 refs 把 [1] 渲成能点开的链接。
    items: list[tuple[str, object, Paragraph | None]] = []
    for p in paragraphs:
        kind = _text_of(p.kind) or "body"
        if kind == "blank":
            items.append(("gap", "", None))
            continue
        if kind == "image":
            items.append(("image", p, p))
            continue
        if kind == "sep":
            # 场景分隔线**不看文本**：它渲成一条细横线，原来的装饰字符
            # （"＊＊＊＊＊＊"、"※※※※"、"─────"）一个字都不印。
            # 单独成项还有个作用：重排回来的 sep 段文本是空的（<hr> 没有
            # 文字），走下面的 splitlines 会被当成空行吞掉，回环不动点就破了。
            items.append(("sep", "", p))
            continue
        if kind not in _ELEMENT_BY_KIND:
            kind = "body"
        raw = _sanitize("" if p.text is None else str(p.text))
        # splitlines() 对 "" 返回 []，所以退化成一行空串 —— 空段照样算一个空行。
        lines = raw.splitlines() or [""]
        # refs 的 at 是"整段"的字符偏移；段被拆成多行时偏移就对不上了。
        # 这种情况只可能出现在脏中间态（手改 JSON 塞了换行），
        # 宁可退化成没有链接的纯文本，也不能把链接插到错误的位置。
        keep = p if len(lines) == 1 and p.refs else (p if p.anchor else None)
        for line in lines:
            line = line.strip()
            if line:
                items.append((kind, line, keep))
            else:
                items.append(("gap", "", None))

    # 折叠 + 掐头去尾。用一遍扫描而不是事后 replace("\n\n", ...)：
    # 字符串级替换分不清"连续空行"和"某段落自己的换行"。
    squeezed: list[tuple[str, object, Paragraph | None]] = []
    for kind, text, para in items:
        if kind == "gap":
            if not squeezed or squeezed[-1][0] == "gap":
                continue
            squeezed.append(("gap", "", None))
        else:
            squeezed.append((kind, text, para))
    while squeezed and squeezed[-1][0] == "gap":
        squeezed.pop()

    # 分隔线两侧的空行没有意义：<hr> 自带上下 margin，再垫一层空行，
    # 两段之间就会空出一大块（原书里"正文 / 空行 / ＊＊＊ / 空行 / 正文"
    # 这种写法很常见）。只去掉紧挨着分隔线的那一个。
    if any(item[0] == "sep" for item in squeezed):
        tight: list[tuple[str, object, Paragraph | None]] = []
        for item in squeezed:
            if item[0] == "gap" and tight and tight[-1][0] == "sep":
                continue
            if item[0] == "sep" and tight and tight[-1][0] == "gap":
                tight.pop()
            tight.append(item)
        squeezed = tight

    out: list[str] = []
    count = 0
    i = 0
    while i < len(squeezed):
        kind, text, para = squeezed[i]
        if kind == "gap":
            out.append('<div class="sep"></div>\n')
            i += 1
            continue
        if kind == "sep":
            # 一条细横线，不印字符。参考精排书（《死灵之书》）的做法：
            # <hr> + 1px 线。字符本身没有信息量（"＊＊＊＊＊＊"与"※※※※"
            # 意思完全一样），印出来只是把作者的装饰习惯带进成品。
            out.append('<hr class="sep"/>\n')
            count += 1
            i += 1
            continue
        if kind == "image":
            figure = _render_figure(text, warnings, where, images_ok)  # type: ignore[arg-type]
            if figure:
                out.append(figure)
                count += 1
            i += 1
            continue
        # 连续 note 段成组：见 docstring。中间夹了空行/正文就断组 ——
        # 两处注释之间的空行是原书的段落分隔，不是"同一块注释"的延续。
        if kind == "note":
            j = i
            while j < len(squeezed) and squeezed[j][0] == "note":
                j += 1
            inner = "".join(
                _para_html("p", t, pr, "note", href_of) for _, t, pr in squeezed[i:j]
            )
            out.append(f'<div class="notes">\n{inner}</div>\n')
            count += j - i
            i = j
            continue
        # 书名页样式只套在正文段上：heading/note 有各自的语义样式，
        # 换成 .front-title 会让它们同时丢掉 .sub/.note 又匹配不到
        # p.front-title，退化成浏览器默认的又大又粗。
        if front and kind == "body" and _is_title_page_line(text):
            out.append(_para_html("p", text, para, "front-title", href_of))
            count += 1
            i += 1
            continue
        tag, cls = _ELEMENT_BY_KIND[kind]
        out.append(_para_html(tag, text, para, cls, href_of))
        count += 1
        i += 1
    return "".join(out), count


# 图片按宽高比分流成四档（§12.2）。阈值是设计文档钉死的，别随手改：
# 竖图（漫画/人像/封面式插页）给 60%，方图 70%，宽横图 90%，极端横条 100%。
_IMAGE_TALL = 0.85
_IMAGE_WIDE = 1.6
_IMAGE_STRIP = 2.5


def _image_ratio_class(width: int, height: int) -> str:
    """按宽高比给出 figure 的档位 class。

    width/height 读不出（都是 0）时按方图兜底 —— 方图是最保守的档位：
    竖图排 60% 会让一张其实很宽的图缩得过小，方图 70% 两边都不难看。
    """
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError):
        return "img-square"
    if w <= 0 or h <= 0:
        return "img-square"
    ratio = w / h
    if ratio < _IMAGE_TALL:
        return "img-tall"
    if ratio < _IMAGE_WIDE:
        return "img-square"
    if ratio <= _IMAGE_STRIP:
        return "img-wide"
    return "img-strip"


def _warn_once(warnings: list[str], message: str) -> None:
    """同一条警告只记一次。

    一本书 45 张插图，缺 image_dir 时若每张都报一遍，报告会被同一句话刷屏，
    真正的其他警告反而被埋掉。
    """
    if message not in warnings:
        warnings.append(message)


def _render_figure(
    p: Paragraph,
    warnings: list[str],
    where: str,
    images_ok: set[str] | None,
) -> str | None:
    """把一个 image 段渲成 <figure><img/><figcaption/></figure>；不可渲染时返回 None。

    返回 None 的三种情况都**不抛异常**：脏中间态优先"能出包"，图片丢了要让人
    在 warnings 里看见，而不是让整本书打不出来。
    """
    name = _text_of(p.text)
    # 文件名合法性再兜一层（model.validate 已经会报，但打包层不能依赖上游校验）。
    if not name or "/" in name or "\\" in name or ".." in name:
        _warn_once(warnings, f"{where}图片段的文件名 {name!r} 不合法（只许纯文件名），已跳过")
        return None
    # 文件存不存在**不在这里报**：45 张图缺目录时每张报一句会把报告刷屏。
    # 存在性由 _load_chapter_images 一次性汇总报告，这里只负责不出裂图。
    if images_ok is not None and name not in images_ok:
        return None
    cls = _image_ratio_class(p.width, p.height)
    caption = _sanitize(_text_of(p.caption))
    cap_xml = f"<figcaption>{_esc(caption)}</figcaption>" if caption else ""
    alt = _esc_attr(caption) if caption else ""
    # 图片路径相对章节文件（Text/chapNNNN.xhtml）向上两级？不 —— Images/ 与
    # Text/ 同级，所以在 Text/ 下用 ../Images/xxx 正好回到 OEBPS 根。
    return (
        f'<figure class="img {cls}">'
        f'<img src="../Images/{_esc_attr(name)}" alt="{alt}"/>'
        f"{cap_xml}</figure>\n"
    )


_FRONT_TITLE_MAX = 24
_FRONT_TERMINAL = "。！？…；;.!?"


def _is_title_page_line(text: str) -> bool:
    """前言区里的一行是否该按"书名页"排版（居中、不缩进）。

    两个条件都满足才算：短（不超过 24 字），且不是完整句子（不以句末标点收尾）。
    "短"防止把一整段前言散文居中；"不是句子"防止把带句号的开场白居中 ——
    后者是正文，居中会把阅读节奏打断。
    """
    s = text.strip()
    if not s or len(s) > _FRONT_TITLE_MAX:
        return False
    return s[-1] not in _FRONT_TERMINAL


def _toc_title(chapter: Chapter) -> str:
    """目录里显示的章节标题（已转义，不含首尾空白）。"""
    return _esc(_sanitize(_text_of(chapter.title)))


def _chapter_filename(index: int) -> str:
    """第 index 章（1 基）的文件名。四位数编号：排序稳定，且到 9999 章都不乱。"""
    return f"Text/chap{index:04d}.xhtml"


def _level_of(ch: Chapter, where: str, warnings: list[str]) -> int:
    """把章节的 level 归一成目录用的 1（章）或 2（卷）。

    为什么统一在这里判：目录那边判卷用 `>= 2`、正文那边原来用 `== 2`，
    两处判据不一致时会出现"目录里 C 章挂到了 level=3 的章节底下，正文里
    又按章排版"这种自相矛盾的包。level>2（更深的层级）一律当卷处理：
    它既然是容器，就不该在目录里变成别人的子节点。
    """
    try:
        level = int(_text_of(ch.level))
    except (TypeError, ValueError):
        warnings.append(f"{where}的 level={_text_of(ch.level)!r} 不是数字（按章级处理）")
        return 1
    if level <= 1:
        return 1
    if level == 2:
        return 2
    warnings.append(f"{where}的 level={level} 不在 1/2 之内（按卷级处理）")
    return 2


def _render_chapters(
    book: Book,
    lang: str,
    book_title: str,
    warnings: list[str],
    *,
    images_ok: set[str] | None = None,
) -> tuple[list[tuple[str, str, str]], int]:
    """渲染所有章节文件，返回 ([(包内路径, 标题, 文档文本)], 段落总数)。

    为什么前言/后记要折进第一/最后一章的 xhtml：契约里的包结构是固定的
    （cover / nav / toc / css / chapNNNN），没有给"前言页"留位置。
    折进相邻章节是唯一"既不丢内容、又不多造文件"的办法。前言具体落在
    "第一个叶子章"上（见下方 front_at）：这样卷标题页上只剩卷标题，
    卷首那一页才真的是一页"分卷题头"。

    没有章节（只有前言后记的书，或清洗把标题全删了）时，前言后记本身就是
    全部正文 —— 单独渲成 chap0001.xhtml。**绝不能只报警告就丢掉**：
    那等于把整本书打成 8 个空壳，用户拿到手是一本翻不开的白书。
    """
    docs: list[tuple[str, str, str]] = []

    if not book.chapters:
        if not (book.front_matter or book.tail_matter):
            warnings.append("这本书没有任何章节，也没有前言后记：只打包信息页与空目录")
            return docs, 0
        anchors = {
            _text_of(getattr(p, "anchor", ""))
            for p in list(book.front_matter) + list(book.tail_matter)
        }
        anchors.discard("")

        def _href_here(anchor: str):
            # 全书的正文都挤在同一个文件里，锚点直接在本篇。
            return f"#{anchor}" if anchor in anchors else None

        front_xml, front_count = _render_paragraphs(
            book.front_matter, warnings, "正文部分", front=True, images_ok=images_ok,
            href_of=_href_here,
        )
        tail_xml, tail_count = _render_paragraphs(
            book.tail_matter, warnings, "正文部分", images_ok=images_ok,
            href_of=_href_here,
        )
        title_text = _sanitize(_text_of(book_title)) or "正文"
        doc = _xhtml_doc(lang, title_text, front_xml + tail_xml, "../Styles/main.css")
        _selfcheck("只有前言后记的正文页", doc, warnings)
        docs.append((_chapter_filename(1), title_text, doc))
        warnings.append(
            f"这本书没有切出章节：把前言 {len(book.front_matter)} 段、"
            f"后记 {len(book.tail_matter)} 段合成一页正文 chap0001.xhtml"
        )
        return docs, front_count + tail_count

    body_paragraphs = 0
    last = len(book.chapters)
    # 前言折进"第一个叶子章"（level=1）而不是第 1 章：第 1 章若是卷标题页，
    # 前言挤在卷标题同一页上，那一页就不像"卷首"而像一页大杂烩了。
    # 全书都是卷（没有叶子章）时退回第 1 章 —— 内容绝不能因为找不到落点而丢。
    front_at = 1
    for idx, ch in enumerate(book.chapters, start=1):
        if not _is_volume(ch):
            front_at = idx
            break

    # 全书锚点 → 所在文件。注释可能排在离引用很远的一章（Calibre 习惯把
    # 全书注释集中到"注释"一章），链接必须写成跨文件路径才跳得过去。
    # 先建表再渲染：渲染正文时还不知道注释落在哪一章。
    anchor_doc: dict[str, str] = {}
    for idx, ch in enumerate(book.chapters, start=1):
        for para in ch.paragraphs:
            anchor = _text_of(getattr(para, "anchor", ""))
            if anchor:
                anchor_doc.setdefault(anchor, _chapter_filename(idx))
    for para in book.front_matter:
        anchor = _text_of(getattr(para, "anchor", ""))
        if anchor:
            anchor_doc.setdefault(anchor, _chapter_filename(front_at))
    for para in book.tail_matter:
        anchor = _text_of(getattr(para, "anchor", ""))
        if anchor:
            anchor_doc.setdefault(anchor, _chapter_filename(last))

    def _href_maker(here: str):
        """给出"从 here 这个文件跳到 anchor 落点"的相对链接。

        章节文件全在 Text/ 下（_chapter_filename 保证），所以同目录之间
        用文件名就够，不需要 ../ 之类的相对路径计算。
        """
        here_name = here.rsplit("/", 1)[-1]

        def _href(anchor: str):
            target = anchor_doc.get(anchor)
            if not target:
                return None
            if target == here:
                return f"#{anchor}"
            return f"{target.rsplit('/', 1)[-1]}#{anchor}"

        return _href

    front_xml, front_count = _render_paragraphs(
        book.front_matter, warnings, "前言部分", front=True, images_ok=images_ok,
        href_of=_href_maker(_chapter_filename(front_at)),
    )
    tail_xml, tail_count = _render_paragraphs(
        book.tail_matter, warnings, "后记部分", images_ok=images_ok,
        href_of=_href_maker(_chapter_filename(last)),
    )
    for idx, ch in enumerate(book.chapters, start=1):
        where = f"第 {idx} 个章节"
        level = _level_of(ch, where, warnings)
        title_text = _sanitize(_text_of(ch.title))
        if not title_text:
            warnings.append(f"{where}标题为空：目录里会出现一个空白条目")

        # h1 一律写在文件里（需求：标题文本必须能在章节文件里找到）。
        # level=2 的卷/部标题用 .vol 加大字号与留白 —— 它是"分卷"这种大结构。
        cls = ' class="vol"' if level == 2 else ""
        body = f"<h1{cls}>{_esc(title_text)}</h1>\n"

        if idx == front_at and front_xml:
            # 前言放在章标题之前：它是整本书的开场，不能躲在第一章标题下面。
            body = front_xml + body

        ch_xml, ch_count = _render_paragraphs(
            ch.paragraphs, warnings, where, images_ok=images_ok,
            href_of=_href_maker(_chapter_filename(idx)),
        )
        body += ch_xml
        body_paragraphs += ch_count

        if idx == last and tail_xml:
            body += tail_xml

        doc = _xhtml_doc(lang, title_text or book_title, body, "../Styles/main.css")
        _selfcheck(where, doc, warnings)
        docs.append((_chapter_filename(idx), title_text, doc))

    total = body_paragraphs + front_count + tail_count
    return docs, total


def _selfcheck(
    where: str, doc: str, warnings: list[str], *, strict: bool = False
) -> None:
    """自检：生成的文档必须能被 XML 解析。

    为什么要这道看起来多余的检查：转义/控制字符漏一处，这一章在阅读器里
    就是白屏，而这种错误在开发期肉眼看不出来（文件本身是能打开的文本）。
    这里用标准库当场解析一遍，宁可多花几毫秒，也不让格式不良的章出门。

    两种失败策略，区别是"坏掉之后还有没有救"：
    - 章节 xhtml 坏了（strict=False）：只报警告。一章难看远好过整本书打不出来，
      而且坏一章时另外几十章还是好的，读者至少能读。
    - content.opf / nav.xhtml / toc.ncx 坏了（strict=True）：直接抛错。
      这三份是整个包的骨架，OPF 不良构等于阅读器根本认不出这是本书，
      交出去只会得到"文件已损坏"，比当场报错更难排查。
    """
    try:
        ET.fromstring(doc.encode("utf-8"))
    except ET.ParseError as exc:
        if strict:
            raise ValueError(f"{where}不是格式良好的 XML（{exc}）：这个包不能交付") from exc
        warnings.append(f"{where}生成的 xhtml 不是格式良好的 XML（{exc}）：这一章在阅读器里可能打不开")


# ---------- 各页面文档 ----------

_XHTML_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<!DOCTYPE html>\n"
    '<html xmlns="http://www.w3.org/1999/xhtml"'
    ' xmlns:epub="http://www.idpf.org/2007/ops"'
    ' xml:lang="{lang}" lang="{lang}">\n'
    "<head>\n"
    '<meta charset="utf-8"/>\n'
    "<title>{title}</title>\n"
    '<link rel="stylesheet" type="text/css" href="{css}"/>\n'
    "</head>\n"
    "<body>\n"
)


def _xhtml_doc(
    lang: str, title: str, body: str, css_href: str, *, body_attrs: str = ""
) -> str:
    """套上 xhtml 骨架。

    为什么同时写 xml:lang 和 lang：前者是 XML 的写法，后者是老 XHTML/浏览器
    认的写法。只写一个总有一部分阅读器拿不到语言，中文断行与字体回退会走偏。

    body_attrs 只给封面页与封底页用：封面是 ` class="cover-body" epub:type="cover"`
    （EPUB3 用 epub:type 标出"这一页是封面"，阅读器据此在书架上取封面、在
    "跳到封面"时定位；不写它，封面页只是一张普通的第一页），封底只有
    ` class="back-body"`（EPUB3 没有"封底"这个 epub:type，硬造一个反而会被
    epubcheck 判未知值）。两页都靠这个 class 去掉整页边距。
    """
    head = _XHTML_HEAD.format(
        lang=_esc_attr(lang), title=_esc(title), css=_esc_attr(css_href)
    )
    if body_attrs:
        head = head.replace("<body>\n", f"<body{body_attrs}>\n")
    return head + body + "</body>\n</html>\n"


def _cover_doc(lang: str, title: str, author: str, image_href: str | None) -> str:
    """封面页：有图就用整幅图，没图就用文字版（书名 + 作者）。

    为什么没图也要有封面页：这是"可选入口" —— 以后 GUI 加了选图功能，
    这条路已经通着；而且翻开第一屏就有明确的"这是一本书"的落点，
    比一上来直接是正文更像成品书。
    """
    if image_href:
        # 不写 width="100%"：EPUB3 里 img 的 width 必须是整数（像素），
        # 百分比是 HTML5 的写法，epubcheck 会判 RSC-005 不合规。
        # 撑满页面交给 CSS 的 .cover img { width: 100% }。
        body = (
            '<div class="cover">\n'
            f'<img src="{_esc_attr(image_href)}" alt="封面"/>\n'
            "</div>\n"
        )
    else:
        body = '<div class="cover-text">\n'
        body += f'<h1 class="cover-title">{_esc(title)}</h1>\n'
        if _text_of(author):
            body += f'<p class="cover-author">{_esc(_text_of(author))}</p>\n'
        body += "</div>\n"
    # class="cover-body" 让 CSS 去掉整页边距（封面要占满一屏），
    # epub:type="cover" 是 EPUB3 声明"这页是封面"的标准写法。
    return _xhtml_doc(
        lang,
        title,
        body,
        "../Styles/main.css",
        body_attrs=' class="cover-body" epub:type="cover"',
    )


def _back_doc(lang: str, title: str, image_href: str) -> str:
    """封底页：整页一张图（纸纹 + 右下角一枚印），没有任何文字。

    与 _cover_doc 同构，但只有"有图"这一条路：封底的全部内容就是那张图，
    没有图就没有封底页 —— 和封面不一样，封面缺图还能退回"书名 + 作者"的
    文字版，封底退回文字版就是一张白纸，纯属给读者多添一次翻页。

    正文第一行那条注释是给重排（epubread）认的：封底页没有文字，重排时
    没法靠可见文字判断"这页是打包器自己加的"，只能靠标记。见 BACK_COVER_MARK。
    """
    body = (
        f"<!-- {BACK_COVER_MARK} -->\n"
        '<div class="back">\n'
        f'<img src="{_esc_attr(image_href)}" alt="封底"/>\n'
        "</div>\n"
    )
    # 不给 epub:type：EPUB3 的结构语义表里没有"封底"，硬造一个未定义值
    # 会被 epubcheck 判成错误。class 只用来去掉整页边距。
    # head 里的 <title> 用书名而不是留空：空 title 虽然良构，但一部分校验器会
    # 记一条"标题为空"的警告，而这一页本来也没有别的字可写。
    return _xhtml_doc(
        lang, title, body, "../Styles/main.css", body_attrs=' class="back-body"'
    )


# ---------- 目录结构 ----------


def _is_volume(ch: Chapter) -> bool:
    """目录用的"这章是不是卷"判据（只读，不打警告）。

    level>2 也当卷：更深的层级既然存在，它就是容器而不是叶子，
    否则它下面的章在目录里会挂错父节点。正文那边由 _level_of 报一次警告，
    这里不重复报 —— 同一件事在日志里出现两遍会让人以为有两个问题。
    """
    try:
        return int(_text_of(ch.level)) >= 2
    except (TypeError, ValueError):
        return False


def _toc_nodes(book: Book) -> list[tuple[int, list[int]]]:
    """把章节树整理成目录的两个层级：[(顶层的章节下标, [它下面的章下标]), ...]。

    规则（契约）：level=2 是卷，level=1 是章。
    - 出现在任何卷之前的章，直接挂在顶层（不造一个假的"卷"把它们裹起来）；
    - 卷下面的章挂进该卷；
    - 卷本身没有下属章时也是叶子（"正文卷"这种光有标题没有章的形态确实存在）。
    没有卷的书，所有章都在顶层 —— 目录自然就是平铺的，不需要特判。
    """
    nodes: list[tuple[int, list[int]]] = []
    # 记住"当前卷"的位置，而不是"最后一个节点"：没有卷的书里，上一个顶层节点
    # 是一章而不是一卷，把下一章挂成它的子节点会把目录变成一条链。
    volume_at: int | None = None
    for idx, ch in enumerate(book.chapters):
        # 与正文用同一个 level 判据（_level_of 会打警告，这里只是读它的结论）：
        # 两处各自解释 level 是上一版的真 bug —— level=3 的章节在目录里被当成卷，
        # 在正文里却按章排版，读者点目录跳不到的层级就出来了。
        if _is_volume(ch):
            nodes.append((idx, []))
            volume_at = len(nodes) - 1
        elif volume_at is None:
            nodes.append((idx, []))
        else:
            nodes[volume_at][1].append(idx)
    return nodes


def _nav_ol(book: Book, nodes: list[tuple[int, list[int]]], depth: int) -> str:
    """递归生成 nav 的 <ol>。depth 只用来缩进（给人看），不影响语义。"""
    pad = "  " * depth
    if not nodes:
        # EPUB3 要求导航的 ol 里至少有一个 li。没有章节时（比如只有前言后记
        # 的书被打包失败、或空章节树）留一个空 ol 是硬性违规。
        # 占位项指向封面页：点进去至少不是"目录是空的"。
        # （这里原本指向我们自己的信息页，2026-10-28 按试用反馈删掉了那一页，
        # 占位项改指封面 —— cover.xhtml 是无条件存在的。）
        lines = [
            pad + "<ol>",
            f'{pad}  <li><a href="Text/cover.xhtml">（本书暂无章节）</a></li>',
            pad + "</ol>",
        ]
        return "\n".join(lines)
    lines = [pad + "<ol>"]
    for idx, children in nodes:
        href = _esc_attr(_chapter_filename(idx + 1))
        label = _toc_title(book.chapters[idx])
        # 卷行带 class="vol"：目录页里卷是"结构"、章是"内容"，光靠缩进不够 ——
        # 缩进在小屏上只差几个字符，读者扫目录时看不出哪几行是卷。
        vol_cls = ' class="vol"' if _is_volume(book.chapters[idx]) else ""
        if children:
            lines.append(f"{pad}  <li{vol_cls}>")
            lines.append(f'{pad}    <a href="{href}">{label}</a>')
            lines.append(_nav_ol(book, [(c, []) for c in children], depth + 2))
            lines.append(f"{pad}  </li>")
        else:
            lines.append(f'{pad}  <li{vol_cls}><a href="{href}">{label}</a></li>')
    lines.append(pad + "</ol>")
    return "\n".join(lines)


def _nav_doc(lang: str, book: Book) -> str:
    """EPUB3 导航文档：阅读器从侧栏/目录按钮打开的就是这一份。

    **它不在 spine 里**（2026-11-06 改）：读者翻页时不必先过一页纯链接的
    目录，导航入口交给阅读器自己的目录按钮；老阅读器靠 toc.ncx。两份目录
    仍然都要生成，缺一份就会出现"某些阅读器目录是空的"。

    为什么目录里只列章节、不列封面：读者要的是"跳到第几章"，
    封面在包里、阅读器另给入口，列进去只会让目录变长。
    （信息页已于 2026-10-28 按试用反馈删除，这里不再提及。）
    """
    nodes = _toc_nodes(book)
    body = '<nav epub:type="toc" id="toc">\n'
    body += '<h1 class="toc-title">目录</h1>\n'
    body += _nav_ol(book, nodes, 1) + "\n"
    body += "</nav>\n"
    return _xhtml_doc(lang, "目录", body, "Styles/main.css")


def _ncx_navpoints(book: Book, nodes: list[tuple[int, list[int]]], depth: int, counter: list[int]) -> str:
    """递归生成 navMap 的 navPoint。

    playOrder 必须按文档顺序连续递增（1,2,3…），不能按层级重置 ——
    有的老阅读器按 playOrder 排序而不是按文档顺序，重置会让卷下的章跑到前面。
    """
    pad = "  " * depth
    lines: list[str] = []
    for idx, children in nodes:
        counter[0] += 1
        n = counter[0]
        src = _esc_attr(_chapter_filename(idx + 1))
        lines.append(f'{pad}<navPoint id="navPoint-{n}" playOrder="{n}">')
        lines.append(f"{pad}  <navLabel>")
        lines.append(f"{pad}    <text>{_toc_title(book.chapters[idx])}</text>")
        lines.append(f"{pad}  </navLabel>")
        lines.append(f'{pad}  <content src="{src}"/>')
        if children:
            lines.append(
                _ncx_navpoints(book, [(c, []) for c in children], depth + 1, counter)
            )
        lines.append(f"{pad}</navPoint>")
    return "\n".join(lines)


def _ncx_doc(book: Book, title: str, book_id: str) -> str:
    """EPUB2 时代的 toc.ncx（给不认 nav.xhtml 的老阅读器）。"""
    nodes = _toc_nodes(book)
    depth = 2 if any(children for _, children in nodes) else 1
    navmap = _ncx_navpoints(book, nodes, 2, [0])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"'
        ' "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        "  <head>\n"
        f'    <meta name="dtb:uid" content="{_esc_attr(book_id)}"/>\n'
        f'    <meta name="dtb:depth" content="{depth}"/>\n'
        '    <meta name="dtb:totalPageCount" content="0"/>\n'
        '    <meta name="dtb:maxPageNumber" content="0"/>\n'
        "  </head>\n"
        "  <docTitle>\n"
        f"    <text>{_esc(title)}</text>\n"
        "  </docTitle>\n"
        "  <navMap>\n"
        f"{navmap}\n"
        "  </navMap>\n"
        "</ncx>\n"
    )


def _container_doc() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0"'
        ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        f'    <rootfile full-path="{CONTAINER_PATH}"'
        ' media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )


def _opf_doc(
    *,
    book_id: str,
    title: str,
    author: str,
    lang: str,
    stamp: str,
    generator: str,
    cover_name: str | None,
    cover_media: str | None,
    back_name: str | None,
    back_media: str | None,
    chapter_docs: list[tuple[str, str, str]],
    font_items: list[tuple[str, str, str]] = (),
    image_items: list[tuple[str, str, str]] = (),
) -> str:
    """content.opf：EPUB3 的清单，同时补 EPUB2 的 spine@toc 与 guide。

    为什么一个包里同时写 EPUB3 与 EPUB2 的东西：EPUB3 阅读器只看 nav，
    老阅读器只看 ncx + guide。多写这几行的成本是一次性的，收益是
    "拷到任何一台设备上都能打开、都能跳章"。
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
        f' unique-identifier="book-id" xml:lang="{_esc_attr(lang)}">',
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
        f'    <dc:identifier id="book-id">{_esc(book_id)}</dc:identifier>',
        f"    <dc:title>{_esc(title)}</dc:title>",
        f"    <dc:language>{_esc(lang)}</dc:language>",
    ]
    # 作者为空时**不输出** dc:creator：空元素会被阅读器显示成"作者：无"，
    # 比干脆没有作者字段更难看。
    if _text_of(author):
        lines.append(f"    <dc:creator>{_esc(author)}</dc:creator>")
    lines += [
        f"    <dc:date>{_esc(stamp)}</dc:date>",
        f'    <meta property="dcterms:modified">{_esc(stamp)}</meta>',
        f'    <meta name="generator" content="{_esc_attr(generator)}"/>',
    ]
    if cover_name:
        # EPUB2 的封面约定：<meta name="cover"> 指向 manifest 里图片的 id。
        lines.append('    <meta name="cover" content="cover-image"/>')
    lines.append("  </metadata>")
    lines.append("  <manifest>")
    lines += [
        '    <item id="nav" href="nav.xhtml"'
        ' media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '    <item id="css" href="Styles/main.css" media-type="text/css"/>',
        '    <item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>',
    ]
    # 封底页与封面页并列写在 manifest 里（2026-11-06 加）。有封底才有这一条：
    # back_name 是"包内图片路径"（Images/back.png），封底页本身恒为 Text/back.xhtml。
    if back_name:
        lines.append(
            '    <item id="back" href="Text/back.xhtml" media-type="application/xhtml+xml"/>'
        )
    # 内嵌字体放在封面图之前：manifest 的顺序不影响解析，但"字体紧跟在样式表
    # 后面"读起来更像一份说明，也与包内条目顺序（css → 字体 → 封面图）对齐。
    for font_id, href, media in font_items:
        lines.append(
            f'    <item id="{_esc_attr(font_id)}" href="{_esc_attr(href)}"'
            f' media-type="{_esc_attr(media)}"/>'
        )
    if cover_name:
        lines.append(
            f'    <item id="cover-image" href="{_esc_attr(cover_name)}"'
            f' media-type="{_esc_attr(cover_media or "image/jpeg")}"'
            ' properties="cover-image"/>'
        )
    if back_name:
        # 封底图**不带** properties="cover-image"：EPUB 里"封面图"只能有一张，
        # 再挂一次会让阅读器在书架上挑到封底去。它只是一张普通图片。
        lines.append(
            f'    <item id="back-image" href="{_esc_attr(back_name)}"'
            f' media-type="{_esc_attr(back_media or "image/png")}"/>'
        )
    # 章节内插图（EPUB 导入带进来的）：紧跟在封面图后面，与包内条目顺序
    # （封面图 → 章节内图片 → 正文）对齐。id 由文件名推出来，保证与 href 一致。
    for img_id, href, media in image_items:
        lines.append(
            f'    <item id="{_esc_attr(img_id)}" href="{_esc_attr(href)}"'
            f' media-type="{_esc_attr(media)}"/>'
        )
    for idx, (path, _title, _doc) in enumerate(chapter_docs, start=1):
        lines.append(
            f'    <item id="chap{idx:04d}" href="{_esc_attr(path)}"'
            ' media-type="application/xhtml+xml"/>'
        )
    lines.append("  </manifest>")
    # spine 顺序 = 读者翻页的顺序：封面 → 正文 → 封底。
    # nav.xhtml **不在这里**（2026-11-06 改）：它是导航文档，不是正文页。
    # 放进 spine 的后果是读者每开一本书都要先翻过一页纯链接的目录，
    # 而 EPUB3 阅读器本就有目录按钮、老阅读器有 toc.ncx —— 一页都不缺。
    lines.append('  <spine toc="ncx">')
    lines.append('    <itemref idref="cover"/>')
    for idx in range(1, len(chapter_docs) + 1):
        lines.append(f'    <itemref idref="chap{idx:04d}"/>')
    if back_name:
        lines.append('    <itemref idref="back"/>')
    lines.append("  </spine>")
    # guide 是 EPUB2 的"从这里开始读"提示，EPUB3 阅读器会忽略它，无害。
    lines.append("  <guide>")
    lines.append('    <reference type="cover" title="封面" href="Text/cover.xhtml"/>')
    lines.append('    <reference type="toc" title="目录" href="nav.xhtml"/>')
    if chapter_docs:
        lines.append(
            '    <reference type="text" title="正文"'
            f' href="{_esc_attr(chapter_docs[0][0])}"/>'
        )
    if back_name:
        # guide 的 type 是 EPUB2 的旧词汇表，里面本来就有 "back"（封底）。
        # 老阅读器靠它从"翻到书末"直接定位；EPUB3 阅读器忽略这一条，
        # 它靠 spine 顺序找到封底。两条路都通，所以两处都写。
        lines.append(
            '    <reference type="back" title="封底" href="Text/back.xhtml"/>'
        )
    lines.append("  </guide>")
    lines.append("</package>")
    return "\n".join(lines) + "\n"


# ---------- 样式表 ----------

# 说明为什么默认不内嵌 ttf：中文字库动不动 10~20 MB，一本几百 KB 的小说会被
# 撑成几十 MB，手机阅读器打开就卡。而宋体/楷体在主流系统上本来就有，
# 长回退链足以覆盖苹果（Songti SC/Kaiti SC）、Windows（宋体/楷体/SimSun）、
# 安卓与 Linux（Noto Serif CJK SC / Source Han Serif SC），最后兜底 serif。
#
# _CSS 里留了三个占位符，全部由 _css_text() 填：
# - __FONTS_BODY__ / __FONTS_HEAD__ 换成内嵌字体的 family 名，不内嵌时换成空串；
# - __BACK_CSS__ 换成封底页那三块规则（_BACK_CSS），没有封底页时换成空串。
# 不内嵌字体、不产封底时，占位符全换成空串，样式表与"从来没有封底这回事"时一字不差
# （tests/test_epub.py 的 case_css_contract 硬断言"没有 @font-face、没有 .ttf"，
#  case_back_cover_page 硬断言"不传封底时 main.css 里根本没有 .back"）。
# 封底页的三块规则单独拿出来，由 _css_text() 按需插进 _CSS 的 __BACK_CSS__ 处。
# 为什么不留死在 _CSS 里：不产封底页的书（CLI 的 epub 子命令、老调用方）不该被塞 20 行
# 用不上的死样式 —— 那会让"封底是可加可不加的一维"变成"加完以后所有书都变胖"。
_BACK_CSS = """/* 封底页：整页一张图（纸纹 + 右下角一枚印），没有任何文字。
   与封面同样整页占满 —— 封底页边上留一圈白页边，会像"图没贴好"。 */
.back {
    margin: 0;
    padding: 0;
    text-align: center;
    text-indent: 0;
}

.back img {
    width: 100%;
    max-width: 100%;
}

body.back-body {
    max-width: none;
    margin: 0;
    padding-bottom: 0;
}

"""

_CSS = """/* huoban 唯一样式表：只引用系统字体，不内嵌 ttf。 */

html, body {
    margin: 0;
    padding: 0;
}

body {
    /* __FONTS_BODY__ 是内嵌字体规划时插进来的 family 名（huoban-serif）。
       没有内嵌字体时它就是空串 —— 这一行随之变成原来的纯系统回退链。 */
    font-family: __FONTS_BODY__"Songti SC", "STSong", "\u5b8b\u4f53", "SimSun",
                 "Noto Serif CJK SC", "Source Han Serif SC", serif;
    font-size: 1em;
    line-height: 1.8;
    /* 注意：这里**不写 color**。深色模式下阅读器会自己注入前景/背景色，
       任何一个写死的 color 都会盖过它，在深色底上变成"黑字配黑底"。
       只有浅色模式才需要层次色，见样式表末尾的 prefers-color-scheme 块。 */
    /* 大屏（平板横屏、桌面阅读器）上限宽：一行超过 40 个汉字时，
       眼睛从行尾折回行首会找不到下一行。手机上 max-width 不生效。 */
    max-width: 40em;
    margin: 0 auto;
    /* 章末留白：一章的最后一页不该让正文顶到屏幕最底边（贴着底边的那行
       看着像"被切断了"，翻页时也分不清是不是还有下文）。 */
    padding-bottom: 2.5em;
}

/* 正文：2em 首行缩进是中文排版惯例；inter-ideograph 让两端对齐按字距伸缩，
   行尾不会剩出参差的锯齿。line-height 1.8 是长段中文的可读性下限。 */
p {
    text-indent: 2em;
    margin: 0.62em 0;
    text-align: justify;
    text-justify: inter-ideograph;
    line-height: 1.8;
    word-wrap: break-word;
    /* 孤行控制：段首/段尾只剩一行落在页边时，宁可整段挪页也不留"寡妇行"。 */
    orphans: 2;
    widows: 2;
}

/* 章标题：居中、不缩进、不加粗（中文标题用楷体本身就够"轻"，
   再加粗会显得又黑又重）。上下留白远大于段间距 —— 章与章之间的呼吸感
   几乎全来自这两个 margin 值。 */
h1 {
    text-indent: 0;
    font-weight: normal;
    text-align: center;
    font-family: __FONTS_HEAD__"Kaiti SC", "STKaiti", "\u6977\u4f53", "KaiTi",
                 "Noto Serif CJK SC", serif;
    font-size: 1.35em;
    line-height: 1.6;
    margin: 2.6em 0 2em;
    /* 每章另起一页：一章一个 xhtml 文件，多数阅读器本来就会在 spine 项之间
       分页，但 PDF 式渲染的引擎（以及"连续滚动"模式下切章）不一定。
       显式声明一次，两种引擎的行为就一致了。 */
    break-before: page;
    page-break-before: always;
    /* 颜色见样式表末尾 prefers-color-scheme: light 块。 */
}

/* 文档里的第一个元素不另起一页。
 * 每个章节文件本身就是"一页的开头"，在首个元素上再强制分页，WebKit 系的
 * 引擎会先吐一张空白页 —— 于是每章前面都多一张白纸。这条覆盖把
 * "章标题必另起一页"限制在真正需要的场合：前言折进同一文档、章标题
 * 前面还有内容时（那时 h1 不是第一个子元素，覆盖不生效，照旧分页）。 */
h1:first-child,
h1.vol:first-child {
    break-before: auto;
    page-break-before: auto;
}

/* 卷/部标题：比章标题更大、留白更多，字距略放开，一眼能看出这是大结构。 */
h1.vol {
    font-size: 1.6em;
    margin: 4em 0 3em;
    letter-spacing: 0.08em;
    /* 卷标题另起一页：分卷是全书最大的结构断层，挤在上一卷末章后面
       会让读者以为还在上一卷里。 */
    break-before: page;
    page-break-before: always;
}

/* 章内小标题：左对齐、加粗、比正文略小 —— 它从属于章，不能盖过章标题。 */
h2.sub {
    text-indent: 0;
    text-align: left;
    font-family: "Kaiti SC", "STKaiti", "\u6977\u4f53", "KaiTi", serif;
    font-size: 1.02em;
    font-weight: bold;
    line-height: 1.6;
    margin: 1.6em 0 0.8em;
    /* 颜色见样式表末尾 prefers-color-scheme: light 块。 */
}

/* 注释/按语：不缩进、小一号、浅一点，让它"退到背景里"，
   不打断正文的阅读节奏。 */
p.note {
    text-indent: 0;
    font-size: 0.9em;
    line-height: 1.7;
    margin: 0.5em 0;
}

/* 注释区：连续的 note 段由渲染层成组包进 <div class="notes">（见
   _render_paragraphs）。一条中灰细线 + 上留白，让读者一眼认出"注释从这里
   开始，到章末为止"，而不是把几条小字当成漏了缩进的正文。
   线色用 rgba 中灰而不是写死的十六进制：浅色底上是浅灰、深色底上是
   深灰，两边都不刺眼，也不违反"常规规则不写死 color"的深色模式契约
   （那是给**文字**颜色的约束，分隔线不跟随阅读器前景色才对）。
   注意：这里不能加"注释"两个字的标题 —— 章节树里没有这个信息，
   渲染层凭空加标题就是编内容。 */
.notes {
    margin-top: 2em;
    padding-top: 0.8em;
    border-top: 1px solid rgba(128, 128, 128, 0.45);
}

.notes p.note:first-child {
    margin-top: 0;
}

/* 诗词/韵文：居中、不缩进、换楷体、字号略小、行距收紧。
   为什么居中而不是像正文那样两端对齐：韵文每句本来就有自己的长度，
   拉开对齐会把"七言/五言"的节奏感拉平。行距 1.55 比正文 1.8 紧，
   是为了让一阕词在视觉上仍是一"块"，而不是散成几行正文。
   段间距 0.28em：句与句之间要近，块与块之间要远 —— 这个值撑住"块"。 */
p.verse {
    text-indent: 0;
    text-align: center;
    font-family: "Kaiti SC", "STKaiti", "\u6977\u4f53", "KaiTi", serif;
    font-size: 0.96em;
    line-height: 1.55;
    margin: 0.28em 0;
    /* 同 body：不写 color。诗词曾经写死 #33302e，深色模式下整块糊成
       一坨看不清的字 —— 排版层次交给字号/缩进/居中，颜色交给阅读器。 */
}

/* 韵文块与正文之间各留一点空：块内行距 0.28em 是"块"的紧，块外必须
   拉开一档，否则诗词上下与正文糊在一起，"这是一首诗"的边界就没了。
   只对"正文↔诗词"的相邻边界生效，诗词行与行之间仍是 0.28em。 */
p:not(.verse) + p.verse,
p.verse + p:not(.verse) {
    margin-top: 0.9em;
}

/* 场景分隔线：一条 1px 细横线，**不印原来的装饰字符**。
   参考精排书的做法（《死灵之书》：hr + border-top:1px solid）。
   为什么不留字符：那串"＊＊＊＊＊＊"没有信息量，印出来只是把作者
   连载时的装饰习惯带进成品；而且不同书写法千奇百怪（﹡×＝─ 都有），
   统一成一条线，全书的分隔才是同一个意思。
   颜色写死一个中灰而不是 currentColor：Kindle/AZW3 对 currentColor 的
   支持不一致，写死至少两边都看得见。 */
hr.sep {
    width: 100%;
    height: 0;
    margin: 1.5em 0;
    border: 0;
    /* 不写颜色：border 默认跟随 currentColor，深色模式下就是阅读器的
       前景色。浅色模式的层次色在下面的 light 媒体查询里补（与 p.verse
       同一套规矩：常规规则里不许写死 color）。 */
    border-top: 1px solid;
}

/* 场景分隔：只给高度，不画线。中文小说里一条横线会把连续叙事切得很碎。
   min-height 兜底：只写 height 时空 div 在部分引擎（AZW3/Kindle）里会被
   折成 0 高，分隔符直接消失。 */
div.sep {
    height: 1.1em;
    min-height: 1.1em;
}

/* 插图（EPUB 导入带进来的）。四档按宽高比分流，只改容器宽度不改图片本身：
   竖图（人像/插页）排 60%，方图 70%，宽横图 90%，极端横条（长条漫画格）100%。
   图片一律 max-width:100% + max-height:95vh —— 上限交给阅读器视口，
   一张 2480×3508 的封面式插页才不会把屏幕撑爆。
   位置原样保留在正文流里，不搬到章末：图在该出现的地方出现，是重排的底线。
   不写 color/边框/底色：图说只是"退到背景里"的一行小字。 */
figure.img {
    text-indent: 0;
    text-align: center;
    margin: 1.2em auto;
    page-break-inside: avoid;
    break-inside: avoid;
}

figure.img img {
    max-width: 100%;
    max-height: 95vh;
    height: auto;
}

figure.img-tall {
    max-width: 60%;
}

figure.img-square {
    max-width: 70%;
}

figure.img-wide {
    max-width: 90%;
}

figure.img-strip {
    max-width: 100%;
}

figcaption {
    text-indent: 0;
    text-align: center;
    font-family: "Kaiti SC", "STKaiti", "\u6977\u4f53", "KaiTi", serif;
    font-size: 0.9em;
    line-height: 1.6;
    margin-top: 1em;
}

/* 封面页 */
.cover {
    margin: 0;
    padding: 0;
    text-align: center;
    text-indent: 0;
}

.cover img {
    width: 100%;
    max-width: 100%;
}

/* 封面页整页占满：EPUB3 的封面页不该带页边距。 */
body.cover-body {
    max-width: none;
    margin: 0;
    /* 正文页底部的留白是给"章末"用的，封面页不需要。 */
    padding-bottom: 0;
}

/* 前言区的书名/作者行：居中、不缩进、字距略开。
   真实样本的前言区往往就是"《书名》"+"作者"两行；按正文段排版
   （两字缩进 + 两端对齐）看着像正文开头，不像一页书名。 */
p.front-title {
    text-indent: 0;
    text-align: center;
    letter-spacing: 0.08em;
    margin: 1.6em 0;
    font-family: "Kaiti SC", "STKaiti", "\u6977\u4f53", "KaiTi", serif;
}

/* 没有封面图时的文字封面：靠上留白 30% 左右，视觉重心在屏幕上半部。 */
.cover-text {
    margin: 32% 0 0;
    text-align: center;
    text-indent: 0;
}

.cover-title {
    text-indent: 0;
    font-family: "Kaiti SC", "STKaiti", "\u6977\u4f53", "KaiTi", serif;
    font-size: 2.1em;
    font-weight: normal;
    line-height: 1.5;
    letter-spacing: 0.12em;
    margin: 0 0 1.4em;
}

.cover-author {
    text-indent: 0;
    font-size: 1.05em;
    letter-spacing: 0.3em;
    margin: 0;
}

__BACK_CSS__/* 目录页：去掉列表符号，卷下的章缩进一级。 */
.toc-title {
    text-indent: 0;
    text-align: center;
    font-family: "Kaiti SC", "STKaiti", "\u6977\u4f53", "KaiTi", serif;
    font-size: 1.5em;
    font-weight: normal;
    margin: 2em 0 1.6em;
}

nav ol {
    text-indent: 0;
    list-style: none;
    margin: 0;
    padding: 0;
}

nav li {
    text-indent: 0;
    line-height: 2;
    font-size: 1em;
}

nav li a {
    text-decoration: none;
    /* 不写 color，理由同 body：目录链接必须跟随阅读器的前景色。 */
}

nav ol ol {
    margin: 0 0 0 1.4em;
}

nav ol ol li {
    font-size: 0.94em;
    line-height: 1.9;
}

/* 目录里的卷行：加粗 + 上方留白。缩进只说明"谁在谁下面"，加粗才让读者
   扫一眼就知道这几行是"卷"、那些是"章"。字号仍用 1em（比子项大），
   卷在目录里必须是视觉上更重的一层。 */
nav li.vol {
    font-weight: bold;
    margin-top: 0.7em;
}

nav li.vol > a {
    letter-spacing: 0.04em;
}

/* 浅色模式下的层次色。
 *
 * 为什么装进 prefers-color-scheme 里：深色模式下阅读器会注入自己的
 * 前景/背景色，写在常规规则里的 color 会盖过它 —— 诗词 p.verse 写死
 * #33302e 的那一版，在深色底上就是黑字配黑底，整块糊住。
 * 放进 light 媒体查询后：浅色下这些层次色照旧生效（章标题的青、注释
 * 的灰、诗词的暖褐），深色下整块规则被跳过，一切跟随阅读器。
 * 认不出这个媒体查询的老阅读器（Kindle 系）什么都不匹配，也走
 * "跟随阅读器"这条最安全的路。 */
@media (prefers-color-scheme: light) {
    body {
        color: #1a1a1a;
    }
    nav li a {
        color: #1a1a1a;
    }
    h1, h1.vol {
        color: #2f5d62;
    }
    h2.sub {
        color: #2f3d3f;
    }
    p.note, figcaption {
        color: #6b6b6b;
    }
    p.verse {
        color: #33302e;
    }
    hr.sep {
        border-top-color: #c9c5be;
    }
    .cover-title, .toc-title {
        color: #2f5d62;
    }
    .cover-author {
        color: #5a676b;
    }
}
"""


def _css_text(plan: FontPlan | None, with_back: bool = False) -> str:
    """产出最终的主样式表文本。

    没有字体规划、也没有封底页时，所有占位符都换成空串 —— 占位符本身不留痕，
    样式表与"从来没有封底这回事"时一字不差，老测试（case_css_contract 要求
    "没有 @font-face、没有 .ttf"）都不用改。with_back 只在真产出封底页时才为真：
    封底那 20 行规则留在没有封底的书里，等于给每个 EPUB 多塞一段死样式，
    不产封底的那一支也没法再跟自己的历史产物逐字节回归。

    有字体规划时：
    - 占位符换成内嵌 family 名（**放在回退链首位**）：包里那份子集字体才
      是"所有设备长得一样"的唯一保证，系统字体只能排在它后面兜底；
    - @font-face 块**插在样式表最前面**：CSS 不要求顺序，但把字体声明放头部
      是惯例，排查时一眼就能看到"这本书到底嵌了什么字体"。
    """
    body_family = ""
    head_family = ""
    extb_family = ""
    header = ""
    if plan is not None:
        for font in plan.fonts:
            if font.family == BODY_FAMILY:
                body_family = f'"{font.family}", '
            elif font.family == HEADING_FAMILY:
                head_family = f'"{font.family}", '
            elif font.family == EXTB_FAMILY:
                extb_family = f'"{font.family}", '
        # 补字 family 排在正文/标题后面：常用字先由思源（大陆字形）接住，
        # 它没有的扩展 B 区字才落到花园明朝。两条链都要挂，标题里也可能出现
        # 生僻字（人名、篇名）。它的 @font-face 带 unicode-range，不会抢常用字。
        body_family += extb_family
        head_family += extb_family
        header = plan.css
    text = (
        _CSS.replace("__FONTS_BODY__", body_family)
        .replace("__FONTS_HEAD__", head_family)
        .replace("__BACK_CSS__", _BACK_CSS if with_back else "")
    )
    if not header:
        return text
    return header + text


# ---------- zip 组装 ----------


def _zip_bytes(entries: list[tuple[str, bytes | Path, bool]]) -> bytes:
    """把 [(条目名, 内容, 是否压缩)] 组装成 zip 字节流。

    内容可以是字节，也可以是**还没读的文件路径**：插图按这一条边读边写，
    一张读完就被下一张顶掉，不必整本图片同时驻留（见 `_load_chapter_images`）。

    两个细节决定"两次打包字节完全相同"：
    - 每个 ZipInfo 显式给 date_time 与 create_system（默认会写当前时间与
      "是哪个系统打的包"，那是两个天然的噪声源）；
    - 条目顺序就是 entries 的顺序，zipfile 按写入顺序落盘，不做排序。
    分块喂给 deflate 与一次性喂进去产出同一条流（没有中间 flush），所以
    改读取时机不改一个字节。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data, compress in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
            info.create_system = 0      # 0 = MS-DOS，不写 unix 权限位
            info.external_attr = 0
            if isinstance(data, Path):
                zf.writestr(info, data.read_bytes())
            else:
                zf.writestr(info, data)
    return buf.getvalue()


# 各图片格式的文件头魔数。用前缀比对而不是完整解析：我们不修改图片，
# 只需要判断"它到底是不是一张这个格式的图"。
_IMAGE_MAGIC = {
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".png": b"\x89PNG\r\n\x1a\n",
    ".gif": b"GIF8",
}


def _load_picture(
    picture_path, warnings: list[str], *, label: str, stem: str, fallback: str
) -> tuple[str, str, bytes] | None:
    """读一张要打进包的整页图，返回 (包内路径, media-type, 字节)；不可用时 None + 警告。

    封底与封面走同一套校验（扩展名 → 读盘 → 非空 → 文件头魔数），只有
    三个地方不同：警告里的人话（label）、包内文件名（stem）、以及失败后
    退到哪儿（fallback，写进警告末尾）。

    为什么提取成共用函数而不是复制一份：两份一模一样的校验代码，改了一处
    忘了另一处，就会出现"封面认 png、封底不认"这种自相矛盾的行为。
    """
    if picture_path is None:
        return None
    raw = _text_of(picture_path)
    if not raw:
        return None
    path = Path(raw)
    ext = path.suffix.lower()
    if ext not in _IMAGE_TYPES:
        warnings.append(
            f"{label} {path.name} 的扩展名 {ext or '（无）'} 不认识（只支持 jpg/png/gif）：{fallback}"
        )
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        warnings.append(f"{label} {path} 读不出来（{exc}）：{fallback}")
        return None
    if not data:
        warnings.append(f"{label} {path} 是个空文件：{fallback}")
        return None
    # 看文件头而不是只看扩展名：扩展名是用户随口起的，字节才是事实。
    # 一个被改了名的 .txt 当封面，会变成阅读器首屏的一张裂图 —— 而这种
    # 问题在打包阶段完全看不出来（包是合法的，破的是阅读体验）。
    if not data.startswith(_IMAGE_MAGIC[ext]):
        warnings.append(
            f"{label} {path.name} 的内容不是真正的 {ext[1:]} 图（文件头对不上）：{fallback}"
        )
        return None
    ext_norm, media = _IMAGE_TYPES[ext]
    return f"Images/{stem}.{ext_norm}", media, data


def _load_cover(cover_path, warnings: list[str]) -> tuple[str, str, bytes] | None:
    """读封面图；不可用时返回 None 并报警告（退回纯文字封面页）。

    封面是锦上添花，缺了不该毁掉正文 —— 所以四种失败都只是警告 + 降级。
    """
    return _load_picture(
        cover_path,
        warnings,
        label="封面",
        stem="cover",
        fallback="改用纯文字封面",
    )


def _load_back_cover(back_path, warnings: list[str]) -> tuple[str, str, bytes] | None:
    """读封底图；不可用时返回 None 并报警告（整页封底就不产了）。

    封底的降级方向与封面相反：封面没图还能写书名作者，封底没图就只剩一张
    白纸 —— 那不是封底，是多出来的一次翻页。所以打包层拿到 None 就直接
    不写这一页，与"没传 back_cover"完全一样。
    """
    return _load_picture(
        back_path,
        warnings,
        label="封底",
        stem="back",
        fallback="这一版不生成封底页",
    )


def _load_chapter_images(
    book: Book, image_dir, warnings: list[str]
) -> dict[str, tuple[Path, str]]:
    """把章节树里引用到的插图**登记**下来，返回 {文件名: (磁盘路径, media-type)}。

    为什么按章节树引用而不是"把 image_dir 里所有图都打包"：
    目录里可能躺着上一次导入留下的旧图、用户手动放进去的图，全打包会让
    包体积凭空变大，还会出现"书里根本没引用"的幽灵条目。章节树才是事实。

    为什么读的是路径而不是字节：一本画册的图片总量可以是正文的几十倍，
    全部读进内存再拼 zip，峰值就是"图片总量 + 包体积 + getvalue 的整份复制"
    三层叠加。这里只验文件头（前 8 个字节，`_IMAGE_MAGIC` 最长 8 字节），
    真正的读推迟到 `_zip_bytes` 逐张进行 —— 一张读完即被下一张顶掉。

    为什么坏图/缺图只警告不抛：EPUB 导入出来的中间态可能被人手改过、
    图片目录可能被搬走；缺几张图仍应打出一本"其余部分完好"的书，
    而不是让整本打包失败。

    返回的 dict 保留插入顺序（= 书中出现顺序），entries 按它写包。
    """
    names: list[str] = []
    for paras in (
        book.front_matter,
        *(ch.paragraphs for ch in book.chapters),
        book.tail_matter,
    ):
        for p in paras:
            if p.kind != "image":
                continue
            name = _text_of(p.text)
            if name and name not in names:
                names.append(name)
    if not names:
        return {}
    if image_dir is None:
        _warn_once(
            warnings,
            f"章节树里有 {len(names)} 张插图，但没给图片目录（image_dir）：图片不会进包",
        )
        return {}
    base = Path(_text_of(image_dir))
    loaded: dict[str, tuple[Path, str]] = {}
    # 逐张收集失败原因，最后汇总成一条警告：一本带 47 张插图的书若图片目录
    # 整个搬走了，逐张报警会把报告刷成 47 行同一句话，真正的其他问题被埋掉。
    problems: list[str] = []
    for name in names:
        # 文件名合法性在 _render_figure 里还会再挡一次；这里先挡住路径穿越，
        # 因为下一步就是拿它拼真实路径去读文件。
        if "/" in name or "\\" in name or ".." in name:
            problems.append(f"{name}（文件名不合法）")
            continue
        ext = Path(name).suffix.lower()
        if ext not in _IMAGE_TYPES:
            problems.append(f"{name}（扩展名不认识，只支持 jpg/png/gif）")
            continue
        path = base / name
        try:
            head = _read_head(path, len(_IMAGE_MAGIC[ext]))
        except OSError:
            problems.append(f"{name}（找不到）")
            continue
        if not head or not head.startswith(_IMAGE_MAGIC[ext]):
            problems.append(f"{name}（内容不是真正的 {ext[1:]} 图）")
            continue
        _ext_norm, media = _IMAGE_TYPES[ext]
        loaded[name] = (path, media)
    if problems:
        if len(problems) <= 5:
            detail = "、".join(problems)
        else:
            detail = "、".join(problems[:5]) + f" 等 {len(problems)} 张"
        _warn_once(warnings, f"{len(problems)} 张插图没能读进来，已跳过：{detail}")
    return loaded


def _read_head(path: Path, size: int) -> bytes:
    """只读文件开头 size 个字节 —— 判图片格式用不着整张图。"""
    with path.open("rb") as fh:
        return fh.read(size)


# ---------- 字体收集 ----------


# 封面页里我们自己写死的文案。这些字**不在章节树里**，但会真的显示在
# 读者眼前 —— 不收集进来，封面上就会出现"标题是思源、这行是系统宋体"的字形跳变。
# 做法是直接渲染一份最简封面页，从生成好的 xhtml 里抠字符，而不是手抄一份字表：
# 手抄的字表会在下次改文案时悄悄过期。
_FONT_PROBE_TITLE = "书"
_FONT_PROBE_AUTHOR = "作者"


def _collect_font_chars(book: Book, nav_doc: str, ncx_doc: str, extra_docs) -> set[str]:
    """收集"这本书要显示的所有字符"。

    来源四类：
    1. 章节树（章标题 + 卷标题 + 每个段落的 text）；
    2. 目录两份（nav.xhtml / toc.ncx）—— 里面有书名的每个字；
    3. 我们自己的页面文案（封面页的书名/作者占位等）；
    4. 元数据（书名、作者、来源路径）。

    **不要**只收章节树：漏掉第 2、3 类的结果就是"正文是对的，但目录/封面
    出现方块或字体跳变"，而且这种缺陷只在翻到那一页时才看得见。

    第 4 类里的 source 现在其实不显示在任何页面上（书籍信息页 2026-10-28 已删），
    但留着不多花几个字，删它会让所有既有 EPUB 的字节都变 —— 不值得。
    """
    chars: set[str] = set()
    for chapter in book.chapters:
        if chapter.title:
            chars |= set(chapter.title)
        for para in chapter.paragraphs:
            if para.text:
                chars |= set(para.text)
    if book.title:
        chars |= set(book.title)
    if book.author:
        chars |= set(book.author)
    if book.source:
        chars |= set(book.source)
    chars |= set(nav_doc) | set(ncx_doc)
    for doc in extra_docs:
        chars |= set(doc)
    # \t\n 这类空白不占字形，混进去只会让子集多几条无用记录；_normalize_chars
    # 也会再剔一遍，这里先剔是为了让"字符数"这个诊断数字好看。
    return {ch for ch in chars if ch not in "\t\n\r"}


def _plan_embed_fonts(book: Book, fonts: bool, warnings: list[str]) -> FontPlan | None:
    """按需规划内嵌字体；拿不到就记一条 warning 并返回 None。

    **这个函数绝不抛异常**（除了自身的 bug）：字体是锦上添花，缺了不该让整本书
    打不出来。CLI 那边不做 try/except 兜底，降级责任全在这里。
    """
    if not fonts:
        # 显式关闭不是"失败"，不该报警告 —— 用户自己选了 --no-fonts，
        # 再提示"没找到字体"会让人以为出了什么事。
        return None
    try:
        nav_doc = _nav_doc("zh-CN", book)
        ncx_doc = _ncx_doc(book, book.title or "未命名", "probe")
        extra = [
            _cover_doc("zh-CN", _FONT_PROBE_TITLE, _FONT_PROBE_AUTHOR, None),
        ]
        chars = _collect_font_chars(book, nav_doc, ncx_doc, extra)
        plan = plan_fonts(chars)
    except Exception as exc:  # noqa: BLE001
        # 计划这一步本身出问题（字体探测异常、磁盘权限等）也只降级：
        # 字体有没有都比"包打不出来"轻得多。
        warnings.append(
            f"字体子集化没能完成（{exc}）：正文用系统字体渲染"
        )
        return None
    if plan is None:
        warnings.append(
            "没找到可内嵌的开源中文字体（思源/Noto），正文用系统字体渲染"
        )
        return None
    warnings.extend(plan.warnings)
    return plan


# ---------- 公开 API ----------


def build_epub(
    book: Book,
    *,
    title: str | None = None,
    author: str | None = None,
    language: str = "zh-CN",
    identifier: str | None = None,
    cover_path: str | Path | None = None,
    back_cover: str | Path | None = None,
    image_dir: str | Path | None = None,
    modified=None,
    generator: str = "huoban",
    fonts: bool = False,
) -> EpubResult:
    """把章节树打成 EPUB3 字节流。

    参数语义：
    - title / author 为空时取 book 的字段；再为空则书名用"未命名"、作者留空
      （作者为空时**不输出** dc:creator）；
    - identifier 为空时用 uuid5(NAMESPACE_URL, 来源路径 + 书名)，同样的输入
      两次生成同一串，方便测试与去重；
    - modified 为空取当前 UTC，格式 YYYY-MM-DDTHH:MM:SSZ（测试里必须传固定值，
      否则"确定性"无从谈起）；
    - cover_path 给了就把图放进包并生成图片封面页，否则生成纯文字封面页；
    - back_cover 给了就在正文之后加一页封底（整页一张图：纸纹 + 右下角一枚印），
      不给就照旧没有这一页。**默认 None，不传与传 None 产物完全一致**（封底这一维
      不影响老调用方；整包相对 0.1.2 已因 nav 移出 spine 变过，基线见 CHANGELOG）。
      与 cover_path 的区别：封面图坏了还能退回文字封面，封底图坏了就直接不产
      这一页（白纸不算封底）；
    - image_dir 是 EPUB 导入时落盘的图片目录（read_epub 的产物）。章节树里
      kind="image" 的段落会引用里面的文件，按文件名读进来打进包。不给时
      图片段一律跳过并警告 —— 没有图片目录就没有图片，不能凭空造。默认 None
      （与 fonts 同一个理由：老调用方的产物不该因为升级而变）。
    - generator 写进 <meta name="generator">；
    - fonts=True 时把正文/标题的 OFL 中文字体裁成子集打进包，并在 CSS 头部
      补 @font-face。默认 **False**，两条理由：
      ① 库层不突变既有产物 —— 老调用方拿到的东西不该因为升级而变大 1 MB；
      ② tests/test_epub.py 的 case_css_contract 硬断言样式表里没有
         @font-face、没有 .ttf，默认关才不会弄坏既有契约。
      需要内嵌的调用方（CLI）显式传 fonts=True。
      **拿不到字体不算错误**：没装 fontTools、本机没有思源/Noto、字体文件损坏，
      一律降级成"系统字体回退链 + 一条 warning"，包照常产出。

    纯函数：不改 book、不写盘、不 print。同样的入参调两次，data 逐字节相同。
    """
    warnings: list[str] = []

    # 元数据全部过 _meta_text（= _sanitize + 去空白）：书名/作者来自 TXT 首行，
    # 里面可能有 \x00 之类的非法字符。正文那边早就清洗了，元数据漏一次就是
    # content.opf 不良构 → 阅读器判"文件已损坏"。这条不是理论风险：
    # encoding.py 用 errors="replace" 解码，NUL 不是解码错误，会原样留下。
    book_title = _meta_text(title) or _meta_text(book.title) or "未命名"
    book_author = _meta_text(author) or _meta_text(book.author)
    lang = _meta_text(language) or "zh-CN"
    gen = _meta_text(generator) or "huoban"
    stamp = _format_modified(modified)
    book_id = _resolve_identifier(identifier, book, book_title)

    # 「没有章节」的具体处理交给 _render_chapters：它会说明前言后记去了哪里。
    # 这里不再重复报警告 —— 同一件事在报告里出现两遍，会让人以为有两个问题。
    cover = _load_cover(cover_path, warnings)
    back = _load_back_cover(back_cover, warnings)
    # 章节内插图要在渲染正文之前读进来：_render_paragraphs 需要知道
    # "哪些图片文件真的存在"，否则会写出一堆指向空文件的 <img>（裂图）。
    chapter_images = _load_chapter_images(book, image_dir, warnings)
    images_ok = set(chapter_images)
    chapter_docs, paragraph_count = _render_chapters(
        book, lang, book_title, warnings, images_ok=images_ok
    )

    cover_name, cover_media = (cover[0], cover[1]) if cover else (None, None)
    # cover.xhtml 在 Text/ 下，图片在 Images/ 下 —— 相对路径必须往上一级。
    cover_href = f"../{cover_name}" if cover_name else None
    # 封底同构：有图才有页，包内名 Images/back.{ext}（不能与 cover.* 同名）。
    back_name, back_media = (back[0], back[1]) if back else (None, None)
    back_href = f"../{back_name}" if back_name else None

    # 字体规划要在拼 content.opf 之前完成（manifest 里要写字体条目）。
    # 它是整个流程里最慢的一步（两套字体实测约 6 秒），但顺序上绕不开：
    # 好在它内部自己吞掉所有失败并降级，不会把异常抛到这里。
    font_plan = _plan_embed_fonts(book, fonts, warnings)
    # 包内路径 → (manifest 里的 id, href, media-type)。
    font_items = [
        (
            # manifest id 直接由包内文件名推出来（serif.ttf → font-serif），
            # 这样 id 与 href 不可能对不上。
            f"font-{Path(f.filename).stem}",
            f.filename[len("OEBPS/"):],
            f.media_type,
        )
        for f in (font_plan.fonts if font_plan else [])
    ]

    # 章节内插图的 manifest 条目：id 由文件名推出来（img003.jpg → img-img003），
    # 保证 id 与 href 不可能对不上。id 里只留 NCName 合法字符。
    image_items = [
        (
            "img-" + re.sub(r"[^A-Za-z0-9_.-]", "-", Path(name).stem),
            f"Images/{name}",
            media,
        )
        for name, (_src, media) in chapter_images.items()
    ]

    nav_doc = _nav_doc(lang, book)
    ncx_doc = _ncx_doc(book, book_title, book_id)
    # 骨架三件套（opf/nav/ncx）坏掉 = 整本书打不开，所以这三个用严格模式：
    # 自检不过直接抛错，不许"打个包出来让用户自己发现"。
    _selfcheck("nav.xhtml", nav_doc, warnings, strict=True)
    _selfcheck("toc.ncx", ncx_doc, warnings, strict=True)

    opf = _opf_doc(
        book_id=book_id,
        title=book_title,
        author=book_author,
        lang=lang,
        stamp=stamp,
        generator=gen,
        cover_name=cover_name,
        cover_media=cover_media,
        back_name=back_name,
        back_media=back_media,
        chapter_docs=chapter_docs,
        font_items=font_items,
        image_items=image_items,
    )
    _selfcheck("content.opf", opf, warnings, strict=True)

    cover_doc = _cover_doc(lang, book_title, book_author, cover_href)
    _selfcheck("cover.xhtml", cover_doc, warnings)
    # 封底页只有有图时才生成（见 _load_back_cover 的降级方向）。
    back_doc = _back_doc(lang, book_title, back_href) if back_href else None
    if back_doc is not None:
        _selfcheck("back.xhtml", back_doc, warnings)

    # 条目顺序就是契约里写死的包结构：**mimetype 必须第一个且必须 STORED**，
    # 这是 OCF 规范里唯一的硬性布局要求，其余条目按契约顺序压缩落盘。
    entries: list[tuple[str, bytes, bool]] = [
        ("mimetype", EPUB_MIMETYPE, False),
        ("META-INF/container.xml", _container_doc().encode("utf-8"), True),
        (CONTAINER_PATH, opf.encode("utf-8"), True),
        ("OEBPS/nav.xhtml", nav_doc.encode("utf-8"), True),
        ("OEBPS/toc.ncx", ncx_doc.encode("utf-8"), True),
        ("OEBPS/Styles/main.css",
         _css_text(font_plan, with_back=back_doc is not None).encode("utf-8"), True),
    ]
    # 契约规定：字体条目插在 main.css 之后、封面图之前，且**不压缩**（STORED）。
    # 字体是已压缩的二进制表，deflate 只多花 CPU 换 3% 体积；而 STORED 让包内
    # 偏移可直接定位，某些阅读器读字体走这条路。
    if font_plan is not None:
        for font in font_plan.fonts:
            entries.append((font.filename, font.data, False))
    if cover:
        # 图片一律 deflate，不再固定 STORED：jpg/gif 确实压不动，但未压缩的
        # PNG（截图改的封面很常见）能压掉一大半。deflate 对已压缩数据只会多
        # 几十字节，比"8MB 封面原样进包"划算得多。
        entries.append((f"OEBPS/{cover[0]}", cover[2], True))
    if back:
        # 封底图紧跟封面图（契约顺序：封面图 → 封底图 → 章节内图片）。
        entries.append((f"OEBPS/{back[0]}", back[2], True))
    # 章节内插图紧跟在封面图之后（契约：封面图 deflate → 章节内图片 deflate →
    # Text/cover.xhtml）。顺序取 chapter_images 的插入顺序 = 书中出现顺序。
    # 这里放的是**路径**而不是字节：真正的读发生在 _zip_bytes 里，一张写完就放手。
    for name, (src, _media) in chapter_images.items():
        entries.append((f"OEBPS/Images/{name}", src, True))
    entries.append(("OEBPS/Text/cover.xhtml", cover_doc.encode("utf-8"), True))
    for path, _title, doc in chapter_docs:
        entries.append((f"OEBPS/{path}", doc.encode("utf-8"), True))
    # 封底页放在所有章节之后：与 spine 末尾一致（翻页顺序 封面 → 正文 → 封底）。
    # 包内条目顺序与 spine 顺序不必相同，但对齐之后"照着 spine 读一遍包里的
    # 文件"这种人工核对不用来回跳。
    if back_doc is not None:
        entries.append(("OEBPS/Text/back.xhtml", back_doc.encode("utf-8"), True))

    data = _zip_bytes(entries)
    return EpubResult(
        data=data,
        files=[name for name, _data, _compress in entries],
        chapters=len(chapter_docs),
        paragraphs=paragraph_count,
        warnings=warnings,
    )


def write_epub(book: Book, path: str | Path, **kwargs) -> EpubResult:
    """打包并落盘，返回与 build_epub 完全相同的 EpubResult。

    为什么不把 path 放进 EpubResult：结果对象描述的是"这个包长什么样"，
    而路径是调用方的上下文；同一个 EpubResult 可能被写到多处，
    塞一个路径进去反而要事先解释"这是哪个路径"。

    父目录不存在就顺手建（调用方常写 outputs/<书名>/book.epub 这种新目录）；
    写入内容与 build_epub 的 data 是同一份字节，不会因为落盘再变一次。
    """
    result = build_epub(book, **kwargs)
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result.data)
    return result
