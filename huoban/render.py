"""章节树 → 干净 TXT。

这一层只认章节树（`model.Book`）：不碰原文、不碰编码、不碰文件名。输入的
每个段落已经由 clean.py 判定好类型（正文 / 标题 / 空行 / 注释），渲染层只
负责排版。这样"什么算正文"与"正文怎么排"不会混在两个模块里，以后加 EPUB
渲染器时，TXT 这一份不需要跟着改。

三条排版约束都是**全局**性质，所以统一放到最后一步做，而不是在每个输出点
各判一次：

1. 行尾不得有空白；
2. 任何位置不得出现连续两个空行（空行表示场景分隔，连出两个就是噪声）；
3. 开头不留空行，结尾恰好一个换行；整本书没有内容时返回空字符串。

为什么"标题前后各留一个空行"也用"先插空行、再统一压缩"的写法：
段落里本来就有 blank 段，章节边界也要空行，两者语义相同。如果章节边界
直接写死两个换行、段落又自己输出一个空行，交界处就会冒出两个连续空行。
分散判断一定会漏，集中压缩才能保证约束恒成立。

为什么不在本模块做"标点归一 / 去零宽"：那是 normalize.py 的职责，而且
必须在段落被判定之前做（脏字符会让缩进判定失效）。渲染层再洗一遍会让
同一件事存在两份实现，报告里也就说不清是谁改的。

为什么不修改传入的 book：章节树是中间态，同一个对象可能正被 model.save()
写盘、被报告统计、被另一个渲染器读取。渲染属于"读"，顺手 strip 一下
paragraph.text 会让内存里的书和磁盘上的 JSON 从此不一致，而且第二次渲染
看到的是被改过的数据，幂等性也就断了。
"""

from __future__ import annotations

from .model import Book, Paragraph

__all__ = ["render_txt", "txt_line_count"]

# 段首缩进的外部形态只在这里出现一次：默认两个全角空格（中文排版标准段首）。
DEFAULT_INDENT = "\u3000\u3000"


def _paragraph_lines(p: Paragraph, indent: str) -> list[str]:
    """把一个段落变成若干行；空段返回 [""]（空行）。

    为什么返回值是列表而不是单个字符串：段落 text 里理论上不该有换行，
    中间态若带进来换行（例如清洗层把碎行拼错、或手改 JSON），直接塞进
    输出会绕过"行尾无空白 / 不得连续空行"这两条全局约束。这里按物理行拆开，
    让每个物理行都走同一套规则 —— 宁可多出几行，也不让脏换行把整份输出的
    排版约束破掉。

    两种段落类型区别只在"要不要缩进"：
      * heading：原样输出，不加缩进，只去掉行尾空白；
      * 其余（body / note / blank 里意外带了字 / 契约外的 kind）：段首加 indent。

    为什么 body/note 要先 lstrip 再拼 indent：清洗层（normalize.clean_line）
    归一后的文本本身就带两个全角空格，直接拼会变成四格缩进。行首空白在中文
    小说里只可能是排版残留，不可能是有意义的正文，所以去掉。这是本模块唯一
    一处"改字"式的排版决定 —— 它只动行首空白，不动任何可见字符。

    为什么契约外的 kind 不抛异常：脏中间态首先要"能渲染出来看一眼"才查得动
    （model.validate 已经会报 kind 越界）。抛异常会让整本书渲染失败，代价远大于
    一行排版难看。
    """
    text = p.text.rstrip()  # 行尾空白一律不留
    if p.kind == "image":
        # 插图段（EPUB 导入专用）：TXT 里没有"图"这回事，把文件名当正文输出
        # 只会变成一行乱码（"img003.jpg"），所以整段跳过。
        return []
    if not text.strip():
        return [""]  # 空段当空行，不输出一行缩进空白

    pieces = text.splitlines() or [text]
    if p.kind in ("heading", "sep"):
        # heading 与 sep 都不缩进：前者是标题，后者是居中的装饰分隔线，
        # 给它加段首缩进会让"＊＊＊＊＊＊"看上去像正文里的一行乱码。
        return [s.rstrip() for s in pieces]
    return [indent + s.lstrip() for s in pieces]


def _collect_lines(book: Book, indent: str) -> list[str]:
    """按"前言 → 每章（标题 + 段落）→ 后记"的顺序摊平成行列表。

    章节标题用 Chapter.title，不用段落：M2 的章节树把标题存成章节的属性，
    标题行不是它自己的一个 Paragraph。契约仍允许 heading 段（EPUB 里可能需要
    章内小标题），那种段落在 _paragraph_lines 里按"不加缩进"处理。

    标题前后的空行先插进来（而不是保留历史状态"上一条是不是空行"）：
    相邻的空行统一由 _squeeze_blank_lines 压成一个，这里插多少都不会出错。
    标题是空白时整块跳过 —— 不能为一行不存在的标题凭空留两个空行。

    空行在列表里永远用真正的空字符串 "" 表示，压缩阶段只需比一次相等。
    """
    lines: list[str] = []
    for p in book.front_matter:
        lines.extend(_paragraph_lines(p, indent))

    for ch in book.chapters:
        title = (ch.title or "").rstrip()
        if title.strip():
            lines.append("")  # 标题前空行
            lines.append(title)
            lines.append("")  # 标题后空行
        for p in ch.paragraphs:
            lines.extend(_paragraph_lines(p, indent))

    for p in book.tail_matter:
        lines.extend(_paragraph_lines(p, indent))
    return lines


def _squeeze_blank_lines(lines: list[str]) -> list[str]:
    """压掉开头的空行与所有连续空行，并去掉结尾的空行。

    只用一遍扫描，靠"上一条已经是空行就跳过"判断连续，而不是输出后再用
    replace("\\n\\n", "\\n") 反复替换：字符串级替换分不清"连续空行"和
    "某个段落的换行"（以及结尾换行），容易把该留的空行吃掉或改错位置。

    结尾空行全部丢掉，由 render_txt 统一补一个 "\\n" —— 这样"结尾恰好一个
    换行"是拼出来的，不依赖输入最后一行的形态。
    """
    out: list[str] = []
    for line in lines:
        if not line.strip():
            # 开头不放空行；前一条已是空行则本次丢弃（连续空行只留一个）
            if not out or out[-1] == "":
                continue
            out.append("")
        else:
            out.append(line)
    while out and out[-1] == "":
        out.pop()
    return out


def render_txt(book: Book, indent: str = DEFAULT_INDENT) -> str:
    """把章节树渲染成干净 TXT（UTF-8 文本，未落盘）。

    输出形态：

    - 每个段落一行；正文与注释段首加 indent（默认两个全角空格）；
    - 章节标题行独占一行、不加缩进，前后各一个空行；
    - 前言在最前，后记在最后，都不加额外的标题；
    - 空段（text 全空白）当空行，连续空行压成一个；
    - 开头没有空行，结尾恰好一个 "\\n"；全书没有内容时返回 ""。

    纯函数：不改 book、不读写文件、不 print。同样的输入调两次结果相同。
    """
    lines = _squeeze_blank_lines(_collect_lines(book, indent))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def txt_line_count(book: Book) -> int:
    """render_txt 输出多少行（按 splitlines 计），给清洗报告核对用。

    为什么数 splitlines 而不是数 "\\n"：结尾那个换行不算行，空字符串的行数
    必须是 0。"\\n".count 会把 "a\\n" 数成 1 行、把 "" 也数成 1 行，两处都不对。
    """
    return len(render_txt(book).splitlines())
