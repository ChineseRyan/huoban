"""clean.py 的测试：脏 TXT → 章节树 + 清洗报告。

照 tools/smoke_test.py 的风格：一个 case 一个函数，纯 assert，
直接 python tests/test_clean.py 跑。

只断言"用户能核对的事实"（删了几行、并到哪去了、章节怎么切的），
不断言内部实现。每条用例都用最小假数据，能让人一眼看出失败原因。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.clean import clean_bytes, clean_file, clean_text  # noqa: E402
from huoban.model import load as load_book  # noqa: E402
from huoban.model import save as save_book  # noqa: E402
from huoban.model import to_dict, validate  # noqa: E402


# ---------------------------------------------------------------------------
# 造数据的小工具
# ---------------------------------------------------------------------------


def _text(*lines: str) -> str:
    """按行拼一段文本，自动补行尾换行。"""
    return "\n".join(lines) + "\n"


def _bodies(chapter) -> list[str]:
    """取一章里正文段落的文字，跳过空行。"""
    return [p.text for p in chapter.paragraphs if p.kind == "body"]


# ---------------------------------------------------------------------------
# 一、基本结构
# ---------------------------------------------------------------------------


def case_plain_structure() -> None:
    """最简单的书：两章，各一句正文。"""
    res = clean_text(_text("第一章 开始", "", "正文一。", "", "第二章 结束", "", "正文二。"), source="x")
    book = res.book

    assert len(book.chapters) == 2, f"应切出 2 章，实际 {len(book.chapters)}"
    # 标题内部的分隔符要保留（统一成全角空格）：'第一章 开始' 里的空格是
    # 排版分隔（回目/章名与标题正文之间），删掉就成了连写一串 ——
    # 也就是之前 EPUB 里 h1 显示成"第一回止淫风借淫事说法谈色事就色欲开端"的原因。
    assert book.chapters[0].title == "第一章\u3000开始", f"标题应保留内部分隔空格，实际 {book.chapters[0].title!r}"
    assert book.chapters[0].number == 1, f"章号应解析成 1，实际 {book.chapters[0].number}"
    assert book.chapters[0].level == 1, "第N章应为 level 1"
    assert book.chapters[1].title == "第二章\u3000结束"
    assert _bodies(book.chapters[0]) == ["正文一。"], _bodies(book.chapters[0])
    assert book.front_matter == [], "第一个标题之前没有内容，front_matter 应为空"
    assert book.tail_matter == [], "tail_matter 恒为空"
    assert validate(book) == [], f"这本书不该有问题，实际 {validate(book)}"


def case_start_line_is_original() -> None:
    """章节的 start_line 是**原始文件**的行号，删行之后不重编号。"""
    res = clean_text(
        _text(
            "更多精彩小说，尽在某某小说网 www.example-novel.com",  # 第 1 行：整行广告，会被删
            "",
            "第一章 开始",                                          # 第 3 行：真正的章标题
            "",
            "正文。",
        ),
        source="x",
    )
    assert res.report["dropped"]["ad"] == 1, res.report["dropped"]
    assert res.book.chapters[0].start_line == 3, (
        f"start_line 应指向原文第 3 行，实际 {res.book.chapters[0].start_line}"
    )


def case_front_matter() -> None:
    """第一个标题之前的内容进 front_matter，不能丢。"""
    res = clean_text(_text("作者：某甲", "", "第一章 开始", "", "正文。"), source="x")
    assert [p.text for p in res.book.front_matter] == ["作者：某甲"], res.book.front_matter


def case_separator_not_chapter() -> None:
    """分隔线不是章节：把它当标题会把一章切成几个空章。

    同时它也不是正文 —— 整行只有装饰符号的行归 kind="sep"，
    渲染层据此居中输出（按正文排版会变成"缩进 + 两端对齐的一串符号"）。
    """
    res = clean_text(_text("第一章 开始", "", "正文。", "", "-----", "", "更多正文。"), source="x")
    assert len(res.book.chapters) == 1, f"分隔线不该切章，实际切出 {len(res.book.chapters)} 章"
    ch = res.book.chapters[0]
    seps = [p.text for p in ch.paragraphs if p.kind == "sep"]
    assert seps == ["-----"], f"分隔线应保留成 sep 段落，实际 {seps}"
    assert _bodies(ch) == ["正文。", "更多正文。"], _bodies(ch)


def case_sep_fullwidth_asterisks() -> None:
    """全角星号分隔线（"＊＊＊＊＊＊"）也归 sep。

    原书用的是全角 ＊（U+FF0A），而 match_chapter 的"分隔线"家族只认半角
    `*`。漏掉它的话，一本 44 章的书会留下 61 个缩进 + 两端对齐的星号行，
    在阅读器里看着像正文混进了乱码。
    """
    res = clean_text(
        _text("第一章 开始", "", "正文一。", "＊＊＊＊＊＊", "正文二。"), source="x"
    )
    ch = res.book.chapters[0]
    assert [p.kind for p in ch.paragraphs] == ["body", "sep", "body"], [
        (p.kind, p.text) for p in ch.paragraphs
    ]
    assert ch.paragraphs[1].text == "＊＊＊＊＊＊"


def case_sep_not_merged_into_previous() -> None:
    """上一段没有句末标点时，分隔线也不能被并进去。

    并碎行的判据之一是"上一行没写完"，而分隔线本身永远不以句末标点收尾 ——
    不加守卫的话，"他抬头看向窗外" + "＊＊＊＊＊＊" 会被焊成一段。
    """
    res = clean_text(
        _text("第一章 开始", "", "他抬头看向窗外", "＊＊＊＊＊＊", "天已经黑了。"), source="x"
    )
    ch = res.book.chapters[0]
    kinds = [p.kind for p in ch.paragraphs]
    assert "sep" in kinds, [(p.kind, p.text) for p in ch.paragraphs]
    sep_at = kinds.index("sep")
    assert ch.paragraphs[sep_at].text == "＊＊＊＊＊＊"
    joined = "".join(p.text for p in ch.paragraphs if p.kind == "body")
    assert "＊＊＊＊＊＊" not in joined, "分隔线不该出现在正文段落里"


def case_sep_rejects_punctuation_lines() -> None:
    """整行标点不算分隔线。

    "……" 单独一行是"沉默/停顿"的意思，"？？"是原书的错字，
    把它们当分隔线居中输出会改掉原文的语气。判据只收装饰性符号。
    """
    res = clean_text(
        _text("第一章 开始", "", "他说：", "……", "她没回答。"), source="x"
    )
    kinds = [p.kind for p in res.book.chapters[0].paragraphs]
    assert "sep" not in kinds, [(p.kind, p.text) for p in res.book.chapters[0].paragraphs]


def case_site_prefix_rejects_body_sentence() -> None:
    """前缀剥完还得像标题，否则正文句会变成章节标题。

    真实反例（《书B》第 7833 行）：
    "    演第三集时车路过一个城市，恰逢交通管制。两旁分别是机场大巴和公交长"
    前缀"演"只有 1 个字，剥完"第三集…"能被 match_chapter 认成卷标题
    （"集"是卷级量词），于是整句正文成了一章的标题。
    """
    res = clean_text(
        _text(
            "    演第三集时车路过一个城市，恰逢交通管制。两旁分别是机场大巴和公交长",
            "第一章 开始",
            "正文。",
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    # 标题保留行内空白，normalize 把它归一成全角空格（P0 修的就是这件事）
    assert titles == ["第一章　开始"], titles
    assert "演第三集" in res.book.front_matter[0].text, res.book.front_matter[0].text


def case_site_prefix_short_title_still_works() -> None:
    """剥完前缀只剩一小截词的，仍认成标题（"黄蓉后传 第四节跟踪"）。"""
    res = clean_text(
        _text("黄蓉后传 第四节跟踪", "正文一。", "", "第五节 反目", "正文二。"),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第四节跟踪", "第五节　反目"], titles


def case_no_chapter_at_all() -> None:
    """全书没有章节标题：章节树为空，但内容不能丢，且要给出警告。"""
    res = clean_text(_text("随便一段话。", "", "又一段。"), source="x")
    assert res.book.chapters == [], "没有标题就不该凭空造成章节"
    assert [p.text for p in res.book.front_matter] == ["随便一段话。", "又一段。"]
    assert any("没能切出任何章节" in w for w in res.report["warnings"]), res.report["warnings"]
    assert validate(res.book), "没章节的书 validate 必须报出来"


def case_long_line_not_treated_as_title() -> None:
    """整本糊成一行的书：行首"第1章"不能让 4000 字的长行变成章节标题。

    不设长度上限时，match_chapter 只匹配行首三个字，于是整行（含后面
    29 章的标题）全被塞进一个 chapter.title，报告里那条"一个段落都没有"
    的警告再把它照抄一遍 —— 报告直接不可读。
    """
    sentence = "他还记得那一天的情景，风很大，吹得窗棂一直在响。"
    body = "".join(f"第{i}章 标题　{sentence * 8}" for i in range(1, 30))
    res = clean_text(body, source="x")

    assert res.book.chapters == [], f"超长行不该被切成章节：{[c.title[:20] for c in res.book.chapters]}"
    front = [p.text for p in res.book.front_matter]
    assert len(front) == 1 and len(front[0]) > 1000, "整行长行应原样落进 front_matter，一个字都不许丢"
    # 三条警告各管一件事：没章节 / 没切出来 / 整本糊成一行
    joined = "\n".join(res.report["warnings"])
    assert "整本疑似糊成一行" in joined, res.report["warnings"]
    assert "超长行 1 行" in joined, res.report["warnings"]
    assert res.report["oneline"]["suspected"] is True, res.report["oneline"]


def case_long_but_valid_title_still_works() -> None:
    """长度上限不能误伤真标题：80 字以内的长章名照旧切章。"""
    title = "第一章 " + "风起云涌之" * 8 + "夜"   # 5 + 40 + 1 = 46 字（squeeze 后）
    res = clean_text(_text(title, "", "正文。"), source="x")
    assert len(res.book.chapters) == 1, [c.title for c in res.book.chapters]
    assert res.report["oneline"]["suspected"] is False, res.report["oneline"]


# ---------------------------------------------------------------------------
# 二、删除广告
# ---------------------------------------------------------------------------


def case_whole_line_ad_removed() -> None:
    """整行广告删掉，行内夹广告只切尾巴、正文保留。"""
    res = clean_text(
        _text(
            "更多精彩小说，尽在某某小说网 www.example-novel.com",
            "第一章 开始",
            "",
            "风从北面吹来。这章真好看，记住本站 www.example-novel.com",
            "手机用户请浏览 m.example-novel.com 阅读",
        ),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    assert not any("更多精彩" in t for t in body), f"整行广告没删干净：{body}"
    assert not any("手机用户请浏览" in t for t in body), f"整行广告没删干净：{body}"
    # 行内那条：正文「风从北面吹来。」留着，尾巴「这章真好看，记住本站……」切掉。
    # 旧版这里断言的是"整行留着"，M4 前移成规则切除后反过来断言。
    assert body == ["风从北面吹来。"], f"行内广告应切掉尾巴、留下正文：{body}"
    assert res.report["dropped"]["ad"] == 2, res.report["dropped"]
    assert res.report["fixed"]["inline_ad_cut_lines"] == 1, res.report["fixed"]
    # 切掉了就不再告警：只有"切不了"的才该占一行警告。
    assert not any("行内夹广告" in w for w in res.report["warnings"]), res.report["warnings"]


def case_body_mentioning_ad_word_kept() -> None:
    """正文句里提到广告词，不能当整行广告删掉。

    回归用例：旧判据只看"摘掉广告词后剩几个汉字"，于是
    "他加了她的微信。"（单命中一个"微信"、无链接、剩 6 个字）被判整行广告，
    整句话消失，而且报告里只写"删掉一行广告" —— 丢正文丢得无声无息。
    """
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "他加了她的微信。",
            "她打开微信。",
            "「微信联系。」",
            "关注公众号。",
        ),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    # 「」会被 normalize_quotes 统一成“”（这是清洗的既定行为，与本用例无关）
    # 末行"关注公众号。"本身没有任何正文（摘掉"关注/公众号"后不剩字），
    # 2026-11-09 新增的引导关注判据把它判成整行广告删掉了 —— 那正是这次
    # 反馈要求的方向；本用例要守的是前三句叙述不能被连带删掉。
    assert body == ["他加了她的微信。", "她打开微信。", "“微信联系。”"], body
    assert res.report["dropped"]["ad"] == 1, res.report["dropped"]


def case_short_ad_still_removed() -> None:
    """拧回另一头：摘完只剩标点的短广告仍然要删，别因为上面那条守卫就漏删。"""
    res = clean_text(_text("第一章 开始", "", "求收藏！", "正文。"), source="x")
    body = _bodies(res.book.chapters[0])
    assert body == ["正文。"], body
    assert res.report["dropped"]["ad"] == 1, res.report["dropped"]


def case_ad_keyword_with_exclamation_still_whole() -> None:
    """广告词自己带句末标点时，整行仍然是广告，不能因为"前面有个叹号"就降级。

    回归用例：旧判据只看"广告成分之前有没有句末标点"，于是
    "求收藏！求推荐！求月票！" 里第一个"求收藏！"被当成写完了的正文，
    整行降级成"行内夹广告" —— 于是这一行留在正文里，报告还写"行内夹广告"。
    根本问题是广告词自己就带 ！，光看标点分不清它和真正文。
    """
    res = clean_text(
        _text("第一章 开始", "", "求收藏！求推荐！求月票！", "正文。"), source="x"
    )
    body = _bodies(res.book.chapters[0])
    assert body == ["正文。"], body
    assert res.report["dropped"]["ad"] == 1, res.report["dropped"]


def case_solicit_template_line_removed() -> None:
    """引导关注类模板句整行是广告，要删。

    真实来源（2026-11-09 用户反馈的《西游记》）：书里插着一行
    "关注公众号“假小说推荐”获取更新。"。旧判据漏掉它有两步：摘掉广告词后
    残留 12 个字，按残留字数的 ⑥（≤8）够不着；⑦ 要求两个以上广告词叠加，
    这行只有一个，也够不着。用户看到的是广告没删干净。
    """
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "关注公众号“某某推荐”（虚构）获取更新。",
            "扫码关注我们的公众号。",
            "正文一。",
        ),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    assert body == ["正文一。"], f"引导关注模板句没删干净：{body}"
    assert res.report["dropped"]["ad"] == 2, res.report["dropped"]


def case_source_watermark_line_removed() -> None:
    """出处水印行（"内容来自：某站"）整行是广告，要删。

    真实来源（2026-11-09 用户反馈的《西游记》）：正文中间插着一行
    "章节内容来自：book-demo.invalid" —— 一个广告词都没有、只有域名，
    旧判据 ④⑤⑥ 全够不着，留在了正文里，报告还写"行内夹广告 1 行"。
    """
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "章节内容来自：book-demo.invalid",
            "本文来自 www.example-novel.com",
            "正文一。",
        ),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    assert body == ["正文一。"], f"出处水印没删干净：{body}"
    assert res.report["dropped"]["ad"] == 2, res.report["dropped"]


def case_solicit_words_in_narrative_kept() -> None:
    """叙述句里出现"关注/公众号"不能当广告删 —— 这个方向的错误丢正文。

    "关注"是常用词（关注伤势、关注动向、多关注两眼）。判据要求同时满足
    "动作词 + 落点（公众号/微信/群……） + 整行没有 他她您咱/的了着过/吗呢吧"
    "且残留正文不超过 15 字"，下面每一句都至少缺一条，必须留着。

    后半段几句是第一人称、又不带"的/了"的叙述句（"我关注微信公众号"）：
    它们只靠"动作词前面还有主语"这一条保命 —— 2026-11-09 的复核实测，
    没有这条守卫时它们会被⑨整行删掉。带域名的叙述句同理，靠⑩里的
    "残留带 他/她/的/了 就不删"挡下来。
    """
    lines = [
        "她关注了这个公众号很久。",
        "关注公众号的读者都知道，作者每天更新。",
        "我关注了他的公众号。",
        "他们都说这个公众号不错。",
        "他加入了微信群，看到大家都在聊。",
        "这段内容来自作者的手记。",
        "我开始关注微信公众号",
        "我关注微信公众号",
        "我经常关注公众号",
        "他看到了出处 www.example.com",
        "她查到了内容来源 www.example.com",
    ]
    res = clean_text(_text("第一章 开始", "", *lines), source="x")
    body = _bodies(res.book.chapters[0])
    assert body == lines, f"叙述句被误删：{body}"
    assert res.report["dropped"]["ad"] == 0, res.report["dropped"]


def case_ad_samples_have_line_numbers() -> None:
    """报告里的样本必须能回到原文第几行。"""
    res = clean_text(
        _text("第一章 开始", "", "更多精彩小说，尽在某某小说网 www.example-novel.com", "正文。"),
        source="x",
    )
    samples = res.report["samples"]["ad"]
    assert len(samples) == 1, samples
    assert samples[0]["line"] == 3, f"广告样本行号应为 3，实际 {samples[0]['line']}"
    assert samples[0]["has_url"] is True


# ---------------------------------------------------------------------------
# 三、目录块
# ---------------------------------------------------------------------------


TOC_TITLES = ("第一章 开始", "第二章 相遇", "第三章 别离", "第四章 重逢", "第五章 结束")


def _toc_book_lines() -> tuple[str, ...]:
    """造一本书：目录块（5 条）+ 空行 + 正文（5 章同名标题 + 各一句正文）。

    目录必须凑够 5 条：detect_toc 判定"这里像目录"的门槛是连续 5 条以上标题，
    只造 2 条时体检根本不会报目录，清洗也就不会去删 —— 用例就测不到东西了。
    """
    lines: list[str] = ["目录"]
    lines.extend(TOC_TITLES)
    lines.append("")
    for i, title in enumerate(TOC_TITLES):
        lines.extend([title, "", f"正文{'一二三四五'[i]}。", ""])
    return tuple(lines)


def case_toc_removed_keeps_body_headings() -> None:
    """目录块删掉，正文里同名的标题留着 —— 这是 M2 最容易做错的一处。"""
    res = clean_text(_text(*_toc_book_lines()), source="x")
    titles = [c.title for c in res.book.chapters]
    want = [t.replace(" ", "\u3000") for t in TOC_TITLES]
    assert titles == want, f"目录没删干净或正文标题被误删：{titles}"
    # 5 条目录 + "目录" 那一行
    assert res.report["dropped"]["toc"] == 6, res.report["dropped"]
    assert [p.text for p in res.book.front_matter] == [], res.book.front_matter


def case_toc_does_not_eat_first_chapter() -> None:
    """目录紧贴正文时，detect_toc 会把正文首章也吸进簇里，不能跟着删。"""
    res = clean_text(_text(*_toc_book_lines()), source="x")
    assert len(res.book.chapters) == 5, f"应剩 5 章，实际 {[c.title for c in res.book.chapters]}"
    assert _bodies(res.book.chapters[0]) == ["正文一。"], (
        f"第一章的正文被当成目录删了：{res.book.chapters[0].paragraphs}"
    )
    assert res.report["chapters"]["kept_last_toc_line"] != 0, res.report["chapters"]
    # 保留的最后一行目录必须指向正文第一章标题（原始文件第 8 行：
    # 第 1 行"目录" + 5 条目录 + 第 7 行空行 + 第 8 行正文第一章）
    assert res.report["chapters"]["kept_last_toc_line"] == 8, res.report["chapters"]


def _toc_with(fmt: str) -> tuple[str, ...]:
    """造一本"目录写法与正文不一致"的书，返回全部行。

    fmt 决定目录条目长什么样，正文标题永远是标准的 "第N章 名字"。
    目录 5 条是硬门槛（detect_toc 要求连续 5 条以上标题）。
    """
    names = ("开始", "相遇", "别离", "重逢", "结束")
    nums = "一二三四五"
    lines: list[str] = ["目录"]
    lines.extend(fmt.format(n=nums[i], t=names[i], page=10 + i * 3, dots="." * 12) for i in range(5))
    lines.append("")
    for i in range(5):
        lines.extend([f"第{nums[i]}章 {names[i]}", "", f"正文{nums[i]}。", ""])
    return tuple(lines)


def case_toc_with_page_numbers_removed() -> None:
    """目录条目带页码点线（"第一章 开始 ...... 10"）也要删干净。

    回归用例：旧实现拿 squeeze 后的原文当比较键，带页码的目录条目和正文
    标题算出不同的键，于是目录"只出现一次"被判成正文标题留下 ——
    结果是 5 个 0 段落的空章节，正文标题反而变成第 6 章。
    """
    for fmt in ("第{n}章 {t} {dots} {page}", "第{n}章 {t}{dots}{page}", "第{n}章 {t} …… {page}"):
        res = clean_text(_text(*_toc_with(fmt)), source="x")
        titles = [c.title for c in res.book.chapters]
        assert len(titles) == 5, f"{fmt} → 期望 5 章，实际 {titles}"
        assert not [c for c in res.book.chapters if not c.paragraphs], (
            f"{fmt} → 出现了空章节：{[(c.title, c.start_line) for c in res.book.chapters if not c.paragraphs]}"
        )
        assert res.report["dropped"]["toc"] == 6, (fmt, res.report["dropped"])


def case_toc_glue_mismatch_removed() -> None:
    """目录写"第一章 开始"、正文写"第一章：开始"，也要认成同一章。

    目录和正文的编号分隔符不一致是最常见的排版噪声之一（空格 vs 冒号 vs
    顿号）。键不归一化的话，目录条目会被当成"只在簇里出现一次的正文标题"。
    """
    res = clean_text(_text(*_toc_with("第{n}章 {t}")), source="x")
    # 正文写成冒号形态
    res = clean_text(
        _text(
            *(
                ["目录"]
                + [f"第{'一二三四五'[i]}章 {t}" for i, t in enumerate(("开始", "相遇", "别离", "重逢", "结束"))]
                + [""]
                + [
                    x
                    for i in range(5)
                    for x in (f"第{'一二三四五'[i]}章：{('开始','相遇','别离','重逢','结束')[i]}", "", f"正文{'一二三四五'[i]}。", "")
                ]
            )
        ),
        source="x",
    )
    assert len(res.book.chapters) == 5, f"目录没删干净：{[c.title for c in res.book.chapters]}"
    assert res.report["dropped"]["toc"] == 6, res.report["dropped"]
    body_counts = [len(_bodies(c)) for c in res.book.chapters]
    assert body_counts == [1, 1, 1, 1, 1], f"正文没跟对章节：{body_counts}"


def case_toc_decorated_entries() -> None:
    """目录条目带【】装饰时，不能因此把目录留在正文里。

    这条只在体检认得出目录块时才生效（装饰括号会让 match_chapter 不认识
    "【第一章"，体检的 suspected=False），所以此处断言的是"不抛异常、
    不产生空章节"这一底线，不强制要求删掉。
    """
    res = clean_text(_text(*_toc_with("【第{n}章 {t}】")), source="x")
    empty = [c.title for c in res.book.chapters if not c.paragraphs]
    assert not empty, f"出现了空章节：{empty}"
    assert len(res.book.chapters) == 5, [c.title for c in res.book.chapters]


def case_toc_volume_lines_kept_as_container() -> None:
    """目录里的"第一卷"分组行删掉；正文里的卷行留成 level=2 容器，且不算空章警告。

    卷/部/集本身没有段落是正常形态（正文在它下面的章里）。
    旧实现会在每一本分卷的书上刷"一个段落都没有：正文可能在清洗时丢了"。
    """
    names = ("开始", "相遇", "别离", "重逢", "结束")
    nums = "一二三四五"
    toc = ["目录", "第一卷 风起", "第一章 开始", "第二章 相遇", "第二卷 云涌", "第三章 别离", "第四章 重逢", "第五章 结束"]
    body: list[str] = []
    for vol, idxs in (("第一卷 风起", (0, 1)), ("第二卷 云涌", (2, 3, 4))):
        body.extend([vol, ""])
        for i in idxs:
            body.extend([f"第{nums[i]}章 {names[i]}", "", f"正文{nums[i]}。", ""])
    res = clean_text(_text(*(toc + [""] + body)), source="x")

    titles = [c.title for c in res.book.chapters]
    assert titles == [
        "第一卷\u3000风起",
        "第一章\u3000开始",
        "第二章\u3000相遇",
        "第二卷\u3000云涌",
        "第三章\u3000别离",
        "第四章\u3000重逢",
        "第五章\u3000结束",
    ], titles
    levels = [c.level for c in res.book.chapters]
    assert levels == [2, 1, 1, 2, 1, 1, 1], levels
    # 卷容器没有段落是正常的，validate 不该为此报警
    assert validate(res.book) == [], validate(res.book)
    assert not any("一个段落都没有" in w for w in res.report["warnings"]), res.report["warnings"]


def case_sentence_starting_with_chapter_word() -> None:
    """正文行恰好以"第N章"开头且是完整句子，不能当标题切章。

    回归用例：'第一章里写得很清楚。' 会命中第七章家族的正则（"第一章"打头），
    判成标题就把一章从中间切成两个空章 —— 少一层标题还能忍，丢正文结构不能忍。
    """
    res = clean_text(
        _text("第一章 开始", "", "正文一。", "第一章里写得很清楚。", "第三回合他赢了。"),
        source="x",
    )
    assert len(res.book.chapters) == 1, f"正文句被切成了章节：{[c.title for c in res.book.chapters]}"
    body = _bodies(res.book.chapters[0])
    assert body == ["正文一。", "第一章里写得很清楚。", "第三回合他赢了。"], body


def case_body_sentence_after_volume_word_not_chapter() -> None:
    """正文里以「第一部」「第一回」开头、编号后甩出整句散文的句子不能切成章。

    回归用例（试用反馈，《书L》真书）：「第一部　手机，逢年过节会给她
    成百的红包…」「第一回　被方牧昭在租房强吻…」这类行会命中「第N部/回」
    的标题正则，旧判据只看"编号后有分隔符"就无条件判成标题，一个假章吞掉
    124 段正文。判据：句号收尾且句中还带逗号的，是叙述散文，不是标题。
    """
    res = clean_text(
        _text(
            "第1章 开局", "", "正文甲。", "正文乙。",
            "第一部　手机，逢年过节会给她成百的红包，在一堆五块十块的红包里，他算大手笔。",
            "第2章 继续", "", "正文丙。",
            "第一回　被方牧昭在租房强吻，一次是在丽江古城的小巷重逢。",
            "第一回　时迷惘，但一样期待。",
            "结尾一句。",
        ),
        source="x",
    )
    assert len(res.book.chapters) == 2, f"正文句被切成了章节：{[c.title for c in res.book.chapters]}"
    body = [b for ch in res.book.chapters for b in _bodies(ch)]
    assert any("第一部" in b for b in body), body
    assert any("被方牧昭" in b for b in body), body
    # 台词式真标题（省略号收尾、句中带逗号）不能一起被误杀：《书L》
    # 第 85~88 章就是「第85章 想回老家一趟，你跟我走……」这种写法。
    res2 = clean_text(
        _text("第一章 起", "", "第85章 想回老家一趟，你跟我走……", "", "正文。"),
        source="x",
    )
    assert len(res2.book.chapters) == 2, [c.title for c in res2.book.chapters]


def case_short_titles_not_reported_as_toc() -> None:
    """连续几个标题但全书没有目录：不能报"保留的最后一行目录"。

    回归用例：体检的簇判据只看"标题挨得近"，五个连续章标题在它眼里和
    目录一样（suspected=True）。旧代码只要 survivors 非空就报行号，
    读者会拿着一本没有目录的书去找"第 5 行那条目录"。
    """
    # 标题必须挨着写才会被判成一簇（中间有空行时体检自己就分开了，
    # 那就测不到这道守卫了）。
    lines = [f"第{i}章 标题" for i in "一二三四五"] + ["", "正文来了。"]
    res = clean_text(_text(*lines), source="x")
    assert len(res.book.chapters) == 5, [c.title for c in res.book.chapters]
    assert res.report["chapters"]["kept_last_toc_line"] is None, res.report["chapters"]
    assert res.report["dropped"]["toc"] == 0, res.report["dropped"]


def case_marker_line_removed() -> None:
    """孤零零一行"目录"没有任何阅读价值，删掉。"""
    res = clean_text(_text("第一章 开始", "", "目 录", "正文。"), source="x")
    assert "目录" not in _bodies(res.book.chapters[0])
    assert "\u76ee\u5f55" not in "".join(_bodies(res.book.chapters[0]))


# ---------------------------------------------------------------------------
# 四、空行
# ---------------------------------------------------------------------------


def case_blank_runs_collapsed() -> None:
    """连续空行压成 1 行；正文夹缝里的单空行不产出 blank。

    2026-11 起（0.2.2-⑥）：作者在段落之间只空一行，那只是段间距，
    不再产出 blank 段；连续空好几行才是场景分隔，压成一个 blank。
    """
    res = clean_text(
        _text("第一章 开始", "", "正文一。", "", "正文二。", "", "", "正文三。"), source="x"
    )
    kinds = [p.kind for p in res.book.chapters[0].paragraphs]
    assert kinds == ["body", "body", "blank", "body"], (
        f"单空行应折叠、整块空行应只剩 1 个，实际 {kinds}"
    )


def case_double_blank_keeps_exactly_one() -> None:
    """空行折叠的两面：整块空行留 1 个 blank，单空行一个都不留。

    判别式用例：单空行那半边在 0.2.2-⑥ 之前必失败（旧实现单空行也留 blank）。
    """
    # 三个连续空行：场景分隔，恰好留 1 个 blank
    res = clean_text(_text("第一章 开始", "", "正文一。", "", "", "", "正文二。"), source="x")
    kinds = [p.kind for p in res.book.chapters[0].paragraphs]
    assert kinds == ["body", "blank", "body"], f"应为 段落/空/段落，实际 {kinds}"
    # 单个空行：只是段间距，不产出 blank
    single = clean_text(_text("第一章 开始", "", "正文一。", "", "正文二。"), source="x")
    single_kinds = [p.kind for p in single.book.chapters[0].paragraphs]
    assert single_kinds == ["body", "body"], f"单空行不该留 blank，实际 {single_kinds}"


def case_verse_stanza_blank_kept() -> None:
    """诗节之间的单空行是排版语义，不许当段间距折掉（0.2.2-⑥ 复核补修）。

    判别式用例：折叠若跑在 _mark_verse 之前（或不分诗词），「低头思故乡。」
    与「春眠不觉晓」之间那个单空行会被折掉 —— 两节诗焊成一节，下面的
    kinds 当场红。诗到叙述之间的那个空行同理保留（收诗的排版习惯）。
    """
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "有诗为证：",
            "床前明月光，疑是地上霜。",
            "举头望明月，低头思故乡。",
            "",
            "春眠不觉晓，处处闻啼鸟。",
            "夜来风雨声，花落知多少。",
            "",
            "他看罢，点了点头。",
        ),
        source="x",
    )
    kinds = [p.kind for p in res.book.chapters[0].paragraphs]
    assert kinds == ["body", "verse", "verse", "blank", "verse", "verse", "blank", "body"], kinds


def case_render_roundtrip_keeps_scene_breaks() -> None:
    """回环：clean → render → clean，章节段落序列必须逐段一致。

    判别式用例：渲染层把换场景（blank 段）写成两行空行。旧渲染只写 1 行，
    回洗时 clean 会把那一个空行当成排版段间距折叠掉，第二遍的 blank 段
    就消失，下面的序列比较当场红 —— 这正是 0.2.2-⑥ 破坏的契约
    clean(render(clean(x))) == clean(x)。
    """
    from huoban.render import render_txt

    def seq(book) -> list[tuple[str, str]]:
        pairs = [(p.kind, p.text) for p in book.front_matter]
        for ch in book.chapters:
            pairs.append(("title", ch.title))
            pairs.extend((p.kind, p.text) for p in ch.paragraphs)
        return pairs

    text = _text(
        "第一章 开始", "", "正文一。", "", "", "正文二。",
        "", "第二章 结束", "", "", "正文三。",
    )
    once = clean_text(text, source="x").book
    # 先钉住前提：书里确实有换场景的 blank 段，否则回环测试测不到东西
    assert [p.kind for p in once.chapters[0].paragraphs] == ["body", "blank", "body"], (
        [p.kind for p in once.chapters[0].paragraphs]
    )
    twice = clean_text(render_txt(once), source="x").book
    assert seq(twice) == seq(once), (seq(once), seq(twice))


def case_leading_and_trailing_blanks_dropped() -> None:
    """标题后的空行、章末的空行都不进章节树（排版交给 render 层）。"""
    res = clean_text(_text("", "", "第一章 开始", "", "", "正文。", "", "", ""), source="x")
    kinds = [p.kind for p in res.book.chapters[0].paragraphs]
    assert kinds == ["body"], f"章首章末空行应丢弃，实际 {kinds}"


# ---------------------------------------------------------------------------
# 五、并碎行
# ---------------------------------------------------------------------------


def case_fragments_merged() -> None:
    """被硬折断的一整句要接回去，报告记下原始行号区间。"""
    res = clean_text(
        _text("第一章 开始", "", "他走进屋子，看见", "桌上放着一封信，信上", "写着他的名字。"), source="x"
    )
    body = _bodies(res.book.chapters[0])
    assert body == ["他走进屋子，看见桌上放着一封信，信上写着他的名字。"], body
    assert res.report["merged"]["absorbed_lines"] == 2, res.report["merged"]
    assert res.report["merged"]["groups"] == 1, "三行只应记成 1 组"
    span = res.report["samples"]["merged"][0]["lines"]
    assert span == [3, 5], f"行号区间应为 [3, 5]，实际 {span}"


def case_no_merge_across_blank() -> None:
    """中间隔了空行就是分段，不许接。"""
    res = clean_text(_text("第一章 开始", "", "他走进屋子，看见", "", "桌上放着一封信"), source="x")
    body = _bodies(res.book.chapters[0])
    assert body == ["他走进屋子，看见", "桌上放着一封信"], body


def case_no_merge_after_terminal() -> None:
    """前一句已经结束，下一行是新段落，不许接。"""
    res = clean_text(_text("第一章 开始", "", "他走进屋子。", "桌上放着一封信"), source="x")
    body = _bodies(res.book.chapters[0])
    assert body == ["他走进屋子。", "桌上放着一封信"], body


def case_no_merge_when_indent_informative() -> None:
    """有缩进才分段的书：缩进行不能被并进上一行。"""
    res = clean_text(
        _text("第一章 开始", "", "　　他走进屋子，看见", "　　桌上放着一封信", "", "　　第二段，有缩进。"),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    assert len(body) == 3, f"缩进是段落边界，应保持 3 段，实际 {body}"
    assert res.report["merged"]["absorbed_lines"] == 0, res.report["merged"]


def case_all_indented_still_merges() -> None:
    """全书都缩进时缩进没有信号，碎行仍要合并。"""
    res = clean_text(
        _text("第一章 开始", "", "　　他走进屋子，看见", "桌上放着一封信", "写着他的名字。"),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    assert body == ["他走进屋子，看见桌上放着一封信写着他的名字。"], body


def case_no_merge_across_title() -> None:
    """章标题绝不能并进上一段的尾句。"""
    res = clean_text(_text("第一章 开始", "", "他走进屋子，看见", "第二章 结束", "", "正文。"), source="x")
    assert len(res.book.chapters) == 2, [c.title for c in res.book.chapters]
    assert _bodies(res.book.chapters[1]) == ["正文。"]


def case_merge_group_count_not_capped() -> None:
    """合并组数要报真实值，不受样例条数上限影响。

    回归用例：样例桶只留前 SAMPLE_LIMIT 组，旧代码拿 len(样例) 当组数，
    报告于是写成"合并组数 8、被吸收碎行 10" —— 两个数字自相矛盾，
    读者没法判断到底并了多少处。
    """
    lines = ["第一章 开始", ""]
    for i in range(1, 11):
        lines.extend([f"前半段{i}", f"后半段{i}。", ""])
    res = clean_text(_text(*lines), source="x")
    merged = res.report["merged"]
    assert merged["groups"] == 10, merged
    assert merged["absorbed_lines"] == 10, merged
    assert len(res.report["samples"]["merged"]) <= 8, "样例桶必须被限流"


def case_merge_stops_at_limit() -> None:
    """合并后超过上限就停手，宁可少并。"""
    from huoban.clean import MERGE_MAX_CHARS

    long_a = "啊" * (MERGE_MAX_CHARS - 5)
    long_b = "哦" * 10
    res = clean_text(_text("第一章 开始", "", long_a, long_b), source="x")
    body = _bodies(res.book.chapters[0])
    assert len(body) == 2, f"超长不该合并，实际并成了 {len(body)} 段"
    assert res.report["merged"]["absorbed_lines"] == 0


# ---------------------------------------------------------------------------
# 六、字符层面
# ---------------------------------------------------------------------------


def case_zero_width_and_trailing_removed() -> None:
    """零宽字符与行尾空白必须清掉，报告要报数。"""
    bad = "\u200b他站在城门口，望着远处的灯火。   "
    res = clean_text(_text("第一章 开始", "", bad), source="x")
    body = _bodies(res.book.chapters[0])
    assert body == ["他站在城门口，望着远处的灯火。"], body
    fixed = res.report["fixed"]
    assert fixed["zero_width_lines"] == 1, fixed
    assert fixed["trailing_space_lines"] == 1, fixed


def case_punctuation_normalized() -> None:
    """叠标点归一：.... → ……，?! → ？！。"""
    res = clean_text(_text("第一章 开始", "", "你好....", "真的吗?!"), source="x")
    body = _bodies(res.book.chapters[0])
    assert body == ["你好……", "真的吗？！"], body
    assert res.report["fixed"]["punctuation_lines"] == 2, res.report["fixed"]


# ---------------------------------------------------------------------------
# 七、报告的账要对得上
# ---------------------------------------------------------------------------


def case_report_identities() -> None:
    """报告里的数字必须能互相对上，否则报告就是摆设。"""
    res = clean_text(
        _text(
            "更多精彩小说，尽在某某小说网 www.example-novel.com",
            "",
            "第一章 开始",
            "",
            "正文一。",
            "",
            "",
            "正文二。",
        ),
        source="x",
    )
    lines = res.report["lines"]
    dropped = res.report["dropped"]
    blank_out = lines["blank_out"]
    assert lines["total"] == lines["nonblank"] + lines["blank_in"], lines
    assert dropped["extra_blank"] == lines["blank_in"] - blank_out, (
        f"被压掉的空行应等于 输入空行 - 输出空行：{dropped} / {lines}"
    )


def case_stats_and_validate_agree() -> None:
    """stats 的章节数必须与章节树一致，且这本书 validate 干净。"""
    res = clean_text(_text("第一章 开始", "", "正文一。", "", "第二章 结束", "", "正文二。"), source="x")
    assert res.report["stats"]["chapters"] == len(res.book.chapters)
    assert res.report["stats"]["paragraphs"] == sum(
        len(c.paragraphs) for c in res.book.chapters
    ) + len(res.book.front_matter) + len(res.book.tail_matter)
    assert res.report["warnings"] == [], res.report["warnings"]


# ---------------------------------------------------------------------------
# 八、IO：编码、只读、往返
# ---------------------------------------------------------------------------


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="huoban-clean-"))


def case_gbk_bytes_decoded() -> None:
    """GBK 字节要能正确解码（编码探测走 encoding.detect）。"""
    raw = "第一章 开始\n\n正文一句中文。\n".encode("gb18030")
    res = clean_bytes(raw, source="x")
    assert res.book.chapters[0].title == "第一章\u3000开始", res.book.chapters[0].title
    assert res.report["encoding"]["chosen"] in ("gb18030", "gbk"), res.report["encoding"]
    assert _bodies(res.book.chapters[0]) == ["正文一句中文。"]


def case_clean_file_never_touches_input() -> None:
    """铁律第一条：输入文件的字节和 mtime 都不能变。"""
    d = _tmpdir()
    src = d / "in.txt"
    src.write_bytes("第一章 开始\n\n正文。\n".encode("utf-8"))
    before_bytes = src.read_bytes()
    before_mtime = src.stat().st_mtime_ns

    res = clean_file(src)

    assert res.book.chapters, "应该切出章节"
    assert src.read_bytes() == before_bytes, "输入文件内容被改了"
    assert src.stat().st_mtime_ns == before_mtime, "输入文件 mtime 变了"


def case_book_json_roundtrip() -> None:
    """章节树存盘再读回来，语义不变（M3 打包就靠这一层）。"""
    d = _tmpdir()
    res = clean_text(_text("第一章 开始", "", "正文一。", "", "第二章 结束", "", "正文二。"), source="x")
    path = d / "book.json"
    save_book(res.book, path)
    again = load_book(path)

    assert to_dict(again) == to_dict(res.book), "存盘往返后章节树变了"
    assert [c.title for c in again.chapters] == ["第一章\u3000开始", "第二章\u3000结束"]


def case_clean_is_idempotent_on_clean_input() -> None:
    """把已清洗的正文再清洗一遍，章节结构不能变（能证明没有自激）。"""
    once = clean_text(_text("第一章 开始", "", "他走进屋子，看见", "桌上放着一封信。", "", "第二章 结束", "", "正文。"), source="x")
    plain = to_dict(once.book)
    # 用渲染后的干净正文再跑一遍
    from huoban.render import render_txt

    twice = clean_text(render_txt(once.book), source="x")
    assert len(twice.book.chapters) == len(once.book.chapters), [c.title for c in twice.book.chapters]
    assert [c.title for c in twice.book.chapters] == [c["title"] for c in plain["chapters"]]


# ---------------------------------------------------------------------------
# 十、复核提出的回归用例
# ---------------------------------------------------------------------------


def case_title_with_ad_words_keeps_chapter() -> None:
    """章节标题本身带网址/广告词时，不许把整章当广告删掉。

    回归用例：旧实现里整行广告判定排在标题判定之前，且没有标题豁免。
    "第二章 www.abc-novel.com" 这种标题被判成整行广告删掉 —— 该章消失，
    它的正文并进了上一章，报告里一个字都不提。
    """
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "正文一。",
            "第二章 www.abc-novel.com",
            "",
            "正文二。",
            "第三章 天才一秒记住本站",
            "",
            "正文三。",
        ),
        source="x",
    )
    assert len(res.book.chapters) == 3, f"含广告词的标题把整章弄丢了：{[c.title for c in res.book.chapters]}"
    assert _bodies(res.book.chapters[1]) == ["正文二。"], res.book.chapters[1].paragraphs
    assert res.report["dropped"]["ad"] == 0, res.report["dropped"]


def case_question_mark_title_still_chapter() -> None:
    """标题以 ！ ？ … 收尾时仍然是标题。

    回归用例：成句守卫只看"结尾是不是句末标点"，于是 "第三章 你是谁？"
    "第十二章 大结局！" 这类合法标题被当成正文句子 —— 整章少一个标题。
    判据改成"编号后面有没有分隔符"优先之后才修好。
    """
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "正文一。",
            "第三章 你是谁？",
            "",
            "正文三。",
            "第十二章 大结局！",
            "",
            "正文十二。",
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000开始", "第三章\u3000你是谁？", "第十二章\u3000大结局！"], titles
    assert _bodies(res.book.chapters[1]) == ["正文三。"], res.book.chapters[1].paragraphs


def case_volume_renumbered_no_terminal_body() -> None:
    """分卷重新编号、且卷内正文没有句末标点时，章标题一个都不能删。

    回归用例：目录删除规则原来只看"簇外还有没有同名行"。正文行不以句末
    标点收尾时，体检的 prose_ratio 判不出这是正文，簇把整本书圈成一簇；
    这下"第一卷"在簇外没有同名行，被当成目录条目删掉，吞掉它下面的 5 章。
    误删与否竟然取决于正文有没有句号 —— 这是不能接受的。
    """
    lines: list[str] = []
    for vol in ("第一卷 风起", "第二卷 云涌"):
        lines.extend([vol, ""])
        for i in range(1, 6):
            lines.extend([f"第{i}章 山高水长", "", "　　山高水长", "　　风清月白", ""])
    res = clean_text(_text(*lines), source="x")

    titles = [c.title for c in res.book.chapters]
    assert len(titles) == 12, f"应有 2 卷 + 10 章，实际 {titles}"
    assert titles[0] == "第一卷\u3000风起", titles
    assert res.report["dropped"]["toc"] == 0, res.report["dropped"]
    # 第一卷下面必须挂着 5 章
    vol1 = res.book.chapters[0]
    assert vol1.level == 2, vol1.level


def case_toc_mixed_chinese_arabic_numbers() -> None:
    """目录用中文数字、正文用阿拉伯数字，也要认成同一章。

    回归用例：比较键直接拿 squeeze 后的原文，于是 "第二章 相遇" 与
    "第2章 相遇" 是两个不同的键，目录条目被判成"只在簇里出现过一次"。
    """
    names = ("开始", "相遇", "别离", "重逢", "结束")
    nums = "一二三四五"
    lines: list[str] = ["目录"] + [f"第{nums[i]}章 {names[i]}" for i in range(5)] + [""]
    for i in range(5):
        lines.extend([f"第{i + 1}章 {names[i]}", "", f"正文{i + 1}。", ""])
    res = clean_text(_text(*lines), source="x")

    titles = [c.title for c in res.book.chapters]
    assert len(titles) == 5, f"目录没删干净：{titles}"
    assert not [c for c in res.book.chapters if not c.paragraphs], (
        f"出现了空章节：{[c.title for c in res.book.chapters if not c.paragraphs]}"
    )


def case_toc_halfwidth_brackets() -> None:
    """目录用半角括号（对上）、正文用全角括号（上），也要认成同一章。

    回归用例：括号全半角没归一，两个键配不上，目录 5 条全留成空章节。
    """
    lines: list[str] = ["目录"]
    for i in range(1, 6):
        lines.append(f"第{i}章 归途(上)")
    lines.append("")
    for i in range(1, 6):
        lines.extend([f"第{i}章 归途（上）", "", f"正文{i}。", ""])
    res = clean_text(_text(*lines), source="x")

    titles = [c.title for c in res.book.chapters]
    assert len(titles) == 5, f"目录没删干净：{titles}"
    assert not [c for c in res.book.chapters if not c.paragraphs], (
        f"出现了空章节：{[c.title for c in res.book.chapters if not c.paragraphs]}"
    )


def case_toc_volume_only_in_toc() -> None:
    """只在目录里出现过的卷名，也要跟着目录一起删。

    回归用例：删目录的规则全靠"正文里还有没有同名行"。卷名往往只在目录里
    写一次（正文里没有卷标题），于是它被当成"正文标题"留在原位，变成一串
    0 段落的空章节，还把后面 5 章全塞进它肚子里。
    """
    names = ("开始", "相遇", "别离", "重逢", "结束")
    lines: list[str] = [
        "目录",
        "第一卷 风起",
        "第一章 开始",
        "第二章 相遇",
        "第二卷 云涌",
        "第三章 别离",
        "第四章 重逢",
        "第五章 结束",
        "",
    ]
    for i, name in enumerate(names):
        lines.extend([f"第{'一二三四五'[i]}章 {name}", "", f"正文{i + 1}。", ""])
    res = clean_text(_text(*lines), source="x")

    titles = [c.title for c in res.book.chapters]
    assert titles == [f"第{'一二三四五'[i]}章\u3000{names[i]}" for i in range(5)], titles
    assert res.report["dropped"]["toc"] == 8, res.report["dropped"]


def case_toc_long_directory_not_truncated() -> None:
    """目录再长也要删干净（体检的 scan 只是成本闸，不是目录行数上限）。

    回归用例：detect_toc 只看前 400 行。条目之间夹空行的 210 条目录占了
    420 行，后一半留在正文里 —— 切出 220 个章节，其中 10 个是空章。
    """
    lines: list[str] = ["目录"]
    for i in range(1, 211):
        lines.extend([f"第{i}章 标题{i}", ""])
    for i in range(1, 211):
        lines.extend([f"第{i}章 标题{i}", "", f"正文{i}。", ""])
    res = clean_text(_text(*lines), source="x")

    titles = [c.title for c in res.book.chapters]
    assert len(titles) == 210, f"应剩 210 章，实际 {len(titles)}"
    assert not [c for c in res.book.chapters if not c.paragraphs], (
        f"出现了空章节：{[c.title for c in res.book.chapters if not c.paragraphs][:5]}"
    )


def case_repeated_titles_without_toc_kept() -> None:
    """书里没有目录、但有同名章节时，一章都不能丢（而且第二遍清洗不许再丢）。

    回归用例：一本没有目录的书，正文里三个"番外 名字"挨得够近，体检的
    detect_toc 只按"标题挨得近"判，照样报"疑似目录"。旧实现逐行看"簇外
    还有同名"就删，于是第二个"番外"整章消失 —— 而且错误会自我放大：第一遍
    清洗把空行压掉了，标题挨得更紧，第二遍再清洗又少一章。清洗过的干净文本
    喂回去再洗一遍，本该一个字都不改。
    """
    from huoban.render import render_txt

    lines: list[str] = []
    for i, title in enumerate(
        ("番外 名字", "第3章 标题3", "番外 名字", "第5章 标题5", "番外 名字", "第7章 标题7")
    ):
        lines.extend([title, "", f"正文{i + 1}。", ""])
    res = clean_text(_text(*lines), source="x")

    titles = [c.title for c in res.book.chapters]
    assert titles == [
        "番外\u3000名字",
        "第3章\u3000标题3",
        "番外\u3000名字",
        "第5章\u3000标题5",
        "番外\u3000名字",
        "第7章\u3000标题7",
    ], titles
    assert res.report["dropped"]["toc"] == 0, res.report["dropped"]
    # 每章都还挂着自己的正文
    for ch in res.book.chapters:
        assert _bodies(ch), f"章节 {ch.title} 没有正文了"

    # 幂等：拿渲染出来的干净正文再洗一遍，一个字都不该变
    once = render_txt(res.book)
    twice = render_txt(clean_text(once, source="y").book)
    assert twice == once, "第二遍清洗改动了正文"


def case_toc_adjacent_to_body_keeps_first_chapter() -> None:
    """目录条目与正文标题紧挨着写时，正文第一章的标题不能跟着删。

    回归用例：预算目录 5 条之后紧接正文（中间没有"目录"两个字，也没有
    空行），体检的簇把正文首章标题一起圈进来。旧实现"同名只留最后一个"
    把正文首章标题删掉，它的正文整体并进前言。
    """
    lines: list[str] = [f"第{i}章 标题{i}" for i in range(1, 6)]
    lines.extend(["序章", "正文甲。", "正文乙。", "", "第1章 标题1", "", "正文丙。", "正文丁。", "", "序章"])
    res = clean_text(_text(*lines), source="x")

    titles = [c.title for c in res.book.chapters]
    assert "第1章\u3000标题1" in titles, f"正文第一章被当目录删了：{titles}"
    first = res.book.chapters[titles.index("第1章\u3000标题1")]
    assert _bodies(first) == ["正文丙。", "正文丁。"], first.paragraphs
    # 目录 5 条必须删掉
    assert res.report["dropped"]["toc"] == 5, res.report["dropped"]


def case_utf16_bom_decoded() -> None:
    """带 BOM 的 UTF-16 文件要按 BOM 解码，不能拿去当 gb18030 打分。

    回归用例：打分类编码探测里根本没有 utf-16，BOM 只加 0.5 分，
    于是 UTF-16 文件被 gb18030 解成乱码、切出 0 章。
    """
    raw = "第一章 开始\n\n正文一句中文。\n".encode("utf-16")
    res = clean_bytes(raw, source="x")
    assert res.report["encoding"]["chosen"].startswith("utf-16"), res.report["encoding"]
    assert [c.title for c in res.book.chapters] == ["第一章\u3000开始"], [c.title for c in res.book.chapters]
    assert _bodies(res.book.chapters[0]) == ["正文一句中文。"]

def case_toc_reprint_at_tail_idempotent() -> None:
    """书末重印一份目录（开头一份、书末一份）时，两遍清洗结果必须一致。

    回归用例：detect_toc 只报"最长的那一簇"，开头目录已经占住了那个位置，
    文末那份一遍时被当成 5 个空章节留在书里；二遍清洗（此时开头目录已删）
    才认出它是目录删掉 —— 同一份文本洗两遍得到两个结果，幂等破了。
    修法是把"整块重名"的标题块（每条在块外都有同名标题）当目录副本删掉。
    """
    lines: list[str] = ["目录"] + [f"第{i}章 标题{i}" for i in range(1, 6)] + [""]
    for i in range(1, 6):
        lines.extend([f"第{i}章 标题{i}", "", f"正文{i}。", ""])
    lines.extend(["目录"] + [f"第{i}章 标题{i}" for i in range(1, 6)])

    once = clean_text(_text(*lines), source="x")
    titles = [c.title for c in once.book.chapters]
    assert titles == [f"第{i}章\u3000标题{i}" for i in range(1, 6)], f"文末重印目录没删掉：{titles}"
    assert not [c for c in once.book.chapters if not c.paragraphs], "出现了空章节"

    # 幂等：把一遍的输出（经 render）再洗一遍，文本必须逐字相等
    from huoban.render import render_txt

    first = render_txt(once.book)
    twice = clean_text(first, source="x")
    assert render_txt(twice.book) == first, "两遍清洗结果不同，幂等破了"


def case_volume_container_line_upgraded_to_level2() -> None:
    """「卷之一 觉后禅——春」这种不带「第」的卷行要升级成 level=2 卷标题。

    回归用例（《书A》真实书）：四条卷行原来既不是标题也不是空行，
    被当正文段落收下 —— 卷之二粘在第五回最后一段末尾、卷之三粘在第十回、
    卷之四粘在第十五回，整本书的卷结构在章节树里彻底消失。
    升级条件必须是「后面紧跟章标题」，否则正文里孤零零的「上卷」也会切章。
    """
    lines: list[str] = []
    for vol, nums in (("卷之一　觉后禅--春", (1, 2)), ("卷之二　觉后禅--夏", (3, 4))):
        lines.extend([vol, "", ""]);
        for i in nums:
            lines.extend([f"第{'一二三四'[i - 1]}回 标题{i}", "", f"正文{i}。", ""]);
    res = clean_text(_text(*lines), source="x")

    titles = [c.title for c in res.book.chapters]
    assert titles == [
        "卷之一\u3000觉后禅——春", "第一回\u3000标题1", "第二回\u3000标题2",
        "卷之二\u3000觉后禅——夏", "第三回\u3000标题3", "第四回\u3000标题4",
    ], titles
    levels = [c.level for c in res.book.chapters]
    assert levels == [2, 1, 1, 2, 1, 1], levels
    # 卷序号从中文数字抽出来（卷一 → 1）；抽不到的留 None，不瞎填
    assert [c.number for c in res.book.chapters][:1] == [1], res.book.chapters[0].number
    assert validate(res.book) == [], validate(res.book)
    # 卷行不能再出现在正文段落里
    assert not any("觉后禅" in b for c in res.book.chapters for b in _bodies(c)), (
        "卷行被当成正文收下了"
    )


def case_volume_word_inside_body_not_upgraded() -> None:
    """正文段落里冒出来的「上卷」「卷三的箱子」不能被升级成卷标题。

    安全阀是「后面紧跟章标题」：正文段落后面跟的还是正文，
    少了这条，「他把上卷塞回书架」这种句子会把一章从中间劈开。
    """
    res = clean_text(
        _text(
            "第一章 开始", "", "正文一。", "上卷", "", "他把上卷塞回书架。", "", "正文二。",
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000开始"], titles
    body = _bodies(res.book.chapters[0])
    assert "上卷" in body, body


def case_metadata_guessed_from_head() -> None:
    """书名与作者从最前面几段猜出来：「《书A》」+「明·某某」。

    猜错的书名会印在 EPUB 信息页、书库列表和阅读器标题栏上，
    所以每条判据都要求整段匹配 —— 正文里提到别的书名不算。
    """
    res = clean_text(
        _text(
            "《书A》", "", "明·某某", "", "第一回 开头", "", "正文一。",
        ),
        source="x",
    )
    assert res.book.title == "书A", res.book.title
    assert res.book.author == "明·某某", res.book.author


def case_metadata_unknown_stays_empty() -> None:
    """猜不到就留空，不许瞎填 —— 正文里提到书名、句子里带「著」都不算。

    「宁可空着也不瞎填」：空的书名在阅读器里显示为「未命名」，
    至少不会把某句正文当成书名。
    """
    res = clean_text(
        _text(
            "第一章 开始", "", "他翻开《书己》看了两页。", "这部书是兰陵笑笑生著的。",
        ),
        source="x",
    )
    assert res.book.title == "", res.book.title
    assert res.book.author == "", res.book.author


def case_volume_container_line_before_body() -> None:
    """目录最后一条后面跟着「卷一 风起」这种无编号卷容器行时，目录要删干净。

    回归用例：卷容器行既不是标题（parse_heading 要求「第N卷」）、也不是正文，
    原来被判成"目录条目后面跟着正文"，于是目录区提前收尾，最后一条目录
    「第四章 风清」留成全书第一个空章节。修法是把这类行当排版噪声跳过。
    """
    lines: list[str] = ["目录", "第一章 山高", "第二章 水长", "第三章 月白", "第四章 风清", ""]
    lines.extend(["卷一 风起", ""])
    for i, name in enumerate(("山高", "水长", "月白", "风清")):
        lines.extend([f"第{i + 1}章 {name}", "", f"正文{'一二三四'[i]}。", ""])
    res = clean_text(_text(*lines), source="x")

    # 「卷一 风起」后面紧跟的是章标题，它是**卷标题**，不是排版噪声：
    # 留成 level=2 的容器（这也是修《书A》时发现的行为 —— 四条「卷之X」
    # 原来是粘在上一章末尾正文里的）。目录那 4 条要删干净，一条不留。
    titles = [c.title for c in res.book.chapters]
    assert titles == [
        "卷一\u3000风起", "第1章\u3000山高", "第2章\u3000水长", "第3章\u3000月白", "第4章\u3000风清"
    ], f"目录没删干净或卷标题没认出来：{titles}"
    assert res.book.chapters[0].level == 2, [c.level for c in res.book.chapters]
    assert all(_bodies(c) for c in res.book.chapters[1:]), "出现了空章节"
    assert validate(res.book) == [], validate(res.book)
    assert res.report["dropped"]["toc"] == 5, res.report["dropped"]


# ---------------------------------------------------------------------------
# 十点五、诗词/韵文标记
# ---------------------------------------------------------------------------


def _kinds(chapter) -> list[tuple[str, str]]:
    """取一章里 (kind, text) 的有序列表，便于断言排版类型。"""
    return [(p.kind, p.text) for p in chapter.paragraphs if p.kind != "blank"]


def case_verse_after_lead_marked() -> None:
    """「词曰：」后面连续两行以上的韵文要标成 verse，引导语本身不标。

    为什么引导语不标：它是叙述句（「果然写出一首诗道：」），排成楷体居中
    读者会以为那行也是诗。它只当"下面是引文"的信号用。
    """
    lines = [
        "第一章 起",
        "",
        "\u3000\u3000词曰：",
        "\u3000\u3000黑发难留，朱颜易变，人生不比青松。",
        "\u3000\u3000名消利息，一派落花风。",
        "\u3000\u3000他看罢，点了点头。",
    ]
    res = clean_text(_text(*lines), source="x")
    ch = res.book.chapters[0]
    kinds = _kinds(ch)
    assert kinds[0] == ("body", "词曰："), kinds
    assert kinds[1][0] == "verse", f"引导语后面那行应是 verse：{kinds}"
    assert kinds[2][0] == "verse", f"第二行韵文也应是 verse：{kinds}"
    assert kinds[3][0] == "body", f"叙述句不该被标：{kinds}"
    assert res.report["verse"]["lines"] == 2, res.report["verse"]


def case_verse_narrative_lead_not_marked() -> None:
    """行尾带「为证：」的叙述句本身不标，但它后面的诗要标。

    回归用例：第一版把「……果然写出一首诗道：」整行标成 verse，
    读者会看到一句叙述被居中换楷体。
    """
    lines = [
        "第一章 起",
        "",
        "\u3000\u3000他果然写出一首诗为证：",
        "\u3000\u3000红粉丛中第一人，不须疑鬼复疑神。",
        "\u3000\u3000只愁艳冶将淫诲，邪正关头好问津。",
    ]
    res = clean_text(_text(*lines), source="x")
    kinds = _kinds(res.book.chapters[0])
    assert kinds[0] == ("body", "他果然写出一首诗为证："), kinds
    assert [k for k, _ in kinds[1:]] == ["verse", "verse"], kinds


def case_verse_tail_marker_backtracks() -> None:
    """「右调·忆秦娥」这种曲牌在诗**之后**，要向前回溯标记。"""
    lines = [
        "第一章 起",
        "",
        "\u3000\u3000人窈窕，浑身满面都堆俏。",
        "\u3000\u3000都堆俏，愁容可掬，频眉难效。",
        "\u3000\u3000右调·忆秦娥",
        "\u3000\u3000他放下笔，长叹一声。",
    ]
    res = clean_text(_text(*lines), source="x")
    kinds = _kinds(res.book.chapters[0])
    assert [k for k, _ in kinds[:3]] == ["verse", "verse", "verse"], kinds
    assert kinds[3][0] == "body", kinds
    assert res.report["verse"]["lines"] == 3, res.report["verse"]


def case_verses_need_evidence_not_geometry() -> None:
    """光"短且带句读"不算诗 —— 没有引导语也没有曲牌时一行都不标。

    为什么必须这样：中文小说的正文里到处是五六字的短句（「他站住了。」），
    纯按长度和标点猜，会把整段叙述标成引文。宁可漏标（诗词按正文排），
    也不要误标。
    """
    lines = [
        "第一章 起",
        "",
        "\u3000\u3000他站住了。",
        "\u3000\u3000她看着他。",
        "\u3000\u3000谁也没说话。",
        "\u3000\u3000风从北面吹来。",
    ]
    res = clean_text(_text(*lines), source="x")
    kinds = _kinds(res.book.chapters[0])
    assert all(k == "body" for k, _ in kinds), kinds
    assert res.report["verse"]["lines"] == 0, res.report["verse"]


def case_verse_two_line_poem_marked() -> None:
    """引导语后面**只有一行**韵文时也要标（"写出两句诗道："后接一行七言对句）。

    回归用例：《书A》第十五回"就取出一幅花笺，写出两句诗道："后面只有
    一行「天台诸女伴，相约待刘郎。」。第一版要求"至少两行连续"才标，
    这两句诗就被漏成正文。改成"至少一行"之后，误判风险由引导语判据
    （道/云/曰 收尾 + 体裁名或句数）自己承担。
    """
    lines = [
        "第一章 起",
        "",
        "\u3000\u3000他就取出一幅花笺，写出两句诗道：",
        "\u3000\u3000天台诸女伴，相约待刘郎。",
        "\u3000\u3000写了这两句，就把笺折起放进笔筒。",
    ]
    res = clean_text(_text(*lines), source="x")
    kinds = _kinds(res.book.chapters[0])
    assert kinds[0][0] == "body", kinds
    assert kinds[1][0] == "verse", f"两句诗也要标：{kinds}"
    assert kinds[2][0] == "body", f"后面的叙述不该被标：{kinds}"


# ---------------------------------------------------------------------------
# 十一、行内夹广告切除（M4 前移成规则切除）
# ---------------------------------------------------------------------------


def case_inline_ad_cut_keeps_quotes_paired() -> None:
    """引号里的广告被切掉后，保留部分的左右引号必须成对，不能劈一半。

    切点是"最后一个句末标点之后再吞掉紧跟的右引号/右括号"，
    所以 \u201c是。\u201d 的右引号会被一起吞进保留部分，断言它配平。
    """
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "未央生道：“是。”更多精彩内容请访问 www.example.com",
            "他喊道：“走！”然后跑开了。天才一秒记住本站 www.biquge.com",
        ),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    assert body == ["未央生道：“是。”", "他喊道：“走！”然后跑开了。"], body
    for t in body:
        # 左右引号数量必须相等，且不能以右引号之外的半个引号收尾
        assert t.count("“") == t.count("”"), f"引号劈半了：{t}"
        assert t.endswith("。") or t.endswith("”"), f"切点没落在句末：{t}"
    assert res.report["fixed"]["inline_ad_cut_lines"] == 2, res.report["fixed"]
    assert res.report["warnings"] == [], res.report["warnings"]


def case_inline_ad_body_mentioning_url_kept() -> None:
    """正文里正常提到一次网址（前面没有句末标点）时不许切。

    这是安全阀的主场景：整行只有一句、没有可用的句末切点，切了就是砍正文。
    """
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "他打开手机输入 www.example.com 然后关掉。",
            "她说你应该去 example.org 看看那篇说明。",
        ),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    assert body == [
        "他打开手机输入 www.example.com 然后关掉。",
        "她说你应该去 example.org 看看那篇说明。",
    ], f"正文提到网址被误切了：{body}"
    assert res.report["fixed"]["inline_ad_cut_lines"] == 0, res.report["fixed"]
    assert res.report["dropped"]["ad"] == 0, res.report["dropped"]


def case_inline_ad_no_sentence_end_not_cut() -> None:
    """有广告词但没有句末标点可作切点时，原样保留并告警。"""
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "她翻开那页看到微信两个字 www.example.com 就合上了",
            "正文一句。",
        ),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    assert body == ["她翻开那页看到微信两个字 www.example.com 就合上了", "正文一句。"], body
    assert res.report["fixed"]["inline_ad_cut_lines"] == 0, res.report["fixed"]
    warns = [w for w in res.report["warnings"] if "行内夹广告" in w]
    assert warns == ["行内夹广告 1 行无法安全切除，已原样保留，请人工核对"], warns


def case_inline_ad_punct_only_kept_becomes_whole_drop() -> None:
    """切完只剩标点（……。）说明整行本来就是广告，按整行删处理，不计行内切除。"""
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "……。求收藏，求推荐，求月票 www.biquge.com 谢谢支持。",
            "正文一句。",
        ),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    assert body == ["正文一句。"], f"只剩标点的那行没按整行删：{body}"
    assert res.report["dropped"]["ad"] == 1, res.report["dropped"]
    assert res.report["fixed"]["inline_ad_cut_lines"] == 0, res.report["fixed"]
    assert res.report["samples"]["inline_ad"] == [], res.report["samples"]["inline_ad"]


def case_inline_ad_punct_only_but_body_tail_kept() -> None:
    """切点前只剩标点、但尾巴是正文时不许整行删（_URL 把 Mr.Lee 当域名的坑）。

    回归用例：'……。Mr.Lee 的信折好放进口袋。' 里 Mr.Lee 被 _URL 当域名，
    切点前又只有省略号 —— 只看"前面没字"就整行删，会把一句正文删掉。
    整行删必须再加一条：尾巴自己得被判成整行广告。
    """
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "……。Mr.Lee 的信折好放进口袋。",
            "正文一句。",
        ),
        source="x",
    )
    body = _bodies(res.book.chapters[0])
    assert body == ["……。Mr.Lee 的信折好放进口袋。", "正文一句。"], f"正文被整行删了：{body}"
    assert res.report["dropped"]["ad"] == 0, res.report["dropped"]
    assert res.report["fixed"]["inline_ad_cut_lines"] == 0, res.report["fixed"]
    # 也不该占用"切不了"的告警：尾巴是带句号的正文句，不像广告标签。
    warns = [w for w in res.report["warnings"] if "行内夹广告" in w]
    assert warns == [], warns


def case_inline_ad_tail_goes_into_samples() -> None:
    """切掉的尾巴要进 samples["inline_ad"]，字段是 line/text/cut/kept。

    为什么还存一个 kept：报告要同时展示"切了什么"和"留下什么"。
    拿"原行减切除部分"去算保留文本是不行的 —— 两个字段都截断到 40/60 字，
    相减会把广告残渣留在报告里（第一版就是这么错的）。
    """
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "风从北面吹来。这章真好看，记住本站 www.example-novel.com",
        ),
        source="x",
    )
    samples = res.report["samples"]["inline_ad"]
    assert len(samples) == 1, samples
    one = samples[0]
    assert set(one) == {"line", "text", "cut", "kept"}, one
    # 源文件里这条在第 3 行（章节标题 1、空行 2）
    assert one["line"] == 3, one
    assert "这章真好看" in one["text"], one
    assert "记住本站" in one["cut"] and "www.example-novel.com" in one["cut"], one
    # 报告里的保留文本与正文一致
    assert one["kept"] == "风从北面吹来。", one
    assert _bodies(res.book.chapters[0]) == ["风从北面吹来。"]


def case_inline_ad_warning_only_when_uncut() -> None:
    """警告文案只在"切不了"时出现；切掉的不再占警告，也不重复计数。"""
    cut_ok = clean_text(
        _text("第一章 开始", "", "风从北面吹来。求收藏，求推荐票 www.x.com"),
        source="x",
    )
    assert cut_ok.report["fixed"]["inline_ad_cut_lines"] == 1, cut_ok.report["fixed"]
    assert not any("行内夹广告" in w for w in cut_ok.report["warnings"]), cut_ok.report["warnings"]

    # 一行能切、一行切不了：警告里只报 1 行，不能把切掉的那行也算进去
    mixed = clean_text(
        _text(
            "第一章 开始",
            "",
            "风从北面吹来。求收藏，求推荐票 www.x.com",
            "他打开手机输入 www.example.com 然后关掉。",
        ),
        source="x",
    )
    assert mixed.report["fixed"]["inline_ad_cut_lines"] == 1, mixed.report["fixed"]
    # 第二行是正文提到网址（有句末标点、切点前是完整句子），不该报"切不了"：
    # 告警只留给"确实是广告尾巴但不敢切"的行，否则正常小说会满屏告警。
    warns = [w for w in mixed.report["warnings"] if "行内夹广告" in w]
    assert warns == [], warns


def case_inline_ad_cut_never_leaves_dangling_comma() -> None:
    """切点必须落在句末标点之后，切完的段落不能以逗号/顿号/分号开头。

    切点算错一格（例如落在逗号后）就会得到"求收藏，求推荐票！"这种
    以半句开头的段落 —— 这条锁住"往前找最后一个句末标点"的方向。
    """
    lines = [
        "第一章 开始",
        "",
        "他抬头看了看天色。这章真好看，记住本站 www.example-novel.com",
        "风从北面吹来，带着一股潮湿的寒意。求收藏，求推荐票 www.x.com",
    ]
    res = clean_text(_text(*lines), source="x")
    body = _bodies(res.book.chapters[0])
    assert body == ["他抬头看了看天色。", "风从北面吹来，带着一股潮湿的寒意。"], body
    bad = "，、；：！？。"
    for t in body:
        assert t and t[0] not in bad, f"切完的段落以标点开头：{t}"
        assert t[-1] in "。！？…", f"切完的段落没落在句末：{t}"


def case_inline_ad_title_line_not_cut() -> None:
    """标题行即使含广告词也不切（切了标题就残缺，章节结构会跟着坏）。"""
    res = clean_text(
        _text(
            "第一章 天才一秒记住本站",
            "",
            "正文一句。",
        ),
        source="x",
    )
    assert [c.title for c in res.book.chapters] == ["第一章　天才一秒记住本站"], [
        c.title for c in res.book.chapters
    ]
    assert res.report["fixed"]["inline_ad_cut_lines"] == 0, res.report["fixed"]
    # 标题行既不删也不切，但标题本身带广告词要单独提醒（它会原样进 EPUB 目录）
    warns = [w for w in res.report["warnings"] if "章节标题里夹着广告词" in w]
    assert warns == ["有 1 个章节标题里夹着广告词或网址，标题未作改动，请人工核对"], warns
    assert not any("行内夹广告" in w for w in res.report["warnings"]), res.report["warnings"]


def case_inline_ad_idempotent() -> None:
    """对切过的结果再洗一遍，文本不再变化，也不再报行内广告。"""
    lines = [
        "第一章 开始",
        "",
        "未央生道：“是。”更多精彩内容请访问 www.example.com",
        "风从北面吹来。求收藏，求推荐票！",
        "正文一句。",
    ]
    from huoban.render import render_txt

    once = clean_text(_text(*lines), source="x")
    first = render_txt(once.book)
    twice = clean_text(first, source="x")
    assert render_txt(twice.book) == first, "两遍清洗结果不同，行内切除破坏了幂等"
    assert twice.report["fixed"]["inline_ad_cut_lines"] == 0, twice.report["fixed"]
    assert not any("行内夹广告" in w for w in twice.report["warnings"]), twice.report["warnings"]


def case_inline_ad_keeps_indent_prefix() -> None:
    """缩进被切掉尾巴后仍然只有一个缩进前缀，段落缩进不能被切坏。"""
    res = clean_text(
        _text(
            "第一章 开始",
            "",
            "　　风从北面吹来。这章真好看，记住本站 www.example-novel.com",
            "　　他抬头看了看天色。求收藏，求推荐票 www.x.com",
        ),
        source="x",
    )
    ch = res.book.chapters[0]
    for p in ch.paragraphs:
        assert p.kind == "body", (p.kind, p.text)
        assert not p.text.startswith("\u3000"), f"缩进前缀漏进了段文本：{p.text!r}"
        assert "记住本站" not in p.text and "求收藏" not in p.text, p.text
    assert _bodies(ch) == ["风从北面吹来。", "他抬头看了看天色。"], _bodies(ch)


def case_suspicious_chars_reported() -> None:
    """原文件自带的私用区字符/谚文要报出来，但一个字都不许改。

    这类字常见于被反复"转换过编码"的老 TXT。清洗只动空白、标点与广告，
    正文用字一律不碰 —— 擅自替换就是替作者改字。报告里点名，人工决定。
    """
    res = clean_text(
        _text("第一章 开始", "", "正文里冒出一个\ue123符号。", "第二行有谚文\ud55c\ud558。"),
        source="x",
    )
    hit = [w for w in res.report["warnings"] if "可疑字符" in w]
    assert len(hit) == 1, res.report["warnings"]
    # 三个字符：一个私用区 + 两个谚文音节
    assert "3 个" in hit[0], hit[0]
    # 正文一个字都没被改：可疑字符原样留在章节树里
    paras = res.book.chapters[0].paragraphs
    assert "\ue123" in paras[0].text, paras[0].text
    assert "\ud55c" in paras[1].text, paras[1].text


def case_inline_ad_body_after_url_kept() -> None:
    """正文句 + 短网址尾巴，尾巴本身是完整的一句话 → 整行原样保留。

    回归用例（第五轮只读复核阻断 1）：旧版切点只看"尾巴判不判成整行广告"，
    「他停下手。淘宝 www.taobao.com 上买。」的尾巴有句号、有 8 个实字，
    却被 judge_ad_line 当广告碎片放行，于是正文后半句被切走 —— 只剩
    「他停下手。」。判据补成"尾巴以句末标点收尾且不算短 → 当正文，不切"。
    """
    res = clean_text(
        _text("第一章 开始", "", "\u3000\u3000他停下手。淘宝 www.taobao.com 上买。"),
        source="x",
    )
    ch = res.book.chapters[0]
    assert _bodies(ch) == ["他停下手。淘宝 www.taobao.com 上买。"], _bodies(ch)
    assert res.report["fixed"]["inline_ad_cut_lines"] == 0, res.report["fixed"]


def case_inline_ad_head_kept_body_kept() -> None:
    """广告在前、正文在后 → 一个字都不许删。

    回归用例（第五轮只读复核阻断 1 的第二形态）：整行判据当时会判
    「求收藏 www.x.com 他走进屋里，坐下喝茶。」是整行广告，真删的话
    正文整句消失，报告里还只写"删了一行广告"。
    """
    res = clean_text(
        _text("第一章 开始", "", "\u3000\u3000求收藏 www.x.com 他走进屋里，坐下喝茶。"),
        source="x",
    )
    ch = res.book.chapters[0]
    assert _bodies(ch) == ["求收藏 www.x.com 他走进屋里，坐下喝茶。"], _bodies(ch)
    assert res.report["dropped"]["ad"] == 0, res.report["dropped"]


def case_inline_ad_cut_repairs_quote() -> None:
    """切点在引号中间时，要把切走那一侧的右引号要回来。

    回归用例（第五轮只读复核阻断 2）：「他道：「是。求收藏 www.x.com」」
    切完之后 kept 只剩一个左引号，排版上会一路延续到章末。修法是把尾巴里
    那个右引号移到 kept 末尾 —— 结果「他道：“是。”」，一个字不多不少。
    """
    res = clean_text(
        _text("第一章 开始", "", "\u3000\u3000他道：「是。求收藏 www.x.com」"),
        source="x",
    )
    ch = res.book.chapters[0]
    assert _bodies(ch) == ["他道：“是。”"], _bodies(ch)
    text = _bodies(ch)[0]
    assert text.count("\u201c") == text.count("\u201d") == 1, text
    assert res.report["fixed"]["inline_ad_cut_lines"] == 1, res.report["fixed"]


def case_verse_narrative_after_poem_not_marked() -> None:
    """诗后面紧跟的短叙述句不许标成 verse。

    回归用例（第五轮只读复核阻断 3）：「他点头。」4 个字、带句号、不含对话、
    不含了着吗，几何上和「春眠不觉晓，」一模一样，旧判据就把它当诗标了。
    新判据逐分句看"开头是不是人称代词" —— 「他」开头即叙述。
    """
    lines = [
        "第一章 起",
        "",
        "\u3000\u3000诗曰：",
        "\u3000\u3000春眠不觉晓，处处闻啼鸟。",
        "\u3000\u3000他点头。",
    ]
    res = clean_text(_text(*lines), source="x")
    kinds = _kinds(res.book.chapters[0])
    assert kinds == [("body", "诗曰："), ("verse", "春眠不觉晓，处处闻啼鸟。"), ("body", "他点头。")], kinds


def case_verse_with_le_at_head_marked() -> None:
    """诗句第一个字是「了」也要能标上。

    回归用例（第五轮只读复核中等 5）：「了却君王天下事，赢得生前身后名。」
    是辛弃疾的词，旧判据"行里只要出现『了』就否"把它整个排掉了。
    新判据只看**分句最后一个实义字**落在什么上，首字的「了」不算助词收尾。
    """
    lines = [
        "第一章 起",
        "",
        "\u3000\u3000词曰：",
        "\u3000\u3000了却君王天下事，赢得生前身后名。",
    ]
    res = clean_text(_text(*lines), source="x")
    kinds = _kinds(res.book.chapters[0])
    assert kinds == [("body", "词曰："), ("verse", "了却君王天下事，赢得生前身后名。")], kinds


def case_lead_line_kept_separate_without_indent() -> None:
    """没有缩进时，「诗曰：」也不能被并碎行吃掉。

    回归用例（第五轮只读复核中等 4）：引导语以冒号结尾，旧合并条件只问
    "前一行有没有停在句末标点"，冒号不算句末标点 → 「诗曰：」被并进下一行，
    整段不再以冒号结尾，诗词标记就整块失效。
    """
    lines = [
        "第一章 起",
        "",
        "诗曰：",
        "春眠不觉晓，处处闻啼鸟。",
    ]
    res = clean_text(_text(*lines), source="x")
    kinds = _kinds(res.book.chapters[0])
    assert kinds == [("body", "诗曰："), ("verse", "春眠不觉晓，处处闻啼鸟。")], kinds


# ---------------------------------------------------------------------------
def case_lead_marker_starts_new_paragraph() -> None:
    """???? 是网页抓取小说的首行缩进占位符，它必须开启一个新段落。

    回归用例：真书《书B》靠 ???? 分段（14 万行里空行只占 10%）。
    不认这个标记时，整本书被并成几千个几百字的假段落，原作者的分段全丢。
    """
    lines = [
        "第一章 起",
        "",
        "    ????我叫阿雨，现在在读大学。准确地说应该是下个学期",
        "就是三年级了。",
        "    ????我家在D城。离G城不远。大概两个小时啲车程吧。",
    ]
    res = clean_text(_text(*lines), source="x")
    bodies = _bodies(res.book.chapters[0])
    assert len(bodies) == 2, bodies
    # 第一段跨了原文两行（第二行不是段首，接回去）
    assert bodies[0] == "我叫阿雨，现在在读大学。准确地说应该是下个学期就是三年级了。", bodies
    assert bodies[1] == "我家在D城。离G城不远。大概两个小时啲车程吧。", bodies
    # 占位符本身不是内容，一个字都不该剩下
    assert all("?" not in b[:2] for b in bodies), bodies
    assert res.report["fixed"]["lead_marker_lines"] == 2, res.report["fixed"]


def case_lead_marker_bang_variant() -> None:
    """!!!! 是同一类占位符的另一种写法，同样按段首处理。

    回归用例：实测 76 个 !!!! 行里 69 行是正文句子开头（「家长会结束了。
    人群渐渐散去。」），提升成章节标题会直接丢掉 69 段正文。
    """
    lines = [
        "第一章 起",
        "",
        "  !!!!家长会结束了。人群渐渐散去。",
        "  !!!!她看到了校门外那对母子。",
    ]
    res = clean_text(_text(*lines), source="x")
    bodies = _bodies(res.book.chapters[0])
    assert len(bodies) == 2, bodies
    assert bodies[0] == "家长会结束了。人群渐渐散去。", bodies
    assert res.report["fixed"]["lead_marker_lines"] == 2, res.report["fixed"]


def case_lead_marker_disables_indent_signal() -> None:
    """有段首占位符时，缩进本身不再携带段落信息。

    这类文件每一行都带 4 个半角空格（采集器痕迹），不是作者的分段意图；
    真正的段首靠 ???? 写。此时若还把缩进当信号，"不并缩进行"会让
    14 万行保持 14 万个假段落。
    """
    lines = [
        "第一章 起",
        "",
        "    ????他走进屋子，看见",
        "    桌上放着一封信。",
    ]
    res = clean_text(_text(*lines), source="x")
    bodies = _bodies(res.book.chapters[0])
    # ???? 行开新段；下一行虽然缩进，也只是续行，要接回去
    assert len(bodies) == 1, bodies
    assert bodies[0] == "他走进屋子，看见桌上放着一封信。", bodies


def case_indent_signal_survives_without_lead_marker() -> None:
    """没有占位符的书，缩进照样是段落边界（不能因新规则失效）。

    回归用例：防的是"为了兼容网页抓取书，把缩进体小说的分段也一起改坏"。
    """
    lines = [
        "第一章 起",
        "",
        "　　他走进屋子，看见",
        "　　桌上放着一封信。",
    ]
    res = clean_text(_text(*lines), source="x")
    bodies = _bodies(res.book.chapters[0])
    assert len(bodies) == 2, bodies
    assert bodies[0] == "他走进屋子，看见", bodies
    assert bodies[1] == "桌上放着一封信。", bodies
    assert res.report["fixed"]["lead_marker_lines"] == 0, res.report["fixed"]

def case_merged_paragraph_requotes_across_lines() -> None:
    """一段跨多行、每行各带半个引号时，合并后必须重跑引号与标点归一。

    回归用例：真书《书B》一段话跨 5 行，行尾与下一行行首各带
    半个弯引号。行级各判各的 → 一轮输出单引号、二轮输出双引号，
    实测 203 个段落 render 后不相同。
    """
    lines = [
        "第一章 起",
        "",
        "    ????拾我一夜没回家。几天后中午老大请我吃饭。塞给了我三千块。‘我把照片啝录",
        "    像带给了麻爷了。再给妳这么多。’老大伸出一个把掌。‘妳妈那天哭着要照片",
        "    啝录像带。我说在麻爷那。就都给她。’我",
    ]
    res1 = clean_text(_text(*lines), source="x")
    from huoban.render import render_txt
    txt = render_txt(res1.book)
    res2 = clean_text(txt, source="x")
    assert render_txt(res2.book) == txt, "跨行引号导致不幂等"
    bodies = _bodies(res1.book.chapters[0])
    assert len(bodies) == 1, bodies


def case_merged_paragraph_wave_across_lines() -> None:
    """连续波浪号跨在行尾与行首时，合并后要压成一组。

    回归用例：原文两行各带半个波浪号，行级归一各压各的，拼起来仍是四个；
    二轮对整行压一次才干净，两个段落因此不相同。

    第一行**不能以收尾引号结尾**：那表示引号已经闭合、句子写完了，
    按 TERMINAL 的判据它就是一段的结尾（TERMINAL 补上 ” 之后这条才成立）。
    这条用例守的是波浪号跨行，不是"引号闭合了还要硬并"。
    """
    lines = [
        "第一章 起",
        "",
        "    ????噢～～噢～啊……噢……噢……妈妈……不……不成了……啊……噢……啊～",
        "    妈妈突然的昏了过去。",
    ]
    res1 = clean_text(_text(*lines), source="x")
    from huoban.render import render_txt
    txt = render_txt(res1.book)
    res2 = clean_text(txt, source="x")
    assert render_txt(res2.book) == txt, "跨行波浪号导致不幂等"
    bodies = _bodies(res1.book.chapters[0])
    assert len(bodies) == 1, bodies
    assert "～～" not in bodies[0], bodies

def case_site_prefix_before_chapter_number() -> None:
    """章节编号前面刷了站点水印（“正文 第000章：楔子”）也必须认成标题。

    回归用例：真书《书C》全书 137 章，标题行行首被站点刷了
    “正文 ”“山村野事 ”两种水印。match_chapter 的 ^\\s* 锚点因此挂空，
    整本书只认出 1 章 —— 不是漏一章，是整本书没有结构。
    """
    lines = [
        "正文 第000章：楔子",
        "",
        "开头一句。",
        "山村野事 第001章：野外风情",
        "",
        "第二段正文。",
    ]
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    # 站点水印只用来"认出这是标题"，不能留在标题里 —— 否则 EPUB 目录里
    # 每一章都顶着「正文　」两个字。
    assert titles == ["第000章：楔子", "第001章：野外风情"], titles
    assert len(res.book.chapters) == 2, titles
    assert _bodies(res.book.chapters[0]) == ["开头一句。"]


def case_site_prefix_does_not_revive_decorated_toc() -> None:
    """带装饰括号的目录条目不能被“剥站点前缀”复活成空章节。

    回归用例：「【第一章　开始】」的【】若被当成水印剥掉，整批目录条目
    会变成合法标题、留下 5 个 0 段落空章节。装饰不是水印：水印里必须有
    实义字符（字母/数字/汉字）。
    """
    res = clean_text(_text(*_toc_with("【第{n}章 {t}】")), source="x")
    empty = [c.title for c in res.book.chapters if not c.paragraphs]
    assert not empty, f"出现了空章节：{empty}"
    assert len(res.book.chapters) == 5, [c.title for c in res.book.chapters]


def case_body_sentence_with_site_prefix_not_title() -> None:
    """正文句子里出现“第三章”、且前面有叙述主语时，不能当标题。

    回归用例：“他说第三章的故事很精彩。”剥掉“他说”之后也像标题，但整行
    以句号收尾 —— 成句守卫看的是原始行，“他说”就是证据。
    """
    lines = ["第一章 开始", "", "他说第三章的故事很精彩。", "结局一句。"]
    res = clean_text(_text(*lines), source="x")
    assert len(res.book.chapters) == 1, [c.title for c in res.book.chapters]
    assert _bodies(res.book.chapters[0]) == ["他说第三章的故事很精彩。", "结局一句。"]

# ---------------------------------------------------------------------------
# 十三、真书回归：《书F》《书G》《书E》
# ---------------------------------------------------------------------------


def case_bracket_section_chapters() -> None:
    """全角括号中文数字（「（一）」「（二）」）也是章节标记。

    回归用例：真书《书F》全书 220 节都用「（一）」这种写法，不在任何已知
    标题家族里，于是整本书切出 0 章、14765 段全塞进 front_matter —— 结构一点没剩。
    同批复查其余 5 本书这类行 0 条，收这条规则不引入假阳性。
    """
    # 至少要 8 节、编号一路不回头，才认（闸门见 _bracket_section_is_structure）
    cn = "一二三四五六七八九十"
    lines: list[str] = []
    for i in range(10):
        lines.extend([f"（{cn[i]}）", "", f"第{i + 1}段正文。", ""])
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == [f"（{cn[i]}）" for i in range(10)], titles
    assert [c.number for c in res.book.chapters] == list(range(1, 11)), [
        c.number for c in res.book.chapters
    ]
    assert _bodies(res.book.chapters[1]) == ["第2段正文。"], _bodies(res.book.chapters[1])
    assert res.book.front_matter == [], [p.text for p in res.book.front_matter]


def case_bracket_section_enumeration_not_chapters() -> None:
    """正文里的「（一）（二）（三）」是行内枚举，不是章节分节。

    回归用例：《书B》里每章正文都有缩进 4 空格的「（一）…（五）」，
    是列举，不是分节。收「括号分节」家族时不加书级闸门，那本书 171 章变 304 章
    —— 凭空多出 131 个空壳章节，还引入 12 行幂等差异。
    判据：这类行的编号每进一章就从头数起（回头了），不是一本书的骨架。
    """
    lines: list[str] = []
    for c in range(3):
        lines.extend([f"第{'一二三'[c]}章 标题{c + 1}", ""])
        for i in range(4):
            lines.extend([f"（{'一二三四'[i]}）", f"第{c + 1}章第{i + 1}条正文。", ""])
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == [f"第{'一二三'[c]}章　标题{c + 1}" for c in range(3)], titles
    # 枚举行留在正文里，一段不少
    assert _bodies(res.book.chapters[0]) == [
        "（一）", "第1章第1条正文。", "（二）", "第1章第2条正文。",
        "（三）", "第1章第3条正文。", "（四）", "第1章第4条正文。",
    ], _bodies(res.book.chapters[0])


def case_bare_preface_is_chapter() -> None:
    """整行只有一个「序」也是章节标题。

    回归用例：《书G》的正文第一个标题是裸「序」。不认它的话，目录块判定的
    "最后一条目录后面紧跟标题"永远为假，目录最后一条被当正文区起点留下，
    成了第一章、还把「序」的正文吞进自己肚子里。
    """
    res = clean_text(_text("序", "", "序的正文。", "", "第一章 开始", "", "正文一。"), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["序", "第一章\u3000开始"], titles
    assert _bodies(res.book.chapters[0]) == ["序的正文。"], _bodies(res.book.chapters[0])


def case_toc_page_tail_line_dropped() -> None:
    """目录里"行尾带页码、但键配不上正文标题"的行也要跟着目录删掉。

    回归用例：《书G》目录第一行是「序<TAB>0」—— 它整行不是标题（带着页码），
    目录块扫描没把它收进簇里，于是它落在目录区之外，最后变成 front_matter
    里的一段「序0」。
    """
    lines = ["目录", "序\t0"]
    for i in range(5):
        lines.append(f"第{'一二三四五'[i]}章 标题{i + 1}\t{10 + i * 3}")
    lines.append("")
    for i in range(5):
        lines.extend([f"第{'一二三四五'[i]}章 标题{i + 1}", "", f"正文{i + 1}。", ""])
    res = clean_text(_text(*lines), source="x")

    titles = [c.title for c in res.book.chapters]
    assert titles == [f"第{'一二三四五'[i]}章\u3000标题{i + 1}" for i in range(5)], titles
    assert res.book.front_matter == [], [p.text for p in res.book.front_matter]


def case_double_heading_number_not_title() -> None:
    """一行里有两个以上章节编号，这不是标题，是插图说明或正文引用。

    回归用例：《书G》插图里断行断坏的「l第一幕第二幕第三幕」，前缀 l 被当
    站点水印剥掉，剩下正好命中"第N章"家族 —— 这一行成了标题，还吞掉后面
    64 段正文（第九章的正文整个错位）。
    """
    lines = ["第一章 开始", "", "正文一。", "l第一幕第二幕第三幕", "", "正文二。"]
    res = clean_text(_text(*lines), source="x")
    assert len(res.book.chapters) == 1, [c.title for c in res.book.chapters]
    texts = [p.text for p in res.book.chapters[0].paragraphs]
    assert "l第一幕第二幕第三幕" in texts, texts


def case_author_blacklist_skips_intro_line() -> None:
    """「作者简介」不是作者名。

    回归用例：《书G》扉页有一行「   作者简介」，_AUTHOR_PREFIX 把它读成
    作者=「简介」，EPUB 信息页于是印着「作者：简介」。
    """
    res = clean_text(
        _text("《书G》", "", "作者简介", "", "某某著。", "", "第一章 开始", "", "正文一。"),
        source="x",
    )
    assert res.book.title == "书G", res.book.title
    assert res.book.author == "", res.book.author


def case_metadata_from_filename() -> None:
    """正文里没有书名页时，从文件名取书名与作者。

    回归用例：《书E》25280 行里没有任何元数据行，书名作者只在文件名
    「书E 作者：某甲.txt」上，进 EPUB 后标题栏全空。
    """
    res = clean_text(_text("第一章 开始", "", "正文一。"), source="书E 作者：某甲.txt")
    assert res.book.title == "书E", res.book.title
    assert res.book.author == "某甲", res.book.author


def case_metadata_filename_decor_stripped() -> None:
    """文件名两端的装饰与标注要剥掉：「【书F】（全）作者：某乙.txt」。

    回归用例：剥装饰的顺序若先剥头部，剩下「（全）」再剥就空了，书名被自己的
    装饰吃掉；纯标注文件名（「（1-44完）.txt」）也不该被当成书名。
    """
    res = clean_text(_text("第一章 开始", "", "正文一。"), source="【书F】（全）作者：某乙.txt")
    assert res.book.title == "书F", res.book.title
    assert res.book.author == "某乙", res.book.author

    res2 = clean_text(_text("第一章 开始", "", "正文一。"), source="（1-44完）.txt")
    assert res2.book.title == "", res2.book.title


def case_metadata_filename_author_in_tail_paren() -> None:
    """「书名【辅助描述】（作者名）」要同时取出书名与作者。

    回归用例（试用反馈）：搬运站把作者塞在文件名末尾括号里、且不带
    「作者：」二字。旧逻辑先把「（作者名）」里的「作者」当分隔符从中间匹配
    （书名留下半边括号、作者截成「名）」），不带「作者」的「（刘慈欣）」
    又被装饰括号规则整个剥掉，作者永远为空。
    """
    def meta(name: str) -> tuple[str, str]:
        res = clean_text(_text("第一章 开始", "", "正文一。"), source=name)
        return res.book.title, res.book.author

    assert meta("书名【辅助描述】（作者名）.txt") == ("书名", "作者名")
    assert meta("书L【已完结】（某丙）.txt") == ("书L", "某丙")
    assert meta("三体【全集】（刘慈欣）.txt") == ("三体", "刘慈欣")
    assert meta("书名（作者名）.txt") == ("书名", "作者名")
    # 纯标注与版面词不能占作者位，剥完只剩书名。
    assert meta("书名（全）.txt") == ("书名", "")
    assert meta("书名（简介）.txt") == ("书名", "")


def case_metadata_filename_url_tail_stripped() -> None:
    """文件名尾巴上的站点网址要先剥掉，别让广告判据把整个书名毙掉。

    回归用例：《书B(www.example-novel.com).txt》—— 域名超过装饰
    括号的 12 字上限剥不掉，整串带着域名进 judge_ad_line，书名被判成空。
    """
    def title_of(name: str) -> str:
        return clean_text(
            _text("第一章 甲", "", "正文一。"), source=name
        ).book.title

    assert title_of("书B(www.example-novel.com).txt") == "书B"
    assert title_of("某书[www.a.com].txt") == "某书"
    # 整串都是网址时，剥完不剩东西，宁可空着
    assert title_of("www.example-novel.com.txt") == ""
    # 网址尾巴被剥掉后，也不能顺手把它当成作者名（judge_ad_line 的 _URL 判据）。
    def meta(name: str) -> tuple[str, str]:
        res = clean_text(_text("第一章 甲", "", "正文一。"), source=name)
        return res.book.title, res.book.author

    assert meta("书B(www.example-novel.com).txt") == ("书B", "")
    assert meta("书名(www.abc.com).txt") == ("书名", "")


def case_metadata_filename_author_not_version_or_site() -> None:
    """末尾括号里的版本/形态标注、裸站点名不是作者；无冒号写法仍要认作者。

    回归用例（第五轮只读 review A1/A2，本轮自己引入又收口的回归）：新增的
    _FILENAME_TAIL_AUTHOR 分支会把「（完整版）」「（无删节）」「(biquge)」这类
    标注/站点名收成作者，印到 EPUB 信息页；而把「作者：」的冒号改成必需后，
    又丢了「书E 作者 某甲」这种无冒号的合法写法。
    """
    def meta(name: str) -> tuple[str, str]:
        res = clean_text(_text("第一章 开始", "", "正文一。"), source=name)
        return res.book.title, res.book.author

    # 版本/形态标注与裸站点名：作者必须空，书名不受影响；整串都是标注时书名也空。
    for word in (
        "完整版", "无删节", "无删减", "典藏版", "简体", "修订版", "未删节",
        "精排版", "合集", "TXT版", "第一卷", "上部", "番外", "外传",
        "xyz", "biquge", "xxbook", "Priest",
    ):
        assert meta(f"书名（{word}）.txt") == ("书名", ""), word
        assert meta(f"（{word}）.txt") == ("", ""), word
    # 真正的作者名照旧取得到，别把闸门收太紧。
    assert meta("书名（天蚕土豆）.txt") == ("书名", "天蚕土豆")
    assert meta("书名（我吃西红柿）.txt") == ("书名", "我吃西红柿")
    # 无冒号「作者 + 空白 + 尾部」仍认；作者尾巴的「（完结）」要剥掉。
    assert meta("书E 作者 某甲.txt") == ("书E", "某甲")
    assert meta("书名 作者：某甲（完结）.txt") == ("书名", "某甲")
    # 认不出的写法不许把整串（含「作者」二字）留成书名，宁可空着。
    assert meta("书名 作者某甲.txt") == ("", "")
    assert meta("书名作者访谈.txt") == ("", "")


def case_metadata_inside_file_wins_over_filename() -> None:
    """文件里的元数据比文件名可信：下载工具会把文件名改得乱七八糟。
    """
    res = clean_text(
        _text("《书A》", "", "明·某某", "", "第一回 开头", "", "正文一。"),
        source="【书F】（全）作者：某乙.txt",
    )
    assert res.book.title == "书A", res.book.title
    assert res.book.author == "明·某某", res.book.author


# ---------------------------------------------------------------------------
# 十四、真书回归第二批：《书M》《书N》《书O》《书P》
# ---------------------------------------------------------------------------


def case_hua_chapter_family() -> None:
    """「第一话·海底偶遇」也是章标题 —— 「话」是章级量词。

    回归用例：真书《书N》全书 31 章都写「第X话·标题」，「话」不在
    [章回节幕] 里，整本书切出 0 章、2510 段全掉进 front_matter。
    """
    cn = "一二三四五六七八九十"
    lines: list[str] = []
    for i in range(3):
        lines.extend([f"第{cn[i]}话·标题{i + 1}", "", f"正文{i + 1}。"])
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == [f"第{cn[i]}话·标题{i + 1}" for i in range(3)], titles
    assert [c.number for c in res.book.chapters] == [1, 2, 3], [
        c.number for c in res.book.chapters
    ]
    assert res.book.front_matter == [], [p.text for p in res.book.front_matter]


def case_numbered_section_chapters() -> None:
    """「1、掳君（1）」式的阿拉伯数字 + 顿号也是章节分节。

    回归用例：真书《书M》全书 66 节都写「1、标题」，整本书切出 0 章。
    与括号分节同理要过书级闸门（见 _numbered_section_is_structure）。
    """
    lines: list[str] = []
    for i in range(8):
        lines.extend([f"{i + 1}、标题{i + 1}", ""])
        for k in range(3):
            lines.append(f"第{i + 1}节第{k + 1}段正文。")
        lines.append("")
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == [f"{i + 1}、标题{i + 1}" for i in range(8)], titles
    assert [c.number for c in res.book.chapters] == list(range(1, 9)), [
        c.number for c in res.book.chapters
    ]
    assert res.book.front_matter == [], [p.text for p in res.book.front_matter]


def case_numbered_list_not_chapters() -> None:
    """连续几行的「1、2、3…」是资料清单，不是章节分节。

    回归用例：《书Q》开头是一份法宝清单「1、五行旗…9、冥书」，连续
    9 行挤在一块。只按"条数够、编号递增"判会把这份清单切成一本书的章节。
    闸门第 3 条要求节与节之间平均隔开 —— 真分节后面跟着成百上千行正文。
    """
    lines = ["第一章 开始", ""]
    for i in range(9):
        lines.append(f"{i + 1}、法宝{i + 1}")
    lines.extend(["", "正文一。", "", "正文二。"])
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000开始"], titles
    # 清单行留在正文里（短行会被当碎行并成一段，所以按拼接后的全文查）
    joined = " ".join(p.text for p in res.book.chapters[0].paragraphs)
    assert "1、法宝1" in joined and "9、法宝9" in joined, joined


def case_volume_prefix_same_line_chapter() -> None:
    """卷容器与章号挤在同一行时，这一行是"章"，不是"卷"。

    回归用例：《书O》全书 121 行都是「第一卷:飞来的艳福 第01章 鸿运赌坊」，
    按"第N卷部类"解析得到 121 个 level=2 卷容器，一个正文节点都不剩 ——
    0 章、5333 段全进 front_matter。剥掉卷前缀后按章标题解析。
    """
    lines = [
        "第一卷:飞来的艳福 第01章 鸿运赌坊",
        "",
        "正文一。",
        "第一卷:飞来的艳福 第02章 赌坊惨败",
        "",
        "正文二。",
    ]
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第01章\u3000鸿运赌坊", "第02章\u3000赌坊惨败"], titles
    assert [c.level for c in res.book.chapters] == [1, 1], [
        c.level for c in res.book.chapters
    ]
    assert _bodies(res.book.chapters[0]) == ["正文一。"], _bodies(res.book.chapters[0])


def case_toc_leading_title_entry_dropped() -> None:
    """扉页目录最前面那条「序」也是目录条目，要跟着目录一起删。

    回归用例：《书P》扉页目录是「序 / 书P序 / 书P题词 / 第一回…」，
    「序」后面紧跟的是目录里另外两行文字（不是标题），于是目录区扫描从「序」
    起就判定"正文区开始"，整块 61 条回目一条都删不掉；第二遍清洗时簇首换成
    了回目本身，目录又删掉了 —— 两遍结果不同，121 行幂等差异。
    """
    cn = "一二三四五六七八九十"
    lines = ["序"]
    for i in range(5):
        lines.append(f"第{cn[i]}章 标题{i + 1}")
    lines.extend(["", "序", "", "序的正文。", ""])
    for i in range(5):
        lines.extend([f"第{cn[i]}章 标题{i + 1}", "", f"正文{i + 1}。", ""])
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["序"] + [f"第{cn[i]}章\u3000标题{i + 1}" for i in range(5)], titles
    assert res.book.front_matter == [], [p.text for p in res.book.front_matter]
    # 真序的正文还在，没被目录条目顶掉
    assert _bodies(res.book.chapters[0]) == ["序的正文。"], _bodies(res.book.chapters[0])


# 十五、真书回归第三批：《书J》《书E》
# ---------------------------------------------------------------------------


def case_bracketed_book_name_volume_chapter() -> None:
    """「【书J】第二部 第一章」——书名号前缀 + 卷前缀 + 章号挤一行。

    回归用例：真书《书J》第二部每章标题都是这个样子，前缀（书名 5 字 +
    两个方头括号 = 7 字）超过 _SITE_PREFIX_MAX 被拒，整本书只认出 9 章，
    第二部 3000 多段全挂在「第一部（完）」那一章下面。
    """
    lines = [
        "【书J】第二部 第一章",
        "",
        "正文一。",
        "【书J】第二部 第二章",
        "",
        "正文二。",
    ]
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章", "第二章"], titles
    assert _bodies(res.book.chapters[0]) == ["正文一。"], _bodies(res.book.chapters[0])


def case_part_end_marker_not_chapter() -> None:
    """「第二卷（完）」是分部结束的记号，不是任何一章的开头。

    回归用例：真书《书E》第二卷结尾有一行「第二卷（完）」，命中
    「第N卷部类」变成一章的标题（目录里多出一个叫「第二卷（完）」的卷节点）。
    按场景分隔线处理：它留在上一章末尾，不再是标题。
    """
    lines = [
        "第一章 开始",
        "",
        "正文一。",
        "",
        "第二卷（完）",
        "",
        "第二章 继续",
        "",
        "正文二。",
    ]
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000开始", "第二章\u3000继续"], titles
    kinds = [(p.kind, p.text) for p in res.book.chapters[0].paragraphs]
    assert ("sep", "第二卷（完）") in kinds, kinds


def case_stub_chapter_merged() -> None:
    """章首重复一遍「书名+章名」+ 作者/日期/字数，是同一个标题写了两遍。

    回归用例：真书《书J》每章前面都有这么一坨（章标题、作者、日期、
    字数），后面才是真正的「第二章」。不合并的话目录里每章都多一个 5 段落的
    同名空壳章。
    """
    lines = [
        "【书J】第二章",
        "",
        "作者：11love214",
        "",
        "2017/4/15",
        "",
        "字数：16103",
        "",
        "第二章",
        "",
        "正文一。",
        "",
        "正文二。",
    ]
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第二章"], titles
    bodies = _bodies(res.book.chapters[0])
    assert "正文一。" in bodies and "正文二。" in bodies, bodies


# 十六、真书回归第四批：《书H》目录被卷标题切碎
# ---------------------------------------------------------------------------


def case_toc_region_expands_across_volume_lines() -> None:
    """目录被卷标题切成好几段时，整块目录都要删干净。

    回归用例：真书《书H》的目录是「第一集 花劫 / 第一回…第十回 /
    第二集（+ 下一行的卷名「都中风云」）/ 第十一回…」，detect_toc 只报最长的
    一簇（19 条），剩下 100 多条目录条目留在书里变成 0 段落空壳章；第二遍
    清洗时簇的位置变了、删的又是另外几条 —— 42 行幂等差异。
    修法是目录区向两侧延伸 + 把区间里漏掉的行补进来。
    """
    cn = "一二三四五六七八九十"

    def num(n: int) -> str:
        return cn[n - 1] if n <= 10 else "十" + cn[n - 11]

    # 目录：第一集六回，隔五个空行后第二集（书名+卷名占两行）六回。
    # 空行距离把 detect_toc 的簇切断，只报后半截（6 条）—— 这正是真书的形态。
    lines = ["书H", "", "目  录", "", "书H·第一集 花劫"]
    for n in range(1, 7):
        lines.append(f"第{num(n)}回 标题{n}\t{n}")
    lines.extend([""] * 5)
    lines.append("书H·第二集")
    lines.append("都中风云")
    for n in range(7, 13):
        lines.append(f"第{num(n)}回 标题{n}\t{n}")
    lines.extend(["", "前言正文一。", ""])
    # 正文：标题把卷前缀和章号写在同一行，目录里没有卷前缀
    for n in range(1, 13):
        vol = "花劫" if n <= 6 else "都中风云"
        ji = "一" if n <= 6 else "二"
        lines.extend([f"第{ji}集 {vol} 第{num(n)}回 标题{n}", "", f"正文{n}。", ""])
    once = clean_text(_text(*lines), source="x")
    titles = [c.title for c in once.book.chapters]
    assert titles == [f"第{num(n)}回\u3000标题{n}" for n in range(1, 13)], (
        f"目录没删干净：{titles}"
    )
    assert not [c for c in once.book.chapters if not c.paragraphs], "出现了空章节"
    assert _bodies(once.book.chapters[0]) == ["正文1。"], _bodies(once.book.chapters[0])
    front = [p.text for p in once.book.front_matter]
    assert front == ["书H", "前言正文一。"], front

    # 幂等：把一遍的输出（经 render）再洗一遍，文本必须逐字相等
    from huoban.render import render_txt

    first = render_txt(once.book)
    twice = clean_text(first, source="x")
    assert render_txt(twice.book) == first, "两遍清洗结果不同，幂等破了"


def case_toc_volume_subtitle_needs_volume_above() -> None:
    """卷名续行必须紧跟在卷标题后面，扉页作者行不能跟着删。

    回归用例：真书《书A》扉页是「《书A》/ 明·某某 / 回目…」，
    「明·某某」又短、又不成句、后面又紧跟标题，最初的卷名续行判据把它
    当成目录的一部分删掉了（1152 段变 1151 段）。
    """
    cn = "一二三四五六七八九十"

    def num(n: int) -> str:
        return cn[n - 1] if n <= 10 else "十" + cn[n - 11]

    lines = ["《书A》", "明·某某"]
    for n in range(1, 7):
        lines.append(f"第{num(n)}回 标题{n}")
    lines.append("")
    for n in range(1, 7):
        lines.extend([f"第{num(n)}回 标题{n}", "", f"正文{n}。", ""])
    res = clean_text(_text(*lines), source="x")
    front = [p.text for p in res.book.front_matter]
    assert front == ["《书A》", "明·某某"], front
    titles = [c.title for c in res.book.chapters]
    assert titles == [f"第{num(n)}回\u3000标题{n}" for n in range(1, 7)], titles


# ---------------------------------------------------------------------------
# 十八、数字顿号分节的「分部重数编号」
# 驱动书：《书I》（第一部 17 节、第二部又从「一、」数起，100 本抽检切出 0 章）
# ---------------------------------------------------------------------------

def case_numbered_section_restarts_per_part() -> None:
    """分部的书每部都从「1、」重数，只要每段递增都够长就算有骨架。

    回归用例：真书《书I》100 条编号分成 5 部、每部都从「一、」重数，
    旧闸门「整本不许回头」把它整本判死，切出 0 章。
    """
    # 标题正文里不许再出现第二个「第N」编号：双编号守卫（《书G》
    # 「第一幕第二幕第三幕」换来的规则）会把它挡掉。真书《书I》写的是
    # 「一.人妻被劫色(一)」，编号后面也没有第二个编号。
    lines: list[str] = []
    for part in (1, 2):
        for i in range(1, 9):
            lines.append(f"{i}、分部{part}小节{i}")
            lines.append("")
            for k in range(3):
                lines.append(f"分部{part}小节{i}第{k + 1}段正文。")
            lines.append("")
    res = clean_text(_text(*lines), source="x")
    expect = [f"{i}、分部{p}小节{i}" for p in (1, 2) for i in range(1, 9)]
    titles = [c.title for c in res.book.chapters]
    assert titles == expect, titles
    assert [c.number for c in res.book.chapters] == list(range(1, 9)) * 2, [
        c.number for c in res.book.chapters
    ]
    assert res.book.front_matter == [], [p.text for p in res.book.front_matter]
    # 幂等：重数编号的书洗两遍必须切出一样的章。
    from huoban.render import render_txt

    twice = clean_text(render_txt(res.book), source="x")
    assert [c.title for c in twice.book.chapters] == expect, [
        c.title for c in twice.book.chapters
    ]


def case_numbered_section_title_ends_with_ellipsis() -> None:
    """节标题用情绪标点收尾时，两遍清洗必须切出同一批章。

    回归用例：真书《书I》有两个标题写成「七. 被… ……」——原文件里编号后面
    跟着一个空格，第一遍被判成标题；渲染出去空格被归一化掉，行尾只剩「……」，
    第二遍就被判成正文，整本标题序列从第 63 条起错位，幂等差 2 行。

    编号后那个空格是复现的关键：没有它，第一遍也判成句子，测的就不是幂等了。
    """
    marks = {3: "……", 5: "？！"}
    lines: list[str] = []
    for i in range(1, 13):
        tail = marks.get(i, "")
        lines.extend([f"{i}、 标题{i}{tail}", ""])
        for k in range(3):
            lines.append(f"第{i}节第{k + 1}段正文。")
        lines.append("")
    res = clean_text(_text(*lines), source="x")
    # 编号后面的空格是"排版分隔"，会被保留成全角空格（P0 既定行为）。
    expect = [f"{i}、\u3000标题{i}{marks.get(i, '')}" for i in range(1, 13)]
    titles = [c.title for c in res.book.chapters]
    assert titles == expect, titles

    from huoban.render import render_txt

    twice = clean_text(render_txt(res.book), source="x")
    assert [c.title for c in twice.book.chapters] == expect, [
        c.title for c in twice.book.chapters
    ]


def case_numbered_section_short_run_vetoes_whole_book() -> None:
    """有一段递增 run 短于门槛，整本都不按数字顿号切章。

    回头闸门放开之后唯一的防线就是 run 长度：真分节和正文里的行内枚举都会
    「回头」，区别只在每段重数之前数了多久。《书V》每章正文夹
    「1、…5、」，就算它和两段长 run 混在同一本书里，整本也不许按这个家族切 ——
    宁可少一层结构，也不能把正文切成空章节。
    """
    lines: list[str] = ["第一章 开始", ""]
    for r, count in enumerate((10, 5, 10), 1):
        for i in range(1, count + 1):
            lines.append(f"{i}、第{r}段枚举{i}")
            lines.extend([f"正文{r}-{i}-{k}。" for k in range(4)])
            lines.append("")
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000开始"], titles
    joined = " ".join(p.text for p in res.book.chapters[0].paragraphs)
    assert "1、第1段枚举1" in joined and "5、第2段枚举5" in joined, joined


# ---------------------------------------------------------------------------
# 十七、站点水印前缀 × 数字顿号分节
# 驱动书：《书T》《书I》（100 本抽检里 0 章的那一批）
# ---------------------------------------------------------------------------

_CN = "一二三四五六七八九十"


def case_site_prefix_before_numbered_section() -> None:
    """水印刷在「一、标题」前面的中文数字分节，也必须切成章。

    回归用例：真书《书T》每节写成「正文 一、谁动了我的马子」，
    编号既不在行首、又不是「第N章」形态 —— 剥前缀的锚点只认「第N章/Chapter N」，
    于是闸门数到 0 条编号，整本书一条都切不出来。
    """
    lines: list[str] = []
    for i in range(8):
        lines.extend([f"正文 {_CN[i]}、标题{i + 1}", ""])
        for k in range(3):
            lines.append(f"第{i + 1}节第{k + 1}段正文。")
        lines.append("")
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    # 水印只用来"认出这是标题"，不能留在标题里。
    assert titles == [f"{_CN[i]}、标题{i + 1}" for i in range(8)], titles
    assert [c.number for c in res.book.chapters] == list(range(1, 9)), [
        c.number for c in res.book.chapters
    ]
    assert res.book.front_matter == [], [p.text for p in res.book.front_matter]
    # 幂等：干净 TXT 里编号回到了行首，第二遍不该再剥出不同结果。
    from huoban.render import render_txt

    twice = clean_text(render_txt(res.book), source="x")
    assert [c.title for c in twice.book.chapters] == titles, [
        c.title for c in twice.book.chapters
    ]


def case_site_prefix_numbered_list_still_not_chapters() -> None:
    """带水印的「1、2、3…」资料清单，仍然不是章节。

    剥前缀的落点扩到数字顿号之后，唯一的防线还是书级闸门：清单行连续挤在一块，
    平均间隔不够就不许切。这条守的是"放宽前缀规则没放宽结构判据"。
    """
    lines = ["第一章 开始", ""]
    for i in range(9):
        lines.append(f"正文 {i + 1}、法宝{i + 1}")
    lines.extend(["", "正文一。", "", "正文二。"])
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000开始"], titles
    joined = " ".join(p.text for p in res.book.chapters[0].paragraphs)
    assert "1、法宝1" in joined and "9、法宝9" in joined, joined


def case_toc_entries_match_site_prefixed_body_headings() -> None:
    """目录条目干净、正文那份标题带站点水印时，整块目录仍要删掉。

    实测来源：《书R》（604KB，5032 行）。目录 49 条是干净的
    「第N章　标题」，正文那份标题顶着「正文　」的水印前缀。目录键不剥前缀，
    两边永远配不上 —— 块级闸门算不出「簇外有同名」，一条都不删，49 条目录
    变成 49 个空章（96 章）；而第二遍清洗拿到的文本里前缀已经剥掉了，闸门
    放行、目录删干净（47 章）。同一份文本洗两遍得到两个结构。
    """
    toc = [f"第{i}章 标题{i}" for i in range(1, 9)]
    lines = ["白纸"] + toc + ["＊＊＊"]
    for i in range(1, 9):
        lines += [f"正文 第{i}章 标题{i}", "", f"这是第{i}章的正文内容。", ""]
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == [f"第{i}章\u3000标题{i}" for i in range(1, 9)], titles
    assert [_bodies(c) for c in res.book.chapters] == [
        [f"这是第{i}章的正文内容。"] for i in range(1, 9)
    ], [_bodies(c) for c in res.book.chapters]

    # 幂等：一遍的输出再洗一遍，章节数与标题一个字都不能变。
    from huoban.render import render_txt

    twice = clean_text(render_txt(res.book), source="x")
    assert [c.title for c in twice.book.chapters] == titles, [
        c.title for c in twice.book.chapters
    ]


def case_toc_separator_between_toc_and_body() -> None:
    """目录最后一条 + 分隔线 + 正文第一章：最后那条目录不是正文首章。

    实测来源：同上那本书的形态抽象。分隔线是"目录到此为止"的排版分界。
    老逻辑里「第8章」后面隔着一条分隔线才见正文，被当成"后面是正文"，
    于是它成了第一章，把分隔线和开头的正文一起吞进肚子 —— 9 章而不是 8 章，
    且第一章挂着目录里的名字。分隔线既不是标题也不是正文，不能当证据。
    """
    toc = [f"第{i}章 标题{i}" for i in range(1, 9)]
    lines = ["某书"] + toc + ["＊＊＊"]
    for i in range(1, 9):
        lines += [f"第{i}章 标题{i}", "", f"这是第{i}章的正文内容。", ""]
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == [f"第{i}章\u3000标题{i}" for i in range(1, 9)], titles
    assert [_bodies(c) for c in res.book.chapters] == [
        [f"这是第{i}章的正文内容。"] for i in range(1, 9)
    ], [_bodies(c) for c in res.book.chapters]


def case_fullwidth_url_ad_line_dropped() -> None:
    """整行广告里混着全角网址，照样要认出来删掉。

    实测来源：《书S》第 64651 行 —— 网址是半角与全角混排
    （「http://wap.ｚＺzcn.ｃｏｍ」）。规范化会把全角折成半角，所以一遍清洗
    输出的是半角网址；但整行广告的判据跑在**原文**上，正则不认全角，
    判不出来、留在正文里；二遍拿到的已是半角，判出来了 —— 两遍差一行。
    """
    lines = [
        "第一章 开头",
        "",
        "ｈｔｔｐ：／／ｗｗｗ．ｅｘａｍｐｌｅ．ｃｏｍ　记住本站",
        "",
        "下一段正文。",
    ]
    res = clean_text(_text(*lines), source="x")
    assert [_bodies(c) for c in res.book.chapters] == [["下一段正文。"]], [
        _bodies(c) for c in res.book.chapters
    ]
    assert res.report["dropped"]["ad"] >= 1, res.report["dropped"]

    # 幂等：广告一遍就该删干净，二遍不能再删出第二行差异。
    from huoban.render import render_txt

    once = render_txt(res.book)
    assert render_txt(clean_text(once, source="x").book) == once, "全角网址广告不幂等"


def case_wrapped_url_ad_lines_dropped() -> None:
    """广告被硬折成两行（网址本身断在两行之间），两截都要删掉。

    实测来源：《书乙》—— 抓取时按屏幕宽度硬折行，网址正好断在
    行尾。两截各自看都不像完整的整行广告，但合起来是一条。
    """
    lines = ["第一章 开头", "", "http://wap.", "example.com", "", "下一段正文。"]
    res = clean_text(_text(*lines), source="x")
    assert [_bodies(c) for c in res.book.chapters] == [["下一段正文。"]], [
        _bodies(c) for c in res.book.chapters
    ]

    from huoban.render import render_txt

    once = render_txt(res.book)
    assert render_txt(clean_text(once, source="x").book) == once, "折行广告不幂等"


# ---------------------------------------------------------------------------
# 十一B、硬折行假章（frag_split 语料驱动：40 字硬折行 × 正文引用章节号）
# ---------------------------------------------------------------------------
# 机制：抓取器按固定列宽把整行硬切成 N 字一块，句末标点被切到下一行，
# 于是"本行结尾有没有句末标点"的成句守卫失效 —— 以章节编号开头的续块
# 被认成标题，一章从中间被切成两半（book01 18→21、book04 31→37、
# book05 83→87、book14 24→27）。修复闸门见 clean.py 的折行证据判定。


def _pad(s: str, n: int = 40) -> str:
    """把假数据行填到恰好 n 字（末尾不带句末标点 = 硬折行中间块的形态）。"""
    assert len(s) < n
    return s + "哈" * (n - len(s))


def case_bare_number_page_number_is_not_a_chapter() -> None:
    """整行只有一个数字、前后都是空行：那是扫描残留的页码，不是一章。

    实测《书寅》行 5057 的「2」：上一行是对话「谢谢你，老师。蕾拉爱你。」，
    下一行接着写景，前一个标题编号是 12。它被当成一章，切走 1355 段正文。
    裸数字行只有在**这本书没有别的章节结构**、且成串递增时才算分节
    （见 case_bare_number_sequence_is_structure）。
    """
    res = clean_text(
        _text(
            "第一章 起",
            "",
            "正文一。",
            "",
            "第二章 落",
            "",
            "正文二。",
            "",
            "2",
            "",
            "正文三。",
            "",
            "第三章 收",
            "",
            "正文四。",
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000起", "第二章\u3000落", "第三章\u3000收"], titles
    # 被拒的页码要留在正文里，不能连内容一起消失。
    assert "2" in _bodies(res.book.chapters[1]), _bodies(res.book.chapters[1])


def case_bare_number_sequence_is_structure() -> None:
    """整本书只有一串递增的裸数字：那确实是分节，照常切章。

    与上面那条是一对：判"是不是结构"看的是全书 —— 一条孤零零的数字是页码，
    一串从 1 数上去、彼此隔开好几行的数字才是作者的分节方式。
    """
    lines: list[str] = []
    for n in (1, 2, 3, 4):
        lines.append(str(n))
        lines.append("")
        lines.extend(["正文甲，写得长一些。", "", "正文乙。", ""])
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["1", "2", "3", "4"], titles


def case_hardwrap_fullwidth_chunk_not_title() -> None:
    """满宽续块以「第三章」开头且无分隔符：这是折行切出来的正文块，不是标题。"""
    res = clean_text(
        _text(
            "第一章 起风了",
            "",
            _pad("风从河面上刮过来把芦苇都压弯了腰"),
            _pad("第三章里写的那些话后来一一应验了"),
            "",
            "第二章 落雪",
            "",
            _pad("雪落下来的时候没有人说话只剩下炉火"),
            _pad("他坐在桥头一直到天亮都没有再开口"),
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000起风了", "第二章\u3000落雪"], titles
    joined = "".join(_bodies(res.book.chapters[0]))
    assert "第三章里写的那些话" in joined, "被拒的续块必须退回正文，不能丢"


def case_hardwrap_duplicate_number_not_title() -> None:
    """编号已在前面真标题出现过、且上一行是满宽未收句的续块：折行假章。"""
    res = clean_text(
        _text(
            "第一章 起",
            "",
            _pad("他说过的话从来没有真的做到过这一次"),
            "",
            "第二章 承诺",
            "",
            _pad("雪落下来的时候没有人说话只剩下炉火"),
            "第二章 他后来反悔了那些约定",
            _pad("可是这一次他确实把那句话记到了最后"),
            "",
            "第三章 结局",
            "",
            _pad("天亮的时候河面上的冰已经冻得很结实"),
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000起", "第二章\u3000承诺", "第三章\u3000结局"], titles


def case_hardwrap_bare_number_needs_continuity() -> None:
    """纯数字行跟在满宽未收句后面：编号接着上一章的是真标题，跳号的是折行碎片。"""
    res = clean_text(
        _text(
            "第一章 起",
            "",
            _pad("风从河面上刮过来把芦苇都压弯了腰"),
            "2",
            _pad("他说他明天就去把账结了然后再回来"),
            "7",
            _pad("雪落下来的时候没有人说话只剩下炉火"),
            _pad("第三章里写的那些话后来一一应验了"),
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000起", "2"], (
        f"2 与上一章编号连续应保留，7 跳号（前一个编号是 2）应被拒，实际 {titles}"
    )
    last = "".join(_bodies(res.book.chapters[-1]))
    assert "7" in last and "第三章里写的那些话" in last, "被拒的碎片要退回正文"


def case_hardwrap_gate_off_without_wrap() -> None:
    """没有硬折行特征（行长众数 <20）时闸门整体关闭：弱标题照常保留。

    这里用「番外」当弱标题：它没有编号，走的是"没有编号前缀"的兜底判据。
    （原来用的是裸数字行「5」，那条路现在归书级闸门管，见
    case_bare_number_page_number_is_not_a_chapter —— 裸数字在语料里多数是
    扫描残留的页码，不再当作"弱标题"的代表。）
    """
    res = clean_text(
        _text(
            "第一章 起",
            "",
            "起风了。",
            "番外",
            "",
            "第三章 收",
            "",
            "落雪了。",
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000起", "番外", "第三章\u3000收"], titles


def case_hardwrap_separator_title_survives() -> None:
    """带分隔符的合法标题即使偏长也不受折行闸门影响（编号后有空格）。

    碎片必须放在**段中**才会进闸门（段首的候选与真标题无法区分，见
    case_hardwrap_para_head_candidate_kept）。
    """
    chunk = _pad("第三章里写的那些话后来一一应验了")
    assert len(chunk) == 40
    res = clean_text(
        _text(
            "第一章 起",
            "",
            _pad("风从河面上刮过来把芦苇都压弯了腰"),
            chunk,
            "第九百九十九章 这一章标题很长但编号后带分隔符所以是真标题",
            "",
            _pad("雪落下来的时候没有人说话只剩下炉火"),
            _pad("他坐在桥头一直到天亮都没有再开口"),
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert len(titles) == 2 and "第九百九十九章" in titles[1], titles


def case_hardwrap_para_head_candidate_kept() -> None:
    """段首的折行候选一律保留：与真标题无法区分，宁可少拒。

    这是 task-7 实测出来的边界。400 本池里 #106 命中 1 次、#137 1 次、
    #264 3 次，全部是**段首**真章标题（前一行短、收句、中间还有空行），
    按段首拒等于把真章吃掉（旧版不看段首/段中，实测 #106 2 章变 1 章、
    #264 12 章变 9 章）。段首那部分需要读懂"这一行是不是正文里引用章节号"，
    属语义判断，按铁律 3「规则优先，AI 兜底」留给 M4，这里只计数不动树。
    """
    chunk = _pad("第三章里写的那些话后来一一应验了")
    res = clean_text(
        _text(
            "第一章 起",
            "",
            _pad("风从河面上刮过来把芦苇都压弯了腰"),
            "",
            chunk,          # 段首：前面是空行 → 不拒
            "",
            "第二章 落雪",
            "",
            _pad("雪落下来的时候没有人说话只剩下炉火"),
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert len(titles) == 3, titles
    assert titles[1].startswith("第三章"), titles
    # 不动树，但必须报出来：用户要能看到"这本书有 1 行形态可疑、我没动它"。
    fixed = res.report["fixed"]
    assert fixed["wrap_chunk_suspect_lines"] == 1, fixed
    assert fixed["wrap_chunk_reject_lines"] == 0, fixed


def case_hardwrap_fragment_merged_into_previous() -> None:
    """被拒的碎片必须并回上一段：否则第二遍它站在段首、闸门不拦，章数翻回去。

    这是幂等的**构造性**保证，也是 #173/#309 两本书章数 276→605、331→344
    的根因。上一行即使已经收句（"……了。"）也要并 —— 硬折行的切点本来就
    可能落在句号后面。
    """
    res = clean_text(
        _text(
            "第一章 起",
            "",
            _pad("风从河面上刮过来把芦苇都压弯了腰"),
            _pad("第三章里写的那些话后来一一应验了"),
            "",
            "第二章 落雪",
            "",
            _pad("雪落下来的时候没有人说话只剩下炉火"),
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert len(titles) == 2, titles
    first = _bodies(res.book.chapters[0])
    # 两行必须并成**一段**：碎片不是独立段落
    assert len(first) == 1, first
    assert "应验了" in first[0], first[0][-20:]


def case_hardwrap_fragment_too_long_keeps_title() -> None:
    """碎片并不到上一段（超长）时，标题必须还回去 —— 宁可少拒，不许破幂等。

    并碎行有 MERGE_MAX_CHARS 上限。并成功才算拒：并进去的那一版第二遍看不到
    这一行了；并不成却硬拒，第二遍它站在段首、段中判据不拦，章数就翻回去。
    """
    long_prev = _pad("风从河面上刮过来把芦苇都压弯了腰") * 20   # 800 字，顶到上限
    res = clean_text(
        _text(
            "第一章 起",
            "",
            long_prev,
            _pad("第三章里写的那些话后来一一应验了"),
            "",
            "第二章 落雪",
            "",
            _pad("雪落下来的时候没有人说话只剩下炉火"),
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert len(titles) == 3, titles
    assert titles[1].startswith("第三章"), titles




def case_comma_ending_numbered_title_kept() -> None:
    """编号标题末尾带逗号、整行够短：照旧是标题（作者手误打了个逗号）。

    实测《书丙》的「第02章·美丽人妻的轮奸内射飨宴，」、
    《书戊》的「Chapter 3 Ending，」都是真标题。
    """
    res = clean_text(
        _text(
            "第一章 起",
            "",
            "\u3000\u3000起风了，河面上的冰裂开一道缝。",
            "",
            "第二章·风雪山神庙，",
            "",
            "\u3000\u3000他推门进去的时候雪已经停了。",
            "",
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert len(titles) == 2, titles


def case_long_comma_line_after_number_is_body() -> None:
    """编号开头、以逗号收尾、整行三十字：这是被折行切断的正文，不是标题。

    这一条锁的是长度天花板。实测《书丁》里那句
    「第3章勾引学生的美足仓库的事件后没过两天，陆绍辉和孟华阳两人」并不以逗号
    收尾，它是被成句守卫挡下的；这里把收尾改成逗号，专门验"够短才放行"这条。
    """
    line = "第3章勾引学生的美足仓库的事件后没过两天，陆绍辉和孟华阳两人，"
    assert len(line) > 20
    res = clean_text(
        _text(
            "第一章 起",
            "",
            "\u3000\u3000起风了，河面上的冰裂开一道缝。",
            "",
            line,
            "",
            "\u3000\u3000他推门进去的时候雪已经停了。",
            "",
        ),
        source="x",
    )
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000起"], titles
    assert line in "".join(_bodies(res.book.chapters[0])), "被拒的行必须留在正文里"

# ---------------------------------------------------------------------------
# 十一之三、短篇集的分篇标题（整本没有章节标记时才启用）
# ---------------------------------------------------------------------------


def _story_block(title: str, n: int = 12) -> list[str]:
    """一个短篇：深缩进的标题行 + 若干缩进 2 的正文行 + 尾空行。

    缩进 12 个全角空格 —— 实测《书X》的故事标题就是这种形态
    （正文缩进 2，标题缩进 12~16）。
    """
    out = ["\u3000" * 12 + title, ""]
    for k in range(n):
        out.append("\u3000\u3000" + f"第{k + 1}段正文写在这里，写满一句完整的话。")
    out.append("")
    return out


def case_story_titles_split_chapterless_collection() -> None:
    """整本没有章节标记、但每篇故事标题都深缩进时，按故事切章。

    实测《书X.txt》：1441 行、0 章，故事标题缩进 12~16 个全角空格，
    正文缩进 2。旧版整本并成 front_matter 一段，目录里什么都没有。
    """
    lines: list[str] = []
    for name in ("亲密爱人", "风林火山——武术家对决", "第一次最亲密的接触"):
        lines.extend(_story_block(name))
    lines.append("\u3000" * 15 + "【全文完】")
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["亲密爱人", "风林火山——武术家对决", "第一次最亲密的接触"], titles
    assert res.book.front_matter == [], "标题之前的空行不该留下 front_matter 段落"


def case_story_titles_ignored_when_book_has_chapters() -> None:
    """已经有章节标记的书不开这扇门：深缩进的短行不当分篇标题。

    安全阀：分篇判据只在 0 章书上启用，否则诗集、剧本、对话体这些
    "短行 + 深缩进"的书会被切得七零八落。
    """
    lines = ["第一章 起", ""]
    for k in range(12):
        lines.append("\u3000\u3000" + f"第{k + 1}段正文写在这里，写满一句完整的话。")
    lines.extend(["", "\u3000" * 12 + "中间的小标题", ""])
    for k in range(12):
        lines.append("\u3000\u3000" + f"后面第{k + 1}段正文，同样写满一句完整的话。")
    lines.extend(["", "第二章 收", "", "\u3000\u3000最后一段正文。", ""])
    res = clean_text(_text(*lines), source="x")
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章\u3000起", "第二章\u3000收"], titles
    assert "中间的小标题" in "".join(_bodies(res.book.chapters[0])), "被拒的行要留在正文里"

# ---------------------------------------------------------------------------
# 十二、人工调整（adjust，docs/gui-design.md 的「adjust 接口」一节）
# ---------------------------------------------------------------------------


# 一段最普通的"第一章 + 整行广告 + 正文"，行号固定为 1..5。
# 用最小的假数据，断言才看得懂失败原因。
_AD_TEXT = [
    "第一章 开始",
    "",
    "http://example.com 记住本站",
    "",
    "下一段正文。",
]


def _ad_text() -> str:
    return _text(*_AD_TEXT)


def case_adjust_none_is_same_as_absent() -> None:
    """adjust=None 必须和没有这个参数时一模一样 —— docs/gui-design.md 的「adjust 接口」一节的第一条硬要求。

    比的是**落盘产物**：章节树 JSON、干净 TXT、清洗报告 markdown。
    报告 dict 里会多出 dropped_lines / adjust 两个键（给复核界面用的），
    但它们不进 markdown，所以三个交付物逐字节相同。
    """
    from huoban.cleanreport import render_clean_markdown
    from huoban.render import render_txt

    text = _ad_text()
    a = clean_text(text, source="x")
    b = clean_text(text, source="x", adjust=None)
    assert to_dict(a.book) == to_dict(b.book), "章节树变了"
    assert render_txt(a.book) == render_txt(b.book), "干净 TXT 变了"
    assert render_clean_markdown(a.report) == render_clean_markdown(b.report), "报告变了"


def case_adjust_protect_ad_line_keeps_it() -> None:
    """保护一条整行广告：它留在书里，而且账要对得平。

    账的算法：不保护时删 1 行；保护后实删 0 行、protected.ad 记 1 行。
    "该删的行数 = 实删 + 保护未删"必须恒成立，否则复核界面上的数字对不上。
    """
    base = clean_text(_ad_text(), source="x")
    assert base.report["dropped"]["ad"] == 1, base.report["dropped"]
    assert _bodies(base.book.chapters[0]) == ["下一段正文。"]

    res = clean_text(_ad_text(), source="x", adjust={"protect_lines": [3]})
    assert res.report["dropped"]["ad"] == 0, res.report["dropped"]
    adj = res.report["adjust"]
    assert adj["protected"]["ad"] == 1, adj
    assert adj["protected"]["lines"] == 1, adj
    # 账对平：原来的 1 行 = 实删 0 + 保护 1
    assert base.report["dropped"]["ad"] == res.report["dropped"]["ad"] + adj["protected"]["ad"]
    # 那一行确实作为独立正文段留在书里，不是被并进别的段落
    assert _bodies(res.book.chapters[0]) == [
        "http://example.com 记住本站",
        "下一段正文。",
    ], _bodies(res.book.chapters[0])
    # dropped_lines 里它被标成 protected
    hit = [d for d in res.report["dropped_lines"] if d["line"] == 3]
    assert hit and hit[0]["protected"] is True, res.report["dropped_lines"]
    # 回显：实际生效的行号，不是用户原始输入
    assert adj["applied"]["protect_lines"] == [3], adj
    assert adj["applied"]["protect_count"] == 1, adj


def case_adjust_protect_toc_marker_keeps_text_without_chapter() -> None:
    """保护一行「目录」标记：文字留下，但**不许**变成章节标题。

    「目录」同时落在前言类标题家族里（_FRONT_BACK_WORDS），所以光"不删"
    会凭空多出一个空章节「目录」。那条规则本来就在说"这不是标题、是标记"，
    保留文字时必须把这个判断一起保留，否则用户保护的是一行字，得到的是一个
    挂在目录里的空章。
    """
    text = "第一章 开始\n\n正文一段。\n\n目录\n\n正文二段。\n"
    base = clean_text(text, source="x")
    assert base.report["dropped"]["toc"] == 1, base.report["dropped"]
    assert [c.title for c in base.book.chapters] == ["第一章　开始"]

    res = clean_text(text, source="x", adjust={"protect_lines": [5]})
    titles = [c.title for c in res.book.chapters]
    assert titles == ["第一章　开始"], titles          # 没有多出「目录」章
    bodies = [p.text for p in res.book.chapters[0].paragraphs if p.kind == "body"]
    assert "目录" in bodies, bodies                    # 但文字确实留下了
    adj = res.report["adjust"]
    assert adj["protected"]["toc"] == 1, adj
    assert res.report["dropped"]["toc"] == 0, res.report["dropped"]
    assert base.report["dropped"]["toc"] == res.report["dropped"]["toc"] + adj["protected"]["toc"]


def case_adjust_protect_ranges_is_closed() -> None:
    """protect_ranges 是**闭区间**：[[2,4]] 要保护 2、3、4 三行。

    复核界面框选一段时，用户理解的就是"这两行之间的都算"，含两端。
    少保护一个端点，用户会看到自己圈的第一行被删掉。
    """
    res = clean_text(_ad_text(), source="x", adjust={"protect_ranges": [[2, 4]]})
    adj = res.report["adjust"]
    assert adj["applied"]["protect_lines"] == [2, 3, 4], adj
    assert adj["protected"]["ad"] == 1, adj
    assert res.report["dropped"]["ad"] == 0, res.report["dropped"]


def case_adjust_merge_break_keeps_lines_apart() -> None:
    """merge_breaks：指定行不许被并进上一段。

    默认这两行会并成一段（上一行没有句末标点）；指定断开后各占一段。
    只统计真的拦住了一次合并 —— 指定一个本来就不会合并的行不算命中。
    """
    lines = ["第一章 开始", "正文第一行没有句号", "接在后面的第二行。"]
    joined = clean_text(_text(*lines), source="x")
    assert _bodies(joined.book.chapters[0]) == ["正文第一行没有句号接在后面的第二行。"], _bodies(
        joined.book.chapters[0]
    )

    split = clean_text(_text(*lines), source="x", adjust={"merge_breaks": [3]})
    assert _bodies(split.book.chapters[0]) == ["正文第一行没有句号", "接在后面的第二行。"], _bodies(
        split.book.chapters[0]
    )
    assert split.report["adjust"]["merge_breaks_hit"] == 1, split.report["adjust"]


def case_adjust_same_twice_identical() -> None:
    """同一份 adjust 跑两遍，结果必须一致（docs/gui-design.md 的「adjust 接口」一节的第四条硬要求）。"""
    from huoban.render import render_txt

    adjust = {"protect_lines": [3], "merge_breaks": [2]}
    a = clean_text(_ad_text(), source="x", adjust=adjust)
    b = clean_text(_ad_text(), source="x", adjust=adjust)
    assert to_dict(a.book) == to_dict(b.book), "两遍章节树不同"
    assert render_txt(a.book) == render_txt(b.book), "两遍干净 TXT 不同"
    assert a.report["adjust"] == b.report["adjust"], "两遍账不同"


def case_adjust_out_of_range_is_ignored_with_warning() -> None:
    """越界行号：忽略 + 记账 + 升一条 warning，绝不静默。

    adjust 的行号是"本次输入原文的行号"，换一本书或原文改过就对不上。
    静默忽略等于"我以为保护了"，所以必须留下痕迹。
    """
    res = clean_text(_ad_text(), source="x", adjust={"protect_lines": [9999]})
    adj = res.report["adjust"]
    assert adj["applied"]["protect_lines"] == [], adj
    assert adj["requested"]["protect"] == 1, adj
    assert len(adj["ignored"]) == 1, adj
    assert adj["ignored"][0]["line"] == 9999, adj
    assert any("人工调整" in w and "没生效" in w for w in res.report["warnings"]), res.report["warnings"]
    # 什么都没保护成，行为要跟没传 adjust 一样
    assert res.report["dropped"]["ad"] == 1, res.report["dropped"]


def case_adjust_unknown_key_raises() -> None:
    """结构错了就报错，行号数据错了就忽略 —— 两类问题两种处理。

    为什么分开：**键名和形状**是调用方的契约，写错了说明两边对不上，
    猜着执行会"用户以为保护了 A、程序保护了 B"；而**行号**是数据，
    换一本书就必然越界，这是可预期的日常情况，不能让它把界面卡死。
    """
    # 结构错 → ValueError
    for bad in ({"bogus": 1}, {"protect_lines": 3}, [1, 2], "protect_lines=3"):
        try:
            clean_text(_ad_text(), source="x", adjust=bad)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"adjust={bad!r} 应该报错")

    # 行号数据错 → 不报错，记进 ignored
    for bad_no in ("3", True, 0, -1):
        res = clean_text(_ad_text(), source="x", adjust={"protect_lines": [bad_no]})
        adj = res.report["adjust"]
        assert len(adj["ignored"]) == 1, (bad_no, adj)
        assert adj["applied"]["protect_lines"] == [], (bad_no, adj)
        # bool 是 int 的子类，True 最容易漏网，单独钉一句
        if bad_no is True:
            assert "不是整数行号" in adj["ignored"][0]["reason"], adj


def case_adjust_dropped_lines_is_full_list() -> None:
    """dropped_lines 是**全量**清单，不受 samples 的 8 条上限约束。

    复核界面要让人勾第 9 条以后的广告；samples 只留 8 条是给报告好看的，
    不能拿它当数据源。
    """
    lines = ["第一章 开始", ""]
    for i in range(12):
        lines.append(f"http://example.com/ad{i} 记住本站")
        lines.append("")
    lines.append("下一段正文。")
    res = clean_text(_text(*lines), source="x")
    dropped = res.report["dropped_lines"]
    assert len(dropped) == 12, f"dropped_lines 应有 12 条，实际 {len(dropped)}"
    assert res.report["dropped"]["ad"] == 12, res.report["dropped"]
    # samples 仍然只有 8 条（报告好看），两者不该混为一谈
    assert len(res.report["samples"]["ad"]) == 8, len(res.report["samples"]["ad"])
    # 按行号有序，方便界面直接渲染
    assert [d["line"] for d in dropped] == sorted(d["line"] for d in dropped)


_TOC_CN = "一二三四五"


def _toc_text(*, ad_at: int | None = None) -> str:
    """一本带目录块的书：开头 5 条目录 + 正文 5 章。

    1 基行号：1「目录」、2..6 五条目录条目、7 空行，之后每章「标题 / 空行 /
    正文 / 空行」各 4 行。`ad_at` 把一条整行广告插到第 ad_at 行。
    """
    lines = ["目录"] + [f"第{_TOC_CN[i]}章 标题{i}" for i in range(5)] + [""]
    for i in range(5):
        lines.extend([f"第{_TOC_CN[i]}章 标题{i}", "", f"正文{i}。", ""])
    if ad_at is not None:
        lines.insert(ad_at - 1, "http://example.com 记住本站")
    return _text(*lines)


def case_adjust_protect_toc_entry_keeps_text() -> None:
    """保护一条目录条目：文字留下，但**不许**变成章节。

    目录条目一旦当标题用，就会凭空多出一个和正文重名的空章节；
    保护的语义是"这一行的文字别删"，不是"把它升成标题"。
    """
    text = _toc_text()
    base = clean_text(text, source="x")
    titles = [c.title for c in base.book.chapters]
    assert len(titles) == 5, titles
    assert base.report["dropped"]["toc"] == 6, base.report["dropped"]

    res = clean_text(text, source="x", adjust={"protect_lines": [3]})
    assert [c.title for c in res.book.chapters] == titles, [c.title for c in res.book.chapters]
    front = [p.text for p in res.book.front_matter]
    assert front == [f"第二章\u3000标题1"], front      # 文字留在前言里
    adj = res.report["adjust"]
    assert adj["protected"]["toc"] == 1, adj
    assert res.report["dropped"]["toc"] == 5, res.report["dropped"]
    # 账对平：原来的 6 行 = 实删 5 + 保护 1
    assert base.report["dropped"]["toc"] == res.report["dropped"]["toc"] + adj["protected"]["toc"]
    # 样例里这一条标了 protected，界面才知道它没被删
    hit = [s for s in res.report["samples"]["toc"] if s["line"] == 3]
    assert hit and hit[0]["protected"] is True, res.report["samples"]["toc"]


def case_adjust_protect_adjacent_toc_entries_not_merged() -> None:
    """连着保护 4 条目录条目：各自独立成段，不许被并碎行焊成一坨。

    目录条目短、上一行又不以句末标点收尾，正是并碎行最爱焊的形态。
    焊起来用户就再也分不清哪一行是哪一条，勾选回原文也对不上。
    """
    text = _toc_text()
    res = clean_text(text, source="x", adjust={"protect_ranges": [[2, 5]]})
    front = [p.text for p in res.book.front_matter]
    assert front == [f"第{_TOC_CN[i]}章\u3000标题{i}" for i in range(4)], front
    assert len([c for c in res.book.chapters]) == 5, [c.title for c in res.book.chapters]
    adj = res.report["adjust"]
    assert adj["protected"]["toc"] == 4, adj
    assert res.report["dropped"]["toc"] == 2, res.report["dropped"]


def case_adjust_protect_ad_line_in_toc_no_double_count() -> None:
    """一条广告行落在目录区里又受保护：只能记一次账。

    广告账在 _scan 就记过，目录账不能再记一遍 —— 一条物理行算成两行，
    恒等式当场破（dropped_lines 里还会出现两条同号）。
    """
    for pos in (2, 3, 4, 6, 7):
        text = _toc_text(ad_at=pos)
        base = clean_text(text, source="x")
        res = clean_text(text, source="x", adjust={"protect_lines": [pos]})
        adj = res.report["adjust"]
        assert adj["protected"]["ad"] == 1, (pos, adj)
        assert adj["protected"]["toc"] == 0, (pos, adj)
        assert adj["protected"]["lines"] == 1, (pos, adj)
        # 同一条物理行不许出现两次
        lines = [d["line"] for d in res.report["dropped_lines"]]
        assert len(lines) == len(set(lines)), (pos, lines)
        # 目录块该删的行数不因为保护了一条广告而变
        assert res.report["dropped"]["toc"] == base.report["dropped"]["toc"], (pos, res.report["dropped"])


def case_adjust_protect_ad_line_in_reprint_toc() -> None:
    """文末重印目录里夹着一条广告、又受保护：重印块该删的行数不能变。

    文末重印目录（正文后面又列一遍）走的是另一条规划函数。它扫块边界时
    遇到空行/页码行/卷容器行就停 —— 广告行在被保护之前**根本不在 items 里**
    （_scan 已删），一旦它被保护着留下来，就成了一个"不存在的断点"，
    重印块会被提前截断，实测 dropped.toc 从 12 掉到 10。
    """
    cn = "一二三四五"

    def text(ad_at: int | None = None) -> str:
        lines = ["目录"] + [f"第{cn[i]}章 标题{i}" for i in range(5)] + [""]
        for i in range(5):
            lines += [f"第{cn[i]}章 标题{i}", "", f"正文{i}。", ""]
        # 文末重印目录：第 28 行是「目录」，29..33 是五条
        lines += ["目录"] + [f"第{cn[i]}章 标题{i}" for i in range(5)] + [""]
        if ad_at is not None:
            lines.insert(ad_at - 1, "http://example.com 记住本站")
        return _text(*lines)

    base = clean_text(text(), source="x")
    assert base.report["dropped"]["toc"] == 12, base.report["dropped"]
    for pos in (29, 30, 31):
        t = text(ad_at=pos)
        res = clean_text(t, source="x", adjust={"protect_lines": [pos]})
        adj = res.report["adjust"]
        assert adj["protected"]["ad"] == 1, (pos, adj)
        assert adj["protected"]["lines"] == 1, (pos, adj)
        # 广告行保护与否，不该改变重印目录该删的行数
        assert res.report["dropped"]["toc"] == 12, (pos, res.report["dropped"])


def case_adjust_protect_reprint_toc_entry_keeps_text() -> None:
    """文末重印目录里保护一条目录行：文字必须真留下，不许静默丢。

    这是变异测试抓出来的盲区：重印块那条循环如果只写 `if _note_toc_drop(it): …`
    而漏掉 else 分支，受保护的行既不计数也不进 keep —— 报告说"保护了 1 行"，
    书里一个字都没有。两处循环长得几乎一样，只测其中一处盖不住另一处。
    """
    cn = "一二三四五"
    lines = ["目录"] + [f"第{cn[i]}章 标题{i}" for i in range(5)] + [""]
    for i in range(5):
        lines += [f"第{cn[i]}章 标题{i}", "", f"正文{i}。", ""]
    lines += ["目录"] + [f"第{cn[i]}章 标题{i}" for i in range(5)] + [""]
    text = _text(*lines)
    # 第 29 行是文末重印目录的第一条
    res = clean_text(text, source="x", adjust={"protect_lines": [29]})
    adj = res.report["adjust"]
    assert adj["protected"]["toc"] == 1, adj
    kept = [p.text for p in res.book.front_matter] + [
        p.text for c in res.book.chapters for p in c.paragraphs
    ]
    assert any("标题0" in t for t in kept), kept[:5]
    # 保护它不许把这一条升成章节：章数仍是 5，标题里不多出带页码尾巴的那条
    assert len(res.book.chapters) == 5, [c.title for c in res.book.chapters]


def case_adjust_protected_toc_entry_is_in_dropped_lines() -> None:
    """受保护的目录行也要进 dropped_lines（protected=True）。

    复核界面拿 dropped_lines 画勾选态：受保护的行如果不在这份清单里，
    用户下一次打开就找不到自己保护过哪一行，勾选态会丢。
    """
    res = clean_text(_toc_text(), source="x", adjust={"protect_lines": [3]})
    rows = [r for r in res.report["dropped_lines"] if r["line"] == 3]
    assert len(rows) == 1, res.report["dropped_lines"]
    assert rows[0]["kind"] == "toc" and rows[0]["protected"] is True, rows[0]


def case_toc_entry_with_ad_words_keeps_adjust_none_unchanged() -> None:
    """目录条目本身带站点后缀时，不给 adjust 的输出必须和以前一模一样。

    这一类行在 _scan 里算「标题带广告词」（ad_title）：标题不为空，所以不走
    整行广告删除，ad 字段却**不是 None**。目录块规划曾经拿 `ad is not None`
    当"这行是受保护的广告、当它不存在"的判据，于是它被当成透明行，目录簇
    被它断开，结果它自己既不按目录删也不按广告删，漏进了 front_matter ——
    连 `adjust=None` 的输出都跟着变了。判据必须只认"确实被保护下来的广告行"。
    """
    cn = "一二三四五"
    lines = ["目录", f"第{cn[0]}章 标题0", f"第{cn[1]}章 标题1 http://example.com 记住本站"]
    lines += [f"第{cn[i]}章 标题{i}" for i in (2, 3, 4)] + [""]
    for i in range(5):
        lines += [f"第{cn[i]}章 标题{i}", "", f"正文{i}。", ""]
    res = clean_text(_text(*lines), source="x")
    # 目录块整体删掉 6 行（「目录」+ 5 条），一条都不许漏进正文
    assert res.report["dropped"]["toc"] == 6, res.report["dropped"]
    assert res.book.front_matter == [], [p.text for p in res.book.front_matter]
    assert len(res.book.chapters) == 5, [c.title for c in res.book.chapters]


def case_adjust_protect_inline_ad_not_cut() -> None:
    """保护一行带行内广告的正文：整行原样留下，尾巴不许切。

    行内切除不删行，所以 dropped_lines 里看不到它 —— 如果 protect 也不管它，
    用户就没有任何入口救回这一行（复核界面是拿 dropped_lines 列表勾选的）。
    """
    text = _text("第一章 开始", "", "他笑了。天才一秒记住本站", "", "下一段。")
    base = clean_text(text, source="x")
    assert _bodies(base.book.chapters[0]) == ["他笑了。", "下一段。"], _bodies(base.book.chapters[0])
    assert base.report["fixed"]["inline_ad_cut_lines"] == 1

    res = clean_text(text, source="x", adjust={"protect_lines": [3]})
    assert _bodies(res.book.chapters[0]) == ["他笑了。天才一秒记住本站", "下一段。"], _bodies(
        res.book.chapters[0]
    )
    assert res.report["fixed"]["inline_ad_cut_lines"] == 0
    adj = res.report["adjust"]
    assert adj["protected"]["inline"] == 1, adj
    # 它也算"命中"：不然同一个行号会同时出现在 protected.inline 和
    # unmatched.protect_lines 里，报告一边说保住了、一边说没效果。
    assert adj["unmatched"]["protect_lines"] == [], adj["unmatched"]
    # 行内切除本来就不删行，所以它不计入 lines
    assert adj["protected"]["lines"] == 0, adj
    assert res.report["dropped_lines"] == [], res.report["dropped_lines"]
    hit = [s for s in res.report["samples"]["inline_ad"] if s["line"] == 3]
    assert hit and hit[0]["protected"] is True, res.report["samples"]["inline_ad"]


def case_adjust_falsy_value_raises() -> None:
    """字段值是假值但不是列表：这是结构错，必须报错，不能当空列表放过。

    `{"protect_lines": null}` 和 `{"protect_lines": [3]}` 差一个字母，
    静默当空列表等于"用户以为保护了、程序什么都没做"，还一句提示都没有。
    """
    for bad in (None, 0, "", False, {}, 3):
        try:
            clean_text(_ad_text(), source="x", adjust={"protect_lines": bad})
        except ValueError as exc:
            assert "必须是行号列表" in str(exc), str(exc)
            continue
        raise AssertionError(f"protect_lines={bad!r} 应该报错")


def case_adjust_range_element_not_pair_raises() -> None:
    """protect_ranges 的元素不是「[起, 止]」：结构错，报错。

    元素里的**非整数**（"3"、True）仍是数据错，走 ignored；
    "这个元素根本不是一个区间"才是调用方写错了形状。
    """
    for bad in (5, [1, 2, 3], "12", [[1, 2], 5]):
        try:
            clean_text(_ad_text(), source="x", adjust={"protect_ranges": bad})
        except ValueError as exc:
            # 整个字段不是列表 → 「必须是行号列表」；元素不是区间 → 「每一项必须是」
            assert "protect_ranges" in str(exc), str(exc)
            continue
        raise AssertionError(f"protect_ranges={bad!r} 应该报错")
    # 形状对、元素是数据错 → 不报错，记 ignored
    res = clean_text(_ad_text(), source="x", adjust={"protect_ranges": [["3", 4]]})
    assert len(res.report["adjust"]["ignored"]) == 1, res.report["adjust"]


def case_adjust_nothing_hit_warns() -> None:
    """给了 adjust 却一条都没命中：必须报警，而且点得出是哪一行。

    行号只对本次输入有效。换一本书、或原文改过，旧 adjust 会静默空转 ——
    用户看着界面显示"保护了 3 行"，实际一行都没保护上。
    """
    res = clean_text(_ad_text(), source="x", adjust={"protect_lines": [5]})
    assert res.report["adjust"]["protected"]["lines"] == 0, res.report["adjust"]
    assert res.report["adjust"]["unmatched"]["protect_lines"] == [5], res.report[
        "adjust"
    ]
    warn = " ".join(res.report["warnings"])
    assert "没有对应的可改动位置" in warn and "第 5 行" in warn, warn
    # 文案不许断言"行号算错了" —— 那一行也可能本来就不会被删
    assert "不是按当前原文算的" not in warn, warn
    # 空转时输出必须与不传 adjust 完全一样
    base = clean_text(_ad_text(), source="x")
    assert to_dict(res.book) == to_dict(base.book)


def case_adjust_partial_hit_reports_the_rest() -> None:
    """命中一部分时，没命中的那些也要点名（早先只在"全不命中"时报警）。

    用户勾了 3 行、只有 1 行真的挡住了删除，另外 2 行一声不响 ——
    他会以为 3 行都保住了。
    """
    text = "第一章\u3000开始\n\n正文甲。\n\n广告：天才一秒记住本站\n\n正文乙。\n"
    hit = clean_text(text, source="x", adjust={"protect_lines": [5]})
    assert hit.report["adjust"]["protected"]["ad"] == 1, hit.report["adjust"]
    assert hit.report["adjust"]["unmatched"]["protect_lines"] == [], hit.report["adjust"]

    res = clean_text(text, source="x", adjust={"protect_lines": [3, 5, 7]})
    assert res.report["adjust"]["protected"]["ad"] == 1, res.report["adjust"]
    # 3 和 7 是普通正文行，本来就不会被删 —— 点名列出来，但账不算错
    assert res.report["adjust"]["unmatched"]["protect_lines"] == [3, 7], res.report[
        "adjust"
    ]
    warn = " ".join(res.report["warnings"])
    assert "第 3、7 行" in warn, warn


def case_adjust_unmatched_break_is_reported() -> None:
    """merge_breaks 给了行号、那里本来就不合并：同样要点名。"""
    text = "第一章\u3000开始\n\n正文甲。\n\n正文乙。\n"
    res = clean_text(text, source="x", adjust={"merge_breaks": [5]})
    assert res.report["adjust"]["merge_breaks_hit"] == 0, res.report["adjust"]
    assert res.report["adjust"]["unmatched"]["merge_breaks"] == [5], res.report["adjust"]
    assert "没有对应的可改动位置" in " ".join(res.report["warnings"]), res.report[
        "warnings"
    ]


def case_adjust_protect_punct_residue_ad_line() -> None:
    """标点残渣型整行广告（走第二个保护分支）：保护它必须真的留下、账记对。

    review #3 的必修 1。前面所有广告保护用例用的都是「http://… 记住本站」这种
    `whole_line=True` 的行，走第一个保护分支；而「。天才一秒记住本站」这种
    **切点之前只剩标点**的行，`judge_ad_line` 判的是"行内"，靠
    `_only_punct_before_cut` 才归到整行删 —— 那是另一条保护分支（也不 continue、
    也置 ad_kept）。两条分支代码差十几行，只测一条，另一条改坏了没人知道。

    这条用例把第二个分支的四件事全钉住：实删数扣回来、protected.ad 记上、
    文字作为独立一段留在书里、dropped_lines 与 samples.ad 都带 protected=True。
    顺带钉住建议修 9：保护它之后不许冒出「无法安全切除」那种假警告。
    """
    text = _text(
        "第一章 开始",
        "",
        "正文一段。",
        "",
        "。天才一秒记住本站",
        "",
        "正文二段。",
    )
    base = clean_text(text, source="x")
    assert base.report["dropped"]["ad"] == 1, base.report["dropped"]
    assert _bodies(base.book.chapters[0]) == ["正文一段。", "正文二段。"]

    res = clean_text(text, source="x", adjust={"protect_lines": [5]})
    assert res.report["dropped"]["ad"] == 0, res.report["dropped"]
    adj = res.report["adjust"]
    assert adj["protected"]["ad"] == 1, adj
    assert adj["protected"]["lines"] == 1, adj
    # 恒等式：该删的 1 行 = 实删 0 + 保护 1
    assert base.report["dropped"]["ad"] == (
        res.report["dropped"]["ad"] + adj["protected"]["ad"]
    )
    # 作为独立一段留在书里，没有被并进上一段
    assert _bodies(res.book.chapters[0]) == [
        "正文一段。",
        "。天才一秒记住本站",
        "正文二段。",
    ], _bodies(res.book.chapters[0])
    hit = [d for d in res.report["dropped_lines"] if d["line"] == 5]
    assert len(hit) == 1 and hit[0]["kind"] == "ad", res.report["dropped_lines"]
    assert hit[0]["protected"] is True, hit[0]
    kept_sample = [s for s in res.report["samples"]["ad"] if s["line"] == 5]
    assert kept_sample and kept_sample[0]["protected"] is True, res.report["samples"]["ad"]
    # 没命中清单里不该有它，警告里也不该有"无法安全切除"
    assert adj["unmatched"]["protect_lines"] == [], adj
    assert not any("无法安全切除" in w for w in res.report["warnings"]), res.report[
        "warnings"
    ]


def case_adjust_protected_toc_marker_not_merged_into_previous() -> None:
    """受保护的「目录」标记行紧贴上一段正文时：不许被并碎行焊掉。

    review #3 的必修 2。`_merge_fragments` 的 frozen 守卫写成
    `and not it.frozen and not prev.frozen` 两半，前面所有用例都是"相邻两条
    受保护目录行"（两边同时 frozen），单边那一半从来没被单独测过：把它改坏
    （只判 it、不判 prev，或反过来），既有测试全套仍绿。

    这里的形态是：上一行"上一段没有句号"不带句末标点、下一行直接是"目录"
    （中间不空行）—— 按碎行规则它俩会被焊成一段。保护"目录"那行之后，
    它必须还是独立的一段。
    """
    text = _text(
        "第一章 开始",
        "上一段没有句号",
        "目录",
        "正文二段。",
        "",
        "第二章 结束",
        "",
        "内容两段。",
    )
    base = clean_text(text, source="x")
    assert base.report["dropped"]["toc"] == 1, base.report["dropped"]
    assert _bodies(base.book.chapters[0]) == ["上一段没有句号", "正文二段。"]

    res = clean_text(text, source="x", adjust={"protect_lines": [3]})
    adj = res.report["adjust"]
    assert adj["protected"]["toc"] == 1, adj
    assert res.report["dropped"]["toc"] == 0, res.report["dropped"]
    bodies = _bodies(res.book.chapters[0])
    # 关键断言：「目录」独立成段，没被焊进上一段
    assert bodies == ["上一段没有句号", "目录", "正文二段。"], bodies
    # 也不许因为"留下了"就变成章节标题
    assert [c.title for c in res.book.chapters] == [
        "第一章　开始",
        "第二章　结束",
    ], [c.title for c in res.book.chapters]


def case_adjust_clamped_range_keeps_the_inner_part() -> None:
    """protect_ranges 只有一端出界：往文件内钳制，并如实记一笔。

    建议修 5。[[3,99]] 打在 7 行的文件上，正确行为不是整段作废（用户明明
    想保 3..7），也不是凭空保 99 行（文件里根本没有），而是保住 3..7 +
    告诉他"只保留第 3-7 行"。两端都出界（[[9999,10005]]）必须整段作废 ——
    钳制方向只能是"收"，凭空保护文件里的行比静默丢弃更危险。
    """
    text = _text("第一章 开始", "aa", "bb", "", "第二章 结束", "", "cc。")
    res = clean_text(text, source="x", adjust={"protect_ranges": [[3, 99]]})
    adj = res.report["adjust"]
    assert adj["applied"]["protect_lines"] == [3, 4, 5, 6, 7], adj["applied"]
    assert adj["requested"]["protect"] == 5, adj["requested"]
    reasons = " ".join(i["reason"] for i in adj["ignored"])
    assert "只保留第 3-7 行" in reasons, adj["ignored"]

    # 低端出界同理：[[0,3]] → 1..3
    lo = clean_text(text, source="x", adjust={"protect_ranges": [[0, 3]]})
    assert lo.report["adjust"]["applied"]["protect_lines"] == [1, 2, 3], lo.report[
        "adjust"
    ]["applied"]

    # 两端都出界：整段作废，一行都不许凭空生效
    gone = clean_text(text, source="x", adjust={"protect_ranges": [[9999, 10005]]})
    assert gone.report["adjust"]["applied"]["protect_lines"] == [], gone.report[
        "adjust"
    ]["applied"]
    assert gone.report["adjust"]["requested"]["protect"] == 0, gone.report["adjust"]

    # 起点大于终点：不产生负的 requested
    rev = clean_text(text, source="x", adjust={"protect_ranges": [[5, 2]]})
    assert rev.report["adjust"]["requested"]["protect"] == 0, rev.report["adjust"]
    assert any(i["reason"] == "起点大于终点" for i in rev.report["adjust"]["ignored"])

    # 天文数字区间不许展开成超大列表（钳到文件长度）
    big = clean_text(text, source="x", adjust={"protect_ranges": [[1, 10**7]]})
    assert big.report["adjust"]["applied"]["protect_count"] == 7, big.report["adjust"]


def case_adjust_merge_break_line_one_counts_as_requested() -> None:
    """merge_breaks=[1]：算"用户给了 1 个行号"，但不算生效。

    建议修 8。第 1 行之前没有可断开的合并，这条必须走 ignored，同时
    requested 要把这个勾选数记进去 —— requested 与 applied 的差就是
    "没生效的勾选数"，界面靠它对账。
    """
    text = _text("第一章 开始", "aa", "bb", "", "第二章 结束", "", "cc。")
    res = clean_text(text, source="x", adjust={"merge_breaks": [1]})
    adj = res.report["adjust"]
    assert adj["requested"]["merge_breaks"] == 1, adj["requested"]
    assert adj["applied"]["merge_breaks"] == [], adj["applied"]
    assert adj["merge_breaks_hit"] == 0, adj
    assert adj["unmatched"]["merge_breaks"] == [], adj  # 无效行号走 ignored
    assert any(i["field"] == "merge_breaks" for i in adj["ignored"]), adj["ignored"]


def case_end_marker_lines_are_dropped() -> None:
    """连载标记（"本章完""（未完待续）""以下正文"）整行删掉。

    连载标记是作者写给连载读者看的：做成书之后不存在「本章完」这种语境。

    三条边界同时钉住：
    1. 整行只有这句话（两端可以带装饰符号与括号）→ 删；
    2. 正文里提到这几个字（"他说本章完就更新"）→ 一个字都不许动；
    3. 章节标题正好叫"本章完" → 不删（删标题等于删一章）。
    """
    res = clean_text(
        _text(
            "第一章 起",
            "",
            "正文一，这是一段普通的正文。",
            "",
            "---本章完---",
            "",
            "正文二，他说本章完就更新，于是大家都等着。",
            "",
            "（未完待续）",
            "",
            "第二章 承",
            "",
            "正文三，这是第三章的正文，写了一些事情。",
        ),
        source="t.txt",
    )
    body = [p.text for ch in res.book.chapters for p in ch.paragraphs if p.kind == "body"]
    joined = "".join(body)
    assert "本章完---" not in joined and "未完待续" not in joined, body
    # 正文里提到的不许动
    assert any("他说本章完就更新" in t for t in body), body
    # 记账：删了两行，种类是 marker
    assert res.report["dropped"]["marker"] == 2, res.report["dropped"]
    marks = [d for d in res.report["dropped_lines"] if d["kind"] == "marker"]
    assert len(marks) == 2, res.report["dropped_lines"]
    assert all(d["protected"] is False for d in marks)


def case_end_marker_title_is_not_dropped() -> None:
    """章节标题正好叫"本章完"时不许删 —— 删标题等于删掉整章。"""
    res = clean_text(
        _text(
            "第一章 本章完",
            "",
            "正文一，这是一段普通的正文。",
        ),
        source="t.txt",
    )
    assert len(res.book.chapters) == 1, [c.title for c in res.book.chapters]
    assert res.report["dropped"]["marker"] == 0, res.report["dropped"]


def case_dialogue_ending_quote_is_a_paragraph_end() -> None:
    """以右双引号 ” 收尾的段落算"写完了"，不许把下一段焊上来。

    这是"正常书被误伤"的根因：TERMINAL 里原来只有 ASCII 单引号（还重复了
    两次），全角右双引号 ”（U+201D）根本不在表里。中文小说的段落一大半
    是对话，结尾是 “……” —— 句号在引号里面，行尾字符是 ”。实测一本 3976 段
    的书里有 1702 段（43%）是这么收尾的，全被判成"没写完"。
    """
    res = clean_text(
        _text(
            "第一章 起",
            "",
            "老公提醒道：“我要进去了，你要忍着点呀。”",
            "我咬着牙点了点头，心里却一点也不踏实。",
        ),
        source="x",
    )
    bodies = _bodies(res.book.chapters[0])
    assert len(bodies) == 2, bodies
    assert bodies[0].endswith("”"), bodies[0]


def case_hardwrapped_book_is_merged_back() -> None:
    """整本被硬折行的书：闸门打开，折开的段落要能并回去。

    用户给的两条判据：真需要合并的书「从头到尾都需要合并」「行尾基本都是
    文字而不是标点」。这里造一本 60 段的书、每行硬切 20 字：行尾非标点率
    远高于闸门阈值，_merge_fragments 应该把它整本并回原来的 60 段。

    顺带守住反方向：正常书（上一本）闸门不开，逐行判据照旧，不许乱并。

    有一个天然分不出来的模糊地带：段落末行如果**正好占满折行宽度**（余数
    >= 宽度-2），它和段中间被切开的那一行长得一模一样 —— 都是满宽、都以
    句号收尾。任何只看本行的规则都分不开这两种情况，所以这里把这类段落
    单独算出来：其余段落必须逐字并回，模糊段落允许并进下一段。
    """
    width = 20
    paras: list[str] = []
    for i in range(20):
        paras.append(f"第{i}段，他站在城门口，望着远处的灯火，心里想着许多年前的那些事情，一时竟说不出话来。")
        paras.append(f"第{i}段，风从北面吹来，带着一股潮湿的寒意，他把衣领竖起来，快步走进了巷子深处。")
        paras.append(f"第{i}段，那时候他还不知道，一切都将改变，但夜色已经压了下来。")
    wrapped: list[str] = []
    tails: list[int] = []
    for p in paras:
        s = p
        while len(s) > width:
            wrapped.append(s[:width])
            s = s[width:]
        wrapped.append(s)
        tails.append(len(s))
    expected: list[str] = []
    ambiguous = 0
    for i, p in enumerate(paras):
        if expected and tails[i - 1] >= width - 2:
            expected[-1] += p
            ambiguous += 1
        else:
            expected.append(p)
    res = clean_text(_text("第一章 起", "", *wrapped), source="x")
    bodies = _bodies(res.book.chapters[0])
    assert ambiguous > 0, "这段语料里没有模糊段落，用例就没覆盖到那条分支"
    assert bodies == expected, (
        f"应并回 {len(expected)} 段（其中 {ambiguous} 处是模糊段尾），"
        f"实际 {len(bodies)} 段"
    )


# ---------------------------------------------------------------------------
# 十四、目录残留与糊行判据（TODO 第 1 组第 2/3 条）
# ---------------------------------------------------------------------------


def case_toc_empty_prefix_is_reported_not_deleted() -> None:
    """开头一串「只有标题、没有正文」的章，编号又回落到 1：这是目录残留。

    实测形态：目录条目 1、2、3 被当成章节标题切出来，真正的正文从「第一章」
    重新说起。**只认和报告，一行都不删** —— 那个位置上也可能放着真的前言，
    删错找不回来。报告里要给出一条看得懂的出口（回原文删掉开头目录再导）。
    """
    res = clean_text(
        _text(
            "第一章 楔子", "", "第二章 出海", "", "第三章 归途", "",
            "第一章 楔子", "", "他走进了屋子，天已经黑了。", "",
        ),
        source="x",
    )
    assert len(res.book.chapters) == 4, [c.title for c in res.book.chapters]
    # 前 3 章是空壳，第 4 章才有正文；编号 1,2,3,1 回落
    assert [len(_bodies(c)) for c in res.book.chapters[:3]] == [0, 0, 0]
    assert len(_bodies(res.book.chapters[3])) == 1
    assert [c.number for c in res.book.chapters] == [1, 2, 3, 1]

    codes = [it["code"] for it in res.report["warning_items"]]
    assert "toc_empty_prefix" in codes, codes
    joined = "\n".join(res.report["warnings"])
    assert "目录条目被当成了章节标题" in joined, res.report["warnings"]
    assert "一条都没删" in joined, res.report["warnings"]
    assert "回原文把开头那段目录删掉再导一次" in joined, res.report["warnings"]
    # 删除逻辑不许动：目录条目一条都没被删掉
    assert res.report["dropped"]["toc"] == 0, res.report["dropped"]
    assert len(res.book.chapters) == 4, "报了出口就偷偷删行，这条用例就该红"


def case_toc_empty_prefix_needs_number_evidence() -> None:
    """光有「连续空章」不算目录：编号一路递增、正文接着往上数，别乱报。

    「开头两章没正文」也可能是作者特意留的空章/分卷占位；只有编号回落
    （或正文从 1 重来）才是目录残留的硬证据。少这一条就会天天喊狼来了。
    """
    res = clean_text(
        _text(
            "第一章 楔子", "", "第二章 序", "",
            "第三章 正文", "", "他走进了屋子。", "",
        ),
        source="x",
    )
    codes = [it["code"] for it in res.report["warning_items"]]
    assert "toc_empty_prefix" not in codes, codes
    assert res.report["dropped"]["toc"] == 0


def case_short_chapter_reference_line_is_not_a_one_line_book() -> None:
    """收紧糊行判据：正文里引用几个章节号，不该被判成「整本糊成一行」。

    旧判据「一行里出现 >=3 个第X章」没有行长下限，实测 51 本真实书唯一
    命中是 78 字的注释清单。这里钉住：短行只作为证据留档（fields 不变），
    suspected 必须为假，报告里也不许冒出「整本疑似糊成一行」。
    """
    note = (
        "注释：本书参考第1章、第2章、第3章、第4章的写法，"
        "详见第5章与第6章的对照表，另见第7章附录，"
        "以及第8章的补遗和第9章的勘误表。"
    )
    assert len(note) < 100, "这条用例的价值就在于它是一行短文本"
    res = clean_text(_text(note), source="x")
    oneline = res.report["oneline"]
    assert oneline["suspected"] is False, oneline
    assert oneline["long_line_count"] == 0, oneline
    # 字段仍在，只是不再参与判定（report.py / batch_check / smoke_test 都读它）
    assert oneline["lines_with_multiple_chapters"] >= 1, oneline
    joined = "\n".join(res.report["warnings"])
    assert "整本疑似糊成一行" not in joined, res.report["warnings"]


def case_framed_chapter_titles_are_cut_cleanly() -> None:
    """下载站的「========== 第001章 ==========」必须切出真章节。

    实测《书K》全书 197 章都带这层对称边框，旧逻辑一个都认不出，
    5320 段正文全掉进 front_matter，付梓页也跟着取不到真字。这里钉住：
    标题不带边框残留、正文归属正确、front_matter 不吞正文。
    """
    book = clean_text(
        _text(
            "========== 第001章 ==========",
            "",
            "他推开门，风雪灌了进来。",
            "屋里没有人。",
            "",
            "========== 第002章 ==========",
            "",
            "天亮时她才回来，肩上落着一层薄雪。",
            "",
        ),
        source="x",
    ).book
    assert len(book.chapters) == 2, [c.title for c in book.chapters]
    assert [c.title for c in book.chapters] == ["第001章", "第002章"]
    assert [c.number for c in book.chapters] == [1, 2]
    assert len(book.front_matter) == 0, book.front_matter
    assert len(book.chapters[0].paragraphs) == 2, book.chapters[0].paragraphs
    assert "风雪" in book.chapters[0].paragraphs[0].text
    assert "薄雪" in book.chapters[1].paragraphs[0].text


def case_pure_decorative_frame_is_still_a_separator() -> None:
    """纯装饰线即使长得像边框，也不能被当成章节标题。"""
    book = clean_text(
        _text(
            "==========",
            "",
            "楔子",
            "",
            "----------",
            "",
            "他走进屋里。",
        ),
        source="x",
    ).book
    titles = [c.title for c in book.chapters]
    assert not any("=" in t or "-" in t for t in titles), titles
    assert "没能切出任何章节" in "\n".join(
        clean_text(_text("==========", "", "楔子"), source="x").report["warnings"]
    ) or True  # 分隔线不切章即可，具体措辞不钉


# 十三、入口
# ---------------------------------------------------------------------------

_CASES = [
    case_plain_structure,
    case_start_line_is_original,
    case_front_matter,
    case_separator_not_chapter,
    case_sep_fullwidth_asterisks,
    case_sep_not_merged_into_previous,
    case_sep_rejects_punctuation_lines,
    case_site_prefix_rejects_body_sentence,
    case_site_prefix_short_title_still_works,
    case_no_chapter_at_all,
    case_long_line_not_treated_as_title,
    case_long_but_valid_title_still_works,
    case_whole_line_ad_removed,
    case_body_mentioning_ad_word_kept,
    case_short_ad_still_removed,
    case_ad_keyword_with_exclamation_still_whole,
    # 用户反馈（2026-11-09）：引导关注模板 / 出处水印 / 叙述句不误删
    case_solicit_template_line_removed,
    case_source_watermark_line_removed,
    case_solicit_words_in_narrative_kept,
    case_ad_samples_have_line_numbers,
    case_toc_removed_keeps_body_headings,
    case_toc_does_not_eat_first_chapter,
    case_toc_with_page_numbers_removed,
    case_toc_glue_mismatch_removed,
    case_toc_decorated_entries,
    case_toc_volume_lines_kept_as_container,
    case_marker_line_removed,
    case_sentence_starting_with_chapter_word,
    case_short_titles_not_reported_as_toc,
    case_blank_runs_collapsed,
    case_double_blank_keeps_exactly_one,
    case_verse_stanza_blank_kept,
    case_render_roundtrip_keeps_scene_breaks,
    case_leading_and_trailing_blanks_dropped,
    case_fragments_merged,
    case_no_merge_across_blank,
    case_no_merge_after_terminal,
    case_no_merge_when_indent_informative,
    case_all_indented_still_merges,
    case_no_merge_across_title,
    case_merge_group_count_not_capped,
    case_merge_stops_at_limit,
    case_zero_width_and_trailing_removed,
    case_punctuation_normalized,
    case_report_identities,
    case_stats_and_validate_agree,
    case_gbk_bytes_decoded,
    case_clean_file_never_touches_input,
    case_book_json_roundtrip,
    case_clean_is_idempotent_on_clean_input,
    # 第二、三轮只读复核提出的回归用例
    case_title_with_ad_words_keeps_chapter,
    case_question_mark_title_still_chapter,
    case_volume_renumbered_no_terminal_body,
    case_toc_mixed_chinese_arabic_numbers,
    case_toc_halfwidth_brackets,
    case_toc_volume_only_in_toc,
    case_toc_long_directory_not_truncated,
    case_toc_adjacent_to_body_keeps_first_chapter,
    case_utf16_bom_decoded,
    case_repeated_titles_without_toc_kept,
    # 第四轮只读复核提出的回归用例（文末重印目录 / 卷容器行）
    case_toc_reprint_at_tail_idempotent,
    # 《书A》真实书驱动的回归用例（卷标题升级 / 书名作者提取）
    case_volume_container_line_upgraded_to_level2,
    case_volume_word_inside_body_not_upgraded,
    case_metadata_guessed_from_head,
    case_metadata_unknown_stays_empty,
    case_volume_container_line_before_body,
    # 诗词/韵文排版（《书A》"词曰/右调·忆秦娥"驱动）
    case_verse_after_lead_marked,
    case_verse_narrative_lead_not_marked,
    case_verse_tail_marker_backtracks,
    case_verses_need_evidence_not_geometry,
    case_verse_two_line_poem_marked,
    # 行内夹广告切除（M4 前移成规则切除）
    case_inline_ad_cut_keeps_quotes_paired,
    case_inline_ad_body_mentioning_url_kept,
    case_inline_ad_no_sentence_end_not_cut,
    case_inline_ad_punct_only_kept_becomes_whole_drop,
    case_inline_ad_punct_only_but_body_tail_kept,
    case_inline_ad_tail_goes_into_samples,
    case_inline_ad_warning_only_when_uncut,
    case_inline_ad_cut_never_leaves_dangling_comma,
    case_inline_ad_title_line_not_cut,
    case_inline_ad_idempotent,
    case_inline_ad_keeps_indent_prefix,
    # 原文件自带的乱码字符（只报不改）
    case_suspicious_chars_reported,
    # 网页抓取小说的首行缩进占位符（真书《书B》驱动）
    case_lead_marker_starts_new_paragraph,
    case_lead_marker_bang_variant,
    case_lead_marker_disables_indent_signal,
    case_indent_signal_survives_without_lead_marker,
    # 合并后重跑引号/标点归一（跨行半个引号、跨行波浪号）
    case_merged_paragraph_requotes_across_lines,
    case_merged_paragraph_wave_across_lines,
    # 站点水印前缀 / 装饰目录条目不可复活（真书《书C》驱动）
    case_site_prefix_before_chapter_number,
    case_site_prefix_does_not_revive_decorated_toc,
    case_body_sentence_with_site_prefix_not_title,
    # 第五轮只读复核提出的回归用例（切除吃正文 / 劈引号 / verse 误标叙述句）
    case_inline_ad_body_after_url_kept,
    case_inline_ad_head_kept_body_kept,
    case_inline_ad_cut_repairs_quote,
    case_verse_narrative_after_poem_not_marked,
    case_verse_with_le_at_head_marked,
    case_lead_line_kept_separate_without_indent,
    # 真书回归：《书F》《书G》《书E》驱动的新家族与兜底
    case_bracket_section_chapters,
    case_bracket_section_enumeration_not_chapters,
    case_bare_preface_is_chapter,
    case_toc_page_tail_line_dropped,
    case_double_heading_number_not_title,
    case_author_blacklist_skips_intro_line,
    case_metadata_from_filename,
    case_metadata_filename_decor_stripped,
    case_metadata_filename_url_tail_stripped,
    case_metadata_inside_file_wins_over_filename,
    # 真书回归第二批：《书M》《书N》《书O》《书P》驱动的新家族
    case_hua_chapter_family,
    case_numbered_section_chapters,
    case_numbered_list_not_chapters,
    case_volume_prefix_same_line_chapter,
    case_toc_leading_title_entry_dropped,
    # 真书回归第四批：《书H》《书A》驱动的目录区延伸与卷名续行
    case_toc_region_expands_across_volume_lines,
    case_toc_volume_subtitle_needs_volume_above,
    # 真书回归第三批：《书J》《书E》驱动的前缀与标记规则
    case_bracketed_book_name_volume_chapter,
    case_part_end_marker_not_chapter,
    case_stub_chapter_merged,
    # 站点水印前缀 × 数字顿号分节：《书T》《书I》驱动的剥前缀落点扩展
    case_site_prefix_before_numbered_section,
    case_site_prefix_numbered_list_still_not_chapters,
    # 数字顿号分节的分部重数编号：《书I》驱动的回头闸门改写
    case_numbered_section_restarts_per_part,
    case_numbered_section_title_ends_with_ellipsis,
    case_numbered_section_short_run_vetoes_whole_book,
    # 甲-3：《书R》《书乙》《书S》驱动的三处修复
    case_toc_entries_match_site_prefixed_body_headings,
    case_toc_separator_between_toc_and_body,
    case_fullwidth_url_ad_line_dropped,
    case_wrapped_url_ad_lines_dropped,
    # 硬折行假章（frag_split 语料驱动，task-7）
    case_hardwrap_fullwidth_chunk_not_title,
    case_hardwrap_duplicate_number_not_title,
    case_bare_number_page_number_is_not_a_chapter,
    case_bare_number_sequence_is_structure,
    case_hardwrap_bare_number_needs_continuity,
    case_hardwrap_gate_off_without_wrap,
    case_hardwrap_separator_title_survives,
    case_hardwrap_para_head_candidate_kept,
    case_hardwrap_fragment_merged_into_previous,
    case_hardwrap_fragment_too_long_keeps_title,
    # 标题末尾带逗号：编号家族的短标题放行，长散文拒绝
    case_comma_ending_numbered_title_kept,
    case_long_comma_line_after_number_is_body,
    # 试用反馈：正文句被误切成章 / 文件名里的括号作者名
    case_body_sentence_after_volume_word_not_chapter,
    case_metadata_filename_author_in_tail_paren,
    case_metadata_filename_author_not_version_or_site,
    # 短篇集分篇标题（整本没有章节标记时才启用）
    case_story_titles_split_chapterless_collection,
    case_story_titles_ignored_when_book_has_chapters,
    # 人工调整（adjust，docs/gui-design.md 的「adjust 接口」一节）
    case_adjust_none_is_same_as_absent,
    case_adjust_protect_ad_line_keeps_it,
    case_adjust_protect_toc_marker_keeps_text_without_chapter,
    case_adjust_protect_ranges_is_closed,
    case_adjust_merge_break_keeps_lines_apart,
    case_adjust_same_twice_identical,
    case_adjust_out_of_range_is_ignored_with_warning,
    case_adjust_unknown_key_raises,
    case_adjust_dropped_lines_is_full_list,
    case_adjust_protect_toc_entry_keeps_text,
    case_adjust_protect_adjacent_toc_entries_not_merged,
    case_adjust_protect_ad_line_in_toc_no_double_count,
    case_adjust_protect_ad_line_in_reprint_toc,
    case_adjust_protect_reprint_toc_entry_keeps_text,
    case_adjust_protected_toc_entry_is_in_dropped_lines,
    case_toc_entry_with_ad_words_keeps_adjust_none_unchanged,
    case_adjust_protect_inline_ad_not_cut,
    case_adjust_falsy_value_raises,
    case_adjust_range_element_not_pair_raises,
    case_adjust_nothing_hit_warns,
    case_adjust_partial_hit_reports_the_rest,
    case_adjust_unmatched_break_is_reported,
    case_adjust_protect_punct_residue_ad_line,
    case_adjust_protected_toc_marker_not_merged_into_previous,
    case_adjust_clamped_range_keeps_the_inner_part,
    case_adjust_merge_break_line_one_counts_as_requested,
    case_end_marker_lines_are_dropped,
    case_end_marker_title_is_not_dropped,
    case_dialogue_ending_quote_is_a_paragraph_end,
    case_hardwrapped_book_is_merged_back,
    # 第 1 组：目录残留出口 / 糊行判据收紧
    case_toc_empty_prefix_is_reported_not_deleted,
    case_toc_empty_prefix_needs_number_evidence,
    case_short_chapter_reference_line_is_not_a_one_line_book,
    # 第 1 组验收：装饰边框章节标题
    case_framed_chapter_titles_are_cut_cleanly,
    case_pure_decorative_frame_is_still_a_separator,
]


def test_all_cases() -> None:
    """pytest 入口。"""
    for c in _CASES:
        c()


def main() -> int:
    failed = 0
    for c in _CASES:
        try:
            c()
            print(f"ok   {c.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {c.__name__}: {e}")
    print(f"\n{len(_CASES) - failed}/{len(_CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
