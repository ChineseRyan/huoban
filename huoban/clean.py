"""确定性清洗：把脏 TXT 变成章节树。

流水线（每一步都只做一件可核对的事）：

    原始字节 → 解码 → 逐行规范化 → 删整行广告 → 删目录块
            → 压空行 → 并碎行 → 切章 → 组装 Book + 报告

铁律：本模块**只读**输入文件，一个字节都不写回。所有落盘由 CLI 层负责，
并且必须写到独立目录 —— 详见 write_outputs 的调用处。

为什么每一步之间要留一条"记录"：铁律第二条要求先报告后动手。用户看到的
必须是"第 17 行这条被并进了第 18 行"这种能核对的事实，不是"已清洗完毕"。
所以本模块的产物除了章节树，还有一份 report 字典（结构见 _build_report）。

判据全部复用 diagnose / normalize / model 的公开函数，本模块不另立标准：
- 是不是标题      → model.parse_heading()（内部转调 diagnose.match_chapter）
- 是不是广告      → diagnose.judge_ad_line()（体检与清洗共用同一份判据）
- 目录块在哪      → diagnose.detect_toc()["cluster_lines"]
- 怎么规范化一行  → normalize.normalize_line()
- 标题的序号/层级 → model.chapter_from_title()
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import model
from .diagnose import (
    TERMINAL,
    cn_to_int,
    detect_toc,
    find_ad_signal,
    judge_ad_line,
    squeeze,
    strip_frame,
    whole_book_in_one_line,
)
from .encoding import decode, detect
from .model import Book, Paragraph
from .normalize import (
    INDENT_STR,
    clean_line,
    find_zero_width,
    normalize_line,
    normalize_punctuation,
    normalize_quotes,
    strip_zero_width,
)

# ---------------------------------------------------------------------------
# 一、常量（所有阈值集中在这里，方便对着报告调）
# ---------------------------------------------------------------------------

# 连续空行最多留几行。留 1 行：空行在中文小说里表示"场景切换"，
# 留 2 行以上只是排版噪声，读者看不出差别。
MAX_BLANK_RUN = 1

# 目录标记行。squeeze 之后比较，所以"目 录""目录："都算。
TOC_MARKERS = ("目录", "目次", "目录：", "目录:")

# 合并碎行时，合并后的段落长度上限。超过就停手：
# 一条没有句末标点的长行更可能是"整章糊成一行"，那是另一个病灶，
# 硬并下去只会把两个真实段落焊死。
MERGE_MAX_CHARS = 800

# 报告里每类样本最多列几条。全量会让人工核对无从下手。
SAMPLE_LIMIT = 8

# 缩进行占比超过这个值，就认为"缩进在这本书里携带段落信息"。
#
# 为什么没有上界（旧实现要求占比 < 0.95 才算有效信号）：上界会让清洗**不幂等**。
# 第一遍输入里缩进行占 89%（混合），判为有效信号、不并缩进行；render 给每个
# 正文段都补上缩进之后，第二遍输入变成 100% 缩进，按旧判据又"没有信号"了，
# 于是 25 个互不相干的段落在第二遍被焊成一段。去掉上界之后，"缩进存在"
# 本身就是信号 —— 真实的缩进体小说里，硬折行的续行顶格写、段首才缩进，
# 正是靠这个区别分段的；而每一行都缩进的文件，每一行本来就是一个段首。
INDENT_SIGNAL_LOW = 0.05

# 这些"标题家族"不当作章节切分点。
# 分隔线（-----）是场景分隔，把它当章节会把一本书切成几百个空章。
NOT_A_CHAPTER_FAMILY = ("分隔线",)

# 标题最长多少字。超了就不当标题。

# 行首缩进占位符。网络小说被整段抓取下来时，原文网页用「首行缩进两字符」
# 排版，转成 TXT 后缩进没了，只剩采集器写下的占位符：有的写 ????，
# 有的写 !!!!。两种都是**段首标记**，不是正文，也不是标题。
#
# 为什么必须认它：这类文件靠空行分段已经失效（实测 14 万行里空行只占 10%），
# 剩下的唯一分段线索就是这些占位符。不认它，整本书会被并成几千个
# 几百字的假段落，原作者的段落边界全部丢失。
#
# 为什么不当标题：实测 76 个 !!!! 行里 69 行是正文句子开头
#（「家长会结束了。人群渐渐散去。」），提升成章节标题会直接丢掉 69 段正文。
_LEAD_MARKERS = ("????", "!!!!")

#
# 为什么需要这条：正文里的"第 N 章"三个字也可能出现在行首（比如角色说
# "第一章就写到这里"），但那种行很短；真出事的是"整本糊成一行"的书 ——
# 一行 4000 字，行首是"第1章 xxx"，match_chapter 认它是标题，
# 于是整本书变成一个标题 4000 字的空章节，报告里那条"一个段落都没有"
# 的警告也跟着把 4000 字照抄进去，人根本没法看。
# 80 字是保守取值：带副标题的长章名一般不超过 40 字，留一倍余量。
MAX_TITLE_CHARS = 80


# ---------------------------------------------------------------------------
# 一之二、人工调整（adjust）—— 复核界面与清洗层之间的唯一契约
# ---------------------------------------------------------------------------

# adjust 只认这三个键。多一个少一个都当场报错：这是跨进程契约，
# 键名拼错后静默失效比报错危险得多 —— 用户勾了"这行别删"，结果照样删了，
# 他只会以为工具没保存。
ADJUST_KEYS = ("protect_lines", "protect_ranges", "merge_breaks")


@dataclass
class _Adjust:
    """解析好的人工调整。行号一律是**本次输入文本**的 1 基行号。

    为什么行号只对本次输入有效：清洗产物是「一段一行」重新渲染的，
    行号和原文对不上（第二遍清洗时同一个行号指着完全不同的内容）。
    复核界面的模型是「每次都从原文重跑，把累计的勾选一起传进来」，
    所以这个限制不碍事。越界的行号一律记进 ignored 并升成告警 ——
    宁可报错，也绝不静默保护到别的行上去。
    """

    protect: frozenset[int] = frozenset()
    merge_breaks: frozenset[int] = frozenset()
    requested_protect: int = 0
    requested_breaks: int = 0
    ignored: list[dict] = field(default_factory=list)


def _parse_adjust(adjust: dict | None, total_lines: int) -> _Adjust:
    """把 adjust 字典解析成行号集合；越界与畸形项记进 ignored，不抛异常。

    为什么不抛：GUI 传进来的是用户点了半天的勾选状态，换了一本书之后
    那些行号必然越界 —— 这是可预期的日常情况，抛异常等于让界面卡死。
    但**绝不静默**：每一个被丢掉的行号都进 report["adjust"]["ignored"]，
    并升成一条 warning，用户能看见"你有 3 个勾选没生效"。
    """
    if adjust is None:
        return _Adjust()
    if not isinstance(adjust, dict):
        raise ValueError(f"adjust 必须是 dict 或 None，收到 {type(adjust).__name__}")
    unknown = sorted(set(adjust) - set(ADJUST_KEYS))
    if unknown:
        raise ValueError(
            f"adjust 里有不认识的键：{'、'.join(unknown)}；"
            f"只接受 {'、'.join(ADJUST_KEYS)}"
        )

    protect: set[int] = set()
    breaks: set[int] = set()
    ignored: list[dict] = []
    requested_protect = 0
    requested_breaks = 0

    def _check(no: object, key: str) -> int | None:
        # bool 是 int 的子类，True 会混进来当第 1 行，必须挡掉。
        if isinstance(no, bool) or not isinstance(no, int):
            ignored.append({"field": key, "line": str(no), "reason": "不是整数行号"})
            return None
        if no < 1 or no > total_lines:
            ignored.append(
                {
                    "field": key,
                    "line": no,
                    "reason": f"越界（本次输入共 {total_lines} 行）",
                }
            )
            return None
        return no

    def _clamp_low(no: object) -> int | None:
        """区间起点越界时往文件内钳制；猜不出范围就返回 None（整段作废）。

        只处理"是整数、但小于 1"这一种：起点 0 / -5 显然是想从开头勾，钳到第 1 行。
        起点大于末尾（如 [[9999,10005]]）整段都在文件外，钳不动也不该钳。
        非整数（"abc"、None、dict）不猜。
        """
        if isinstance(no, bool) or not isinstance(no, int):
            return None
        if no < 1:
            return 1
        return None

    def _clamp_high(no: object) -> int | None:
        """区间终点越界时往文件内钳制；同 _clamp_low，只钳不扩。"""
        if isinstance(no, bool) or not isinstance(no, int):
            return None
        if no > total_lines:
            return total_lines
        return None

    def _list_field(key: str) -> list:
        """取一个"行号列表"字段；结构错就抛 ValueError。

        为什么不能写成 `adjust.get(key) or []`：那样 {"protect_lines": null}、
        0、""、False、{} 全会被当成"没给"，静默通过 —— 用户明明写错了结构，
        工具一声不吭当没写，正是最难查的一类错。这里只把**缺省**当空列表，
        其余一律要求是 list/tuple。
        """
        if key not in adjust:
            return []
        value = adjust[key]
        if not isinstance(value, (list, tuple)):
            raise ValueError(
                f"adjust['{key}'] 必须是行号列表，实际是 {type(value).__name__}"
            )
        return list(value)

    raw_protect = _list_field("protect_lines")
    for no in raw_protect:
        requested_protect += 1
        v = _check(no, "protect_lines")
        if v is not None:
            protect.add(v)

    raw_ranges = _list_field("protect_ranges")
    for rng in raw_ranges:
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            # 结构错，不是数据错：一个"不是 [起, 止] 的东西"没法记成
            # "第几行没生效"，只能让调用方改输入。与 protect_lines 里
            # "元素不是整数"不同 —— 那是结构对、值不对，只忽略并记账。
            raise ValueError(
                f"adjust['protect_ranges'] 的每一项必须是 [起, 止]，实际是 {rng!r}"
            )
        lo_v = _check(rng[0], "protect_ranges")
        hi_v = _check(rng[1], "protect_ranges")
        if lo_v is None and hi_v is None:
            # 两端都不是有效行号：_check 已各记一笔 ignored，整段作废。
            continue
        if lo_v is None or hi_v is None:
            # **只有一端有效**：早先写法是 `continue`（整段作废），实测共 10 行时
            # `[[7,11]]` 把第 7~10 行的保护静默丢掉、`requested` 也不累计 —— 本函数
            # 上面自述的不变式（"requested 与 applied 之差 = 没生效的勾选数"）因此
            # 破了：差值是 0，用户以为勾上了。改成"往文件内钳制"：落在文件里的那
            # 半截仍然有效。钳制**绝不往外扩** —— 起点是"不是整数"这类猜不出范围的
            # 无效值时宁可整段作废，也不凭空多保护几行。
            lo_c = lo_v if lo_v is not None else _clamp_low(rng[0])
            hi_c = hi_v if hi_v is not None else _clamp_high(rng[1])
            if lo_c is None or hi_c is None:
                continue
            if lo_c > hi_c:
                ignored.append(
                    {
                        "field": "protect_ranges",
                        "line": f"{rng[0]}-{rng[1]}",
                        "reason": "钳制后起点仍大于终点",
                    }
                )
                continue
            ignored.append(
                {
                    "field": "protect_ranges",
                    "line": f"{rng[0]}-{rng[1]}",
                    "reason": f"区间有一端不在本次输入里（共 {total_lines} 行），"
                              f"只保留第 {lo_c}-{hi_c} 行",
                }
            )
            requested_protect += hi_c - lo_c + 1
            protect.update(range(lo_c, hi_c + 1))
            continue
        if lo_v > hi_v:
            ignored.append(
                {
                    "field": "protect_ranges",
                    "line": f"{lo_v}-{hi_v}",
                    "reason": "起点大于终点",
                }
            )
            continue
        # 闭区间：两头都算。计数按"用户勾了多少行"记，重复勾选也算，
        # 这样 requested 与 applied 的差就是"没生效的勾选数"。
        requested_protect += hi_v - lo_v + 1
        protect.update(range(lo_v, hi_v + 1))

    raw_breaks = _list_field("merge_breaks")
    for no in raw_breaks:
        requested_breaks += 1
        v = _check(no, "merge_breaks")
        if v is None:
            continue
        if v == 1:
            ignored.append(
                {
                    "field": "merge_breaks",
                    "line": 1,
                    "reason": "第 1 行之前没有可断开的合并",
                }
            )
            continue
        breaks.add(v)

    return _Adjust(
        protect=frozenset(protect),
        merge_breaks=frozenset(breaks),
        requested_protect=requested_protect,
        requested_breaks=requested_breaks,
        ignored=ignored,
    )


# ---------------------------------------------------------------------------
# 二、行对象
# ---------------------------------------------------------------------------


@dataclass
class _Line:
    """清洗过程中的一行。所有字段都是"已经规范化过"的状态。

    no 保留的是**原始文件**的 1 基行号，报告与章节 start_line 都用它。
    从头到尾不重新编号，是为了让用户能"照着报告回到原文第几行"。
    """

    no: int
    raw: str
    text: str            # normalize_line 的结果，可能带 INDENT_STR 前缀
    indented: bool
    blank: bool = False
    heading: model.Heading | None = None
    ad: dict | None = None      # judge_ad_line 的证据（整行广告已经不在流水线里了）
    # 这一行"当前覆盖到原文第几行"。并碎行会把后面的行吸收进来，
    # no 始终保持起始行号（报告要拿它回溯原文），last_no 跟着往后走，
    # 用来判断下一次还能不能继续并 —— 三行以上的碎句要连并两次。
    last_no: int = 0
    # 这一行是诗词/韵文（见 _mark_verse）。默认 False，只有 _build_book
    # 把它写成 Paragraph(kind="verse") 时才起作用 —— 排版层据此换字体、居中。
    verse: bool = False
    # 行内夹广告切掉的那截尾巴（见 cut_inline_ad）。空串＝这一行没被切过；
    # 非空串说明正文只剩前面一段，尾巴进报告 samples 供人工核对。
    cut: str = ""
    # 原文件在这行开头写了段首占位符（???? / !!!!，见 _LEAD_MARKERS）。
    # 它是"原作者在这里另起一段"的明确证据，比任何启发式都硬 —— 并碎行
    # 遇到它必须收手，哪怕上一行没写完（实测有 162 行是这样）。
    para_start: bool = False
    # 整行只有装饰符号（"＊＊＊＊＊＊"、"***"、"———"），是原作者的场景分隔线。
    # 它不是正文：渲染层据此居中输出（见 epub.py 的 p.sep），
    # 否则一个缩进 + 两端对齐的"＊＊＊＊＊＊"看上去就像正文里混进了乱码。
    sep: bool = False
    # 硬折行假标题闸门（见 _wrap_fake_chunk）判出"这一行是上一段被硬折行
    # 切开的半截"时，heading 当场清空，原判出来的标题对象存到这里。
    #
    # 为什么不在 _scan 里就定案：**拒绝的唯一合法前提是它并得回上一段**。
    # 并碎行有长度上限（MERGE_MAX_CHARS）、也不合并段首占位符行与分隔线，
    # 那些情况下这一行会作为独立行留在正文里；第二遍它站在段首、段中判据
    # 不拦它，章数就翻回去了 —— 幂等当场破掉（实测 #173 章数 276→605、
    # #309 331→344）。所以最终裁定权在 _merge_fragments：并成功才算拒，
    # 并不成就把标题还回去，宁可少拒一个假章，也不许破幂等。
    wrap_frag: bool = False
    wrap_heading: "model.Heading | None" = None
    # 人工保护（adjust 的 protect_lines / protect_ranges）留下的行。语义是
    # **"这一行的文字不许动，也不许改它的结构"**：不许当章节标题、不许升级成
    # 卷标题、不许被并碎行焊进邻段。凡是"事后还要改这一行"的步骤都得绕开它。
    #
    # 为什么必须显式记一笔：受保护的行在 _scan 里可能还带着 heading（比如
    # 「目录」同时属于前言类标题家族、目录条目「第一章 开始 …… 3」也认得出
    # 章节形态）。光把 heading 清空还不够 —— _promote_volume_lines 会拿
    # 「第一卷 风起」这种行当卷标题，_merge_fragments 会把相邻的短行焊成一段。
    # 实测：4 条相邻的受保护目录行不加这道闸会被焊成一段，书前凭空多出 4 个
    # 带页码尾巴的假章节。
    frozen: bool = False
    # 被人工保护下来的**整行广告**。它有两个身份：账上算"该删但没删"（进
    # adjust.protected.ad），结构上又以独立一段留在 items 里。
    #
    # 为什么不能拿 `ad is not None` 当判据：`ad` 非空的还有一类"标题行里带
    # 广告词"的行（ad_title，比如「第二章 标题1 http://… 记住本站」）—— 那类行
    # 从来没被删过，账也没记过，目录块规划里它就该像普通标题一样当证据。
    # 早先的写法把两者混为一谈，结果目录簇被这类行断开，它自己既不删也不留，
    # 漏进了 front_matter，`adjust=None` 的输出都跟着变了（review 抓到的）。
    ad_kept: bool = False

    def __post_init__(self) -> None:
        if not self.last_no:
            self.last_no = self.no

    @property
    def content(self) -> str:
        """去掉段首缩进之后的正文。

        为什么正文要脱掉缩进：缩进是**排版**信息，由 render 层统一加回来
        （见 render.render_txt）。中间态里留着它，合并、去重、比较字数
        全都要先 lstrip 一遍，多一处漏了就多一个 bug。
        """
        if self.text.startswith(INDENT_STR):
            return self.text[len(INDENT_STR):]
        return self.text

    @property
    def is_title(self) -> bool:
        return self.heading is not None


# 场景分隔线允许出现的字符。只收装饰性符号，**刻意不收句读标点** ——
# 单独一行「……」是"沉默/停顿"的意思，单独一行「？？」是原书的错字，
# 把它们当分隔线居中输出会改掉原文的语气。
_SEP_CHARS = frozenset(
    "＊*☆★※◇◆○●□■△▲▽▼♦♠♣♥♡〓▬▭"
    "-=~—－＿_＋+·∙⋅～"
    # 语料实测补进来的（396 本真书里漏认的）：
    #   ﹡ U+FE61 小号星号（一本 10 行）、× 乘号（一本 5 行）、
    #   ＝ 全角等号（三本，35 个连写）、制表符横线 ─ ━ ═ 等（一本 41 字符）。
    # 都只收"整行都是它"的情形，正文里出现这些符号不会被误伤。
    "﹡×＝﹢﹣"
    "─━│┃┄┅┆┇┈┉┊┋═║╌╍╎╏"
    "▪▫◊◈◉◎❖❀✿✧✦✳✽✾❋"
)


def _is_separator_line(text: str) -> bool:
    """整行是不是只有装饰符号（场景分隔线）。

    判据：去掉空白后至少 3 个字符，且每一个都在 _SEP_CHARS 里。
    要求"全是符号"而不是"含符号"：正文里出现「★」是可能的（游戏界面、
    分数），只有整行都是符号时它才不可能是正文。

    为什么至少要 3 个：单个「·」常被用来当列表点或分隔，居中输出没意义；
    两个连写的「——」是破折号，不是分隔线。
    """
    s = squeeze(strip_zero_width(text))
    # 私用区字符（U+E000–U+F8FF）是字体造字，阅读器里多半是个方框：
    # 实测有一本书的分隔线是 "\ue4c6＊＊＊…"，前面那个私用区字符不能
    # 让整行判不成分隔线。剥掉它再看剩下的。
    s = "".join(ch for ch in s if not ("\ue000" <= ch <= "\uf8ff"))
    if len(s) < 3:
        return False
    return all(ch in _SEP_CHARS for ch in s)


# 分部结束标记：「第一部（完）」「第二部完」「全书完」这类行。
# 《书J》第一部结尾有一行「书J第一部（完）」：它带着书名水印，
# 剥完前缀成了「第一部（完）」，正好命中「第N卷部类」，于是变成一章的标题，
# 把后面第二部 3000 多段正文全吞进这一章 —— 目录里会出现一个叫「第一部（完）」
# 的巨型章节。这类行是**分部结束的记号**，不是任何一章的开头，按场景分隔线处理。
_PART_END_MARKER = re.compile(
    r"^第\s*[0-9０-９〇零一二三四五六七八九十百千万两]{1,6}\s*[卷部集篇]\s*"
    r"[（(]?\s*(?:完|終|终|结束|完本)\s*[）)]?$"
)


def _is_part_end_marker(text: str) -> bool:
    """这一行是不是"第N部（完）"这种分部结束标记。"""
    core = _strip_site_prefix(text)
    return _PART_END_MARKER.match(squeeze(core)) is not None


# 章节结束标记：连载时作者写给读者看的那几句，做成书之后没有任何意义。
# 连载标记是作者写给连载读者看的：做成书之后不存在「本章完」这种语境，
# 整行删。
#
# 只认"整行就是这句话"（允许两端带装饰符号与括号）：正文里出现「本章完」
# 三个字的地方一定还有别的字，整行恰好只有它才说明它是标记。
_END_MARKER_RE = re.compile(
    r"^(?:本章完|本章结束|本章终|本章已完|本章未完|未完待续|待续|以下正文|正文开始)$"
)


def _is_end_marker(text: str) -> bool:
    """这一行是不是"本章完"这类连载标记（整行删掉的那种）。"""
    core = squeeze(_strip_decor(strip_zero_width(text)))
    core = core.strip("".join(_SEP_CHARS))
    return bool(_END_MARKER_RE.match(core))


def _ends_terminal(text: str) -> bool:
    """这一行是不是停在句末标点上。

    空行、单字符标点行一律算"不完整"由调用方另行处理，这里只回答
    "末尾那个字符是不是句末标点"。
    """
    body = text.rstrip()
    if not body:
        return False
    # 收尾的引号/括号本身就在 TERMINAL 里（「你走吧」以 」收尾即算写完），
    # 所以这里不必先剥。曾经试过"先剥再判"，结果整行只有「（一）」这种
    # 枚举行剥完只剩「（一」，被判成没写完，下一段就被焊上来（回归用例
    # case_bracket_section_enumeration_not_chapters 当场红）。
    return body.endswith(tuple(TERMINAL))


def _ends_lead(text: str) -> bool:
    """这一行是不是停在冒号/分号上 —— 也就是在"引出下文"。

    和 _ends_terminal 分开：句末标点表示"句子完了"，冒号分号表示
    "下面就是内容"。碎行合并只该接前者；把"诗曰："和它引出的诗句
    焊成一段，诗词标记就全废了（引导语只有在**独占一行**时才是证据）。
    """
    body = text.rstrip()
    if not body:
        return False
    return body.endswith(("：", ":", "；", ";"))


def _is_toc_marker(text: str) -> bool:
    return squeeze(text) in TOC_MARKERS


def strip_lead_marker(raw: str) -> tuple[str, bool]:
    """剥掉行首的段首占位符（???? / !!!!），返回 (剩下的内容, 是不是段首)。

    必须先 lstrip 再判断：原文件是「    ????我叫阿雨…」——4 个半角空格是
    缩进，问号串才是标记。顺序反了就永远匹配不上。

    但返回的文本**必须保留缩进**：缩进在这类文件里虽然不再表示段首（段首由
    ???? 表示），clean_line 仍要靠它算 indent_width，render 也靠它决定排版。
    所以这里只把"空白 + 标记"这一段前缀精确去掉，剩下的原样交回去。
    """
    s = raw.lstrip(" \u3000\t")
    for mark in _LEAD_MARKERS:
        if s.startswith(mark):
            # 前缀 = 原行长度 - 剥完后的长度，中间的空白与标记一起消失
            return raw[: len(raw) - len(s)] + s[len(mark):], True
    return raw, False


# 标题里"编号"与"标题正文"之间常见的分隔符。只为区分
# "第三章 你是谁？"（标题）和 "第一章就说得很清楚！"（正文句子）。
_TITLE_SEPARATORS = " \u3000\t·、：:—-"

# 句末标点：以这些收尾的行更可能是一句正文。
_SENTENCE_TAIL = "。！？…"

# 收尾的右引号/右括号。必须先剥掉它们再看"最后一个实义字符"是什么：
# '第一章里，他问：“真的吗？”' 的最后一个字符是 '”'，不剥就永远进不了
# 标点分支，会被当成标题把一章劈成两半。
_TRAILING_CLOSERS = "”’」』）)】]"

# 章节编号前缀。只负责"把编号与标题正文切开"，不负责判是不是标题
# （那是 model.parse_heading 的活）。三种形态对应第N章类、Chapter N、
# 卷首特殊章；Markdown 标题与纯数字行没有"编号后的标题正文"，走兜底判据。
_HEADING_PREFIX = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,8}\s*[卷部集篇章回节幕话話]"
    r"|[Cc][Hh][Aa][Pp][Tt][Ee][Rr]\s*[0-9０-９]{1,4}"
    r"|序章|序言|楔子|引子|前言|引言|后记|尾声|终章|大结局|番外|外传|附录"
    r")"
)


# 不锚定的章节编号。用来数"一行里有几个编号"（双编号守卫）。
# 不能用 _HEADING_PREFIX：那个带 ^，findall 永远只找得到行首那一个。
_HEADING_NUM_ANY = re.compile(
    r"第\s*[0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,8}\s*[卷部集篇章回节幕话話]"
)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


# 词前缀（不是"第N章"那种带编号的前缀）后面允许跟多少字才算标题正文。
# 12 与站点水印的 _SITE_PREFIX_TITLE_TAIL 同口径：标题正文短，散文长。
_WORD_PREFIX_TAIL_MAX = 12

# 以逗号/顿号收尾的行，整行（剥掉收尾分隔符后）不超过这么多字才仍算标题。
# 带编号的行有结构锚点，作者在标题末尾顺手打逗号是常事（实测「第02章·美丽人妻的
# 轮奸内射飨宴，」「Chapter 3 Ending，」都是真标题，15~16 字）；而
# 「第3章勾引学生的美足仓库的事件后没过两天，陆绍辉和孟华阳两人」有三十字，
# 是被折行切断的正文。20 是两条样本之间的空档。
_COMMA_TITLE_MAX = 20

# 词前缀本身。与 _HEADING_PREFIX 里的那一支逐字一致，改一处要改两处 ——
# 分成两条是因为_heading_of 要单独问"这行是词前缀家族吗"。
_WORD_PREFIX = re.compile(
    r"^\s*(?:序章|序言|楔子|引子|前言|引言|后记|尾声|终章|大结局|番外|外传|附录)"
)


def _title_tail(core: str) -> str | None:
    """把"编号前缀"和它后面的标题正文切开；切不开（不认识的前缀）返回 None。"""
    m = _HEADING_PREFIX.match(core)
    if m is None:
        return None
    return core[m.end():]


def _looks_like_sentence(text: str, *, section_core: str | None = None) -> bool:
    """这行读起来是一句正文，而不是一个标题。

    判据必须建立在**原始行**上：规范化会把行内空白全删掉
    （"第三章 你是谁？" → "第三章你是谁？"），拿规范化后的文本判
    "编号后面有没有分隔符"，答案永远是没有，一整批合法标题会被吞成正文。

    三条依据：
      1. 编号后面紧跟分隔符（空格、冒号、破折号）→ 编号与标题正文是两段，
         这是标题。这条**优先于**句末标点：'第一章 序。'、'第十二章 大结局！'
         都是合法标题。
      2. 标点紧贴编号收尾（"第一章里写得很清楚。"）→ 整行是一句话，是正文。
      3. 没有编号前缀的家族（Markdown 标题、纯数字行）走老判据。

    section_core 只给数字顿号分节这一族用：那类标题可以带情绪标点收尾
    （「七.被凌辱轮奸过后……」），而情绪标点在 _SENTENCE_TAIL 里，会把这行
    判成句子。传进来的必须是**剥过水印前缀的归一化文本**，判定只看"像不像标题"。
    """
    s = text.strip()
    if not s:
        return False
    core = s.rstrip(_TRAILING_CLOSERS)
    if not core:
        return False
    # 纯西文的句点收尾是正文：'Chapter 2 is over.' 不是标题。
    # 中文标题不用英文句点收尾，所以这条只在整行没有汉字时生效。
    if core[-1] in ".!?" and not _has_cjk(s):
        return True

    # 数字顿号分节的标题：行首编号已经标明它是节，行尾情绪标点不算句子证据。
    if section_core is not None and _section_line_is_title(section_core):
        return False

    tail = _title_tail(core)
    if tail is not None:
        if not squeeze(tail):
            return False            # 编号后面没有正文（"第一章！"）→ 标题
        if tail[0] in _TITLE_SEPARATORS:
            # 编号 + 分隔符通常是标题（「第三章 你是谁？」）。但正文里也有
            # 以章节词开头、编号后面甩出一整句散文的句子 ——《书L》里
            # 「第一部　手机，逢年过节会给她成百的红包，…他算大手笔。」
            # 「第一回　时迷惘，但一样期待。」就是；旧代码在这里无条件判成
            # 标题，把 124 段正文吞进一个假章。区分信号是"句中句读"：真标题
            # 的标点一律在末尾（「第一章 序。」「第02章·…飨宴，」），散文的
            # 逗号在句中、后面还跟着半句话。剥掉收尾标点后还留逗号/顿号/
            # 分号 → 这是句子，不是标题。
            # 只认"句号收尾且句中还带逗号"这一种形态：叙述散文写完一句用
            # 句号，句中逗号是它的从句；而网络小说的台词标题即便带逗号，
            # 也一律用省略号/叹号/问号收尾（《书L》第 85~88 章就是
            # 「第85章 想回老家一趟，你跟我走……」这种，用 _SENTENCE_TAIL
            # 一刀切会把这四个真标题误杀成正文）。
            # 已知边界（规则无法两全，留待 M4 语义判断）：
            # 骈句式合法标题「第X章 初入江湖，风起云涌。」形态完全相同会被
            # 误判成正文；而「第一回 时迷惘，但一样期待…／？／！」这类非句号
            # 收尾的假章仍会漏。真实语料 108 条标题中该形态 0 条。
            if core[-1] == "。":
                inner = squeeze(tail).rstrip(_TITLE_SEPARATORS + _SENTENCE_TAIL + ",，、;；")
                if any(ch in "，,、;；" for ch in inner):
                    return True
            return False            # 编号后有分隔符 → 标题
        return core[-1] in _SENTENCE_TAIL

    if core[-1] == "。":
        return True
    return core[-1] in _SENTENCE_TAIL and not any(ch in _TITLE_SEPARATORS for ch in s)


# 站点水印前缀。有的站把"正文""山村野事"这类字样直接刷在章节标题行行首，
# 于是 match_chapter 的 ^\s* 锚点挂空，一整本书 137 章只认出 1 章。
# 判据很窄：前缀必须很短（<=6 字）、不含句末标点、剥掉之后必须真的能解析出标题。
# 第三条是安全阀 —— "他说第三章的故事很精彩。"剥完也像标题，但它整行以句号收尾，
# 会被 _looks_like_sentence 挡在门外（那个守卫看的是原始行，前缀里的"他说"就是证据）。
_SITE_PREFIX_MAX = 6
_SITE_PREFIX_STOP = "。！？…;；"
# 剥完前缀后，编号后面那一截最多允许多少字（不含句读）才算"标题"。
# 超过这个长度还带逗号句号的，是正文句被前缀规则误伤（见 _title_tail_is_titleish）。
_SITE_PREFIX_TITLE_TAIL = 12
_SITE_PREFIX_HEAD = re.compile(
    r"第\s*[0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,8}\s*[卷部集篇章回节幕话話]"
    r"|Chapter\s*[0-9０-９]{1,4}"
)
# 「正文 一、谁动了我的马子」：数字顿号分节的编号不是「第N章」那种形态，上面
# 那条锚点整行都找不到，于是这一类书"认得出标题、过不了闸门"（闸门也先调
# _strip_site_prefix，剥不动就等于一条编号都没数到 —— 见 _numbered_section_is_structure）。
# 编号字符集与 _NUM_SECTION_LINE、diagnose 的「数字顿号分节」保持一致，改一处要改三处。
# 这条**只在上一条整行找不到时才生效**：一行里若同时有「第N章」和正文里的「3、」，
# 落点必须按「第N章」定，否则原来能认的标题会被剥到编号清单上去。
_SITE_PREFIX_NUM_HEAD = re.compile(
    r"(?:[0-9０-９]{1,4}|[一二三四五六七八九十百]{1,4})\s*[、.．]"
)
# 整块被一对括号包住的装饰前缀（书名号、方头括号、圆括号）。
# 《书J》第二部每章标题写成「【书J】第二部 第一章」——
# 前缀是书名 5 字 + 两个方头括号，共 7 字，超了 _SITE_PREFIX_MAX 就被拒，
# 结果整本书只认出 9 章，第二部 3000 多段全挂在「第一部（完）」那一章下面。
# 被括号整块包住的字样不可能是正文句的开头，长度可以放宽到 12 字。
_BRACKETED_PREFIX = re.compile(r"^[【\[（(][^【\[（()）\]】]{1,12}[】\]）)]$")


def _title_tail_is_titleish(rest: str) -> bool:
    """剥掉水印前缀之后，剩下这截到底像不像"编号 + 标题"。

    为什么需要这道闸：前缀规则只看"很短、不含句末标点、剥完能解析出标题"，
    而这三条挡不住正文句。实测《书B》里有这样一行：

        "    演第三集时车路过一个城市，恰逢交通管制。两旁分别是机场大巴和公交长"

    前缀"演"只有一个字、剥完"第三集…"又能被 match_chapter 认成卷标题
    （"集"是卷级量词），于是整句正文变成了一章的标题 —— 那一章后面
    还挂着几百段正文，读者在目录里看到的是一整句话。

    判据：编号之后要么什么都没有、要么紧跟分隔符（"：楔子"/" 兽心沸腾"）、
    要么是一小截不含句读的词（"跟踪"）。剩下第三种情形 —— 编号后面直接
    接一长串带逗号句号的文字 —— 那是正文句，不是标题。
    """
    tail = _title_tail(rest)
    if tail is None:
        return True                 # 没有编号前缀可切（序章/番外一类），交给上层判断
    if not squeeze(tail):
        return True                 # "第一章" 后面什么都没有
    if tail[0] in _TITLE_SEPARATORS:
        return True                 # "第000章：楔子"
    if len(squeeze(tail)) <= _SITE_PREFIX_TITLE_TAIL:
        if not any(ch in _SITE_PREFIX_STOP for ch in tail):
            return True             # "第四节跟踪"
    return False


# 「第一卷:飞来的艳福 第01章 鸿运赌坊」—— 卷容器与章号挤在同一行。
# 按「第N卷部类」解析会得到 level=2 的卷标题，整本书 121 章全成了卷容器，
# 一个正文节点都不剩（《书O》：0 章、5333 段全掉进 front_matter）。
# 卷前缀后面既然还挂着一个章号，这一行的身份就是"章"，把卷前缀剥掉再解析。
# 卷前缀里不许有句末标点（「第一卷写得很好。第2章…」是正文不是标题）。
_VOLUME_THEN_CHAPTER = re.compile(
    r"^\s*第\s*[0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,8}\s*[卷部集篇]"
    r"[^。！？；\n]{0,20}?"
    r"(?P<chap>第\s*[0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,8}\s*[章回节幕话話])"
)


def _strip_volume_prefix(s: str) -> str | None:
    """「第一卷:飞来的艳福 第01章 鸿运赌坊」→「第01章 鸿运赌坊」；不命中返回 None。

    单独抽成函数，是因为它要作用两次：一次在行首，一次在剥掉括号前缀之后
    （「【书J】第二部 第一章」剥完前缀才露出「第二部 第一章」）。
    """
    m_vol = _VOLUME_THEN_CHAPTER.match(s)
    if m_vol is None:
        return None
    rest = s[m_vol.start("chap"):]
    if not _title_tail_is_titleish(rest):
        return None
    return rest


def _strip_site_prefix(raw: str) -> str:
    """剥掉章节编号之前的站点水印前缀；没有前缀就原样返回。"""
    s = raw.lstrip(" \u3000\t")
    # 卷容器 + 章号挤在同一行：剥掉卷前缀，让这一行按章标题解析。
    vol = _strip_volume_prefix(s)
    if vol is not None:
        return vol
    # 行首本来就能解析出标题 → 不剥。
    # 水印规则是给"锚点挂空、整行认不出标题"的行兜底的。已经在行的中部
    # 找编号再往前剥，会把好标题切坏：《书I》的「1、第1部第1节」先命中
    # 「第1部」（第N卷部类在行中也能搜到），前缀"1、"被当水印剥掉，
    # 标题塌成「第1节」，顿号分节这一族整族的序号全丢了。
    if model.parse_heading(s) is not None:
        return raw
    m = _SITE_PREFIX_HEAD.search(s)
    if m is None:
        # 「正文 一、谁动了我的马子」：整行没有「第N章」那种锚点，
        # 退一步按"数字/中文数字 + 顿号"定位编号（见 _SITE_PREFIX_NUM_HEAD）。
        m = _SITE_PREFIX_NUM_HEAD.search(s)
    if m is None or m.start() == 0:
        return raw
    prefix = s[: m.start()]
    # 长度按**去掉空白后**算：站点水印后面常跟两个半角空格（"VIP章节  第033章…"），
    # 按原样长度算会凭空多出 2 字，前缀整条被判成"太长"——认标题时用归一后的文本
    # 又能剥掉，于是标题取值保留了水印（《书壬》：215 个标题顶着「VIP章节　」，
    # 而且第一遍留、第二遍剥，两遍结果不同，42 行幂等差异）。
    # 整块被括号包住的前缀（书名号里的书名）另外放宽长度，见 _BRACKETED_PREFIX。
    squeezed = squeeze(prefix)
    if len(squeezed) > _SITE_PREFIX_MAX and not _BRACKETED_PREFIX.match(squeezed):
        return raw
    if any(ch in _SITE_PREFIX_STOP for ch in prefix):
        return raw
    # 前缀里必须有个**实义字符**（字母/数字/汉字）。纯装饰不算前缀：
    # 「【第一章　开始】」这种目录条目的装饰括号若被当水印剥掉，整批目录条目
    # 会复活成 0 段落空章节 —— 那正是目录块判定要消灭的东西。
    if not any(ch.isalnum() for ch in prefix):
        return raw
    rest = s[m.start():]
    # 剥完还得像标题。没有这道闸，"演第三集时车路过一个城市，恰逢交通管制。"
    # 会变成一章的标题（详见 _title_tail_is_titleish 的说明）。
    if not _title_tail_is_titleish(rest):
        return raw
    # 前缀后面可能还压着一层卷前缀（「【书J】第二部 第一章」）。
    vol = _strip_volume_prefix(rest)
    if vol is not None:
        return vol
    return rest


# 「（一）」「（十二）」整行。与 diagnose 的家族同名，但这里必须自己再扫一遍：
# "这是分节标题还是正文里的行内枚举"要看全书，逐行看不出来。
_BRACKET_FAMILY = "括号分节"
_BRACKET_SECTION_LINE = re.compile(
    r"^[（(]\s*([0-9０-９〇零一二三四五六七八九十百]{1,4})\s*[）)]$"
)
# 至少这么多节、且最大编号至少这么大，才认这本书是"按节分章"。
_BRACKET_MIN_SECTIONS = 8


def _bracket_section_is_structure(lines: list[str]) -> bool:
    """全书判断：「（一）」这类行是章节分节，还是正文里的行内枚举。

    判据只有一条 —— **编号不许回头**。《书F》从（一）排到（二百二十），
    中途只会越数越大；《书B》里的「（一）（二）（三）（四）」是
    正文里的列举，每进一章就从（一）重新数起，必然出现"下一个编号比上一个小"。
    真书上这两种写法长得一模一样，缩进也一样（书F 2 空格、书B 4 空格），
    单行判据分不开：不做书级闸门，乱伦那本书会凭空多出 131 个空壳章节。

    再加两道量纲闸：条数太少、最大编号太小，都不像一本书的骨架。
    """
    nums: list[int] = []
    for raw in lines:
        m = _BRACKET_SECTION_LINE.match(squeeze(strip_zero_width(raw)))
        if m is None:
            continue
        n = cn_to_int(m.group(1))
        if n is not None:
            nums.append(n)
    if len(nums) < _BRACKET_MIN_SECTIONS or max(nums) < _BRACKET_MIN_SECTIONS:
        return False
    return all(b >= a for a, b in zip(nums, nums[1:]))


# 「1、掳君（1）」式的阿拉伯数字 + 顿号分节。与括号分节同理，单行判据
# 分不开"分节标题"和"正文里的编号清单"，必须过书级闸门。
_NUM_SECTION_FAMILY = "数字顿号分节"
_NUM_SECTION_LINE = re.compile(r"^\s*((?:[0-9０-９]{1,4}|[一二三四五六七八九十百]{1,4}))\s*[、.．]\s*\S")
_NUM_SECTION_MIN_SECTIONS = 8
# 整本书别的章节结构一条都没认出来时，门槛降到 5：7 节的短书（《书T》）
# 靠 8 这条永远过不去，可它又确实按「一、二、…」分节。
# 为什么只在"零其他结构"时降：书里已经有 110 章时再放行 7 条顿号编号，
# 等于把好书从中间切开（《书U》就是这么被挡住的）。
_NUM_SECTION_MIN_SECTIONS_FALLBACK = 5
# 节与节之间平均至少隔这么多行。《书M》66 节平均隔 74 行；
# 《书Q》开头那份「1、2、3…」资料清单是连续 9 行，平均隔 1 行。
_NUM_SECTION_MIN_GAP = 5.0
# 编号后面那一截的最大长度、以及不许出现的句读。
# 「七、八根大小不等的阴茎，“噗”的一声笑了…」是正文里的列举，不是节标题；
# 《书癸》有 18 行这种正文，混进编号序列后把 157 节的递增 run
# 切成 [33,25,4,14,…] 一堆短段，整本书从 158 章掉到 1 章。
_NUM_SECTION_TAIL_MAX = 30
_NUM_SECTION_TAIL_STOP = "。！？；;…"
# 节标题允许用情绪标点收尾：「七.被凌辱轮奸过后……」「八.他说完就走了？！」。
# 这类行在原文件里编号后面常跟一个空格（"七. 被…"），成句守卫看到分隔符就放行；
# 渲染出去空格被归一化掉，第二遍再判就成了句子 ——《书I》因此幂等差 2 行。
# 所以对数字顿号分节这一族放宽：先剥行尾连续的情绪标点，再看剩下那截像不像正文。
_SECTION_TITLE_TAIL_STRIP = "…！？!?"
# 剥完情绪标点后，剩下这截里出现这些就还是正文。比闸门的 stop 表多了逗号：
# 真标题很少带逗号，带逗号的十有八九是正文句被编号误伤。
_SECTION_TITLE_TAIL_STOP = "。！？；;…，,"
# 编号 + 分隔符 + 标题正文，尾巴单独取出来判（_NUM_SECTION_LINE 的 end 落在
# 标题首字之后，拿它切片会漏掉一个字）。
_SECTION_TITLE_SPLIT = re.compile(
    r"^\s*(?:[0-9０-９]{1,4}|[一二三四五六七八九十百]{1,4})\s*[、.．]\s*(?P<tail>.*)$"
)


def _section_line_is_title(core: str) -> bool:
    """「七.被…」「1、标题」这类编号行，按"标题性"再判一次。

    只在 _looks_like_sentence 已经判成句子时当放宽用（见 _heading_of），
    所以判据可以窄：宁可判"不像标题"，也不能凭空造标题 —— 正文里那种
    「编号 + 带逗号长句」的列举一旦被认成标题，整本结构就散了。
    """
    m = _SECTION_TITLE_SPLIT.match(core.strip())
    if m is None:
        return False
    tail = m.group("tail").strip().rstrip(_SECTION_TITLE_TAIL_STRIP).strip()
    if not tail:
        return True
    if len(tail) > _NUM_SECTION_TAIL_MAX:
        return False
    return not any(ch in _SECTION_TITLE_TAIL_STOP for ch in tail)


def _numbered_section_is_structure(
    lines: list[str], *, min_sections: int = _NUM_SECTION_MIN_SECTIONS
) -> bool:
    """全书判断：「1、标题」这类行是章节分节，还是正文里的一串编号清单。

    三道闸（缺一不可）：
      1. 条数与最大编号都至少 8 —— 一两行编号不是骨架；
      2. 编号回头只许发生在**分部换卷**上：每一段连续递增都得够长（>= 8 条）。
         旧写法是"整本不许回头"，那把《书I》这种一部 17 节、二部 26 节、
         每部都从「一、」重数的书整本判死了（100 本抽检里它有 100 条编号、
         切出 0 章）。而正文里每章重数的枚举（《书V》每章
         「1、…5、」）run 长度只有 1-5，靠这一条照样挡住。
      3. 节与节之间平均隔开 >= _NUM_SECTION_MIN_GAP 行 —— 真分节后面
         跟着成百上千行正文，而资料清单/注意事项是连续几行挤成一块。
    第 3 条是《书Q》逼出来的：它开头「1、五行旗…9、冥书」连续 9 行，
    前两道闸全过，会把一份法宝清单切成一本书的章节。
    """
    nums: list[int] = []
    pos: list[int] = []
    for i, raw in enumerate(lines):
        # 先剥站点水印前缀：「正文 一、谁动了我的马子」的编号不在行首。
        # 标题识别那边（_heading_of）本来就先剥前缀再解析，闸门必须用同一口径，
        # 否则这类书「认得出标题、却过不了闸门」，一条都切不出来。
        s = squeeze(_strip_site_prefix(strip_zero_width(raw)))
        m = _NUM_SECTION_LINE.match(s)
        if m is None:
            continue
        n = cn_to_int(m.group(1))
        if n is None:
            continue
        # 编号后面那截必须像标题：短、不带句读。正文里的「七、八根…」在这里被剔除，
        # 否则真分节书的递增序列会被这些假编号打断（见 _NUM_SECTION_TAIL_MAX 的说明）。
        tail = squeeze(s[m.end():])
        if not tail or len(tail) > _NUM_SECTION_TAIL_MAX:
            continue
        if any(ch in _NUM_SECTION_TAIL_STOP for ch in tail):
            continue
        nums.append(n)
        pos.append(i)
    if len(nums) < min_sections or max(nums) < min_sections:
        return False
    run = 1
    for prev, cur in zip(nums, nums[1:]):
        if cur < prev:
            if run < min_sections:
                return False
            run = 1
        else:
            run += 1
    if run < min_sections:
        return False
    gap = (pos[-1] - pos[0]) / (len(pos) - 1)
    return gap >= _NUM_SECTION_MIN_GAP


# 整行只有一个数字（「2」「17」）。语料里它多数是扫描/OCR 残留的页码，
# 少数才是"用数字当章节号"的分节书。
_BARE_NUMBER_LINE = re.compile(r"^[0-9０-９]{1,4}$")
# 至少这么多条才算骨架。一条孤零零的数字不可能是章节 —— 实测《书寅》
# 行 5057 的「2」夹在两段对话之间（上一行「谢谢你，老师。蕾拉爱你。」、
# 下一行「鸟声啾鸣…」），却按「纯数字行」家族切走 1355 段正文。
_BARE_NUMBER_MIN_LINES = 3
# 编号之间平均至少隔这么多行。与顿号分节同口径：真分节后面跟着成段正文，
# 而"1 2 3"挤成一坨的资料清单不满足。
_BARE_NUMBER_MIN_GAP = 5.0


def _bare_number_is_structure(lines: list[str]) -> bool:
    """整行只有一个数字的行，是章节编号还是扫描残留的页码。

    判据只有一条：**成串**。真编号是按顺序排下去的一串（「1 2 3 4…」），
    扫描残留的页码则是孤零零一条 —— 实测《书寅》整本 12 万行只有一条
    「2」，它把 1355 段正文切成了独立一章。

    曾经还要求"这本书没有别的章节结构"（有「第N章」就不认裸数字），实测把它
    去掉才对：《书W》每章内部用「1 2 3…」标小节、章标题仍是「第一章…」，
    两道结构本来就并存；按旧判据那 41 条小节号全被当正文留下，书里多出 41 个
    只写着「1」「2」的孤立段落。页码成串的书这一版仍认不出来（与旧版一致），
    要治得靠"页码总在页末、且每页只有一条"这类版式证据，不在这一层。
    """
    nums: list[int] = []
    pos: list[int] = []
    for i, raw in enumerate(lines):
        s = squeeze(_strip_site_prefix(strip_zero_width(raw)))
        if not _BARE_NUMBER_LINE.match(s):
            continue
        n = cn_to_int(s)
        if n is None:
            continue
        nums.append(n)
        pos.append(i)
    if len(nums) < _BARE_NUMBER_MIN_LINES:
        return False
    run = best = 1
    for prev, cur in zip(nums, nums[1:]):
        run = run + 1 if cur > prev else 1
        best = max(best, run)
    if best < _BARE_NUMBER_MIN_LINES:
        return False
    if (pos[-1] - pos[0]) / (len(pos) - 1) < _BARE_NUMBER_MIN_GAP:
        return False
    return True


# 短篇集：整本一条章节标记都没有时（诗文集、论坛体、短篇合集），故事标题
# 只能靠**排版**认 —— 深缩进、前后都是空行、整行只有一个短标签。
# 实测《书X》：故事标题缩进 12~16 个全角空格，正文段落一律缩进 2，
# 判据命中 7 条（6 个故事标题 + 「（上）」「（下）」「【全文完】」这类分篇标记）。
_STORY_TITLE_MAX = 20          # 标题正文最长字数
_STORY_TITLE_MIN_INDENT = 6    # 绝对下限：比正文段首缩进（通常 2）深得多
_STORY_TITLE_INDENT_GAP = 4    # 相对下限：还要比全书非空行缩进的众数深这么多
_STORY_TITLE_MIN_COUNT = 2     # 至少两条才算"分篇"，一条只是排版失误
_STORY_TITLE_MIN_GAP = 10      # 候选之间平均至少隔这么多非空行
# 整行被括号包住、内文是这些词的，是"完结标记"不是标题（「【全文完】」）。
# 「（上）」「（下）」不在此列：它们标的是同一篇的上下部，算分篇。
_STORY_TITLE_END_WORDS = frozenset(
    {"全文完", "完", "全书完", "未完待续", "待续", "结束", "终", "完结"}
)


def _lead_width(raw: str) -> int:
    """行首缩进的宽度（全角空格、半角空格、制表符都按一个字符算）。"""
    n = 0
    for ch in raw:
        if ch in " \t\u3000":
            n += 1
        else:
            break
    return n


def _story_title_lines(lines: list[str]) -> list[int]:
    """整本没有章节标记时，找出短篇集的分篇标题行（返回原始行下标）。

    只在 clean_text 里"整本一条标题都没认出来"时才调用 —— 有章节结构的书
    不掺和这套判据，深缩进的短行在那种书里多半是诗词引文或排版噪音。

    判据（四条同时成立）：
      1. 整行前后都是空行（独占一段）；
      2. 缩进比全书非空行的缩进众数深 _STORY_TITLE_INDENT_GAP 以上，
         且不低于 _STORY_TITLE_MIN_INDENT —— 正文段首缩进 2，标题缩进 12~16；
      3. 整行只有 _STORY_TITLE_MAX 字以内，且不以标点收尾（标题是标签不是句子）；
      4. 不是分隔线、不是「【全文完】」这类完结标记。
    再加两道**书级**闸门，防"整本每行都是短行"的诗集被切成一堆空章节：
      5. 候选至少 _STORY_TITLE_MIN_COUNT 条；
      6. 候选不超过非空行的三分之一，且平均间隔不少于 _STORY_TITLE_MIN_GAP 行。
    """
    body = [i for i, raw in enumerate(lines) if raw.strip()]
    if len(body) < _STORY_TITLE_MIN_COUNT * _STORY_TITLE_MIN_GAP:
        return []
    indent_mode = Counter(_lead_width(lines[i]) for i in body).most_common(1)[0][0]
    threshold = max(_STORY_TITLE_MIN_INDENT, indent_mode + _STORY_TITLE_INDENT_GAP)
    cands: list[int] = []
    for i in body:
        raw = lines[i]
        if i > 0 and lines[i - 1].strip():
            continue
        if i + 1 < len(lines) and lines[i + 1].strip():
            continue
        if _lead_width(raw) < threshold:
            continue
        text = squeeze(strip_zero_width(raw))
        if not text or len(text) > _STORY_TITLE_MAX:
            continue
        if text[-1] in "。！？…，、；：":
            continue
        if _is_separator_line(text):
            continue
        if text.strip("【】（）()〔〕[]\u3000 ") in _STORY_TITLE_END_WORDS:
            continue
        cands.append(i)
    if len(cands) < _STORY_TITLE_MIN_COUNT:
        return []
    # 候选不能超过非空行的三分之一：诗集、格言集每一行都是"短行 + 前后空行"，
    # 全线命中；短篇集里标题只占极少数，正文段落才是大多数。
    if len(cands) * 3 > len(body):
        return []
    if (cands[-1] - cands[0]) / (len(cands) - 1) < _STORY_TITLE_MIN_GAP:
        return []
    return cands


def _heading_of(
    raw: str,
    text: str,
    *,
    allow_bracket_section: bool = True,
    allow_numbered_section: bool = True,
) -> model.Heading | None:
    """认章节标题；分隔线之类的弱信号在这里就被挡掉。

    为什么不直接用 model.parse_heading() 的结论：
    它必须对体检报告负责（"这行像标题"），清洗则要对切章负责（"就在这切"）。
    两件事的代价不一样 —— 误判一个分隔线会把一章切成几十个空章，
    所以清洗这边额外挡掉弱家族。挡住的行退回当正文/空行处理。

    两个入参分工不同，别合并：
      raw  —— 已去零宽的**原始行**。成句守卫必须看它：规范化会删掉行内空白，
              "第三章 你是谁？" 变成 "第三章你是谁？"，编号后的分隔符没了，
              一整批合法标题会被当正文吞掉。
      text —— normalize_line 之后的行，用于长度守卫与标题抽取值。
    """
    # 站点水印前缀（"正文 第000章：楔子"）会让 parse_heading 的行首锚点挂空，
    # 先剥再解析。_looks_like_sentence 仍看 raw —— 前缀本身常是"这不是标题"的证据
    # （"他说第三章…"剥完也像标题，但整行以句号收尾，那道守卫会把它挡掉）。
    core = _strip_site_prefix(text)
    # 分部结束标记（「第一部（完）」）不是标题，见 _PART_END_MARKER。
    if _is_part_end_marker(text):
        return None
    h = model.parse_heading(core)
    if h is None:
        return None
    if h.family in NOT_A_CHAPTER_FAMILY:
        return None
    # 括号分节要过书级闸门（见 _bracket_section_is_structure）：不通过就当正文。
    if h.family == _BRACKET_FAMILY and not allow_bracket_section:
        return None
    if h.family == _NUM_SECTION_FAMILY and not allow_numbered_section:
        return None
    # 裸数字行（「2」）不在这里挡：它要不要算章节取决于**整本书**有没有成串的
    # 编号（见 _bare_number_is_structure），单行视野看不出来，闸门在 _scan 里。
    # 长度守卫：一整行 4000 字、"第1章"只是它开头的三个字时，这不是标题，
    # 是"整本糊成一行"。判成标题的后果是整本书变成一个 4000 字标题的空章节。
    if len(squeeze(core)) > MAX_TITLE_CHARS:
        return None
    # 以逗号/顿号/分号收尾的整行不可能是标题：标题是一个完整的标签，半句话才是
    # 被折行切断的正文。这条挡住的是"词前缀 + 冒号"这一族 —— 判据"编号后面紧跟
    # 分隔符就是标题"是为「第三章 你是谁？」设计的（空格把编号与标题切开），可
    # 「前言：本人是第一次写小黄文，昨天因为时间有限，」的冒号后面接的是散文。
    # 实测《书寅》：作者在每章开头写「前言：…」，这些行以逗号收尾，却按
    # 上面的分隔符判据成了标题，把上一章整个正文抢走 —— '第二章、美人中计'
    # 成了 0 段的空章，5 个真章节的正文全挂到了「前言」底下。
    # 先剥掉收尾的分隔符再判：「第六章、」这种"编号 + 顿号、标题正文为空"是
    # 合法标题（实测《书子》六~八章就是这个写法），剥完剩「第六章」，
    # 末字是「章」不是逗号。真正要挡的是「前言：…时间有限，」这种半句话收尾。
    bare = squeeze(core).rstrip(_TITLE_SEPARATORS)
    if bare[-1:] in "，、；,;":
        # 例外：**带编号**且标题正文很短的，仍算标题 —— 作者在标题末尾顺手打了
        # 逗号是常事（实测「第02章·美丽人妻的轮奸内射飨宴，」「Chapter 3 Ending，」
        # 都是真标题）。带编号的行有明确的结构锚点，标题正文只有十几个字，
        # 冒号/逗号后面接不上半句散文；而「第3章勾引学生的美足仓库的事件后没过
        # 两天，陆绍辉和孟华阳两人」这种，标题正文二十多字，是被折行切断的正文。
        # 词前缀家族（前言/后记…）不享受这条例外：它们的散文尾巴同样短。
        if h.number is None or len(bare.rstrip("，、；,;")) > _COMMA_TITLE_MAX:
            return None
    # 词前缀（前言/后记/序言…）后面跟的"标题正文"必须很短才算标题。
    # 这类前缀不带编号，"编号后紧跟分隔符就是标题"那条判据在它们身上没有依据 ——
    # 冒号后面可能是标题（「序章：雪夜」），也可能是散文（「前言：上一章乃是过渡，
    # 肉戏只是小菜…」，作者在每章开头的创作谈）。长度是唯一可靠的区分：标题正文
    # 十几个字顶天了，散文一行三四十字还被折行切在半句上。
    m_word = _WORD_PREFIX.match(core)
    if m_word is not None:
        rest = core[m_word.end():].lstrip(_TITLE_SEPARATORS)
        if len(squeeze(rest)) > _WORD_PREFIX_TAIL_MAX:
            return None
    # 成句守卫：标题不是句子。正文里"第一章里写得很清楚。"这类行会命中
    # 第七章家族的正则（"第一章"打头），判成标题就把一章从中间切成两个空章。
    # 宁可漏认一个怪标题，也不能把正文切成空章节：前者只是少一层标题，
    # 后者是丢正文结构，而且丢得无声无息。
    if _looks_like_sentence(
        raw, section_core=core if h.family == _NUM_SECTION_FAMILY else None
    ):
        return None
    # 一行里出现两个以上章节编号 → 这不是标题，是插图说明/正文引用。
    # 反例（《书G》）：插图里断行断坏的「l第一幕第二幕第三幕」，前缀 "l" 被当成
    # 站点水印剥掉，剩下「第一幕第二幕第三幕」正好命中第N章类，于是这一行成了
    # 一章的标题、还吞掉后面 64 段正文。一个章节标题只标一个编号，两个就是引用。
    # 数的是**剥过前缀之后**的文本：卷前缀 + 章号同行的那种标题
    # （「第一卷:飞来的艳福 第01章 鸿运赌坊」）天然带两个编号，
    # 拿原文数会把它误杀，而它剥完前缀只剩一个章号。
    if len(_HEADING_NUM_ANY.findall(core)) >= 2:
        return None
    return h


# ---------------------------------------------------------------------------
# 三、目录块
# ---------------------------------------------------------------------------

# 目录条目和正文标题，同一章常常写成两个样子：
#   目录：第一章 起始 ........ 10       正文：第一章 起始
#   目录：第一章 起始                    正文：第一章：起始
#   目录：【第一章 起始】                正文：第一章 起始
#   目录：第1章 起始                     正文：第一章 起始
# 拿 squeeze 后的原文当键，上面每一对都配不上，于是目录条目"只出现一次"，
# 被规则 3 当正文标题留下 —— 结果是一堆 0 段落的空章节。所以比较前先归一。

# 行尾的点线与页码（"...... 10"、"···12"、"…… 7"、"----10"）。
# 页码前面的点线可能缺失（目录直接写 "第1章 开始 10"），所以点线部分是"可省"的；
# 末尾必须带数字才敢砍，否则"第一卷 上"这种以数字结尾的书名会被砍掉尾巴。
# 前面要求至少一个分隔符（点线、空白、冒号），才能把"标题正文自己以数字结尾"
# 和"页码"分开 —— 否则"第10章"里的 10 会被当成页码砍掉。
_TOC_PAGE_TAIL = re.compile(
    r"(?:"
    r"(?:[\s.．·…:：\-—_]+[0-9０-９]{1,5})+"       # ...... 10 / … 7 / - 12
    r"|第\s*[0-9０-９]{1,5}\s*页"                   # 第 10 页
    r"|[Pp]\s*\.?\s*[0-9０-９]{1,5}"                # P10 / p.10
    r"|[（(【\[]\s*[0-9０-９]{1,5}\s*[)）】\]]"       # （10）/ [10]
    r")\s*$"
)

# 装饰括号：目录里常见的【第一章 起始】、《》、"" 等。只在两端剥。
_TOC_DECOR = "【】[]（）()《》〈〉「」『』\"'“”‘’"

# 半角括号与全角括号是同一件事，只是输入法不同：目录写 "(上)"、正文写 "（上）"
# 时两边的键必须相等。只在**键**上统一，不动正文里的原文。
_TOC_BRACKETS = str.maketrans(
    {
        "(": "（",
        ")": "）",
        "[": "【",
        "]": "】",
        "〈": "《",
        "〉": "》",
        "「": "“",
        "」": "”",
        "『": "‘",
        "』": "’",
    }
)

# 编号与标题之间的分隔符：目录写空格、正文写冒号，是最常见的不一致。
# 注意不剥空格本身（squeeze 已经剥了），这里剥的是"有语义但两边不一致"的符号。
_TOC_GLUE = "：:·—－-、，,"

# 装饰括号的配对表，_strip_decor 用
_DECOR_PAIRS = {"（": "）", "【": "】", "《": "》", "“": "”", "‘": "’"}


def _strip_decor(s: str) -> str:
    """剥掉两端的装饰括号。**成对才剥**，落单的残渣另外清掉。

    str.strip("()") 是按字符剥的，成对时看不出问题，不配对时就会出错：
    '第十二章归途（上' 这种只有左括号的行，按字符剥虽然不动它，但配上
    正文里同名的 '第十二章归途（上）'（两边都成对）就永远配不上 —— 一边剥成
    '第十二章归途（上'，另一边剥成 '第十二章归途'。所以先处理成对的括号，
    最后再把落单的单个括号清掉，两边才会收敛到同一个键。
    """
    changed = True
    while changed and len(s) >= 2:
        changed = False
        for left, right in _DECOR_PAIRS.items():
            if s[0] == left and s[-1] == right:
                s = s[1:-1]
                changed = True
                break
    return s.strip("".join(_DECOR_PAIRS) + "".join(_DECOR_PAIRS.values()))


def toc_key(text: str) -> str:
    """把一行标题归一成"目录条目和正文标题能配得上"的键。

    顺序有讲究：
      1. 先 squeeze 抹掉所有空白与零宽字符（两边排版不同，这是最大噪声源）；
      2. 砍掉行尾页码（必须在剥装饰括号之前，
         "【第一章 起始】....10" 的页码在括号外面）；
      3. 剥掉两端的装饰括号，再抹掉中间的分隔符；
      4. 把编号统一成阿拉伯数字：目录写"第一章"、正文写"第1章"是常见混排，
         不归一就永远配不上；
      5. 括号造型统一（半角/全角是同一件事）。
    """
    # 页码必须在 squeeze **之前**砍：squeeze 会把 "标题 10" 压成 "标题10"，
    # 之后再想区分"行尾页码"和"标题正文自己以数字结尾"就没有分隔符可依了。
    s = _TOC_PAGE_TAIL.sub("", strip_zero_width(text).strip())
    s = squeeze(s)
    # 括号造型必须先统一再剥：'第1章归途(上)' 与 '第1章归途（上）' 是同一条，
    # 不先统一，一边剥成 '第1章归途'、另一边留着括号，键永远配不上。
    s = s.translate(_TOC_BRACKETS)
    s = _strip_decor(s)
    for ch in _TOC_GLUE:
        s = s.replace(ch, "")
    s = _strip_decor(s)
    return _normalize_units(_normalize_numbers(s))


def _toc_key_line(raw: str) -> str:
    """一行的目录比对键：先剥站点水印前缀，再走 toc_key。

    为什么必须剥前缀：目录条目是干净的「第一章　标题」，正文里那一份却常被
    站点刷上水印（「正文　第一章　标题」）。两边都用 toc_key(raw) 算，键永远
    对不上 —— 块级闸门看到"簇外一个同名都没有"，整块目录一条都不删，几十条
    目录变成几十个空章；而第二遍清洗时前缀已在渲染标题时剥掉了，闸门又能
    放行，于是同一份文本洗两遍得到两个结构（幂等破）。

    正文行不受影响：_strip_site_prefix 开头就判 model.parse_heading，
    行首解析不出标题就原样返回；能解析出来的行剥了前缀，正是我们想要的。
    """
    return toc_key(_strip_site_prefix(raw))


# "第 + 数字" 里的中文数字，归一成阿拉伯数字。只处理章节编号这一种形态，
# 不碰标题正文里的数字（"一百零八将"不该变成"108将"）。
_TOC_NUMBER = re.compile(r"第([0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,10})")


def _normalize_numbers(s: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        v = cn_to_int(m.group(1))
        return f"第{v}" if v is not None else m.group(0)

    return _TOC_NUMBER.sub(repl, s)


# 章节量词归一：目录写「第5回」、正文写「第5章」是同一章。量词不参与比较。
_TOC_UNIT = re.compile(r"第([0-9]{1,10})\s*[卷部集篇章回节幕话話]")


def _normalize_units(s: str) -> str:
    """抹掉章节编号后面的量词（第5回 == 第5章）。**必须在 _normalize_numbers 之后调**：

    中文数字只有归一成阿拉伯数字，量词才落在「数字 + 量词」这个固定形态上。
    """
    return _TOC_UNIT.sub(r"第\1", s)


# 页码行：两行式目录的第二行（「…… 10」）、单独一行的「第 10 页」「P10」「（10）」。
# 它们既不是标题也不是正文，是排版噪声：判定「后面跟的是什么」时跳过，
# 落在目录区里时一起删掉。
_PAGE_NOISE = re.compile(
    r"^(?:"
    r"[.．·…\-—_、\s]*[0-9０-９]{1,5}[.．·…\-—_、\s]*"   # ...... 10 / ···12
    r"|第\s*[0-9０-９]{1,5}\s*页"
    r"|[Pp]\s*\.?\s*[0-9０-９]{1,5}"
    r"|[（(【\[]\s*[0-9０-９]{1,5}\s*[)）】\]]"
    r"|[.．·…\-—_]{2,}"
    r")$"
)


def _is_page_noise(text: str) -> bool:
    """这一行是不是目录里的页码/点线噪声（不是标题，也不是正文）。"""
    return bool(_PAGE_NOISE.match(squeeze(strip_zero_width(text))))


# 无「第」的卷容器行：「卷一 风起」「上卷」「中篇」。它们既不是章节标题
# （model.parse_heading 要求「第N卷」），也不是正文 —— 是排版上的分区标记。
# 判「后面跟的是正文还是又一个标题」时跳过它们，否则目录最后一条后面跟着
# 这样一行，就会被误判成「后面是正文」而让目录区提前收尾，最后一条目录
# 直接留成全书第一个空章节。
_VOLUME_CONTAINER = re.compile(
    r"^(?:"
    r"[上中下]\s*[卷部篇集]"                                  # 上卷 / 中篇
    r"|卷\s*[0-9０-９〇零一二三四五六七八九十百壹贰貳叁參肆伍陆陸柒捌玖拾]{1,3}"   # 卷一 / 卷1
    r"|卷\s*之\s*[0-9０-９〇零一二三四五六七八九十百壹贰貳叁參肆伍陆陸柒捌玖拾]{1,3}"  # 卷之一
    r")"
)


def _is_volume_container(text: str) -> bool:
    """这一行是不是无编号的卷/部/篇分区标记（不是标题，也不是正文）。"""
    s = squeeze(strip_zero_width(text))
    if not s or len(s) > 20:
        return False
    # 带句末标点的就不是分区标记，是正文（"卷三的箱子很重。"）
    if s[-1] in TERMINAL:
        return False
    return bool(_VOLUME_CONTAINER.match(s))




# 从卷容器行里抽卷序号的捕获组。只认阿拉伯与中文数字（含大写），
# 「上卷/中篇」抽不出序号，返回 None —— 抽不到不影响切章，只影响序号校验。
_VOLUME_NUM = re.compile(
    r"^(?:\u5377\s*\u4e4b?\s*([0-9\uff10-\uff19\u3007\u96f6\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u58f1\u8d30\u8cb3\u53c1\u53c3\u8086\u4f0d\u9646\u9678\u67d2\u634c\u7396\u62fe]{1,3}))"
)


def _vol_title_number(text: str) -> int | None:
    """从卷容器行里抽卷序号：卷之一 → 1，卷三 → 3，上卷 → None。"""
    m = _VOLUME_NUM.match(squeeze(strip_zero_width(text)))
    if m is None:
        return None
    return cn_to_int(m.group(1))


def _title_follows(items: list["_Line"], i: int) -> bool:
    """第 i 行之后第一个有效行是不是章节标题。

    这是把无编号卷容器行升级成卷标题的唯一依据，别省。
    「卷一 风起」单独出现时八成是排版分区标记，但正文里也可能出现
    「上卷」这种孤零零的短段。区分办法是看它后面紧跟什么：
    卷标题后面一定是章标题，正文段落后面一定还是正文。
    """
    for k in range(i + 1, len(items)):
        it = items[k]
        if it.blank or _is_page_noise(it.raw):
            continue
        return it.is_title
    return False


# 卷容器行冒充卷标题时用的家族名。它不在诊断层的家族表里 ——
# 诊断层（M1 体检）认不出这种行是正常的：体检只回答「这行像不像章节」，
# 「卷之一」确实不像。清洗层这边有上下文（后面紧跟章标题）才敢认。
_VOLUME_FAMILY = "卷容器"


def _promote_volume_lines(items: list["_Line"]) -> list[_Line]:
    """把「卷一 风起」这类无编号卷容器行升级成 level=2 的卷标题。

    为什么要升级：这些行既不满足「第N卷」的标题形态，也不是正文，
    原来被当正文段落收下，于是《书A》里四条「卷之一 觉后禅——春」
    全都粘在前一章的最后一段末尾，整本书的卷结构在章节树里彻底消失。
    它们本来就是卷级标题，只是编号写法特殊（不带「第」）。

    升级条件（两条同时满足，缺一不可）：
      1. 形态是卷容器（_is_volume_container）；
      2. 后面第一个有效行是章节标题（_title_follows）。
    第 2 条是安全阀：正文里冒出来的「上卷」「中篇」后面跟的是正文，
    不该被升级成卷标题、把一章从中间切开。

    必须在删目录之后调：目录条目里也有「卷一」这种行，那些要删掉，
    不该升级成章节。
    """
    for i, it in enumerate(items):
        if it.is_title or it.blank or it.ad is not None:
            continue
        # 硬折行碎片不许升级成卷标题：它是正文中间被切断的半句，
        # 「第一卷……」这种开头恰恰是它最常撞上的形态，升级上去等于
        # 把刚拦下来的假章换个门又放回章节树里。
        if it.wrap_frag:
            continue
        # 人工保护的行同样不许升级：它的结构已经由用户定死了（见 _Line.frozen）。
        # 防御性闸门，当前走不到 —— 被冻住的行只有两种：目录标记行（「目录」，
        # 不满足 _is_volume_container）与原先的目录条目（_scan 已认成章节标题，
        # 同样不满足）。留着是因为它守的是"结构由用户定死"这条不变量，
        # 以后 _is_volume_container 放宽就可能踩到。
        if it.frozen:
            continue
        if not _is_volume_container(it.raw):
            continue
        if not _title_follows(items, i):
            continue
        it.heading = model.Heading(
            number=_vol_title_number(it.raw), level=2, family=_VOLUME_FAMILY
        )
        # 升级成标题之后，也要按"标题"的口径重算一次文本：_scan 那时还认不出
        # 它是标题，走的是正文口径，行内空白已经被删掉了（"卷之一 觉后禅" →
        # "卷之一觉后禅"）。卷标题跟章节标题同样是排版标记，空白要留。
        it.text = normalize_line(it.raw, keep_inline_space=True).text
    return items


def _has_body_after(items: list[_Line], i: int) -> bool:
    """第 i 行之后，第一个有效行是不是正文段落（而不是又一个标题）。

    这是区分「目录条目」和「正文标题」唯一不依赖重复性的证据：
      目录条目的下一行   → 还是目录条目（标题）
      正文第一章的标题   → 下一行是正文
    空行和页码行不算证据，跳过。扫到没有下文（书在这儿结束）也算 False ——
    目录写在书末时，最后一条后面什么都没有，它同样是目录条目。
    """
    for k in range(i + 1, len(items)):
        it = items[k]
        if it.blank or _is_page_noise(it.raw) or _is_volume_container(it.raw):
            continue
        return not it.is_title
    return False



# 目录块向两侧延伸时允许吞进来的卷标题形态：第N集 / 第N卷 / 第N部。
# 《书H》的目录里每条卷标题都带书名（「书H·第三集 大围剿」），
# 所以用 search 而不是 match，前面允许有书名。
_TOC_VOLUME_TITLE = re.compile(
    r"第\s*[0-9０-９〇零一二三四五六七八九十百]{1,4}\s*[卷部集篇]"
)
# 卷名续行（「书H· 第二集」的次行「都中风云」）的最大长度。
_TOC_SUBTITLE_MAX = 12
# 目录块延伸的硬上限（条目数）。防止一本通篇短标题的书被一口吞掉。
_TOC_EXPAND_MAX = 400


def _looks_like_toc_volume(text: str) -> bool:
    """这一行像不像目录里的卷标题（「第N集/卷/部」，可能带书名前缀）。"""
    s = squeeze(_strip_site_prefix(text))
    if not s or len(s) > 30:
        return False
    return bool(_TOC_VOLUME_TITLE.search(s))


def _toc_keys_of(text: str) -> set[str]:
    """一行标题的「同名校验键」候选集合。

    为什么要多个：正文标题常把卷前缀和章号写在同一行（《书H》正文是
    「第一集 花劫 第一回 天作奇缘」，目录里只有「第一回 天作奇缘」），
    按原样算键永远对不上，目录条目一条都延伸不出来。剥掉卷前缀之后再算
    一次，两边就同键了。
    """
    keys = {toc_key(text)}
    # _strip_site_prefix 自己就会剥一层卷前缀，所以剥完的结果无论有没有
    # 再剥一层卷前缀，都要算一个键。
    core = _strip_site_prefix(text)
    keys.add(toc_key(core))
    vol = _strip_volume_prefix(core)
    if vol is not None:
        keys.add(toc_key(vol))
    keys.discard("")
    return keys


def _prev_is_toc_volume(items: list["_Line"], i: int) -> bool:
    """第 i 行之前第一个有效行是不是卷标题。

    卷名续行（「书H· 第二集」下一行的「都中风云」）是卷标题的一部分，
    但它本身不是标题、也没有同名，只能靠这个位置关系认出来。
    不能只看「短、不成句、后面跟着标题」—— 扉页的作者行（书A 的
    「明·某某」）同样短、同样后面跟着回目，会被一起删掉。
    """
    for k in range(i - 1, -1, -1):
        it = items[k]
        if it.blank or _is_page_noise(it.raw):
            continue
        return _looks_like_toc_volume(it.raw)
    return False


def _title_key_pos(items: list["_Line"]) -> dict[str, list[int]]:
    """标题键 → 行下标列表。延伸与补洞都要按「后面还有没有同名」判，共用一份。"""
    pos: dict[str, list[int]] = {}
    for k, it in enumerate(items):
        if it.is_title:
            for key in _toc_keys_of(it.raw):
                pos.setdefault(key, []).append(k)
    return pos


def _expand_toc_region(
    items: list["_Line"],
    region: list[int],
    key_pos: dict[str, list[int]],
    *,
    forward: bool,
) -> None:
    """把目录区沿标题行向两侧延伸，直到碰到正文为止。

    为什么必须延伸：detect_toc 只报**最长的那一簇**。《书H》的目录每十回
    被一条卷标题行断开（「书H·第三集 大围剿」），最长的一簇只有 19 条，
    于是 100 多条目录留在书里变成一串 0 段落的空章节。更糟的是它不幂等：
    第一遍删掉 19 条之后，第二遍的簇换了位置，删的是另外 19 条 —— 洗两遍
    得到两个结果。

    单行安全阀：目录条目的定义是「正文里还有一份」，所以只吞**在文件后面
    还能找到同名行**的标题。正文自己的标题只出现一次，走到它就停。
    卷标题行与卷名续行本来就没有同名，按形态放行 —— 它们在目录里是分节
    标记，在正文里后面跟的是正文，碰不到（延伸一碰到正文就收尾）。
    """
    if forward:
        i = region[-1] + 1
        limit = region[0] + _TOC_EXPAND_MAX
        while i < len(items) and i <= limit:
            it = items[i]
            if it.blank or _is_page_noise(it.raw):
                i += 1
                continue
            if it.is_title:
                later = any(
                    j > i for key in _toc_keys_of(it.raw) for j in key_pos.get(key, ())
                )
                if later or _looks_like_toc_volume(it.raw):
                    region.append(i)
                    i += 1
                    continue
                break
            # 卷名续行：紧跟在卷标题后面、后面第一个有效行还是标题
            if (
                len(squeeze(it.raw)) <= _TOC_SUBTITLE_MAX
                and not _ends_terminal(it.raw)
                and _prev_is_toc_volume(items, i)
                and _title_follows(items, i)
            ):
                region.append(i)
                i += 1
                continue
            break
        return

    i = region[0] - 1
    limit = region[-1] - _TOC_EXPAND_MAX
    while i >= 0 and i >= limit:
        it = items[i]
        if it.blank or _is_page_noise(it.raw):
            i -= 1
            continue
        if it.is_title:
            later = any(
                j > i for key in _toc_keys_of(it.raw) for j in key_pos.get(key, ())
            )
            if later or _looks_like_toc_volume(it.raw):
                region.insert(0, i)
                i -= 1
                continue
            break
        if (
            len(squeeze(it.raw)) <= _TOC_SUBTITLE_MAX
            and not _ends_terminal(it.raw)
            and _prev_is_toc_volume(items, i)
            and _title_follows(items, i)
        ):
            region.insert(0, i)
            i -= 1
            continue
        break


def _fill_toc_region_gaps(
    items: list["_Line"], region: list[int], key_pos: dict[str, list[int]]
) -> None:
    """把目录区**内部**漏掉的行也收进来（卷标题、卷名续行）。

    簇是按 match_chapter 认出来的，而 match_chapter 要求行首就是「第N卷」。
    《书H》的卷标题写成「书H·第三集 大围剿」，书名前缀让它不进簇，
    于是它夹在两段回目中间：往前延伸、往后延伸都从簇的两端出发，谁也扫不到它。
    留着这一行就是一个把后面 700 多段正文全吞进去的卷章节。

    内部的行只按形态收（同名在后的标题 / 卷标题 / 卷名续行），不按方向。
    """
    inside = set(region)
    lo, hi = region[0], region[-1]
    for k in range(lo, hi + 1):
        if k in inside:
            continue
        it = items[k]
        if it.blank or _is_page_noise(it.raw):
            continue
        if it.is_title:
            later = any(
                j > k for key in _toc_keys_of(it.raw) for j in key_pos.get(key, ())
            )
            if later or _looks_like_toc_volume(it.raw):
                region.append(k)
            continue
        if (
            len(squeeze(it.raw)) <= _TOC_SUBTITLE_MAX
            and not _ends_terminal(it.raw)
            and _prev_is_toc_volume(items, k)
            and _title_follows(items, k)
        ):
            region.append(k)
    region.sort()


def _cluster_ends_at_separator(items: list[_Line], cluster: list[int]) -> bool:
    """这一簇标题是不是**紧接在场景分隔线之前**结束的。

    是的话，这簇就是整份目录：分隔线是"目录到此为止"的分界，正文在它之后。
    没有这条，目录最后一条会被当成"正文第一章的标题"留下来 —— 它后面紧跟
    的不是标题而是正文散文，正好命中 region 循环里的规则 a。实测《白纸——圣魔
    邪灵》：49 条目录删掉 48 条，剩下那一条顶着第 49 章的名字，把全书开头的
    正文吞进自己肚子里。

    只看簇**之后**的第一条有效行，空行与页码行跳过：目录末尾常留一两个空行。
    """
    for k in range(cluster[-1] + 1, len(items)):
        it = items[k]
        if it.blank or _is_page_noise(it.raw):
            continue
        return it.sep
    return False


def _plan_toc_drop(
    items: list[_Line], toc: dict, *, has_toc_marker: bool = False
) -> tuple[set[int], int | None, bool]:
    """算出目录块里该删哪些行、保留的最后一行在哪、是否「疑似目录却没敢删」。

    目录块最难的地方不是「认出它是目录」，而是**它的最后一行往往就是
    正文的第一章标题**：目录写完，正文紧接着开始，中间常常只隔一个空行。
    体检的 detect_toc 会把这一行也吸进簇里（因为它和前面的标题挨得近）。
    所以光看「在簇里」不能当删除依据，判据分两层：

      1) 块级闸门：这一簇到底是不是目录（见下面的 matched 与 has_toc_marker）；
      2) 簇内逐行分类：从第一行往后扫，**目录区**在遇到正文区第一行时结束。
         正文区的第一行满足二选一：
           a. 它后面紧跟的不是标题、而是正文段落；
           b. 它的键在目录区里已经出现过，且它与上一条目录之间隔了空行
              （一份目录不会把同一条列两遍；隔了空行又撞键，说明目录完了、
               正文开始了 —— 典型形态是「目录末尾 + 正文的卷标题」）。
         目录区一律删，正文区一律留。夹在中间的页码行（「…… 10」「第 10 页」
         「P10」）不是标题也不是正文，落在目录区范围里就一起删掉。

    为什么不能只看「簇外有没有同名」逐行判：正文那份标题自己也常被吸进簇，
    簇外计数就归零了（一本书只有一个「第1章」，簇里一份、簇外没有）。
    逐行判的另一个后果是：只在目录里出现的条目（卷名、下册重编号）永远删不掉。

    返回 (要删的行下标集合, 保留的最后一行的 1 基行号或 None, 是否疑似目录却没删)。
    """
    if not toc.get("suspected"):
        return set(), None, False
    # cluster_lines 是**原始文件的 1 基行号**，而 items 已经删过广告行与目录标记行，
    # 下标和行号早就错位了。必须按 it.no 反查下标：直接当 0 基下标用，会漏掉
    # 前面每一条被删掉的广告行，整块簇平移几十行，删到完全无关的正文上。
    by_no = {it.no: i for i, it in enumerate(items)}
    cluster = [by_no[n] for n in toc.get("cluster_lines") or [] if n in by_no]

    # 体检判「像标题」用的是 diagnose.match_chapter，它只看正则，不管这一行
    # 是不是成句的正文。清洗这边有一道额外的成句守卫（_heading_of），会把
    # 「第3章里有话说。」这类正文行挡在标题之外。于是两边对「簇里有几条标题」
    # 的认知不一致：正文行凑数也能把簇撑到 5 条，体检报「疑似目录」。
    # 这里按清洗自己的判定重新过滤一遍，并用同一个门槛（5 条）复核。
    cluster = [i for i in cluster if items[i].is_title]
    if len(cluster) < 5:
        return set(), None, False

    in_cluster = set(cluster)
    # 键要用**原始行**算：toc_key 要先按原文砍行尾页码，而 it.text 已经被
    # 规范化删掉了行内空白，「第1章 标题 10」会变成「第1章标题10」，页码砍不掉。
    # 还要先剥站点水印前缀（见 _toc_key_line）：正文那份标题常被刷上
    # 「正文　」之类的站点前缀，不剥就永远配不上目录条目。
    outside = Counter(
        _toc_key_line(it.raw) for i, it in enumerate(items) if i not in in_cluster
    )

    distinct = {_toc_key_line(items[i].raw) for i in cluster}
    distinct.discard("")
    matched = sum(1 for k in distinct if outside.get(k, 0) >= 1)

    # 块级闸门：把「目录条目的定义是正文里还有一份」这条性质从逐行推广到整块。
    #
    # 只看单行会出事。一本没有目录的书，正文里几个同名标题（三个「番外」）
    # 只要挨得够近，就会被体检圈成一簇，其中一份因为「簇外还有同名」被删掉
    # —— 整章消失。更麻烦的是这错会自我放大：第一遍清洗出的干净文本里空行
    # 被压缩、标题挨得更近，第二遍再清洗时又少一章。干净文本喂回去洗一遍本来
    # 应该什么都不改，这是清洗器的基本承诺（幂等）。
    #
    # 两条放行条件，各自够硬：
    #   ① 簇内**互不相同的键**里至少 2 个在簇外找得到同名行。
    #      为什么是 2 个不同的键而不是比例：同一个章节名出现多次（三个「番外」）
    #      只会让 matched 停在 1 —— distinct 去重后本来就是一个键，这种书没有
    #      目录，matched=1 就该拦下。
    #   ② 文件里真的有一行「目录」，且簇内至少有 4 个互不相同的条目。
    #      这条是给「目录残缺」准备的：目录列了 5 章、正文只有 1 章能对上时
    #      matched 上不去（唯一那份正文标题自己也被吸进了簇），但一行「目录」
    #      本身就足以证明这本书有目录。
    # 没有「目录」标记的书只能走①：正文里几个挨得近的重名标题不是目录。
    if not (matched >= 2 or (has_toc_marker and len(distinct) >= 4)):
        return set(), None, False

    # 每一行「后面紧跟的那个有效行，是不是标题」。
    # 空行与页码行（两行式目录的「…… 10」）不算证据：把它们当成「后面是正文」
    # 的证据，整块两行式目录会被判成正文，一行都删不掉，而且是静默失败
    # （报告里只剩「目录」标记行被删，看起来像这本书没有目录）。
    next_is_title = [False] * len(items)
    seen_title = False
    for i in range(len(items) - 1, -1, -1):
        it = items[i]
        next_is_title[i] = seen_title
        if it.is_title:
            seen_title = True
        elif (
            it.blank
            or _is_page_noise(it.raw)
            or _is_volume_container(it.raw)
            or it.sep
            # 整行广告也是"透明"的：没有人工保护时它根本不在 items 里
            # （_scan 已经删了），这里把它当断点的话，同一本书"保护了这条广告"
            # 和"没保护"会算出两份不同的目录块 —— 实测保护一条夹在目录里的
            # 广告，dropped.toc 从 6 掉到 2，四条目录条目白白留在书里。
            or it.ad_kept
        ):
            # 卷容器行、场景分隔线都不是证据：它们既不能证明「后面是标题」，
            # 也不能证明「后面是正文」，继续往前找真正的证据。
            # 分隔线漏在这里的后果：目录末尾「第8章」+ 分隔线 + 正文「第1章」
            # 这一簇里，第8章后面隔着分隔线才见正文，却被当成"后面是正文"，
            # 于是它成了正文首章，把分隔线一起吞进肚子（实测假数据 9 章）。
            continue
        else:
            seen_title = False

    # 簇紧挨着分隔线结束 => 整簇都是目录，规则 a（"后面不是标题"）不再有效。
    # 规则 b（"键在目录区里出现过、且隔了空行"）照样有效：那是正文重名的硬证据。
    ends_at_sep = _cluster_ends_at_separator(items, cluster)

    region: list[int] = []      # 目录区（全是标题行）
    skipped: list[int] = []     # 簇首被跳过的标题（可能是目录条目，见下）
    seen_keys: set[str] = set()
    body_start: int | None = None
    prev_dir: int | None = None
    for i in cluster:
        key = _toc_key_line(items[i].raw)
        # 与上一条目录之间隔了"空行或分隔线" —— 都算隔开了一段。
        # 分隔线也是硬边界：目录末尾用 ＊＊＊ 收尾、正文紧接着重新列一遍标题
        # （实测《书R》的形态：目录 49 条 + 分隔线 + 正文标题），
        # 只看空行的话这条线索就漏了，最后一条目录会被当成正文首章。
        gap_break = prev_dir is not None and any(
            items[k].blank or items[k].sep for k in range(prev_dir + 1, i)
        )
        # 行尾带页码的行永远是目录条目，不可能是正文的第一行。
        # 反例（《书G》）：目录最后一条「第十九章作家的创造方法\t0」，
        # 紧跟的正文标题是裸「序」，那时 match_chapter 还不认「序」，
        # next_is_title 判 False，于是这条目录条目被当成"正文区起点"留下来，
        # 成了第一章，还把「序」的正文吞进自己肚子里。
        # 页码是目录独有的标记 —— 正文标题后面不会挂一个页码。
        if _TOC_PAGE_TAIL.search(items[i].raw):
            region.append(i)
            if key:
                seen_keys.add(key)
            prev_dir = i
            continue
        if (
            not next_is_title[i] and _has_body_after(items, i) and not ends_at_sep
        ) or (key and key in seen_keys and gap_break):
            if not region:
                # 簇的第一条就已经是正文，说明这一簇不是从目录开头起的 ——
                # 跳过它继续往后找真正的目录区。
                # 《书P》：扉页的裸「序」被吸进了簇首，它后面紧跟序文
                # （正文），按老逻辑当场判定「正文区开始」，整块 61 条回目
                # 一条都删不掉。而且第二遍清洗时簇首换成了回目本身，目录又
                # 删掉了 —— 两遍结果不同，121 行差异。
                skipped.append(i)
                continue
            body_start = i
            break
        region.append(i)
        if key:
            seen_keys.add(key)
        prev_dir = i

    drop: set[int] = set()
    if region:
        # 簇只是目录的一部分：detect_toc 只报最长的那一簇，两侧继续吞标题行。
        # 往后吞的前提是还没撞到正文（body_start 为 None）；往前吞总是安全的，
        # 因为碰到正文（非标题行）就收尾。
        key_pos = _title_key_pos(items)
        _expand_toc_region(items, region, key_pos, forward=body_start is None)
        _expand_toc_region(items, region, key_pos, forward=False)
        _fill_toc_region_gaps(items, region, key_pos)
        # 目录区起点还要往回探：紧挨着目录、行尾带页码、又不是标题的行，
        # 本身就是目录条目 —— 只是它的键（「序 0」）没有被认成标题，
        # 体检的簇没有把它收进来（《书G》目录第一行就是这样，最后残留成
        # front_matter 里的一段「序0」）。
        start = region[0]
        while start > 0:
            prev = items[start - 1]
            if prev.is_title or not _TOC_PAGE_TAIL.search(prev.raw):
                break
            start -= 1
        # 目录区范围内（含正文第一行之前的页码行）整体删掉。
        end = (body_start - 1) if body_start is not None else region[-1]
        region_set = set(region)
        for k in range(start, end + 1):
            if k not in region_set and _is_page_noise(items[k].raw):
                drop.add(k)
        drop.update(range(start, region[0]))   # 回探进来的那几行
        drop.update(region)

        # 簇首被跳过的标题，如果它的键在目录区之后还能找到同名行，它本身就是
        # 目录条目，跟着一起删。《书P》扉页目录把「序」列在最前面，后面
        # 紧跟的却是目录里「书P序」「书P题词」两行文字（不是标题），
        # 于是「序」被判成"正文区起点"跳过 —— 不删的话它会留成一个只有两段
        # 目录文字的空章节，和真正的「序」重名。
        for k in skipped:
            key = _toc_key_line(items[k].raw)
            if key and any(_toc_key_line(it.raw) == key for it in items[region[-1] + 1 :]):
                drop.add(k)

    # 一行真目录条目都没删 => 这块压根不是目录，只是几个挨得近的标题。
    # 但「文件里写了目录、却一条都没敢删」要报出来：不报的话这类漏删在
    # 清洗报告里完全不可见，用户只会看到输出书里有几个空章节。
    unsure = bool(has_toc_marker and not region)
    if not region:
        return set(), None, unsure
    # 报告要的是原始文件的 1 基行号，不是本列表的下标
    kept_last = items[body_start].no if body_start is not None else None
    return drop, kept_last, unsure


# 文末重印目录：一整块标题，每一条在别处都能找到同名标题。
# 常见形态是"目录"写两遍（开头一份、书末一份），或者正文之后附一份
# "目录/章节索引"。它们不是章节，是目录的副本。
_REPRINT_MIN_TITLES = 3


def _plan_reprint_toc_drop(items: list[_Line], *, exclude: set[int] | None = None) -> set[int]:
    """找出文末重印的目录块，返回该删的行下标。

    只靠 detect_toc 不够：它取的是"最长的那一簇"，一本书同时有开头目录和
    文末目录时只会报开头那一份。文末那份因此一遍清洗时被当成一串空章节
    留在书里，第二遍清洗（此时开头目录已被删）才被认出来删掉 —— 于是
    同一份文本洗两遍得到两个结果，清洗器的幂等承诺就破了。

    判据是"整块重名"，不是"单行重名"：
      1. 连续 3 条以上标题，中间只允许夹空行/页码行/卷容器行；
      2. 块内**每一条**的键在块外都存在同名标题。

    为什么必须"整块全部重名"：正文里真有几个挨着写的短章节（三个"番外"）
    是正常的，它们的键在块外没有同名，条件 2 不成立；而一份重印目录的
    每一条都对应正文里已有的章节（目录就是这个意思），无一例外。
    "全部"这条硬要求也正是它不敢乱删的原因 —— 只要有一条对不上，
    就说明这块里混进了正文标题，整块放弃。
    """
    skip = exclude or set()
    blocks: list[list[int]] = []
    cur: list[int] = []
    for i, it in enumerate(items):
        if i in skip:
            continue
        if it.is_title:
            cur.append(i)
        elif it.blank or _is_page_noise(it.raw) or _is_volume_container(it.raw) or it.ad_kept:
            # 空行与编号噪声不打断块：文末目录的条目之间也常夹空行。
            # 整行广告同理 —— 没有人工保护时它已经被 _scan 删掉、不在 items 里，
            # 这里把它当断点会让"保护了一条广告"改变整块目录的识别结果。
            continue
        else:
            if len(cur) >= _REPRINT_MIN_TITLES:
                blocks.append(cur)
            cur = []
    if len(cur) >= _REPRINT_MIN_TITLES:
        blocks.append(cur)

    if not blocks:
        return set()

    drop: set[int] = set()
    for block in blocks:
        keys = [_toc_key_line(items[i].raw) for i in block]
        if any(not k for k in keys):
            continue
        block_set = set(block)
        outside = Counter(
            _toc_key_line(it.raw) for i, it in enumerate(items)
            if it.is_title and i not in block_set
        )
        if all(outside.get(k, 0) >= 1 for k in keys):
            drop.update(block)
    return drop


# ---------------------------------------------------------------------------
# 四、行内夹广告切除
# ---------------------------------------------------------------------------


def _inline_cut_point(text: str) -> int | None:
    """算出行内广告的切点下标（保留部分是 text[:下标]）；算不出返回 None。

    切点 = 广告信号**往前**最后一个句末标点之后，再把紧跟的右引号/右括号
    一起吞掉。后半截为什么要吞：切点落在句末标点之后，紧跟着的右引号其实
    属于**被保留的那句**（'未央生道：“是。”更多精彩……' 里的 ” 是台词的
    收尾），不吞掉就会劈出一个孤零零的左引号。收尾字符表复用
    _TRAILING_CLOSERS，不另立一份。

    句末标点表也用 clean 自己的 _SENTENCE_TAIL（与 diagnose._SENTENCE_END
    同值）：引号、括号**故意不算**句末 —— 它们会出现在广告词内部，
    算进去就会在一句广告中间找切点。
    """
    pos = find_ad_signal(text)
    if pos is None:
        return None
    # 从信号处往前找最后一个句末标点。找不到就没切点 —— 不能从行首一刀切下来，
    # 那样等于把整行交给广告判定，而判定已经说过它是"行内"（有正文残留）。
    k = -1
    for i in range(pos - 1, -1, -1):
        if text[i] in _SENTENCE_TAIL:
            k = i
            break
    if k < 0:
        return None
    k += 1
    while k < len(text) and text[k] in _TRAILING_CLOSERS:
        k += 1
    return k


# 尾巴多短才算"一句广告标签"。真广告尾巴通常不拿句号收尾
# （"更多精彩内容请访问 www.example.com"），拿标点收尾的那些都很短
# （"求收藏，求推荐票！"）。超过这个长度的、还以句末标点收尾的尾巴
# 更可能是一句人写的正文，宁可漏切。
_TAIL_MAX_SHORT = 16

# 切点落在引号中间时用来把右引号要回来。normalize 之后中文引号基本只剩 “”，
# 但原文可能带 〔〕 以外的各种形态，所以把常见几对都列上。
_QUOTE_PAIRS_FULL = (
    ("\u201c", "\u201d"),
    ("\u2018", "\u2019"),
    ("\u300c", "\u300d"),
    ("\u300e", "\u300f"),
    ("\uff02", "\uff02"),
)


def _tail_looks_like_sentence(tail: str) -> bool:
    """切出来的尾巴是不是一句"人写的、以句末标点收尾的、不算短的话"。

    "他停下手。淘宝 www.taobao.com 上买。" 切出来尾巴是"淘宝…上买。"——
    它有句号、有 8 个实字，是一句完整的正文（买了东西）。旧判据只看
    judge_ad_line(尾巴)["whole_line"]，而那种判据会把它当广告碎片放行，
    于是正文后半句被切走。
    """
    s = tail.strip()
    if not s or s[-1] not in _SENTENCE_TAIL:
        return False
    return len(squeeze(s)) > _TAIL_MAX_SHORT


def _balance_cut(kept: str, tail: str) -> tuple[str, str] | None:
    """切点落在引号/括号中间时，把被切走那一侧的右引号要回来。

    '他道：「是。求收藏 www.x.com」' 的切点在"是。"之后，右引号却跟着
    广告尾巴一起被切走，留下一个只有左引号的残句（排版上会一路延伸到
    章末，读者看得出来是 bug）。把尾巴里那个对应的右引号移到 kept 末尾，
    结果就是 '他道：「是。」'—— 一个字没多、一个字没少。

    找不到配对（尾巴里根本没有那个右引号）返回 None，调用方据此拒切。
    """
    for left, right in _QUOTE_PAIRS_FULL:
        if left == right:
            continue
        for _ in range(kept.count(left) - kept.count(right)):
            idx = tail.find(right)
            if idx < 0:
                return None
            kept += right
            tail = tail[:idx] + tail[idx + 1 :]
    return kept, tail


def cut_inline_ad(text: str) -> tuple[str, str] | None:
    """尝试切掉一行尾巴上的广告，返回 (保留正文, 切掉的尾巴)；不敢切返回 None。

    判据只有三条，且**一条都不自己发明**：
      1. 广告信号位置来自 diagnose.find_ad_signal（与 judge_ad_line 同源，
         两处判据各自演化是这个项目已经踩过的坑）；
      2. 切点 = 信号位置**往前**最后一个句末标点之后（句末标点表同样来自
         diagnose：_SENTENCE_END，刻意不含引号括号 —— 引号会出现在广告词内部）；
      3. 尾巴必须自己站得住脚：judge_ad_line(尾巴)["whole_line"] 为真才切。

    第 3 条是**安全阀**，不是冗余。没有它，'他抬头。Mr.Lee 的信折好放进口袋。'
    会被当成"正文 + 网址广告"切掉后半句（_URL 把 Mr.Lee 当域名）——
    少删一条广告只是留点噪声，误删正文是事故。

    三条保险各挡一类事故：
      - 找不到句末标点 → 不切（'他打开手机输入 www.example.com 然后关掉。'
        是正文提到网址，前面根本没有切点）；
      - 保留部分是空串或只有标点 → 说明这行本来就是整行广告，交给调用方
        按整行删处理，绝不留下一个空段落；
      - 尾巴判不成整行广告 → 不切（见上）。
    """
    k = _inline_cut_point(text)
    if k is None:
        return None
    kept, tail = text[:k], text[k:]
    if not squeeze(kept) or not _has_real_word(kept):
        # squeeze 去掉空白与标点后什么都不剩 → 保留部分只有标点（'……。'），
        # 这一行其实是整行广告，只是先前被判成了行内。
        return None
    if not tail.strip():
        return None
    # 尾巴是一句完整的、够长的、以句末标点收尾的话 → 那是正文，不是广告标签。
    # judge_ad_line 只回答"这行是不是整行广告"，它对"淘宝 www.taobao.com 上买。"
    # 这种带句号的短句会放行，所以这一条必须单独判。
    if _tail_looks_like_sentence(tail):
        return None
    evidence = judge_ad_line(tail)
    if not evidence or not evidence["whole_line"]:
        # 尾巴自己不像一句完整的整行广告 → 宁可不切。正文里"提到网址"、
        # 西文人名被 _URL 误当域名，都靠这一条挡住。
        return None
    balanced = _balance_cut(kept, tail)
    if balanced is None:
        # 切点劈开了引号对，而且尾巴里找不到那个右引号（说明它压根不在这行）
        # → 拒切，别把一个引号留成孤儿。
        return None
    return balanced




def _ad_tail_uncut(text: str) -> bool:
    """这一行是「正文 + 广告尾巴」但没敢切、值得请人核对吗？

    只有在**能算出切点**（前面是完整句子）的前提下才谈得上"行内夹广告"，
    但仅此还不够 —— 正文里写「屏幕上显示着 www.taobao.com 的页面。」也满足
    "前面有句话、后面有网址"，那是叙述，不是广告。

    判据是"尾巴自己像不像广告"，两条都要：
      - 有链接，或命中两个以上广告词（与整行广告判据同源，不另造表）；
      - 尾巴里没有句末标点 —— 广告尾巴是贴上去的一截标签（'记住本站 www.x.com'），
        而正文句子一定会拿句号/问号收尾（'的页面。'）。
    故意不追求全覆盖：这条只决定"要不要占用一行告警"，漏报不影响正文。
    """
    k = _inline_cut_point(text)
    if k is None:
        return False
    tail = text[k:]
    evidence = judge_ad_line(tail)
    if evidence is None:
        return False
    if not evidence["has_url"] and len(evidence["keywords"]) < 2:
        return False
    return not any(ch in _SENTENCE_TAIL for ch in tail)


def _no_punct_ad_line(text: str) -> bool:
    """整行一个句末标点都没有、却带着广告信号 —— 这种行没法切，只能报。

    为什么单独一支：切点算法要求"信号前面有句末标点"，一行从头到尾没有
    句号问号时它必然算不出切点（'她翻开那页看到微信两个字 www.example.com
    就合上了'）。这种行大概率是原书断行断坏了，也可能是广告被塞进了句中，
    不能擅自删，但必须让人看见。

    限定"整行没有句末标点"是为了挡住正常正文：'他打开手机输入
    www.example.com 然后关掉。' 有句号，就是清清楚楚的叙述句。
    """
    if find_ad_signal(text) is None:
        return False
    return not any(ch in _SENTENCE_TAIL for ch in text)


def _only_punct_before_cut(text: str) -> bool:
    """这一行是不是"切点之前只剩标点"的整行广告（该按整行删处理）。

    cut_inline_ad 有两种"拒切"：尾巴不像广告、保留部分只剩标点。前者要原样
    留着，后者说明这行本来就是整行广告、该整行删，这个函数只回答第二种。

    两个条件缺一不可：
      - 切点之前没有实义字符（否则是真正文，不是整行广告）；
      - 尾巴自己必须被判成**整行广告**。少了这一条就会误删正文：
        '……。Mr.Lee 的信折好放进口袋。' 的切点之前只有省略号，但后半句
        是正文（_URL 把 Mr.Lee 当域名才出现的信号），判不成整行广告，
        这时必须原样保留交给人工核对，不能整行删。
    """
    k = _inline_cut_point(text)
    if k is None:
        return False
    if _has_real_word(text[:k]):
        return False
    tail = text[k:]
    if not tail.strip():
        return False
    evidence = judge_ad_line(tail)
    return bool(evidence and evidence["whole_line"])


def _has_real_word(text: str) -> bool:
    """这段文字里还剩不剩**实义字符**（汉字、字母、数字）。

    用来挡"保留部分只有标点"（'……。'）：只看是否非空远远不够 ——
    标点也是字符，但一段只剩省略号的正文没有任何阅读价值，那就是空段。
    汉字与全角数字的 isalnum() 都是 True，所以不必单独判码位区间。
    """
    return any(ch.isalnum() for ch in text)


# ---------------------------------------------------------------------------
# 五、逐行扫描
# ---------------------------------------------------------------------------


# 全书级"这本书是不是硬折行"的判据（两条判据：从头到尾都要合并、
# 行尾基本都是文字而不是标点）。实测 600 本真书：正常书这个比例是 0.02–0.36，
# 硬折行书 0.88；把一本正常书按 40 字硬折行后是 0.52。0.45 落在正常书与
# 折行书之间那条缝里 —— 比 0.6 更宽，宁可多认几本折行书（它们本来就要并），
# 也不要把真折行书漏掉。
_HARDWRAP_RATIO = 0.45

# 样本太少时不给判（一本只有几十行的短篇，比例没有统计意义）。
_HARDWRAP_MIN_LINES = 60


def _hardwrap_ratio(lines: list[str]) -> float:
    """非空行里"行尾不是句末标点"的占比。

    这就是这里说的"行尾是文字而不是标点"。硬折行的书每一行都在句子中间
    被切断，所以这个比例接近 1；正常书的段落基本都以句号/问号/引号收尾。
    样本不足 _HARDWRAP_MIN_LINES 行时返回 0.0（判不了就当不是）。
    """
    nb = [s.strip() for s in lines if s.strip()]
    if len(nb) < _HARDWRAP_MIN_LINES:
        return 0.0
    ends = 0
    for line in nb:
        core = line.rstrip(_TRAILING_CLOSERS).rstrip()
        if core and core[-1] in TERMINAL:
            ends += 1
    return 1 - ends / len(nb)


def _hardwrap_width(lines: list[str]) -> int:
    """这本书是不是被"每行硬切 N 字"的工具折过：是就返回 N，不是返回 0。

    判据：非空行（strip 后）长度的众数落在 [20, 120] 且占比 >= 20% 才算硬折行。
    正常小说行长参差、再长的行也占不到两成；硬折行文件里满宽块占绝对多数。
    上下限与占比都是保守闸：**宁可漏判**（返回 0 = 闸门整体关闭、行为与改动前
    逐字节相同），不可在没折行的书上误杀真标题。语料实测：197 本里只有 frag_split
    类触发（16 本全 W=40），其余 12 类 W=0。
    """
    counts = Counter(len(s.strip()) for s in lines if s.strip())
    if not counts:
        return 0
    width, hit = counts.most_common(1)[0]
    if width < 20 or width > 120 or hit / sum(counts.values()) < 0.2:
        return 0
    return width


def _local_wrap_widths(lines: list[str]) -> dict[int, int]:
    """逐行给出"这一处被按多少字硬折行"的局部估计，返回 {原始行下标: 宽度}。

    全书级 _hardwrap_width 在合集类书上量不出来：《书寅》全书量出 0（众数
    占比不到 20%），而 0-500 行单独量是 35。量不出来，靠宽度打开的假标题闸门
    （_wrap_fake_chunk）就整本关着，正文行被当成章节标题 —— 那本书 804 个标题里
    "第三集中，我试着参进历史事件…"这种正文句就是这样混进来的。

    按每 _LOCAL_WIDTH_SEG 行一段量，段内所有行共用同一个宽度，与并碎行那边
    _local_hard_info 的宽度口径完全一致（同一段数据必须给同一个答案，否则
    切章与并段会对同一行给出矛盾的判断）。

    量不出宽度的段整段不收录：闸门拿不到宽度就不开，行为与改动前逐字节相同。
    """
    nb = [i for i, s in enumerate(lines) if s.strip()]
    out: dict[int, int] = {}
    for start in range(0, len(nb), _LOCAL_WIDTH_SEG):
        chunk = nb[start:start + _LOCAL_WIDTH_SEG]
        w = _hardwrap_width([lines[i] for i in chunk])
        if w:
            for i in chunk:
                out[i] = w
    return out


# 局部闸门的窗口半径，按**非空行**计。
#
# 为什么不能只看全书：判据"行尾是文字而不是标点"是**段落形态**的判断，而合集类书
# 里常常这一部是硬折行、那一部是一行一段。实测
# <书目录>\示例合集.txt
# （13770922 字节、136726 行）全书比 0.180 —— 全书级闸门永远不开，整本 102956 段里
# 有 2928 段不以标点结尾，全是硬折行留下的残片。
#
# 为什么也不能只看一块：硬折行区的块只有 2–5 行（一个自然段折成两三行、段间还留着
# 空行），块内比例虽是 1.0，样本却不够；而 404 行的大块是 0.74、249 行的大块是 0.01。
# 块太碎，块级闸门实测只把 2928 段残片降到 2899 段。滑动窗口两头都照顾得到。
#
# 窗口实测（±100 行）：行 3 → 0.671、行 300 → 0.737（硬折行区）；
# 行 2900 → 0.025、行 40100 → 0.015、行 48100 → 0.010（一行一段区）。区分干净，
# 所以沿用同一个阈值 _HARDWRAP_RATIO，只是把统计范围从全书缩到附近。
_HARDWRAP_WINDOW = 100


# 折行宽度按每 _LOCAL_WIDTH_SEG 行一段来量：同一部里折行宽度是稳定的，
# 一整本书却可能换好几次（实测《书寅》全书量出 0 —— 众数占比不够 20%，
# 而 0-500 行、500-1000 行各自量出来都是 35）。
_LOCAL_WIDTH_SEG = 500

# 硬折行区里"整块就是一段"的行数上限。
#
# 实测《书寅》行 0-4000：块（空行之间）平均 4.79 行，97.1% 的块首行带缩进，
# 76.5% 的块内续行也带缩进 —— 缩进在这本书里不是分段信号，空行才是。这种块里
# 每一行都以句末标点收尾（70.2%）但并不是段落结尾：源文的折行是"句子优先、太长
# 才按宽度切"，所以一段话会被切成几行、行行都收在句号上。逐行判据把这种小块
# （2~8 行）切成好几段，而按空行分它们各自就是一段。
#
# 但大块不能整块并，两种大块都不行：行 297 起那块 404 行里有 14 字的"二章绫清竹
# 的宿命"标题、有 12 字的短行，那些短行确实是段落结尾；行 1500-3000 的巨块
# （204/144/124 行）更是"一行一段、空行只是稀疏分隔"。10 行是"这块就是一段"的上限。
_HARDWRAP_BLOCK_MAX = 10


def _local_hard_info(items: list[_Line]) -> tuple[set[int], dict[int, int], set[int]]:
    """逐行判"这附近是不是硬折行"，返回 (命中行号集合, {行号: 折行宽度}, 可整块并的行号)。

    对非空行做前缀和，逐行取前后各 _HARDWRAP_WINDOW 行的窗口；窗口内"行尾不是句末
    标点"的占比 >= _HARDWRAP_RATIO 且样本数 >= _HARDWRAP_MIN_LINES，就判这一行处在
    硬折行区。宽度另按每 _LOCAL_WIDTH_SEG 行一段量，段内所有行共用。

    幂等靠这条：合并之后第二遍再看同一处，段落已经一行一段，窗口比例掉到 0.03 上下，
    闸门自然关掉，不会把并好的段落再焊一遍。

    用前缀和而不是逐行重算窗口：这本书 12 万非空行，逐行重算要 2400 万次判断。
    """
    body = [it for it in items if not it.blank]
    if not body:
        return set(), {}, set()
    pre = [0] * (len(body) + 1)
    for i, it in enumerate(body):
        core = it.content.rstrip(_TRAILING_CLOSERS).rstrip()
        bad = 0 if (core and core[-1] in TERMINAL) else 1
        pre[i + 1] = pre[i] + bad

    flags: set[int] = set()
    n = len(body)
    for i, it in enumerate(body):
        a = max(0, i - _HARDWRAP_WINDOW)
        b = min(n, i + _HARDWRAP_WINDOW + 1)
        cnt = b - a
        if cnt < _HARDWRAP_MIN_LINES:
            continue
        if (pre[b] - pre[a]) / cnt >= _HARDWRAP_RATIO:
            flags.add(it.no)

    widths: dict[int, int] = {}
    for s in range(0, n, _LOCAL_WIDTH_SEG):
        chunk = body[s:s + _LOCAL_WIDTH_SEG]
        w = _hardwrap_width([it.content for it in chunk])
        if w:
            for it in chunk:
                widths[it.no] = w

    # 整块并：小块（2~_HARDWRAP_BLOCK_MAX 行）且块里每一行都在硬折行区。
    block_join: set[int] = set()

    def flush(run: list[_Line]) -> None:
        if 1 < len(run) <= _HARDWRAP_BLOCK_MAX and all(it.no in flags for it in run):
            block_join.update(it.no for it in run)

    run: list[_Line] = []
    for it in items:
        if it.blank:
            flush(run)
            run = []
        else:
            run.append(it)
    flush(run)
    return flags, widths, block_join


def _head_chunk_is_prose(raw: str) -> bool:
    """段首的"满宽候选"里带逗号的，判成正文而不是标题。

    为什么需要这条：_wrap_fake_chunk 的 a 条（候选自己占满一整块折行宽度）
    在段首不拒 —— 400 本池里段首 a 命中全是真章标题（见 _scan 里的注释）。
    但那一批里混着另一种行：硬折行文本被**空行**从中间断开，后半段的头一块
    落在段首，读起来就是一整句正文。

    实测（300 本随机语料、每本前 4000 行）：段首 a 命中且含逗号的候选只有
    10 条，7 本书 —— 其中 9 条是正文（「第一节课是中国性史课，按进度，应该是
    讲的是古代唐朝皇帝唐明皇，是如」「第十章龙女蒙难小龙女独自回房，心中不免
    惆怅，看到朝夕相处的清儿重伤」），1 条是真标题后面粘了作者声明
    （「第三回误会冰释（故事纯属虚构，请勿模仿…」）。

    逗号是关键区分：真标题即使长，逗号也极少出现在标题正文里；而"编号开头的
    正文"里逗号是常态。粘了括注的那条真标题用"括号前很短"救回来 —— 括号是
    作者声明/警告的起点，标题本体在它前面，十几个字顶天。
    """
    core = squeeze(strip_zero_width(raw))
    if "，" not in core and "," not in core:
        return False
    cut = core.find("（")
    if cut > 0 and len(core[:cut]) <= 12:
        return False
    return True


def _wrap_fake_chunk(
    raw: str,
    heading: model.Heading,
    *,
    width: int,
    dup: bool,
    prev_len: int,
    prev_sent: bool,
    prev_number: int | None,
    prev_is_title: bool,
) -> str:
    """这一行是不是硬折行切出来的"假章节"？（task-7，REPORT.md 缺陷 3）

    返回命中的判据（"a"/"b"/"c"），没命中返回空串。为什么要返回是哪一条：
    三条判据的可信度不一样 —— a 条（候选自己就占满一整块折行宽度）是形态证据，
    段首段中都成立；b/c 条靠"上一行满宽未收句"当旁证，在段首会误杀真标题，
    调用方要区别对待（见 _scan 里 mid_para 那段注释）。

    缺陷成因：正文段被按固定字数切成满宽块，块首常是"第三章里写的那些话…"这类
    引用章节号的句子，且折行把句末标点切到了下一行 —— _looks_like_sentence 的
    "以句末标点收尾"依据失效，整块被认成新标题。四本受害书（18→21、31→37、
    83→87、24→27）的 16 个假标题全在这里拦掉。

    三条判据（R = a ∨ b ∨ c），全部只在 width>0（本书确被硬折行）时生效：
      a. 候选**自己就是一整块满宽文本**（squeeze 后 >= width-2）且编号后紧跟正文
         （无分隔符）：真标题不会占满一整块折行宽度。-2 容差来自 book14 有一块
         实际 39 字（段尾收句后被切）。
      b. 编号与前面已认过的标题**重复**（同家族同号），且上一非空行是满宽未收句
         的块：正文在引用旧章节号。
      c. "纯数字行"家族 + 上一行满宽未收句 + 编号接不上上一个标题（!= prev+1）：
         那是被切出来的数字碎片。连续性是 book04/book05 的生死线 —— book04 真标题
         "19" 前面正是 18 章（保留），book05 假碎片"7""0"接不上（拒掉）。

    取证（tmp-b2/probe8_predR.py，13 类 197 本、808 个真标题逐本核对）：
    frag_split 杀掉 16/16 假标题，四类真基准 0 误伤；其余 12 类 0 命中。
    已知残余风险：a 会误杀"无分隔符且 >=38 字"的真长标题 —— 语料中 0 例
    （真标题要么带分隔符要么短），且上限受 MAX_TITLE_CHARS=80 约束，只可能发生在
    硬折行书上，闸门范围已足够窄。
    """
    core = raw.strip().rstrip(_TRAILING_CLOSERS)
    if not core:
        return False
    tail = _title_tail(core)
    # 与 _looks_like_sentence 同一套"编号后有没有分隔符"的算法：
    # tail 为 None（Markdown/纯数字这类没有"第N章"前缀的家族）也算无分隔符。
    no_sep = tail is None or (bool(squeeze(tail)) and tail[0] not in _TITLE_SEPARATORS)
    full_prev = prev_len >= width and not prev_sent
    if no_sep and len(squeeze(raw)) >= width - 2:      # a
        return "a"
    if dup and full_prev and not prev_is_title:        # b
        return "b"
    if heading.family == "纯数字行" and full_prev and (
        prev_number is None or heading.number != prev_number + 1
    ):                                                 # c
        return "c"
    return ""


def _scan(
    lines: list[str],
    *,
    bracket_sections: bool = True,
    numbered_sections: bool = True,
    bare_numbers: bool = True,
    story_titles: frozenset[int] = frozenset(),
    protect: frozenset[int] = frozenset(),
) -> tuple[list[_Line], dict]:
    """逐行规范化 + 分类 + 删整行广告与目录标记。

    这个方法里**只做单行判断**，跨行的事（压空行、并碎行、切章）都在后面。
    单行判断放一起，是因为"体检说 9 行广告、清洗删 11 行"这种事故最容易
    发生在两处判据各自演化的时候。

    protect 是人工保护的行号集合（见 _parse_adjust）：命中的行照样走完
    规范化，但**不许当整行广告或目录标记删掉**。保护只否决"删除"这一类
    决定，不否决字符层面的规范化 —— 这里说的是"这一行别删"，不是
    "这一行原样进书"（原样进书会带着零宽字符和行尾空白）。

    story_titles 是"整本没有章节标记"时由 _story_title_lines 认出来的分篇
    标题行号（原始行下标）。默认空集，正常书一条都用不上。
    """
    items: list[_Line] = []
    ad_samples: list[dict] = []
    inline_samples: list[dict] = []
    # 每一行"本来会被删"的记录，全量。samples 只留 SAMPLE_LIMIT 条给报告
    # 好读，这份是给复核界面列勾选项的 —— 只留 8 条的话，第 9 行以后的
    # 广告用户永远勾不到。
    dropped_lines: list[dict] = []
    protect_hit: list[int] = []
    protected_ad = 0      # 命中保护、本来会被当整行广告删掉的行
    ad_whole = 0
    ad_inline_cut = 0     # 行内夹广告：成功切掉的
    ad_inline_keep = 0    # 行内夹广告：能算切点、但不敢切而原样留着的（要告警）
    ad_inline_protected = 0   # 行内夹广告：本来能切，因为人工保护没切
    inline_protected_lines: list[int] = []   # 这些行号（同上，给 unmatched 用）
    ad_title = 0          # 章节标题里夹着广告词/网址的（不删不切，只提醒）
    toc_marker = 0
    toc_marker_protected = 0   # 目录标记行里被保护留下的（仍计入"这本书有目录"的证据）
    end_marker = 0             # "本章完"这类连载标记，整行删掉的
    end_marker_protected = 0   # 同上，被人工保护留下的
    # 硬折行闸门（task-7）：本书没被按字数硬切过时 width=0，下面整段逻辑不生效。
    # 合集类书全书量不出宽度（这一部折行、那一部一行一段），再给一份按段量的
    # 局部宽度兜底，见 _local_wrap_widths。
    width = _hardwrap_width(lines)
    local_width = _local_wrap_widths(lines)
    # 闸门命中的假标题行数。**这是判前的数**：真正被拒掉的行数要等
    # 并碎行阶段定案（并得回上一段才算拒，见 _merge_fragments），
    # 进报告的是那边算出来的 wrap_rejected。这个数只给取证脚本看。
    wrap_gate_hit = 0
    # 段首 a 条 + 带逗号被直接判成正文的行数（见 _head_chunk_is_prose）。
    # 这些行没走 _merge_fragments 那条路（前面隔着空行，并不进上一段），
    # 所以拒掉的账要在这里自己记，不能靠那边的 wrap_rejected。
    wrap_head_dropped = 0
    seen_nums: Counter = Counter()      # 已标题化行的 (家族, 序号) 计数
    prev_number: int | None = None      # 上一个已标题化行的序号
    # 逐行记下"这一行最终是不是标题"（含后面被当广告删掉的行）。
    # 下标 = 行号，给 _prev_nonblank 用 —— items 只收留存的正文行，下标对不上。
    line_title: list[bool] = []

    def _prev_nonblank(idx: int) -> tuple[int, bool, bool]:
        """上一非空行的 (长度, 是否以句末标点收尾, 是否已被判成标题)。

        只看行号、不看 items：被删掉的广告行同样可能是折行块的一部分，
        与取证脚本 probe8_predR.py 的行为保持一致。
        """
        for j in range(idx - 1, -1, -1):
            t = lines[j].strip()
            if t:
                pc = t.rstrip(_TRAILING_CLOSERS)
                return (
                    len(t),
                    bool(pc) and pc[-1] in _SENTENCE_TAIL,
                    line_title[j],
                )
        return 0, False, False

    def _note_drop(no: int, kind: str, text: str, protected: bool) -> None:
        """记一行"本来会被删"。

        protected=True 表示这次被人工保护留下了。账目口径（报告里必须对得上）：
            该删的行数 = dropped.ad + adjust.protected.ad
        前者是实删，后者是保护未删。少记一边，用户就会发现"数字对不上"。
        """
        dropped_lines.append(
            {"line": no, "kind": kind, "protected": protected, "text": text[:60]}
        )
        if protected:
            # 记下"这一行确实被保护住了"。adjust 里给了行号却没命中的，
            # 报告要能点名（见 _build_report 的 unmatched）。
            protect_hit.append(no)

    for i, raw in enumerate(lines):
        # 段首占位符先剥掉：它是"原作者在这里另起一段"的证据（见 _LEAD_MARKERS），
        # 但本身不是内容。剥了之后剩下的话照常走规范化 —— 注意喂给 normalize_line
        # 的是剥过的文本，否则 4 个半角空格 + 问号串会被当成正文缩进留下来。
        body, para_start = strip_lead_marker(raw)
        cl = normalize_line(body)
        blank = not cl.text.strip()
        # 标题判定必须喂**原始行**（只去零宽）：normalize_line 会把行内空白
        # 全删掉，"第三章 你是谁？" 变成 "第三章你是谁？"，编号后的分隔符没了，
        # 成句守卫就再也分不清"编号 + 标题"和"编号开头的正文句"。
        heading = (
            None
            if blank
            else _heading_of(
                strip_zero_width(raw),
                cl.text,
                allow_bracket_section=bracket_sections,
                allow_numbered_section=numbered_sections,
            )
        )
        # 短篇集的分篇标题：整本没有章节标记时由 _story_title_lines 认出来的行，
        # 这里补一张"标题身份证"。没有编号（number=None）、层级 1。
        if heading is None and i in story_titles:
            heading = model.Heading(number=None, level=1, family="短篇集标题")
        # 裸数字行（整行只有一个数字，如「2」）：语料里它多半是扫描残留的页码。
        # 两条路，任一成立就认它是章节：
        #   1. 书级闸门说这本书有成串编号（_bare_number_is_structure）—— 《书W》
        #      每章内部用「1 2 3…」标小节，章标题仍是「第一章…」，两道结构并存，
        #      全放行才不会把 41 条小节号打成正文里的孤立段落。
        #   2. 书级闸门没开，但这一行在**段中**、且编号接着上一章往下数 —— 那是
        #      「第N章」被折行切断后剩下的半截（见 case_hardwrap_bare_number_needs_continuity）。
        # 段首的孤立数字两条都不占：实测《书寅》行 5057 那条「2」前面隔着
        # 空行、编号跳号，它切走了 1355 段正文成一章。
        if heading is not None and heading.family == "纯数字行" and not bare_numbers:
            mid = i > 0 and bool(lines[i - 1].strip())
            if not (
                mid
                and heading.number is not None
                and prev_number is not None
                and heading.number == prev_number + 1
            ):
                heading = None
        # 硬折行假标题闸门：_heading_of 只有单行视野，看不出"这一行是上一段被
        # 切开的半截"。跨行的证据（上一行是不是满宽未收句、这个编号见过没有）
        # 在这里补 —— 这是 _scan 里唯一一处跨行判断，因为再往后挪，章节树
        # 已经建好，切错的章就把正文永久分家了。
        wrap_heading: model.Heading | None = None
        # 人工保护留下的行（见 _Line.frozen）。默认 False；只有目录标记行
        # 会在这里被置上 —— 整行广告的保护走 entry 那条路（entry 非空就
        # 天然挡住并碎行与卷标题升级），不需要这个开关。
        frozen = False
        #
        # 序号状态（seen_nums / prev_number）对**每一个标题候选**推进，含被
        # 闸门拒掉的那一个：折行块引用的旧编号本身也是"这本书被切坏"的证据，
        # 拒掉就不登记的话，下一行的重复判据（b 条）与连续性判据（c 条）会拿错
        # 基准，反过来误杀真标题。与取证脚本 probe8_predR.py 逐字一致。
        if heading is not None:
            cand_num = heading.number
            if cand_num is not None:
                seen_nums[(heading.family, cand_num)] += 1
                dup = seen_nums[(heading.family, cand_num)] > 1
            else:
                dup = False
            # 宽度优先用全书级（那是"整本都被折过"的硬证据），量不出来再看这一段的。
            w_here = width or local_width.get(i, 0)
            if w_here > 0:
                prev_len, prev_sent, prev_title = _prev_nonblank(i)
                # **只在「段中」才拒**：紧邻上一行非空。段首的候选与真标题在
                # 单行视野里无法区分 —— 实测 400 本池里 #106 命中 1 次、#137
                # 1 次、#264 3 次，全部落在段首且全是真标题（#264 那本 45 个
                # 标题里 34 个是裸「第N章」，全在段首），按段首拒会把真章吃掉：
                # 旧版（不看段首/段中、命中即拒）实测 #106 2 章变 1 章、#137
                # 1 章变 0 章、#264 12 章变 9 章。段首那部分属语义判断，按铁律 3
                # 「规则优先，AI 兜底」归 M4：这里只**计数**，让它进报告的
                # "疑似、原样保留"一栏，章节结构一个字不动。
                hit = _wrap_fake_chunk(
                    strip_zero_width(raw),
                    heading,
                    width=w_here,
                    dup=dup,
                    prev_len=prev_len,
                    prev_sent=prev_sent,
                    prev_number=prev_number,
                    prev_is_title=prev_title,
                )
                if hit:
                    wrap_gate_hit += 1
                mid_para = i > 0 and bool(lines[i - 1].strip())
                # b/c 条只在段中拒：它们靠"上一行满宽未收句"当旁证，段首没有
                # 上一行可比，命中基本等于误杀真标题（见上面那段取证注释）。
                # a 条段中也拒；段首则**分情况**（见 _head_chunk_is_prose）：
                # 带逗号的当正文，不带的照旧保留 —— 400 本池里段首 a 命中
                # 绝大多数是真章标题，不能一刀切。实测《书寅》行 6649
                # '第三集中，我试着参进历史事件，不知道大家感觉怎样？但是，有几点是'
                # 就是前一种：32 字正好等于该段的折行宽度，前面隔着空行，
                # 原来只在报告里记一笔"疑似"，正文结构照旧被它切成两章。
                if hit and mid_para:
                    wrap_heading = heading
                    heading = None
                elif hit == "a" and _head_chunk_is_prose(raw):
                    # 这里**不设** wrap_heading：段首的碎片前面隔着空行，
                    # 永远并不进上一段，_merge_fragments 会把标题原样还回来
                    # （见那边 forced 分支），设了等于白拒。
                    heading = None
                    wrap_head_dropped += 1
            # 编号链在**判完之后**才推进：c 条要比的是"本行编号 vs 上一个标题编号"，
            # 先推进就成了自己和自己比。probe8_predR.py 也是这个顺序。
            if cand_num is not None:
                prev_number = cand_num
        line_title.append(heading is not None)
        # 标题行要保留行内空白：'第一回 止淫风借淫事说法 谈色事就色欲开端'
        # 里的空格是作者人工分段的对仗标记，删掉就成了连写一串。正文行照旧
        # 删（"他 说" 是噪声）。这件事必须等 heading 判完才知道 —— 判标题
        # 用的是默认那份文本，取值用保留空白这份。
        if heading is not None:
            # 标题取值也要用**剥掉站点水印**的那一版：认标题时用 _strip_site_prefix
            # 才认得出来，若取值仍拿原文，目录里就会显示「正文　第000章：楔子」——
            # 站点水印跑进了章节标题，读者在 EPUB 目录里直接看到它。
            # 取值用 body 而不是 raw：body 已经剥掉段首占位符（"!!!!"），
            # 用 raw 的话四个感叹号会被标点归一并成一个「！」，标题成了「！（一）」。
            cl = normalize_line(
                _strip_site_prefix(strip_frame(strip_zero_width(body))),
                keep_inline_space=True,
            )
        # 场景分隔线（整行只有装饰符号）。它既不是标题也不是正文，
        # 渲染层要单独排版，所以在这里就标出来。
        sep = (
            (not blank)
            and heading is None
            and (_is_separator_line(cl.text) or _is_part_end_marker(cl.text))
        )
        # 章节结束标记（"本章完""（未完待续）""以下正文"）：整行删。
        # 放在广告判据之前：这类行既没有广告词也没有网址，judge_ad_line 对它
        # 返回 None，晚一步就漏过去了。标题豁免同样适用 —— 万一某章标题正好
        # 叫「本章完」，删了就是删一章。
        if (not blank) and heading is None and _is_end_marker(cl.text):
            if (i + 1) in protect:
                end_marker_protected += 1
                _note_drop(i + 1, "marker", cl.text, True)
            else:
                end_marker += 1
                _note_drop(i + 1, "marker", cl.text, False)
                continue

        entry = None if blank else judge_ad_line(raw, i + 1)

        # 整行广告：摘掉广告成分后不剩正文，整行删。
        #
        # 标题豁免：章标题本身带网址或广告词时（"第二章 www.abc-novel.com"、
        # "第三章 天才一秒记住本站"），删掉这一行等于删掉整个章节 —— 它的正文
        # 会被并进上一章，报告里一条警告都不会有。少删一条广告只是留点噪声，
        # 丢一章是丢结构，代价不对等。
        # 被人工保护的整行广告：留下，但不再走行内切除（整行广告没有
        # 可切的正文前缀，硬切只会切出半截残渣）。
        skip_inline = False
        # 本行是不是「受保护的整行广告」（见 _Line.ad_kept）。
        ad_kept = False
        if entry is not None and entry["whole_line"] and heading is None:
            if (i + 1) in protect:
                # 人工保护：这一行不删。注意这里**不清空 entry** ——
                # entry 非 None 会让并碎行跳过它（_merge_fragments 只并
                # ad is None 的行），这正是我们要的：用户勾"这一行别删"，
                # 意思是这一行要以独立一段留在书里，不是被焊进上一段尾巴。
                protected_ad += 1
                _note_drop(i + 1, "ad", entry["text"], True)
                if len(ad_samples) < SAMPLE_LIMIT:
                    ad_samples.append(
                        {
                            "line": entry["line"],
                            "text": entry["text"],
                            "keywords": entry["keywords"],
                            "has_url": entry["has_url"],
                            "protected": True,
                        }
                    )
                # 防御性：整行广告（whole_line=True）按定义没有"前面完整句子 +
                # 后面广告尾巴"的形态，_inline_cut_point 对它返回 None，所以
                # 这个开关当前走不到。留着是因为它守的是"保护的行一个字都不动"，
                # 一旦判广告的规则放宽就会踩到。
                skip_inline = True
                ad_kept = True
            else:
                ad_whole += 1
                _note_drop(i + 1, "ad", entry["text"], False)
                if len(ad_samples) < SAMPLE_LIMIT:
                    ad_samples.append(
                        {
                            "line": entry["line"],
                            "text": entry["text"],
                            "keywords": entry["keywords"],
                            "has_url": entry["has_url"],
                            "protected": False,
                        }
                    )
                continue
        # 行内夹广告：正文后面缀了广告尾巴。按"最后一句句末标点"处切掉，
        # 切不动就原样留着 —— 少删一条广告只是噪声，错切就是正文残缺。
        #
        # 标题行不切：章标题里带广告词时（"第三章 天才一秒记住本站"）标题
        # 本身就是章节结构，切掉尾巴等于毁掉标题。
        line_text = cl.text
        cut_text = ""
        if entry is not None and not skip_inline:
            # "行内夹广告"的定义收紧成：**这一行能算出切点**，也就是
            # "前面是完整句子、后面挂着一截东西"。只有这时才谈得上切与切不动。
            #
            # 为什么不能只看 judge_ad_line 返回非 None：它同时负责回答"这行像不像
            # 广告"，正文里提到"微信""校对""手打"，甚至出现 Mr.Lee 这种被 _URL
            # 误当域名的西文人名，都会得到一个 whole_line=False 的结论 —— 那是
            # "不是整行广告"，不是"是行内广告但切不动"。把后者算进告警，
            # 一本正常小说会平白多出几十条"无法安全切除"，报告就没人看了。
            if heading is not None:
                # 标题行不切也不算行内广告，但标题里带广告词/网址本身值得看。
                ad_title += 1
            elif _inline_cut_point(cl.text) is None:
                # 算不出切点：信号出现在句中、前面没有句末标点。绝大多数情况
                # 这是正文提到广告词（'他打开手机输入 www.example.com 然后关掉。'），
                # 一个字都不能动。只有"整行连一个句末标点都没有"才值得报警 ——
                # 那种行更像原书断行断坏了、或广告被塞进了句中。
                if _no_punct_ad_line(cl.text):
                    ad_inline_keep += 1
            else:
                piece = cut_inline_ad(cl.text)
                if piece is not None:
                    if (i + 1) in protect:
                        # 人工保护：这一行的文字一个字都不动 —— 行内广告的
                        # 尾巴也不切。为什么必须让行内切除也听 protect：
                        # 行内切除**不删行**，所以它不出现在 dropped_lines
                        # 那份全量勾选清单里，用户没有第二个入口叫它停手；
                        # 不在这里拦，用户点了"这一行别动"却发现尾巴还是没了。
                        # 单独记一个数，不混进 dropped.ad 的账：行内切除本来
                        # 就不算"删行"，混进去恒等式会对不上。
                        ad_inline_protected += 1
                        inline_protected_lines.append(i + 1)
                        # 也算"命中"：这一行确实因为你说了别动而改变了行为
                        # （尾巴没被切）。不记进来，同一个行号会同时出现在
                        # protected.inline 和 unmatched.protect_lines 里，
                        # 报告前后自相矛盾（"保住了 1 行" + "这 1 行没效果"）。
                        protect_hit.append(i + 1)
                        if len(inline_samples) < SAMPLE_LIMIT:
                            inline_samples.append(
                                {
                                    "line": i + 1,
                                    "text": raw.strip()[:60],
                                    "cut": piece[1].strip()[:40],
                                    "kept": cl.text.strip()[:60],
                                    "protected": True,
                                }
                            )
                    else:
                        # 保留部分就是 cl.text 的前缀，缩进（INDENT_STR）自然跟着
                        # 留下来 —— 缩进是排版信息，切广告不该顺手把它抹掉。
                        line_text = piece[0]
                        cut_text = piece[1]
                        ad_inline_cut += 1
                        if len(inline_samples) < SAMPLE_LIMIT:
                            inline_samples.append(
                                {
                                    "line": i + 1,
                                    "text": raw.strip()[:60],
                                    "cut": cut_text.strip()[:40],
                                    # 留下什么也要单独存：原行与切除部分都截断过，
                                    # 报告里拿"原行减切除"去算会算出半截广告残渣。
                                    "kept": line_text.strip()[:60],
                                }
                            )
                elif _only_punct_before_cut(cl.text):
                    # 切点之前一个字都没有（只剩标点）：这行本来就是整行广告，
                    # 只是 judge_ad_line 因为别的原因把它判成了"行内"。按整行
                    # 删处理，否则会留下一个只剩标点的空段落。
                    if (i + 1) in protect:
                        # 人工保护：这行留下。它只剩标点 + 广告尾巴，
                        # 切不出正文，所以也不做行内切除。
                        protected_ad += 1
                        ad_kept = True
                        _note_drop(i + 1, "ad", entry["text"], True)
                        if len(ad_samples) < SAMPLE_LIMIT:
                            ad_samples.append(
                                {
                                    "line": entry["line"],
                                    "text": entry["text"],
                                    "keywords": entry["keywords"],
                                    "has_url": entry["has_url"],
                                    "protected": True,
                                }
                            )
                    else:
                        ad_whole += 1
                        _note_drop(i + 1, "ad", entry["text"], False)
                        if len(ad_samples) < SAMPLE_LIMIT:
                            ad_samples.append(
                                {
                                    "line": entry["line"],
                                    "text": entry["text"],
                                    "keywords": entry["keywords"],
                                    "has_url": entry["has_url"],
                                    "protected": False,
                                }
                            )
                        continue
                elif _ad_tail_uncut(cl.text):
                    # 有切点、尾巴也确实像广告，却因为判不成"整行广告"而不敢切：
                    # 它会留在正文里，用户打开 EPUB 就能看见，必须报一句。
                    ad_inline_keep += 1

        # 目录标记行（哪怕不在目录块里也删：单独一行"目录"没有任何阅读价值）
        # 也要计数：报告里的"删了几行"必须覆盖每一个被删掉的行，
        # 否则用户对账时会发现"少的那一行不知道去哪了"。
        if not blank and _is_toc_marker(cl.text):
            # 保护行照样计入 toc_marker：这个数同时是"这本书有目录"的硬证据
            # （clean_text 拿它当 has_toc_marker 传给 _plan_toc_drop），
            # 保护一行"目录"不该让整块目录的识别失效。
            toc_marker += 1
            if (i + 1) in protect:
                toc_marker_protected += 1
                _note_drop(i + 1, "toc", cl.text, True)
                # 人工保护 = "这一行的文字别删"，**不等于**"让它当章节标题"。
                # 「目录」这类行同时落在前言类标题家族里（见 _FRONT_BACK_WORDS），
                # 不清掉 heading 就会凭空多出一个空章节「目录」挂进书里 ——
                # 那条规则本来就是在说"这不是标题，是标记"，保留文字的同时
                # 必须保留这个判断。所以这里降级成普通正文行。
                heading = None
                if line_title:
                    # 2000 行已经按"是标题"记过一笔，_prev_nonblank 会拿它
                    # 判断下一行前面是不是标题，这里同步改回去。
                    line_title[-1] = False
                # 这一行到此为止结构就定死了：不许再被升级成卷标题、不许被
                # 并碎行焊进邻段（见 _Line.frozen 的注释）。
                frozen = True
            else:
                _note_drop(i + 1, "toc", cl.text, False)
                continue

        items.append(
            _Line(
                no=i + 1,
                raw=raw,
                text=line_text,
                indented=cl.indented,
                blank=blank,
                heading=heading,
                ad=entry,
                cut=cut_text,
                para_start=para_start,
                sep=sep,
                wrap_frag=wrap_heading is not None,
                wrap_heading=wrap_heading,
                frozen=frozen,
                ad_kept=ad_kept,
            )
        )
    return items, {
        "ad_whole": ad_whole,
        "ad_inline_cut": ad_inline_cut,
        "ad_inline_keep": ad_inline_keep,
        "ad_inline_protected": ad_inline_protected,
        "inline_protected_lines": inline_protected_lines,
        "ad_title": ad_title,
        "ad_samples": ad_samples,
        "inline_samples": inline_samples,
        "toc_marker": toc_marker,
        "toc_marker_protected": toc_marker_protected,
        "end_marker": end_marker,
        "end_marker_protected": end_marker_protected,
        "protected_ad": protected_ad,
        "protect_hit": protect_hit,
        "dropped_lines": dropped_lines,
        "wrap_gate_hit": wrap_gate_hit,
        "wrap_head_dropped": wrap_head_dropped,
    }


def _scan_fixed(lines: list[str]) -> dict:
    """统计"字符层面改了多少行"，给清洗报告用。

    为什么要另外算一遍而不是读 _Line 的元信息：
    normalize_line() 只回一个最终文本，"零宽 36 行 / 行尾空白 36 行 / 标点 2 行"
    这种分项数字它不提供。这里按 normalize_line 的同一步骤序列重算一次，
    **只用于统计**；最终文本永远信 normalize_line() 那一份，
    这样即使这里漏了一步，也不会让输出变脏，最多让报告少报一行。
    """
    zero_width = trailing = indent_norm = punct = space = changed = lead_marker = 0
    for raw in lines:
        # 段首占位符（???? / !!!!）单独计一行：它是被**删掉的内容**，
        # 混进"修复"里会让人以为改了字。报告里它该和"删了多少行"放在一起说。
        _, para_start = strip_lead_marker(raw)
        if para_start:
            lead_marker += 1
        cl = normalize_line(raw)
        if cl.changed:
            changed += 1
        if find_zero_width(raw)["total"] > 0:
            zero_width += 1
        if raw != raw.rstrip():
            trailing += 1
        base = clean_line(strip_zero_width(raw))
        if base.spaces_collapsed > 0:
            space += 1
        # 判缩进是否被改过：有缩进但原始缩进不是标准的两个全角空格
        if base.indented and base.indent_raw != INDENT_STR:
            indent_norm += 1
        # 只比标点/引号这一步的输入输出。不能拿 "标点归一后的整段" 与
        # base.text 比：normalize_punctuation 顺手会删"中文之间的空白"
        # （"第一章 开始" → "第一章开始"），那不是标点改动，混进来会多报。
        punct_out = normalize_quotes(normalize_punctuation(base.text))
        if squeeze(punct_out) != squeeze(base.text):
            punct += 1
    return {
        "zero_width_lines": zero_width,
        "trailing_space_lines": trailing,
        "indent_normalized_lines": indent_norm,
        "punctuation_lines": punct,
        "space_collapsed_lines": space,
        "changed_lines": changed,
        "lead_marker_lines": lead_marker,
    }


# ---------------------------------------------------------------------------
# 六、压空行
# ---------------------------------------------------------------------------


def _collapse_blanks(items: list[_Line]) -> list[_Line]:
    """连续空行压到 MAX_BLANK_RUN 行。

    整块空行本身不承载信息（场景切换只需要"有空行"这一个事实），
    而空行是唯一会让输出体积翻倍的噪声。
    """
    out: list[_Line] = []
    run = 0
    for it in items:
        if it.blank:
            run += 1
            if run > MAX_BLANK_RUN:
                continue
            out.append(it)
        else:
            run = 0
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# 五点五、标诗词
# ---------------------------------------------------------------------------

# 引导语：出现在**行尾**、带冒号，说明"下面是引文"。中文小说里诗词几乎
# 总由这样一句领出来，这是最硬的一条信号。
# 不收"道："单字：正文里"某某道："极常见，后面接的还是白话对话。
_VERSE_LEADS = (
    "词曰", "诗曰", "诗云", "偈曰", "赞曰", "有诗为证", "有词为证",
    "诗道", "词云", "批云", "为证", "以诗为证",
    # 下面几个是第五轮只读复核补的召回：「正是：」在章回小说里后面几乎
    # 总跟一首诗；「有诗一首：」「词一首：」是同一种引导语的另一种写法。
    # 刻意**不收** "古人云" / "常言道"：它们后面多数接一句白话解释，
    # 标成韵文会让读者以为那是一段引诗。
    "正是", "有诗一首", "有词一首", "诗一首", "词一首",
)

# 引导语还有第二种写法：不是固定的"诗曰"，而是一句叙述带着"体裁"收尾，
# 例如"写五言四句的一首偈道：""又写出四句道：""续两句道："。
# 判据要求同时满足三条：
#   1. 行尾是冒号，且冒号前一个字是 道/云/曰（"说"不算：太口语，后面常是白话）；
#   2. 冒号前出现过诗词体裁名（诗/词/偈/曲/赋/联/绝/律）；
#   3. 或者出现过"N句"这种句数（"四句道""两句道"）。
# 单独的"他说道："三条都不满足，不会误判。
_VERSE_NOUNS = ("诗", "词", "偈", "曲", "赋", "联", "绝", "律")
_VERSE_COUNT = re.compile(r"[一二三四五六七八九十两]+\s*句")

# 收尾标记：诗词结尾会注一句曲牌/词牌，例如"右调·忆秦娥""右其一"。
# 见到它，说明**前面若干行**都是诗 —— 方向与引导语相反，要向前回溯。
_VERSE_TAIL = re.compile(r"^[（(]?右\s*(调|其)")

# 引文内部可能出现对话（"某某道："），那一行是叙事而不是韵文。
_VERSE_DIALOG = ("道：", "：“", "道:「", "云：“")

# 现代白话的助词与语气词。韵文里几乎不出现，叙述句里几乎一定有 ——
# 这是"短句"与"诗句"最省事的分界线：「他看罢，点了点头。」短、带句读、
# 不含对话，光看几何完全像五言，但一个「了」就露了。
# 刻意**不收代词**（我/你/他）：词里"我住长江头"是常态，
# 按代词排会把真正的词一起排掉。
# 白话助词。判据不是"行里出现这些字就否"（"了却君王天下事"整句第一个字就是
# 「了」，那是最有名的宋词之一），而是"某个分句**以**这些字收尾" ——
# "众人静了下来。"最后一顿落在「了」，那是叙述句的典型收尾。
_VERSE_STOP = "了着吗呢吧啊呀的嘛"

# 出现在**分句中间**就说明这是白话叙述的助词。放在首字不算：
# 「了却君王天下事」的「了」是"了却"的一部分，不是完成体标记。
_VERSE_MID_STOP = "了着过吗呢吧啊呀嘛的"

# 分句末尾一层层剥掉的东西：句末标点、右引号右括号。剥完再看最后一个实义字。
_VERSE_TAILS = "。！？…!?，、；：;:,.\u201d\u2019\u300d\u300f\uff09)"

# 以人称代词开头的分句当作叙述，不是韵文。诗词里的「我」出现在句子中间
# （"我住长江头"），不会顶格当主语铺陈 —— 用这一条挡住扩散到叙述句。
_VERSE_SUBJECT = "他她它您咱"

# 分句切分：只按"句中停顿"切。句末标点不切，留给最后一段自己剥。
_VERSE_CLAUSE_SPLIT = re.compile(r"[，、；：,;]")

# 一行最多这么长才可能是诗词的一行。七言律诗连标点 16 字，留一倍余量。
_VERSE_MAX_CHARS = 30


def _lead_close(text: str) -> bool:
    """这一行是不是"行尾引导语"，即下一行开始是引文（如「词曰：」）。

    两种写法都认：
      * 固定引导语：词曰 / 诗云 / 为证 …（_VERSE_LEADS）；
      * 叙述式引导语：写五言四句的一首偈道 / 又写出四句道 / 续两句道 …，
        靠"道/云/曰 收尾 + 前面有体裁名或句数"认出来（见 _VERSE_NOUNS）。
    两行都只当信号，本身不标 verse。
    """
    s = squeeze(text)
    if not s.endswith(("：", ":")):
        return False
    body = s.rstrip("：:")
    if any(body.endswith(k) for k in _VERSE_LEADS):
        return True
    if not body or body[-1] not in "道云曰":
        return False
    return any(n in body for n in _VERSE_NOUNS) or bool(_VERSE_COUNT.search(body))


def _tail_mark(text: str) -> bool:
    """这一行是不是曲牌/词牌收尾标记（如「右调·忆秦娥」）。"""
    return bool(_VERSE_TAIL.match(text.strip()))


def _verse_like(text: str) -> bool:
    """这一行像不像韵文的一行。

    判据只认"像"，不认"是"：宁可漏标（诗词按正文排，只是不够好看），
    也不要误标（把叙述句居中换楷体，读者会以为读到的是引文）。

      * 短：超过 _VERSE_MAX_CHARS 字的一行不可能是五言/七言/词句；
      * 不含对话：诗中不会有「某某道：」这种叙述标记；
      * 不以冒号/分号结尾：那是在引出下文，本身不是韵文；
      * 带句读：一行诗里总有逗号或句号，光秃秃一串字那是标语；
      * **逐分句看收尾与开头**（见下），这是"诗句"与"短叙述句"的分界线。

    逐分句判据为什么必要：光看整行几何，「他点头。」（4 字、带句号、
    不含对话）和「春眠不觉晓，」（6 字、带逗号）长得一模一样。
    旧版靠"整行不含了/着/的"来分，结果两头都错 ——
    「他点头。」一个助词都没有，照样被标成诗；而「了却君王天下事」
    第一个字就是「了」，最著名的宋词之一反而标不上。
    改成**看每个分句的最后一个实义字**：「静了下来」「点了点头」收在
    助词上（叙述），「天下事」「身后名」收在实词上（韵文）。
    再加一条"分句不以人称代词开头"，把「他点头」这类短叙述句挡掉。
    """
    s = text.strip()
    if not s or len(s) > _VERSE_MAX_CHARS:
        return False
    if any(k in s for k in _VERSE_DIALOG):
        return False
    if s.endswith(("：", ":", "；", ";")):
        return False
    for clause in _VERSE_CLAUSE_SPLIT.split(s):
        piece = clause.strip().strip(_VERSE_TAILS).strip()
        if not piece:
            continue
        if piece[0] in _VERSE_SUBJECT:
            return False
        if piece[-1] in _VERSE_STOP:
            return False
        if any(ch in _VERSE_MID_STOP for ch in piece[1:]):
            return False
    return any(ch in s for ch in "，。！？、；：")


def _mark_verse(items: list[_Line]) -> int:
    """就地给 _Line 打上 verse 标记，返回标记行数。

    三条规则，都要求"附近有硬证据"，不做纯几何猜测：

      1. 引导语向后：某行以「词曰：/诗曰：/为证：」结尾时，它**后面**连续
         像韵文的行都是诗。引导语那一行本身不标 —— 它是叙述句
         （「果然写出一首诗道：」），排成楷体居中的话读者会以为那行也是诗。
      2. 曲牌向前：「右调·忆秦娥」出现在诗**之后**，因此反过来向前回溯。
      3. 引导语后面至少要有**一行**像韵文的行，否则整段撤回
         （引导语本身可能是"他说："这类误判）。只要有一行就标 ——
         有些诗只有两句（"写出两句诗道："后面紧跟一行七言对句），
         要求"至少两行"会把它们漏掉。

    为什么必须放在并碎行**之后**：并碎行会把相邻的碎句接起来，段落边界
    在并完之后才是最终的。在并之前标记，标记会落到已经被合并掉的行上。
    """
    body = [it for it in items if not it.blank and not it.is_title]
    count = len(body)
    marks = [False] * count

    for i, it in enumerate(body):
        if not _lead_close(it.content):
            continue
        j = i + 1
        run = 0
        while j < count and _verse_like(body[j].content):
            marks[j] = True
            run += 1
            j += 1
        # 引导语后面一行都不像韵文：这条引导语判错了，撤回。
        if run < 1:
            for k in range(i + 1, j):
                marks[k] = False

    for i, it in enumerate(body):
        if not _tail_mark(it.content):
            continue
        marks[i] = True
        j = i - 1
        run = 0
        while j >= 0 and _verse_like(body[j].content):
            marks[j] = True
            run += 1
            j -= 1
        if run < 2:
            marks[i] = False

    # 未被任何硬证据覆盖、但前后都是韵文的行也补上 —— 词里常有长短句，
    # 其中一句恰好只有三四个字、或干脆没有句读。
    for i in range(1, count - 1):
        if marks[i]:
            continue
        if marks[i - 1] and marks[i + 1] and _verse_like(body[i].content):
            marks[i] = True

    for it, mark in zip(body, marks):
        it.verse = mark
    return sum(1 for m in marks if m)


# ---------------------------------------------------------------------------
# 七、并碎行
# ---------------------------------------------------------------------------


def _indent_is_informative(items: list[_Line]) -> bool:
    """判断"缩进"在这本书里是否还携带段落信息。

    缩进行占比超过 INDENT_SIGNAL_LOW 就算携带信息，**不设上界**。
    上界会让整个流水线不幂等：第一遍 89% 缩进判为有效、不并缩进行；
    render 给每段补上缩进后第二遍变成 100%，旧判据认为"没信号"，
    于是所有段落被焊成一段。幂等是「clean(render(clean(x))) == clean(x)」
    这条底线，比"多并几段"重要得多。
    """
    body = [it for it in items if not it.blank]
    if not body:
        return False
    # 文件里有段首占位符（???? / !!!!）时，缩进**不再**是段落信号。
    #
    # 这类文件（网页抓取后缩进丢失）的形态是"每一行都带 4 个半角空格"，
    # 那是采集器的痕迹，不是作者的分段意图；真正的段首靠 ???? 写。
    # 此时若还把缩进当信号，"不并缩进行"会让 14 万行保持 14 万个假段落，
    # 而原作者的真实段落（靠 ???? 划分）反而被淹没。
    if any(it.para_start for it in body):
        return False
    ratio = sum(1 for it in body if it.indented) / len(body)
    return ratio > INDENT_SIGNAL_LOW


def _join(a: str, b: str) -> str:
    """把两段正文接起来。

    中文之间不加空格；只有"两边都是半角字母数字"时才补一个空格，
    否则 "hello" + "world" 会粘成 "helloworld" 这种假词。
    """
    if not a:
        return b
    if not b:
        return a
    if a[-1].isascii() and a[-1].isalnum() and b[0].isascii() and b[0].isalnum():
        return a + " " + b
    return a + b


def _merge_fragments(
    items: list[_Line],
    *,
    merge_breaks: frozenset[int] = frozenset(),
    hard: bool = False,
    width: int = 0,
) -> tuple[list[_Line], list[dict], int, int, int, int]:
    """把被硬折断的半句接回去。

    判据（任一不满足就不合并，宁可少并）：
    1. 前一行**没有**停在句末标点上 —— 这是"半句"的定义；
    2. 当前行不是标题、不是空行、不是广告行；
    3. 当前行没有段首缩进 —— 缩进是段落边界，有缩进说明作者本来就要分段
       （缩进本身没有信号时这条失效，见 _indent_is_informative）；
    4. 合并后长度不超过 MERGE_MAX_CHARS。

    为什么广告行不参与合并：行内广告是待切除的病灶，把它和后面的正文粘在
    一起，M4 切除时就得多切一刀，还容易连正文一起切掉。让病灶保持孤立。

    **例外：硬折行碎片（it.wrap_frag）**。这种行已被闸门判出"是上一段被
    硬折行切开的半截"，判据 1、3 对它不适用 —— 折行点本来就落在句子中间，
    上一行当然没停在句末标点上；缩进则是采集器给的，不是作者的分段意图。
    放宽这几条是有代价的（可能把两个真段落焊成一段），但**不合并的代价更大**：
    它会作为独立行留在正文里，第二遍清洗时站在段首，段中判据不拦它，章数
    立刻翻回去，幂等破掉（实测 #173 章数 276→605、#309 331→344）。
    所以这里对碎片是"能并就必须并"。

    并得回去的碎片才算真的被拒（wrap_rejected 返回值）；并回去的（长度超限、
    撞上段首占位符或分隔线）就把标题**还回去**，当它没被拒过。宁可少拒一个
    假章，也不许破幂等 —— 幂等是 M2 契约，假章只是美观问题。

    merge_breaks 是人工指定"这一行不许并进上一行"的行号集合（见 _parse_adjust）。
    它只**否决合并**，不改任何文本：用户看着一段被焊死的两段，点"这里断开"，
    重跑时这一行就单独成段。注意它和第 1 条判据是两回事 —— 判据 1 是规则自己
    判断"上一行没写完"，merge_breaks 是人工覆盖规则，优先级更高。

    返回 (合并后的列表, 合并组全量清单, 被吸收的行数,
    合并组总数, 被拒掉的硬折行碎片数, 被人工断开合并的次数)。
    组数与被吸收行数必须分开两个数：组数是"并了几处"，被吸收行数是"吞掉几行"，
    一处合并可能吞 4 行。两者互推不出来，报告里各占一格。
    """
    informative = _indent_is_informative(items)
    # 局部闸门：全书不是硬折行，不代表这一段附近不是（合集类书就是这样）。
    # 窗口命中时，这一行的缩进与长度上限都按硬折行放宽，判据与全书级一致。
    local_hard, local_width, block_join = _local_hard_info(items)
    # 当前载体"最后一片"的原始行长（不是合并后的总长）。硬折行的判据要用它：
    # 合并之后 prev.content 会越并越长，拿它跟折行宽度比就永远成立 ——
    # 实测那样一本 3976 段的书会被并成 78 段。
    tail_len = 0
    out: list[_Line] = []
    groups: list[dict] = []
    # 正在拼的那一组（out[-1] 是它的载体）。记下来是为了报告能写出
    # "原文第 17~19 行并成了一段"，而不是"并了 2 次"这种没法核对的说法。
    open_group: dict | None = None
    absorbed = 0
    group_total = 0
    wrap_rejected = 0
    breaks_hit_lines: set[int] = set()

    for it in items:
        prev = out[-1] if out else None
        # 硬折行碎片：放宽"上一行必须没写完""当前行不能有缩进"两条。
        forced = it.wrap_frag
        # 这一行附近是不是硬折行（全书级闸门没开时的第二道证据）。
        # block_join 也算：那块已经判成"整块就是一段"，块里的行当然按硬折行口径
        # 处理 —— 缩进是折行工具给的，长度上限也不该管。
        hard_here = hard or it.no in local_hard or it.no in block_join
        width_here = width or local_width.get(it.no, 0)
        can_merge = (
            prev is not None
            and not it.blank
            and not prev.blank
            and not it.is_title
            and not prev.is_title
            and it.ad is None
            and prev.ad is None
            # 人工保护的行不许被焊进邻段，也不许把邻段吸进自己身上：
            # 规格要求"这一行的文字别动"，改了它的分段就是改了它的结构。
            and not it.frozen
            and not prev.frozen
            # 硬折行区里还有两条更强的证据：
            #   a. 上一行**正好占满折行宽度** —— 它是被工具切开的，哪怕恰好切在
            #      一个句号/收尾引号后面也一样（折行点落在「」」后面是常事）。
            #      没有这条，凡是"以引号收尾的折行块"都会被当成段落结尾。
            #   b. 这一行属于一个**小块**（2~10 行、块里每行都在硬折行区）——
            #      这种块整块就是一段，行行收在句号上只是"句子优先折行"的结果。
            and (
                forced
                or not _ends_terminal(prev.content)
                or (hard_here and width_here and tail_len >= width_here - 2)
                or it.no in block_join
            )
            # 以冒号/分号结尾的行是在"引出下文"（"诗曰："/"词曰："/"有诗为证："），
            # 它和下一行不是一句被折断的句子，合并会把引导语和韵文焊成一段，
            # 后面 _mark_verse 就再也认不出这一块是诗了。
            and (forced or it.no in block_join or not _ends_lead(prev.content))
            and prev.last_no == it.no - 1  # 必须物理相邻：中间隔了空行就是分段
            # 段首占位符是原作者写下的分段证据，比任何启发式都硬。
            # 这类文件（网页抓取、缩进丢失）唯一的段落线索就是它，
            # 放过去整本书会被并成几千个几百字的假段落。
            and not it.para_start
            # 分隔线是独立的一块排版，并进上一段就变成句尾的乱码。
            and not it.sep
            and not prev.sep
            # hard = 这本书是硬折行的（见 _hardwrap_ratio）：那时候缩进是
            # 折行工具给的，不携带分段信息；长度上限也不该管 —— 一整段被切成
            # 十几行，并回去当然会超过 800 字，卡上限就是把真段落截成两半。
            # 但"缩进不算数"必须同时有折行宽度撑腰：只看"行尾不带句末标点"
            # 就放行，会把「一行一句、行行以逗号收尾」的正常书也当成硬折行，
            # 整章焊成一段 —— 实测《书丑》因此少掉 71 段。
            and (
                forced
                or not (informative and it.indented)
                or (hard_here and width_here)
            )
            and (
                hard_here
                or it.no in block_join
                or len(prev.content) + len(it.content) <= MERGE_MAX_CHARS
            )
        )
        if can_merge and it.no in merge_breaks:
            # 人工否决：这一行是用户指定的段落起点。只统计"真的拦住了一次
            # 本来会发生的合并"，别把"给了个行号但它本来就不合并"也算进去 ——
            # 那样报告里的数就不可信了。
            can_merge = False
            breaks_hit_lines.add(it.no)
        if can_merge:
            merged = _join(prev.content, it.content)
            # 合并之后必须把"标点归一 + 引号归一"整条重跑一遍。
            #
            # 为什么：这两个归一都是**逐行独立**跑的（_scan 里对每行各调一次），
            # 而它们都带状态或跨字符视野：
            #   · normalize_quotes 是状态机 —— 行内引号层数的奇偶决定这一行用
            #     双引号还是单引号。一段跨 5 行、每行各带半个引号时行级各判各的；
            #     合并成一段后二轮拿到整行、状态机从零重数，实测 203 个段落
            #     一轮输出单引号、二轮输出双引号。
            #   · normalize_punctuation 折叠连续重复标点（"噢～～"→"噢～"）——
            #     重复符号正好跨在行尾与下一行行首时，行级各压各的压不干净，
            #     二轮对整行压一次才干净，实测 98 个段落因此差一两个字符。
            # 两步各自幂等（实测 6 组样本全幂等），重跑既修二轮也不破坏一轮。
            merged = normalize_quotes(normalize_punctuation(merged))
            prev_text = prev.content      # 合并前的载体文本，给 pieces 留底
            prev.text = (INDENT_STR if prev.indented else "") + merged
            prev.last_no = it.no          # 后续行要跟它比相邻
            tail_len = len(it.content)
            absorbed += 1
            if forced:
                wrap_rejected += 1
            if open_group is not None:
                open_group["lines"][-1] = it.no      # 末行号跟着走
                open_group["pieces"].append({"line": it.no, "text": it.content})
                open_group["result"] = merged
            else:
                # pieces 记的是**每一片原文**，界面要拿它画"这几行并成了一段"；
                # result 记**拼好的整段**（不截断），界面的右栏直接读它。
                # 两者都全量记：样例只留 8 条是为了报告好读，但复核界面
                # 要能列出每一处合并（决定29），截断就没得看了。
                open_group = {
                    "lines": [prev.no, it.no],
                    "pieces": [
                        {"line": prev.no, "text": prev_text},
                        {"line": it.no, "text": it.content},
                    ],
                    "result": merged,
                }
                group_total += 1
                groups.append(open_group)
            continue
        if forced:
            # 并不到上一段 → 这个碎片不许被拒。标题还回去，文本按标题口径
            # 重算一次（_scan 那时按正文口径删过行内空白，"第N章 标题"会
            # 变成"第N章标题"，目录里就少了那个分隔）。
            it.heading = it.wrap_heading
            it.wrap_heading = None
            it.wrap_frag = False
            body, _ = strip_lead_marker(it.raw)
            it.text = normalize_line(
                _strip_site_prefix(strip_frame(strip_zero_width(body))),
                keep_inline_space=True,
            ).text
        open_group = None
        out.append(it)
        tail_len = len(it.content)
    return out, groups, absorbed, group_total, wrap_rejected, breaks_hit_lines


# ---------------------------------------------------------------------------
# 六点五、猜书名与作者
# ---------------------------------------------------------------------------

# 只在前几段里找：书名与作者一定在正文最前面。往后扫只会扫到
# 正文里提到的别的书（例如人物随手翻开一本别的书）。
METADATA_SCAN = 8

# 书名号包裹、且整段只有书名。必须整段独占：正文里提到别的书名不算。
_TITLE_BOOK_MARKS = re.compile(
    r"^[\s\u3000]*[\u300a<]([^\u300a\u300b<>]{1,40})[\u300b>][\s\u3000]*$"
)

# 前缀式：「书名：书D」。论坛搬运的 TXT 几乎都这么写，
# 而且后面常跟「作者：」「首发」「链接：」几行一起出现。
# 只认「书名」「作品名」「小说名」三种 —— 「标题」「名字」在正文里太常见。
_TITLE_PREFIX = re.compile(
    r"^[\s\u3000]*(?:书\s*名|作\s*品\s*名|小\s*说\s*名)"
    r"\s*[:：]\s*([^\s，。；：]{1,40})[\s\u3000]*$"
)

# 作者行的几种常见写法。全部要求**整段匹配**，不做行内搜索 ——
# 行内搜索会把正文里任何一句带「著」字的话认成作者署名。
#   明·某某        → 朝代 + 间隔号 + 名字（古籍整理本的常见写法）
#   作者：某某 / 著者:某某
#   兰陵笑笑生 著
_AUTHOR_DYNASTY = re.compile(r"^[\s\u3000]*([\u4e00-\u9fff]{1,4})[\u00b7\u30fb]\s*([^\s\uff0c\u3002\uff1b\uff1a]{1,20})[\s\u3000]*$")
_AUTHOR_PREFIX = re.compile(r"^[\s\u3000]*(?:\u4f5c\s*\u8005|\u8457\s*\u8005)\s*[:\uff1a]?\s*([^\s\uff0c\u3002\uff1b\uff1a]{1,20})[\s\u3000]*$")
_AUTHOR_SUFFIX = re.compile(r"^[\s\u3000]*([^\s\uff0c\u3002\uff1b\uff1a]{1,20})\s*\u8457[\s\u3000]*$")

# 「作者简介」「作者的话」「作者：暂无」这类不是作者名。判据是整段匹配、又允许
# 「作者」后面不带冒号（「兰陵笑笑生 著」就是这么写的），于是「作者简介」会被
# 读成作者=「简介」——《书G》的扉页因此印着「作者：简介」。
# 这里列的是"出现在作者位但明显是版面词"的短串，只做整串比对，不做包含判断。
_AUTHOR_BLACKLIST = frozenset(
    {
        "简介", "介绍", "说明", "声明", "内容", "目录", "序", "前言", "后记",
        "编辑", "推荐", "责编", "校对", "排版", "整理", "录入", "手打",
        "未知", "佚名", "不详", "暂无", "无", "网络", "转载", "原创",
    }
)


# 文件名里的「作者：某某」。手机阅读器导出的 TXT 常常正文里一个元数据行都没有，
# 书名作者全写在文件名上（「书E 作者：某甲.txt」）。
# 为什么必须锚定在尾部、且「作者」后要么跟冒号要么跟空白：不带冒号时
# 「（作者名）」里的「作者」二字会被从中间匹配，把「（」留在书名、「名）」
# 当作者（见 bug2 实测）；而「书E 作者 某甲」这类无冒号写法是合法的，
# 用「\s+」分支放行 —— 「作者名」中间没有空白，仍不会误匹配。
_FILENAME_AUTHOR = re.compile(r"[\s\-_—·]*作\s*者\s*(?:[:\uff1a]\s*|\s+)(.+)$")

# 末尾一对括号里包着的作者名：「书名（作者名）」「三体【全集】（刘慈欣）」。
# 搬运站把作者塞进文件名末尾括号里、且不带「作者：」二字，是常见写法；
# 没有这条会被下面的装饰括号规则整个剥掉，作者永远为空。
_FILENAME_TAIL_AUTHOR = re.compile(
    r"[\s\-_—·]*[（(]\s*([^（()）【\[\]】《》\s]{1,20})\s*[)）]\s*$"
)

# 文件名两端的装饰括号与收尾标注：「【书F】（全）」「（1-44完）」「[精校版]」。
# 只剥成对括号包住的那一段，不按字符剥（按字符剥会把「妈妈】」这种半边括号吃掉）。
_FILENAME_DECOR_BODY = r"[^（()）【\[\]】《》]{0,12}"
# 整串被一对括号包住：「【书F】」——括号只是装饰，书名在里面。
_FILENAME_WRAPPED = re.compile(
    r"^\s*([（(【\[《])\s*(" + _FILENAME_DECOR_BODY + r")\s*([)）】\]》])\s*$"
)
_FILENAME_HEAD = re.compile(
    r"^\s*[（(【\[《]\s*" + _FILENAME_DECOR_BODY + r"\s*[)）】\]》]\s*"
)
_FILENAME_TAIL = re.compile(
    r"\s*[（(【\[《]\s*" + _FILENAME_DECOR_BODY + r"\s*[)）】\]》]\s*$"
)
_FILENAME_PAIRS = {"（": "）", "(": ")", "【": "】", "[": "]", "《": "》"}
# 文件名尾巴上的网址：「(www.example-novel.com)」「[m.abc.com]」。搬运站习惯把自己的
# 域名塞进文件名，长度普遍超过装饰括号的 12 字上限，剥不掉就轮不到"去掉装饰"
# 那一步，最后整串带着域名进广告判据、书名被判成空
# （《书B(www.example-novel.com).txt》就是这样丢掉书名的）。
_FILENAME_URL_TAIL = re.compile(
    r"[\s\-_—·]*[（(\[【]?\s*(?:www\.|https?://|m\.|wap\.)[^\s）)\]】]{2,60}\s*[）)\]】]?\s*$",
    re.IGNORECASE,
)
# 括号里全是这类字，说明它标的是"全本/第几册"，不是书名。
_FILENAME_MARKER_ONLY = re.compile(r"^[全完本完结精校版上中下册卷集部第0-9０-９\-—~～至到\s]+$")

# 末尾括号里的「版本/形态标注」与「裸站点名」不是作者名，但上面三条判据
# （纯标注字符类、版面词黑名单、广告/网址判据）恰好都挡不住：例如「完整版」
# 的「整」不在 _FILENAME_MARKER_ONLY 的字符类里，「biquge」既无域名也无广告
# 词，judge_ad_line 直接返回 None。这条只在 P1 的末尾括号分支里用，不并入
# _FILENAME_MARKER_ONLY：后者还管书名收口，把 A-Za-z 放进去会把
# 「Harry Potter」这类纯英文书名误判成标注清空。
# 形态一：以版本/形态字收尾的短串（「完整版」「无删节」「第一卷」…）
# 形态二：纯 ASCII 字母数字（「xyz」「biquge」「Priest」…）—— 括号里的
#   搬运站代号几乎都是这种；带点/空格的「J.K.Rowling」不匹配，仍可当作者。
_FILENAME_AUTHOR_NOISE = re.compile(
    r"^(?:[A-Za-z0-9]+|[\u4e00-\u9fff]{0,8}"
    r"[版集册本节减体完结校订修藏简精全合排整未无卷部篇传]"
    r"|(?:TXT|txt)[\u4e00-\u9fff]{0,3})$"
)
_FILENAME_AUTHOR_NOISE_WORDS = frozenset(
    {"外传", "番外", "前传", "后传", "正文", "补遗", "番外篇"}
)


def _filename_marker(text: str) -> bool:
    """这段文字是不是纯标注（「（全）」「1-44完」「第3册」）。

    判之前把括号本身去掉：「（全）」的括号里只有一个"全"，它就是标注。
    """
    core = re.sub(r"[（(【\[《)）】\]》\s]", "", text)
    return bool(core) and _FILENAME_MARKER_ONLY.match(core) is not None


def _strip_filename_decor(stem: str) -> str:
    """反复剥文件名两端的成对装饰括号，返回剩下的书名候选。

    为什么不能一句 _FILENAME_DECOR.sub 循环到不动：
    「【书F】（全）」先剥头部剩「（全）」，再剥尾部就空了 —— 书名被自己的
    装饰吃掉，最后拿到一个空书名。所以顺序固定成：先看整串是不是被一对括号
    包着（是就去掉这层括号），再剥尾部标注，最后剥头部装饰；每一步都要求
    "剥完还剩东西"。
    """
    while True:
        m = _FILENAME_WRAPPED.match(stem)
        if m is not None:
            inner = m.group(2).strip()
            if (
                inner
                and _FILENAME_PAIRS.get(m.group(1)) == m.group(3)
                and not _FILENAME_MARKER_ONLY.match(inner)
            ):
                stem = inner
                continue
        # 网址尾巴先于装饰括号剥：它常被括号包着但长度超出装饰上限。
        m = _FILENAME_URL_TAIL.search(stem)
        if m is not None and stem[: m.start()].strip():
            stem = stem[: m.start()]
            continue
        m = _FILENAME_TAIL.search(stem)
        if m is not None and stem[: m.start()].strip() and not _filename_marker(stem[: m.start()]):
            stem = stem[: m.start()]
            continue
        m = _FILENAME_HEAD.match(stem)
        if m is not None and stem[m.end() :].strip() and not _filename_marker(stem[m.end() :]):
            stem = stem[m.end() :]
            continue
        return stem


def _metadata_from_filename(source: str) -> tuple[str, str]:
    """从**文件名**里猜书名与作者，猜不到返回 ("", "")。

    为什么需要这条兜底：手机阅读器/网盘导出的 TXT 经常正文里没有任何书名页
    （《书E》25280 行里找不到一行「书名：」），书名作者只存在于文件名。
    没有这条，这类书进 EPUB 后标题栏是空的，书库里一排「未命名」。

    为什么只在 source 以 .txt 结尾时用：clean_text() 的测试与库调用传的是
    逻辑名（"x"），不是真文件，拿它当书名会凭空造出一个假书名。

    为什么文件里的元数据行优先：正文里的「书名：」是作者/搬运者写进内容的，
    比文件名更可信；文件名可能被下载工具改过（「xxx(1).txt」）。
    """
    if not source:
        return "", ""
    name = Path(source).name
    if not name.lower().endswith(".txt"):
        return "", ""
    stem = name[:-4]
    author = ""
    # 先认带「作者：」/「作者 」的写法，再认末尾括号里的裸作者名。两者都
    # 要求锚定在文件名尾部 —— 中间出现的「作者」二字（「书名作者访谈.txt」）
    # 不是作者位。末尾括号一旦出现就剥掉：它要么是作者，要么是标注/站点名，
    # 留在书名里都会把结果带歪（review A1）。
    m = _FILENAME_AUTHOR.search(stem)
    if m is not None:
        author = m.group(1).strip()
        stem = stem[: m.start()]
    else:
        m = _FILENAME_TAIL_AUTHOR.search(stem)
        if m is not None:
            candidate = m.group(1).strip()
            # 纯标注（「（全）」「（完结）」）、版面词（「简介」「目录」…）、
            # 版本/形态标注（「完整版」「番外」）与裸站点名（「biquge」）
            # 都不是作者名；长度/广告判据在下面统一收口。
            if (
                not _filename_marker(candidate)
                and candidate not in _AUTHOR_BLACKLIST
                and judge_ad_line(candidate) is None
                and _FILENAME_AUTHOR_NOISE.match(candidate) is None
                and candidate not in _FILENAME_AUTHOR_NOISE_WORDS
                and 1 <= len(candidate) <= 20
            ):
                author = candidate
            stem = stem[: m.start()]
    stem = _strip_filename_decor(stem)
    # 「作者」后面可能还挂着「（完结）」这类装饰，作者名本身不该带它。
    author = _strip_filename_decor(author).strip(" \t-_—·")
    title = stem.strip(" \t-_—·")
    # 无冒号又无空白的「书名 作者某甲」认不出作者，整串会当书名；
    # 留下「作者」二字的书名明显不是书名，宁可空着（review A2）。
    if "作者" in title:
        title = ""
    if not (1 <= len(title) <= 40) or judge_ad_line(title) is not None or _filename_marker(title):
        title = ""
    if not (1 <= len(author) <= 20) or author in _AUTHOR_BLACKLIST or judge_ad_line(author) is not None:
        author = ""
    return title, author


def _guess_metadata(paragraphs: list[Paragraph]) -> tuple[str, str]:
    """从正文最前面几段里猜书名与作者，返回 (书名, 作者)。

    猜不到就返回空串 —— **宁可空着也不瞎填**。书名作者错了不会让程序崩，
    但会印在 EPUB 的信息页、书库列表和阅读器标题栏上，比空着难看得多。
    所以每条判据都要求整段匹配、长度受限，而且只在前 METADATA_SCAN 段里找。

    为什么放在清洗层而不是 EPUB 层：书名与作者是**书的属性**，不是某种输出
    格式的属性。存进章节树，以后渲染 TXT、AZW3 都能直接用。
    """
    title = ""
    author = ""
    for p in paragraphs[:METADATA_SCAN]:
        text = p.text.strip()
        if not text or len(text) > 40:
            continue
        if not title:
            m = _TITLE_BOOK_MARKS.match(text)
            if m is not None:
                cand = m.group(1).strip()
                # 书名号里包着广告（书名号里的串自己就是广告）不认。
                if cand and judge_ad_line(cand) is None:
                    title = cand
                    continue
            m = _TITLE_PREFIX.match(text)
            if m is not None:
                cand = m.group(1).strip()
                # 同上：前缀后面挂着一串网址的不认。
                if cand and judge_ad_line(cand) is None:
                    title = cand
                    continue
        if not author:
            m = _AUTHOR_DYNASTY.match(text)
            if m is not None:
                # 朝代保留：「明·某某」比「某某」信息多。
                cand = f"{m.group(1)}·{m.group(2)}"
                if cand not in _AUTHOR_BLACKLIST:
                    author = cand
                    continue
            m = _AUTHOR_PREFIX.match(text)
            if m is not None:
                cand = m.group(1)
                if cand not in _AUTHOR_BLACKLIST:
                    author = cand
                    continue
            m = _AUTHOR_SUFFIX.match(text)
            if m is not None:
                cand = m.group(1)
                if cand not in _AUTHOR_BLACKLIST:
                    author = cand
                    continue
        if title and author:
            break
    return title, author

# ---------------------------------------------------------------------------
# 八、组装章节树
# ---------------------------------------------------------------------------


# 章首元数据块的段数上限。有的作者在每章开头重复一遍「书名+章名」「作者」
# 「日期」「字数」，真正的章标题跟在后面几行 —— 前一条标题下的段数不超过这个数，
# 且与后一章同名时，判为同一个标题写了两遍（见 _build_book 的空壳合并）。
_STUB_MAX_PARAGRAPHS = 6


def _build_book(items: list[_Line], *, source: str, encoding: str) -> tuple[Book, dict]:
    """把行流切成章节树。

    front_matter 收"第一个标题之前"的内容（前言、残留广告、版权页）。
    tail_matter 这里永远是空的：最后一章之后的行归属最后一章更符合阅读预期
    （"全书完""作者的话"本来就在最后一章里），单独拆出来反而会让渲染层
    不知道要不要加分隔。留空字段是为了以后真要拆时有地方放。

    章节边界的空行一律丢掉：标题前后要不要空行是排版问题，
    由 render 层统一决定（它按"标题前后各一空行"输出）。中间态里留着，
    换一种输出格式就得再洗干净一次。
    """
    book = Book(source=source, encoding=encoding)
    current: list[Paragraph] | None = None      # None => front_matter

    for it in items:
        if it.is_title:
            title = it.content.strip()
            # 空壳标题合并：同一章的标题被原书写了两遍（一次在章首、一次是站点
            # 水印，如「正文 第009章：妙手回春」紧跟「山村野事 第009章：妙手回春」），
            # 第一遍后面一个字都没有。保留它就是一个 0 段落的空章节 + 一条
            # "一个段落都没有"的假警告；合并则两行标题归一章，正文一段不少。
            # 判据用 toc_key 归一后比较：前缀、空格、装饰、数字写法全都抹平。
            #
            # 为什么不把"被合并掉的标题行"记进 dropped_lines：那份清单的口径是
            # "这一行的内容会不会消失"，给复核界面列"别删这一行"的勾选项。
            # 这里合并的是**标题字符串本身**（章还是那一章、正文一段不少），
            # 勾选它没有任何可保护的东西 —— 记进去只会让用户看到一堆勾了也
            # 没用的行。这是既有的口径边界（review 曾建议统一记账），维持现状。
            if book.chapters:
                prev = book.chapters[-1]
                prev_key = toc_key(_strip_site_prefix(prev.title))
                if prev_key and prev_key == toc_key(_strip_site_prefix(title)):
                    if not prev.paragraphs:
                        # 保留先出现的那个标题（它在原文件里更靠前，通常是正式标题），
                        # 只把 start_line 让给后一个 —— 空壳自己不需要位置。
                        prev.start_line = min(prev.start_line, it.no)
                        continue
                    if len(prev.paragraphs) <= _STUB_MAX_PARAGRAPHS:
                        # 章首元数据块：《书J》每一章前面都重复一遍
                        # 「【书J】第二章」+ 作者/日期/字数，紧接着才是真正的
                        # 「第二章」。前一条标题下面只有这几行，后一条才是正文起点。
                        # 并成一章（元数据行留在章首），否则目录里每章都多一个
                        # 5 段落的同名空壳。判据同上：toc_key 归一后同名。
                        assert it.heading is not None
                        ch = model.Chapter(
                            title=title,
                            number=it.heading.number,
                            level=it.heading.level,
                            start_line=min(prev.start_line, it.no),
                        )
                        ch.paragraphs.extend(prev.paragraphs)
                        book.chapters[-1] = ch
                        current = ch.paragraphs
                        continue
            # 层级与序号用**已解析好的 heading**，不交给 chapter_from_title 重解析：
            # 「卷之一 觉后禅——春」这种行 parse_heading 认不出来（它要求「第N卷」），
            # 重解析会把清洗层费力升级出来的 level=2 又拍回 level=1，
            # 于是整本书的卷结构在章节树里再次消失。
            assert it.heading is not None
            ch = model.Chapter(
                title=title,
                number=it.heading.number,
                level=it.heading.level,
                start_line=it.no,
            )
            book.chapters.append(ch)
            current = ch.paragraphs
            continue
        if it.blank:
            # 章首的空行不留（紧跟在标题后面）；章内空行留作场景分隔
            if current and current:
                current.append(Paragraph("", kind="blank"))
            continue
        target = current if current is not None else book.front_matter
        # 诗词/韵文单独一类：渲染层据此居中换楷体（见 epub.py 的 p.verse）。
        if it.sep:
            kind = "sep"
        elif it.verse:
            kind = "verse"
        else:
            kind = "body"
        target.append(Paragraph(it.content, kind=kind))

    for lst in [book.front_matter, *(c.paragraphs for c in book.chapters)]:
        while lst and lst[-1].kind == "blank":
            lst.pop()

    # 书名与作者从最前面几段里猜（见 _guess_metadata）。先看第一个章节之前的
    # 内容 —— 那里本来就是书名页/版权页的位置；不够再往后看第一章开头几段，
    # 因为不少 TXT 是"第一章标题 → 书名 → 作者 → 正文"这种顺序。
    probe: list[Paragraph] = list(book.front_matter)
    if book.chapters:
        probe.extend(book.chapters[0].paragraphs)
    book.title, book.author = _guess_metadata(probe)
    # 正文里一个元数据行都没有时（手机阅读器导出的书几乎都这样），退回到文件名。
    if not book.title or not book.author:
        fname_title, fname_author = _metadata_from_filename(source)
        if not book.title:
            book.title = fname_title
        if not book.author:
            book.author = fname_author
    return book, {}


# ---------------------------------------------------------------------------
# 八点五、可疑字符
# ---------------------------------------------------------------------------

# 三类"几乎不可能出现在正常中文小说正文里"的字符：
#   U+E000-U+F8FF 私用区：编码转换工具遇到表示不了的字会往这里塞，
#                 解码出来就是一串没人认识的符号；
#   U+AC00-U+D7AF 谚文音节：中文书里成片出现只可能是解码把别的编码当成了 GB18030；
#   U+FFFD        替换符：解码失败留下的洞。
# 它们不是清洗造成的（清洗只动空白、标点与广告），所以不能"修"，只能告诉人。
_SUSPICIOUS_HINT = "私用区字符 / 谚文 / 解码替换符"


def _suspicious_chars(book: Book) -> tuple[int, list[str]]:
    """数一数书里有几个可疑字符，返回 (总数, 前几个样例)。

    为什么要在清洗报告里报这个：这类字符会原样进 EPUB，正文里突然冒出一个
    莫名其妙的符号，读者只会以为是清洗洗坏了。其实是原文件就带着它
    （常见于多年前被反复"转换过编码"的盗版 TXT）。说清楚比闷着强。
    """
    counts: dict[str, int] = {}
    for p in [*book.front_matter, *(q for c in book.chapters for q in c.paragraphs)]:
        for ch in p.text:
            o = ord(ch)
            if 0xE000 <= o <= 0xF8FF or 0xAC00 <= o <= 0xD7AF or o == 0xFFFD:
                counts[ch] = counts.get(ch, 0) + 1
    total = sum(counts.values())
    samples = [f"{ch}（U+{ord(ch):04X}）" for ch in list(counts)[:5]]
    return total, samples


# ---------------------------------------------------------------------------
# 九、报告
# ---------------------------------------------------------------------------


def _short_lines(nums: list[int], limit: int = 5) -> str:
    """把行号列表写成「第 3、8、12 行（等 7 个）」这种短串，给警告文案用。

    警告是给人看的：一屏几十个行号没人读得下去，前 5 个足够对上号，
    剩下的用总数交代清楚。
    """
    head = "、".join(str(n) for n in nums[:limit])
    more = f"（共 {len(nums)} 个）" if len(nums) > limit else ""
    return f"第 {head} 行{more}"


# 目录残留在开头留下的空章，长这个样子：一章都没有正文，标题像目录条目，
# 编号还往回走 —— 因为目录条目本身是 1、2、3……，紧跟其后的正文又从 1 说起。
# 前缀至少要这么长才拿出来说：编号回落这条证据已经够硬，不必再拧长度门槛。
_TOC_EMPTY_MIN = 2


def _toc_like_empty_prefix(book: Book) -> dict | None:
    """认出「开头连续若干 0 段章 + 编号回落」这一种目录残留。

    **只负责认和报告，一行都不删**：目录块删错比留着糟得多 —— 真前言被当成
    目录删掉，用户是找不回来的。所以这里只产出"这是怎么回事、出口在哪"。

    判据两条，缺一不可：
      1. 从第一章起，连续若干章（卷容器 level=2 不算）一个正文段落都没有；
      2. 编号能证明这是目录 —— 要么这批空章自己的编号在回落（或不动），
         要么紧跟其后的第一个有编号的章，编号不大于这批空章的最后编号。
         目录列到第 N 章、正文又从第 1 章说起，就是这个形状。

    返回 None 表示没认出来。认出来时给 {"count", "first_line", "last_line",
    "titles"}，文案与界面都从这里取数，不各自猜。
    """
    chapters = list(getattr(book, "chapters", []) or [])
    if len(chapters) < _TOC_EMPTY_MIN + 1:
        return None
    head: list = []
    for ch in chapters:
        if ch.level == 2:
            break
        if any((p.text or "").strip() for p in ch.paragraphs):
            break
        head.append(ch)
    if len(head) < _TOC_EMPTY_MIN:
        return None
    nums = [ch.number for ch in head if ch.number is not None]
    if len(nums) < _TOC_EMPTY_MIN:
        return None
    # 空章自己就在回落 / 重复：目录条目倒着列，或者压根没有编号递增的意思。
    fell = any(b <= a for a, b in zip(nums, nums[1:]))
    if not fell:
        # 空章一路递增（1、2、3…N），那就看正文从哪儿接上：又从 1 开始才算目录。
        rest = [ch.number for ch in chapters[len(head):] if ch.number is not None]
        fell = bool(rest) and rest[0] <= nums[-1]
    if not fell:
        return None
    return {
        "count": len(head),
        "first_line": int(head[0].start_line or 0),
        "last_line": int(head[-1].start_line or 0),
        "titles": [(ch.title or "").strip() for ch in head[:3]],
    }


def _build_report(
    *,
    file: str,
    size_bytes: int,
    encoding: str,
    confidence: float,
    total_lines: int,
    blank_in: int,
    nonblank_in: int,
    dropped_ad: int,
    dropped_toc: int,
    dropped_marker: int = 0,
    ad_inline_cut: int,
    ad_inline_keep: int,
    fixed: dict,
    ad_samples: list[dict],
    inline_samples: list[dict],
    toc_samples: list[dict],
    merged_groups: list[dict],
    merged_group_total: int,
    merged_absorbed: int,
    kept_last_toc_line: int | None,
    book: Book,
    oneline: dict | None = None,
    toc_unsure: bool = False,
    verse_lines: int = 0,
    ad_title: int = 0,
    wrap_reject: int = 0,
    wrap_suspect: int = 0,
    dropped_lines: list[dict] | None = None,
    adjust_info: dict | None = None,
) -> dict:
    """组装清洗报告字典（结构被 cleanreport.py 冻结，键名不许改）。

    数字的意义必须能互相对上，否则报告就成了摆设：
      total = nonblank_in + blank_in
      blank_out = 章节树里 kind="blank" 的段数
      dropped.extra_blank = blank_in - blank_out
    使用者据此可以一眼看出"少了多少行、少在哪一步"。
    """
    blank_out = sum(
        1 for p in [*book.front_matter, *(q for c in book.chapters for q in c.paragraphs)]
        if p.kind == "blank"
    )
    # 章节树体检的结果是结构化的（model.validate_items）：一条一章的问题
    # 会在界面上按 code 合并计数（决定31），所以这里不能先压成字符串。
    #
    # no_chapters 这一条清洗层有更具体的说法（"可能整章糊成一行，或标题格式
    # 不在已知家族内"），体检层那句只说后果（"没有目录可跳转"）。同一件事报两遍
    # 用户会以为是两个毛病，所以留下清洗层这句，体检层那条在这里摘掉。
    warning_items = [
        it for it in model.validate_items(book) if it["code"] != "no_chapters"
    ]
    # 行内夹广告分两档：切掉的不用再说了（它已经在 fixed.description 里
    # 有数字），只有**切不了的**才值得占用一行告警 —— 那种行里还带着网址，
    # 用户打开 EPUB 就能看见，必须告诉他去哪几行核对。
    if ad_inline_keep:
        warning_items.append({
            "code": "ad_inline",
            "level": model.LEVEL_LAYOUT,
            "text": f"行内夹广告 {ad_inline_keep} 行无法安全切除，已原样保留，请人工核对",
            "count": ad_inline_keep,
        })
    # 章节标题里带广告词/网址：标题是结构，不能删也不能切，只能请人看一眼。
    # 这类标题会原样进 EPUB 的目录，读者一眼就能看见。
    if ad_title:
        warning_items.append({
            "code": "ad_title",
            "level": model.LEVEL_ORDER,
            "text": f"有 {ad_title} 个章节标题里夹着广告词或网址，标题未作改动，请人工核对",
            "count": ad_title,
        })
    # 疑似目录却一条都没敢删：这类漏删如果不说，用户在报告里完全看不见，
    # 只会觉得"输出里怎么多了几个空章节"。宁可多一句嘴。
    if toc_unsure:
        warning_items.append({
            "code": "toc_unsure",
            "level": model.LEVEL_ORDER,
            "text": "开头疑似目录块，但没能确认它和正文的对应关系，一条都没删："
                    "目录条目可能残留成空章节，请人工核对前几章",
        })
    # 目录残留是另一种漏删：目录没有页码、开头也没有「目录」两个字，检测不到块，
    # 于是目录条目被当成正文标题、前面多出几章 0 段空章。这种形态认得出来，
    # 但**仍然不删** —— 真前言和目录在同一位置长得一样，删错就找不回来了。
    # 所以只给一句出口说明：让它「成书前先回原文把目录块删掉再导」。
    toc_prefix = _toc_like_empty_prefix(book)
    if toc_prefix:
        # start_line 是清洗前的行号（1 基），取不到就不硬报行号 —— 报个 0 会让人
        # 去翻不存在的行。
        near = ""
        if toc_prefix["first_line"] > 0:
            near = "（原文约 " + _short_lines(
                list(range(toc_prefix["first_line"], toc_prefix["last_line"] + 1))
            ) + "）"
        warning_items.append({
            "code": "toc_empty_prefix",
            "level": model.LEVEL_ORDER,
            "text": f"开头 {toc_prefix['count']} 章只有标题、没有正文{near}，"
                    "而且紧接着的正文编号又从头说起 —— 这是目录条目被当成了章节标题。"
                    "一条都没删：这里也可能是真的前言，删错找不回来。"
                    "成书后这几章会是空页；要彻底干净，请回原文把开头那段目录删掉再导一次",
            "count": toc_prefix["count"],
        })
    # 可疑字符（私用区/谚文/替换符）：原文件自带的，清洗不碰它们，但要报出来。
    suspicious_total, suspicious_samples = _suspicious_chars(book)
    if suspicious_total:
        warning_items.append({
            "code": "suspicious",
            "level": model.LEVEL_LAYOUT,
            "text": f"正文里有 {suspicious_total} 个可疑字符（{_SUSPICIOUS_HINT}），"
                    f"例如 {'、'.join(suspicious_samples)}：这些是原文件就带着的乱码，"
                    "清洗不会改动正文用字，如需要请人工核对",
            "count": suspicious_total,
        })
    if not book.chapters:
        warning_items.append({
            "code": "no_chapters",
            "level": model.LEVEL_LOSS,
            "text": "没能切出任何章节：本书可能整章糊成一行，或标题格式不在已知家族内",
        })
    # 整本糊成一行是独立的病灶，要单独点出来 —— 只说"没切出章节"，
    # 用户不知道该去看原文件的哪里，也不知道这不是清洗把正文丢了。
    if oneline and oneline.get("suspected"):
        warning_items.append({
            "code": "oneline",
            "level": model.LEVEL_LOSS,
            "text": f"整本疑似糊成一行：超长行 {oneline['long_line_count']} 行，"
                    f"一行里塞了多个章节标记的行 {oneline['lines_with_multiple_chapters']} 行。"
                    "M2 不会在一行内部切章（那要先猜句子边界），请先用 M1 的体检报告确认，"
                    "再手工或等 M4 处理",
        })
    # 人工调整里有行号没生效（越界、格式不对）：这是"勾了但没起作用"，
    # 不报出来用户会以为工具把他的意见吞了。只列前 5 条，其余说个总数。
    if adjust_info:
        ignored = adjust_info.get("ignored") or []
        if ignored:
            head = "、".join(
                f"{it.get('field')}:{it.get('line')}（{it.get('reason')}）"
                for it in ignored[:5]
            )
            more = f" 等 {len(ignored)} 条" if len(ignored) > 5 else ""
            warning_items.append({
                "code": "adjust_ignored",
                "level": model.LEVEL_ORDER,
                "text": f"人工调整里有 {len(ignored)} 个行号没生效：{head}{more}。"
                        "行号是按本次输入的原文行号算的，换了书或原文改过就对不上了",
                "count": len(ignored),
            })
        # 给了行号却什么都没拦住：不报的话，用户把上一本书的 adjust 拿来重跑，
        # 会得到一份和没给 adjust 一模一样的输出，还以为是工具坏了/自己勾错了。
        #
        # 措辞只陈述事实、不猜原因。两种情形都真实存在，而且从清洗这一层分不开：
        #   1. 行号对不上这一份原文（换了书、原文改过）—— 行号只对本次输入有效；
        #   2. 行号对得上，但那一行本来就不会被删/被并（正文里的普通句子）。
        # 早先写成"可能不是按当前原文算的"，对第 2 种是误诊。
        unmatched = adjust_info.get("unmatched") or {}
        miss_protect = unmatched.get("protect_lines") or []
        miss_breaks = unmatched.get("merge_breaks") or []
        miss_total = len(miss_protect) + len(miss_breaks)
        requested_info = adjust_info.get("requested") or {}
        requested_total = requested_info.get("protect", 0) + requested_info.get(
            "merge_breaks", 0
        )
        if miss_total:
            parts = []
            if miss_protect:
                parts.append(f"保护 {_short_lines(miss_protect)}")
            if miss_breaks:
                parts.append(f"断开 {_short_lines(miss_breaks)}")
            warning_items.append({
                "code": "adjust_unmatched",
                "level": model.LEVEL_ORDER,
                "text": f"人工调整里有 {miss_total} 个行号没有对应的可改动位置："
                        f"{'、'.join(parts)}。"
                        "要么这些行本来就不会被删/被并（那就不需要保护），"
                        "要么行号不是按这一份原文算的（行号只对本次输入有效）",
                "count": miss_total,
            })
        elif requested_total > 0 and not (
            (adjust_info.get("protected") or {}).get("lines", 0)
            or (adjust_info.get("protected") or {}).get("inline", 0)
            or (adjust_info.get("merge_breaks_hit") or 0)
        ):
            warning_items.append({
                "code": "adjust_no_hit",
                "level": model.LEVEL_ORDER,
                "text": f"人工调整给了 {requested_total} 个行号，但一条都没命中，"
                        "本次输出与不给 adjust 时完全一样",
                "count": requested_total,
            })

    return {
        "file": file,
        "size_bytes": size_bytes,
        "encoding": {"chosen": encoding, "confidence": confidence},
        "lines": {
            "total": total_lines,
            "nonblank": nonblank_in,
            "blank_in": blank_in,
            "blank_out": blank_out,
        },
        "dropped": {
            "ad": dropped_ad,
            "toc": dropped_toc,
            # "本章完"这类连载标记。单开一格而不是并进 ad：它们是两类东西，
            # 用户看报告时要能分清"删掉的是广告"还是"删掉的是废话标记"。
            "marker": dropped_marker,
            "extra_blank": blank_in - blank_out,
        },
        # 每一行"本来会被删"的记录，**全量**，给复核界面列勾选项用。
        # samples 只留 8 条是为了报告好读；但用户要保护第 9 行以后的广告时，
        # 光有 samples 就永远勾不到 —— 所以这里另开一份不截断的。
        # 每条 {"line": 原始 1 基行号, "kind": "ad"|"toc",
        #       "protected": 是否因人工保护而留下, "text": 前 60 字}
        "dropped_lines": dropped_lines or [],
        # 人工调整的账。protect 生效的行不进 dropped.ad / dropped.toc，
        # 而是记在这里 —— 这样 "该删的行数 = 实删 + 保护未删" 永远对得平。
        "adjust": adjust_info
        or {
            "applied": {"protect_lines": [], "merge_breaks": [], "protect_count": 0},
            "requested": {"protect": 0, "merge_breaks": 0},
            "protected": {"ad": 0, "toc": 0, "inline": 0, "lines": 0},
            "merge_breaks_hit": 0,
            "unmatched": {"protect_lines": [], "merge_breaks": []},
            "ignored": [],
        },
        "merged": {"groups": merged_group_total, "absorbed_lines": merged_absorbed},
        # 被标成诗词/韵文的段落数。它不改任何字符，只决定排版（居中、楷体、
        # 不缩进），所以不放进 fixed —— fixed 那张表的每一行都是"改了多少字"。
        "verse": {"lines": verse_lines},
        # 整本一行检测的原始证据。cleanreport 不读它，是给 --json 的消费者
        # 和以后 M3/M4 排期用的：这里存证据，不存结论。
        "oneline": oneline
        or {"long_line_count": 0, "lines_with_multiple_chapters": 0, "suspected": False},
        "fixed": {**fixed, "inline_ad_cut_lines": ad_inline_cut,
                  "wrap_chunk_reject_lines": wrap_reject,
                  # 形态像硬折行假标题、但**没能确认**（在段首、或并不回上一段）
                  # 的行数。它们一律原样保留、章节结构不动 —— 这属语义判断，
                  # 按铁律 3 留给 M4。报告里如实说出这个数，是为了让"这本书
                  # 章数为什么比别的版本多几章"有据可查。
                  "wrap_chunk_suspect_lines": wrap_suspect},
        "samples": {
            "ad": ad_samples,
            "toc": toc_samples,
            # 合并组在 _merge_fragments 里是**全量**记的（界面要列每一处），
            # 报告只放前 SAMPLE_LIMIT 条、result 也只留前 80 字 —— 报告是给人
            # 读的，整段拼好的正文放进来只会把报告撑长。
            "merged": [
                {**g, "result": str(g.get("result") or "")[:80]}
                for g in merged_groups[:SAMPLE_LIMIT]
            ],
            "inline_ad": inline_samples,
        },
        "chapters": {
            "count": len(book.chapters),
            "front_paragraphs": len(book.front_matter),
            "tail_paragraphs": len(book.tail_matter),
            "kept_last_toc_line": kept_last_toc_line,
        },
        "stats": model.stats(book),
        # 给人读的整句清单（清洗报告、CLI 用），与 warning_items 一一对应。
        "warnings": [it["text"] for it in warning_items],
        # 结构化清单：{"code", "level", "text"}。界面按 code 合并计数（决定31）。
        "warning_items": warning_items,
    }


# ---------------------------------------------------------------------------
# 十、对外入口
# ---------------------------------------------------------------------------


@dataclass
class CleanResult:
    """一次清洗的全部产物：章节树 + 报告。

    不再单独返回"干净文本"：干净文本是 render 层从章节树渲染出来的派生物，
    存下来就有两个真相来源，早晚不一致。
    """

    book: Book
    report: dict
    text: str = field(default="", repr=False)   # 解码后的原始文本，排查用
    # 合并明细，**全量**：每处一条 {"lines": [起, 止], "pieces": [{"line", "text"}...],
    # "result": 拼好的整段}。报告里的 samples 只放前 8 条（给人读），复核界面
    # 要列每一处，所以单独从返回值给 —— 不塞进 report，免得报告 JSON 被
    # 整本正文撑成几 MB。
    merges: list[dict] = field(default_factory=list, repr=False)


def clean_text(text: str, *, source: str = "", encoding: str = "utf-8",
               confidence: float = 0.0, size_bytes: int | None = None,
               adjust: dict | None = None) -> CleanResult:
    """清洗一段已经解码好的文本。这是整条流水线的唯一入口。

    encoding/confidence 由解码方传进来：文本一旦变成 str，原始编码就不可考了，
    而"这本书原来是 GBK"是以后复核"解码有没有解错"的唯一线索。

    adjust 是人工调整（复核界面传回来的勾选状态），只覆盖两类决定：
    删除（protect_lines / protect_ranges）与合并（merge_breaks）。
    adjust=None 时行为与没有这个参数时逐字节相同 —— 这是硬要求，
    规则层不许因为多了一条人工通道就改变自己的判断。
    """
    lines = text.splitlines()
    adj = _parse_adjust(adjust, len(lines))
    # 括号分节（「（一）」）要先在全书上判一次身份，再逐行扫。
    bracket_sections = _bracket_section_is_structure(lines)
    numbered_sections = _numbered_section_is_structure(lines)
    # 裸数字行（整行只有一个数字）同样先在全书上判身份：一条「2」是扫描残留的
    # 页码，一串「1 2 3…」才可能是分节。
    bare_numbers = _bare_number_is_structure(lines)
    if not numbered_sections:
        # 兜底：整本书一条别的章节结构都认不出来时，顿号分节的门槛降到 5
        # （7 节的短书《书T》靠 8 过不去）。书里已有别的标题就绝不降，
        # 否则会把好书从中间切开（《书U》110 章 / 7 条顿号编号）。
        probe_items, _ = _scan(
            lines, bracket_sections=bracket_sections, numbered_sections=False,
            bare_numbers=bare_numbers, protect=adj.protect,
        )
        if not any(it.is_title for it in probe_items):
            numbered_sections = _numbered_section_is_structure(
                lines, min_sections=_NUM_SECTION_MIN_SECTIONS_FALLBACK
            )
    items, ad_info = _scan(
        lines, bracket_sections=bracket_sections, numbered_sections=numbered_sections,
        bare_numbers=bare_numbers, protect=adj.protect,
    )
    # 短篇集：整本一条章节标记都没有（诗文集、论坛体、短篇合集）时，故事标题
    # 只能靠排版认。只在 0 章的书上开这扇门 —— 有章节结构的书不掺和，
    # 免得深缩进的短行（诗词引文、排版噪音）被切成一堆章节。多一次 _scan 的
    # 成本只落在这些书上。
    if not any(it.is_title for it in items):
        story_titles = _story_title_lines(lines)
        if story_titles:
            items, ad_info = _scan(
                lines, bracket_sections=bracket_sections,
                numbered_sections=numbered_sections, bare_numbers=bare_numbers,
                story_titles=frozenset(story_titles), protect=adj.protect,
            )
    # 目录块必须在**原始行**上判：_scan 会把"目录"标记行整行删掉、把广告行删掉，
    # 拿剩下的行去检测，"目录"那一行就从簇里缺了一块，短目录（2~4 条）直接
    # 跌到 detect_toc 的 5 条门槛之下，整块目录留在正文里变成一串空章节。
    #
    # 传去零宽后的原文而不是 items 的文本，还顺带修掉了另一件事：规范化会删掉
    # 行内空白，"第1章 标题 10" 变成 "第1章标题10"，页码归一就再也认不出来。
    toc = detect_toc([strip_zero_width(ln) for ln in lines])
    # 文件里有没有一行"目录"。_scan 已经把这类行整行删掉（并把行数记进
    # ad_info["toc_marker"]），但它同时是"这本书有目录"最硬的证据：
    # 目录列了 5 章、正文只对上 1 章时，"簇外还有同名"这条判据必然不够用
    # （唯一那份正文标题自己也被吸进了簇）。所以单独把这个事实传下去。
    has_toc_marker = ad_info["toc_marker"] > 0
    toc_drop, kept_last_toc_line, toc_unsure = _plan_toc_drop(
        items, toc, has_toc_marker=has_toc_marker
    )

    # 目录块里要删的行按"原始行号"定位，避免前面的删除让下标错位
    toc_samples: list[dict] = []
    dropped_lines: list[dict] = list(ad_info["dropped_lines"])
    dropped_toc = 0
    protected_toc = 0
    # 这次 adjust 里真正命中（保护住/断开过）的行号。给报告算 unmatched 用。
    protect_hit_lines: set[int] = set(ad_info["protect_hit"])

    def _note_toc_drop(it: _Line) -> bool:
        """目录块里的一行：返回 True 表示该删，False 表示**留下**。

        False 有两种情形，调用方都必须 keep.append 把这一行留下：
          1. 用户点名保护了它；
          2. 它其实已经被 _scan 按整行广告记过一笔账（kind="ad"）。

        情形 2 是「一条物理行只记一次账」的落实。目录块里夹着整行广告是常态，
        那种行在 _scan 阶段就已经进了 dropped_lines；这里再记一笔 toc 账，
        报告就会出现两条同号记录（一条 ad 一条 toc），adjust.protected.lines
        把同一行数两遍，toc 侧的恒等式当场对不上（实测 base 9 = 实删 9 +
        保护 1 = 10）。
        """
        nonlocal protected_toc
        if it.ad_kept:
            # 防御性分支：改完 `_plan_toc_drop` 之后，整行广告已经不会出现在
            # 目录块的删除集合里（那两处扫描对广告行透明），所以这里当前
            # 走不到。留着是因为「一条物理行只记一次账」是账目的底线，
            # 不该依赖上游恰好不送进来；变异测试也确认了它现在测不到。
            return False
        if it.no in adj.protect:
            protected_toc += 1
            protect_hit_lines.add(it.no)
            dropped_lines.append(
                {"line": it.no, "kind": "toc", "protected": True, "text": it.content[:60]}
            )
            if len(toc_samples) < SAMPLE_LIMIT:
                toc_samples.append(
                    {"line": it.no, "text": it.content[:60], "protected": True}
                )
            return False
        dropped_lines.append(
            {"line": it.no, "kind": "toc", "protected": False, "text": it.content[:60]}
        )
        if len(toc_samples) < SAMPLE_LIMIT:
            toc_samples.append(
                {"line": it.no, "text": it.content[:60], "protected": False}
            )
        return True

    def _keep_toc_line(it: _Line) -> _Line:
        """本该删掉、但被人工保护留下的目录行：降级成普通正文行再留。

        保护的语义是「这一行的文字别删」，**不是**「把它升成章节标题」。
        目录条目「第一章　开始　……　13」在 _heading_of 眼里是合法标题，
        直接留下会在书前凭空多出几个带页码尾巴的假章节；相邻几条还会被
        并碎行焊成一坨（都是短行、上一行都没收句）。所以既要清标题，
        也要冻住结构（见 _Line.frozen）。
        """
        it.heading = None
        it.frozen = True
        return it

    if toc_drop:
        keep: list[_Line] = []
        for i, it in enumerate(items):
            if i in toc_drop:
                if _note_toc_drop(it):
                    dropped_toc += 1
                else:
                    keep.append(_keep_toc_line(it))
            else:
                keep.append(it)
        items = keep

    # 文末重印目录（开头一份、书末又一份）。detect_toc 只报最长的那一簇，
    # 文末那份一遍清洗时留在书里当空章节，二遍才被删 —— 非幂等的来源。
    # 必须放在上面那次过滤**之后**：此时正文标题的自重复才看得清。
    reprint_drop = _plan_reprint_toc_drop(items)
    if reprint_drop:
        keep = []
        for i, it in enumerate(items):
            if i in reprint_drop:
                if _note_toc_drop(it):
                    dropped_toc += 1
                else:
                    keep.append(_keep_toc_line(it))
            else:
                keep.append(it)
        items = keep

    # 目录删干净之后，才轮得到「卷一 风起」这类无编号卷容器行升级成卷标题。
    # 顺序不能反：目录条目里也有「卷一」这种行，那些该删掉，不该变成章节。
    items = _promote_volume_lines(items)

    items = _collapse_blanks(items)
    (
        items,
        merged_groups,
        merged_absorbed,
        merged_group_total,
        wrap_rejected,
        merge_breaks_hit,
    ) = _merge_fragments(
        items,
        merge_breaks=adj.merge_breaks,
        # 全书级闸门：这本书是不是硬折行的。判据只看"行尾是不是标点"，
        # 与行长无关 —— 硬折行的书行长可能参差（语料里就有 59–237 字混排的），
        # 但**几乎每一行都断在句子中间**，这才是稳定的信号。
        hard=_hardwrap_ratio(lines) >= _HARDWRAP_RATIO,
        # 折行宽度只给硬折行的书用（见上面那条"占满宽度"判据）。
        width=_hardwrap_width(lines),
    )
    # merge_breaks_hit 是"确实拦住了一次合并"的行号集合，不是计数。
    merge_breaks_hit_count = len(merge_breaks_hit)
    # 并碎行之后**不再**补切/补删广告。理由：_merge_fragments 只合并
    # ad is None 的行，而 judge_ad_line 只在「没有任何广告词、也没有网址」时
    # 才返回 None（diagnose.py:499）—— 合并出来的段落里不可能凭空长出广告信号，
    # 这里再跑一遍逐行判据必然是空转。全角网址那种「原文判不出、渲染后判得出」
    # 的情况，已经由 diagnose._FOLD_WIDE 在逐行判定阶段就地解决。
    # 标诗词必须在并碎行**之后**：段落边界要等并完才是最终的，否则标记会落在
    # 已经被合并掉的行上。标完立刻组装章节树，中间不要再插入任何改行序的步骤。
    verse_lines = _mark_verse(items)
    book, _ = _build_book(items, source=source, encoding=encoding)

    fixed = _scan_fixed(lines)
    blank_in = sum(1 for ln in lines if not strip_zero_width(ln).strip())
    # 用**原始行**判"整本一行"：规范化会压掉行内空白，但长度还在；
    # 拿原文更直接对上体检报告里的同一组数字。
    oneline = whole_book_in_one_line(lines)
    report = _build_report(
        file=source,
        size_bytes=size_bytes if size_bytes is not None else len(text.encode("utf-8")),
        encoding=encoding,
        confidence=confidence,
        total_lines=len(lines),
        blank_in=blank_in,
        nonblank_in=len(lines) - blank_in,
        dropped_ad=ad_info["ad_whole"],
        # 目录标记行里被人工保护的那些**不算删**，但也照样计入
        # ad_info["toc_marker"]（那是"这本书有目录"的证据）。
        # 两边都要减一次，账才对得平。
        dropped_toc=dropped_toc
        + ad_info["toc_marker"]
        - ad_info["toc_marker_protected"],
        dropped_marker=ad_info["end_marker"],
        ad_inline_cut=ad_info["ad_inline_cut"],
        ad_inline_keep=ad_info["ad_inline_keep"],
        ad_title=ad_info["ad_title"],
        fixed=fixed,
        ad_samples=ad_info["ad_samples"],
        inline_samples=ad_info["inline_samples"],
        toc_samples=toc_samples,
        merged_groups=merged_groups,
        merged_group_total=merged_group_total,
        merged_absorbed=merged_absorbed,
        kept_last_toc_line=kept_last_toc_line,
        book=book,
        oneline=oneline,
        toc_unsure=toc_unsure,
        verse_lines=verse_lines,
        wrap_reject=wrap_rejected + ad_info["wrap_head_dropped"],
        wrap_suspect=max(0, ad_info["wrap_gate_hit"] - wrap_rejected),
        dropped_lines=sorted(
            dropped_lines, key=lambda d: (d["line"], d["kind"])
        ),
        adjust_info={
            # applied 回显"实际生效的行号"，不是用户原始输入 —— 界面据此
            # 把勾选态画回去，越界的那些不该出现在勾选态里。
            # 注意 protect_lines 这一项**包含 protect_ranges 展开出来的行**：
            # protect_ranges=[[2,4]] 会在这里变成 [2, 3, 4]。界面要区分
            # "用户勾了单行"还是"勾了一段"，得看自己发出去的原始 adjust，
            # 不能靠这个字段反推勾选粒度。
            "applied": {
                "protect_lines": sorted(adj.protect),
                "merge_breaks": sorted(adj.merge_breaks),
                "protect_count": len(adj.protect),
            },
            "requested": {
                "protect": adj.requested_protect,
                "merge_breaks": adj.requested_breaks,
            },
            "protected": {
                "ad": ad_info["protected_ad"],
                "toc": protected_toc + ad_info["toc_marker_protected"],
                # "本章完"这类标记被保护留下的行数。同样进 lines 总和，
                # 否则"该删的行数 = 实删 + 保护未删"对不上。
                "marker": ad_info["end_marker_protected"],
                # 行内广告因为保护没切的行数。它**不算进 lines**：行内切除
                # 本来就不删行，混进去会让 "该删的行数 = 实删 + 保护未删"
                # 这个恒等式对不上。单独列出来，界面才好告诉用户"这行的
                # 广告尾巴还在，是你自己要求别动的"。
                "inline": ad_info["ad_inline_protected"],
                "lines": ad_info["protected_ad"]
                + protected_toc
                + ad_info["toc_marker_protected"]
                + ad_info["end_marker_protected"],
            },
            "merge_breaks_hit": merge_breaks_hit_count,
            # 给了行号、但那个行号上没有任何东西可保护/可断开的位置。界面据此
            # 告诉用户"这一条勾了但没效果"，而不是让他盯着输出猜。
            # 注意"没命中"不等于"行号错了"：正文里的普通行本来就不会被删，
            # 保护它当然没有效果 —— 所以文案只陈述事实，不猜原因。
            "unmatched": {
                "protect_lines": sorted(adj.protect - protect_hit_lines),
                "merge_breaks": sorted(adj.merge_breaks - merge_breaks_hit),
            },
            "ignored": adj.ignored,
        },
    )
    return CleanResult(book=book, report=report, text=text, merges=merged_groups)


def clean_bytes(raw: bytes, *, source: str = "",
                adjust: dict | None = None) -> CleanResult:
    """从原始字节开始清洗：先探编码，再解码，再走 clean_text。

    编码探测复用 encoding.detect()（常用字占比打分），不用 try/except ——
    用异常判定编码会静默吞掉"能解但解错"的情况，那正是最坏的一种错。
    """
    candidates = detect(raw)
    chosen = candidates[0]
    text = decode(raw, chosen.encoding)
    return clean_text(
        text,
        source=source,
        encoding=chosen.encoding,
        confidence=round(chosen.score, 3),
        size_bytes=len(raw),
        adjust=adjust,
    )


def clean_file(path: Path | str, *, adjust: dict | None = None) -> CleanResult:
    """清洗一个文件。**只读**：输入文件的字节数、mtime 都不会被碰。

    为什么这里不检查"输出会不会覆盖输入"：本函数没有输出路径。
    写盘统一由 CLI 的 write_outputs() 负责，那里才有路径可比。
    """
    p = Path(path)
    raw = p.read_bytes()
    return clean_bytes(raw, source=str(p), adjust=adjust)
