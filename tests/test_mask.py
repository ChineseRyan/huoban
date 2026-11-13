"""tools/mask.py 的用例：影子语料的同构性保证。

约定沿用 tests/test_clean.py：case_* 函数 + _CASES 列表 + main()。
数据全部当场手搓（不用 samples/ 里的文件，也不用任何真书）——
这个测试本身也必须"零原文"，否则它就变成了又一条泄漏渠道。
"""

from __future__ import annotations

import importlib.util as _ilu
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SPEC = _ilu.spec_from_file_location("mask", str(ROOT / "tools" / "mask.py"))
mask = _ilu.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mask)

from huoban.clean import clean_text  # noqa: E402
from huoban.diagnose import AD_KEYWORDS  # noqa: E402

# 手搓的假正文：一句普通叙述 + 一句和它一字不差的重复（测"重复行照样重复"）
BODY = "林江夜里提着灯笼走过石桥，水声很响"
LINE_AD = "请记住本站 www.example-novel.com，最新章节第一时间发布"
LINE_HEAD = "第十二章 夜过石桥"
LINE_NUM = "12、夜过石桥"
LINE_PLAIN = "他站了很久，没有说话。"
LINE_TOC = "目录"


def _doc() -> str:
    return "\n".join([LINE_HEAD, "", BODY, BODY, LINE_AD, LINE_TOC, LINE_NUM, LINE_PLAIN])


def case_length_and_lines_kept() -> None:
    """孪生本必须逐行对齐：行数、每行长度、行分隔符全不变。

    长度一变，行长分布就变了，清洗看到的"形态"就不是同一本书。
    """
    src = _doc().replace("\n", "\r\n")  # \r\n 的书不能因为掩码变成 \n 的书
    out = mask.mask_text(src)
    assert len(out) == len(src), (len(out), len(src))
    assert out.count("\r\n") == src.count("\r\n")
    assert len(out.split("\r\n")) == len(src.split("\r\n"))


def case_only_cjk_changes() -> None:
    """只有汉字被换；西文字母、数字、标点、空白一个都不动。

    这是整份影子语料的安全底线：一旦动了 ASCII，替换就可能自己拼出
    "www."、".com"、"1234.com" 这种假域名 —— 孪生本凭空多出广告行，
    同构性作废，而且比原书"更脏"，调试结论全错。
    """
    for line in (BODY, LINE_AD, LINE_HEAD, LINE_NUM, LINE_PLAIN):
        got = mask.mask_line(line)
        kept = [c for c in line if not mask._is_cjk(c)]
        got_ascii = [c for c in got if not mask._is_cjk(c)]
        assert kept == got_ascii, (line, got)
        assert len(got) == len(line)


def case_body_text_is_gone() -> None:
    """正文里的字（按二元组看）必须消失：这是"脱敏"这件事本身。

    刻意不逐字断言 —— 单个汉字合法地留在结构标记里（"第十二章"的"第"，
    填充表里的"桥"），逐字判会误报。真正要消失的是**连续的字串**。
    """
    out = mask.mask_text(_doc())
    for frag in ("林江", "提着", "了灯笼", "夜里", "水声很响", "他站了很久"):
        assert frag not in out, frag


def case_markers_survive() -> None:
    """结构标记原样保留：规则要靠它们才判得出章节与广告行。

    断言的是标记片段，不是整行 —— 一行标题里"第十二章"是标记、
    后面的题名是正文，正文部分本来就该被替换掉。
    """
    out = mask.mask_text(_doc())
    assert "第十二章" in out
    assert out.count("12、") == 1, out
    assert "www.example-novel.com" in out
    assert "请记住本站" in out
    assert "目录" in out  # clean.py 用它定位目录区，抹掉就少删目录行
    # 「最新章节」不在任何词表里，它是正文，本来就该被替换掉
    assert "最新章节" not in out


def case_no_fake_ad_or_heading() -> None:
    """替换不许凭空造出任何广告关键词或章节标题 —— 反向检查最重要的一条。

    做法：拿一行不含任何关键词的纯正文，掩码后不许出现任何关键词，
    也不许出现"第X章"式标题。
    """
    plain = "他站了很久，没有说话。"
    assert not any(k in plain for k in AD_KEYWORDS)
    got = mask.mask_line(plain)
    assert not any(k in got for k in AD_KEYWORDS), got
    assert not re.search(r"第[0-9〇零一二三四五六七八九十百千万两]+[章节卷部集回篇]", got), got


def case_duplicates_stay_duplicates() -> None:
    """同一行永远得到同一个孪生行：原书的重复行在孪生本里仍要重复。

    重复行占比是体检的一个指标，随机替换会把它抹成 0。
    """
    assert mask.mask_line(BODY) == mask.mask_line(BODY)
    out = mask.mask_text(_doc())
    lines = [ln for ln in out.split("\n") if ln]
    dup = {ln for ln in lines if lines.count(ln) >= 2}
    assert len(dup) == 1, dup
    assert mask.mask_line(BODY) in dup


def case_filler_table_is_clean() -> None:
    """填充字表不许混进结构/广告标记用字（混进去时 _filler_ok 必须拦下）。"""
    mask._filler_ok()  # 不抛异常即通过
    bad = set(mask._FILLER) & mask._forbidden()
    assert not bad, "".join(sorted(bad))
    tampered = mask._FILLER + "第章"
    saved = mask._FILLER
    try:
        mask._FILLER = tampered
        try:
            mask._filler_ok()
        except SystemExit:
            return
        raise AssertionError("混进禁用字时 _filler_ok 没有拦下")
    finally:
        mask._FILLER = saved


def case_counts_are_isomorphic() -> None:
    """真本 vs 孪生本：章节数、广告行数、重复行计数必须一致。

    这是整份工具能不能替代真本下结论的判据，也是 check 子命令的迷你版。
    """
    src = "\n".join([
        "书名：夜过石桥",
        "作者：某某",
        "",
        "www.example-novel.com",
        "请记住本站，每天第一时间更新",
        "目录",
        "1、第一章 夜行",
        "2、第二章 石桥",
        "",
        LINE_HEAD,
        BODY,
        BODY,
        LINE_AD,
        "第十三章 灯灭了",
        "他站了很久，没有说话。",
        "第十四章 回程",
        "他站了很久，没有说话。",
    ])
    a = clean_text(src, source="real").report
    b = clean_text(mask.mask_text(src), source="twin").report
    flat_a, flat_b = mask._report_counts(a), mask._report_counts(b)
    keys = sorted(set(flat_a) & set(flat_b))
    assert keys, "两个报告没有可比字段"
    diff = [k for k in keys if flat_a[k] != flat_b[k]]
    # 只允许正文长度类字段有偏差（填充字与原字长度相同，理论上不该有）；
    # 章节/删除/样例计数必须严格相等。
    for k in diff:
        assert not k.startswith(("stats.chapters", "dropped.", "samples.", "chapters.")), (
            k, flat_a[k], flat_b[k])
    assert a["stats"]["chapters"] == b["stats"]["chapters"], (
        a["stats"]["chapters"], b["stats"]["chapters"])
    assert a["dropped"]["ad"] == b["dropped"]["ad"], (a["dropped"]["ad"], b["dropped"]["ad"])
    assert a["stats"]["chars"] == b["stats"]["chars"]
    assert a["lines"]["total"] == b["lines"]["total"]
    assert a["lines"]["nonblank"] == b["lines"]["nonblank"]


def case_report_counts_has_no_text() -> None:
    """_report_counts 的输出不许带任何原文：这份东西要能安全贴进会话。

    做法：喂一个每个文本键都塞满可识别暗号的报告，断言暗号一个都不出现，
    且值全是数字/布尔。
    """
    secret = "暗号金丝小枣"
    rep = {
        "file": f"{secret}.txt",
        "warnings": [f"发现 {secret}"],
        "stats": {"chapters": 3, "longest_title": secret},
        "samples": {"ad": [{"line": 1, "text": secret, "keywords": [secret]}]},
        "dropped": {"ad": 2},
    }
    flat = mask._report_counts(rep)
    assert flat, "一个字段都没取到"
    blob = " ".join(str(v) for v in flat.values()) + " " + " ".join(flat)
    assert secret not in blob
    assert all(isinstance(v, (int, float)) for v in flat.values()), flat
    assert flat["dropped.ad"] == 2
    assert flat["stats.chapters"] == 3
    assert flat["samples.ad[0].line"] == 1
    assert flat["samples.ad.len"] == 1


_CASES = [
    case_length_and_lines_kept,
    case_only_cjk_changes,
    case_body_text_is_gone,
    case_markers_survive,
    case_no_fake_ad_or_heading,
    case_duplicates_stay_duplicates,
    case_filler_table_is_clean,
    case_counts_are_isomorphic,
    case_report_counts_has_no_text,
]


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
