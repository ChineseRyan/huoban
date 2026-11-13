"""tools/batch_check.py 的用例：重名标题到底算不算缺陷。

约定沿用 tests/test_clean.py：case_* 函数 + _CASES 列表 + main()。

为什么单测这个：抽检脚本的判据是"要不要人工看"的唯一入口，判错了
要么把合集当故障（噪声九成），要么把真缺陷漏掉。实测 400 本里被标
「重名标题」的 21 本，19 本属于"编号按部重数"的正常形态。
"""

from __future__ import annotations

import importlib.util as _ilu
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SPEC = _ilu.spec_from_file_location("batch_check", str(ROOT / "tools" / "batch_check.py"))
bc = _ilu.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bc)

from huoban.model import Chapter  # noqa: E402


def _vol(title: str, num: int) -> Chapter:
    return Chapter(title=title, number=num, level=2)


def _ch(title: str, num: int) -> Chapter:
    return Chapter(title=title, number=num, level=1)


def _rec(**kw) -> dict:
    """一条"看着像本正常书"的记录，用例只覆盖需要变化的字段。"""
    base = dict(
        ok=True, name="假书", chapters=40, paragraphs=4000, front_matter=0,
        max_chapter_paragraphs=100, front_matter_ratio=0.0, source_lines=5000,
        duplicate_titles=0, duplicate_ratio=0.0, duplicate_same_volume=0,
        duplicate_same_volume_ratio=0.0, volumes=0, number_restarts=0,
        idempotent_diff=0, seconds=1.0,
    )
    base.update(kw)
    return base


def case_duplicate_stats_counts_volume_scope() -> None:
    """_duplicate_stats：跨卷同名不算卷内重名，卷内同名才算。

    实测来源《书庚》（18 卷、编号按部重数）：全书重名很多，
    卷内一个都没有 —— 那种书是正常的。
    """
    chapters = [
        _vol("第一部", 1), _ch("第一章", 1), _ch("第二章", 2),
        _vol("第二部", 2), _ch("第一章", 1), _ch("第二章", 2),
    ]
    dup_all, dup_same, volumes, restarts = bc._duplicate_stats(chapters)
    assert (dup_all, dup_same, volumes) == (2, 0, 2), (dup_all, dup_same, volumes)
    assert restarts == 2, restarts


def case_same_volume_duplicate_is_a_defect() -> None:
    """同一卷里出现两个同名标题 → 记异常（水印/空壳章节的典型形态）。"""
    chapters = [_vol("第一部", 1)]
    chapters += [_ch(f"第{i}章", i) for i in range(1, 9)]
    chapters += [_vol("第二部", 2)]
    chapters += [_ch(f"第{i}章", i) for i in range(1, 9)]
    chapters += [_ch(f"第{i}章", i) for i in range(1, 9)]  # 第二卷整轮重复八条
    dup_all, dup_same, volumes, _ = bc._duplicate_stats(chapters)
    rec = _rec(chapters=len(chapters), volumes=volumes,
               duplicate_titles=dup_all, duplicate_ratio=dup_all / len(chapters),
               duplicate_same_volume=dup_same,
               duplicate_same_volume_ratio=dup_same / len(chapters),
               number_restarts=2)
    assert bc.flags_of(rec) == ["卷内重名31%"], bc.flags_of(rec)
    assert bc.notes_of(rec) == [], bc.notes_of(rec)


def case_cross_volume_duplicates_are_not_flagged() -> None:
    """编号按卷重数（跨卷同名）不是缺陷：不该占用异常名额。"""
    rec = _rec(volumes=3, duplicate_titles=20, duplicate_ratio=0.5,
               duplicate_same_volume=0, duplicate_same_volume_ratio=0.0,
               number_restarts=3)
    assert bc.flags_of(rec) == [], bc.flags_of(rec)
    assert bc.notes_of(rec) == [], bc.notes_of(rec)


def case_no_volume_restarts_is_a_note_not_a_flag() -> None:
    """整本没有卷标记、编号却重启多次 → 合集形态，写提示不算异常。

    实测来源《书辛》（314 章、重名 311）：多部合集，
    原文里根本没有卷标题；按项目约定合集不拆分，清洗结果是对的。
    """
    rec = _rec(chapters=100, volumes=0, duplicate_titles=90, duplicate_ratio=0.9,
               number_restarts=5)
    assert bc.flags_of(rec) == [], bc.flags_of(rec)
    assert bc.notes_of(rec) == ["编号按部重数90%(重启5次)"], bc.notes_of(rec)


def case_duplicate_without_restart_is_a_flag() -> None:
    """没有卷标记、编号也没重启，却有三成以上重名 → 只能是水印/空壳章节。"""
    rec = _rec(volumes=0, duplicate_titles=20, duplicate_ratio=0.5, number_restarts=1)
    assert bc.flags_of(rec) == ["重名标题50%"], bc.flags_of(rec)
    assert bc.notes_of(rec) == [], bc.notes_of(rec)


def case_encoding_suspect_is_flagged_first() -> None:
    """探测只能"硬解"（所有候选都严格解码失败）→ 报编码可疑，且排在症状前面。

    实测来源：换池验证里 16 本 UTF-16LE 无 BOM 的改造语料，detect() 全选
    gb18030 且 strict=False，解出来是乱码；旧版记录只会显示「0章 / 章太少 /
    重名标题」，看起来像"书没章节标记"，其实是"解错了"，处置完全不同。
    """
    rec = _rec(encoding="gb18030", encoding_strict=False)
    flags = bc.flags_of(rec)
    assert flags[0] == "编码可疑(gb18030)", flags


def case_oneline_book_is_flagged() -> None:
    """整本糊成一行 → 除了「0章」还要点明「整本一行」。

    两者必须分开：短篇集/本来就没有章节标记的书也是 0 章，但处置完全不同
    —— 前者要重切（M4 或人工），后者按项目约定原样保留。
    """
    rec = _rec(chapters=0, paragraphs=100, front_matter=100, front_matter_ratio=1.0,
               source_lines=3000, oneline_suspected=True, oneline_long_lines=1)
    flags = bc.flags_of(rec)
    assert flags[:2] == ["0章", "整本一行"], flags


def case_suspected_but_no_long_line_is_not_oneline() -> None:
    """只"疑似"（一行里有多个章节编号）但**没有**超长行 → 不许报整本一行。

    实测来源：no_chapter 改造语料里的 B06，10939 行正常长度的行，只是正文引用了
    两个章节号，clean 的 oneline.suspected 就为真；基线 book05（正常书）同样为真。
    拿 suspected 当判据会把「无章节标记的书」误标成「整本一行」，处置就错了。
    """
    rec = _rec(chapters=0, paragraphs=100, front_matter=100, front_matter_ratio=1.0,
               source_lines=3000, oneline_suspected=True, oneline_long_lines=0)
    assert "整本一行" not in bc.flags_of(rec), bc.flags_of(rec)
    # 有章节、但有超长行的书，另报一档，不混进 0 章那一档
    rec2 = _rec(chapters=40, oneline_suspected=True, oneline_long_lines=3)
    assert "超长行3" in bc.flags_of(rec2), bc.flags_of(rec2)
    assert "整本一行" not in bc.flags_of(rec2), bc.flags_of(rec2)


def _fake_book() -> str:
    """合成假书：5 章、每章 4 段，全用填充字，不含任何真书正文。

    段长 200 字是刻意的：拼成一行后总长 >3000 字，才会被 clean 判成「超长行」
    （`whole_book_in_one_line` 的门槛是 3000 字符）。段太短的话，整本一行版
    根本不会被识别，用例就成了假绿。
    """
    fill = ("木山水石田土云雨风光星河海湖桥路园墙门帘盘碗筷鱼肉蛋盐糖茶饼"
            "瓜果梨桃李杏枣豆角莓芝兰竹松柏草芽根茎皮")
    cn = "零一二三四五六七八九"
    out: list[str] = []
    for c in range(1, 6):
        out.append(f"第{cn[c]}章　假题{c}")
        for p in range(4):
            body = "".join(fill[(c * 7 + p * 13 + i * 5) % len(fill)] for i in range(200))
            out.append("　　" + body + "。")
        out.append("")
    return "\n".join(out) + "\n"


def case_check_one_detects_encoding_and_oneline() -> None:
    """端到端：合成假数据走完整条 check_one，验证两条新判据真的接上了。

    为什么要有这条：前面两条只喂现成的 rec，判据写对了但 check_one 忘了把
    字段填进去，照样是假绿 —— 那正是这次换池验证暴露的坑（旧版对"解错了"
    和"没章节标记"输出一模一样）。这里用真文件跑一遍。
    """
    import tempfile

    text = _fake_book()
    d = Path(tempfile.mkdtemp(prefix="huoban-bc-"))

    plain = d / "fake_utf8.txt"
    plain.write_bytes(text.encode("utf-8"))
    rec = bc.check_one(plain)
    assert rec["ok"], rec.get("error")
    assert rec["chapters"] >= 1, rec["chapters"]
    assert not any("编码可疑" in f for f in bc.flags_of(rec)), bc.flags_of(rec)
    assert "整本一行" not in bc.flags_of(rec), bc.flags_of(rec)

    u16 = d / "fake_utf16le.txt"
    u16.write_bytes(text.encode("utf-16-le"))
    rec16 = bc.check_one(u16)
    assert rec16["ok"], rec16.get("error")
    # task-6 修复后的行为：UTF-16LE 无 BOM 被 detect 正确选中（strict=True），
    # 章数应与 utf-8 版一致 —— 这条同时钉住"编码可疑"判据不再有假阳性
    assert rec16["encoding_strict"] is True, rec16.get("encoding")
    assert not any(f.startswith("编码可疑") for f in bc.flags_of(rec16)), bc.flags_of(rec16)
    assert rec16["chapters"] == rec["chapters"], (rec16["chapters"], rec["chapters"])

    one = d / "fake_oneline.txt"
    one.write_bytes(
        ("".join(ln.strip() for ln in text.splitlines() if ln.strip()) + "\n").encode("utf-8")
    )
    rec1 = bc.check_one(one)
    assert rec1["ok"], rec1.get("error")
    assert "整本一行" in bc.flags_of(rec1), bc.flags_of(rec1)


_CASES = [
    case_duplicate_stats_counts_volume_scope,
    case_same_volume_duplicate_is_a_defect,
    case_cross_volume_duplicates_are_not_flagged,
    case_no_volume_restarts_is_a_note_not_a_flag,
    case_duplicate_without_restart_is_a_flag,
    case_encoding_suspect_is_flagged_first,
    case_oneline_book_is_flagged,
    case_suspected_but_no_long_line_is_not_oneline,
    case_check_one_detects_encoding_and_oneline,
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
