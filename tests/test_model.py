"""model.py 的测试：章节树 JSON 中间态。

用假数据覆盖：往返序列化、卷章两级、序号重复/缺失的校验、中英文混排字数统计、
空书、读回 JSON 再写出一致。只断言"结构对不对"，不断言错误措辞。

跑法：python tests/test_model.py（不依赖 pytest）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.model import (  # noqa: E402
    SCHEMA_VERSION,
    Book,
    Chapter,
    Paragraph,
    chapter_from_title,
    from_dict,
    load,
    parse_heading,
    save,
    stats,
    to_dict,
    validate,
)


def _tmp_dir() -> Path:
    """测试产物目录固定在项目 backups/ 下：规则要求临时文件不落项目外。

    用固定目录而不是 mkdtemp()：本项目的规则禁止删除文件，每次跑都新建
    一个 mkdtemp 目录会一直堆积。固定目录每次覆盖，跑一百次也只占一份。
    """
    d = ROOT / "backups" / "test-model-tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sample_book() -> Book:
    """一本结构齐全的假书：前言 + 卷章两级 + 后记，段落类型都有。"""
    return Book(
        title="测试之书",
        author="佚名",
        encoding="gb18030",
        source="samples/messy_gbk.txt",
        front_matter=[
            Paragraph(text="本书由某某整理，仅供试读。", kind="note"),
            Paragraph(text="", kind="blank"),
        ],
        chapters=[
            Chapter(
                title="第一卷 风起",
                number=1,
                level=2,
                start_line=10,
                paragraphs=[Paragraph(text="")],
            ),
            Chapter(
                title="第一章 开始",
                number=1,
                level=1,
                start_line=12,
                paragraphs=[
                    Paragraph(text="他走了三天。", kind="body"),
                    Paragraph(text="", kind="blank"),
                    Paragraph(text="第一章 开始", kind="heading"),
                ],
            ),
            Chapter(
                title="第二章 结束",
                number=2,
                level=1,
                start_line=30,
                paragraphs=[Paragraph(text="风停了。", kind="body")],
            ),
        ],
        tail_matter=[Paragraph(text="全书完。", kind="note")],
    )


def case_roundtrip() -> None:
    """to_dict → JSON → from_dict 必须一字不差地还原。"""
    book = _sample_book()
    d = to_dict(book)
    assert d["version"] == SCHEMA_VERSION, d["version"]
    assert set(d) == {
        "version", "title", "author", "encoding", "source",
        "front_matter", "chapters", "tail_matter",
    }, sorted(d)

    # 经过真正的 JSON 文本往返，而不是只过一遍 dict
    back = from_dict(json.loads(json.dumps(d, ensure_ascii=False)))
    assert back == book, "往返后有字段丢失或被改动"

    # 未解析出序号的章节要保住 None，不能变成 0
    b2 = Book(chapters=[Chapter(title="楔子", number=None)])
    assert from_dict(to_dict(b2)).chapters[0].number is None

    # 段落允许写成纯字符串（手写 JSON 的常见写法）
    loose = from_dict({"version": "1", "chapters": [{"title": "第一章", "paragraphs": ["正文一"]}]})
    assert loose.chapters[0].paragraphs[0] == Paragraph(text="正文一")
    assert loose.chapters[0].paragraphs[0].kind == "body"


def case_two_levels() -> None:
    """卷/部/集是 level 2，章/回/节是 level 1；序号复用 diagnose.cn_to_int()。"""
    cases = [
        ("第一卷 风起", 1, 2),
        ("第 2 部 云涌", 2, 2),
        ("第三集", 3, 2),
        ("第一章 开始", 1, 1),
        ("第十二章 夜行", 12, 1),
        ("第一百二十三章", 123, 1),
        ("第3回 惊变", 3, 1),
        ("第 ０９ 节", 9, 1),
        ("Chapter 7", 7, 1),
        ("  42  ", 42, 1),
        ("## 第五章 井", 5, 1),
        ("楔子", None, 1),
        ("番外 旧事", None, 1),
    ]
    for line, number, level in cases:
        got = parse_heading(line)
        assert got is not None, f"{line!r} 应该被认成标题"
        assert (got.number, got.level) == (number, level), (line, got)

    # 正文行必须返回"没判定"，而不是瞎猜一个层级
    for line in ("他走了三天。", "", "   ", "第", "这里说的是第一卷的内容"):
        assert parse_heading(line) is None, line

    ch = chapter_from_title("第十二章 夜行", start_line=88)
    assert (ch.number, ch.level, ch.start_line) == (12, 1, 88), ch


def case_validate_problems() -> None:
    """序号重复、序号倒退、标题为空、段落全空、没记录编码都要报出来。"""
    book = Book(
        encoding="",  # 故意漏掉
        chapters=[
            Chapter(title="第一章", number=1, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第二章", number=2, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第二章（重复）", number=2, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第五章", number=5, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第三章", number=3, paragraphs=[Paragraph(text="正文")]),  # 倒退
            Chapter(title="", number=6, paragraphs=[Paragraph(text="")]),  # 标题空 + 段落全空
            Chapter(title="第七章", number=7, paragraphs=[Paragraph(text="正文", kind="ad")]),  # 类型越界
        ],
    )
    issues = validate(book)
    joined = "\n".join(issues)
    assert any("encoding" in s for s in issues), joined
    assert any("标题为空" in s for s in issues), joined
    assert any("段落全空" in s for s in issues), joined
    assert any("序号重复" in s for s in issues), joined
    assert any("契约外的段落类型" in s for s in issues), joined
    assert any("非递增" in s for s in issues), joined
    # 与章节有关的问题都要能核对：必须点名"第几个章节"
    # （encoding 那条不是章节问题，本来就不该带章节号）
    assert all("第 " in s for s in issues if "encoding" not in s), joined


def case_validate_missing_number() -> None:
    """序号缺失（None）不是问题，不该被当成 0 或报重复；健康书返回空清单。"""
    book = Book(
        encoding="utf-8",
        chapters=[
            Chapter(title="楔子", number=None, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第一章", number=1, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="番外", number=None, paragraphs=[Paragraph(text="正文")]),
            Chapter(title="第二章", number=2, paragraphs=[Paragraph(text="正文")]),
        ],
    )
    assert validate(book) == [], validate(book)

    # 空书：必须同时提醒"没章节"和"没编码"
    empty_issues = validate(Book())
    assert any("没有任何章节" in s for s in empty_issues), empty_issues
    assert any("encoding" in s for s in empty_issues), empty_issues


def case_validate_volume_restart() -> None:
    """每卷章号从 1 重新开始，不能误报成"序号倒退"。"""
    chapters = []
    for vol in (1, 2):
        chapters.append(Chapter(title=f"第{vol}卷", number=vol, level=2, paragraphs=[Paragraph(text="卷首")]))
        for n in (1, 2, 3):
            chapters.append(
                Chapter(title=f"第{vol}卷第{n}章", number=n, level=1, paragraphs=[Paragraph(text="正文")])
            )
    assert validate(Book(encoding="utf-8", chapters=chapters)) == [], validate(
        Book(encoding="utf-8", chapters=chapters)
    )


def case_validate_volume_may_be_empty() -> None:
    """卷/部标题本身没有段落是正常的（正文在下面各章里），不能报"正文丢了"。

    level=2 是容器：渲染层会把它排成一个小标题，它后面跟着的章才是内容。
    每本分卷的书都报这条错误，等于让警告失去意义。
    """
    chapters = [
        Chapter(title="第一卷 风起", number=1, level=2, paragraphs=[]),
        Chapter(title="第一章 起始", number=1, paragraphs=[Paragraph(text="正文")]),
        Chapter(title="第二章 相遇", number=2, paragraphs=[Paragraph(text="正文")]),
        Chapter(title="第二卷 云涌", number=2, level=2, paragraphs=[]),
        Chapter(title="第三章 别离", number=3, paragraphs=[Paragraph(text="正文")]),
    ]
    issues = validate(Book(encoding="utf-8", chapters=chapters))
    assert issues == [], issues

    # 但 level=1 的章还是不许空 —— 豁免只给容器标题，别把闸整个拆了
    broken = Book(
        encoding="utf-8",
        chapters=[
            Chapter(title="第一章 起始", number=1, paragraphs=[]),
        ],
    )
    broken_issues = validate(broken)
    assert any("一个段落都没有" in s for s in broken_issues), broken_issues


def case_validate_duplicate_volume_titles() -> None:
    """两卷都写成"第1卷"（盗版合并本常见）时，各卷的章不能混成一组误报重复。"""
    chapters = []
    for _ in range(2):
        chapters.append(Chapter(title="第1卷", number=1, level=2, paragraphs=[Paragraph(text="卷首")]))
        for n in (1, 2):
            chapters.append(
                Chapter(title=f"第{n}章", number=n, level=1, paragraphs=[Paragraph(text="正文")])
            )
    issues = validate(Book(encoding="utf-8", chapters=chapters))
    # 卷序号确实重复了，这一条该报 —— 说明分组没有把两卷的章混在一起
    assert len(issues) == 1, issues
    assert "章节序号重复" in issues[0] and "卷/部/集标题" in issues[0], issues
    # 各卷内部的章 1、2 递增，不许出现任何误报
    assert not any("非递增" in s for s in issues), issues


def case_anchor_refs_roundtrip() -> None:
    """anchor 与 refs 只在非空时才写进 JSON（v1 旧文件重新序列化必须逐字节不变）。"""
    para = Paragraph(text="正文继续。", refs=[{"at": 2, "note": "n1", "label": "[1]"}],
                     anchor="nr1")
    note = Paragraph(text="注一。", kind="note", anchor="n1")
    plain = Paragraph(text="普通段。")
    book = Book(title="书", chapters=[Chapter(title="第一章", number=1, level=1,
                                              start_line=1, paragraphs=[para, note, plain])])
    data = to_dict(book)
    p0, p1, p2 = data["chapters"][0]["paragraphs"]
    assert p0["anchor"] == "nr1" and p0["refs"][0]["note"] == "n1", p0
    assert p1["anchor"] == "n1" and "refs" not in p1, p1
    assert "anchor" not in p2 and "refs" not in p2, p2

    back = from_dict(data)
    got = back.chapters[0].paragraphs
    assert got[0].anchor == "nr1" and got[0].refs == [{"at": 2, "note": "n1", "label": "[1]"}]
    assert got[1].anchor == "n1" and got[1].refs == []
    assert got[2].anchor == "" and got[2].refs == []
    # 再序列化一次必须与第一次逐字节相同
    assert json.dumps(to_dict(back), ensure_ascii=False) == json.dumps(data, ensure_ascii=False)


def case_validate_refs() -> None:
    """锚点/引用里的 id 会原样拼进 xhtml 属性，必须过白名单校验。"""
    bad_anchor = Book(encoding="utf-8", chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                                        paragraphs=[Paragraph(text="正文。", anchor='a"b')])])
    assert any("anchor" in msg for msg in validate(bad_anchor)), validate(bad_anchor)

    bad_note = Book(encoding="utf-8", chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                                      paragraphs=[Paragraph(text="正文。",
                                                            refs=[{"at": 0, "note": "x y", "label": "[1]"}])])])
    assert any("行内引用" in msg for msg in validate(bad_note)), validate(bad_note)

    bad_at = Book(encoding="utf-8", chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                                    paragraphs=[Paragraph(text="正文。",
                                                          refs=[{"at": 99, "note": "n1", "label": "[1]"}])])])
    assert any("行内引用" in msg for msg in validate(bad_at)), validate(bad_at)

    ok = Book(encoding="utf-8", chapters=[Chapter(title="一", number=1, level=1, start_line=1,
                                paragraphs=[Paragraph(text="正文。",
                                                      refs=[{"at": 2, "note": "n1", "label": "[1]"}],
                                                      anchor="nr1"),
                                            Paragraph(text="注。", kind="note", anchor="n1")])])
    assert validate(ok) == [], validate(ok)


def case_stats() -> None:
    """字数只数汉字与字母数字：中英混排、标点空白都不算。"""
    assert stats(Book()) == {
        "chapters": 0,
        "paragraphs": 0,
        "chars": 0,
        "avg_chars_per_chapter": 0.0,
        "longest_title": "",
    }, stats(Book())

    book = Book(
        encoding="utf-8",
        front_matter=[Paragraph(text="序言abc")],  # 2 + 3 = 5
        chapters=[
            Chapter(
                title="第一章 开始",
                number=1,
                paragraphs=[Paragraph(text="他走了三天abc123，路很远。")],  # 5 + 3 + 3 + 3 = 14
            ),
            Chapter(
                title="第二章 结束以及一个很长很长很长的尾巴",
                number=2,
                paragraphs=[Paragraph(text="第二段的字。")],  # 5
            ),
        ],
        tail_matter=[Paragraph(text="全书完")],  # 3
    )
    s = stats(book)
    assert s["chapters"] == 2, s
    assert s["paragraphs"] == 4, s          # 前言 1 + 章内 2 + 后记 1
    assert s["chars"] == 5 + 14 + 5 + 3, s
    # 平均每章字数只算章节正文，不把前言后记摊进去
    assert s["avg_chars_per_chapter"] == round((14 + 5) / 2, 1), s
    assert s["longest_title"] == "第二章 结束以及一个很长很长很长的尾巴", s


def case_save_load_identical() -> None:
    """存盘 → 读回 → 再存盘，两次文件内容必须一致（稳定结构）。"""
    book = _sample_book()
    d = _tmp_dir()
    p1 = d / "book.json"
    p2 = d / "sub" / "book2.json"  # 父目录不存在也要能写

    save(book, p1)
    text1 = p1.read_text(encoding="utf-8")
    assert "第一章 开始" in text1, "中文不该被转义成 \\uXXXX"
    assert f'"version": "{SCHEMA_VERSION}"' in text1, text1[:200]

    back = load(p1)
    assert back == book, "存盘读回后结构变了"

    save(back, p2)
    assert p2.read_text(encoding="utf-8") == text1, "读回再写出，文件内容不一致"


def case_version_guard() -> None:
    """主版本号不认识时必须报错，不能按旧语义读出一本错位的书。

    v2 相对 v1 只是 Paragraph 多了三个默认字段（image 段专用），旧文件按 v2
    读入无损 —— 所以 1 和 2 都要接受；其余主版本仍然拒绝。
    """
    for good in ("1", "2", "2.0", "1.5.9-rc"):
        ok = from_dict({"version": good, "chapters": []})
        assert isinstance(ok, Book), good
    for bad in ("0", "3", "9.0"):
        try:
            from_dict({"version": bad, "chapters": []})
        except ValueError:
            continue
        raise AssertionError(f"版本 {bad} 应该被拒绝")


def case_framed_heading_keeps_its_number() -> None:
    """剥边框后不能只认家族不抽号：Heading.number 必须跟着回来。

    家族由 match_chapter 认、序号由 parse_heading 里的 _HEADING_NUM 抽，
    两处若有一处没走 strip_frame，目录编号检查就会整族失真。
    """
    h = parse_heading("========== 第001章 ==========")
    assert h is not None
    assert h.family == "第N章类", h
    assert h.number == 1, h
    assert h.level == 1, h
    assert parse_heading("==========").family == "分隔线"


def case_v1_tree_reserializes_unchanged() -> None:
    """docs/gui-design.md 的「EPUB 导入」一节验收要求：旧 v1 章节树读入后重新序列化，**除 version 外**逐字节不变。

    为什么不是"逐字节"：schema 升到 2 了，to_dict 写出的 version 字段必须是
    新的（否则下次读入会以为这是 v1 树）。除这一处以外一个字节都不许动 ——
    这是"schema 升级对老文件零影响"的可执行证明。
    """
    v1 = {
        "version": "1",
        "title": "旧书",
        "author": "某人",
        "encoding": "utf-8",
        "source": "X:/样本/旧书.txt",
        "front_matter": [
            {"text": "前言一段。", "kind": "note"},
            {"text": "", "kind": "blank"},
        ],
        "chapters": [
            {
                "title": "第一卷",
                "number": 1,
                "level": 2,
                "start_line": 0,
                "paragraphs": [],
            },
            {
                "title": "第一章 开端",
                "number": 1,
                "level": 1,
                "start_line": 10,
                "paragraphs": [
                    {"text": "正文一。", "kind": "body"},
                    {"text": "＊＊＊", "kind": "sep"},
                    {"text": "词曰。", "kind": "verse"},
                ],
            },
        ],
        "tail_matter": [],
    }
    text_in = json.dumps(v1, ensure_ascii=False, indent=2) + "\n"
    book = from_dict(json.loads(text_in))
    d = _tmp_dir()
    p = d / "v1-book.json"
    save(book, p)
    text_out = p.read_text(encoding="utf-8")

    # 只允许 version 那一行不同：把两边都换成占位再比
    patched_in = text_in.replace('"version": "1"', '"version": "<V>"')
    patched_out = text_out.replace(f'"version": "{SCHEMA_VERSION}"', '"version": "<V>"')
    assert patched_in == patched_out, "旧 v1 树重新序列化后除 version 外应该逐字节不变"

    # 读回来的结构也一模一样（含空卷、number、start_line）
    assert to_dict(book)["chapters"] == v1["chapters"]
    assert to_dict(book)["front_matter"] == v1["front_matter"]


# ---------- pytest 兼容层 ----------
# 用例本体统一叫 case_*（照 tools/smoke_test.py 的风格，脚本直跑时按顺序执行）。
# 但本机恰好装了 pytest，验收要求 "python -m pytest" 也要全绿，
# 而 pytest 只收集 test_* 开头的名字，所以下面给每条用例挂一个薄壳。
# 没装 pytest 时，这几个函数只是没人调用的普通函数，不影响 "python tests/test_model.py"。


def test_roundtrip() -> None:
    case_roundtrip()


def test_two_levels() -> None:
    case_two_levels()


def test_validate_problems() -> None:
    case_validate_problems()


def test_validate_missing_number() -> None:
    case_validate_missing_number()


def test_validate_volume_restart() -> None:
    case_validate_volume_restart()


def test_validate_volume_may_be_empty() -> None:
    case_validate_volume_may_be_empty()


def test_validate_duplicate_volume_titles() -> None:
    case_validate_duplicate_volume_titles()


def test_stats() -> None:
    case_stats()


def test_save_load_identical() -> None:
    case_save_load_identical()


def test_version_guard() -> None:
    case_version_guard()


def test_v1_tree_reserializes_unchanged() -> None:
    case_v1_tree_reserializes_unchanged()


def main() -> int:
    cases = [
        case_roundtrip,
        case_two_levels,
        case_validate_problems,
        case_validate_missing_number,
        case_validate_volume_restart,
        case_validate_volume_may_be_empty,
        case_validate_duplicate_volume_titles,
        case_anchor_refs_roundtrip,
        case_validate_refs,
        case_stats,
        case_save_load_identical,
        case_version_guard,
        case_v1_tree_reserializes_unchanged,
        case_framed_heading_keeps_its_number,
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
