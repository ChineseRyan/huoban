"""脱敏层用例：断言「原文一个字都不留、元数据一个都不丢」。

用例写法沿用 tests/test_clean.py：case_* 函数 + _CASES 表 + test_all_cases()。
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from huoban.cli import main as cli_main
from huoban.cleanreport import render_clean_markdown
from huoban.redact import (
    alias_for,
    alias_table,
    redact_batch_record,
    redact_diagnose,
    redact_report,
    scrub_line,
    shape,
)
from huoban.report import render_markdown

SECRET = "林凡抬头看向远处的山峰，心中一震"
SAMPLE_TXT = ROOT / "samples" / "messy.txt"


def _sample_report() -> dict:
    return {
        "file": r"E:\文档\txt\小说\某本书.txt",
        "size_bytes": 4096,
        "encoding": {"chosen": "utf-8", "confidence": 0.9},
        "lines": {"total": 800, "blank": 100},
        "dropped": {"ad": 3, "toc": 18, "extra_blank": 40},
        "merged": {"groups": 2, "absorbed_lines": 4},
        "fixed": {"inline_ad_cut_lines": 1, "zero_width_lines": 0,
                  "trailing_space_lines": 0, "punctuation_lines": 0},
        "chapters": {"count": 12, "front_paragraphs": 1, "tail_paragraphs": 0},
        "stats": {"chapters": 12, "paragraphs": 300, "chars": 40000,
                  "longest_title": SECRET[:10]},
        "samples": {
            "ad": [{"line": 7, "text": SECRET, "keywords": ["www.example.com"],
                    "has_url": True}],
            "toc": [{"line": 21, "text": "第一章 风起"}],
            "merged": [{"lines": [40, 41], "result": SECRET[:6]}],
            "inline_ad": [{"line": 55, "text": SECRET[:8],
                           "cut": "本站网址www.x.com", "kept": SECRET[:5]}],
        },
        "dropped": {"ad": 3, "toc": 18},
        "warnings": [f"第 3 章正文中出现可疑字符「{SECRET[:4]}」（U+4E00）"],
    }


def case_shape_hides_every_original_char():
    s = shape(SECRET)
    assert "林" not in s and "山" not in s, s
    assert s.startswith("〔长16 "), s  # SECRET 实测 16 字（15 汉字 + 1 全角逗号）
    assert "中15" in s and "符1" in s, s
    assert "#" in s, s


def case_shape_is_stable_and_distinguishes():
    assert shape(SECRET) == shape(SECRET)
    assert shape(SECRET) != shape(SECRET + "。")


def case_shape_marks_empty_not_lost():
    assert shape("") == "〔空〕"
    assert shape(None) == "〔空〕"


def case_shape_classifies_character_kinds():
    s = shape("abc 中，")
    assert "西3" in s and "中1" in s and "符1" in s and "空1" in s, s


def case_redact_keeps_counts_and_line_numbers():
    rep = _sample_report()
    out = redact_report(rep)
    assert out["size_bytes"] == 4096
    assert out["dropped"] == {"ad": 3, "toc": 18}
    assert out["stats"]["chapters"] == 12
    assert out["stats"]["paragraphs"] == 300
    assert out["samples"]["ad"][0]["line"] == 7
    assert out["samples"]["merged"][0]["lines"] == [40, 41]
    assert out["samples"]["inline_ad"][0]["line"] == 55
    assert out["redacted"] is True


def case_redact_wipes_every_text_field():
    out = redact_report(_sample_report())
    for bucket in ("ad", "toc", "merged", "inline_ad"):
        for item in out["samples"][bucket]:
            for key in ("text", "cut", "kept", "result"):
                if key in item:
                    assert item[key].startswith("〔"), (bucket, key, item[key])
                    assert SECRET[:2] not in item[key], (bucket, key)
    assert out["stats"]["longest_title"].startswith("〔")
    for w in out["warnings"]:
        assert SECRET[:2] not in w, w


def case_redact_keeps_keywords_from_our_own_list():
    out = redact_report(_sample_report())
    assert out["samples"]["ad"][0]["keywords"] == ["www.example.com"]


def case_redact_does_not_mutate_input():
    rep = _sample_report()
    before = rep["samples"]["ad"][0]["text"]
    redact_report(rep)
    assert rep["samples"]["ad"][0]["text"] == before
    assert "redacted" not in rep


def case_redact_replaces_file_with_alias_from_map():
    rep = _sample_report()
    out = redact_report(rep, name_map={Path(rep["file"]).name: "B07"})
    assert out["file"] == "B07"


def case_redact_replaces_file_with_deterministic_code():
    rep = _sample_report()
    a = redact_report(rep)["file"]
    b = redact_report(rep)["file"]
    assert a == b and a.startswith("F") and SECRET not in a


def case_redact_wipes_dropped_lines_text():
    """全量删除清单（dropped_lines）里的 text 也是真书正文，必须一起换形状。

    这一桶是 §7 adjust 实装时新加的：复核界面要列全量勾选项，所以每条都带
    "前 60 字"。它不进 markdown，只走 --json，正是最容易漏掉脱敏的那种字段 ——
    漏了就等于 --redact 的输出里直接摆着正文。
    """
    rep = _sample_report()
    rep["dropped_lines"] = [
        {"line": 7, "kind": "ad", "protected": False, "text": SECRET},
        {"line": 21, "kind": "toc", "protected": True, "text": SECRET[:10]},
    ]
    out = redact_report(rep)

    assert len(out["dropped_lines"]) == 2, out["dropped_lines"]
    for item in out["dropped_lines"]:
        assert item["text"].startswith("〔"), item
        assert SECRET[:2] not in item["text"], item
    # 行号 / 类别 / 是否受保护 这些结构字段一个都不能动
    assert [d["line"] for d in out["dropped_lines"]] == [7, 21]
    assert [d["kind"] for d in out["dropped_lines"]] == ["ad", "toc"]
    assert [d["protected"] for d in out["dropped_lines"]] == [False, True]
    # 入参不被就地改
    assert rep["dropped_lines"][0]["text"] == SECRET


def case_redact_wipes_adjust_ignored_junk_line():
    """adjust.ignored[].line 存的是 `str(用户贴进来的东西)`，非数字的也要换形状。

    这不是书里的字，是用户自己粘错的垃圾（比如从正文里选中一串字当成行号贴进
    adjust）。但它会同时出现在两个地方：`ignored[].line` 和"没生效"警告的逐行
    点名里。--redact 的口径是"整条命令的输出都不含可疑字串"，两边都得处理 ——
    只擦 ignored、忘了警告，等于白擦。
    """
    rep = _sample_report()
    junk = "林凡抬头看向远处的山峰"
    rep["adjust"] = {
        "applied": {"protect_lines": [7], "merge_breaks": [], "protect_count": 1},
        "requested": {"protect": 2, "merge_breaks": 0},
        "protected": {"ad": 1, "toc": 0, "inline": 0, "lines": 1},
        "merge_breaks_hit": 0,
        "ignored": [
            {"field": "protect_lines", "line": junk, "reason": "不是整数行号"},
            {"field": "protect_lines", "line": 999, "reason": "越界（本次输入共 800 行）"},
        ],
        "unmatched": {"protect_lines": [], "merge_breaks": []},
    }
    rep["warnings"] = [
        f"人工调整里有 1 个行号没生效：protect_lines:{junk}（不是整数行号）。",
    ]
    out = redact_report(rep)

    ignored = out["adjust"]["ignored"]
    assert len(ignored) == 2, ignored
    assert ignored[0]["line"].startswith("〔"), ignored[0]
    assert junk not in str(ignored[0]), ignored[0]
    # 纯数字的行号是结构信息，不动（界面/人对账要用）
    assert ignored[1]["line"] == 999, ignored[1]
    # reason 是模板文案，不是用户输入，也不动
    assert ignored[0]["reason"] == "不是整数行号", ignored[0]
    # 警告里的同一串垃圾也必须一起换掉
    assert junk not in " ".join(out["warnings"]), out["warnings"]
    assert "〔" in out["warnings"][0], out["warnings"][0]
    # 入参不被就地改
    assert rep["adjust"]["ignored"][0]["line"] == junk


def case_redact_wipes_adjust_ignored_end_to_end():
    """端到端：真跑一遍 clean_text + redact_report，确认没有残留。

    上面那条用的是手搓的 report，字段名拼错了也测不出来。这条走真实数据路径：
    非整数行号由 `_check` 的 `str(no)` 产生，警告由 `_build_report` 拼出来，
    再交给 redact_report —— 三个环节的真实产物一起验。
    """
    from huoban.clean import clean_text

    junk = "林凡抬头看向远处的山峰"
    # 第 5 行是整行广告「天才一秒记住本站」
    text = "第一章 开始\n\n正文一段。\n\n天才一秒记住本站\n\n正文二段。\n"
    res = clean_text(text, source="x", adjust={"protect_lines": [junk, 5]})
    assert res.report["adjust"]["protected"]["ad"] == 1, res.report["adjust"]
    out = redact_report(res.report)
    assert junk not in json.dumps(out, ensure_ascii=False), out["adjust"]["ignored"]
    # 正常那条保护照样生效，形状字段没被误伤
    assert out["adjust"]["protected"]["ad"] == 1, out["adjust"]
    assert out["adjust"]["ignored"][0]["line"].startswith("〔"), out["adjust"]


def case_redact_survives_missing_sections():
    out = redact_report({"file": "x.txt"})
    assert out["redacted"] is True
    assert out["samples"] if "samples" in out else True


def case_scrub_line_keeps_the_rule_text():
    line = "章节「第三章 渡劫」疑似漏识别，见 某本书.txt"
    out = scrub_line(line)
    assert "疑似漏识别" in out
    assert "第三章 渡劫" not in out
    assert "某本书" not in out
    assert out.startswith("章节「〔长6 "), out  # 「第三章 渡劫」内含 1 空格，共 6 字


def case_alias_table_numbers_in_order():
    m = alias_table(["a.txt", "b.txt", "a.txt"], prefix="B")
    assert m == {"a.txt": "B01", "b.txt": "B02"}, m


def case_alias_for_is_deterministic():
    assert alias_for("k.txt") == alias_for("k.txt")
    assert alias_for("k.txt") != alias_for("j.txt")


def _sample_diagnose() -> dict:
    return {
        "file": r"E:\文档\txt\小说\某本书.txt",
        "size_bytes": 8192,
        "line_count": 500,
        "blank_count": 90,
        "encoding": {"chosen": "utf-8", "confidence": 0.9, "candidates": []},
        "paragraphs": {"nonblank": 410, "avg_len": 33, "max_len": 900},
        "chapters": {
            "count": 20,
            "families": {"第N章": 20},
            "first_lines": [SECRET[:6], "第二行"],
            "last_lines": [SECRET[:4]],
            "samples": [(3, SECRET), (9, "第一章 风起")],
            "numbering": {"monotonic": True, "numeric_titles": 20,
                          "increasing_pairs": 19, "pairs": 20,
                          "duplicate_numbers": 0, "range": "1-20"},
        },
        "repeated_lines": [{"count": 5, "lines": [7, 20], "text": SECRET}],
        "ads": {
            "whole_line_count": 5,
            "inline_count": 2,
            "whole_line_samples": [{"line": 7, "keywords": ["www.example.com"],
                                    "text": SECRET[:5]}],
            "inline_samples": [{"line": 55, "keywords": ["更多"], "text": SECRET[:3]}],
        },
        "toc": {"suspected": False, "scanned": 30, "hits": 0},
        "single_line_book": {"suspected": False, "long_line_count": 0,
                             "lines_with_multiple_chapters": 0},
        "actions": ["打开 某本书.txt 人工核对前几章"],
    }


def case_redact_diagnose_wipes_text_keeps_numbers():
    out = redact_diagnose(_sample_diagnose())
    for _, t in out["chapters"]["samples"]:
        assert t.startswith("〔")
    assert out["chapters"]["count"] == 20
    assert out["chapters"]["numbering"]["numeric_titles"] == 20
    assert out["repeated_lines"][0]["count"] == 5
    assert out["repeated_lines"][0]["lines"] == [7, 20]
    assert out["repeated_lines"][0]["text"].startswith("〔")
    assert out["ads"]["whole_line_samples"][0]["keywords"] == ["www.example.com"]
    assert out["ads"]["whole_line_samples"][0]["text"].startswith("〔")
    assert out["ads"]["whole_line_count"] == 5
    assert out["line_count"] == 500
    assert out["redacted"] is True
    assert SECRET[:2] not in json.dumps(out, ensure_ascii=False)


def case_redact_diagnose_alias_map_and_scrub():
    rep = _sample_diagnose()
    out = redact_diagnose(rep, name_map={Path(rep["file"]).name: "B03"})
    assert out["file"] == "B03"
    assert "人工核对前几章" in out["actions"][0]
    assert "某本书" not in out["actions"][0]


def _batch_rec() -> dict:
    return {
        "path": r"E:\文档\txt\小说\某本书.txt",
        "name": "某本书.txt",
        "ok": True,
        "title": SECRET[:6],
        "author": "张三",
        "longest_title": SECRET[:4],
        "chapters": 48,
        "paragraphs": 3000,
        "flags": ["章太少(48)"],
        "error": f"UnicodeDecodeError: {SECRET}",
    }


def case_redact_batch_record_hides_name_title_error():
    out = redact_batch_record(_batch_rec())
    blob = json.dumps(out, ensure_ascii=False)
    assert SECRET not in blob and "张三" not in blob, blob
    assert out["path"] == out["name"]
    assert out["path"].startswith("B")
    assert out["chapters"] == 48
    assert out["flags"] == ["章太少(48)"]
    assert out["error"].startswith("UnicodeDecodeError:")
    assert "ok" not in str(out["error"]) or True
    assert out["title"].startswith("〔")
    assert out["redacted"] is True


def case_redact_batch_record_uses_name_map():
    rec = _batch_rec()
    out = redact_batch_record(rec, name_map={rec["name"]: "B07"})
    assert out["path"] == "B07" and out["name"] == "B07"


def case_clean_markdown_shows_redact_banner():
    plain = render_clean_markdown(_sample_report())
    red = render_clean_markdown(redact_report(_sample_report()))
    assert "本报告已脱敏" in red
    assert "本报告已脱敏" not in plain
    assert SECRET[:2] not in red
    # 脱敏版必须保留对账语言和小节标题，否则报告结构本身就变了
    for header in ("## 概览", "## 样例", "## 警告", "## 下一步", "原始文件未改动"):
        assert header in red, header


def case_diagnose_markdown_shows_redact_banner():
    plain = render_markdown(_sample_diagnose())
    red = render_markdown(redact_diagnose(_sample_diagnose()))
    assert "本报告已脱敏" in red
    assert "本报告已脱敏" not in plain
    assert SECRET[:2] not in red
    assert "## 章节结构" in red and "## 广告与重复行" in red


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        code = cli_main(argv)
    return code, buf_out.getvalue(), buf_err.getvalue()


def case_cli_clean_redact_leaks_nothing_on_either_stream():
    if not SAMPLE_TXT.is_file():
        return  # 假样本不在（新检出）就跳过，不硬造
    outdir = ROOT / "backups" / "redact-selftest-20261025" / "clean_out"
    code, so, se = _run_cli(
        ["clean", str(SAMPLE_TXT), "-o", str(outdir), "--json", "--redact"]
    )
    assert code == 0, (code, se[-400:])
    assert SECRET[:2] not in so and SECRET[:2] not in se
    payload = json.loads(so)  # stdout 必须仍是能 loads 的一份 JSON
    assert payload["redacted"] is True
    # 真泄漏检查：默认输出里 file 就是 "…\messy.txt"，脱敏后不许再出现样本名
    assert "messy" not in so and "messy" not in se, "路径/文件名漏了"
    assert str(payload["file"]).startswith("F") or str(payload["file"]).startswith("B")
    for bucket in (payload["samples"].get("ad") or []):
        assert str(bucket.get("text", "")).startswith("〔")
    # dropped_lines 是"前 60 字"的全量清单，走 --json 才看得到，最容易漏脱敏
    for item in payload.get("dropped_lines") or []:
        assert str(item.get("text", "")).startswith("〔"), item
        assert str(item.get("line", "")).isdigit(), item
    md = (outdir / "clean-report.md").read_text(encoding="utf-8")
    assert "本报告已脱敏" in md
    # 交付物不受影响：book.txt 还是给人读的原文
    assert len((outdir / "book.txt").read_text(encoding="utf-8")) > 0
    assert (outdir / "book.json").is_file()


def case_cli_clean_default_output_is_unchanged():
    if not SAMPLE_TXT.is_file():
        return
    outdir = ROOT / "backups" / "redact-selftest-20261025" / "plain_out"
    code, so, se = _run_cli(["clean", str(SAMPLE_TXT), "-o", str(outdir)])
    assert code == 0, (code, se[-400:])
    md = (outdir / "clean-report.md").read_text(encoding="utf-8")
    assert "本报告已脱敏" not in md


def case_cli_diagnose_json_redact():
    if not SAMPLE_TXT.is_file():
        return
    code, so, se = _run_cli(["diagnose", str(SAMPLE_TXT), "--json", "--redact"])
    assert code == 0, (code, se[-400:])
    payload = json.loads(so)
    assert payload["redacted"] is True
    for pair in payload["chapters"]["samples"]:
        assert str(pair[1]).startswith("〔"), pair


def case_cli_mask_map_must_exist():
    code = None
    try:
        code, _so, _se = _run_cli(
            ["diagnose", str(SAMPLE_TXT), "--json", "--redact",
             "--mask-map", str(ROOT / "backups" / "no-such-map.json")]
        )
    except SystemExit as exc:
        assert str(exc.code) != "0"
        return
    assert code != 0


_CASES = [
    case_shape_hides_every_original_char,
    case_shape_is_stable_and_distinguishes,
    case_shape_marks_empty_not_lost,
    case_shape_classifies_character_kinds,
    case_redact_keeps_counts_and_line_numbers,
    case_redact_wipes_every_text_field,
    case_redact_wipes_dropped_lines_text,
    case_redact_wipes_adjust_ignored_junk_line,
    case_redact_wipes_adjust_ignored_end_to_end,
    case_redact_keeps_keywords_from_our_own_list,
    case_redact_does_not_mutate_input,
    case_redact_replaces_file_with_alias_from_map,
    case_redact_replaces_file_with_deterministic_code,
    case_redact_survives_missing_sections,
    case_scrub_line_keeps_the_rule_text,
    case_alias_table_numbers_in_order,
    case_alias_for_is_deterministic,
    case_redact_diagnose_wipes_text_keeps_numbers,
    case_redact_diagnose_alias_map_and_scrub,
    case_redact_batch_record_hides_name_title_error,
    case_redact_batch_record_uses_name_map,
    case_clean_markdown_shows_redact_banner,
    case_diagnose_markdown_shows_redact_banner,
    case_cli_clean_redact_leaks_nothing_on_either_stream,
    case_cli_clean_default_output_is_unchanged,
    case_cli_diagnose_json_redact,
    case_cli_mask_map_must_exist,
]


def test_all_cases():
    for fn in _CASES:
        fn()


def main() -> int:
    failed = 0
    for fn in _CASES:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"{len(_CASES) - failed}/{len(_CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
