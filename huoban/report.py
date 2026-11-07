"""把体检结果渲染成人类能核对的 markdown。"""

from __future__ import annotations


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(out)


def render_markdown(r: dict) -> str:
    p = r["paragraphs"]
    ch = r["chapters"]
    enc = r["encoding"]

    parts: list[str] = []
    parts.append(f"# 体检报告：{r['file']}\n")
    # 脱敏标记（--redact 时由 redact_diagnose 打上）：先告诉读者这份是
    # "可贴进 AI 会话"的变体，样例栏全是形状描述而不是被吞了内容。
    if r.get("redacted"):
        parts.append(
            "> **本报告已脱敏。** 标题候选、重复行、广告样例里的正文都换成了"
            "「长N 中N 西N 空N 符N #指纹」的形状描述；行号、次数、命中词与全部"
            "计数未动。核对原文请拿行号回本地文件用眼睛看。\n"
        )
    parts.append(
        _table(
            ["项目", "值"],
            [
                ["文件大小", f"{r['size_bytes'] / 1024:.1f} KB"],
                ["总行数", r["line_count"]],
                ["空行", r["blank_count"]],
                ["探测编码", f"{enc['chosen']}（置信度 {enc['confidence']}）"],
            ],
        )
    )

    parts.append("\n## 编码候选\n")
    parts.append(
        _table(
            ["编码", "得分", "汉字占比", "常用字占比", "替换字符", "严格解码"],
            [
                [c["encoding"], c["score"], c["cjk_ratio"], c["common_ratio"], c["replacement_ratio"], "是" if c["strict_ok"] else "否"]
                for c in enc["candidates"]
            ],
        )
    )

    parts.append("\n## 章节结构\n")
    num = ch["numbering"]
    parts.append(f"- 章节标题候选：**{ch['count']}** 行")
    if ch["families"]:
        parts.append("- 模式分布：" + "、".join(f"{k} {v}" for k, v in ch["families"].items()))
    if num.get("monotonic") is not None:
        parts.append(
            f"- 数字编号：{num['numeric_titles']} 个，递增 {num.get('increasing_pairs')}/{num.get('pairs')}，"
            f"重复号 {num.get('duplicate_numbers')}，范围 {num.get('range')}，"
            f"连续性判定：{'连续' if num['monotonic'] else '不连续'}"
        )
    if ch["samples"]:
        parts.append("\n前几个标题候选：\n")
        parts.append(_table(["行号", "内容"], [[n, t] for n, t in ch["samples"]]))

    parts.append("\n## 广告与重复行\n")
    reps = r["repeated_lines"]
    parts.append(f"- 整行重复 ≥3 次的条目：**{len(reps)}** 条")
    if reps:
        parts.append(_table(["次数", "行号", "内容"], [[x["count"], str(x["lines"]), x["text"]] for x in reps[:15]]))
    ads = r["ads"]
    parts.append(f"\n- 整行广告：**{ads['whole_line_count']}** 行")
    parts.append(f"- 行内夹广告：**{ads['inline_count']}** 行")
    if ads["whole_line_samples"]:
        parts.append("\n整行广告样例：\n")
        parts.append(_table(["行号", "命中词", "内容"], [[x["line"], ",".join(x["keywords"]), x["text"]] for x in ads["whole_line_samples"][:8]]))
    if ads["inline_samples"]:
        parts.append("\n行内夹广告样例（需行内切除）：\n")
        parts.append(_table(["行号", "命中词", "内容"], [[x["line"], ",".join(x["keywords"]), x["text"]] for x in ads["inline_samples"][:8]]))

    parts.append("\n## 段落形态\n")
    parts.append(
        _table(
            ["指标", "值"],
            [
                ["非空行", p.get("nonblank", 0)],
                ["平均行长", p.get("avg_len", 0)],
                ["最长行", p.get("max_len", 0)],
                ["碎裂短行", f"{p.get('short_break_count', 0)}（{p.get('short_break_ratio', 0):.0%}）"],
                ["超长行 >800 字", p.get("over_long_count", 0)],
                ["含零宽字符的行", p.get("zero_width_count", 0)],
                ["行尾有残留空白", p.get("trailing_space_count", 0)],
            ],
        )
    )

    parts.append("\n## 目录块与糊行检测\n")
    toc = r["toc"]
    single = r["single_line_book"]
    parts.append(
        _table(
            ["检测", "结果"],
            [
                ["开头疑似目录块", f"{'是' if toc['suspected'] else '否'}（扫描 {toc['scanned']} 行，命中 {toc['hits']} 行，最密集连续簇 {toc.get('cluster_len', 0)} 行 @ {toc.get('cluster_first_lines', [])[:5]}）"],
                ["疑似整本书糊成一行", f"{'是' if single['suspected'] else '否'}（超长行 {single['long_line_count']}，含多个章节标记的行 {single['lines_with_multiple_chapters']}）"],
            ],
        )
    )

    parts.append("\n## 建议动作\n")
    for i, act in enumerate(r["actions"], 1):
        parts.append(f"{i}. {act}")

    return "\n".join(parts) + "\n"
