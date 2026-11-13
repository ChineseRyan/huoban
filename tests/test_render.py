"""render.py 的测试：章节树 → 干净 TXT。

照 tools/smoke_test.py 的风格：逐个直跑，一个 case 一个函数，纯 assert，
直接 python tests/test_render.py 跑。只断言排版契约（缩进、空行、行尾、结尾换行、
幂等），不断言内部实现细节。

覆盖的边界：
- 正常两章（缩进 / 标题 / 标题前后空行）；
- 只有 front_matter、只有 tail_matter、空书；
- blank 段、连续 blank 段、全空白段（空白段渲成两行空行，连续空行至多两行）；
- 标题原样保留（含全角空格与半角空格）；
- heading 段（契约允许）不加缩进；
- 段落自带行尾空格必须 strip；
- 幂等且不修改入参；
- txt_line_count 与实际行数一致；
- 全书范围的硬约束：开头无空行、无连续三个空行、行尾无空白、结尾恰好一个换行。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.model import Book, Chapter, Paragraph  # noqa: E402
from huoban.render import render_txt, txt_line_count  # noqa: E402

INDENT = "\u3000\u3000"


def _book(*chapters: Chapter, front=None, tail=None) -> Book:
    """造一本书，省掉每个 case 重复写三遍字段名。"""
    return Book(
        front_matter=list(front or []),
        chapters=list(chapters),
        tail_matter=list(tail or []),
    )


def _lines(text: str) -> list[str]:
    """把渲染结果切成行；最后一个换行不算一行。"""
    assert text.endswith("\n"), f"结尾必须以换行结束：{text!r}"
    return text.split("\n")[:-1]


def case_two_chapters() -> None:
    """两章各两段：段有缩进、标题无缩进、标题前后各一空行。"""
    book = _book(
        Chapter(title="第一章 开始", paragraphs=[
            Paragraph(text="他走了三天。"),
            Paragraph(text="终于看见了灯火。"),
        ]),
        Chapter(title="第二章 转折", paragraphs=[
            Paragraph(text="天亮了。"),
            Paragraph(text="他又走了。"),
        ]),
    )
    text = render_txt(book)
    lines = _lines(text)

    # 开头不留空行：第一行就是第一章的标题
    assert lines[0] == "第一章 开始", lines[:3]
    # 正文行都带缩进，且缩进之后紧跟正文（不是四个空格）
    assert INDENT + "他走了三天。" in lines, lines
    assert INDENT + "终于看见了灯火。" in lines, lines
    # 标题独占一行、不加缩进
    for title in ("第一章 开始", "第二章 转折"):
        idx = lines.index(title)
        assert not lines[idx].startswith(INDENT), lines[idx]
        assert lines[idx + 1] == "", f"标题 {title!r} 后面缺空行：{lines}"
    # 第一章标题后面紧跟一段正文，不再多空行
    i1 = lines.index("第一章 开始")
    assert lines[i1 + 2] == INDENT + "他走了三天。", lines
    # 第二章标题前面有一个空行（第一章最后一段之后）
    i2 = lines.index("第二章 转折")
    assert lines[i2 - 1] == "", lines
    # 全书顺序：前言 > 章一 > 章二
    assert i1 < i2


def case_front_matter_only() -> None:
    """没有章节、只有前言：照常渲染，且 note 段也带缩进。"""
    book = _book(front=[
        Paragraph(text="本书由某某整理，仅供试读。", kind="note"),
        Paragraph(text="", kind="blank"),
        Paragraph(text="他用了一个下午把它录完。"),
    ])
    text = render_txt(book)
    lines = _lines(text)
    assert lines == [
        INDENT + "本书由某某整理，仅供试读。",
        "",
        "",
        INDENT + "他用了一个下午把它录完。",
    ], lines
    assert lines[0].startswith(INDENT), lines[0]


def case_empty_book() -> None:
    """空书返回空字符串，不是 "\n"。"""
    assert render_txt(Book()) == "", repr(render_txt(Book()))
    assert txt_line_count(Book()) == 0


def case_blank_paragraph() -> None:
    """单个 blank 段输出两行空行（换场景的固定写法）。"""
    book = _book(front=[
        Paragraph(text="甲段"),
        Paragraph(text="", kind="blank"),
        Paragraph(text="乙段"),
    ])
    assert render_txt(book) == INDENT + "甲段\n\n\n" + INDENT + "乙段\n", repr(render_txt(book))


def case_consecutive_blanks() -> None:
    """连续 blank 段（含全空白段）最多压到两行空行。"""
    book = _book(front=[
        Paragraph(text="甲段"),
        Paragraph(text="", kind="blank"),
        Paragraph(text="   ", kind="blank"),
        Paragraph(text="\u3000\u3000", kind="blank"),
        Paragraph(text="\t"),
        Paragraph(text="乙段"),
    ])
    text = render_txt(book)
    assert text == INDENT + "甲段\n\n\n" + INDENT + "乙段\n", repr(text)
    assert "\n\n\n\n" not in text, repr(text)


def case_title_single_blank_each_side() -> None:
    """换场景改成两行空行后，章节标题前后仍各只有一个空行。

    为什么不会跟着变两行：标题前后各插一个空行，两章交界处（上一章末尾
    的空位 + 标题前空行）最多也只有两行，且正文段本身不产出空行；
    而章节树里章首/章末的 blank 段已被 clean 的 _build_book 弹掉。
    """
    book = _book(
        Chapter(title="第一章 开始", paragraphs=[
            Paragraph(text="甲段"),
            Paragraph(text="", kind="blank"),
            Paragraph(text="乙段"),
        ]),
        Chapter(title="第二章 结束", paragraphs=[Paragraph(text="丙段")]),
    )
    lines = _lines(render_txt(book))
    i1 = lines.index("第一章 开始")
    i2 = lines.index("第二章 结束")
    # 第一章标题后：恰一个空行，紧接正文
    assert lines[i1 + 1] == "" and lines[i1 + 2] == INDENT + "甲段", lines
    # 第二章标题前：恰一个空行，紧接上一章最后一段
    assert lines[i2 - 1] == "" and lines[i2 - 2] == INDENT + "乙段", lines


def case_trailing_blanks_dropped() -> None:
    """结尾的 blank 段不产出空行：结尾仍恰好一个换行。"""
    book = _book(front=[
        Paragraph(text="正文。"),
        Paragraph(text="", kind="blank"),
        Paragraph(text="", kind="blank"),
    ])
    text = render_txt(book)
    assert text == INDENT + "正文。\n", repr(text)


def case_title_keeps_spaces() -> None:
    """标题里的全角空格与半角空格原样保留。"""
    title = "第一章\u3000风 起 录"
    book = _book(Chapter(title=title, paragraphs=[Paragraph(text="正文。")]))
    lines = _lines(render_txt(book))
    assert title in lines, lines
    line = lines[lines.index(title)]
    assert line == title, repr(line)
    assert not line.startswith(INDENT), repr(line)


def case_tail_matter_last() -> None:
    """tail_matter 输出在最后（章节正文之后）。"""
    book = _book(
        Chapter(title="第一章 开始", paragraphs=[Paragraph(text="正文。")]),
        tail=[
            Paragraph(text="全书完", kind="note"),
            Paragraph(text="感谢阅读。"),
        ],
    )
    lines = _lines(render_txt(book))
    assert lines[-2] == INDENT + "全书完", lines[-3:]
    assert lines[-1] == INDENT + "感谢阅读。", lines[-3:]
    assert lines.index("第一章 开始") < lines.index(INDENT + "全书完")


def case_trailing_space_stripped() -> None:
    """段落 text 自带的行尾空白必须 strip。"""
    book = _book(
        Chapter(title="第一章 开始  ", paragraphs=[
            Paragraph(text="他走了三天。   "),
            Paragraph(text="天亮了。\t \u3000"),
        ]),
        front=[Paragraph(text="前言  ", kind="note")],
    )
    text = render_txt(book)
    for line in _lines(text):
        assert line == line.rstrip(), f"行尾有空白：{line!r}"
    lines = _lines(text)
    assert INDENT + "他走了三天。" in lines, lines
    assert lines[0] == INDENT + "前言", lines[:2]


def case_heading_paragraph() -> None:
    """kind=heading 的段落不加缩进（契约允许章内小标题）。"""
    book = _book(Chapter(title="第一章 开始", paragraphs=[
        Paragraph(text="楔子", kind="heading"),
        Paragraph(text="正文。"),
    ]))
    lines = _lines(render_txt(book))
    assert "楔子" in lines, lines
    assert not lines[lines.index("楔子")].startswith(INDENT), lines
    assert INDENT + "正文。" in lines, lines


def case_sep_paragraph_no_indent() -> None:
    """kind=sep 的场景分隔线不缩进（居中交给 EPUB 的 CSS）。

    纯文本没有居中，缩进就成了唯一能表达"这不是正文"的手段 ——
    所以 sep 与 heading 一样原样输出。
    """
    book = _book(Chapter(title="第一章 开始", paragraphs=[
        Paragraph(text="正文一。"),
        Paragraph(text="＊＊＊＊＊＊", kind="sep"),
        Paragraph(text="正文二。"),
    ]))
    lines = _lines(render_txt(book))
    assert "＊＊＊＊＊＊" in lines, lines
    assert not lines[lines.index("＊＊＊＊＊＊")].startswith(INDENT), lines
    assert INDENT + "正文二。" in lines, lines


def case_idempotent_no_mutation() -> None:
    """render_txt 调两次结果相同，且不修改 book 里的 paragraph.text。"""
    book = _book(
        Chapter(title="第一章 开始", paragraphs=[
            Paragraph(text="  他走了三天。  "),
            Paragraph(text="", kind="blank"),
            Paragraph(text="终于看见了灯火。\t", kind="note"),
        ]),
        front=[Paragraph(text="前言  ")],
        tail=[Paragraph(text="全书完")],
    )
    snapshot = [p.text for p in book.front_matter]
    snapshot += [p.text for ch in book.chapters for p in ch.paragraphs]
    snapshot += [p.text for p in book.tail_matter]

    first = render_txt(book)
    second = render_txt(book)
    assert first == second, "两次渲染结果不同，不是纯函数"

    after = [p.text for p in book.front_matter]
    after += [p.text for ch in book.chapters for p in ch.paragraphs]
    after += [p.text for p in book.tail_matter]
    assert after == snapshot, f"入参被修改了：{snapshot} -> {after}"
    assert book.chapters[0].title == "第一章 开始"
    # 入参里本来就有的行尾空白还在（说明没就地 strip）
    assert book.chapters[0].paragraphs[0].text == "  他走了三天。  "


def case_custom_indent() -> None:
    """indent 参数可控：换成半角四空格时正文跟着变。"""
    book = _book(Chapter(title="第一章 开始", paragraphs=[Paragraph(text="正文。")]))
    text = render_txt(book, indent="    ")
    lines = _lines(text)
    assert "    正文。" in lines, lines
    assert INDENT + "正文。" not in lines, lines


def case_line_count() -> None:
    """txt_line_count 等于 render_txt 输出的实际行数。"""
    books = [
        Book(),
        _book(front=[Paragraph(text="只有前言")]),
        _book(
            Chapter(title="第一章", paragraphs=[
                Paragraph(text="甲"), Paragraph(text="", kind="blank"), Paragraph(text="乙"),
            ]),
            Chapter(title="第二章", paragraphs=[Paragraph(text="丙")]),
            tail=[Paragraph(text="完")],
        ),
    ]
    for book in books:
        expected = len(render_txt(book).splitlines())
        assert txt_line_count(book) == expected, (expected, txt_line_count(book))
    # 具体值也钉一下，防止两边一起错。第三本手数一遍：
    #   标题一 / 空 / 甲 / 空 / 空 / 乙 / 空 / 标题二 / 空 / 丙 / 完  = 11 行。
    # （甲乙之间那个 blank 段写两行空行；标题前后仍各一行。）
    # 注意"丙"与"完"之间没有空行：契约只规定标题前后各一空行，
    # 没有规定后记前必须空行，这里就按"少做"处理，不凭空插一行。
    assert txt_line_count(books[1]) == 1, txt_line_count(books[1])
    assert txt_line_count(books[2]) == 11, txt_line_count(books[2])
    assert render_txt(books[2]) == "\n".join([
        "第一章", "",
        INDENT + "甲", "",
        "",
        INDENT + "乙", "",
        "第二章", "",
        INDENT + "丙",
        INDENT + "完",
    ]) + "\n", repr(render_txt(books[2]))


def case_global_constraints() -> None:
    """全书硬约束：开头无空行、无连续三个空行、行尾无空白、结尾恰好一个换行。"""
    book = _book(
        Chapter(title="第一章 空标题测试", paragraphs=[
            Paragraph(text="", kind="blank"),
            Paragraph(text="   ", kind="blank"),
            Paragraph(text="正文一  "),
        ]),
        Chapter(title="", paragraphs=[Paragraph(text="没有标题的章")]),  # 空标题不占行
        Chapter(title="第三章   ", paragraphs=[
            Paragraph(text="正文二"),
            Paragraph(text="", kind="blank"),
            Paragraph(text="", kind="blank"),
            Paragraph(text="正文三"),
        ]),
        front=[Paragraph(text="", kind="blank"), Paragraph(text="前言")],
        tail=[Paragraph(text="", kind="blank"), Paragraph(text="后记")],
    )
    text = render_txt(book)
    assert not text.startswith("\n"), repr(text[:20])
    assert not text.endswith("\n\n"), repr(text[-20:])
    assert text.endswith("\n"), repr(text[-20:])
    # 换场景允许两行空行，但不许出现三行及以上
    assert "\n\n\n\n" not in text, repr(text)
    for line in text.split("\n"):
        assert line == line.rstrip(), f"行尾有空白：{line!r}"
    # 空标题的章不产生标题行，但它的正文照样输出
    assert INDENT + "没有标题的章" in _lines(text), _lines(text)


def main() -> int:
    cases = [
        case_two_chapters,
        case_front_matter_only,
        case_empty_book,
        case_blank_paragraph,
        case_consecutive_blanks,
        case_title_single_blank_each_side,
        case_trailing_blanks_dropped,
        case_title_keeps_spaces,
        case_tail_matter_last,
        case_trailing_space_stripped,
        case_heading_paragraph,
        case_sep_paragraph_no_indent,
        case_idempotent_no_mutation,
        case_custom_indent,
        case_line_count,
        case_global_constraints,
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
