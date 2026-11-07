"""影子语料：把真书换成"结构相同的孪生本"，让 AI 能读语料而读不到原文。

为什么需要它（本项目实测）：批量报告和日志里全是书名，样例正文会进清洗报告，
这些一旦贴进 AI 会话就撞上提供方的入口内容审查（406 data_inspection_failed），
且审查在模型运行之前，整条历史每次重放每次被拒 —— 会话救不回来。
但调试又确实需要真书的**形态**：碎行分布、目录块密度、糊行、重复行。
两头兼顾的办法就是：形态留下，字换掉。

三个子命令：

    python tools/mask.py map --root <小说目录> --out backups/mask/map.json
        只读文件名，生命名别名表（B01、B02…）。不读任何正文。
    python tools/mask.py twin 书.txt -o 孪生本.txt
        逐字替换正文，保留结构与结构标记。
    python tools/mask.py check 书.txt
        真本 vs 孪生本跑同一套清洗，逐字段比计数，报"哪里不同构"。

保结构的具体含义（孪生本与原书逐行对齐）：
- 行数、每行字符数、空行位置、行首缩进、标点与符号位置全部不变；
  所以体检与清洗看到的行长分布、目录密度、糊行判定都不会变。
- 只有"汉字"被换成同类填充字。西文字母、数字、标点一个都不动 ——
  动它们等于凭空造出 "www." / ".com" / "1234.com" 这类假广告，
  同构性立刻作废（这是实测踩出来的取舍，不是保守）。
- 同一行永远得到同一个孪生行，原书里的重复行在孪生本里照样重复。
- 结构标记原样保留：章节/分节标题、URL、我们词表里的广告关键词。
  这些是规则的触发物，抹掉它们孪生本就不再是同构样本，而是另一本书了。

诚实的边界：clean.py 的判定规则里，凡是只看"长度/空白/标点/符号/数量"的，
孪生本必然同构；凡是看"具体是哪个字"的（少数特例词），孪生本会漏触发。
check 子命令就是把漏的地方量出来 —— 不同构的字段就是这份影子语料的失真度，
用它决定能不能拿孪生本替代真本下结论。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.clean import clean_text  # noqa: E402
from huoban.diagnose import AD_KEYWORDS  # noqa: E402  我们自己的词表，非书内原文
from huoban.encoding import decode  # noqa: E402
from huoban.redact import (  # noqa: E402
    alias_for,
    alias_table,
    load_name_map,
    shape,
    _lookup_alias,
)


def _default_root() -> Path:
    """真书根目录：和 batch_check 用同一个，避免两处各写一遍路径。"""
    sys.path.insert(0, str(ROOT / "tools"))
    import batch_check  # noqa: PLC0415

    return Path(batch_check.DEFAULT_ROOT)

# 填充字表：常用汉字，刻意避开一切会被规则当作标记的字。
# 用固定表而不是随机汉字：随机可能碰上「第」「章」这类字，
# 孪生本里凭空多出章节标题，同构性当场作废。混进禁用字由 _filler_ok() 拦
# （实测第一版就混进了 明/月/窗/花 四个字 —— 分别来自 声明/求月票/加入书签/求鲜花）。
_FILLER = ("木山水石田土云雨风光星河海湖桥路园墙门帘盘碗筷"
           "鱼肉蛋盐糖茶饼瓜果梨桃李杏枣豆角莓芝兰竹松柏草芽根茎皮")

# 章节/分节/前后附文标记里的汉字 —— 一律不许出现在替换结果里。
_MARK_CHARS = "第章节卷部集回篇折幕册话序楔引终大结局番外传附前后记言介说声明内容目录"
# 数字（含中文数字）也是标记的一部分：正文里被替换出来的「一十百千万」
# 有机会凑出「第十二章」这种假标题，所以同样禁掉。
_MARK_NUMS = "零〇一二三四五六七八九十百千万两廿卅1234567890"

# 结构锚词：clean.py / diagnose.py 的**自有词表**里的结构词，不是书里的原文。
# 为什么要单独保留：「目录」「前言」这类行是前后附文与目录区的锚点，
# 一旦被替换，目录区就检测不出来，dropped.toc 从 7 变 6 —— 孪生本不再同构。
# 这些词是我们代码里的常量，保留它们不泄露任何一本书的内容。
#
# 与 diagnose.CHAPTER_PATTERNS 的「卷首特殊章」家族逐字对齐（diagnose.py:30-34）：
# 少一个词，某一本真书的对应章节就会在孪生本里消失（实测《后 记》这种写法
# 就是"词被全角空格拆开"造成的丢失，见 _SPACED_STRUCT_RE）。
_STRUCT_WORDS = (
    # —— 卷首特殊章（前缀匹配那一族，diagnose.py:30）——
    "序章", "序言", "楔子", "引子", "前言", "引言", "后记", "尾声", "终章",
    "大结局", "番外", "外传", "附录", "目录",
    # —— 卷首特殊章（整行匹配那一族，diagnose.py:34）——
    "自序", "小序",
    # —— 正文/收尾/版权页里的结构行 ——
    "正文", "全书完", "本书完", "已完结", "完结", "简介", "内容介绍",
    "作者", "书名",
)
_STRUCT_RE = re.compile("|".join(re.escape(w) for w in _STRUCT_WORDS))

# 「后 记」「序 言」这类被空白/全角空格拆开的结构词：真书里常见（B05 实测
# 「后\u3000记」是全书最后一章的标题）。clean 的 normalize_line 会把中文之间的
# 空白删掉，所以**真本里它们是标题，孪生本里两个字一旦被替换就什么都不是**，
# 章节数当场 41→40。因此结构词要按"字 + 可选空白 + 字"容忍匹配。
# 空白不算"改内容"：它本来就留在那一行里，保留的是字不是排版。
_WS_IN_STRUCT = r"[ \t\u3000\u00a0]*"
# 「序 / 自序 / 小序」是整行匹配家族（diagnose.py:34）：整行只许有这一个词
# 加首尾空白。真书里写成「序\u3000」「自 序」也一样是标题，字必须保住。
_SPACED_BARE_ORDER_RE = re.compile(
    r"^[ \t\u3000\u00a0]*"
    + _WS_IN_STRUCT.join("自") + "序[ \t\u3000\u00a0]*$"
    r"|^[ \t\u3000\u00a0]*序[ \t\u3000\u00a0]*$"
)
_SPACED_STRUCT_RE = re.compile(
    "|".join(_WS_IN_STRUCT.join(re.escape(c) for c in w) for w in _STRUCT_WORDS)
)

# 结构标记：孪生本里原样保留（不替换）的片段。
_MARKERS = (
    # 第X章/卷/节/部/集/回/篇（含中文数字）
    re.compile(r"第\s*[" + _MARK_NUMS + r"]+\s*[章节卷部集回篇折幕]"),
    # 数字/中文数字 + 顿号或点：目录分节行「1、…」「三、…」
    re.compile(r"^\s*[" + _MARK_NUMS + r"]+[、.．,，:：]"),
    # 括号式分节标题：整行「（12）」「【三】」这类
    re.compile(r"^[^\w]{0,2}[（(【\[]\s*[" + _MARK_NUMS + r"]{1,8}\s*[）)\]】]"),
    # URL / 站名 —— 广告行的判据，必须留着
    re.compile(r"(?:https?://|www\.|m\.|wap\.)[A-Za-z0-9#./_~:@%+-]+"),
    re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\."
               r"(?:com|net|org|cc|info|la|tw|vn|xyz|top|vip|club)(?:\b|$)",
               re.IGNORECASE),
)
# 广告关键词：词表是 diagnose 里的（我们写的），命中即保留 —— 规则要看它才判得出广告行。
_AD_ALT = "|".join(re.escape(k) for k in sorted(AD_KEYWORDS, key=len, reverse=True) if k)
_AD_RE = re.compile(_AD_ALT) if _AD_ALT else None


def _forbidden() -> set[str]:
    """替换结果里不许出现的字：结构标记用字 + 广告词表用字 + 结构锚词用字。

    这是**第一道闸**，闭集、便宜、能挡住绝大多数（第/章/月/票/本/站/请/问 …）。
    但它不可能穷尽 —— 两个合法的填充字连起来仍有机会拼出某个词。
    所以真正的判据是 `fake_markers()`：直接拿规则去扫孪生本，
    凡是命中且不来自原书本来的命中，就是替换造出来的假标记。
    """
    bad = set(_MARK_CHARS) | set(_MARK_NUMS)
    for kw in AD_KEYWORDS:
        bad |= set(kw)
    for w in _STRUCT_WORDS:
        bad |= set(w)
    return bad


def fake_markers(orig: str, twin: str) -> list[str]:
    """孪生行里"由替换凭空造出来的假标记"，正常应为空列表。

    判据：孪生行上的每个标记命中，必须被**原行**同一位置的命中标记覆盖。
    覆盖不到，就说明这几个字是新拼出来的（例如正文被换成「请记住本站」）。
    长度不变、原标记位置不动，所以按位置比是可靠的。

    为什么靠"事后扫"而不是"事前禁字"：事前禁字禁不完（两个合法填充字连起来
    仍可能拼出某个词），而且禁过头会把常用字全禁掉，填充表直接没字可用。
    真判据是拿同一套规则去扫孪生本 —— 规则怎么判，我们就怎么验。
    """
    if orig == twin:
        return []
    src = _marker_spans(orig)
    out: list[str] = []
    for s, e in _marker_spans(twin):
        covered = any(os_ <= s and e <= oe for os_, oe in src)
        if not covered:
            out.append(twin[s:e])
    return out


def _filler_ok() -> None:
    """填充表自检：混进禁用字就拒绝启动，而不是跑完才发现孪生本是假的。"""
    bad = sorted(set(_FILLER) & _forbidden())
    if bad:
        raise SystemExit(f"填充字表混进结构/广告标记用字 {len(bad)} 个：{''.join(bad[:10])}")


def _seed(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8], 16)


def _marker_spans(line: str) -> list[tuple[int, int]]:
    """这一行里要原样保留的片段（结构标记 + 命中的广告词），按位置合并。"""
    spans: list[tuple[int, int]] = []
    for pat in _MARKERS:
        for m in pat.finditer(line):
            spans.append(m.span())
    if _AD_RE:
        for m in _AD_RE.finditer(line):
            spans.append(m.span())
    for m in _STRUCT_RE.finditer(line):
        spans.append(m.span())
    for m in _SPACED_STRUCT_RE.finditer(line):
        spans.append(m.span())
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for s, e in spans[1:]:
        ps, pe = merged[-1]
        if s <= pe:                      # 重叠或相接：并成一段
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF


def _bare_order_line(line: str) -> bool:
    """整行就是「序 / 自序 / 小序」这类家族标题（可带空白）。

    diagnose.py:34 用 `^\\s*(?:序|自序|小序)\\s*$` 整行匹配，所以这一行必须
    一个字都不换 —— 换成任何字，这一章在孪生本里就没了。
    """
    return _SPACED_BARE_ORDER_RE.match(line) is not None


def _mask_line_once(line: str, salt: int) -> str:
    """按 salt 取填充字。salt 只用来在同字同位置换不出好结果时改一个候选。"""
    out = list(line)
    if _bare_order_line(line):
        return line                     # 整行家族标题，一个字都不动
    protected = [False] * len(line)
    for s, e in _marker_spans(line):
        for i in range(s, min(e, len(line))):
            protected[i] = True
    for i, ch in enumerate(out):
        if protected[i] or not _is_cjk(ch):
            continue
        # 摘要决定取表里哪个字：同一行同样位置的同一个字永远换成同一个填充字，
        # 所以重复行在孪生本里仍然是重复行。
        out[i] = _FILLER[(_seed(ch + str(i) + str(salt)) >> (i % 7)) % len(_FILLER)]
    return "".join(out)


_MASK_TRIES = 6


def mask_line(line: str) -> str:
    """一行的孪生文本：长度不变，标记原样，汉字换成填充字。

    只动汉字。西文字母和数字动不得 —— 替换会拼出假域名/假广告词，
    而且它们本身通常就是标记的一部分。标点、空白、零宽全部原样。

    换完必须验一遍（fake_markers）：填充字有机会拼出规则认识、原文却不认识的
    词。拼出来就换一个候选重取，最多 _MASK_TRIES 次；仍拼出来就交给调用方判死。
    """
    if not line:
        return line
    for salt in range(_MASK_TRIES):
        cand = _mask_line_once(line, salt)
        if not fake_markers(line, cand):
            return cand
    return _mask_line_once(line, _MASK_TRIES - 1)


def fake_scan(orig: str, twin: str) -> list[tuple[int, str]]:
    """整本孪生里的假标记清单 [(行号, 拼出来的串)]，空列表才算干净。

    逐行按位置比：mask_text 保证行数与每行长度不变，所以第 i 行对第 i 行。
    """
    a = re.split(r"(\r\n|\r|\n)", orig)[0::2]
    b = re.split(r"(\r\n|\r|\n)", twin)[0::2]
    if len(a) != len(b):
        return [(0, "行数不一致")]
    bad: list[tuple[int, str]] = []
    for i, (ln, tl) in enumerate(zip(a, b), start=1):
        for hit in fake_markers(ln, tl):
            bad.append((i, hit))
    return bad


def mask_text(text: str) -> str:
    """整本孪生：逐行处理，行分隔符原样保留（\r\n 的书不该变成 \n 的书）。"""
    parts = re.split(r"(\r\n|\r|\n)", text)
    # re.split 带捕获组：偶数位是行内容，奇数位是分隔符
    for i in range(0, len(parts), 2):
        if parts[i]:
            parts[i] = mask_line(parts[i])
    return "".join(parts)


def _report_counts(rep: dict) -> dict[str, object]:
    """从清洗报告里挑出"该同构"的数字字段，扁平化成 {点分路径: 值}。

    刻意只取计数，文本键（text/cut/kept/result/file/warnings/题名/作者/命中词）
    一律不进 —— 这份东西是要打到终端、可能贴进会话的，只能是元数据。
    """
    skip = {"text", "cut", "kept", "result", "file", "warnings",
            "longest_title", "title", "author", "keywords", "encoding"}
    flat: dict[str, object] = {}

    def walk(node: object, prefix: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in skip:
                    continue
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(node, list):
            flat[f"{prefix}.len"] = len(node)
            for i, v in enumerate(node):
                walk(v, f"{prefix}[{i}]")
        elif isinstance(node, bool):
            flat[prefix] = int(node)
        elif isinstance(node, (int, float)):
            flat[prefix] = node

    walk(rep, "")
    return flat


def _title_shapes(book) -> list[str]:
    """章节标题的形状序列（长度 + 字符类别），用来比"切出来的章节是不是同一个"。

    不用标题本身：标题就是原文。形状能区分「同一本书」和「切歪了的另一本书」。
    """
    out = []
    for ch in book.chapters:
        t = ch.title or ""
        cjk = sum(1 for c in t if _is_cjk(c))
        dig = sum(1 for c in t if c.isdigit())
        out.append(f"{len(t)}/{cjk}/{dig}")
    return out


def cmd_map(argv: list[str]) -> int:
    """只读文件名，生命名别名表。正文一个字都不碰。"""
    ap = argparse.ArgumentParser(prog="mask.py map", description="给一批 TXT 的生命名别名表")
    ap.add_argument("--root", required=True, help="扫描目录")
    ap.add_argument("--out", required=True, help="别名表 JSON 写到哪")
    ap.add_argument("--min-bytes", type=int, default=0, help="忽略小于此字节数的文件")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2
    files = sorted(p for p in root.rglob("*.txt")
                   if p.is_file() and p.stat().st_size >= args.min_bytes)
    if not files:
        print("没有 .txt 文件", file=sys.stderr)
        return 2
    mapping = alias_table([p.name for p in files], prefix="B")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"别名表 {len(mapping)} 条 → {out}", file=sys.stderr)
    return 0


def cmd_twin(argv: list[str]) -> int:
    """产出一本孪生本。输入只读（铁律 1）。"""
    ap = argparse.ArgumentParser(prog="mask.py twin", description="生成结构同构的影子语料")
    ap.add_argument("path", help="原书 TXT（只读）")
    ap.add_argument("-o", "--out", required=True, help="孪生本输出路径")
    ap.add_argument("--encoding", default="", help="指定原书编码；默认自动探测")
    args = ap.parse_args(argv)

    src = Path(args.path)
    if not src.is_file():
        print(f"找不到文件：{src}", file=sys.stderr)
        return 2
    out = Path(args.out)
    if out.resolve() == src.resolve():
        print("输出路径与输入文件相同，拒绝写入", file=sys.stderr)
        return 2

    text = decode(src.read_bytes(), args.encoding or None)
    masked = mask_text(text)
    fake = fake_scan(text, masked)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 必须按字节写、禁掉文本模式的换行转换：Windows 下 write_text 会把原书的
    # CRLF 写坏成 CRCRLF（实测原书 3229 个 CRLF 全丢、凭空多出 3067 个空行，
    # 段落数从 3104 翻倍到 6172）。孪生本要逐字节同构，newline="" 是硬要求。
    with out.open("w", encoding="utf-8", newline="") as fh:
        fh.write(masked)
    print(f"原 {len(text)} 字 / 孪生 {len(masked)} 字 / 假标记 {len(fake)} 处 → {out}",
          file=sys.stderr)
    if len(masked) != len(text):
        print("长度不一致：掩码过程改了字符数，同构性不成立，别用这份", file=sys.stderr)
        return 1
    for no, hit in fake[:20]:
        print(f"  FAKE line{no} <{hit}>", file=sys.stderr)
    if fake:
        print("上面这些是替换拼出来的假标记：孪生本会比原书"
              "多出章节或广告行，先改填充字表再用", file=sys.stderr)
        return 1
    return 0


def cmd_check(argv: list[str]) -> int:
    """真本 vs 孪生本：同一套清洗跑两遍，逐字段比计数。

    输出只用字段名 + 数字，整份可以直接贴进 AI 会话。
    退出码 0 = 完全同构；1 = 有失真字段（就是影子语料的边界）。
    """
    ap = argparse.ArgumentParser(prog="mask.py check", description="同构性校验")
    ap.add_argument("path", help="原书 TXT（只读）")
    ap.add_argument("--tolerance", type=int, default=0,
                    help="允许的绝对偏差；超出才记为失真")
    ap.add_argument("--verbose", action="store_true", help="列出全部字段名（默认只列失真的）")
    args = ap.parse_args(argv)

    src = Path(args.path)
    if not src.is_file():
        print(f"找不到文件：{src}", file=sys.stderr)
        return 2

    text = decode(src.read_bytes())
    real = clean_text(text, source="real")
    twin = clean_text(mask_text(text), source="twin")

    a, b = _report_counts(real.report), _report_counts(twin.report)
    keys = sorted(set(a) | set(b))
    same, drift, only_one = 0, 0, 0
    worst: list[tuple[str, object, object, float]] = []
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None:
            only_one += 1
            worst.append((k, va, vb, float("inf")))
            continue
        if va == vb:
            same += 1
            continue
        try:
            delta = abs(float(va) - float(vb))
        except (TypeError, ValueError):
            delta = float("inf")
        if delta > args.tolerance:
            drift += 1
            worst.append((k, va, vb, delta))
        else:
            same += 1

    ta, tb = _title_shapes(real.book), _title_shapes(twin.book)
    title_same = sum(1 for x, y in zip(ta, tb) if x == y)

    print(f"source={shape(str(src))}")
    print(f"fields_compared={len(keys)} identical={same} drift={drift} only_one_side={only_one}")
    print(f"titles real={len(ta)} twin={len(tb)} identical_shape={title_same}")
    if args.verbose:
        for k in keys:
            print(f"  {k}: real={a.get(k)} twin={b.get(k)}")
    for k, va, vb, delta in worst[:25]:
        print(f"  DRIFT {k}: real={va} twin={vb} delta={delta if delta != float('inf') else 'n/a'}")
    print(f"key_counts chapters={real.report['stats']['chapters']}/{twin.report['stats']['chapters']} "
          f"ad={real.report['dropped']['ad']}/{twin.report['dropped']['ad']} "
          f"toc={real.report['dropped']['toc']}/{twin.report['dropped']['toc']} "
          f"merged={real.report['merged']['groups']}/{twin.report['merged']['groups']} "
          f"inline={real.report['fixed']['inline_ad_cut_lines']}/{twin.report['fixed']['inline_ad_cut_lines']}")
    print("原始文件未改动。")
    return 0 if drift == 0 and only_one == 0 and title_same == len(ta) else 1


def cmd_batch(argv: list[str]) -> int:
    """一批真本 → 一批孪生本 + 逐本同构性结论。

    这是 Phase 2 的正题：以后调规则就吃这批孪生本。
    每本只打一行，行里只有代号、长度、假标记数、字段一致数 ——
    整份输出可以直接贴进会话，不含任何书名或正文。
    孪生本文件名用代号，所以代号表（name-map.json）是唯一的"翻回真书"钥匙。
    """
    ap = argparse.ArgumentParser(prog="mask.py batch", description="批量生成孪生本并校验同构")
    ap.add_argument("--root", default="", help="真书目录；默认用 batch_check 的根")
    ap.add_argument("--limit", type=int, default=10, help="最多处理几本")
    ap.add_argument("--min-bytes", type=int, default=300 * 1024, help="忽略太小的文件")
    ap.add_argument("--out-dir", required=True, help="孪生本输出目录")
    ap.add_argument("--mask-map", default="", help="别名表 JSON；没有就现场生成")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else _default_root()
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2
    files = sorted(p for p in root.rglob("*.txt")
                   if p.is_file() and p.stat().st_size >= args.min_bytes)
    if not files:
        print("没有 .txt 文件", file=sys.stderr)
        return 2
    files = files[:args.limit] if args.limit > 0 else files

    if args.mask_map:
        try:
            name_map = load_name_map(args.mask_map)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    else:
        name_map = alias_table([p.name for p in files], prefix="B")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "name-map.json").write_text(
        json.dumps(name_map, ensure_ascii=False, indent=1), encoding="utf-8")

    ok = 0
    bad_fake = 0
    bad_drift = 0
    worst_fields: dict[str, int] = {}
    for i, src in enumerate(files, start=1):
        code = _lookup_alias(src.name, name_map) or alias_for(src.name, "B")
        try:
            text = decode(src.read_bytes())
        except Exception as exc:  # noqa: BLE001  读不动就记一行，不中断整批
            print(f"{i:>3} {code} READ_FAIL {type(exc).__name__}", file=sys.stderr)
            bad_drift += 1
            continue
        twin = mask_text(text)
        fake = fake_scan(text, twin)
        if len(twin) != len(text):
            print(f"{i:>3} {code} LEN_FAIL", file=sys.stderr)
            bad_fake += 1
            continue
        real_rep = clean_text(text, source="real").report
        twin_rep = clean_text(twin, source="twin").report
        a, b = _report_counts(real_rep), _report_counts(twin_rep)
        keys = sorted(set(a) | set(b))
        drift = [k for k in keys if a.get(k) != b.get(k)]
        for k in drift:
            worst_fields[k] = worst_fields.get(k, 0) + 1
        # 同 cmd_twin：按字节写，禁掉文本模式的换行转换，否则孪生本读回来
        # 会多出上千空行、段落数翻倍，同构性校验就白做了。
        with (out_dir / f"{code}.txt").open("w", encoding="utf-8", newline="") as fh:
            fh.write(twin)
        if fake:
            bad_fake += 1
        elif drift:
            bad_drift += 1
        else:
            ok += 1
        print(f"{i:>3} {code} bytes={len(text)} fake={len(fake)} "
              f"fields={len(keys) - len(drift)}/{len(keys)} drift={len(drift)}")

    print(f"\nSUMMARY books={len(files)} clean={ok} fake_marker={bad_fake} "
          f"drift={bad_drift} twins_dir={out_dir}")
    if worst_fields:
        top = sorted(worst_fields.items(), key=lambda kv: -kv[1])[:12]
        print("DRIFT_FIELDS (name: 几本书漂了)")
        for k, n in top:
            print(f"  {k}: {n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _filler_ok()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    handlers = {"map": cmd_map, "twin": cmd_twin, "check": cmd_check, "batch": cmd_batch}
    fn = handlers.get(argv[0])
    if fn is None:
        print(f"不认识的子命令：{argv[0]}（可用：map / twin / check / batch）", file=sys.stderr)
        return 2
    return fn(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
