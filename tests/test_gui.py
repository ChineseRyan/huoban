"""GUI 层的单元测试：不依赖 webview 窗口，只测纯数据的那部分。

`gui/api.py` 里真正需要窗口的只有 pick_file / pick_cover / preview_cover，
其余（safe_name、_summary、_book_meta、复核页的五类分组/分页/点开看原文、
apply_adjust 的形状校验）都能在无窗口环境下直接跑 —— 这几处正是最容易悄悄
改坏的地方，所以必须有用例钉住。

跑法：`python tests/test_gui.py`（`pytest` 收集不到这些用例，只能直跑）。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.api import (  # noqa: E402
    Api,
    InputError,
    Job,
    Session,
    REVIEW_PAGE,
    _ALERT_BRIEF,
    _book_glyphs,
    _book_meta,
    _detail,
    _detail_chap,
    _rows_chap,
    _epub_summary,
    _groups_of,
    _review_payload,
    safe_name,
)
from huoban.clean import clean_text  # noqa: E402

# 一份"什么都有"的假数据：整行广告 + 目录条目 + 碎行合并 + 行内广告。
SAMPLE = """目录

第一章　启程　……　1
第二章　归途　……　9
第三章　重逢　……　21

第一章　启程

正文一段。

天才一秒记住本站

正文二段没有句号
接上去的后半句

第二章　归途

正文三段。

第三章　重逢

正文四段。
"""


def _report(text: str = SAMPLE, *, adjust: dict | None = None) -> dict:
    return clean_text(text, source="假书.txt", adjust=adjust).report


# ---------- safe_name ----------


def case_safe_name_keeps_chinese_and_spaces():
    """产物目录是给人看的：中文、空格、括号一律保留，只换真正非法的字符。"""
    assert safe_name("书E") == "书E"
    assert safe_name("The Great Gatsby") == "The Great Gatsby"
    assert safe_name("第一卷（上）") == "第一卷（上）"


def case_safe_name_replaces_illegal_chars():
    """Windows 文件名里不能出现 / : * ? " < > |，书名从正文里猜，可能带这些。"""
    assert safe_name('书名:副标题/其一*问号?"尖括号"') == "书名_副标题_其一_问号__尖括号_"


def case_safe_name_handles_reserved_names():
    """CON/PRN/NUL 这类保留名当目录名会直接失败，加下划线绕开。"""
    assert safe_name("CON") == "CON_"
    assert safe_name("con") == "con_"
    assert safe_name("com1") == "com1_"
    assert safe_name("COM0") == "COM0"   # 只有 1-9 是保留名，COM0 不是


def case_safe_name_empty_falls_back():
    """书名可能是空的（正文里猜不到、文件名也没有），要有兜底。"""
    assert safe_name("") == "未命名"
    assert safe_name("   ") == "未命名"
    assert safe_name("...") == "未命名"
    assert safe_name("", fallback="书") == "书"


def case_safe_name_truncates_long_title():
    """超长书名会让路径超 Windows 限制，截到 80 字符。"""
    long = "书" * 200
    out = safe_name(long)
    assert len(out) == 80, len(out)


# ---------- _book_meta ----------


def case_book_meta_paragraphs_match_epub_side():
    """④ 与 ⑤ 两页的「段」必须是同一个口径。

    实测踩过：④ 显示 24 段、⑤ 显示 14 段，用户以为丢了内容。原因是
    model.stats 数段落对象（含空行占位段），epub 那边只数写进 XHTML 的
    <p>，空行折叠成 <div class="sep"> 不计。两处都必须用 model.stats。
    """
    result = clean_text(SAMPLE, source="假书.txt")
    meta = _book_meta(result.book)
    assert meta["chapters"] == len(result.book.chapters), meta
    # 与 model.stats 同源：自己再算一遍比对
    from huoban.model import stats as real_stats

    assert meta["paragraphs"] == real_stats(result.book)["paragraphs"], meta
    assert meta["title"], "书名不该为空（clean.py 会从正文或文件名猜）"
    assert meta["language"] == "zh-CN"


def case_book_meta_title_can_be_edited_by_user():
    """④ 页的书名是**预填值**，不是事实 —— 用户改了要能生效。"""
    result = clean_text(SAMPLE, source="假书.txt")
    before = result.book.title
    result.book.title = "我自己起的名字"
    meta = _book_meta(result.book)
    assert meta["title"] == "我自己起的名字"
    assert before != meta["title"]


# ---------- 复核页数据（五类分组 / 分页 / 点开看原文） ----------


def _session_for(text: str = SAMPLE, *, adjust: dict | None = None, name: str = "假书.txt"):
    """造一个只够复核页用的会话。

    `_source_lines` 要真去读原文（行号是原文行号，清洗后的文本里删掉的行已经
    不在），所以这里把同一份文本落成临时文件，保证两边行号对得上。
    """
    import tempfile

    td = tempfile.mkdtemp()
    src = Path(td) / name
    src.write_text(text, encoding="utf-8")
    return Session(source=src, result=clean_text(text, source=name, adjust=adjust), adjust=adjust)


def _group(payload: dict, key: str) -> dict:
    return next(g for g in payload["groups"] if g["key"] == key)


def case_review_groups_cover_five_kinds():
    """复核页就五类：删除 / 合并 / 切章 / 章节树 / 缺字（决定36）。

    顺序即界面上的顺序。能勾的只有删除与合并 —— 其余三类是"只能看"的事实，
    给个假开关是骗人。
    """
    payload = _review_payload(_session_for())
    keys = [g["key"] for g in payload["groups"]]
    assert keys == ["del", "merge", "chap", "tree", "font"], keys
    for g in payload["groups"]:
        for name in ("name", "unit", "act", "hint", "num", "rows", "note", "more"):
            assert name in g, (name, g["key"])
        assert isinstance(g["num"], int) and g["num"] >= 0, g
        assert isinstance(g["rows"], list), g
    assert [g["key"] for g in payload["groups"] if g["act"]] == ["del", "merge"]
    assert payload["name"], "复核页顶部要显示书名"


def case_review_rows_carry_line_and_tag():
    """删除行要有行号（能回本地核对）与类目（广告 / 目录）。"""
    rows = _group(_review_payload(_session_for()), "del")["rows"]
    assert rows, "这份假数据里应当有整行广告/目录被删"
    for row in rows:
        assert isinstance(row["ln"], int) and row["ln"] > 0, row
        assert row["tag"] in ("广告", "目录"), row
        assert isinstance(row["kept"], bool), row
    tags = {r["tag"] for r in rows}
    assert "toc" not in tags
    assert "广告" in tags and "目录" in tags, tags


def case_review_rows_are_brief():
    """列表行只送 60 字摘要 —— 两千条删除每条都带全文，JSON 会到几十 MB。

    全文按需取（`review_detail`），一次一条。
    """
    for g in _review_payload(_session_for())["groups"]:
        for row in g["rows"]:
            assert len(row["tx"]) <= 61, (g["key"], row["tx"])


def case_review_delete_detail_shows_context():
    """点开一条删除：左栏是原文（那一行带删除线），右栏是去掉它之后的样子。

    这就是决定25 的形态 —— 两栏对照本身说明一切，不需要一句"删掉 1 行，
    前后文不动"。
    """
    s = _session_for()
    rows = _group(_review_payload(s), "del")["rows"]
    detail = _detail(s, "del", 0)
    assert detail["lcap"] == "原文" and detail["rcap"] == "删除后", detail
    gone = [p for p in detail["left"] if p.get("cls") == "gone"]
    assert len(gone) == 1, detail
    assert gone[0]["ln"] == rows[0]["ln"], detail
    # 那一行的字必须是原文里真有的一行（不是摘要、不是形状描述）
    originals = [ln.strip() for ln in SAMPLE.splitlines()]
    assert gone[0]["t"] in originals, gone[0]["t"]
    assert len(detail["right"]) == len(detail["left"]) - 1, detail


def case_review_merge_rows_and_detail():
    """合并处：行号区间、拼接点、以及"左边几行、右边一段"的对照。"""
    s = _session_for()
    rows = _group(_review_payload(s), "merge")["rows"]
    assert rows, "这份假数据里应当有碎行合并"
    row = rows[0]
    assert row["ln2"] >= row["ln"], row
    assert row["cut"] == row["ln2"], row
    assert row["tag"].startswith("并 "), row
    detail = _detail(s, "merge", 0)
    assert detail["rcap"] == "合并后", detail
    assert len(detail["left"]) >= 2, detail
    assert all(isinstance(p.get("ln"), int) for p in detail["left"]), detail
    assert detail["right"] and detail["right"][0]["t"], detail


def case_review_merge_is_full_not_sample():
    """决定29：合并明细全量记录，不再只留 8 条样例。

    这条用例故意造 12 处合并 —— 超过旧的 SAMPLE_LIMIT（8），
    只要还截断就必然失败。
    """
    text = (
        "第一章　起\n\n"
        + "".join(f"第 {i} 句没写完\n接上后半句\n\n" for i in range(12))
        + "第二章　终\n\n最后一段。\n"
    )
    s = _session_for(text)
    assert len(s.result.merges) >= 12, len(s.result.merges)
    g = _group(_review_payload(s), "merge")
    assert g["num"] == len(s.result.merges), (g["num"], len(s.result.merges))


def case_review_marks_kept_from_adjust():
    """重跑之后勾选态要画回去：保护过的行带 kept，不是每次从零开始。"""
    base = _session_for()
    ln = _group(_review_payload(base), "del")["rows"][0]["ln"]
    again = _session_for(adjust={"protect_lines": [ln]})
    rows = _group(_review_payload(again), "del")["rows"]
    hit = [r for r in rows if r["ln"] == ln]
    assert hit and hit[0]["kept"] is True, (ln, hit)


def case_review_marks_break_from_adjust():
    """合并处的「别并」同理：勾过的那一处 brk 为真。"""
    base = _session_for()
    cut = _group(_review_payload(base), "merge")["rows"][0]["cut"]
    again = _session_for(adjust={"merge_breaks": [cut]})
    rows = _group(_review_payload(again), "merge")["rows"]
    hit = [r for r in rows if r["cut"] == cut]
    # 断开之后那一处已经不合并了，但它必须还在清单里 —— 否则「别并」是单向门，
    # 用户没法把勾取消掉。合成行带着 brk=True 与「已断开」的类目。
    assert hit and hit[0]["brk"] is True, (cut, hit)
    assert hit[0]["tag"] == "已断开", hit[0]


def case_review_paginates_and_more():
    """决定27：滚到底再要下一批，不是一次把三千条塞进 DOM。"""
    text = (
        "第一章　起\n\n"
        + "\n\n".join(
            f"正文第 {i} 段，带句号收尾。\n\n天才一秒记住本站 https://example.com/{i}"
            for i in range(1, 101)
        )
        + "\n\n第二章　终\n\n最后一段。\n"
    )
    api = Api()
    api._sessions["j1"] = _session_for(text)
    g = _group(api.review("j1"), "del")
    assert g["num"] == 100, g["num"]
    assert len(g["rows"]) == REVIEW_PAGE, len(g["rows"])
    assert g["more"] is True
    more = api.review_more("j1", "del", offset=REVIEW_PAGE)
    assert more["rows"], more
    assert more["rows"][0]["ln"] > g["rows"][-1]["ln"], more
    assert more["more"] is True
    assert api.review_more("j1", "del", offset=500)["more"] is False


def case_review_detail_out_of_range_is_error():
    """行号越界、类目不存在都要返回 error，不能让界面拿到半截数据。"""
    api = Api()
    api._sessions["j1"] = _session_for()
    assert api.review_detail("j1", "del", 99999)["error"]
    assert api.review_detail("j1", "nope", 0)["error"]
    assert api.review_more("j1", "nope")["rows"] == []


def case_review_empty_book_has_no_rows():
    """干净的书：五类都在，列表给空数组而不是缺字段。"""
    payload = _review_payload(_session_for("第一章　起\n\n只有一段正文，干干净净。\n"))
    assert _group(payload, "del")["rows"] == []
    assert _group(payload, "merge")["rows"] == []
    assert _group(payload, "del")["num"] == 0
    assert isinstance(_group(payload, "font")["note"], str)


def case_chapter_rows_groups_volumes_no_false_alerts():
    """多卷书：每卷章号从 1 重新开始，不能报一堆假「编号倒退/重复/空章」。

    回归用例（试用反馈，见附图1）：五卷、每卷三章、卷容器本身 0 段，
    旧 _chapter_rows 把全书章节当一条线比较，一次报出 12 条假警报（5 空章
    + 2 编号重复 + 5 编号倒退）。编号比较必须复用 model.chapter_groups()
    按卷分组，且 level=2 卷容器不报"空章"。
    """
    from gui.api import _chapter_rows
    from huoban.model import Book, Chapter, Paragraph

    chapters = []
    for v in range(1, 6):
        chapters.append(Chapter(title=f"第{v}卷", number=v, level=2, paragraphs=[]))
        for c in range(1, 4):
            chapters.append(Chapter(title=f"第{c}章", number=c, level=1,
                                    paragraphs=[Paragraph(text="正文。")]))
    rows = _chapter_rows(Book(title="多卷书", chapters=chapters))
    assert rows == [], f"多卷书不该有假警报：{[(r['tag'], r['tx']) for r in rows]}"

    # 同一组里真重复、真倒退仍要报出来 —— 修假警报不能把真问题一起吞掉。
    broken = [
        Chapter(title="第一章", number=1, level=1, paragraphs=[Paragraph(text="正文。")]),
        Chapter(title="第一章", number=1, level=1, paragraphs=[Paragraph(text="正文。")]),
        Chapter(title="第0章", number=0, level=1, paragraphs=[Paragraph(text="正文。")]),
    ]
    codes = {r["code"] for r in _chapter_rows(Book(title="错书", chapters=broken))}
    assert "number_dup" in codes and "number_decrease" in codes, codes


def case_chapter_rows_five_keys_and_idx_roundtrip():
    """章节树每一行带五键，idx 是章节下标，_detail_tree 能按 idx 回查到原章。

    回归用例（第五轮只读 review C3）：_chapter_rows 重写了 add() 与排序，
    得锁住行结构没变、idx 没被排序换掉、level=2 卷容器不报"空章"。
    """
    from types import SimpleNamespace
    from gui.api import _chapter_rows, _detail_tree
    from huoban.model import Book, Chapter, Paragraph

    book = Book(title="错书", chapters=[
        Chapter(title="", number=5, level=1, paragraphs=[Paragraph(text="正文。")]),
        Chapter(title="有标题", number=6, level=1, paragraphs=[]),
    ])
    rows = _chapter_rows(book)
    assert rows, rows
    by_code = {}
    for r in rows:
        assert set(r) == {"ln", "idx", "code", "tx", "tag"}, r
        assert 0 <= r["idx"] < len(book.chapters), r
        by_code[r["code"]] = r
    # 无标题在第 0 章、0 段在第 1 章，idx 指的是产生该条的那一个章。
    assert by_code["empty_title"]["idx"] == 0, by_code
    assert by_code["chapter_empty"]["idx"] == 1, by_code
    session = SimpleNamespace(result=SimpleNamespace(book=book))
    detail = _detail_tree(session, by_code["chapter_empty"])
    assert "有标题 · 0 段" in [x["t"] for x in detail["left"]], detail
    assert detail["ro"] is True, detail

    # level=2 卷容器 0 段不报空章；同一份书里 level=1 的真空章仍报。
    vol_book = Book(title="卷书", chapters=[
        Chapter(title="第一卷", number=1, level=2, paragraphs=[]),
        Chapter(title="第一章", number=1, level=1, paragraphs=[]),
    ])
    vol_rows = _chapter_rows(vol_book)
    assert any(r["code"] == "chapter_empty" and r["idx"] == 1 for r in vol_rows), vol_rows
    assert not any(r["code"] == "chapter_empty" and r["idx"] == 0 for r in vol_rows), vol_rows


def case_review_groups_are_cached_per_result():
    """分组清单挂在会话上缓存；重跑换了 result 就要重算。"""
    s = _session_for()
    first = _groups_of(s)
    assert _groups_of(s) is first, "同一个 result 不该重算"
    s.result = clean_text(SAMPLE, source="假书.txt", adjust={"protect_lines": [5]})
    assert _groups_of(s) is not first, "换了 result 必须重算"


# ---------- apply_adjust 的形状校验 ----------


def case_apply_adjust_rejects_bad_shape_without_job():
    """结构错必须当场返回 error，不能起一个注定失败的 job。

    `_parse_adjust` 的 ValueError 是"你的请求本身不对"，界面要立刻看到；
    行号数据错（越界等）才走"忽略并记账"。
    """
    api = Api()
    # 没有会话时先给"会话不在"的错，不会崩
    assert api.apply_adjust("nope", {})["error"]
    assert api.review("nope")["error"]


def case_api_has_underscore_window():
    """窗口引用必须是下划线私有 —— 否则 pywebview 会递归遍历整个 COM 树。

    实测踩过：公开的 self.window 会让它去钻
    Window → native → AccessibilityObject，日志刷几百行 recursion。
    """
    api = Api()
    assert hasattr(api, "_window"), "必须有 _window"
    assert not hasattr(api, "window"), "绝不能有公开的 window 属性"


def case_app_info_reports_azw3_availability():
    """`app_info` 要如实说这台机器能不能导 AZW3 —— 界面据此决定置灰与否。

    没装 Calibre 的机器上，让人勾了再失败，比一开始就说清楚差得多。
    这里只断言类型与它跟 `azw3.available()` 一致，不断言具体真假
    （装没装是机器的事，不是代码的事）。
    """
    from huoban.azw3 import available as azw3_available

    info = Api().app_info()
    assert isinstance(info.get("azw3"), bool), "azw3 必须是 bool"
    assert info["azw3"] == (azw3_available() is not None)
    assert isinstance(info.get("version"), str)
    assert isinstance(info.get("max_input_mb"), int)


def case_import_summary_is_not_a_clean_bill():
    """导入的书不能套清洗那张账单 —— 否则界面会显示"清洗跑了但什么都没删"。

    `_import_summary` 的 dropped/fixed/merged 全给空 dict，而不是一堆 0：
    一堆 0 是假信息（用户会以为 cleaners 干活了但一无所获），空 dict 配上
    kind="epub" 才让前端知道"这条入口压根没有这张账单"。
    """
    from gui.api import _import_summary
    from huoban.model import Book, Chapter, Paragraph

    book = Book(
        title="导入的书",
        author="某人",
        encoding="utf-8",
        source="x.epub",
        front_matter=[Paragraph(text="前言一段")],
        chapters=[Chapter(title="第一章", level=1, paragraphs=[Paragraph(text="正文")])],
        tail_matter=[],
    )
    report = {
        "epub_version": "2.0",
        "nav_entries": 5,
        "chapters": 1,
        "volumes": 0,
        "merged_groups": 0,
        "merged_pieces": 3,
        "front_matter_entries": 1,
        "toc_dropped": 0,
        "continuation_docs": 0,
        "empty_chapters": 0,
        "images_kept": 2,
        "images_decorative": ["decor.png"],
        "images_orphan": [],
        "cover": "cover.jpg",
        "fonts": 1,
        "warnings": [],
    }
    out = _import_summary(report, book, outdir=Path("X:/tmp/书_gui"))

    assert out["kind"] == "epub", "kind 必须标成 epub，前端靠它分支"
    assert out["dropped"] == {} and out["fixed"] == {} and out["merged"] == {}, (
        "导入这条路没有删除/改写/合并的账单，必须是空 dict 而不是一堆 0"
    )
    assert out["adjust"] == {}
    assert out["stats"]["chapters"] == 1
    # 导入没有 clean-report.md，报告是 report.json
    assert out["report_path"].endswith("report.json")
    assert out["import"]["images_kept"] == 2
    assert out["import"]["merged_pieces"] == 3, "分片总数要如实带出来（前端另有措辞）"


def case_import_summary_never_leaks_image_names():
    """导入账单只给计数，不给图片文件名 —— 那些名字来自原书，没必要送进界面。"""
    from gui.api import _import_summary
    from huoban.model import Book

    book = Book(title="t", author="", encoding="utf-8", source="x.epub",
                front_matter=[], chapters=[], tail_matter=[])
    report = {
        "epub_version": "3.0", "nav_entries": 0, "chapters": 0, "volumes": 0,
        "merged_groups": 0, "merged_pieces": 0, "front_matter_entries": 0,
        "toc_dropped": 0, "continuation_docs": 0, "empty_chapters": 0,
        "images_kept": 1, "images_decorative": 0, "images_orphan": 0,
        "images_kept_origin": ["SECRET-IMAGE-NAME.png"],
        "images_decorative": ["SECRET-DECOR.png"],
        "images_orphan": ["SECRET-ORPHAN.png"],
        "cover": "SECRET-COVER.jpg", "fonts": 0, "warnings": [],
    }
    out = _import_summary(report, book, outdir=Path("X:/tmp/t_gui"))
    blob = json.dumps(out, ensure_ascii=False)
    assert "SECRET" not in blob, f"图片文件名不该进界面载荷：{blob[:200]}"
    assert out["import"]["images_kept"] == 1
    # cover 也只给布尔，不给文件名
    assert out["import"]["cover"] is True


def case_import_summary_carries_source_size():
    """载荷要带源文件字节数 —— ② 页第一行要显示"EPUB 2.0 · 20 MB"。

    导入这条路的 size_bytes 不是清洗输入的行数统计，得单独填源文件大小；
    漏了就会显示 "0 B"（第一版就是这样）。
    """
    from gui.api import _import_summary
    from huoban.model import Book

    book = Book(title="t", author="", encoding="utf-8", source="x.epub",
                front_matter=[], chapters=[], tail_matter=[])
    out = _import_summary({"epub_version": "2.0"}, book, outdir=Path("X:/tmp/t_gui"))
    assert out["size_bytes"] == 0, "占位必须是 0，由调用方填真实值"
    assert out["lines"] == {"total": 0, "nonblank": 0}
    assert out["encoding"]["confidence"] == 0.0, "导入没有编码置信度这回事"


def case_pick_file_offers_epub_too():
    """「选文件」的对话框必须同时认 TXT 与 EPUB（review #4 的 S6）。

    后端早就支持拖 .epub（docs/gui-design.md 的「EPUB 导入」一节），但对话框的 file_types 只写了 txt ——
    用户点"也可以点这里选文件"时看到的是被筛过的列表，.epub 是灰的。
    用一个假窗口接住调用参数就能验，不用真开窗。
    """
    from gui.api import Api

    seen = {}

    class _FakeWindow:
        def create_file_dialog(self, **kwargs):
            seen.update(kwargs)
            return []          # 用户点了取消

    api = Api()
    api._window = _FakeWindow()
    out = api.pick_file()
    assert out["paths"] == [], "取消时应该返回空列表"
    types = " ".join(seen.get("file_types") or ())
    assert ".epub" in types, f"对话框的类型过滤漏了 .epub：{types}"
    assert ".txt" in types, f"对话框的类型过滤漏了 .txt：{types}"


def _tmp_root(tag: str) -> Path:
    """每次调用一个新目录（本项目禁删，所以绝不重用固定路径）。

    踩过：探针复用 `tmp/m1` 之后，上一轮留下的 `同名_gui` 被当成"本轮缺陷"，
    M1 三个名字变成 (2)/(3)/(4)、M3 直接假红。用例也一样要避开。
    """
    import tempfile

    return Path(tempfile.mkdtemp(prefix=f"t2e-{tag}-"))


def case_import_outdir_reuses_only_same_source():
    """TXT 入口的目录归属：同源重导复用，异源撞名另起（review #5 的 1）。

    两种情形在磁盘上长得一模一样（同名目录、都有 book.json），只能靠
    book.json 里记的 source 区分：
      - 同一个输入再来一次（改了源文件再拖一遍）→ 该复用，不该增生 `(2)`；
      - 两本不同的书解析出同一个书名 → 必须另起，否则上一本的
        book.json / report.json 被默默覆写（实测复现过）。
    """
    from gui.api import _import_outdir

    root = _tmp_root("owner")
    a = root / "vol1.txt"
    b = root / "vol2.txt"
    a.write_text("《同本书》\n", encoding="utf-8")
    b.write_text("《同本书》\n", encoding="utf-8")

    first = _import_outdir(a, "同本书")
    assert first.name == "同本书_gui", first.name
    first.mkdir()
    # 造一份"属于 a"的 book.json（save_book 写的就是这个字段）
    (first / "book.json").write_text(
        json.dumps({"source": str(a)}, ensure_ascii=False), encoding="utf-8"
    )

    again = _import_outdir(a, "同本书")
    assert again == first, f"同一输入重导该复用 {first.name}，却给了 {again.name}"

    other = _import_outdir(b, "同本书")
    assert other != first, "不同输入撞名还复用，会覆写上一本的产物"
    assert other.name == "同本书 (2)_gui", other.name


def case_import_outdir_third_source_skips_occupied():
    """第三次换一个源来撞名，要跳过已被占的 `(2)`，而不是永远落在 `(2)` 上。"""
    from gui.api import _import_outdir

    root = _tmp_root("owner3")
    srcs = []
    for i in range(3):
        p = root / f"v{i}.txt"
        p.write_text("《同本书》\n", encoding="utf-8")
        srcs.append(p)

    dirs = []
    for p in srcs:
        d = _import_outdir(p, "同本书")
        assert not d.exists() or d.name == dirs[-1].name if dirs else True
        d.mkdir()
        (d / "book.json").write_text(
            json.dumps({"source": str(p)}, ensure_ascii=False), encoding="utf-8"
        )
        dirs.append(d)
    names = [d.name for d in dirs]
    assert names == ["同本书_gui", "同本书 (2)_gui", "同本书 (3)_gui"], names

    # 回头再导第一个源：应当回到它自己的 `同本书_gui`
    back = _import_outdir(srcs[0], "同本书")
    assert back.name == "同本书_gui", back.name


def case_import_outdir_finds_own_numbered_dir():
    """自己的目录被挤到 `(2)`，再导一次要回到 `(2)`，不能一路增生。

    这是"序号里也要查归属"的唯一触发路径：
      B 先导 → `同本书_gui` 归 B；A 再导 → `同本书 (2)_gui` 归 A；
      A 又导一次 → 基名归 B（不是我的），必须顺着序号找到归 A 的那个。
    不查序号候选的归属就会给出 `(3)`，每导一次多一个空目录。
    """
    from gui.api import _import_outdir

    root = _tmp_root("ownernum")
    a = root / "a.txt"
    b = root / "b.txt"
    for p in (a, b):
        p.write_text("《同本书》\n", encoding="utf-8")

    def take(src: Path) -> Path:
        d = _import_outdir(src, "同本书")
        d.mkdir()
        (d / "book.json").write_text(
            json.dumps({"source": str(src)}, ensure_ascii=False), encoding="utf-8"
        )
        return d

    db = take(b)
    da = take(a)
    assert db.name == "同本书_gui", db.name
    assert da.name == "同本书 (2)_gui", da.name

    again = _import_outdir(a, "同本书")
    assert again.name == "同本书 (2)_gui", f"该回到自己的 (2)，却给了 {again.name}"


def case_import_outdir_treats_broken_book_json_as_taken():
    """book.json 坏了 / 没有 source 时，当成"别人的目录"，宁可另起也不覆写。"""
    from gui.api import _import_outdir

    root = _tmp_root("ownerbad")
    src = root / "vol1.txt"
    src.write_text("《同本书》\n", encoding="utf-8")

    occupied = root / "同本书_gui"
    occupied.mkdir()
    (occupied / "book.json").write_text("{ 这不是 JSON", encoding="utf-8")

    got = _import_outdir(src, "同本书")
    assert got.name == "同本书 (2)_gui", got.name

    # 没有 book.json 的目录同理
    root2 = _tmp_root("ownernone")
    src2 = root2 / "vol1.txt"
    src2.write_text("《同本书》\n", encoding="utf-8")
    (root2 / "同本书_gui").mkdir()
    got2 = _import_outdir(src2, "同本书")
    assert got2.name == "同本书 (2)_gui", got2.name


def case_build_failure_says_packaging_not_cleaning():
    """打包阶段出错要报「打包失败」，不能报「清洗失败」（review #5 的 2）。

    打包时 `book.json` 被占用抛的是 `PermissionError`（OSError），
    光按异常类型分会落进"清洗失败" —— 用户看到会去换文件试，
    而真正要处理的是占用/权限。
    """
    from gui.api import Api, InputError

    api = Api()
    titles = {}
    for stage, exc in (
        ("build", PermissionError("被占用")),
        ("build", OSError("地址写错")),
        ("import", RuntimeError("清洗内核炸了")),
        ("import", InputError("文件不合格")),
        ("import", ValueError("章节树不自洽")),
    ):
        job = api._new_job(stage)
        try:
            api._spawn(job, lambda e=exc: (_ for _ in ()).throw(e))
        except BaseException:  # noqa: BLE001 - _spawn 内部兜住，这里防御
            pass
        job.thread.join(timeout=10)
        titles[(stage, type(exc).__name__)] = job.error_title

    assert titles[("build", "PermissionError")] == "打包失败", titles
    assert titles[("build", "OSError")] == "打包失败", titles
    assert titles[("import", "RuntimeError")] == "清洗失败", titles
    assert titles[("import", "InputError")] == "这个文件不能处理", titles
    assert titles[("import", "ValueError")] == "打包失败", titles


def case_build_keeps_epub_when_book_json_is_locked():
    """book.json 写不进去时，成品 EPUB 必须还在、且警告要说明（review #5 的 2）。

    实测踩过：`book.json` 设只读 → 整条 build 报失败、payload 为 None，
    而 9KB 的 `甲书.epub` 已经安静躺在目录里，用户完全不知道。
    """
    import subprocess
    import sys

    script = r'''
import stat, sys, time, tempfile
from pathlib import Path
sys.path.insert(0, r"@ROOT@")
from gui.api import Api

root = Path(tempfile.mkdtemp(prefix="t2e-lock-"))
src = root / "甲书.txt"
src.write_text("《甲书》\n作者：某人\n\n第一章　开\n\n正文第一段。\n\n"
               "第二章　终\n\n正文第二段。\n", encoding="utf-8")
api = Api()
r = api.import_file(str(src))
jid = r["job_id"]
for _ in range(2000):
    p = api.job_poll(jid)
    if p["done"]:
        break
    time.sleep(0.02)
assert not p["error"], p["error"]
outdir = Path(p["payload"]["outdir"])
bp = outdir / "book.json"
bp.chmod(stat.S_IREAD)
try:
    b = api.build_epub(jid, {"fonts": False})
    bid = b["job_id"]
    for _ in range(4000):
        q = api.job_poll(bid)
        if q["done"]:
            break
        time.sleep(0.02)
    made = sorted(x.name for x in outdir.glob("*.epub"))
    print("TITLE:", repr(q["error_title"]))
    print("EPUB:", made, [x.stat().st_size for x in outdir.glob("*.epub")])
    print("WARN:", [w[:20] for w in (q["payload"] or {}).get("warnings", [])])
    print("OK:", bool(q["payload"]) and bool(made) and any("book.json" in w for w in q["payload"]["warnings"]))
finally:
    bp.chmod(stat.S_IWRITE)
'''.replace("@ROOT@", str(Path(__file__).resolve().parents[1]))

    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=300,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    out = proc.stdout
    assert "OK: True" in out, f"成品丢了或没警告：\n{out}\n{proc.stderr[-400:]}"


def case_unique_dir_never_reuses_existing():
    """目录名撞了必须换一个，不能复用（review #4 的 M1）。

    复用会覆写别人的 book.json / report.json；更麻烦的是旧 images/ 还在，
    打包时 `write_epub(image_dir=…)` 会把上一本书的插图一起塞进新 EPUB。
    """
    from gui.api import _unique_dir

    root = _tmp_root("uniq")
    (root / "书_gui").mkdir()
    got = _unique_dir(root, "书", "_gui")
    assert got.name != "书_gui", "撞了还返回同一个名字，会覆写"
    assert not got.exists(), "返回的目录还没建，调用方负责"
    got.mkdir()
    third = _unique_dir(root, "书", "_gui")
    assert len({got.name, third.name, "书_gui"}) == 3


def case_unique_dir_numbering_is_sequential():
    """序号要连续、可读，且每个返回值的父目录都对。"""
    from gui.api import _unique_dir

    root = _tmp_root("seq")
    names = []
    for _ in range(3):
        d = _unique_dir(root, "同名", "_gui")
        names.append(d.name)
        d.mkdir()
    assert names == ["同名_gui", "同名 (2)_gui", "同名 (3)_gui"], names
    assert all(_unique_dir(root, "别的", "_gui").parent == root for _ in range(1))


def _tiny_epub(path: Path, *, no_docs: bool = False) -> None:
    """造一本最小 EPUB。no_docs=True 时 manifest 里没有任何 xhtml。"""
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        if no_docs:
            zf.writestr(
                "OEBPS/content.opf",
                '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                'version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>空书</dc:title></metadata>"
                '<manifest><item id="i1" href="a.png" media-type="image/png"/></manifest>'
                "<spine/></package>",
            )
            zf.writestr("OEBPS/a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
            return
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>小书</dc:title></metadata>"
            '<manifest><item id="c1" href="c1.xhtml" '
            'media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>',
        )
        zf.writestr(
            "OEBPS/c1.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title></head>'
            "<body><h1>第一章</h1><p>正文一段。</p></body></html>",
        )


def _book_epub_with_back(path: Path) -> bytes:
    """造一本"有封底、没内嵌字体"的最小 EPUB，返回封底图的字节。

    封底页照自家产物写：body 里就是 <!-- back-cover --> 标记 + Images/back.png。
    给"导进来再打出去，封底还在不在"那条端到端用（TODO 0.2.2 第 5 条）。
    """
    import zipfile

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>有封底的书</dc:title><dc:creator>某人</dc:creator></metadata>"
            '<manifest>'
            '<item id="c1" href="Text/c1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="back" href="Text/back.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="back-image" href="Images/back.png" media-type="image/png"/>'
            "</manifest>"
            '<spine><itemref idref="c1"/><itemref idref="back"/></spine>'
            '<guide><reference type="back" title="封底" href="Text/back.xhtml"/></guide>'
            "</package>",
        )
        zf.writestr(
            "OEBPS/Text/c1.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title></head>'
            "<body><h1>第一章</h1><p>正文一段。</p><p>正文二段。</p></body></html>",
        )
        zf.writestr(
            "OEBPS/Text/back.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>封底</title></head>'
            '<body><!-- back-cover --><div class="back">'
            '<img src="../Images/back.png" alt=""/></div></body></html>',
        )
        zf.writestr("OEBPS/Images/back.png", png)
    return png


def case_epub_roundtrip_keeps_the_original_back_cover():
    """导进来再打出去，原书封底页/图/样式都还在（TODO 0.2.2 第 5 条验收）。

    旧行为：封底页整页跳过（防"封底续进末章、每导一次多两段"的老设计，别动），
    但那张图也跟着没了 —— 于是回环一次，产物就永久少一页封底。封底是宣传渠道。
    """
    import zipfile

    from huoban.epubread import read_epub_with_report

    root = _tmp_root("back-e2e")
    src = root / "有封底.epub"
    png = _book_epub_with_back(src)

    api = Api()
    import_job = Job(job_id="t-be-import")
    session = Session(source=src, adjust=None)
    api._sessions["t-be-import"] = session
    api._run_import_epub(import_job, session)
    assert import_job.error == "", import_job.error
    meta = import_job.payload["meta"]
    assert meta["has_source_back"] is True, meta
    assert meta["source_fonts"] == 0, "夹具没有内嵌字体，默认值要跟着它走"

    # 封底页本身仍然整页跳过：正文里不许出现封底那段
    book = session.result.book
    before = [p.text for ch in book.chapters for p in ch.paragraphs]
    assert before == ["正文一段。", "正文二段。"], before

    # 按 ④ 页默认值打包：没选封面（cover_mode 空），字体不嵌 —— 封底仍要贴回去
    build_job = Job(job_id="t-be-build")
    api._sessions["t-be-build"] = session
    api._run_build(build_job, session, {
        "title": meta["title"], "author": meta.get("author", ""),
        "language": "zh-CN", "fonts": False, "cover_mode": "",
    })
    assert build_job.error == "", build_job.error
    epub = Path(build_job.payload["epub"])
    with zipfile.ZipFile(epub) as zf:
        names = zf.namelist()
        assert "OEBPS/Text/back.xhtml" in names, names
        backs = [n for n in names if n.startswith("OEBPS/Images/back.")]
        assert len(backs) == 1, names
        assert zf.read(backs[0]) == png, "封底图字节与原书对不上"
        page = zf.read("OEBPS/Text/back.xhtml").decode("utf-8")
        css = zf.read("OEBPS/Styles/main.css").decode("utf-8")
    assert "back-body" in page and "<img" in page, page[:200]
    assert ".back {" in css, "封底样式没进 main.css"

    # 回环一轮：正文一字不增不减，封底页仍被认成自家生成页（不会续进末章）。
    # 要给 image_dir —— back_file 只在落图片目录时才有值（与 cover_file 同款语义）。
    again = read_epub_with_report(epub, image_dir=root / "回环" / "images")
    assert again.report["generated_pages"] == 1, again.report["generated_pages"]
    after = [p.text for ch in again.book.chapters for p in ch.paragraphs]
    assert after == before, f"回环一轮正文变了：{after}"
    assert again.report["back_file"], "第二轮没认出封底"


def case_epub_broken_package_is_input_error():
    """包内损坏必须收成 EpubReadError（review #4 的 M2）。

    早先只有"打开 zip"那一步被包住，章节解压时抛的 BadZipFile / zlib.error /
    RuntimeError 会一路冒到界面层，落成"清洗失败" + 英文原文（含包内路径）。
    """
    from huoban.epubread import EpubReadError, read_epub_with_report

    path = _tmp_root("m2") / "bad.epub"
    _tiny_epub(path)
    raw = bytearray(path.read_bytes())
    idx = raw.find(b"PK\x03\x04")
    while idx >= 0:
        name_len = int.from_bytes(raw[idx + 26:idx + 28], "little")
        extra_len = int.from_bytes(raw[idx + 28:idx + 30], "little")
        comp_len = int.from_bytes(raw[idx + 18:idx + 22], "little")
        if bytes(raw[idx + 30:idx + 30 + name_len]).endswith(b"c1.xhtml"):
            start = idx + 30 + name_len + extra_len
            for k in range(start, min(start + comp_len, len(raw))):
                raw[k] ^= 0xFF
            break
        idx = raw.find(b"PK\x03\x04", idx + 4)
    path.write_bytes(bytes(raw))

    try:
        read_epub_with_report(path)
    except EpubReadError as exc:
        msg = str(exc)
        assert "bad.epub" in msg, "报错要说清是哪个文件"
        assert "BadZipFile" in msg or "zlib" in msg or "损坏" in msg, msg
    except BaseException as exc:  # noqa: BLE001
        raise AssertionError(f"应该收成 EpubReadError，实际是 {type(exc).__name__}: {exc}")
    else:
        raise AssertionError("坏包居然读成功了")


def case_broken_package_message_explains_itself():
    """损坏包的报错里不能出现「（error）」这种没信息量的类名（review #5 的 3）。

    `zlib.error` 的类名就是 `error`，早先格式化直接写
    `exc.__class__.__name__`，用户看到的是
    「包里有损坏或不支持的内容（error）」—— 一个叫 error 的东西是什么？
    """
    from huoban.epubread import _digest_exc

    import zlib

    # 这三个都在 `read_epub_with_report` 的捕获元组里，必须都能讲人话
    for exc, must in (
        (zlib.error("bad"), "压缩流"),
        (__import__("zipfile").BadZipFile("x"), "ZIP"),
        (RuntimeError("enc"), "加密"),
    ):
        got = _digest_exc(exc)
        assert "：" in got, f"{type(exc).__name__} 没给人话：{got}"
        assert must in got, f"{type(exc).__name__} 的说明里该有「{must}」，实际 {got}"

    # 认不出来的异常只给类名，不能凭空编一句话
    assert _digest_exc(ValueError("x")) == "ValueError"

    # 端到端：真造一个坏包，报错文案里必须带人话
    path = _tmp_root("digest") / "bad.epub"
    _tiny_epub(path)
    raw = bytearray(path.read_bytes())
    idx = raw.find(b"PK\x03\x04")
    while idx >= 0:
        name_len = int.from_bytes(raw[idx + 26:idx + 28], "little")
        extra_len = int.from_bytes(raw[idx + 28:idx + 30], "little")
        comp_len = int.from_bytes(raw[idx + 18:idx + 22], "little")
        if bytes(raw[idx + 30:idx + 30 + name_len]).endswith(b"c1.xhtml"):
            start = idx + 30 + name_len + extra_len
            for k in range(start, min(start + comp_len, len(raw))):
                raw[k] ^= 0xFF
            break
        idx = raw.find(b"PK\x03\x04", idx + 4)
    path.write_bytes(bytes(raw))

    from huoban.epubread import EpubReadError, read_epub_with_report

    try:
        read_epub_with_report(path)
    except EpubReadError as exc:
        msg = str(exc)
        assert "（error）" not in msg, f"还是那句没信息量的：{msg}"
        assert "压缩流" in msg or "ZIP" in msg, msg
    else:
        raise AssertionError("坏包居然读过去了")


def case_epub_failure_leaves_no_stage_dir():
    """解析失败不能在用户输入目录旁边留下空的 images 目录（review #4 的 M3）。

    早先 `mkdir` 在 spine 校验之前，于是"没有任何正文文档"这种失败也会留下
    `<文件名>_gui\\images` 空目录 —— 界面上是"报错了但多了个空文件夹"。
    """
    from huoban.epubread import EpubReadError, read_epub_with_report

    root = _tmp_root("m3")
    path = root / "empty.epub"
    _tiny_epub(path, no_docs=True)
    side = root / "empty_gui"
    try:
        read_epub_with_report(path, image_dir=side / "images")
    except EpubReadError:
        pass
    else:
        raise AssertionError("没有正文文档的包居然读成功了")
    assert not side.exists(), f"失败却留下了目录：{side}"


# ---------- 空输入：不能再长得像成功 ----------


def case_zero_byte_file_is_rejected_at_entry():
    """0 字节文件必须在入口就拒掉（docs/gui-design.md 的「验收」一节第 5 条实测出的唯一文案缺口）。

    早先它一路走完清洗，拿回 0 章 0 字 + 两条技术警告，界面显示成一次**成功** ——
    用户拖错文件时看到的就是这个。规则写成"等于 0 字节"而不是"小于 1KB"：
    前者是事实，后者会挡住真的只有几十字的占位文件。
    """
    root = _tmp_root("zero")
    for name in ("空.txt", "空.epub"):
        path = root / name
        path.write_bytes(b"")
        job = Job(job_id="t")
        session = Session(source=path, adjust=None)
        try:
            Api()._run_import(job, session)
        except InputError as exc:
            assert "0 字节" in str(exc), f"{name} 的文案不对：{exc}"
        else:
            raise AssertionError(f"{name} 是 0 字节，居然走完了全程")
    # 0 字节的 EPUB 也归"这个文件不能处理"，不该落到 BadZipFile 那句英文里
    assert Api()._friendly_error(InputError("这个文件是空的（0 字节），里面没有正文")) == (
        "这个文件是空的（0 字节），里面没有正文"
    )


def case_format_gate_comes_before_the_size_gate():
    """格式闸在大小闸前面（TODO 0.2.2 第 2 条）。

    上一版的顺序是先看大小再看后缀，于是同一个 mp4：大于 50 MB 报「太大」、
    小于 50 MB 报「格式不支持」。用户换个小一点的再拖一次，得到的是另一句
    话，还以为是自己文件的问题。格式是「这个格式收不收」，先答它。
    """
    import gui.api as api_mod

    root = _tmp_root("gate")
    job = Job(job_id="t")
    old = api_mod.MAX_INPUT_BYTES
    api_mod.MAX_INPUT_BYTES = 100  # 不真造 50 MB 文件：把上限压小，顺序问题一样暴露
    try:
        # 同时"超上限"又"格式不收" → 必须报格式，不许提大小
        p = root / "片子.mp4"
        p.write_bytes(b"\x00" * 200)
        try:
            Api()._run_import(job, Session(source=p, adjust=None))
        except InputError as exc:
            msg = str(exc)
            assert "只接受 TXT 或 EPUB" in msg, msg
            assert ".mp4" in msg, msg
            assert "上限" not in msg, f"格式不收的文件不该先报大小：{msg}"
        else:
            raise AssertionError("mp4 居然过闸了")

        # 收的格式又超上限 → 仍然报大小（顺序换过头会把这句话吞掉）
        p = root / "大书.txt"
        p.write_bytes("第一章 开始\n".encode("utf-8") * 30)
        try:
            Api()._run_import(job, Session(source=p, adjust=None))
        except InputError as exc:
            assert "上限" in str(exc), str(exc)
        else:
            raise AssertionError("超上限的 txt 居然过闸了")
    finally:
        api_mod.MAX_INPUT_BYTES = old


def case_office_files_get_their_own_wording():
    """拖来 Word/RTF/WPS/ODT 说「虽为书稿，活板无能为力」（TODO 0.2.2 第 1 条）。

    它们是书稿，不是拖错了文件；笼统的「只接受 TXT 或 EPUB」会让人以为自己
    点错了东西。普通的"不是书"（mp4/图片）仍然用老文案。
    """
    root = _tmp_root("docx")
    for name in ("书稿.docx", "书稿.doc", "书稿.docm", "书稿.dotm", "书稿.dotx",
                 "书稿.rtf", "书稿.wps", "书稿.odt", "书稿.pages"):
        p = root / name
        p.write_bytes(b"PK\x03\x04not-really")
        try:
            Api()._run_import(Job(job_id="t"), Session(source=p, adjust=None))
        except InputError as exc:
            msg = str(exc)
            assert "虽为书稿，活板无能为力" in msg, f"{name} 的文案不对：{msg}"
            assert p.suffix in msg, f"{name} 的文案没点出后缀：{msg}"
        else:
            raise AssertionError(f"{name} 居然过闸了")

    p = root / "照片.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    try:
        Api()._run_import(Job(job_id="t"), Session(source=p, adjust=None))
    except InputError as exc:
        assert str(exc).startswith("只接受 TXT 或 EPUB 文件"), str(exc)
    else:
        raise AssertionError("png 居然过闸了")


def case_summary_content_state_flags_a_textless_book():
    """清洗完一个字不剩 → content_state=empty，不能让前端自己猜。"""
    from gui.api import _summary

    blank = clean_text("   \n\n\t  \n", source="空白.txt").report
    out = _summary(blank, outdir=Path("X:/tmp/空白_gui"))
    assert out["stats"]["chars"] == 0, out["stats"]
    assert out["content_state"] == "empty", out["content_state"]

    ok = _summary(_report(), outdir=Path("X:/tmp/书_gui"))
    assert ok["content_state"] == "text", ok["content_state"]


def case_import_summary_separates_images_only_from_empty():
    """0 字 + 有图是图集/漫画，0 字 + 没图才是空壳 —— 两句话，不能混。

    把漫画说成"你拖错了文件"是假话；把空壳说成"正常结果"是 docs/gui-design.md 的「验收」一节第 5 条那个缺口。
    """
    from gui.api import _import_summary
    from huoban.model import Book, Chapter, Paragraph

    book = Book(
        title="图集",
        author="",
        encoding="utf-8",
        source="comic.epub",
        front_matter=[],
        # 图片段不计字数（model.stats 明确跳过 kind=="image"），所以这本书 0 字
        chapters=[
            Chapter(title="第一话", level=1, paragraphs=[Paragraph(text="p1.png", kind="image")])
        ],
        tail_matter=[],
    )
    base = {
        "epub_version": "3.0", "nav_entries": 1, "chapters": 1, "volumes": 0,
        "merged_groups": 0, "merged_pieces": 1, "front_matter_entries": 0,
        "toc_dropped": 0, "continuation_docs": 0, "empty_chapters": 0,
        "images_decorative": [], "images_orphan": [], "cover": "", "fonts": 0,
        "warnings": [],
    }
    with_images = _import_summary(
        {**base, "images_kept": 12}, book, outdir=Path("X:/tmp/图集_gui")
    )
    assert with_images["stats"]["chars"] == 0, with_images["stats"]
    assert with_images["content_state"] == "images_only", with_images["content_state"]

    without = _import_summary(
        {**base, "images_kept": 0}, book, outdir=Path("X:/tmp/图集_gui")
    )
    assert without["content_state"] == "empty", without["content_state"]


def case_content_state_rules_are_exhaustive():
    """三态互斥且覆盖全：有字就是 text，没字看有没有图，都没有才是 empty。"""
    from gui.api import _content_state

    assert _content_state(1, 0) == "text"
    assert _content_state(1, 9) == "text", "有正文时图再多也不算异常"
    assert _content_state(0, 1) == "images_only"
    assert _content_state(0, 0) == "empty"


# ---------- 2026-10-28 试用反馈：封面书名 / 原书封面 / 输出目录 ----------


def _fake_session(
    tmp: Path, *, source_cover: str | None = None, source_back: str | None = None
) -> Session:
    """造一个"清洗已经跑完"的会话，用来直接调 worker 里的函数。"""
    result = clean_text(SAMPLE, source="假书.txt")
    outdir = tmp / "假书_gui"
    outdir.mkdir(parents=True, exist_ok=True)
    session = Session(source=tmp / "假书.txt", result=result, outdir=outdir)
    if source_cover is not None:
        session.extra["source_cover"] = source_cover
    if source_back is not None:
        session.extra["source_back_cover"] = source_back
    return session


def case_book_meta_has_source_cover_default():
    """④ 页要判断「用原书封面」这个按钮显不显示，缺省必须是 False 而不是缺席。

    TXT 那条路没有原书封面；键缺席时前端 `meta.has_source_cover` 是 undefined，
    按钮会走 `!undefined` → 显示，用户点下去才发现没东西可用。
    """
    result = clean_text(SAMPLE, source="假书.txt")
    meta = _book_meta(result.book)
    assert "has_source_cover" in meta, sorted(meta)
    assert meta["has_source_cover"] is False, meta["has_source_cover"]
    # 原书封底同一条规矩：键必须在、缺省 False（TODO 0.2.2 第 5 条）
    assert meta["has_source_back"] is False, meta["has_source_back"]


def case_cover_for_build_uses_edited_title():
    """试用反馈第 1 条：封面书名要用 ④ 页改过的那个。

    原来 `_cover_for_build` 取 `session.result.book.title`，用户在 ④ 页把
    「正文首行猜出来的书名+推荐语」改成真书名后，封面还是印着旧的。
    这里用同一本书、只改 meta 里的 title，两张封面必须不一样。
    """
    from gui import cover as cover_mod

    if not cover_mod.available():
        print("     skip Pillow 不在，跳过本条（分母照算）")
        return
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        session = _fake_session(tmp)
        api = Api()

        first = api._cover_for_build(session, {"cover_mode": "auto", "title": "第一个名字"})
        assert first is not None and first.is_file(), first
        a = first.read_bytes()
        second = api._cover_for_build(session, {"cover_mode": "auto", "title": "第二个名字"})
        assert second is not None
        b = second.read_bytes()
        assert a != b, "书名不同，封面字节却一样 —— 说明用的还是 session 里的旧书名"


def case_preview_cover_uses_edited_title_and_author():
    """预览这一半的封面：④ 页改什么就画什么（试用反馈第 1 条）。

    三处断点都在这条路上：界面没给 m-author 挂监听、请求只带 title、
    后端只读 book.author。这里从最里层钉住 —— 传进去的 title/author
    必须原样交给画封面的那一层，没传才回落到清洗猜的值。
    """
    from gui import cover as cover_mod

    if not cover_mod.available():
        print("     skip Pillow 不在，跳过本条（分母照算）")
        return
    got: list[tuple[str, str]] = []
    real = cover_mod.make_look

    def fake_make_look(look, title, author="", **kw):
        got.append((title, author))
        return b"\x89PNG\r\n\x1a\n" + b"0" * 8

    cover_mod.make_look = fake_make_look
    try:
        api = Api()
        session = _session_for()
        api._sessions["j1"] = session
        # 清洗猜出来的值先摆好，才看得出「没传」时回落的是谁
        session.result.book.title = "猜出来的书名"
        session.result.book.author = "猜出来的作者"

        res = api.preview_cover("j1", {"title": "改过的书名", "author": "改过的作者"})
        assert not res.get("error"), res
        assert got[-1] == ("改过的书名", "改过的作者"), got[-1]

        # 只改书名：作者回落
        res = api.preview_cover("j1", {"title": "改过的书名"})
        assert not res.get("error"), res
        assert got[-1] == ("改过的书名", "猜出来的作者"), got[-1]

        # 两样都不传：都回落
        res = api.preview_cover("j1", {})
        assert not res.get("error"), res
        assert got[-1] == ("猜出来的书名", "猜出来的作者"), got[-1]
    finally:
        cover_mod.make_look = real


def case_ui_meta_page_sends_author_to_cover_preview():
    """界面上那两处断点同样要钉住（静态读源码）。

    JS 没法在无窗口环境里跑，但"监听有没有挂"和"请求里带没带 author"
    都是源码里一眼可见的事实 —— 漏了就是用户改名后封面不动。
    """
    src = _ui_source()
    assert 'el("m-author").addEventListener("input"' in src, "作者框没有 input 监听：改作者封面不会重画"
    start = src.index("function requestCoverPreview(")
    head = src[start:start + 500]
    assert 'title: el("m-title").value.trim()' in head, "预览请求没带书名"
    assert 'author: el("m-author").value.trim()' in head, "预览请求没带作者"


def case_preview_cover_keeps_the_look_on_screen():
    """改书名/作者时封面必须停在当前那张上（试用反馈第 1 条）。

    用户换了几个封面再改书名，画面跳回队列第一张。根因是界面重画时只带了
    title/author，后端走到 `first_look(title)` —— 队列按书名排序，换个书名
    起点也就换了。这条钉住后端契约的三种情形：
      1. 带 look、不带 step → 原样画它，不许换款；
      2. 带 step 且带 current → 从 current 往后走一格（不是重新回起点）；
      3. 什么都不带 → 确定性起点（第一次进 ④ 页那条路）。
    """
    from gui import cover as cover_mod

    if not cover_mod.available():
        print("     skip Pillow 不在，跳过本条（分母照算）")
        return
    queue = cover_mod.all_looks("甲书")
    assert len(queue) >= 3, f"队列太短，测不出三种情形：{len(queue)}"
    picked = queue[2]
    picked_key = cover_mod.look_key(picked)
    got: list[str] = []
    real = cover_mod.make_look

    def fake_make_look(look, title, author="", **kw):
        got.append(cover_mod.look_key(look))
        return b"\x89PNG\r\n\x1a\n" + b"0" * 8

    cover_mod.make_look = fake_make_look
    try:
        api = Api()
        session = _session_for()
        api._sessions["j1"] = session
        session.result.book.title = "甲书"
        session.result.book.author = "某人"

        # 1. 改书名触发的重画：带上屏幕上那张，画出来必须还是它
        res = api.preview_cover("j1", {"title": "改过的书名", "look": picked})
        assert not res.get("error"), res
        assert got[-1] == picked_key, f"改书名后换了款：{got[-1]} != {picked_key}"
        assert res.get("look_key") == picked_key, res.get("look_key")

        # 2. 「换一个」：从当前那张往后走，不是回队列第一项
        seen = [cover_mod.look_key(x) for x in queue[:3]]
        res = api.preview_cover("j1", {"title": "甲书", "step": True,
                                       "current": picked_key, "seen": seen})
        assert not res.get("error"), res
        want = cover_mod.look_key(cover_mod.next_look("甲书", picked_key, seen))
        assert got[-1] == want, f"换一个没从当前那张往后走：{got[-1]} != {want}"
        assert got[-1] != cover_mod.look_key(queue[0]), "换一个跳回队列第一张了"

        # 3. 什么都不带：回确定性起点（第一次进 ④ 页）
        res = api.preview_cover("j1", {"title": "甲书"})
        assert not res.get("error"), res
        assert got[-1] == cover_mod.look_key(cover_mod.first_look("甲书")), got[-1]
    finally:
        cover_mod.make_look = real


def case_build_uses_the_look_on_screen():
    """④ 页当前那张封面必须原样打进成品，而不是回落到队列第一项。

    `_cover_for_build` 原来只认 `cover_mode`，meta 里根本没有 `cover_look`
    这个键 —— 预览换了五张，打出来的永远是第一张。
    """
    from gui import cover as cover_mod

    if not cover_mod.available():
        print("     skip Pillow 不在，跳过本条（分母照算）")
        return
    import tempfile

    queue = cover_mod.all_looks("乙书")
    picked = queue[1]

    with tempfile.TemporaryDirectory() as td:
        session = _fake_session(Path(td))
        session.result.book.title = "乙书"
        api = Api()
        path = api._cover_for_build(session, {
            "title": "乙书", "author": "某", "cover_mode": "auto", "cover_look": picked,
        })
        assert path is not None and Path(path).is_file(), "当前那张封面没落盘"
        want = cover_mod.make_look(picked, "乙书", "某")
        assert Path(path).read_bytes() == want, "成品封面不是屏幕上那张"

        # 没带 cover_look（老调用方 / 第一次进页）→ 回落确定性起点，不能炸
        path2 = api._cover_for_build(session, {"title": "乙书", "cover_mode": "auto"})
        assert path2 is not None and Path(path2).is_file(), "没带 cover_look 就打不出封面了"
        assert Path(path2).read_bytes() == cover_mod.make_look(
            cover_mod.first_look("乙书"), "乙书", ""), "回落的不是确定性起点"


def case_back_cover_goes_into_the_package():
    """封底跟封面同一款：auto 才有，图片自己挑的两种没有（我们不知道纸纹底色）。

    封底是包里的一页，必须走 build_epub 的 back_cover 参数；不给就是 None，
    这一维不影响老产物（不产封底的包与传 None 完全一致）。
    """
    from gui import cover as cover_mod

    if not cover_mod.available():
        print("     skip Pillow 不在，跳过本条（分母照算）")
        return
    import tempfile
    import zipfile

    queue = cover_mod.all_looks("丙书")
    picked = queue[2]

    with tempfile.TemporaryDirectory() as td:
        session = _fake_session(Path(td))
        session.result.book.title = "丙书"
        api = Api()

        # 1) auto + 当前那张 → 落盘 back.png，字节与 make_back_cover 一致
        path = api._back_for_build(session, {
            "title": "丙书", "author": "某", "cover_mode": "auto", "cover_look": picked,
        })
        assert path is not None and Path(path).is_file(), "封底没落盘"
        assert Path(path).read_bytes() == cover_mod.make_back_cover(picked), \
            "封底不是封面那款（版式/配色对不上）"

        # 2) 没带 cover_look → 回落确定性起点，不能炸
        path2 = api._back_for_build(session, {"title": "丙书", "cover_mode": "auto"})
        assert path2 is not None and Path(path2).is_file(), "没带 cover_look 就打不出封底了"
        assert Path(path2).read_bytes() == cover_mod.make_back_cover(
            cover_mod.first_look("丙书")), "回落的不是封面的确定性起点"

        # 3) 自选图 / 原书封面 / 没选封面 → 一律没有封底
        for meta in ({"cover_mode": "image"}, {"cover_mode": "source"},
                     {"cover_mode": ""}, {}):
            assert api._back_for_build(session, meta) is None, meta

        # 4) 端到端：打包产物里真的有封底页，且排在正文之后
        job = Job(job_id="t-back")
        api._sessions["t-back"] = session
        api._run_build(job, session, {
            "title": "丙书", "author": "某", "language": "zh-CN", "fonts": False,
            "cover_mode": "auto", "cover_look": picked,
        })
        assert job.error == "", job.error
        epub_path = Path(job.payload["epub"])
        names = zipfile.ZipFile(epub_path).namelist()
        assert "OEBPS/Text/back.xhtml" in names, names
        assert names[-1] == "OEBPS/Text/back.xhtml", "封底页该排在最后一章之后"
        assert any(n.startswith("OEBPS/Images/back.") for n in names), names


def case_back_cover_falls_back_to_the_imported_one():
    """非自动配色时把原书封底贴回去（TODO 0.2.2 第 5 条）。

    原书封底是宣传渠道，导入一次丢一次，等于每回环一轮少一样东西。
    自选图 / 原书封面这两种"用自己的图"的选择不该硬画一张米白底封底，
    但原书既然自带封底，就把那张图**原样**贴回去 —— 字节必须一模一样。
    """
    from gui import cover as cover_mod

    if not cover_mod.available():
        print("     skip Pillow 不在，跳过本条（分母照算）")
        return
    import tempfile
    import zipfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        png = cover_mod.make_back_cover(cover_mod.first_look("丁书"))
        src_back = tmp / "原书封底.png"
        src_back.write_bytes(png)
        session = _fake_session(tmp, source_back=str(src_back))
        session.result.book.title = "丁书"
        api = Api()

        # 1) 不画封底的四种选择 → 都回落原书封底那张
        for meta in ({"cover_mode": "image"}, {"cover_mode": "source"},
                     {"cover_mode": ""}, {}):
            got = api._back_for_build(session, meta)
            assert got is not None and Path(got) == src_back, (meta, got)

        # 2) auto 仍然现画：原图只做"画不出来时的垫背"，不冒充同款封底
        drawn = api._back_for_build(session, {"title": "丁书", "cover_mode": "auto"})
        assert drawn is not None and Path(drawn).name == "back.png", drawn
        assert Path(drawn).read_bytes() == cover_mod.make_back_cover(
            cover_mod.first_look("丁书")), "auto 拿原书封底充数了"

        # 3) 端到端：用原书封面模式打包，封底页与图都在，且图字节未变
        job = Job(job_id="t-src-back")
        api._sessions["t-src-back"] = session
        api._run_build(job, session, {
            "title": "丁书", "author": "某", "language": "zh-CN", "fonts": False,
            "cover_mode": "source",
        })
        assert job.error == "", job.error
        with zipfile.ZipFile(Path(job.payload["epub"])) as zf:
            names = zf.namelist()
            assert "OEBPS/Text/back.xhtml" in names, names
            backs = [n for n in names if n.startswith("OEBPS/Images/back.")]
            assert len(backs) == 1, names
            assert zf.read(backs[0]) == png, "封底图被打包层改过，字节对不上"


def case_ui_cover_caption_has_no_queue_position():
    """封面右下角那句「3 / 45」按试用反馈删了（试用反馈第 2 条）。

    只删文案不够：那个 span 与它的 CSS 都要走，否则界面上留个空格子；
    后端 index/total 两个键留着（老调用方还在读），但不许再有人拿它拼分母。
    """
    src = _ui_source()
    assert "cover-cap-seq" not in src, "右下角那个位次 span 还在"
    assert ".cover-cap .seq" not in src, "位次那格 CSS 还在"
    assert "coverCap(name, seq)" not in src, "coverCap 还留着第二个形参"
    assert 'res.index + " / " + res.total' not in src, "还在拼「第几张 / 共几张」"


def case_ui_cover_redraw_keeps_the_current_look():
    """界面重画封面时必须把屏幕上那张一起发过去（试用反馈第 1 条，静态读源码）。

    这一类断点只能靠读源码钉：JS 不跑，但「发没发 look」是源码里一眼可见的事实。
    """
    src = _ui_source()
    start = src.index("function requestCoverPreview(")
    head = src[start:start + 900]
    assert "if (coverState.look) { opts.look = coverState.look; }" in head, \
        "重画请求没带当前那张 look —— 后端只能回落队列第一张"
    assert "requestCoverPreview(false)" in src, "防抖重画那条路没标明不是「换一个」"
    assert "requestCoverPreview(true)" in src, "「换一个」那条路断了"


def case_author_box_defines_the_author_everywhere():
    """④ 页作者框是唯一真相：清空 = 不写作者（不回落清洗猜的），
    没带 author 键才回落。预览、成品封面、EPUB 三处必须同口径（review a）。
    """
    from gui import cover as cover_mod
    from gui.api import _meta_author

    # 纯函数部分：带键（含空串）用框里的，没带键才回落猜的。
    book = _session_for().result.book
    book.author = "清洗猜的作者"
    assert _meta_author({"author": "填过的作者"}, book) == "填过的作者"
    assert _meta_author({"author": ""}, book) == "", "清空作者框不该回落猜的作者"
    assert _meta_author({"author": "  "}, book) == ""
    assert _meta_author({}, book) == "清洗猜的作者", "没带键才回落导入时猜的值"
    assert _meta_author({}, type("B", (), {})()) == "", "没有 author 属性也不能炸"

    if not cover_mod.available():
        print("     skip Pillow 不在，跳过封面那一半（分母照算）")
        return
    got: list[tuple[str, str]] = []
    real = cover_mod.make_look

    def fake_make_look(look, title, author="", **kw):
        got.append((title, author))
        return b"\x89PNG\r\n\x1a\n" + b"0" * 8

    cover_mod.make_look = fake_make_look
    try:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            session = _fake_session(tmp)
            session.result.book.author = "清洗猜的作者"
            api = Api()
            api._sessions["j1"] = session

            # ① 预览：清空框 → 不画作者
            res = api.preview_cover("j1", {"title": "书名", "author": ""})
            assert not res.get("error"), res
            assert got[-1] == ("书名", ""), got[-1]

            # ② 成品封面：同一份 meta 清空 → 也不画作者
            got.clear()
            api._cover_for_build(session, {"cover_mode": "auto", "title": "书名", "author": ""})
            assert got[-1] == ("书名", ""), got[-1]

            # ③ 打包：_book.author 也不该被写回成猜的值（write_epub 会读它）
            job = Job(job_id="t-author")
            api._run_build(job, session, {"title": "书名", "author": "", "fonts": False})
            assert job.error == "", job.error
            assert session.result.book.author == "", session.result.book.author

            # 反面对照：没带 author 键 → 三处都回落猜的作者
            session.result.book.author = "清洗猜的作者"
            got.clear()
            api.preview_cover("j1", {"title": "书名"})
            assert got[-1] == ("书名", "清洗猜的作者"), got[-1]
            got.clear()
            api._cover_for_build(session, {"cover_mode": "auto", "title": "书名"})
            assert got[-1] == ("书名", "清洗猜的作者"), got[-1]
            job2 = Job(job_id="t-author2")
            api._run_build(job2, session, {"title": "书名", "fonts": False})
            assert job2.error == "", job2.error
            assert session.result.book.author == "清洗猜的作者", session.result.book.author
    finally:
        cover_mod.make_look = real


def case_cover_source_without_file_returns_none():
    """「用原书封面」但会话里没有封面文件时必须返回 None，不能抛。

    封面失败不该阻断打包：调用方（`_run_build`）把 None 当"无封面"继续走。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        session = _fake_session(Path(td))
        api = Api()
        assert api._cover_for_build(session, {"cover_mode": "source"}) is None
        # 路径写了但文件不存在，同样只能返回 None
        session.extra["source_cover"] = str(Path(td) / "nope.jpg")
        assert api._cover_for_build(session, {"cover_mode": "source"}) is None


def case_cover_modes_none_and_image_default_to_none():
    """没选封面 / 选了自选图但还没挑图，都返回 None（= 阅读器文字封面）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        session = _fake_session(Path(td))
        api = Api()
        assert api._cover_for_build(session, {}) is None
        assert api._cover_for_build(session, {"cover_mode": ""}) is None
        assert api._cover_for_build(session, {"cover_mode": "image"}) is None


def case_build_writes_epub_to_custom_outdir():
    """试用反馈第 7 条：输出目录可自定义。

    成品 EPUB 落到 meta["outdir"]，而中间产物（book.json / 报告）仍在会话目录 ——
    这两件事分开：中间产物是"这次清洗的证据"，成品是"用户要的东西"。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        session = _fake_session(tmp)
        api = Api()
        job = Job(job_id="t1")
        custom = tmp / "别处" / "更深一层"
        api._run_build(job, session, {
            "title": "自定义目录", "author": "某人", "language": "zh-CN",
            "fonts": False, "outdir": str(custom),
        })
        assert job.error == "", job.error
        payload = job.payload
        # 比 resolve() 后的路径：临时目录可能落在 8.3 短名下，
        # 后端回传的是 expanduser+mkdir 之后的那份，字面量不同但指向同一处。
        assert Path(payload["outdir"]).resolve() == custom.resolve(), payload["outdir"]
        assert (custom / "自定义目录.epub").is_file(), sorted(p.name for p in custom.iterdir())
        # 中间产物仍在会话目录里
        assert (session.outdir / "book.json").is_file()


def case_build_outdir_falls_back_when_unusable():
    """输出目录填了个不能用的值（这里是空串）→ 退回会话目录，不能因此打包失败。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        session = _fake_session(tmp)
        api = Api()
        job = Job(job_id="t2")
        api._run_build(job, session, {"title": "退回默认", "fonts": False, "outdir": ""})
        assert job.error == "", job.error
        assert job.payload["outdir"] == str(session.outdir), job.payload["outdir"]


def case_book_glyphs_are_the_books_own_characters():
    """付梓动画的字模必须取自这本书（试用反馈第 6 条）。

    界面里写死过一串《兰亭集序》开头（"永和九年岁在癸丑……"）当兜底，而
    后端从来没给过字模 —— 于是每本书印出来的都是同一批字，用户一眼看出不对。
    取法也改过一次：先按频次取高频字，结果每本书都是"的一了他"，用户要求
    改成"从书里抽一段真实文字"。这里钉住：抽的是正文里连续的一段、只认
    body 段、同一本书每次一样、没有正文时返回空串（前端退回兜底）。
    """
    from huoban.model import Book, Chapter, Paragraph

    book = Book(title="三国演义")
    ch = Chapter(title="第一回")
    ch.paragraphs = [
        Paragraph(text="话说天下大势，分久必合，合久必分。"),
        Paragraph(text="分久必合，合久必分。"),
    ]
    book.chapters = [ch]

    got = _book_glyphs(book, n=5)
    # 抽的是一段真话，不是高频字：必须是正文里连续的一段
    assert got == "话说天下大", got
    assert "永" not in got, f"兜底那串的字混进来了：{got}"
    # 同一本书每次抽到的必须一样，否则每次付梓都换一套字
    assert _book_glyphs(book, n=5) == got, "两次抽到的字模不一致"
    # 一个字都没有（纯外文/空书）→ 空串，交给前端兜底
    assert _book_glyphs(Book()) == ""
    assert _book_glyphs(Book(chapters=[Chapter(title="x", paragraphs=[Paragraph(text="hello world")])])) == ""
    # 只认 body：标题与诗词不进字模
    verse_only = Book(chapters=[Chapter(title="x", paragraphs=[
        Paragraph(text="床前明月光", kind="verse"),
        Paragraph(text="疑是地上霜", kind="heading"),
    ])])
    assert _book_glyphs(verse_only) == "", "标题/诗词被抽进字模了"
    # 段落都短（对话体）：从 40% 处把汉字接着攒，但**同样的字只收一次** ——
    # 十段"好"只印得出一枚「好」。字模表里不许有重字（试用反馈第 3 条：
    # 付梓那一屏印重复的字，看着像卡住了）。
    short = Book(chapters=[Chapter(title="x", paragraphs=[
        Paragraph(text="好") for _ in range(10)
    ])])
    assert _book_glyphs(short, n=3) == "好", _book_glyphs(short, n=3)
    # 攒不够 n 个就返回已有的那几枚，不拿重复字凑数
    mixed = Book(chapters=[Chapter(title="x", paragraphs=[
        Paragraph(text="甲乙丙丁戊")
    ])])
    assert _book_glyphs(mixed, n=48) == "甲乙丙丁戊", _book_glyphs(mixed, n=48)
    # 长段落里有重字时，出来的是保序去重后的结果（不是截前 n 个）
    repeat = Book(chapters=[Chapter(title="x", paragraphs=[
        Paragraph(text="天地玄黄天地玄黄宇宙洪荒")
    ])])
    assert _book_glyphs(repeat, n=8) == "天地玄黄宇宙洪荒", _book_glyphs(repeat)


def case_ui_ink_glyphs_come_from_the_payload():
    """付梓那一屏读的是后端给的 payload.glyphs，且不许再印写死的古文。

    试用反馈：0 章书取不到真字时前端回落《兰亭集序》，每本书印出来都是
    "永和九年岁在癸丑"，一眼假。现在取不到就一枚字模都不印。
    """
    src = _ui_source()
    assert "currentPayload.glyphs" in src, "前端没有读后端给的 glyphs"
    assert "BG_GLYPHS" not in src, "写死的《兰亭集序》兜底没删干净"
    assert "永和九年岁在癸丑" not in src, "写死的古文还留在界面里"
    assert "return \"\";" in src, "取不到真字时应返回空串"
    assert "var ch = bgNextGlyph();" in src, "bgBornInk 要从 bgNextGlyph 取字"
    assert "if (!ch) { return; }" in src, "bgBornInk 要在空字源时不造字"
    assert "if (!bgGlyphSource())" in src, "bgTickInk 要在空字源时跳过造字"
    # 字模表绕圈时不许把同一个字印两遍（试用反馈第 3 条）：取字要跳过在场那些
    assert "function bgNextGlyph()" in src, "取字要单独成函数，才测得住"
    assert "live[bgSlots[i].gl.textContent] = true;" in src, "取字没把在场字记进黑名单"


def case_book_glyphs_fall_back_to_front_matter_when_no_chapters():
    """0 章书正文全在 front_matter，付梓页也要能印这本书自己的真字。

    实测《书K》旧版认不出装饰边框标题 → 0 章 → glyphs 空 → 前端
    印兰亭集序。现在后端补 front_matter 回落，纯外文仍返回空串。
    """
    from huoban.model import Book, Chapter, Paragraph

    no_chapter = Book(title="短篇")
    no_chapter.front_matter = [
        Paragraph(text="他推开门走进屋里。"),
        Paragraph(text="窗外下着大雪，风把窗棂吹得直响。"),
        Paragraph(text="她端来一碗热茶，放在桌上没有说话。"),
    ]
    got = _book_glyphs(no_chapter, n=8)
    assert got and "永" not in got, got
    assert got == "窗外下着大雪风把", got
    # 有章节时仍然只认 chapters，不把前言混进来
    with_ch = Book(title="有章")
    with_ch.front_matter = [Paragraph(text="本书由某某整理，切勿印我。")]
    ch = Chapter(title="第一章")
    ch.paragraphs = [Paragraph(text="话说天下大势，分久必合，合久必分。")]
    with_ch.chapters = [ch]
    assert "某某" not in _book_glyphs(with_ch, n=8)
    # 纯外文：一个汉字都没有，仍然空串
    foreign = Book()
    foreign.front_matter = [Paragraph(text="hello world, no chinese at all.")]
    assert _book_glyphs(foreign) == ""


def case_review_payload_gives_every_row_key_for_select_all():
    """复核页的「全选」必须能真的全选（试用反馈第 5 条）。

    以前界面的全选只遍历已经加载的那 40 行，用户得一直往下滚、把每页都
    拉出来才可能全选上 —— 他自己就是这么描述的。所以 payload 必须把每一类
    的**全量**行号一次给全（keys），与分页无关；只有能勾的两类给。
    """
    from types import SimpleNamespace

    from huoban.model import Book

    report = {
        "dropped_lines": [
            {"line": i, "text": "广告 %d" % i, "kind": "ad"} for i in range(1, 61)
        ]
    }
    result = SimpleNamespace(report=report, book=Book(title="测试书"), merges=[], text="")
    session = SimpleNamespace(result=result, adjust=None, extra={}, source=Path("x.txt"))

    payload = _review_payload(session)
    del_g = next(g for g in payload["groups"] if g["key"] == "del")
    # 只送 40 行给列表，但 keys 必须是 60 个 —— 这正是「真全选」的全部依据
    assert len(del_g["rows"]) == REVIEW_PAGE, len(del_g["rows"])
    assert del_g["more"] is True
    assert del_g["keys"] == list(range(1, 61)), del_g["keys"][:5]
    # 只能看的三类不给 keys：给一个勾不动的行号集合只会让人以为能勾
    for g in payload["groups"]:
        if not g["act"]:
            assert g["keys"] == [], (g["key"], g["keys"][:3])
    # 前端必须用这份全量 keys，而不是退回按已加载的行拼
    src = _ui_source()
    assert "function rvAllKeys" in src and "rvAllKeys(g).forEach" in src, "全选没接全量 keys"


def case_review_zero_group_lights_up_without_a_click():
    """数字是 0 的那一类，小灯直接点亮，不用点一遍（2026-11-22 开发者定）。

    依据：数字 0 就代表这一类没有东西要复核，让人先点一下才亮是多余动作。
    连带两件事必须一起改，否则界面自相矛盾：
      ① 灯的判据要认 num === 0（不然灯亮着、按钮还不转绿）；
      ② `rvAllSeen()` 也要把 num === 0 算成已看（不然五类全 0 的干净书，
         按钮永远不转绿 —— 而那本书根本没有东西可看）。
    只改「什么时候亮」，灯的尺寸、颜色、过渡一个字不动。
    """
    src = _ui_source()

    # 灯的判据：两种亮法 —— 点过了，或者这一类本来就是 0
    dot = src[src.find('var dot = document.createElement("span");'):]
    dot = dot[:dot.find("host.appendChild(b);")]
    assert 'dot.className = "sk-seen"' in dot, "灯的类名被人改了"
    assert "rv.seen[g.key]" in dot, "点过才亮的判据被删了"
    assert "g.num === 0" in dot, "0 的那一类没有直接点亮"
    assert '" on"' in dot, "点亮用的类名没了"

    # 按钮转绿的判据必须和灯一致
    allseen = src[src.find("function rvAllSeen()"):]
    allseen = allseen[:allseen.find("\n  }")]
    assert "rv.seen[g.key]" in allseen, "已看判据没接上"
    assert "g.num === 0" in allseen, "0 的类没算成已看：干净书按钮永远不转绿"

    # 设计定稿的三样东西不许被顺手改
    assert ".sk-seen.on { background: var(--seen); opacity: 1; }" in src, \
        "灯的颜色被改了（设计已定稿）"
    assert "width: 5px; height: 5px" in src, "灯的尺寸被改了（设计已定稿）"


# ---------- 界面（gui/ui.html）的静态不变量 ----------
#
# 这一组不启动窗口，只读 ui.html 的文本。上一轮 review 抓出三个「界面明明有、
# 但从没人调用」的高危断线（复核页整屏不可达、完成页 currentPayload 从不更新、
# 轮询不认门牌），全都能靠这几条断言当场拦住 —— 而它们在 test_gui.py 里
# 一条都没有，因为以前只测后端。


def _ui_source() -> str:
    return (ROOT / "gui" / "ui.html").read_text(encoding="utf-8")


def case_ui_drop_listener_blocks_the_default_action():
    """`drop` 监听器里必须有 `ev.preventDefault()` —— 默认行为要在 JS 侧兜住。

    为什么这条是硬要求（2026-11-22 定因，证据在 pywebview 源码里）：
    Python 侧那条路要 `num_listeners > 0` 才起效
    （`webview/platforms/edgechromium.py` 的 `on_script_notify`：
    `if _dnd_state['num_listeners'] == 0: return`），而它从
    `webview/util.py` 起线程注入运行时就已开始接收鼠标 —— 从那一刻到
    `gui/app.py` 的 `_register_drop` 做完两次 `evaluate_js` 之间，有一段
    「默认行为无人拦」的窗口期。这期间拖进第一本书，WebView2 的
    `AllowExternalDrop`（默认 True，pywebview 从不设置）会接管，表现为
    偶发弹出系统的「打开文件」窗口。

    JS 侧补一句 `preventDefault` 就与 Python 侧是否就绪无关了。
    **它不会妨碍取绝对路径**：`webview/dom/element.py` 生成的 handler 是
    `function(e){ e.preventDefault(); window.pywebview._jsApiCallback(...) }` ——
    `preventDefault` 在前、取路径的 `postMessageWithAdditionalObjects` 在后，
    同一个函数内，且 `preventDefault` 只取消默认动作、不阻止事件送达别的监听器。

    所以别再把它删掉：删了这句，偶发弹窗会回来，而用例是唯一拦得住的地方。
    """
    src = _ui_source()
    # 找到 drop 监听器那一段（从 addEventListener("drop" 到它的结束 }）, false);）
    start = src.find('document.addEventListener("drop"')
    assert start > 0, "ui.html 里找不到 drop 监听器"
    tail = src[start:]
    end = tail.find("}, false);")
    assert end > 0, "drop 监听器没有以 }, false); 收尾，形态变了要同步改用例"
    block = tail[: end + 1]
    assert "preventDefault" in block, (
        "drop 监听器里没有 preventDefault —— 窗口期内默认行为无人拦，"
        "拖第一本书会偶发弹出系统「打开文件」窗口。"
        "注意它只拦默认动作，不影响 Python 侧取绝对路径。"
    )
    # 光有字符串不够：必须真的调在 ev 上
    assert re.search(r"\b(ev|e|event)\.preventDefault\(\)", block), (
        "drop 监听器里出现了 preventDefault 但没有调用在事件对象上：" + block[:200]
    )


def case_ui_every_button_has_a_listener():
    """每颗按钮都得有人监听。

    ③ 复核页栽在这里：`renderReview` 有定义、`setView("review")` 也只写在它
    内部，但 `#btn-review` 从来没挂过监听 —— 按钮亮着、点了没反应，整屏不可达。
    写完一个视图很容易漏掉「接电」，这条就是拿电笔挨个点一遍。
    """
    src = _ui_source()
    ids = sorted(set(re.findall(r'id="(btn-[a-z0-9-]+)"', src)))
    assert len(ids) >= 15, "按钮数量不对劲：" + str(len(ids))
    dead = [b for b in ids if 'el("' + b + '").addEventListener' not in src]
    assert not dead, "这些按钮没有监听：" + repr(dead)


def case_ui_every_view_is_reachable():
    """`setView` 的每个目标都必须在 CSS 里登记过，反过来也要有人去。

    漏一个方向的后果不一样：CSS 里没登记 → 切过去一片空白；
    从来没人调用 → 那一屏是死的（复核页就是「没人调用」那一侧）。
    """
    src = _ui_source()
    declared = set(re.findall(r'body\[data-view="([a-z]+)"\]', src))
    reached = set(re.findall(r'setView\("([a-z]+)"\)', src))
    assert not (reached - declared), "切到了没登记的视图：" + repr(sorted(reached - declared))
    for view in ("result", "review", "meta", "done"):
        assert view in reached, "没有任何地方切到 " + view


def case_ui_done_page_keeps_its_payload():
    """完成页开头必须把 payload 记下来。

    这一行是那页所有按钮的命根子：「打开这本书」要 payload.epub、
    「打开所在目录」要 payload.epub || payload.outdir。漏了它，两个按钮
    一个静默 return、一个开错目录，后端辛苦算的 epub/stem/cover 全白给。
    """
    src = _ui_source()
    start = src.index("function renderDone(payload)")
    head = src[start : start + 900]
    assert "currentPayload = payload" in head, "renderDone 忘了记 payload"


def case_ui_polling_respects_the_job_badge():
    """两处轮询都得认门牌，否则「取消」等于没取消。

    `stopPolling` 只清得掉 setTimeout，清不掉已经在飞的 promise —— 用户点了
    取消，三百毫秒后那个 promise 照样落地：done=false 就把轮询自己接回来，
    done=true 就把人弹回结果页。
    """
    src = _ui_source()
    n = src.count("jobId !== activeJobId")
    assert n >= 4, "轮询的门牌校验少了（then/catch 各一道，两处轮询共四道）：" + str(n)


def case_ui_has_no_external_links():
    """零外链。唯一允许出现的是 SVG 命名空间（内联 data URI 的必需属性）。

    这个界面是 html= 一次性注入的单文件，一旦引了外部资源，断网就白屏。
    """
    src = _ui_source()
    # 引号在这条正则里两头都要排掉，直接在源码里写会打架，拼出来。
    stop = chr(34) + chr(39) + "\\s<>"
    urls = re.findall("https?://[^" + stop + "]+", src)
    for url in urls:
        assert url == "http://www.w3.org/2000/svg", "出现了外链：" + url


def case_ui_is_one_lf_file():
    """单文件、只有一个 script 块、行尾全 LF。

    `html=_read_ui()` 只在 app.py 里注入一次；CRLF 混进来会让 `node --check`
    与所有按行 grep 的工具出怪事（本项目一直只认 LF）。
    """
    raw = (ROOT / "gui" / "ui.html").read_bytes()
    assert b'\r' not in raw, "ui.html 里混进了 CRLF"
    src = raw.decode("utf-8")
    assert src.count("<script") == 1, "script 块不止一个"
    assert "<link " not in src, "不该有 <link>（会引外部样式）"
    assert "<img src=\"http" not in src, "不该有远程图片"


# ---------- 首屏拖放：一团墨跟手（第 3 组） ----------
#
# 动效本身是 CSS，测试看不见画面；能钉住的是「三态的字都在」「分类只看 MIME」
# 「离开一定会清态」这三件会真正咬人的事 —— 少一个字，用户拖进来就只看到
# 一团墨和一句旧话；分类里混进扩展名判断，.txt 就会被误判成"不是书"；
# 清态漏一条路径，拖到框外墨团会一直挂在屏幕上。


def case_ui_drop_has_welcome_state():
    """悬停只有一个迎宾态：一团跟手的墨 + 「请松手」。

    2026-11-17 开发者定：拖拽悬停阶段读不到文件名（实测只有 types:["Files"]
    与 item.type，getAsFile()/webkitGetAsEntry() 都是 null），书/非书判不出来，
    所以不再分三档；拖文件进来只有这一句。
    """
    src = _ui_source()
    assert '<span class="t-idle">把书拖到这里</span>' in src, "首屏平时那句没了"
    assert '<span class="t-drag">请松手</span>' in src, "迎宾态文案不对（应为「请松手」）"
    # 平时态默认可见，迎宾态默认藏起来
    assert ".drop-title .t-idle { opacity: 1" in src, "平时态没有默认可见"
    assert '#drop-area[data-drop="drag"] .drop-title .t-drag' in src, "迎宾态没接上"
    # 换态时平时那句要退场，否则两句话叠在一起
    assert '#drop-area[data-drop="drag"] .drop-title .t-idle' in src, "迎宾时平时那句没退场"
    # 旧的三档判词一个都不许留
    for gone in ("书稿已得", "非书也", "t-book", "t-other"):
        assert gone not in src, "旧的三档判词还在：" + gone


def case_ui_drop_does_not_classify_during_drag():
    """悬停期不许再按 MIME 判书/非书（0.2.3 新口径，2026-11-17）。

    实测：dragenter/dragover 里 getAsFile()/webkitGetAsEntry() 全是 null，
    .lnk 连 MIME 都没有 —— 判了也是猜。判词退场，交给松手后的流程。
    """
    src = _ui_source()
    for gone in ("var BOOK_MIME", "var NOT_BOOK", "function classifyDrag", "function kindsOf"):
        assert gone not in src, "悬停期的 MIME 判据还留着：" + gone
    drag = src[src.find('["dragenter", "dragover"].forEach'):]
    drag = drag[:drag.find('["dragleave", "dragend"].forEach')]
    assert "it.type" not in drag and "item.type" not in drag, "悬停处理仍读 MIME"


def case_ui_drop_state_holds_until_verdict():
    """0.2.3 第 2 条：松手瞬间不许先跳回初始态，状态要撑到结论到达。

    根因：drop 监听无条件清态，而切屏要等 Python 回调 __onDropPath，
    中间隔着一次 JS→Python→JS 往返，界面会露一下「把书拖到这里」。

    三条要求（2026-11-14 口径，2026-11-17 迎宾态沿用）：
      1. drop 时若当前已是迎宾态（data-drop="drag"），**保持**到 __onDropPath；
      2. 兜底：600–800ms 内没等到 __onDropPath（拖的是网页文字/图片，Python
         取不到路径）就自己清态并复位墨团；
      3. __onDropPath 到达先取消定时器，别让它在下一屏里再动一次状态。
    另外「完全没表态」（state 为空）时仍应立即清掉，不留残影。
    """
    src = _ui_source()
    assert 'area.removeAttribute("data-drop")' in src, "没有清掉 data-drop"
    assert 'document.body.setAttribute("data-drop-active", state ? "1" : "0")' in src, \
        "没复位 body 上的拖拽标记（主题按钮与「关于」的透明度就回不来）"

    i = src.find("// 松手必须收墨")
    assert i >= 0, "找不到松手那一段（注释被删了？）"
    block = src[i:i + 2200]

    assert "var dropClearTimer = null;" in block, "没有兜底定时器的变量"
    assert 'var state = area.getAttribute("data-drop");' in block, \
        "drop 时没读当前状态，无法判断该不该保持"
    assert 'if (state === "drag")' in block, \
        "迎宾态没有保持状态（又回到无条件清态）"
    m = re.search(r"setTimeout\(function \(\) \{[^}]*\}, (\d+)\);", block)
    assert m, "没有挂兜底定时器"
    assert 600 <= int(m.group(1)) <= 800, "兜底定时器不在 600–800ms：" + m.group(1)
    assert 'setDropState("");' in block, "没表态时没有立即清态"

    j = src.find("window.__onDropPath = function (path) {")
    assert j >= 0, "找不到 __onDropPath"
    tail = src[j:j + 500]
    assert "clearTimeout(dropClearTimer)" in tail, "__onDropPath 没取消兜底定时器"
    assert 'setDropState("");' in tail, "松手进导入时没清态"

    assert 'if (name === "dragleave" && ev.relatedTarget) { return; }' in src, \
        "子元素之间移动会误清态（或反之根本没兜底）"
    assert 'document.body.getAttribute("data-view") !== "drop"' in src, \
        "别的屏也会冒墨团"
    # 墨心跟手：坐标要写成相对 #drop-area 的像素值，写死百分比就不跟手了
    assert "getBoundingClientRect()" in src and 'style.setProperty("--x"' in src, \
        "墨团没跟光标"


def case_ui_drag_colors_come_from_cover_palette():
    """五支拖拽色必须来自封面色板 + 墨，深浅两档齐全（2026-11-17 定）。

    不是「随便找五个好看的颜色」：四支印色取自 `gui/cover.py` 的六套配色，
    用户之后挑封面会见到同一套；墨是兜底色。深浅各一档，跟着主题走。
    """
    src = _ui_source()
    m = re.search(r"var DRAG_COLORS = \[(.+?)\n  \];", src, re.S)
    assert m, "找不到五色表 DRAG_COLORS（或被改成了别处）"
    block = m.group(1).lower()

    # 浅色档：四支印色 + 墨
    lights = ("#a83a2e", "#2c6c5f", "#35577d", "#8e4b26", "#1b1d1e")
    # 深色档：提亮后的同一套
    darks = ("#d65e4d", "#6fa79a", "#8fb0d6", "#c98a5b", "#e8e4da")
    for hexv in lights + darks:
        assert hexv in block, "五色表少了这一档：" + hexv

    # 四支印色必须真在封面色板里（同源，不是新造）
    cover = (ROOT / "gui" / "cover.py").read_text(encoding="utf-8")
    for hexv in ("#2C6C5F", "#35577D", "#8E4B26"):
        assert hexv in cover, "这支色不在封面色板里：" + hexv

    # 不许出现通用软件的语义色
    for bad in ("#00ff00", "#ff0000", "#22c55e", "#ef4444"):
        assert bad not in block, "拖拽段里出现了通用语义色：" + bad


def case_ui_drop_first_frame_snaps_into_place():
    """第一次显形那一帧不许有位移过渡，否则墨团会从左上角飞过来。

    兜底的 --x/--y 是 50%，那是相对墨团自身 560px 算的，落在区域左上 280,280；
    带着 420ms 的 transform 过渡显形，第一眼就是"从别处飘到光标"。所以首次写
    坐标要挂 bloom-snap 关掉过渡，写完逼一次样式计算让坐标当场就位，再摘掉类。
    只钉实现不变量，不钉具体毫秒数。
    """
    src = _ui_source()
    assert "#drop-area.bloom-snap .bloom { transition: opacity 300ms linear; }" in src, \
        "没有关位移过渡的口子：首帧墨团会从兜底位置飘过去"
    assert "#drop-area.bloom-snap .bloom { transition: none;" not in src, \
        "连淡入也掐了：墨团会硬跳出来"
    # 关过渡 / 写坐标 / 逼样式 / 摘类的顺序不能乱：先摘类再写坐标就白关了
    body = src[src.find("function placeBloom"):]
    body = body[:body.find("function setDropState")]
    # 「第一次」必须靠自己的标记判：placeBloom 是在 setDropState 之后调的，
    # 那时候 data-drop 已经挂上了，拿它判永远判不出首帧
    assert "var bloomSeated = false;" in src, "没有记「这一次是否已经就位」的标记"
    assert "var first = !bloomSeated;" in body, "分不出是不是第一次显形"
    assert 'classList.remove("bloom-snap");\n      bloomSeated = true;' in body, \
        "就位之后没记下来，后续每一下 dragover 都会关过渡（跟手就变成硬跳）"
    assert 'bloomSeated = false;' in src[src.find("function setDropState"):src.find("function setDropState") + 400], \
        "收墨时没复位，第二次拖进来又会从兜底位置飘过来"
    for piece in ('area.classList.add("bloom-snap")',
                  'style.setProperty("--x"',
                  'style.setProperty("--y"',
                  'getComputedStyle(area.querySelector(".bloom")).transform',
                  'area.classList.remove("bloom-snap")'):
        assert piece in body, "首次显形没做对：" + piece
    order = [body.find(p) for p in ('classList.add("bloom-snap")',
                                    'style.setProperty("--x"',
                                    'getComputedStyle(',
                                    'classList.remove("bloom-snap")')]
    assert order == sorted(order), "关过渡的顺序不对（必须在写坐标之前挂、量完之后摘）"
    # 只在首次挂：后续跟手还得留着那条缓动，否则墨团是硬贴着光标走
    assert body.count('classList.add("bloom-snap")') == 1, \
        "不是首次也会关过渡，跟手就不跟了"


def case_ui_drag_office_docs_get_no_verdict():
    """办公文档拖进来也不再单独判「非书也」：悬停只有迎宾态。

    2026-11-17 起悬停期不分类，办公文档和别的文件走同一条路：迎宾 → 松手 →
    Python 按后缀拒。这条钉住「不许偷偷把办公文档的判词加回来」。
    """
    src = _ui_source()
    for gone in ("NOT_BOOK_DOC", "msword", "openxmlformats", "kswps",
                 "oasis.opendocument", "vnd.ms-excel", "vnd.ms-powerpoint"):
        assert gone not in src, "办公文档的 MIME 判据又回来了：" + gone


def case_ui_drag_without_files_type_is_silent():
    """拖的不是文件（网页选中文字）就不表态：不摆墨团、不改字。

    悬停期只剩一个可靠信号：types 里有没有 "Files"（实测：拖文件一定有；
    拖网页里选中的文字没有）。没有就返回。
    """
    src = _ui_source()
    fn = src[src.find("function isFileDrag"):src.find("function placeBloom")]
    assert '"Files"' in fn, "没有按 Files 判「是不是拖文件」"
    assert "dt.types" in fn, "没读 dt.types"
    drag = src[src.find('["dragenter", "dragover"].forEach'):]
    drag = drag[:drag.find('["dragleave", "dragend"].forEach')]
    i_guard = drag.find("if (!isFileDrag(ev.dataTransfer)) { return; }")
    i_set = drag.find('setDropState("drag")')
    assert i_guard >= 0 and i_set >= 0, "拖拽主循环缺了「判是不是文件」或「改状态」"
    assert i_guard < i_set, "信息不足时没在改状态之前返回"
    assert drag.find("placeBloom(ev)") > i_guard, "没文件的拖拽也在摆墨团"


def case_ui_drag_page_text_is_not_a_book():
    """选中文字不带 "Files"：走同一条「不表态」的路（0.2.3 新口径）。

    旧实现在 dt.types 里排 text/plain 等名单；现在只看有没有 "Files"，
    名单连同 MIME 判据一起退场 —— 这条钉住名单不许默默回来。
    """
    src = _ui_source()
    assert "TEXT_DRAG" not in src, "选中文字的老 MIME 名单还留着"
    assert "text/plain" not in src, "还在按 MIME 认书"


def case_ui_drag_exe_and_json_get_no_verdict():
    """exe / json 不再单独判：悬停只有迎宾态，松手后由 Python 拒。

    2026-11-17 起判词退场；这条钉住 MIME 黑名单不许加回来。
    """
    src = _ui_source()
    for gone in ("x-msdownload", "application/json", "x-msi", "zip-compressed",
                 "isNotBook"):
        assert gone not in src, "MIME 黑名单又回来了：" + gone


def case_ui_drag_color_is_stable_within_one_drag():
    """一次拖拽只抽一次色：dragover 不许重抽（跳色像故障）。

    抽色挂在「还没进迎宾态」这个守卫里；整段拖拽 data-drop 一直是 "drag"，
    所以只会抽一次。
    """
    src = _ui_source()
    drag = src[src.find('["dragenter", "dragover"].forEach'):]
    drag = drag[:drag.find('["dragleave", "dragend"].forEach')]
    assert drag.count("pickDragTone();") == 1, "抽色没有限制在一次"
    i_pick = drag.find("pickDragTone();")
    i_guard = drag.find('if (area.getAttribute("data-drop") !== "drag")')
    assert 0 <= i_guard < i_pick, "没有「已在迎宾态就不重抽」的守卫"


def case_ui_font_checkbox_keeps_the_users_choice():
    """用户手动改过「内嵌字体」之后，重进④页不许把他的勾冲回默认（0.2.2 补修）。

    默认值按导入包来（见 case_ui_font_default_follows_the_imported_package），
    但 renderMeta 每次重画④页（重进、校定、重跑）都会跑一遍，无条件写默认值
    就等于把用户的选择抹掉。所以默认值只在「他没动过」时写；换一本书时清回
    未动的状态，让这本的默认值重新生效。同一本书重跑（forceImport）不清。
    """
    src = _ui_source()
    assert "var fontsTouched = false;" in src, "「用户动过没有」的标记没了"
    listen = src[src.find('el("m-fonts").addEventListener("change"'):]
    listen = listen[:listen.find("});")]
    assert "fontsTouched = true;" in listen, "用户动勾没记下来"
    meta = src[src.find("function renderMeta"):]
    meta = meta[:meta.find('setView("meta")')]
    i_guard = meta.find("if (!fontsTouched) {")
    i_sf = meta.find("var sf = meta.source_fonts;")
    assert i_guard >= 0, "默认值又变成无条件写了"
    assert i_guard < i_sf, "默认值的写入口没被这个标记挡住"
    start = src[src.find("function startImport(path)"):]
    start = start[:start.find("function forceImport")]
    assert "fontsTouched = false;" in start, "换一本书时没让默认值重新生效"
    i_force = src.find("function forceImport(jobId)")
    assert i_force >= 0, "forceImport 的签名改了，这条用例已失去判断力（先修用例）"
    force = src[i_force:]
    force = force[:force.find("\n  function ")]
    assert "fontsTouched = false;" not in force, "同一本书重跑把用户的勾清掉了"


def case_ui_click_area_is_the_frame_inside():
    """首屏只有裁切线围出的框能点（0.2.2 第 4 条）。

    以前整个窗口接 click，点哪儿都弹文件选择框，看着像满屏都是按钮。现在挂一个
    与 .marks 同一尺寸的透明热区 #drop-hot：框里是手型光标、点一下弹框；框外
    什么都不发生。「关于」按钮在框外，仍是独立按钮；标题字不吃鼠标，点字要落到
    底下的热区。
    """
    src = _ui_source()
    css = src[:src.find("</style>")]

    def block(sel: str) -> str:
        start = css.find(sel)
        assert start >= 0, "样式里找不到 " + sel
        start += len(sel)
        return css[start:css.find("}", start)]

    hot = block("#drop-hot {")
    for piece in ("position: absolute", "inset: 40px", "cursor: pointer"):
        assert piece in hot, "热区样式少了：" + piece
    marks = block("#drop-area .marks {")
    assert "inset: 40px" in marks, ".marks 的裁切线位置变了"
    assert "inset: 40px" in hot, "热区与裁切线必须同一个 inset，否则「框里能点」名不副实"
    area = block("#drop-area {")
    assert "cursor:" not in area, "#drop-area 又整块变手型了（等于满屏可点）"
    title = block(".drop-title {")
    assert "pointer-events: none" in title, "标题吃了鼠标，点字弹不出文件框"
    # 监听只挂热区一份：两份会连弹两次文件框
    assert 'el("drop-hot").addEventListener("click"' in src, "热区没接 click"
    assert 'el("drop-area").addEventListener("click"' not in src, \
        "#drop-area 又接了 click，会连弹两次文件框"
    # 热区在 DOM 里要排在 .marks 前面：悬停收笔色靠 ~ 兄弟选择器
    i_hot = src.find('<div id="drop-hot" aria-hidden="true"></div>')
    i_ripple = src.find('<div class="ripple"')
    i_marks = src.find('<div class="marks">')
    assert i_hot >= 0 and i_ripple >= 0 and i_marks >= 0, "首屏的 HTML 结构缺件"
    assert i_ripple < i_hot < i_marks, "热区的位置不对（悬停规则靠它做兄弟选择）"
    assert "#drop-hot:hover ~ .marks i" in css, "悬停收笔色没改绑热区"


def case_ui_font_default_follows_the_imported_package():
    """内嵌字体的默认勾选按导入包来（0.2.2 第 5 条③）。

    TXT 那条路没有原书字体，按老规矩默认勾上；EPUB 导入时 meta.source_fonts
    是数字 —— 包里带了字体（>0）才勾，没带就不勾（勾了只是白打包一份）。
    用户手改照旧以复选框为准，这里只钉默认值。
    """
    src = _ui_source()
    meta = src[src.find("function renderMeta"):]
    meta = meta[:meta.find('setView("meta")')]
    assert "var sf = meta.source_fonts;" in meta, "没读导入包带没带字体"
    assert 'el("m-fonts").checked = (sf === undefined || sf === null)' in meta, \
        "字体默认没按包来（TXT 那条路要保留默认勾上）"
    assert 'el("m-fonts").checked = true;' not in src, "又硬写默认勾上了"


# ---------- 第 1 组：整本糊行默认拒收 + 「仍然继续」口子 ----------


def _one_line_book_text() -> str:
    sentence = "他还记得那一天的情景，风很大，吹得窗棂一直在响。"
    return "".join(f"第{i}章 标题　{sentence * 8}" for i in range(1, 30))


def _poll_job(api: Api, job_id: str, rounds: int = 2000) -> dict:
    for _ in range(rounds):
        p = api.job_poll(job_id)
        if p["done"]:
            return p
        time.sleep(0.02)
    raise AssertionError("worker 没在预期时间内结束：" + job_id)


def case_one_line_book_is_rejected_with_a_continue_exit():
    """整本糊成一行：默认拒收，错误页必须能亮出「仍然继续」。

    铁律是不给没有换行的文本猜分段/断句，但规则不可能全对 —— 误判一次
    就把用户挡在门外很难受，所以拒收只立一道**默认**墙，job_poll 要带回
    can_continue，前端据此显示出口。拒收发生在写盘之前：一个字节都不该落。
    """
    root = _tmp_root("oneline-reject")
    src = root / "糊行书.txt"
    src.write_text(_one_line_book_text(), encoding="utf-8")
    api = Api()
    job_id = api.import_file(str(src))["job_id"]
    p = _poll_job(api, job_id)

    assert p["error"], "糊行书不该被当成成功导入"
    assert p["payload"] is None
    assert p["error_title"] == "这个文件不能处理", p["error_title"]
    assert p["can_continue"] is True, "默认拒收必须给口子"
    assert "整本书几乎都糊在一两个超长行里" in p["error"], p["error"]
    assert "仍然继续" in p["error"], p["error"]
    assert "回原文把换行修好" in p["error"], p["error"]
    # 一个字节都没写盘：session 还没拿到结果，也没有产物目录
    session = api._sessions[job_id]
    assert session.result is None
    assert session.outdir is None
    assert not list(root.glob("*_gui*")), "拒收了却已经建了产物目录"


def case_force_import_reruns_the_rejected_book():
    """点「仍然继续」：同一个 Session 挂旗子重跑，这次照旧清洗成书。

    旗子挂 Session.extra 而不是 adjust（clean.py 的 ADJUST_KEYS 只认三个键，
    多一个就 ValueError），且新 job_id 必须重新登记会话，否则后面 review /
    build_epub 全说「会话不在了」。
    """
    root = _tmp_root("oneline-force")
    src = root / "糊行书.txt"
    src.write_text(_one_line_book_text(), encoding="utf-8")
    api = Api()
    old_id = api.import_file(str(src))["job_id"]
    assert _poll_job(api, old_id)["can_continue"] is True

    r = api.force_import(old_id)
    assert r.get("job_id"), r
    new_id = r["job_id"]
    assert new_id != old_id, "重跑必须用新 job_id，前端才能重新轮询"
    q = _poll_job(api, new_id)
    assert not q["error"], q["error"]
    assert q["can_continue"] is False
    assert q["payload"] is not None, "「仍然继续」之后要照旧出结果页"
    assert q["payload"]["oneline"]["suspected"] is True
    assert q["payload"]["chapters"]["count"] == 0
    # 同一份会话、旗子挂上了、新 id 在会话表里
    assert api._sessions[new_id] is api._sessions[old_id]
    assert api._sessions[new_id].extra.get("force_one_line") is True
    assert api._sessions[new_id].result is not None


def case_normal_book_is_not_rejected_by_one_line_gate():
    """正常书不该被糊行闸门误拦（判据是 0 章 + 超长行，缺一不可）。"""
    root = _tmp_root("oneline-normal")
    src = root / "正常书.txt"
    src.write_text(
        "《正常书》\n作者：某人\n\n第一章　开\n\n正文第一段。\n\n"
        "第二章　终\n\n正文第二段。\n",
        encoding="utf-8",
    )
    api = Api()
    p = _poll_job(api, api.import_file(str(src))["job_id"])
    assert not p["error"], p["error"]
    assert p["can_continue"] is False
    assert p["payload"]["chapters"]["count"] == 2


def case_is_one_line_book_only_when_zero_chapters_and_suspected():
    """纯判据：切出章节就不拦，没超长行也不拦。

    基线 book05 曾出现 suspected 为真而全书正常（正文里引用了几个章节号），
    收紧后 suspected 只认超长行，但这里把两道闸门都钉住，防的是哪天有人
    把「有章节」那道去掉、把正常书挡在门外。
    """
    class _Book:
        def __init__(self, chapters):
            self.chapters = chapters

    true_report = {"oneline": {"suspected": True, "long_line_count": 1}}
    false_report = {"oneline": {"suspected": False, "long_line_count": 0}}
    assert Api._is_one_line_book(_Book([]), true_report) is True
    assert Api._is_one_line_book(_Book([]), false_report) is False
    assert Api._is_one_line_book(_Book([1]), true_report) is False
    assert Api._is_one_line_book(_Book([]), {}) is False


def case_toc_empty_prefix_has_a_readable_exit_in_alerts():
    """目录残留：告警简报、复核页「切章」行、详情三处都得说人话。

    只认和报告、一行都不删 —— 用例同时钉住 dropped.toc == 0。
    """
    text = (
        "第一章 楔子\n\n第二章 出海\n\n第三章 归途\n\n"
        "第一章 楔子\n\n他走进了屋子，天已经黑了。\n"
    )
    report = clean_text(text, source="x.txt").report
    session = _session_for(text, name="目录残留.txt")

    assert report["dropped"]["toc"] == 0, "报了出口就偷偷删行"
    codes = [it["code"] for it in report["warning_items"]]
    assert "toc_empty_prefix" in codes, codes
    assert _ALERT_BRIEF["toc_empty_prefix"].format(n=3) == "开头 3 章只有标题、没有正文"

    rows = _rows_chap(report)
    hits = [r for r in rows if r.get("code") == "toc_empty_prefix"]
    assert len(hits) == 1, rows
    assert hits[0]["tag"] == "切章"
    assert "开头 3 章" in hits[0]["tx"], hits[0]

    detail = _detail_chap(session, hits[0])
    assert detail["left"], "左边该列出那几张空壳章"
    assert "一条都没删" in detail["prose"], detail["prose"]
    assert "回原文把开头那段目录删掉" in detail["prose"], detail["prose"]


def case_toc_empty_prefix_detail_lists_only_the_opening_run():
    """复核页左侧只列「开头连续那一段」空壳，正文中间另有空章不许混进来
    （review c，与 clean.py 的 _toc_like_empty_prefix 同口径）。
    """
    from huoban.model import Book, Chapter, Paragraph

    chapters = [
        Chapter(title="目录一", number=1, paragraphs=[]),
        Chapter(title="目录二", number=2, paragraphs=[]),
        Chapter(title="正文", number=3, paragraphs=[Paragraph(kind="body", text="他走进了屋子。")]),
        Chapter(title="中间空章", number=4, paragraphs=[]),
    ]
    book = Book(title="t", author="", encoding="utf-8", source="x.txt",
                chapters=chapters, front_matter=[], tail_matter=[])
    session = _session_for()
    session.result.book = book
    session.result.report = {"samples": {}}
    detail = _detail_chap(session, {"code": "toc_empty_prefix"})
    titles = [it["t"] for it in detail["left"]]
    assert titles == ["目录一", "目录二"], titles


def case_error_page_wires_the_continue_button():
    """错误页「仍然继续」：DOM、showError 出口、poll 触发、按钮监听四处都得接上。

    showError 有 20 多个调用点，只有 can_continue 那一条能亮出这颗按钮；
    没亮的时候它必须 hidden，不能变成一个永远点得动的死按钮。
    """
    src = _ui_source()
    assert 'id="btn-err-continue"' in src
    assert "仍然继续" in src
    assert 'el("btn-err-continue").addEventListener' in src
    assert "errContinue = typeof opts.continue" in src
    assert 'el("btn-err-continue").hidden = !errContinue' in src
    assert "if (res.can_continue)" in src
    assert "errOpts.continue = function () { forceImport(jobId); }" in src
    assert "function forceImport(jobId)" in src
    assert "a.force_import(jobId)" in src


def case_error_page_body_keeps_jargon_in_the_detail():
    """出错页的正文说人话：实现术语与英文堆栈只能进「技术细节」（2026-11-17 判过）。

    正文（`e-body`）回答的是「我该做什么」；pywebview、job_poll、hooks、轮询这些
    是维护者的话，堆栈更是。它们全归 opts.detail 那个折角，正文只留能照做的动作。
    注意：断言拦的是「作为正文出现」，不是把这些话从文件里删掉 —— 挪进技术细节
    之后它们仍该在，删掉反而是把证据丢了。
    """
    src = _ui_source()
    # 正文位 = 那句话后面紧跟 ", "（第二个实参），技术细节位 = 前面跟着 detail:
    for gone in ('"pywebview 的桥没有注入成功。请重启程序',
                 '"轮询出错"',
                 '"后端没有回应"',
                 '"后端返回了空的结果。"',
                 '"job_poll 返回了空值。",',
                 'showError("打不开文件选择框", String(err)',
                 'showError("打不开这本书", String(err)',
                 'showError("打不开复核页", String(err)'):
        assert gone not in src, "正文里还留着术语或堆栈：" + gone
    assert "pywebview 的桥没有注入成功" in src, "术语被删了，而不是挪进技术细节"
    assert 'detail: "job_poll 返回了空值。"' in src, \
        "job_poll 那句被删了，而不是挪进技术细节"
    assert src.count("detail: String(err)") >= 3, \
        "英文堆栈没有挪进技术细节（至少三处 catch 分支）"


def case_error_page_detail_box_lines_up_with_the_body():
    """技术细节的灰框要和正文左对齐，矮窗下只留外层一层滚动（2026-11-17 判过）。

    正文被那根 2px 印色竖线与 18px 间距推到右边，而 .err-more 是 .err-head 的兄弟，
    默认贴着 .err-main 的左缘 —— 差的正好是这 20px。矮窗下 pre 自己还带 max-height，
    和外层 .scroll 叠成两层滚动，用户不知道该滚哪一个。
    """
    src = _ui_source()
    assert ".err-more { margin-top: 16px; margin-left: 20px; }" in src, \
        "技术细节没跟正文左对齐（差 20px）"
    assert ".err-more pre { max-height: none; }" in src, \
        "矮窗下 pre 还限高：会出现两层滚动"
    assert "max-height: 120px" not in src, "矮窗下 pre 的旧限高还在"


def case_error_page_exits_are_a_ladder():
    """三颗出口按后果分档：「仍然继续」不再是虚线小字（2026-11-17 判过）。

    这屏唯一会改结果的动作是「仍然继续」（明知整本一行还要做），原来和
    「打开所在目录」（只是看一眼）同为虚线小字 —— 后果最重的看起来最轻。
    现在它提一档成描边按钮，和下面那行小字之间拉开 7px。
    """
    src = _ui_source()
    assert '<button class="ghost" id="btn-err-continue"' in src, \
        "「仍然继续」还是虚线小字"
    assert '<button class="dashed" id="btn-err-reveal"' in src, \
        "「打开所在目录」该留在最轻那一档"
    assert "#view-error #btn-err-reveal { margin-top: 7px; }" in src, \
        "两档之间没有拉开间距"


def case_error_page_body_splits_paragraphs():
    """正文按空行拆成段：段距交给 CSS，不再靠一个空行撑开（2026-11-17 判过乙）。

    原来是 `white-space: pre-wrap` + 文案里的空行，段间 25–26px；四段就吃掉 77px，
    720×520 上正文一长就要滚半天。拆成 `<p class="err-p">` 之后段距是一个值，
    长文案才收得回来。段内的单换行仍按换行显示（`.err-p` 自己 pre-wrap），文案口径不变。
    """
    src = _ui_source()
    assert "function errBody(node, body)" in src, "没有拆段的渲染函数"
    assert 'errBody(el("e-body"), body);' in src, "showError 还在用 text() 直填正文"
    assert 'text(el("e-body"), body || "")' not in src, "旧的直填还在"
    assert r"split(/\n{2,}/)" in src, "拆段没按空行切"
    assert 'p.className = "err-p";' in src, "段落没有统一的类名"


def case_error_page_spacing_is_one_scale():
    """出错页的段距收成一档：标题下 6 / 段间 12 / 正文到技术细节 16（矮窗 12）。

    2026-11-17 开发者判「收窄」：原值 10 / 25–26（空行）/ 18（矮窗）。
    这几个数不是为这一屏新造的孤例 —— 6 与 12 都在文件里已有的档位里
    （9 / 10 / 12 / 14 / 16 都在用），技术细节那 16 与矮窗 12 同理。
    """
    src = _ui_source()
    css = src[:src.find("</style>")]

    def block(sel):
        i = css.find(sel + " {")
        assert i >= 0, "找不到样式块：" + sel
        j = css.find("}", i)
        return css[i:j]

    para = block(".err-body .err-p")
    assert "margin: 0 0 12px;" in para, "段距不是 12px"
    assert "white-space: pre-wrap;" in para, "段内换行没保住"
    assert "margin: 0 0 6px;" in block(".err-title"), "标题下没有收到 6px"
    assert "margin-top: 16px;" in block(".err-more"), "正文到技术细节不是 16px"
    assert ".err-more { margin-top: 12px; }" in css, "矮窗下技术细节没收到 12px"
    assert "margin: 0 0 10px;" not in css, "标题下的旧值还在"
    assert ".err-more { margin-top: 18px; }" not in css, "矮窗下技术细节的旧值还在"


def case_ui_context_menu_only_on_the_three_boxes():
    """右键菜单只挂在书名 / 作者 / 输出目录三个框上（2026-11-17 开发者定范围）。

    正式包里全局右键是关的（pywebview 把右键菜单、浏览器快捷键、DevTools 绑在同一个
    debug 开关上，见 `gui/app.py` 的 `_debug()`）—— 这一条不许把那道闸打开，所以入口是
    「这三个 id 之一」，不是「任何输入框」也不是「整页」。
    触发也不押 `contextmenu`：WebView2 关掉默认菜单之后它派不派发，各家说法不一。
    """
    src = _ui_source()
    assert 'var CTX_IDS = ["m-title", "m-author", "m-outdir"];' in src, "三个框的名单不对"
    head = src[src.find("var CTX_IDS"):]
    assert '"m-lang"' not in head[:head.find(";")], "语言框不该进来"
    assert "if (ev.button !== 2)" in src, "没有按右键（button 2）判"
    assert "CTX_IDS.indexOf(t.id) < 0" in src, "没有拿 id 名单挡住别的元素"
    assert 'document.addEventListener("mousedown"' in src, "没有在 mousedown 上接右键"
    assert "ctxOpen(t, ev.clientX, ev.clientY)" in src, "右键没有把菜单放到光标处"
    # contextmenu 只做兜底，不许在那里开菜单
    tail = src[src.find('document.addEventListener("contextmenu"'):]
    assert "ctxOpen" not in tail[:240], "contextmenu 里又去开菜单了（它只该 preventDefault）"


def case_ui_context_menu_is_the_apps_own_paper():
    """菜单必须是自家的纸墨，不是系统弹层（2026-11-17 开发者明确要求）。

    粗看要像这身纸墨长出来的一块：纸底、墨字、印色当悬停，深浅交给变量跟着主题走。
    所以这一块里不许出现写死的白 / 灰 —— 那就是 Windows 那套弹层的样子。
    """
    src = _ui_source()
    i = src.find(".ctx-menu {")
    j = src.find(".ctx-menu button:disabled")
    assert i >= 0 and j > i, "找不到 .ctx-menu 的样式块"
    css = src[i:j]
    for need in ("background: var(--paper)", "border: 1px solid var(--rule)",
                 "color: var(--ink)", "font-family: var(--sans)"):
        assert need in css, "菜单没走界面自己的变量：" + need
    assert "var(--seal-tint)" in src[src.find(".ctx-menu button:hover"):], \
        "悬停没有印色那一层"
    low = css.lower()
    for bad in ("#fff", "#ffffff", "white", "#f0f0f0", "#e5e5e5", "#f3f3f3"):
        assert bad not in low, "菜单里出现系统弹层的颜色：" + bad


def case_ui_context_menu_paste_degrades_without_api():
    """拿不到 paste_text 时「粘贴」置灰、不隐藏（契约里的可缺省动作）。

    隐藏会让菜单在两次右键里位置跳一下，也让人以为这功能不存在；置灰是
    「这台机器上暂时按不动」。「全选」不依赖后端，照常可用。
    """
    src = _ui_source()
    assert 'el("ctx-paste").disabled = !(a && a.paste_text);' in src, \
        "没有按后端有没有 paste_text 置灰"
    assert 'el("ctx-paste").hidden' not in src, "「粘贴」被藏起来了，应该置灰"
    assert 'el("ctx-selectall").addEventListener' in src, "「全选」没有接上"
    assert "t.select();" in src, "「全选」没有真去选"
    # 粘贴要把文本插到光标处并替换选中，还要让既有监听（重排字号 / 更新摘要）跟着跑
    assert "t.setSelectionRange(pos, pos)" in src, "粘完没把光标放到插入点之后"
    assert 'new Event("input", { bubbles: true })' in src, \
        "程序改值没有派 input：字号不重排、输出目录摘要不更新"


def case_ui_context_menu_closes_on_escape_and_view_change():
    """菜单不许赖在屏幕上：Esc、点别处、换屏三处都要收。

    它是 fixed 浮层，换屏（setView）不带走它就会悬在下一屏上 —— 那正是
    「界面没排完」的样子。
    """
    src = _ui_source()
    assert 'if (ev.key === "Escape") { ctxClose(); }' in src, "Esc 收不掉菜单"
    assert "if (m && !m.hidden && !m.contains(ev.target)) { ctxClose(); }" in src, \
        "点别处收不掉菜单"
    body = src[src.find("function setView("):]
    body = body[:body.find("\n  }")]
    assert "ctxClose();" in body, "换屏时没把菜单收走"


# ---------- 验收期补：清洗等待页必须飘这本书自己的话 ----------


def case_import_excerpt_returns_this_books_own_lines():
    """拖入时只读解码文件头，回送的每一片都必须是原文里真有的字。

    以前等待页飘的是写死的假广告（"更多精彩小说，尽在某某小说网"），
    用户在自己书上看到一眼就认出来不是自己的书。现在后端挑真广告行、
    真断行对、相邻两行真正文，绝不造字。
    """
    root = _tmp_root("excerpt")
    src = root / "真书.txt"
    body = (
        "更多精彩小说，尽在某某小说网 www.example-novel.com\n"
        "他站在城门口，\n"
        "望着远处的灯火。\n"
        "风从北面吹来，带着一股潮湿的寒意。\n"
        "那时候他还不知道，一切都将改变。\n"
    )
    src.write_text(body, encoding="utf-8")
    api = Api()
    out = api.import_excerpt(str(src))
    assert any("某某小说网" in s for s in out["drop"]), out["drop"]
    assert out["merge"], "干净断行对没挑出来"
    # 每一片都得是原文的连续子串（截短允许，造字不允许）
    for piece in out["drop"]:
        assert piece in body, piece
    for pair in out["merge"]:
        assert len(pair) == 2
        assert pair[0] in body and pair[1] in body, pair
    # 写死的假碎行是把一整行劈成两半（"风从北面吹来，/带着一股潮湿的寒意。"），
    # 而这里只取相邻两真行，不会凭空造出那种组合。
    assert ["风从北面吹来，", "带着一股潮湿的寒意。"] not in out["merge"], out["merge"]
    # EPUB / 不存在 / 空路径：都返回空清单，界面宁可安静
    assert api.import_excerpt(str(root / "无.txt")) == {"drop": [], "merge": []}
    assert api.import_excerpt("") == {"drop": [], "merge": []}
    assert api.import_excerpt(str(root / "书.epub")) == {"drop": [], "merge": []}


def case_import_excerpt_is_read_only_and_bounded():
    """只读取文件头、不改输入文件、不写盘、字段形状固定。"""
    root = _tmp_root("excerpt-ro")
    src = root / "只读.txt"
    src.write_text(
        "第一章 开\n\n" + "正文一句话，接下去说。\n" * 40,
        encoding="utf-8",
    )
    before = src.read_bytes()
    out = Api().import_excerpt(str(src))
    assert set(out.keys()) == {"drop", "merge"}
    assert src.read_bytes() == before, "import_excerpt 改了输入文件"
    # 每片都截到 22 字以内，不会把整本正文塞进动画
    for piece in out["drop"]:
        assert len(piece) <= 22
    for pair in out["merge"]:
        assert all(len(x) <= 22 for x in pair)
    assert len(out["drop"]) <= Api.EXCERPT_MAX_ITEMS
    assert len(out["merge"]) <= Api.EXCERPT_MAX_ITEMS


def case_ui_clean_wait_screen_uses_real_book_lines():
    """清洗等待页不再有写死的假广告，且拖入时会去后端取真内容。"""
    src = _ui_source()
    assert "BG_SAMPLES" not in src, "写死的假样例没删干净"
    assert "example-novel.com" not in src, "假广告还留在界面里"
    assert "var bgFeed = null;" in src, "没有真内容源变量"
    assert "a.import_excerpt" in src, "startImport 没去取真内容"
    assert "normalizeFeed(feed)" in src, "取回的内容没校验形状"
    assert "if (!sample) { return false; }" in src, "取不到真内容时还在造假片"
    assert "bgFeedSeq" in src, "换书时没有作废旧回报"
    assert "function normalizeFeed(res)" in src


# ---------- 右键粘贴：`paste_text`（剪贴板由 Python 读） ----------
#
# 契约（与 UI设计肥鱼 定的交界件）：无入参；成功 {"text": "..."}；剪贴板为空或
# 只有非文本 → {"text": ""}（**空不是失败**）；读不到 → {"error": "..."}。
# 可缺省方法：界面用 `a.paste_text &&` 守卫，缺席时「粘贴」置灰、「全选」照常。
# 剪贴板必须由 Python 读：spike 结论是 pywebview 没接 PermissionRequested，
# navigator.clipboard 的权限与安全上下文都不确定。


def case_paste_text_returns_the_clipboard_text():
    """读到什么就给什么，不裁剪、不改写（书名里可能有空格与全角标点）。"""
    import gui.api as api_mod

    want = "　《三体》 刘慈欣　"
    old = api_mod._read_clipboard
    api_mod._read_clipboard = lambda: want
    try:
        out = Api().paste_text()
    finally:
        api_mod._read_clipboard = old
    assert out == {"text": want}, out


def case_paste_text_empty_clipboard_is_not_an_error():
    """空剪贴板 / 只有图片 → 空串，且**不许**变成失败（菜单会误置灰成"坏了"）。"""
    import gui.api as api_mod

    old = api_mod._read_clipboard
    for empty in ("", None):
        api_mod._read_clipboard = lambda e=empty: "" if e is None else e
        try:
            out = Api().paste_text()
        finally:
            api_mod._read_clipboard = old
        assert out.get("text") == "", out
        assert "error" not in out, f"空剪贴板被当成失败了：{out}"


def case_paste_text_failure_returns_words_not_exception():
    """被别的程序占着时返回人话，不抛异常（界面据此把「粘贴」置灰）。"""
    import gui.api as api_mod

    def boom():
        raise OSError("剪贴板被别的程序占着")

    old = api_mod._read_clipboard
    api_mod._read_clipboard = boom
    try:
        out = Api().paste_text()
    finally:
        api_mod._read_clipboard = old
    assert out.get("error"), out
    assert "剪贴板" in out["error"], out
    assert "Traceback" not in out["error"], out


def case_paste_text_reads_the_windows_clipboard_for_real():
    """真读一次本机剪贴板：返回的一定是 str，且不抛。

    只断言类型与"不炸"：剪贴板里此刻是什么由机器决定，用例不能依赖它。
    这一条同时证明「用的是 Python 侧实现」——桩被移除后仍然出结果。
    """
    out = Api().paste_text()
    assert isinstance(out, dict), out
    if "error" in out:
        assert out["error"], out
    else:
        assert isinstance(out.get("text"), str), out


# ---------- 清洗中途检查点：取消在下一次回调内生效（todo/0.3.0.md） ----------
#
# 以前 `clean_file` 是一次同步调用、中途没有检查点，点「取消」只能等它跑完，
# 几十秒后书上完了才认账。现在 worker 传一个闭包给 `clean_file`，每约 2000 行
# 查一次旗子。这几条用例钉住「旗子真被查到」「取消被认成取消而不是失败」。


def _checkpoint_book(tmp: Path, lines: int = 40) -> Path:
    """造一本小书：只用它过格式/大小闸，清洗本身由桩替换。"""
    src = tmp / "检查点.txt"
    body = ["第一章　起", ""]
    body += ["正文一段，以句号收尾。" if i % 2 else "" for i in range(lines)]
    src.write_text("\n".join(body) + "\n", encoding="utf-8")
    return src


def _run_with_fake_clean(api_mod, *, total: int, cancel_at: int, path: Path):
    """替换 `clean_file`，让检查点按给定节奏回调；返回 (job, raised, steps)。"""
    job = Job(job_id="t-checkpoint")
    session = Session(source=path, adjust=None)
    steps: list[tuple[str, int, int]] = []

    def fake_clean_file(src, *, adjust=None, on_step=None):
        assert on_step is not None, "worker 没把 on_step 传给 clean_file"
        for i in range(1, total + 1):
            if i == cancel_at:
                job.cancel_requested = True
            # 先记账再回调：回调正是取消抛出的地方，抛出去之后这一行就不会再回来，
            # 而"停在哪一行"恰恰要靠这一步记下来。
            steps.append(("scan", i, total))
            on_step(i, total, "scan")
        raise AssertionError("旗子立了，清洗却一路跑到底")

    old = api_mod.clean_file
    api_mod.clean_file = fake_clean_file
    try:
        raised = None
        try:
            Api()._run_import(job, session)
        except BaseException as exc:  # noqa: BLE001
            raised = exc
    finally:
        api_mod.clean_file = old
    return job, raised, steps


def case_cancel_during_clean_fires_at_the_next_checkpoint():
    """立旗子在 7000 行，必须在下一个检查点（8000）就停，不是跑满 9000。"""
    import gui.api as api_mod

    root = _tmp_root("checkpoint")
    src = _checkpoint_book(root)
    job, raised, steps = _run_with_fake_clean(
        api_mod, total=9000, cancel_at=7000, path=src
    )

    assert isinstance(raised, api_mod.Cancelled), (
        "取消没被认成 Cancelled，而是：%r" % (raised,)
    )
    expect = ((7000 // api_mod.CANCEL_CHECK_LINES) + 1) * api_mod.CANCEL_CHECK_LINES
    reached = steps[-1][1]
    assert reached == expect, (
        f"该在下一个检查点 {expect} 停下，实际停在 {reached}（共跑了 {len(steps)} 行）"
    )
    assert steps[-1][0] == "scan", steps[-1]
    # 卡点要能看见：跑到的位置记在 job 上
    assert str(expect) in job.last_step, job.last_step
    assert "scan" in job.last_step, job.last_step


def case_cancelled_clean_is_not_reported_as_failure():
    """取消走 `_spawn` 之后必须是「取消」，不能变成「清洗失败」。"""
    import gui.api as api_mod

    root = _tmp_root("checkpoint2")
    src = _checkpoint_book(root)
    api = Api()
    job, raised, _steps = _run_with_fake_clean(
        api_mod, total=9000, cancel_at=100, path=src
    )
    assert isinstance(raised, api_mod.Cancelled), raised

    # 真走一遍 _spawn：同一个桩，同一条取消路径
    job2 = api._new_job("import")
    session = Session(source=src, adjust=None)

    def fake_clean_file(src, *, adjust=None, on_step=None):
        job2.cancel_requested = True
        for i in range(1, 9001):
            on_step(i, 9000, "scan")

    old = api_mod.clean_file
    api_mod.clean_file = fake_clean_file
    try:
        api._spawn(job2, lambda: api._run_import(job2, session))
        # 桩必须等线程跑完再还原：`_spawn` 是异步起线程，
        # 在它返回时就还原，线程拿到的已经是真的 clean_file 了。
        job2.thread.join(timeout=20)
    finally:
        api_mod.clean_file = old

    assert job2.done is True
    assert job2.cancelled is True, "取消没被记成取消"
    assert job2.error == "", f"取消被当成失败报出来了：{job2.error}"
    assert job2.error_title == "", f"取消被贴了个失败标题：{job2.error_title}"
    assert job2.payload is None, job2.payload
    # 产物目录不残留
    assert not (src.parent / f"{src.stem}_gui").exists(), "取消后还留了产物目录"


def case_job_last_step_stays_out_of_the_payload():
    """`last_step` 只进日志与技术细节，不许混进送界面的 payload。"""
    job = Job(job_id="t-laststep")
    assert hasattr(job, "last_step"), "Job 没有 last_step"
    assert job.last_step == "", "默认必须是空串（没跑过就没什么可说）"

    api = Api()
    api._jobs[job.job_id] = job
    view = api.job_poll(job.job_id)
    assert "last_step" not in view, f"job_poll 把 last_step 送到界面了：{view}"


# ---------- 关于页外链通道（方案 C：白名单写死在 Python 侧） ----------
#
# `todo/0.3.0.md` 的「【设计】“关于”页重设计」里的「外链通道（方案 C）」：三个正式地址写死、逐字比对，
# 其余一律拒绝并给「人话」。坑写在条目里：白名单不许由前端传任意 URL 直接放行，
# 也不许只靠 `target="_blank"`（那是全放行）。

ABOUT_GOOD_URLS = (
    "https://www.xiaohongshu.com/user/profile/63c200200000000027028222",
    "https://github.com/ChineseRyan/huoban",
    "https://ifdian.net/a/ChineseRyan",
)


def _stub_open(fn):
    """把「真去开浏览器」那一步换成假的。库代码只返回数据，测试也不该弹窗。"""
    import gui.api as api_mod

    old = api_mod._open_external
    api_mod._open_external = fn
    return api_mod, old


def case_about_links_is_the_same_source_as_the_whitelist():
    """页脚要显示的三条地址必须与放行白名单同源 —— 两处漂了就会出现「显示得出来、点不动」。

    ui.html 不写死 http 串（`case_ui_has_no_external_links` 要求单文件零外链），
    所以地址由这个接口供；`open_url` 又拿同一份做白名单。这条用例把两者焊在一起：
    接口给的每一条都必须真能被 `open_url` 放行。
    """
    from gui.api import _ABOUT_LINKS, _ABOUT_URLS

    out = Api().about_links()
    links = out["links"]
    assert set(links) == {"xiaohongshu", "github", "ifdian"}, links
    assert "error" not in out, out

    # 逐字等于白名单那三条（顺序无所谓，成员必须一模一样）
    assert sorted(links.values()) == sorted(_ABOUT_URLS), (links, _ABOUT_URLS)
    assert dict(_ABOUT_LINKS) == links, "键名或地址被改动了：%r" % (links,)

    opened: list[str] = []
    api_mod, old = _stub_open(opened.append)
    try:
        api = Api()
        for url in links.values():
            assert api.open_url(url).get("ok") is True, "%r 显示得出来却点不动" % (url,)
    finally:
        api_mod._open_external = old
    assert opened == [links["xiaohongshu"], links["github"], links["ifdian"]], opened


def case_open_url_allows_exactly_the_three_written_in_addresses():
    """只放行写死的三条地址，其余一律拒绝 —— 这不是「过滤一下」，是白名单。

    原来的 `gui/api.py` 只有 `os.startfile`：「用系统默认程序打开」这个能力谁都能传
    任意地址进来。关于页的外链要走 js_api，所以放行范围必须在 Python 侧写死。
    """
    from gui.api import _ABOUT_URLS

    assert tuple(_ABOUT_URLS) == ABOUT_GOOD_URLS, (
        "白名单被改了或宽度变了：%r" % (tuple(_ABOUT_URLS),)
    )

    opened: list[str] = []
    api_mod, old = _stub_open(opened.append)
    try:
        api = Api()
        for url in ABOUT_GOOD_URLS:
            out = api.open_url(url)
            assert out.get("ok") is True, (url, out)
            assert "error" not in out, (url, out)
        assert opened == list(ABOUT_GOOD_URLS), "放行的地址该原样交给系统浏览器：%r" % (opened,)

        opened.clear()
        # 每一条都必须被拒：同域名换路径 / 带 query / 更短 / http / 换大小写 /
        # 少一位 / 换协议。白名单是逐字比对，不是「同一个网站就放行」。
        bad = (
            "https://github.com/ChineseRyan/huoban-notes",
            "https://github.com/ChineseRyan/huoban?tab=readme",
            "https://github.com/ChineseRyan",
            "http://github.com/ChineseRyan/huoban",
            "https://GitHub.com/ChineseRyan/huoban",
            "https://www.xiaohongshu.com/user/profile/63c20020000000002702822",
            "https://www.xiaohongshu.com/",
            "https://ifdian.net/a/",
            "javascript:alert(1)",
            "file:///C:/Windows/System32/calc.exe",
            "",
            "   ",
        )
        for url in bad:
            out = api.open_url(url)
            assert out.get("ok") is not True, "%r 不该放行：%r" % (url, out)
            assert out.get("error"), "%r 被拒了却没给人话：%r" % (url, out)
            assert "Traceback" not in out["error"], out
        assert opened == [], "被拒的地址竟然真交给系统打开了：%r" % (opened,)
    finally:
        api_mod._open_external = old


def case_open_url_turns_open_failure_into_words():
    """系统浏览器打不开时只返回人话，不抛异常、不 print（库代码只返回数据）。"""
    def boom(_url):
        raise OSError("没有默认浏览器")

    api_mod, old = _stub_open(boom)
    try:
        out = Api().open_url(ABOUT_GOOD_URLS[1])
    finally:
        api_mod._open_external = old
    assert out.get("ok") is not True, out
    assert "没有默认浏览器" in out.get("error", ""), out


# ---------- 用例表 ----------

_CASES = [
    case_safe_name_keeps_chinese_and_spaces,
    case_safe_name_replaces_illegal_chars,
    case_safe_name_handles_reserved_names,
    case_safe_name_empty_falls_back,
    case_safe_name_truncates_long_title,
    case_book_meta_paragraphs_match_epub_side,
    case_book_meta_title_can_be_edited_by_user,
    case_review_groups_cover_five_kinds,
    case_review_rows_carry_line_and_tag,
    case_review_rows_are_brief,
    case_review_delete_detail_shows_context,
    case_review_merge_rows_and_detail,
    case_review_merge_is_full_not_sample,
    case_review_marks_kept_from_adjust,
    case_review_marks_break_from_adjust,
    case_review_paginates_and_more,
    case_review_detail_out_of_range_is_error,
    case_review_empty_book_has_no_rows,
    # 试用反馈：多卷书复核页的假警报
    case_chapter_rows_groups_volumes_no_false_alerts,
    case_chapter_rows_five_keys_and_idx_roundtrip,
    case_review_groups_are_cached_per_result,
    case_apply_adjust_rejects_bad_shape_without_job,
    case_api_has_underscore_window,
    case_app_info_reports_azw3_availability,
    case_import_summary_is_not_a_clean_bill,
    case_import_summary_never_leaks_image_names,
    case_import_summary_carries_source_size,
    case_pick_file_offers_epub_too,
    case_import_outdir_reuses_only_same_source,
    case_import_outdir_finds_own_numbered_dir,
    case_import_outdir_third_source_skips_occupied,
    case_import_outdir_treats_broken_book_json_as_taken,
    case_build_failure_says_packaging_not_cleaning,
    case_build_keeps_epub_when_book_json_is_locked,
    case_unique_dir_never_reuses_existing,
    case_unique_dir_numbering_is_sequential,
    case_epub_roundtrip_keeps_the_original_back_cover,
    case_epub_broken_package_is_input_error,
    case_broken_package_message_explains_itself,
    case_epub_failure_leaves_no_stage_dir,
    case_zero_byte_file_is_rejected_at_entry,
    case_format_gate_comes_before_the_size_gate,
    case_office_files_get_their_own_wording,
    case_summary_content_state_flags_a_textless_book,
    case_import_summary_separates_images_only_from_empty,
    case_content_state_rules_are_exhaustive,
    case_book_meta_has_source_cover_default,
    case_cover_for_build_uses_edited_title,
    case_preview_cover_uses_edited_title_and_author,
    case_ui_meta_page_sends_author_to_cover_preview,
    case_cover_source_without_file_returns_none,
    case_cover_modes_none_and_image_default_to_none,
    case_build_writes_epub_to_custom_outdir,
    case_build_outdir_falls_back_when_unusable,
    case_book_glyphs_are_the_books_own_characters,
    case_ui_ink_glyphs_come_from_the_payload,
    case_book_glyphs_fall_back_to_front_matter_when_no_chapters,
    case_review_payload_gives_every_row_key_for_select_all,
    # 2026-11-22 开发者定：数字 0 的那一类，小灯直接点亮
    case_review_zero_group_lights_up_without_a_click,
    case_ui_drop_listener_blocks_the_default_action,
 case_ui_every_button_has_a_listener,
    case_ui_every_view_is_reachable,
    case_ui_done_page_keeps_its_payload,
    case_ui_polling_respects_the_job_badge,
    case_ui_has_no_external_links,
    case_ui_is_one_lf_file,
    # 0.2.3 第 1 条（2026-11-17 开发者定）：悬停只有迎宾态 ——
    # 一团跟手的墨（五色随机、单次不换）+「请松手」，悬停期不判书/非书
    case_ui_drop_has_welcome_state,
    case_ui_drop_does_not_classify_during_drag,
    case_ui_drop_state_holds_until_verdict,
    case_ui_drag_colors_come_from_cover_palette,
    case_ui_drag_color_is_stable_within_one_drag,
    case_ui_drop_first_frame_snaps_into_place,
    case_ui_drag_office_docs_get_no_verdict,
    case_ui_drag_without_files_type_is_silent,
    case_ui_click_area_is_the_frame_inside,
    case_ui_font_default_follows_the_imported_package,
    case_ui_drag_page_text_is_not_a_book,
    # 用户反馈（2026-11-09）：exe/json 拖进虚框不许亮「书稿已得」（现由迎宾态统一覆盖）
    case_ui_drag_exe_and_json_get_no_verdict,
    case_ui_font_checkbox_keeps_the_users_choice,
    # 第 1 组：糊行默认拒收 + 仍然继续 / 目录残留出口 / 错误页接线
    case_one_line_book_is_rejected_with_a_continue_exit,
    case_force_import_reruns_the_rejected_book,
    case_normal_book_is_not_rejected_by_one_line_gate,
    case_is_one_line_book_only_when_zero_chapters_and_suspected,
    case_toc_empty_prefix_has_a_readable_exit_in_alerts,
    case_error_page_wires_the_continue_button,
    # 0.3.0 出错页逐屏重审（2026-11-17 开发者判过 1–5 条）：正文说人话、
    # 技术细节与正文左对齐且矮窗只有一层滚动、三颗出口按后果分档
    case_error_page_body_keeps_jargon_in_the_detail,
    case_error_page_detail_box_lines_up_with_the_body,
    case_error_page_exits_are_a_ladder,
    # 0.3.0 出错页段间距收窄（2026-11-17 判过乙：按空行拆段、段距 12）
    case_error_page_body_splits_paragraphs,
    case_error_page_spacing_is_one_scale,
    # 0.3.0 书名 / 作者 / 输出目录输入框的右键菜单（2026-11-17 开发者定范围与配色）
    case_ui_context_menu_only_on_the_three_boxes,
    case_ui_context_menu_is_the_apps_own_paper,
    case_ui_context_menu_paste_degrades_without_api,
    case_ui_context_menu_closes_on_escape_and_view_change,
    # 验收期补的：装饰边框切章 / 清洗等待页飘真内容 / 付梓字模不印古文
    case_import_excerpt_returns_this_books_own_lines,
    case_import_excerpt_is_read_only_and_bounded,
    case_ui_clean_wait_screen_uses_real_book_lines,
    # 验收期第二轮：作者一致性 / 复核页只列开头连续空章
    case_author_box_defines_the_author_everywhere,
    case_toc_empty_prefix_detail_lists_only_the_opening_run,
    # 试用反馈第三轮：封面不跳回第一张 / 删掉「3 / 45」/ 字模不印重字
    case_preview_cover_keeps_the_look_on_screen,
    case_build_uses_the_look_on_screen,
    case_back_cover_goes_into_the_package,
    case_back_cover_falls_back_to_the_imported_one,
    case_ui_cover_caption_has_no_queue_position,
    case_ui_cover_redraw_keeps_the_current_look,
    # 0.3.0 关于页：外链通道（方案 C）白名单在 Python 侧
    case_paste_text_returns_the_clipboard_text,
    case_paste_text_empty_clipboard_is_not_an_error,
    case_paste_text_failure_returns_words_not_exception,
    case_paste_text_reads_the_windows_clipboard_for_real,
    case_cancel_during_clean_fires_at_the_next_checkpoint,
    case_cancelled_clean_is_not_reported_as_failure,
    case_job_last_step_stays_out_of_the_payload,
    case_about_links_is_the_same_source_as_the_whitelist,
    case_open_url_allows_exactly_the_three_written_in_addresses,
    case_open_url_turns_open_failure_into_words,
]


def main() -> int:
    failed = 0
    for case in _CASES:
        try:
            case()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {case.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {case.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {case.__name__}")
    total = len(_CASES)
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
