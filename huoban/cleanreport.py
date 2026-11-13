"""把 clean.py 的清洗结果渲染成人类能核对的 markdown。

只渲染，不计算：本模块不读文件、不改数据、不打印任何东西。
输入是 clean.py 产出的 report dict（键名已冻结），输出 markdown 字符串。
"""

from __future__ import annotations

# 单元格里文本的展示上限（字符）。超长就截断，避免样例把报告撑爆。
_TEXT_LIMIT = 60
# 每类样例最多展示的条数。
_SAMPLE_LIMIT = 8


def _clip(s: str, limit: int = _TEXT_LIMIT) -> str:
    """超长文本截断，并注明原文总字数，方便知道被截掉了多少。"""
    if len(s) <= limit:
        return s
    return f"{s[:limit]}…（共 {len(s)} 字）"


def _cell(value: object) -> str:
    """单元格文本：压成单行、截断、转义竖线（不转义会把表格撑破）。"""
    s = str(value)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return _clip(s).replace("|", "\\|")


def _table(headers: list[str], rows: list[list[object]]) -> str:
    """渲染 markdown 表格，转义与对齐风格跟 report.py 的 _table 保持一致。"""
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(_cell(c) for c in row) + " |")
    return "\n".join(out)


def _kb(n: object) -> str:
    """字节数转成 KB 展示；拿不到数字就说不知道。"""
    try:
        return f"{float(n) / 1024:.1f} KB"
    except (TypeError, ValueError):
        return "未知"


def _num1(v: object) -> str:
    """浮点指标保留一位小数；非数字原样转字符串。"""
    if isinstance(v, (int, float)):
        return f"{v:.1f}"
    return str(v)


def _yes_no(flag: object) -> str:
    """布尔转"是/否"，给表格用。"""
    return "是" if flag else "否"


def _lines_brief(nums: list[int], limit: int = 5) -> str:
    """行号列表压成一行短串：前几个点出来，剩下的用总数交代。

    跟 clean.py 警告文案里的写法保持同一口味，人在这两处看到的是同一个东西。
    """
    head = "、".join(str(n) for n in nums[:limit])
    more = f" 等 {len(nums)} 个" if len(nums) > limit else ""
    return f"第 {head} 行{more}"


def _caption(name: str, total: int, shown: int) -> str:
    """样例桶的说明行：空桶、全展示、截断展示三种情况各自说清楚。

    total 与 shown 必须分开传：total 是**实际删掉/合并掉的条数**（来自
    dropped/merged 计数），shown 是**本次留下了几条样例**（被 _SAMPLE_LIMIT 截过）。
    以前只传样例数组长度，于是删掉 20 行广告的书会显示"共 8 条，全部展示"，
    报告看起来像在隐瞒 12 条。两个数字分开写，读者才能拿它去对账。
    """
    if total <= 0:
        return f"- {name}：无"
    if shown <= 0:
        # 确实发生了，但这次没留样例（比如目录标记行只计数不入样例桶）。
        return f"- {name}：共 {total} 条，本次未保留样例"
    if shown < total:
        return f"- {name}：共 {total} 条，此处展示前 {shown} 条"
    return f"- {name}：共 {total} 条，全部展示"


def render_clean_markdown(report: dict) -> str:
    """把清洗报告 dict 渲染成 markdown（纯函数：不读文件、不改 report、不打印）。"""
    ln = report.get("lines") or {}
    dropped = report.get("dropped") or {}
    merged = report.get("merged") or {}
    fixed = report.get("fixed") or {}
    samples = report.get("samples") or {}
    chapters = report.get("chapters") or {}
    stats = report.get("stats") or {}
    enc = report.get("encoding") or {}
    warnings = report.get("warnings") or []
    # 人工调整的账。样例区要用到（受保护未删的行必须和"已删除"分表显示），
    # 所以提到最前面读一次，后面的"人工调整"小节直接复用。
    adjust = report.get("adjust") or {}
    protected = adjust.get("protected") or {}

    # 真实条数取自删除/合并统计，样例条数另算：两者相等才说"全部展示"。
    ad_cnt = int(dropped.get("ad") or 0)
    toc_cnt = int(dropped.get("toc") or 0)
    merged_cnt = int(merged.get("groups") or 0)
    # 渲染时还会再按 _SAMPLE_LIMIT 切片，所以"展示几条"要按切片后的算。
    # 广告/目录/行内三桶在样例区还要按"删掉的 / 人工保护留下的"分开数，
    # 所以这三桶的 shown 数不在这里算（见样例区 _split_samples）。
    merged_shown = min(len(samples.get("merged") or []), _SAMPLE_LIMIT)
    # 行内广告：条数记在 fixed 里（它是"改写"不是"删除"），样例另存一桶。
    # 两个数不同源，所以不能拿 len(样例) 当条数（样例会被截断到 8 条）。
    inline_cnt = int(fixed.get("inline_ad_cut_lines") or 0)
    # 合计只算真删掉的内容：整行广告、目录行、连载结束标记、段首缩进占位符。
    # "压掉的空行"是排版用的，不算删内容，所以在表里单列一行、明确不计入合计；
    # 以前它被算进合计，合计比各行含义对不上，读者会以为正文被删了。
    lead_cnt = int(fixed.get("lead_marker_lines") or 0)
    dropped_total = (
        int(dropped.get("ad") or 0)
        + int(dropped.get("toc") or 0)
        + int(dropped.get("marker") or 0)
        + lead_cnt
    )

    parts: list[str] = []
    parts.append(f"# 清洗报告：{report.get('file', '（未知文件）')}\n")
    # 脱敏标记：这份报告是"能整份贴进 AI 会话"的变体，人读时要先知道
    # 样例栏里为什么全是形状描述，否则会以为程序吞了内容。
    # 脱敏本身在 huoban/redact.py 做完，这里只负责如实显示数据的样子。
    if report.get("redacted"):
        parts.append(
            "> **本报告已脱敏。** 样例正文、书名、路径、警告里的引文都换成了"
            "「长N 中N 西N 空N 符N #指纹」的形状描述。行号、命中词、所有计数"
            "未动。要核对原文，拿行号回本地文件用眼睛看 —— 人看原文，AI 不看。\n"
        )
    # 输入只读，一个字节都不写回。这句要显眼，放在最前面。
    parts.append(
        "> **原始文件未改动。** 输入文件全程只读，所有结果写进独立的输出目录，"
        "本报告只是供人逐项核对的账单。\n"
    )

    parts.append("## 概览\n")
    verse = report.get("verse") or {}
    parts.append(
        _table(
            ["项目", "值"],
            [
                ["输入文件", report.get("file", "（未知文件）")],
                ["文件大小", _kb(report.get("size_bytes"))],
                ["探测编码", f"{enc.get('chosen', '未知')}（置信度 {enc.get('confidence', '未知')}）"],
                ["总行数", ln.get("total", 0)],
                ["章节数", chapters.get("count", stats.get("chapters", 0))],
                ["段落数", stats.get("paragraphs", 0)],
                # 诗词不改字，只换排版（居中、楷体、不缩进），所以不进"修复"表。
                # 列在这里是为了让"某章看起来怪"时能先排除"被标成诗词了"。
                ["诗词/韵文段落", verse.get("lines", 0)],
                ["正文字数", stats.get("chars", 0)],
                ["平均每章字数", f"{_num1(stats.get('avg_chars_per_chapter', 0))} 字"],
                ["最长章节标题", stats.get("longest_title") or "（无）"],
            ],
        )
    )

    parts.append("\n## 行数\n")
    parts.append(
        _table(
            ["项目", "行数"],
            [
                ["总行数（输入）", ln.get("total", 0)],
                ["非空行", ln.get("nonblank", 0)],
                ["输入空行", ln.get("blank_in", 0)],
                ["输出空行（章节内保留）", ln.get("blank_out", 0)],
            ],
        )
    )
    # "输出空行"只数章节树里留下来的空行段；book.txt 里每个章节标题前后还会
    # 补空行（排版用，由 render 层决定），所以打开输出文件数空行会更多。
    # 这两个数不是一回事，写清楚，别让人拿文件行数来对账。
    parts.append(
        "\n> 输出空行只统计章节树里保留的空行段；"
        "book.txt 里每个章节标题前后还会补空行（排版用），文件里的空行会更多。\n"
    )

    # "被压掉的空行"= 输入空行 - 输出空行。它包含三类：连续空行里多余的那几条，
    # 章节标题两侧的分隔空行（这两类不算删掉内容，排版时 render 层会重新按
    # "标题前后各一空行"补回来），以及正文段与段之间的单个空行（0.2.2 起从源头
    # 折叠：它原本只是排版空行，章节树里不再保留 blank 段，渲染出来也就不会变成
    # 一段可见间距）。所以标签不能写"多余"，更不能让人以为删掉了正文。
    parts.append("\n## 删除（统计，输入文件本身未被改动）\n")
    parts.append(
        _table(
            ["类别", "行数"],
            [
                ["整行广告", dropped.get("ad", 0)],
                ["目录行", dropped.get("toc", 0)],
                # "本章完""未完待续"这类连载标记：连载时给读者看的，
                # 做成书之后没有意义，整行删。
                ["章节结束标记", dropped.get("marker", 0)],
                # 段首缩进占位符（???? / !!!!）：网页抓取小说靠它标首行缩进，
                # 原字符不是给读者看的，所以算"删掉的内容"。放在删除表而不是
                # 修复表：修复表每一行含义是"改了多少字"，这里一个字都没改。
                ["段首缩进占位符", lead_cnt],
                # 空行只是排版用的，不计入下面的合计（口径见上面的注释）。
                ["压掉的空行（排版用，不计入合计）", dropped.get("extra_blank", 0)],
                ["合计", dropped_total],
            ],
        )
    )

    parts.append("\n## 碎行合并\n")
    parts.append(
        _table(
            ["项目", "数量"],
            [
                ["合并组数", merged.get("groups", 0)],
                ["被吸收的碎行", merged.get("absorbed_lines", 0)],
            ],
        )
    )

    parts.append("\n## 修复\n")
    # 硬折行假标题那两行只在真的出现时才显示：99% 的书没被按字数硬切过，
    # 每份报告都挂两行 0 只是噪声。
    fix_rows = [
        ["含零宽字符的行", fixed.get("zero_width_lines", 0)],
        ["行尾残留空白的行", fixed.get("trailing_space_lines", 0)],
        ["缩进被归一的段落", fixed.get("indent_normalized_lines", 0)],
        ["标点被归一的行", fixed.get("punctuation_lines", 0)],
        ["连续空格被折叠的行", fixed.get("space_collapsed_lines", 0)],
        ["行内夹广告被切除的行", fixed.get("inline_ad_cut_lines", 0)],
        ["实际发生改动的行", fixed.get("changed_lines", 0)],
    ]
    wrap_reject = int(fixed.get("wrap_chunk_reject_lines") or 0)
    wrap_suspect = int(fixed.get("wrap_chunk_suspect_lines") or 0)
    if wrap_reject:
        fix_rows.append(["硬折行切出来的假标题（已并回上一段）", wrap_reject])
    if wrap_suspect:
        fix_rows.append(["疑似硬折行假标题（证据不足，原样保留）", wrap_suspect])
    parts.append(_table(["项目", "行数"], fix_rows))

    # 人工调整只在真用了才显示：默认跑一遍没有 adjust，报告要和以前一字不差。
    # "真用了"包含三种：保护住了行、断开了合并、**给了行号却没命中**。
    # 第三种最需要写进报告 —— 用户以为保住了一行，实际什么都没发生，
    # 只有这里点名他才看得见（警告区也有一句，但人核对时会盯着这张表）。
    protected_lines = int(protected.get("lines") or 0)
    protected_inline = int(protected.get("inline") or 0)
    breaks_hit = int(adjust.get("merge_breaks_hit") or 0)
    unmatched = adjust.get("unmatched") or {}
    miss_protect = [int(x) for x in (unmatched.get("protect_lines") or [])]
    miss_breaks = [int(x) for x in (unmatched.get("merge_breaks") or [])]
    if protected_lines or protected_inline or breaks_hit or miss_protect or miss_breaks:
        parts.append("\n## 人工调整\n")
        rows: list[list[object]] = []
        if protected_lines:
            rows.append(["人工保护未删的行", protected_lines])
            rows.append(["其中：整行广告", int(protected.get("ad") or 0)])
            rows.append(["其中：目录块", int(protected.get("toc") or 0)])
        if protected_inline:
            # 行内切除不删行，所以它不算进"保护未删的行"；但用户确实说了
            # "这行别动"，尾巴还留在书里，必须如实报一句。
            rows.append(["行内广告（受保护未切）", protected_inline])
        if breaks_hit:
            rows.append(["强制断开的碎行合并", breaks_hit])
        if miss_protect:
            rows.append(["给了行号但没东西可保护", _lines_brief(miss_protect)])
        if miss_breaks:
            rows.append(["给了行号但那里本来就不合并", _lines_brief(miss_breaks)])
        parts.append(_table(["项目", "数量"], rows))
        # 行号口径必须写清楚：它指的是清洗前的原文行号，不是输出文件的行号。
        # 说反了，用户会拿着报告去输出文件里找第 1024 行，永远找不到。
        parts.append(
            "\n> 这些是人工在复核界面勾过「别动」的：保护的行没有删，"
            "指定断开的合并没有并。**行号指清洗前的原文行号**，"
            "拿它回原文件核对，不要拿输出文件的行号对。\n"
            "> 保护的口径只管**文字不删**，不管排版不变：这一行的缩进、"
            "是否被标成韵文，仍按正常规则走。\n"
        )

    parts.append("\n## 章节与前后附文\n")
    ch_lines = [
        f"- 章节标题：**{chapters.get('count', 0)}** 个",
        f"- 第一章之前的段落：{chapters.get('front_paragraphs', 0)}",
        f"- 最后一章之后的段落：{chapters.get('tail_paragraphs', 0)}",
    ]
    kept = chapters.get("kept_last_toc_line", None)
    # 只有拿到行号才显示这行；None 表示 clean 没给出这个信息。
    if kept is not None:
        ch_lines.append(f"- 保留的最后一行目录：{kept}")
    parts.append("\n".join(ch_lines))

    parts.append("\n## 样例")

    # 样例里混着"删掉的"和"人工保护留下的"两类。必须分表：混在一张
    # "（已删除）"表里，caption 的"共 N 条"就和表格行数自相矛盾 ——
    # 用户会以为自己保护的那行其实被删了。
    def _split_samples(bucket: str) -> tuple[list[dict], list[dict]]:
        rows_all = [x for x in (samples.get(bucket) or []) if isinstance(x, dict)]
        gone = [x for x in rows_all if not x.get("protected")]
        kept = [x for x in rows_all if x.get("protected")]
        return gone, kept

    ad_gone, ad_kept = _split_samples("ad")
    parts.append("\n### 整行广告（已删除）\n")
    parts.append(_caption("广告样例", ad_cnt, min(len(ad_gone), _SAMPLE_LIMIT)))
    ads = ad_gone[:_SAMPLE_LIMIT]
    if ads:
        parts.append("")
        parts.append(
            _table(
                ["行号", "命中词", "含链接", "内容"],
                [
                    [
                        x.get("line", ""),
                        "、".join(x.get("keywords") or []) or "（无）",
                        _yes_no(x.get("has_url")),
                        x.get("text", ""),
                    ]
                    for x in ads
                ],
            )
        )
    if ad_kept:
        prot_ad = int(protected.get("ad") or 0)
        parts.append("\n### 整行广告（人工保护未删）\n")
        parts.append(
            _caption("人工保护未删的整行广告", prot_ad, min(len(ad_kept), _SAMPLE_LIMIT))
        )
        parts.append("")
        parts.append(
            _table(
                ["行号", "命中词", "含链接", "内容"],
                [
                    [
                        x.get("line", ""),
                        "、".join(x.get("keywords") or []) or "（无）",
                        _yes_no(x.get("has_url")),
                        x.get("text", ""),
                    ]
                    for x in ad_kept[:_SAMPLE_LIMIT]
                ],
            )
        )

    toc_gone, toc_kept = _split_samples("toc")
    parts.append("\n### 目录行（已删除）\n")
    parts.append(_caption("目录样例", toc_cnt, min(len(toc_gone), _SAMPLE_LIMIT)))
    tocs = toc_gone[:_SAMPLE_LIMIT]
    if tocs:
        parts.append("")
        parts.append(_table(["行号", "内容"], [[x.get("line", ""), x.get("text", "")] for x in tocs]))
    if toc_kept:
        prot_toc = int(protected.get("toc") or 0)
        parts.append("\n### 目录行（人工保护未删）\n")
        parts.append(
            _caption("人工保护未删的目录行", prot_toc, min(len(toc_kept), _SAMPLE_LIMIT))
        )
        parts.append("")
        parts.append(
            _table(
                ["行号", "内容"],
                [[x.get("line", ""), x.get("text", "")] for x in toc_kept[:_SAMPLE_LIMIT]],
            )
        )

    parts.append("\n### 行内广告（已切掉尾巴）\n")
    inline_gone, inline_kept = _split_samples("inline_ad")
    parts.append(_caption("行内广告样例", inline_cnt, min(len(inline_gone), _SAMPLE_LIMIT)))
    inls = inline_gone[:_SAMPLE_LIMIT]
    if inls:
        parts.append("")
        parts.append(
            _table(
                ["行号", "切除的部分", "保留的正文"],
                [
                    [
                        x.get("line", ""),
                        x.get("cut", ""),
                        # 用清洗层存下的 kept，不要拿"原行减切除部分"去算：
                        # 两个字段都截断过，相减会剩下半截广告残渣。
                        x.get("kept", ""),
                    ]
                    for x in inls
                ],
            )
        )
    if inline_kept:
        parts.append("\n### 行内广告（人工保护未切）\n")
        parts.append(
            _caption(
                "人工保护未切的行内广告", protected_inline, min(len(inline_kept), _SAMPLE_LIMIT)
            )
        )
        parts.append("")
        parts.append(
            _table(
                ["行号", "本该切掉的部分", "保留的整行"],
                [
                    [
                        x.get("line", ""),
                        x.get("cut", ""),
                        x.get("kept", ""),
                    ]
                    for x in inline_kept[:_SAMPLE_LIMIT]
                ],
            )
        )

    parts.append("\n### 碎行合并\n")
    parts.append(_caption("合并样例", merged_cnt, merged_shown))
    mges = list(samples.get("merged") or [])[:_SAMPLE_LIMIT]
    if mges:
        parts.append("")
        parts.append(
            _table(
                ["行号", "合并结果"],
                [
                    ["、".join(str(n) for n in (x.get("lines") or [])), x.get("result", "")]
                    for x in mges
                ],
            )
        )

    parts.append("\n## 警告\n")
    # 这一节是人工核对的重点，空也要在，不能让小节凭空消失。
    if warnings:
        for w in warnings:
            parts.append(f"- {w}")
    else:
        parts.append("无（没有发现需要人工确认的问题）")

    parts.append("\n## 下一步\n")
    parts.append(
        "\n".join(
            [
                "1. 先核对上面的删除与修复统计：数字对得上，说明清洗动作符合预期。",
                "2. 打开中间态 `book.json`（章节树 JSON）检查章节标题、顺序与前后附文——"
                "它是清洗与打包的分界。",
                "3. 章节树确认无误后，再渲染成干净 TXT 或 EPUB。",
                "4. 回滚：不需要回滚——**原始文件未被修改**，所有产物都在独立输出目录里；"
                "不满意就重跑，不要去改输入文件。",
            ]
        )
    )

    return "\n".join(parts) + "\n"
