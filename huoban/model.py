"""章节树 JSON（中间态）。

为什么需要这一层：清洗只负责"把脏 TXT 变成带段落的章节树"，
打包只负责"把章节树变成 EPUB / AZW3 / TXT"。两边只通过这个 schema 对话，
互不知道对方的规则细节。好处是：加一种输出格式不用回头碰清洗，
改一条清洗规则也不用碰渲染。没有这一层，两种输出就会各自长出一套清洗逻辑，
迟早不一致。

为什么用 dataclass 而不是裸 dict：
- 字段有类型，clean.py 里拼错字段名会当场报错，而不是被 JSON 静默吞掉；
- 序列化只集中在 to_dict / from_dict 两个函数，schema 迁移（version 字段）
  只需要改这两处，不用全项目搜索 json.dumps；
- 段落类型 kind 是渲染层唯一的排版依据（要不要缩进、要不要落空行、
  要不要包 <p>），裸 dict 里它很容易被写成随便什么字符串。

为什么不复用 diagnose.py 的 CHAPTER_PATTERNS 自己再写一套编号识别：
那会导致"体检说这是第 12 章、清洗出来是 11 章"这种两份实现打架的情况。
本模块的序号解析只做一件事 —— 从已经判定为标题的行里把数字抽出来，
交给 diagnose.cn_to_int() 转换，判"像不像标题"一律用 diagnose.match_chapter()。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .diagnose import cn_to_int, match_chapter, strip_frame

# schema 版本。字段增删、语义变化（例如 level 的含义改了）时必须 +1，
# 让 from_dict 能拒绝按旧语义读新文件，而不是读出一本错的书。
#
# v1 → v2（EPUB 导入 G6）：Paragraph 新增 caption / width / height 三个字段，
# 只在 kind="image" 的段落上出现。这是**纯增量**：v1 的章节树按 v2 读入后
# 一个字段都不会变（from_dict 接受 1 和 2 两个主版本），to_dict 也只对 image
# 段写新字段 —— 旧 v1 文件重新序列化后除 version 外逐字节不变（见 docs/gui-design.md 的「EPUB 导入」一节里的验收要求）。
SCHEMA_VERSION = "2"

# 段落类型。渲染层按这个决定排版，清洗层按这个标注，两边共用同一份取值。
# verse = 诗词/韵文（词曰、诗云、右调·XXX 之类）：本质是"引文"，
# 排版上要跟正文拉开距离 —— 居中、不缩进、换楷体、字号略小。
# image = 插图（EPUB 导入专用）：text 是**图片文件名**（不是正文），
# caption 是图说，width/height 是像素尺寸。渲染层把它排成 figure。
PARAGRAPH_KINDS = ("body", "heading", "blank", "note", "verse", "sep", "image")

# 图片文件名允许的扩展名（与 epub.py 的 _IMAGE_TYPES 对齐）。
# image 段的 text 必须满足：非空、纯文件名（不含 / \ 与 ..）、扩展名在此表内 ——
# 渲染层会把它直接拼成 ../Images/<name> 写进 xhtml，脏名字会变成路径穿越。
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif")

# anchor / refs 里的 id 会被原样拼进 xhtml 的 id="..." 与 href="#..."：
# 只要带空白、引号、尖括号或 & 就能跳出属性、注入标签。用"白名单"而不是
# "黑名单"：合法 id 本来就只是字母数字加 - _ . :。
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass
class Paragraph:
    """一个段落。

    为什么不直接把段落存成字符串：标题、空行、注释在渲染时待遇完全不同
    （标题要进目录、空行是场景分隔、注释可能要斜体或跳过），
    这些信息在"合并碎行"那一步就丢掉了，事后靠内容猜不回来。
    """

    text: str
    kind: str = "body"  # "body" | "heading" | "blank" | "note" | "verse" | "sep" | "image"
    # 以下三个字段只在 kind == "image" 时有意义（schema v2，EPUB 导入专用）：
    # - caption：图说（原书没有就是空串；不要自己编）；
    # - width / height：图片像素尺寸，读不出时都是 0（渲染层按方图兜底）。
    # 旧代码（清洗 TXT 那条线）永远不碰这三个字段，默认值让它们保持原样。
    caption: str = ""
    width: int = 0
    height: int = 0
    # 以下两个字段同样只在 EPUB 导入时出现（schema v2 的增量，纯可选）：
    # - anchor：本段在原书里的 HTML id。注释段靠它被正文里的引用指到；
    # - refs：本段正文里的**行内引用**，每条 {"at": 字符偏移, "note": 目标 id,
    #   "label": 显示文字（如 "[1]"）}。偏移是"插在第 at 个字符之前"。
    #   没有它，正文里的 [1] 只是一串死文字，弹注在阅读器里点不开。
    # 清洗 TXT 那条线永远不写这两个字段，to_dict 也只在非空时才写。
    anchor: str = ""
    refs: list[dict] = field(default_factory=list)


@dataclass
class Chapter:
    """一个章节节点。

    start_line 记录的是**清洗前**原文里的行号（1 基）。它的价值不是给人看，
    而是清洗报告和人工复核时能"从章节树一步跳回原文第几行" ——
    没有它，出了错只能整本重新肉眼比对。
    """

    title: str
    number: int | None = None  # 解析出的章节序号（"第十二章" → 12），解析不出就是 None
    level: int = 1  # 1=章级，2=卷/部/集级
    start_line: int = 0  # 清洗前的行号（1 基）
    paragraphs: list[Paragraph] = field(default_factory=list)


@dataclass
class Book:
    """整本书的章节树。"""

    title: str = ""
    author: str = ""
    encoding: str = ""
    source: str = ""
    # 第一个章节之前的内容：序章以外的前言、版权页、广告残留、目录块。
    # 单独存而不是硬塞进第一章，是因为它的排版和归属都不确定，
    # 留给渲染层决定"丢弃 / 当扉页 / 当前言"。
    front_matter: list[Paragraph] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    # 最后一个章节之后的内容：后记、全书完、作者的话、广告尾巴。
    tail_matter: list[Paragraph] = field(default_factory=list)


# ---------- 章节标题解析（复用 diagnose 的判定） ----------

# "第<数字><单位>"。数字部分直接交给 diagnose.cn_to_int()，这里只负责定位。
# 与 diagnose._NUM_TITLE 的区别：这里带上"单位"捕获组，因为单位决定层级
# （卷/部/集 是 level 2，章/回/节 是 level 1）。
_HEADING_NUM = re.compile(
    r"^\s*第\s*([0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,10})\s*([卷部集篇章回节幕话話])"
)
_CHAPTER_EN = re.compile(r"^\s*[Cc][Hh][Aa][Pp][Tt][Ee][Rr]\s*([0-9]{1,4})")
# 「（一）」「（十二）」式分节的序号。括号里的数字同样是"第几个"，
# 所以照样交给 cn_to_int() 转，不另写一套中文数字转换。
_BRACKET_SECTION = re.compile(r"^\s*[（(]\s*([0-9０-９〇零一二三四五六七八九十百]{1,4})\s*[）)]\s*$")
# 「1、掳君（1）」式分节的序号，在顿号前面。
_NUM_SECTION = re.compile(r"^\s*((?:[0-9０-９]{1,4}|[一二三四五六七八九十百]{1,4}))\s*[、.．]")

# 卷/部/集/篇 比章高一级。按契约：level 1 = 章级，level 2 = 卷/部/集级。
_VOLUME_UNITS = "卷部集篇"

# diagnose.match_chapter() 返回的家族名 → 默认层级。
# 没有数字的家族（序章、楔子、番外、目录）一律算章级：
# 它们确实是"章这一层"的节点，只是没有序号。
_LEVEL_BY_FAMILY = {
    "第N卷部类": 2,
    "第N章类": 1,
    "Chapter N": 1,
    "卷首特殊章": 1,
    "括号分节": 1,
    "数字顿号分节": 1,
    "Markdown 标题": 1,
    "纯数字行": 1,
    "分隔线": 1,
}


class Heading(NamedTuple):
    """标题解析结果：序号、层级、命中的模式家族。"""

    number: int | None
    level: int
    family: str


def parse_heading(line: str) -> Heading | None:
    """认出"这一行是不是章节标题"，并给出序号与层级；不是标题返回 None。

    为什么复用 diagnose.match_chapter() 判"是不是标题"：体检报告和清洗结果
    必须是同一套判据。两边各写一套正则，迟早出现"报告说有 30 章、清洗出 29 章"。

    为什么返回 None 而不是"猜一个 level 1"：调用方（clean.py）靠 None 决定
    "这行是正文，别当标题切章"。瞎猜会把正文行切成章节，破坏比漏判严重得多，
    所以宁可返回"没判定"。

    为什么先剥 Markdown 井号再找序号："## 第十二章" 命中 diagnose 的
    "Markdown 标题"家族，但序号在井号后面。不剥就会丢掉序号，
    后面的重复/连续性检查跟着全瞎。
    """
    # 下载站常给标题套一圈对称装饰边框（「========== 第001章 ==========」）。
    # 体检的 match_chapter 已经剥过，这里必须同样剥一次再抽序号，否则
    # 家族认得出、序号却全丢（Heading.number 恒为 None），目录编号检查跟着瞎。
    # 纯装饰分隔线 strip_frame 原样保留，仍归「分隔线」家族由 clean.py 挡掉。
    line = strip_frame(line)
    family = match_chapter(line)
    if family is None:
        return None

    level = _LEVEL_BY_FAMILY.get(family, 1)
    number: int | None = None

    m = _HEADING_NUM.match(line)
    if m is None:
        m = _HEADING_NUM.match(line.lstrip("# \t"))
    if m is not None:
        unit = m.group(2)
        level = 2 if unit in _VOLUME_UNITS else 1
        # cn_to_int 是 diagnose 的实现，这里绝不自带一套转换：
        # "一百二十三" 的进位规则只允许存在一份。
        number = cn_to_int(m.group(1))
    else:
        m_en = _CHAPTER_EN.match(line)
        if m_en is not None:
            try:
                number = int(m_en.group(1))
            except ValueError:
                number = None
        elif family == "纯数字行":
            # "123" 单独一行也是章节标记的一种形态，序号就在行里。
            # 仍然交给 cn_to_int()（它同时认全角数字），不另写转换。
            number = cn_to_int(line.strip())
        elif family == "数字顿号分节":
            # 顿号前面的阿拉伯数字就是这个"节"的序号（《书M》1..66）。
            m_num = _NUM_SECTION.match(line)
            if m_num is not None:
                number = cn_to_int(m_num.group(1))
        elif family == "括号分节":
            # 括号里的数字就是这个"节"的序号（《书F》1..220）。
            # 取不到就留 None —— 序号只影响报告里的连续性检查，不影响切章。
            m_br = _BRACKET_SECTION.match(line)
            if m_br is not None:
                number = cn_to_int(m_br.group(1))
    return Heading(number, level, family)


def chapter_from_title(title: str, start_line: int = 0) -> Chapter:
    """用一个标题行造出 Chapter，序号与层级自动解析。

    给 clean.py 省掉"先 parse_heading 再手填两个字段"的重复代码，
    也保证所有章节的 number/level 都出自同一个解析器。
    """
    heading = parse_heading(title)
    if heading is None:
        # 调用方明确说"这是一章"就照办，只是序号不知道、层级按章级处理。
        return Chapter(title=title, number=None, level=1, start_line=start_line)
    return Chapter(title=title, number=heading.number, level=heading.level, start_line=start_line)


# ---------- 序列化 ----------


def _paragraph_to_dict(p: Paragraph) -> dict:
    d = {"text": p.text, "kind": p.kind}
    # 三个新字段只在 image 段上写：普通段落多出 "caption": "" 会让
    # v1 的旧文件重新序列化后字节发生变化（docs/gui-design.md 的「EPUB 导入」一节验收要求：除 version 外不变）。
    if p.kind == "image":
        d["caption"] = p.caption
        d["width"] = p.width
        d["height"] = p.height
    # anchor / refs 同理：只在真有值时写。清洗 TXT 那条线产出的段落
    # 两个字段都空，序列化结果与之前逐字节相同。
    if p.anchor:
        d["anchor"] = p.anchor
    if p.refs:
        d["refs"] = [
            {"at": int(r.get("at", 0)), "note": str(r.get("note", "")), "label": str(r.get("label", ""))}
            for r in p.refs
        ]
    return d


def _paragraph_from_dict(raw) -> Paragraph:
    # 允许直接写字符串（{"paragraphs": ["正文一", "正文二"]}）：
    # 手写 JSON 和测试里造样本会方便很多，代价只是多一个 isinstance 判断。
    if isinstance(raw, str):
        return Paragraph(text=raw)
    if not isinstance(raw, dict):
        return Paragraph(text=str(raw))
    text = raw.get("text")
    kind = raw.get("kind")
    caption = raw.get("caption")
    anchor = raw.get("anchor")
    refs: list[dict] = []
    for r in _as_list(raw.get("refs")):
        if not isinstance(r, dict):
            continue
        note = r.get("note")
        if not note:
            continue
        refs.append(
            {
                "at": _as_int(r.get("at"), 0),
                "note": str(note),
                "label": "" if r.get("label") is None else str(r.get("label")),
            }
        )
    return Paragraph(
        text="" if text is None else str(text),
        kind="body" if kind is None else str(kind),
        caption="" if caption is None else str(caption),
        width=_as_int(raw.get("width"), 0),
        height=_as_int(raw.get("height"), 0),
        anchor="" if anchor is None else str(anchor),
        refs=refs,
    )


def _chapter_to_dict(ch: Chapter) -> dict:
    return {
        "title": ch.title,
        "number": ch.number,
        "level": ch.level,
        "start_line": ch.start_line,
        "paragraphs": [_paragraph_to_dict(p) for p in ch.paragraphs],
    }


def _as_list(value) -> list:
    """把 JSON 里的值当作列表用。

    为什么需要它：手改 JSON 时很容易把 "chapters": [] 写成 "chapters": {} 或
    手滑留成字符串。直接 for 遍历 dict 会遍历键、遍历字符串会遍历字符，
    结果是"读进来一堆莫名其妙的空章节"，比报错更难查。这里一律退化成空列表，
    让 validate() 能报"没有任何章节"，问题一眼可见。
    """
    return value if isinstance(value, list) else []


def _as_int(value, default: int = 0) -> int:
    """把 JSON 里的值转成 int；转不了就用默认值。

    脏 JSON 不该让整本书读不进来 —— 一个 level 写错不该毁掉 500 章的内容。
    bool 要单独挡掉，因为 Python 里 True 是 int 的子类，会悄悄变成 1。
    """
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value) -> int | None:
    """同 _as_int，但保留 None 的语义（"没解析出序号"和"序号是 0"是两回事）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _chapter_from_dict(raw) -> Chapter:
    if not isinstance(raw, dict):
        return Chapter(title=str(raw))
    title = raw.get("title")
    return Chapter(
        title="" if title is None else str(title),
        number=_as_optional_int(raw.get("number")),
        level=_as_int(raw.get("level"), 1),
        start_line=_as_int(raw.get("start_line"), 0),
        paragraphs=[_paragraph_from_dict(x) for x in _as_list(raw.get("paragraphs"))],
    )


def to_dict(book: Book) -> dict:
    """把章节树转成可以直接 json.dumps 的普通字典。

    为什么返回 dict 而不是 JSON 字符串：缩进几位、中文要不要转义、
    要不要写文件，各家调用方的要求不一样。序列化策略留给调用方，
    本函数只保证"结构稳定、字段齐全、每次键的顺序一致"。
    返回的是新构造的对象，调用方随便改都污染不到原来那本书。
    """
    return {
        "version": SCHEMA_VERSION,
        "title": book.title,
        "author": book.author,
        "encoding": book.encoding,
        "source": book.source,
        "front_matter": [_paragraph_to_dict(p) for p in book.front_matter],
        "chapters": [_chapter_to_dict(ch) for ch in book.chapters],
        "tail_matter": [_paragraph_to_dict(p) for p in book.tail_matter],
    }


def from_dict(d: dict) -> Book:
    """从 to_dict() 的产物还原章节树。

    为什么缺字段宽容、版本号严格：
    缺字段通常意味着旧文件或手写 JSON，用默认值补齐还能用；
    但主版本号对不上意味着"字段含义可能已经变了"（比如 level 的编号方式改了），
    照着旧语义读会读出一本结构错位的书，还不如当场报错让人去看。

    为什么段落允许是纯字符串：手写样本和早期草稿里常见这种写法，
    兼容它不会带来歧义。
    """
    if not isinstance(d, dict):
        raise TypeError(f"章节树应该是 dict，实际是 {type(d).__name__}")

    version = str(d.get("version", SCHEMA_VERSION))
    major = version.split(".")[0].strip()
    # v2 相对 v1 只是 Paragraph 多了三个默认字段（image 段专用），旧文件按
    # v2 读入无损 —— 所以两个主版本都接受；其余版本仍然拒绝，宁可当场报错，
    # 也不按不认识的语义读出一本结构错位的书。
    if major not in ("1", "2"):
        raise ValueError(
            f"不认识的章节树版本 {version!r}（当前支持 1 和 {SCHEMA_VERSION}）："
            "字段含义可能已变，拒绝按旧语义读出错误的章节结构"
        )

    def _text(key: str) -> str:
        v = d.get(key)
        return "" if v is None else str(v)

    return Book(
        title=_text("title"),
        author=_text("author"),
        encoding=_text("encoding"),
        source=_text("source"),
        front_matter=[_paragraph_from_dict(x) for x in _as_list(d.get("front_matter"))],
        chapters=[_chapter_from_dict(x) for x in _as_list(d.get("chapters"))],
        tail_matter=[_paragraph_from_dict(x) for x in _as_list(d.get("tail_matter"))],
    )


def save(book: Book, path: Path) -> None:
    """把章节树写成 JSON 文件（UTF-8）。

    为什么固定 ensure_ascii=False：这份文件是给人排查用的，
    中文全变成 \\uXXXX 就失去了"能直接打开看"的价值；
    为什么固定 indent=2：diff 时一行一个字段，改了什么一眼能看出来。
    父目录不存在就顺手建：调用方写的常是 outputs/<书名>/book.json 这种新目录。
    """
    path = Path(path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_dict(book), ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def load(path: Path) -> Book:
    """读回 save() 写出的 JSON 文件。

    只接受 UTF-8：save 只会写 UTF-8，读到别的东西说明文件被换过，
    该报错而不是猜编码（猜编码是 encoding.py 的职责，不是这里的）。
    """
    return from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------- 校验 ----------


def _iter_groups(book: Book) -> list[tuple[str, list[tuple[int, Chapter]]]]:
    """把章节按"卷"分组，供序号检查使用。

    为什么必须分组：每一卷的章号都从 1 重新开始。如果把全书章节当一条线
    比较递增，卷二的第一章必然被报成"从 300 倒退到 1"，全是假警报。
    分组规则：
    - 所有卷标题归同一组，卷与卷之间比较序号（一卷二卷三卷必须递增）；
    - 章归到"当前所在的卷"名下，每卷的章号从 1 重新开始；
    - 第一个卷之前的章归到"全书（无卷）"；
    - 没编号的章（序章、番外）留在组里但跳过比较 —— 没判定就别瞎猜。

    返回 [(组名, [(章节下标, 章节), ...]), ...]，组名只用于给人看的提示。
    """
    # 组 key 用 (层级, 卷序号) 而不是卷号：两卷都写成"第1卷"（常见于盗版合并本）
    # 时，按卷号做 key 会把两卷的章混成一组，然后互相误报序号重复。
    groups: dict[tuple[int, int], list[tuple[int, Chapter]]] = {}
    group_names: dict[tuple[int, int], str] = {}
    # 卷序号用递增计数器，不用章节序号：卷标题没编号（"正文卷"）时也要能分开。
    volume_seq = 0
    for idx, ch in enumerate(book.chapters):
        if ch.level >= 2:
            volume_seq += 1
            # 卷标题全部并进同一个组（key 的第二个分量固定 0），
            # 这样"第1卷 → 第2卷 → 第3卷"之间才能比较递增。
            key: tuple[int, int] = (2, 0)
            group_names[key] = "卷/部/集标题"
        else:
            key = (1, volume_seq)
            if key not in group_names:
                group_names[key] = (
                    "全书（第一个卷之前）" if volume_seq == 0 else f"第 {volume_seq} 个卷"
                )
        groups.setdefault(key, []).append((idx, ch))
    return [(group_names[key], items) for key, items in groups.items()]


def chapter_groups(book: Book) -> list[tuple[str, list[tuple[int, Chapter]]]]:
    """公开入口：把章节按卷分组，供序号比较复用（详见 _iter_groups）。

    为什么要有公开的这一层：复核页要按组比章号，不该自己再写一条
    "全书一条线比较"的循环 —— 每卷章号都从 1 重新开始，全局比较必然
    把卷二的第一章全报成"编号倒退"。分组规则只在这里维护一份。
    """
    return _iter_groups(book)


# 告警分三级（决定32）。三级不是三种颜色，是三种"该谁管"：
#   loss   东西丢了 —— 得回去看原文件，光看成品看不出所以然；
#   order  结构错了 —— 目录会跳错位置、调整没生效，还能改；
#   layout 排版瑕疵 —— 书照读，留意即可。
# 级别由产生告警的这一层判定，界面不猜；界面只负责把同级的合成一行。
LEVEL_LOSS = "loss"
LEVEL_ORDER = "order"
LEVEL_LAYOUT = "layout"


def validate_items(book: Book) -> list[dict]:
    """检查章节树是否健康，返回结构化的问题清单；空列表代表没发现问题。

    为什么返回 dict 而不是字符串：同一类问题会一条一章地出现（十三个空章就是
    十三行），界面要按 code 合并计数才压得到三行以内（决定31）。text 是给人读的
    整句，一句一章、带可核对的位置；code 是合并用的类别；level 是严重度（见上面
    三个 LEVEL_* 常量）。排序、计数、措辞归并都在界面那一侧做 —— 这一层只保证
    "同类同 code"。

    为什么返回清单而不是抛异常：
    这些问题大多不致命（脏书的中间态本来就带病），先渲染出来看一眼
    往往比"整个流程挂掉"更有用。清单要能贴进清洗报告给人核对，
    所以每条都带"第几个章节、什么标题、原文第几行"这类可核对的信息。

    为什么序号检查要分组（见 _iter_groups）：每卷章号重新开始，
    全局比较必然把卷二的第一章误报成倒退。

    检查项：
    1. 编码字段为空（丢失原始编码 = 以后无法复现解码）；
    2. 一个章节都没有（打包后没有目录）；
    3. 章节标题为空（渲染出空白目录项）；
    4. level 不是 1/2（契约外取值，渲染层无法排版）；
    5. 段落全空（多半是清洗把正文丢了）。**level=2 的卷/部/集标题豁免这一条**：
       它是容器，正文在下面的章里，空段落是正常形态；
    6. 段落 kind 不在 PARAGRAPH_KINDS 之内（渲染层没法定排版）；
    7. 序号重复、序号非递增（同组内比较，缺号不算问题 —— 没判定就别瞎猜）；
    8. 图片段（kind="image"）的 text 不是安全的图片文件名（空 / 带路径分隔符 /
       带 .. / 扩展名不在白名单）—— 渲染层会把它拼进 ../Images/<name>，
       脏名字等于路径穿越；
    9. 非图片段带了 caption（图说只属于 image 段，渲染层会忽略它 —— 不报就悄悄丢了）。
    """
    issues: list[dict] = []

    def _add(code: str, level: str, text: str) -> None:
        issues.append({"code": code, "level": level, "text": text})

    def _check_paragraph(p: Paragraph, label: str) -> None:
        """单段的契约检查（图片文件名白名单 / caption 归属）。"""
        if p.kind == "image":
            name = p.text.strip()
            if not name:
                _add("image_name", LEVEL_LAYOUT, f"{label}有图片段的文件名为空：渲染层拼不出图片路径")
            elif "/" in name or "\\" in name or ".." in name:
                _add("image_name", LEVEL_LAYOUT,
                    f"{label}图片文件名 {name!r} 含路径分隔符或 ..（只许纯文件名）"
                )
            elif Path(name).suffix.lower() not in IMAGE_EXTS:
                _add("image_name", LEVEL_LAYOUT,
                    f"{label}图片文件名 {name!r} 的扩展名不在白名单"
                    f"（只允许 {'/'.join(IMAGE_EXTS)}）"
                )
        elif p.caption:
            # caption 是图说，只属于 image 段；出现在正文段上说明上游标错了，
            # 渲染层会直接忽略它 —— 不报的话这段文字就悄悄丢了。
            _add("caption_on_text", LEVEL_LAYOUT, f"{label}非图片段落带了 caption（图说只属于 image 段）")
        # anchor / refs 会直接拼进 xhtml 的 id 与 href：脏值等于注入。
        if p.anchor and not _SAFE_ID.match(p.anchor):
            _add("bad_anchor", LEVEL_LAYOUT, f"{label}段落 anchor {p.anchor!r} 不是安全的 HTML id（含空白或引号等）")
        for ref in p.refs:
            note = str(ref.get("note") or "")
            if not note:
                _add("bad_ref", LEVEL_LAYOUT, f"{label}行内引用的目标为空：渲染层拼不出链接")
            elif not _SAFE_ID.match(note):
                _add("bad_ref", LEVEL_LAYOUT, f"{label}行内引用目标 {note!r} 不是安全的 HTML id")
            at = ref.get("at")
            if not isinstance(at, int) or at < 0:
                _add("bad_ref", LEVEL_LAYOUT, f"{label}行内引用的位置 {at!r} 不是非负整数")
            elif at > len(p.text):
                _add("bad_ref", LEVEL_LAYOUT, f"{label}行内引用的位置 {at} 超出本段长度 {len(p.text)}")

    if not book.encoding.strip():
        _add("encoding_empty", LEVEL_LAYOUT, "encoding 字段为空：清洗时没记录原始编码，日后无法复核解码是否正确")

    if not book.chapters:
        _add("no_chapters", LEVEL_LOSS, "没有任何章节：全书内容都会落在 front_matter，打包后没有目录可跳转")

    for idx, ch in enumerate(book.chapters):
        label = f"第 {idx + 1} 个章节"
        title = ch.title.strip()
        if not title:
            _add("empty_title", LEVEL_ORDER,
                f"{label}标题为空（start_line={ch.start_line}）：渲染后目录里会出现一个空白条目"
            )
        if ch.level not in (1, 2):
            _add("bad_level", LEVEL_ORDER,
                f"{label}「{ch.title}」的 level={ch.level} 不在 1/2 之内（1=章级，2=卷/部/集级）"
            )
        # 卷/部/集（level=2）是**容器标题**，正文都在它下面的章里，本身没有
        # 段落是正常形态。不豁免的话每本分卷的书都会报一条"正文可能丢了"，
        # 天天喊狼来了，真丢正文的时候人反而不看了。
        if ch.level != 2 and not ch.paragraphs:
            _add("chapter_empty", LEVEL_LOSS, f"{label}「{ch.title}」一个段落都没有：正文可能在清洗时丢了")
        elif ch.level != 2 and all(not p.text.strip() for p in ch.paragraphs):
            _add("chapter_blank", LEVEL_LOSS,
                f"{label}「{ch.title}」段落全空（{len(ch.paragraphs)} 段都没字）：正文可能在清洗时丢了"
            )
        # kind 是渲染层唯一的排版依据，契约外的取值会让它不知道该缩进还是该落空行。
        bad_kinds = sorted({p.kind for p in ch.paragraphs if p.kind not in PARAGRAPH_KINDS})
        if bad_kinds:
            _add("bad_kinds", LEVEL_LAYOUT,
                f"{label}「{ch.title}」出现契约外的段落类型 {bad_kinds}"
                f"（只允许 {'/'.join(PARAGRAPH_KINDS)}）：渲染层无法决定怎么排版"
            )
        for p in ch.paragraphs:
            _check_paragraph(p, f"{label}「{ch.title}」")

    for pos, p in enumerate(book.front_matter):
        _check_paragraph(p, f"前言第 {pos + 1} 段")
    for pos, p in enumerate(book.tail_matter):
        _check_paragraph(p, f"后记第 {pos + 1} 段")

    for group_name, items in _iter_groups(book):
        first_seen: dict[int, int] = {}
        prev: tuple[int, int] | None = None
        for idx, ch in items:
            if ch.number is None:
                continue  # 没解析出序号就跳过，不做推测
            if ch.number in first_seen:
                first = first_seen[ch.number]
                _add("number_dup", LEVEL_ORDER,
                    f"章节序号重复：{group_name} 里第 {idx + 1} 个章节「{ch.title}」的序号 "
                    f"{ch.number} 与第 {first + 1} 个章节「{book.chapters[first].title}」相同"
                )
            else:
                first_seen[ch.number] = idx
            if prev is not None and ch.number < prev[0]:
                _add("number_decrease", LEVEL_ORDER,
                    f"章节序号非递增：{group_name} 里第 {idx + 1} 个章节「{ch.title}」序号 "
                    f"{ch.number} 小于第 {prev[1] + 1} 个章节的 {prev[0]}（start_line={ch.start_line}）"
                )
            prev = (ch.number, idx)

    return issues


def validate(book: Book) -> list[str]:
    """问题清单的纯文字版（清洗报告、CLI 用）。结构见 validate_items()。"""
    return [it["text"] for it in validate_items(book)]



# ---------- 统计 ----------


def count_chars(text: str) -> int:
    """数字数：只数中文字符与字母数字，不数空白和标点。

    为什么不直接用 len()：不同来源的书排版习惯不同（全角空格缩进、
    空行隔场景、每句一个换行），len() 会把排版噪声算成内容，
    两本书的字数就没法比了。只数"读者真正看到的字"才可比。
    """
    n = 0
    for ch in text:
        if ch.isspace():
            continue
        o = ord(ch)
        if (
            0x4E00 <= o <= 0x9FFF  # 常用汉字
            or 0x3400 <= o <= 0x4DBF  # 扩展 A
            or 0xF900 <= o <= 0xFAFF  # 兼容汉字
            or 0x20000 <= o <= 0x2FA1F  # 扩展 B~F
        ):
            n += 1
        elif ch.isalnum():  # 拉丁字母、阿拉伯数字、假名等
            n += 1
    return n


def stats(book: Book) -> dict:
    """统计这本书的规模：章节数、段数、字数、平均每章字数、最长标题。

    为什么平均每章字数用"章节内字数 / 章节数"，而不是"总字数 / 章节数"：
    front_matter 与 tail_matter 不属于任何一章，混进去会把平均字数虚高，
    这个数是用来看"每章大概多长"的，算错就失去意义。

    为什么最长标题返回字符串而不是长度：给报告看的时候要能对上
    "是不是有个超长标题混进来了"，只给数字还得回去找。

    图片段（kind="image"）整段跳过：它的 text 是图片文件名而不是正文，
    把它算进段落数和字数会让"这本书多少字"虚高，也会让平均每章字数失真。
    """
    def _text_only(paragraphs: list[Paragraph]) -> list[Paragraph]:
        return [p for p in paragraphs if p.kind != "image"]

    total_paragraphs = len(_text_only(book.front_matter)) + len(_text_only(book.tail_matter))
    total_chars = sum(count_chars(p.text) for p in _text_only(book.front_matter))
    total_chars += sum(count_chars(p.text) for p in _text_only(book.tail_matter))

    chapter_chars = 0
    longest_title = ""
    for ch in book.chapters:
        text_paras = _text_only(ch.paragraphs)
        total_paragraphs += len(text_paras)
        chapter_chars += sum(count_chars(p.text) for p in text_paras)
        if len(ch.title) > len(longest_title):
            longest_title = ch.title
    total_chars += chapter_chars

    n = len(book.chapters)
    return {
        "chapters": n,
        "paragraphs": total_paragraphs,
        "chars": total_chars,
        "avg_chars_per_chapter": round(chapter_chars / n, 1) if n else 0.0,
        "longest_title": longest_title,
    }
