"""清洗报告渲染的直跑测试：用自造的 report dict 断言渲染行为。

不 import clean.py（还在写），只 import 渲染器；
所有样本都是手搓的假数据，跑 python tests/test_cleanreport.py 即可。
"""

from __future__ import annotations

import copy
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.cleanreport import render_clean_markdown  # noqa: E402


def _base_report() -> dict:
    """一份"全字段都有值"的假报告，各 case 在它上面改一处。"""
    return {
        "file": r"C:\books\samples\messy.txt",
        "size_bytes": 4321,
        "encoding": {"chosen": "utf-8", "confidence": 0.48},
        "lines": {"total": 77, "nonblank": 61, "blank_in": 16, "blank_out": 11},
        "dropped": {"ad": 9, "toc": 7, "extra_blank": 5},
        "merged": {"groups": 3, "absorbed_lines": 3},
        "fixed": {
            "zero_width_lines": 36,
            "trailing_space_lines": 36,
            "indent_normalized_lines": 0,
            "punctuation_lines": 2,
            "space_collapsed_lines": 0,
            "changed_lines": 38,
        },
        "samples": {
            "ad": [
                {
                    "line": 1,
                    "text": "更多精彩小说请访问 www.example.com",
                    "keywords": ["更多精彩", "www"],
                    "has_url": True,
                }
            ],
            "toc": [{"line": 5, "text": "目录"}, {"line": 6, "text": "第一章 起始"}],
            "merged": [{"lines": [17, 18], "result": "他站在城门口，望着远处的灯火。"}],
        },
        "chapters": {"count": 6, "front_paragraphs": 1, "tail_paragraphs": 0, "kept_last_toc_line": 13},
        "stats": {
            "chapters": 6,
            "paragraphs": 36,
            "chars": 1234,
            "avg_chars_per_chapter": 205.7,
            "longest_title": "第六章 终局",
        },
        "warnings": ["第 3 个章节「第三章 抉择」一个段落都没有"],
    }


def case_full() -> None:
    """全字段：关键小节与关键数字都要出现，且表头齐全。"""
    out = render_clean_markdown(_base_report())
    for header in ("概览", "行数", "删除", "碎行合并", "修复", "章节与前后附文", "样例", "警告", "下一步"):
        assert header in out, header
    # 铁律 1 必须在报告里显眼地写出来
    assert "原始文件未改动" in out, "缺少只读声明"
    # 摘要三要素
    assert "messy.txt" in out
    assert "utf-8" in out and "0.48" in out
    assert "77" in out
    # 统计口径
    assert "4321 / 1024" not in out and "4.2 KB" in out, "文件大小没换算成 KB"
    assert "205.7" in out
    assert "第六章 终局" in out
    assert "第三章 抉择" in out, "警告内容没渲染出来"
    assert "2、7、9" not in out
    # 删除合计 = 9 + 7 + 5
    assert "21" in out, "删除合计没算出来"
    # 保留的目录行号
    assert "13" in out
    # 下一步要提到中间态
    assert "book.json" in out


def case_samples_empty() -> None:
    """真的什么都没删也没合并：样例桶空，三处都写"无"。"""
    r = _base_report()
    r["samples"] = {"ad": [], "toc": [], "merged": []}
    # "无"要求计数也归零：删了 9 行却留 0 条样例属于另一种情况，
    # 由 case_samples_total_without_examples 覆盖。
    r["dropped"] = {"ad": 0, "toc": 0, "extra_blank": 5}
    r["merged"] = {"groups": 0, "absorbed_lines": 0}
    out = render_clean_markdown(r)
    assert "广告样例：无" in out
    assert "目录样例：无" in out
    assert "合并样例：无" in out
    # 空桶时不该再画表头
    assert "| 命中词 |" not in out
    assert "| 合并结果 |" not in out


def case_samples_missing_keys() -> None:
    """samples 连键都没有（防御性）：照样不炸，按"没留样例"如实说明。"""
    r = _base_report()
    r["samples"] = {}
    r["dropped"] = {"ad": 0, "toc": 0, "extra_blank": 5}
    r["merged"] = {"groups": 0, "absorbed_lines": 0}
    out = render_clean_markdown(r)
    assert "广告样例：无" in out
    assert out.endswith("\n")


def case_warnings_empty() -> None:
    """warnings 为空：小节必须还在，写"无"。"""
    r = _base_report()
    r["warnings"] = []
    out = render_clean_markdown(r)
    assert "警告" in out
    seg = out.split("警告")[-1]
    assert "无" in seg, "warnings 为空时这一节应该写「无」"


def case_kept_last_toc_line_none() -> None:
    """kept_last_toc_line 为 None：这一行整个不显示。"""
    r = _base_report()
    r["chapters"]["kept_last_toc_line"] = None
    out = render_clean_markdown(r)
    assert "保留的最后一行目录" not in out


def case_kept_last_toc_line_zero() -> None:
    """kept_last_toc_line 为 0：0 是合法行号之外的值，按"有值就显示"处理，不能当 None。"""
    r = _base_report()
    r["chapters"]["kept_last_toc_line"] = 0
    out = render_clean_markdown(r)
    assert "保留的最后一行目录：0" in out


def case_pipe_in_cell() -> None:
    """单元格含竖线：必须转义成 「反斜杠 + 竖线」，否则表格会被撑破。"""
    r = _base_report()
    r["samples"]["ad"] = [
        {"line": 3, "text": "a|b|c 广告", "keywords": ["a|b"], "has_url": False}
    ]
    out = render_clean_markdown(r)
    assert r"a\|b\|c" in out, out
    assert r"a\|b" in out
    # 转义后，表格行的竖线数量仍与表头一致（4 列表头 = 5 个分隔竖线）
    header_prefix = "| 行号 | 命中词 | 含链接 | 内容 |"
    assert header_prefix in out
    for line in out.splitlines():
        if "a\\|b\\|c" in line:
            assert line.count("|") - line.count("\\|") == 5, line


def case_zeros() -> None:
    """各项数字全为 0：表格仍在、0 照实显示，不出现除零或空白单元格。"""
    r = _base_report()
    r["size_bytes"] = 0
    r["lines"] = {"total": 0, "nonblank": 0, "blank_in": 0, "blank_out": 0}
    r["dropped"] = {"ad": 0, "toc": 0, "extra_blank": 0}
    r["merged"] = {"groups": 0, "absorbed_lines": 0}
    r["fixed"] = {
        "zero_width_lines": 0,
        "trailing_space_lines": 0,
        "indent_normalized_lines": 0,
        "punctuation_lines": 0,
        "space_collapsed_lines": 0,
        "changed_lines": 0,
    }
    r["chapters"] = {"count": 0, "front_paragraphs": 0, "tail_paragraphs": 0, "kept_last_toc_line": None}
    r["stats"] = {
        "chapters": 0,
        "paragraphs": 0,
        "chars": 0,
        "avg_chars_per_chapter": 0.0,
        "longest_title": "",
    }
    r["samples"] = {"ad": [], "toc": [], "merged": []}
    r["warnings"] = []
    out = render_clean_markdown(r)
    assert "0.0 KB" in out
    assert "0.0 字" in out
    assert "（无）" in out, "空的最长标题要有占位"
    # 整行合并计数为 0 也要有行
    assert "整行广告" in out
    assert "合计" in out
    assert out.endswith("\n")


def case_hardwrap_rows_hidden_when_zero() -> None:
    """硬折行那两行没数据时不出现：99% 的书没被硬切过，挂两行 0 只是噪声。"""
    r = _base_report()
    out = render_clean_markdown(r)
    assert "硬折行" not in out, "没有硬折行证据时不该出现这一行"


def case_hardwrap_rows_shown_when_nonzero() -> None:
    """有硬折行证据时，拒绝数与"证据不足原样保留"数要分别显示。

    两个数必须分得开：前者是**已改动**（并回了上一段），后者是**没动**，
    混成一个数会让人以为章节结构被改了却没在报告里说清。
    """
    r = _base_report()
    r["fixed"]["wrap_chunk_reject_lines"] = 12
    r["fixed"]["wrap_chunk_suspect_lines"] = 3
    out = render_clean_markdown(r)
    assert "硬折行切出来的假标题（已并回上一段）" in out
    assert "疑似硬折行假标题（证据不足，原样保留）" in out
    assert "12" in out and "3" in out


def case_adjust_section_hidden_when_unused() -> None:
    """没传 adjust 时，报告里连"人工调整"这四个字都不该出现。

    默认跑一遍是没有人工介入的，报告要和以前一字不差 —— 否则每本书都多一节
    "人工调整 0"，读报告的人会以为有什么自动调整发生了。
    """
    out = render_clean_markdown(_base_report())
    assert "人工调整" not in out, "没有 adjust 时不该出现人工调整小节"


def case_adjust_section_shown_when_used() -> None:
    """用了 adjust 就列清楚：保护了几行、其中广告/目录各几行、断开了几次合并。"""
    r = _base_report()
    r["adjust"] = {
        "applied": {"protect_lines": [1024, 1051], "merge_breaks": [1052], "protect_count": 2},
        "requested": {"protect": 2, "merge_breaks": 1},
        "protected": {"ad": 1, "toc": 1, "lines": 2},
        "merge_breaks_hit": 1,
        "ignored": [],
    }
    out = render_clean_markdown(r)
    assert "人工调整" in out, "有 adjust 时必须有这一节"
    assert "人工保护未删的行" in out
    assert "整行广告" in out and "目录块" in out
    assert "强制断开的碎行合并" in out
    # 行号口径必须写清楚：说反了用户会拿输出文件的行号去找
    assert "清洗前的原文行号" in out
    # 保护范围的口径也要写明：只管文字不删，不管排版（缩进/韵文标记）不变 ——
    # 用户勾了"别动"之后看到缩进变了会以为程序没执行他的勾选。
    assert "保护的口径只管" in out and "文字不删" in out, out[-600:]


def case_adjust_section_needs_only_breaks() -> None:
    """只断开了合并、一行都没保护时，那一节也要出现（只是少几行）。"""
    r = _base_report()
    r["adjust"] = {
        "applied": {"protect_lines": [], "merge_breaks": [12], "protect_count": 0},
        "requested": {"protect": 0, "merge_breaks": 1},
        "protected": {"ad": 0, "toc": 0, "lines": 0},
        "merge_breaks_hit": 1,
        "ignored": [],
    }
    out = render_clean_markdown(r)
    assert "人工调整" in out
    assert "强制断开的碎行合并" in out
    assert "人工保护未删的行" not in out, "0 行不该挂出来"


def case_adjust_section_shown_when_nothing_matched() -> None:
    """给了行号却一条都没命中：那一节也要出现，并点出是哪几行。

    否则用户以为保护生效了，报告里连"人工调整"四个字都没有 —— 静默空转
    是最坏的一种失败。
    """
    r = _base_report()
    r["adjust"] = {
        "applied": {"protect_lines": [5005, 6006], "merge_breaks": [7007], "protect_count": 2},
        "requested": {"protect": 2, "merge_breaks": 1},
        "protected": {"ad": 0, "toc": 0, "inline": 0, "lines": 0},
        "merge_breaks_hit": 0,
        "unmatched": {"protect_lines": [5005, 6006], "merge_breaks": [7007]},
        "ignored": [],
    }
    out = render_clean_markdown(r)
    assert "人工调整" in out, "没命中也要有这一节"
    assert "给了行号但没东西可保护" in out
    assert "5005" in out and "6006" in out
    assert "给了行号但那里本来就不合并" in out and "7007" in out
    # 0 的自留地不该被挂出来
    assert "人工保护未删的行" not in out


def case_adjust_samples_split_from_deleted() -> None:
    """样例里"删掉的"和"人工保护留下的"必须分表。

    混在一张「（已删除）」表里，caption 的"共 N 条"和表格行数自相矛盾，
    用户会以为自己保护的那一行其实被删了 —— 这正是复核界面最不能出的错。
    """
    r = _base_report()
    r["adjust"] = {
        "applied": {"protect_lines": [3003], "merge_breaks": [], "protect_count": 1},
        "requested": {"protect": 1, "merge_breaks": 0},
        "protected": {"ad": 1, "toc": 0, "lines": 1},
        "merge_breaks_hit": 0,
        "ignored": [],
    }
    # 行号特意取 4 位数：_base_report 里的统计表也有 "| 3 |" "| 6 |" 这类小数字，
    # 用它们做断言会撞车（踩过一次）。
    r["samples"]["ad"] = [
        {"line": 3003, "text": "记住本站", "keywords": ["记住本站"], "has_url": False, "protected": True},
        {"line": 9009, "text": "www.example.com", "keywords": ["www"], "has_url": True, "protected": False},
    ]
    out = render_clean_markdown(r)
    assert "整行广告（人工保护未删）" in out, "保护下来的广告要有单独小节"
    # 两张表各放各的：已删除表里不许出现第 3 行，保护表里不许出现第 9 行
    deleted_part, kept_part = out.split("整行广告（人工保护未删）")
    assert "| 9009 |" in deleted_part, deleted_part[-300:]
    assert "| 3003 |" not in deleted_part, "受保护的广告不该出现在「已删除」表里"
    assert "| 3003 |" in kept_part, kept_part[:300]
    assert "| 9009 |" not in kept_part, "已删除的广告不该出现在「保护未删」表里"


def case_adjust_toc_samples_split_from_deleted() -> None:
    """目录行同理：保护下来的目录条目单独一张表。"""
    r = _base_report()
    r["adjust"] = {
        "applied": {"protect_lines": [6006], "merge_breaks": [], "protect_count": 1},
        "requested": {"protect": 1, "merge_breaks": 0},
        "protected": {"ad": 0, "toc": 1, "lines": 1},
        "merge_breaks_hit": 0,
        "ignored": [],
    }
    r["samples"]["toc"] = [
        {"line": 5005, "text": "目录", "protected": False},
        {"line": 6006, "text": "第一章 起始", "protected": True},
    ]
    out = render_clean_markdown(r)
    assert "目录行（人工保护未删）" in out
    deleted_part, kept_part = out.split("目录行（人工保护未删）")
    assert "| 6006 |" not in deleted_part, "受保护的目录行不该出现在「已删除」表里"
    assert "| 6006 |" in kept_part
    assert "| 5005 |" in deleted_part and "| 5005 |" not in kept_part


def case_adjust_inline_protected_row() -> None:
    """行内广告受保护未切：人工调整表里要单独一行，且不混进"保护未删的行"。

    行内切除本来就不删行，把它算进"保护未删的行"会让账目对不上
    （dropped.ad 是 0，保护却记了 1）。
    """
    r = _base_report()
    r["adjust"] = {
        "applied": {"protect_lines": [3], "merge_breaks": [], "protect_count": 1},
        "requested": {"protect": 1, "merge_breaks": 0},
        "protected": {"ad": 0, "toc": 0, "inline": 1, "lines": 0},
        "merge_breaks_hit": 0,
        "ignored": [],
    }
    out = render_clean_markdown(r)
    assert "人工调整" in out
    assert "行内广告（受保护未切）" in out
    assert "人工保护未删的行" not in out, "行内切除不删行，不该出现在这一行"


def case_long_text_clipped() -> None:
    """超长 text 截断：不能整段塞进单元格，要注明总字数。"""
    long_text = "广" * 120
    r = _base_report()
    r["samples"]["ad"] = [
        {"line": 8, "text": long_text, "keywords": ["广"], "has_url": True}
    ]
    r["samples"]["toc"] = [{"line": 9, "text": long_text}]
    r["samples"]["merged"] = [{"lines": [10, 11, 12], "result": long_text}]
    out = render_clean_markdown(r)
    assert "共 120 字" in out, "截断后应注明原文总字数"
    assert long_text not in out, "超长文本没有被截断"
    # 任何一行都不该长得离谱（表格行本身有限）
    assert all(len(line) < 400 for line in out.splitlines()), "有异常长的行"


def case_samples_capped() -> None:
    """样例多于 8 条：只展示前 8 条，并写明共多少条。

    这里刻意让"真实条数"与"样例条数"相等（12/12、10/10、9/9），
    这样能验证 total 取自 dropped/merged 之后依然对得上；
    两者不等的情况由 case_samples_total_exceeds_shown 覆盖。
    """
    r = _base_report()
    r["dropped"]["ad"] = 12
    r["dropped"]["toc"] = 10
    r["merged"]["groups"] = 9
    r["samples"]["ad"] = [
        {"line": i, "text": f"广告{i}", "keywords": ["广告"], "has_url": True} for i in range(1, 13)
    ]
    r["samples"]["toc"] = [{"line": i, "text": f"第{i}章"} for i in range(1, 11)]
    r["samples"]["merged"] = [
        {"lines": [i, i + 1], "result": f"合并{i}"} for i in range(1, 10)
    ]
    out = render_clean_markdown(r)
    assert "共 12 条，此处展示前 8 条" in out
    assert "共 10 条，此处展示前 8 条" in out
    assert "共 9 条，此处展示前 8 条" in out
    assert "广告9" not in out, "第 9 条不该出现"
    assert "广告8" in out, "第 8 条应该出现"


def case_samples_total_exceeds_shown() -> None:
    """真实条数 > 留下的样例数：说明里的"共 N 条"必须是真实条数。

    回归用例：旧代码拿 len(samples["ad"]) 当总数，删掉 20 行却只留 8 条样例时
    会写"共 8 条，全部展示"，读者以为只删了 8 行，账对不上。
    """
    r = _base_report()
    r["dropped"]["ad"] = 20
    r["samples"]["ad"] = [
        {"line": i, "text": f"广告{i}", "keywords": ["广告"], "has_url": True} for i in range(1, 9)
    ]
    r["samples"]["toc"] = []
    r["dropped"]["toc"] = 0
    r["merged"]["groups"] = 0
    r["samples"]["merged"] = []
    out = render_clean_markdown(r)
    assert "共 20 条，此处展示前 8 条" in out, out[out.index("广告样例") :][:200]
    assert "共 8 条" not in out, "不能把样例条数当总条数"


def case_samples_total_without_examples() -> None:
    """有真实条数但一个样例都没留：要写"本次未保留样例"，不能写"无"。"""
    r = _base_report()
    r["dropped"]["toc"] = 6
    r["samples"]["toc"] = []
    out = render_clean_markdown(r)
    assert "共 6 条，本次未保留样例" in out, "有删除却没有样例时说法不对"
    assert "目录样例：无" not in out, "删了 6 行却说无，是自相矛盾"


def case_ends_with_newline() -> None:
    """输出必须以换行结尾，且不是空串。"""
    out = render_clean_markdown(_base_report())
    assert out.endswith("\n")
    assert not out.endswith("\n\n")
    assert len(out) > 200


def case_no_stdout() -> None:
    """渲染器不许 print 业务结果（职责在 CLI 层）。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_clean_markdown(_base_report())
    assert buf.getvalue() == "", f"渲染时不该有输出：{buf.getvalue()!r}"


def case_pure_no_mutation() -> None:
    """纯函数：不许改传入的 dict（render 之后原样可比）。"""
    r = _base_report()
    before = copy.deepcopy(r)
    render_clean_markdown(r)
    assert r == before, "渲染过程改动了传进去的 report"


def case_no_azw3_in_next_steps() -> None:
    """「下一步」不许提 AZW3 —— 它全仓零实现，写在给用户看的报告里就是谎。

    AGENTS.md 曾声称 M3 已做 AZW3（调 ebook-convert），2026-10-26 实测订正：
    没有任何 azw3 函数、子命令或 ebook-convert 调用。这条用例是防它被"顺手
    补回去" —— 计划文字留在 AGENTS.md 里就够了，报告只写现在真能跑的东西。
    """
    out = render_clean_markdown(_base_report())
    assert "AZW3" not in out, "报告里不许出现未实现的格式"
    # 但两条真实出口必须在
    assert "干净 TXT" in out and "EPUB" in out


_CASES = [
    case_full,
    case_samples_empty,
    case_samples_missing_keys,
    case_warnings_empty,
    case_kept_last_toc_line_none,
    case_kept_last_toc_line_zero,
    case_pipe_in_cell,
    case_zeros,
    case_hardwrap_rows_hidden_when_zero,
    case_hardwrap_rows_shown_when_nonzero,
    # 人工调整（adjust）
    case_adjust_section_hidden_when_unused,
    case_adjust_section_shown_when_used,
    case_adjust_section_needs_only_breaks,
    case_adjust_section_shown_when_nothing_matched,
    case_adjust_samples_split_from_deleted,
    case_adjust_toc_samples_split_from_deleted,
    case_adjust_inline_protected_row,
    case_long_text_clipped,
    case_samples_capped,
    case_samples_total_exceeds_shown,
    case_samples_total_without_examples,
    case_ends_with_newline,
    case_no_stdout,
    case_pure_no_mutation,
    case_no_azw3_in_next_steps,
]


def test_all_cases() -> None:
    """pytest 入口：把全部 case 跑一遍，任一失败就抛 AssertionError。

    直跑入口是 main()；这个函数只是让 `pytest tests/` 也能收集到，两不耽误。
    """
    for c in _CASES:
        c()


def main() -> int:
    cases = _CASES
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
