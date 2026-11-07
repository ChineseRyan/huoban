"""diagnose.cn_to_int 与 _CN_NUM/_CN_UNIT 的逐字用例。

为什么要单独钉这张表：中文数字转换是整个章节识别的地基 —— 模型层
（model.parse_heading）、清洗层的顿号分节闸门、体检的"序号是否递增"
全部走 diagnose.cn_to_int() 这一个实现。它错一个单位，整本书的章节
结构就会整族丢失（实测：《书A》第三回原文写作"第叁回"，大写数字
不在表里时那一章直接掉进上一章正文）。

这些用例钉的是**当前实测行为**，包括已知的取舍（位值式"二〇二四"
不支持）。行为要改，先改这里，别让它悄悄漂移。

约定沿用 tests/test_model.py：case_* 函数 + main() 逐条跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.diagnose import (  # noqa: E402
    _numbering,
    cn_to_int,
    match_chapter,
    strip_frame,
    whole_book_in_one_line,
)


def _check(pairs: list[tuple[str, int | None]]) -> None:
    bad = [(s, cn_to_int(s), want) for s, want in pairs if cn_to_int(s) != want]
    assert not bad, bad


def case_ones_and_tens() -> None:
    """个位与十位：单个数字、十、十几、几十几。"""
    _check([
        ("一", 1), ("二", 2), ("两", 2), ("九", 9),
        ("十", 10), ("十一", 11), ("二十", 20), ("二十一", 21),
    ])


def case_hundreds_and_thousands() -> None:
    """百、千与"零"占位：零只是占位符，不参与运算。

    注意"一千零一十"= 1010：零后面接着"一十"，等价于"一千零一十"，
    这是中文数字的常规读法，不是 1000+0+10 之外的什么特殊规则。
    """
    _check([
        ("一百", 100), ("一百零一", 101), ("一百一十", 110), ("一百二十三", 123),
        ("两百", 200), ("两百零五", 205),
        ("一千", 1000), ("一千零一", 1001), ("一千零一十", 1010), ("三千六百", 3600),
    ])


def case_wan_splits_sections() -> None:
    """"万"把前面一段乘 10000 再累加，后面的"零三百"重新起算。

    "十万"= 100000：十 前面没有数字时按 1 算（"十"就是"一十"）。
    """
    _check([
        ("一万", 10000), ("一万零三百", 10300), ("十万", 100000),
    ])


def case_financial_numerals() -> None:
    """大写（壹贰叁…）与繁体异体（貳參陸）必须认。

    实测来源：《书A》"第叁回"；繁体版 txt 里"貳""參""陸"混用。
    """
    _check([
        ("壹", 1), ("叁", 3), ("貳", 2), ("參", 3), ("陆", 6), ("陸", 6),
        ("壹佰贰拾叁", 123),
    ])


def case_arabic_and_fullwidth_digits() -> None:
    """纯阿拉伯数字直接转；全角数字先折半角；前后空白先去掉。"""
    _check([
        ("12", 12), ("０１２", 12), (" 12 ", 12), ("007", 7),
    ])


def case_rejects_non_numbers() -> None:
    """不是数字的一律返回 None —— 调用方靠 None 判"这行不是章节标题"。

    "一亿"返回 None 是有意的：章节号不会有亿级，"亿"不在 _CN_UNIT 里。
    混排（"1万"）也返回 None：只认"纯阿拉伯"或"纯中文"两种形态。
    """
    _check([
        ("", None), ("第", None), ("abc", None), ("一亿", None), ("1万", None),
        ("三章", None),
    ])


def case_positional_form_is_not_supported() -> None:
    """位值式写法（"二〇二四"= 2024）不支持，这是已知取舍，不是 bug。

    本项目的用途只有章节号，章节号不用位值式（"第二〇章"这种写法不存在）。
    真遇上了，"二〇二四"会按"二 〇 二 四"逐个覆盖 num，最后剩下 4 ——
    错得离谱但只影响这种畸形输入，且不会误判成"章节结构可信"。
    这条用例的价值是：将来谁想支持位值式，会先看到这里已经明确放弃了。
    """
    _check([("二〇二四", 4), ("二〇", 0)])


def case_match_chapter_recognizes_financial_heading() -> None:
    """大写数字的标题行也要被认成章节标题，且家族名正确。

    "第叁回"若不被 match_chapter 认出来，model.parse_heading 就解析不出
    标题，这一回会被当成正文吞进上一章（《书A》24 章变 23 章）。
    """
    assert match_chapter("第三回　夜过石桥") == "第N章类"
    assert match_chapter("第叁回　夜过石桥") == "第N章类"
    assert match_chapter("第１２章 夜行") == "第N章类"
    assert match_chapter("1、夜行") == "数字顿号分节"
    assert match_chapter("他站了很久，没有说话。") is None


def case_whole_book_in_one_line_needs_a_long_line() -> None:
    """糊行判据只认「超过 3000 字的行」，短行里塞再多章节号也不算。

    TODO 第 1 组第 2 条：旧判据把「一行出现 >=3 个第X章」也当糊行，
    实测 51 本真实书唯一命中的是一行 78 字的注释清单（正文引用章节号
    是家常便饭）。三个字段名是跨模块契约（report.py / batch_check.py /
    smoke_test.py 都读），判据收紧但字段一个都不能少。
    """
    # 短行：三个章节标记，但只有几十字 → 不算糊行，只把证据留在字段里
    note = "注释：见第1章、第2章、第3章，另见第4章的附录。"
    assert len(note) < 100
    short = whole_book_in_one_line([note])
    assert short["suspected"] is False, short
    assert short["long_line_count"] == 0, short
    assert short["lines_with_multiple_chapters"] == 1, short

    # 干净的正常书：没有超长行，也不该误报
    plain = whole_book_in_one_line(["第一章　开", "", "他走了很久。", "", "第二章　终"])
    assert plain["suspected"] is False, plain

    # 真糊行：一整行 5000+ 字，判据必须仍为真（tools/smoke_test.py 也盯着）
    sentence = "他还记得那一天的情景，风很大，吹得窗棂一直在响。"
    body = "".join(f"第{i}章 标题　{sentence * 8}" for i in range(1, 30))
    assert len(body) > 3000
    long = whole_book_in_one_line([body])
    assert long["suspected"] is True, long
    assert long["long_line_count"] == 1, long
    assert long["long_line_samples"][0][1] == len(body), long["long_line_samples"]


def case_strip_frame_peels_symmetric_borders() -> None:
    """下载站的「========== 第001章 ==========」必须剥成「第001章」。

    实测《书K》197 个章节标题全带这层边框，不剥就一章都认不出。
    纯装饰分隔线、两侧字符不一致的伪边框、正文里提到章节号的行都不许动。
    """
    assert strip_frame("========== 第001章 ==========") == "第001章"
    assert strip_frame("──── 第一章 ────") == "第一章"
    assert strip_frame("★★★ 第三章 ★★★") == "第三章"
    assert strip_frame("=== 序 ===") == "序"
    assert strip_frame("  ====== 番外 第2章 ======  ") == "番外 第2章"
    # 纯装饰线：整行原样保留，仍归「分隔线」家族
    assert strip_frame("==========") == "=========="
    assert strip_frame("----------") == "----------"
    # 两侧不是同一个字符：不算对称边框
    assert strip_frame("=== 第1章 ---") == "=== 第1章 ---"
    # 正文里引用章节号、正常标题：一个字都不动
    assert strip_frame("他说第三章的故事很精彩。") == "他说第三章的故事很精彩。"
    assert strip_frame("第一章 山雨欲来") == "第一章 山雨欲来"


def case_framed_headings_recognized_and_numbered() -> None:
    """体检侧：剥完边框既要认出家族，也要把序号排成递增序列。"""
    assert match_chapter("========== 第001章 ==========") == "第N章类"
    assert match_chapter("==========") == "分隔线"
    numbered = _numbering([
        "========== 第001章 ==========",
        "========== 第002章 ==========",
        "========== 第003章 ==========",
    ])
    assert numbered["numeric_titles"] == 3, numbered
    assert numbered["monotonic"] is True, numbered


def main() -> int:
    cases = [
        case_ones_and_tens,
        case_hundreds_and_thousands,
        case_wan_splits_sections,
        case_financial_numerals,
        case_arabic_and_fullwidth_digits,
        case_rejects_non_numbers,
        case_positional_form_is_not_supported,
        case_match_chapter_recognizes_financial_heading,
        case_whole_book_in_one_line_needs_a_long_line,
        case_strip_frame_peels_symmetric_borders,
        case_framed_headings_recognized_and_numbered,
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
