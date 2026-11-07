"""字符与标点规范化：M2 里"确定性"的那一半。

本模块只做**规则能 100% 判定**的改写。凡是要看上下文、看语义的
（英文句点该不该转全角、单个半角 ! 算不算中文标点、直引号朝哪边），
一律原样保留 —— 宁可不判，也不瞎猜。

三条硬约束，每个函数都遵守：

1. **删除必须可核对**：被去掉的字符都要能报出来（CleanLine.trailing_removed /
   indent_raw / spaces_collapsed，以及 find_zero_width），不许让正文无声消失。
2. **幂等**：f(f(x)) == f(x)。所有改写都收敛到唯一规范形，二次执行不再变。
3. **纯函数**：不读写文件、不 print、无全局状态。批次怎么展示由调用方决定。

单行处理的推荐顺序（normalize_line() 已按此封装）：

    strip_zero_width → clean_line → normalize_punctuation → normalize_quotes

为什么是这个顺序：先删零宽（否则行首零宽会让缩进判定失效），
再处理空白与缩进，再归一堆叠标点与多余空白，最后统一引号。
每一步都只依赖前一步已经稳定的形态，倒过来会出现"二次执行还会变"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

# 复用体检模块的判定常量，绝不复制一份 —— 否则报告与清洗会用两套标准。
from .diagnose import AD_KEYWORDS, TERMINAL, _URL, squeeze

__all__ = [
    "ZERO_WIDTH_CHARS",
    "INDENT_MIN_WIDTH",
    "INDENT_STR",
    "CleanLine",
    "find_zero_width",
    "strip_zero_width",
    "clean_line",
    "clean_line_text",
    "normalize_punctuation",
    "normalize_quotes",
    "normalize_line",
    "analyze_line",
]

# ---------------------------------------------------------------------------
# 一、零宽与不可见字符
# ---------------------------------------------------------------------------

# 这些字符在中文正文里没有任何排版意义，出现即残留（网页复制、转码、
# 拼接两段文件留下的）。注意 U+FEFF 不只是文件头的 BOM：拼接文本时它会
# 出现在行首、甚至行中间，一次或几次都不固定，所以按"出现即删"处理。
ZERO_WIDTH_CHARS = (
    "\u200b"                          # 零宽空格 ZWSP
    "\u200c"                          # 零宽不连字 ZWNJ
    "\u200d"                          # 零宽连字 ZWJ
    "\u200e\u200f"                    # 左到右 / 右到左标记
    "\u180e"                          # 蒙古文元音分隔符
    "\u202a\u202b\u202c\u202d\u202e"  # 双向控制符
    "\u2060"                          # 词连接符 WJ
    "\u2061\u2062\u2063\u2064"        # 不可见运算符
    "\u2066\u2067\u2068\u2069"        # 双向隔离符
    "\ufeff"                          # BOM 残留 / 零宽不换行空格
)

# 唯一的例外：emoji 组合靠 ZWJ 连接（👨👩👧 是一个字，删了会散成三个），
# 那是"合法用法"而不是残留，必须原样保留。中文语境里的 ZWJ/ZWNJ 一定是残留。
_EMOJI_JOINERS = "\u200c\u200d"
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # 表情、象形符号、补充符号
    (0x1F1E6, 0x1F1FF),  # 区域指示符（国旗）
    (0x2600, 0x27BF),    # 杂项符号与装饰符号
    (0x2B00, 0x2BFF),    # 杂项符号与箭头
    (0xFE00, 0xFE0F),    # 变体选择符（决定 emoji 呈现方式）
    (0x20E3, 0x20E3),    # 组合用键帽
)


def _is_emoji(ch: str) -> bool:
    """这个字符是不是 emoji 的组成部分（用来判断 ZWJ 该不该保留）。"""
    if not ch:
        return False
    o = ord(ch)
    for lo, hi in _EMOJI_RANGES:
        if lo <= o <= hi:
            return True
    return False


def _emoji_joined(text: str, i: int) -> bool:
    """位于 i 的 ZWJ/ZWNJ 是否贴在 emoji 旁边 —— 是就属于 emoji 组合，别删。"""
    prev = text[i - 1] if i > 0 else ""
    nxt = text[i + 1] if i + 1 < len(text) else ""
    return _is_emoji(prev) or _is_emoji(nxt)


def find_zero_width(text: str) -> dict:
    """报出文本里的不可见字符：删几个、各是什么码位、几个因 emoji 组合保留。

    只负责"说清楚"，不做改写 —— 删除由 strip_zero_width 执行。
    """
    by_char: dict[str, int] = {}
    kept = 0
    for i, ch in enumerate(text):
        if ch not in ZERO_WIDTH_CHARS:
            continue
        if ch in _EMOJI_JOINERS and _emoji_joined(text, i):
            kept += 1
            continue
        key = f"U+{ord(ch):04X}"
        by_char[key] = by_char.get(key, 0) + 1
    removable = sum(by_char.values())
    return {
        "total": removable + kept,
        "removable": removable,
        "kept_emoji_joined": kept,
        "by_char": by_char,
    }


def strip_zero_width(text: str) -> str:
    """删除零宽/不可见字符；emoji 组合里的 ZWJ/ZWNJ 保留（删了 emoji 会散架）。

    这是纯删除操作：删了什么用 find_zero_width() 报出来，可逐项核对。
    幂等：删完的文本里不剩可删字符，二次执行原样返回。
    """
    if not any(ch in ZERO_WIDTH_CHARS for ch in text):
        return text
    out: list[str] = []
    for i, ch in enumerate(text):
        if ch in ZERO_WIDTH_CHARS:
            if ch in _EMOJI_JOINERS and _emoji_joined(text, i):
                out.append(ch)
            continue
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# 二、空白与缩进
# ---------------------------------------------------------------------------

# 缩进阈值，单位是"半角格"：全角空格算 2 格、Tab 算 4 格、半角空格/NBSP 算 1 格。
# 中文小说的段首缩进是"两个全角空格"或"四个半角空格"；只出现 1 格的多半是手滑，
# 不当缩进，直接去掉。
INDENT_MIN_WIDTH = 2
# 归一后的段首缩进：两个全角空格（中文排版的标准段首）
INDENT_STR = "\u3000\u3000"

_SPACE_CHARS = " \t\u3000\u00a0"
_SPACE_RUN = re.compile(r"[ \t\u3000\u00a0]+")
_INDENT_WIDTH = {"\u3000": 2, "\t": 4, " ": 1, "\u00a0": 1}


@dataclass(frozen=True)
class CleanLine:
    """一行规范化的结果。

    缩进只归成"有/无"两态（indented），原始缩进宽度与原文单独记着，
    这样清洗报告能写出"第 N 行有 4 格缩进，已归一成两个全角空格"，
    而不是无声地把段落信息抹掉。
    """

    raw: str
    text: str
    indented: bool
    indent_width: int      # 原始缩进宽度，单位半角格（全角空格 2、Tab 4、半角空格 1）
    indent_raw: str        # 被吃掉的原始缩进原文，供核对
    trailing_removed: str  # 被吃掉的原始行尾空白，供核对
    spaces_collapsed: int  # 行内连续空白压成一个时净减少的字符数

    @property
    def changed(self) -> bool:
        return self.text != self.raw

    def as_dict(self) -> dict:
        return {
            "raw_len": len(self.raw),
            "clean_len": len(self.text),
            "indented": self.indented,
            "indent_width": self.indent_width,
            "trailing_removed": len(self.trailing_removed),
            "spaces_collapsed": self.spaces_collapsed,
            "changed": self.changed,
        }


def clean_line(line: str) -> CleanLine:
    """单行空白规范化：去行尾空白、归一缩进、压掉行内连续空白。

    **缩进是段落信息，不能压掉。** 中文小说用"两个全角空格"或"四个半角空格"
    表示段首缩进；这里把它归成唯一形态 INDENT_STR（两个全角空格），
    有/无缩进是布尔量 indented，原始宽度和原文另存在 CleanLine 里。
    这样"有缩进的段首"与"没缩进的碎行"在后续步骤里可判可分。

    注意：本函数不处理零宽字符（那是 strip_zero_width 的事）。若行首带零宽，
    缩进判定会失效 —— 先 strip_zero_width 再调用；normalize_line 已按此封装。

    幂等：clean_line(clean_line(x).text).text == clean_line(x).text。
    """
    raw = line
    stripped = line.rstrip()
    trailing_removed = line[len(stripped):]

    m = _SPACE_RUN.match(stripped)
    indent_raw = m.group(0) if m else ""
    indent_width = sum(_INDENT_WIDTH.get(c, 1) for c in indent_raw)

    body = stripped[len(indent_raw):]
    collapsed = _SPACE_RUN.sub(" ", body)

    indented = indent_width >= INDENT_MIN_WIDTH
    text = (INDENT_STR if indented else "") + collapsed
    return CleanLine(
        raw=raw,
        text=text,
        indented=indented,
        indent_width=indent_width,
        indent_raw=indent_raw,
        trailing_removed=trailing_removed,
        spaces_collapsed=len(body) - len(collapsed),
    )


def clean_line_text(line: str) -> str:
    """只要文本、不要元信息时的便捷入口。"""
    return clean_line(line).text


# ---------------------------------------------------------------------------
# 三、标点归一
# ---------------------------------------------------------------------------

# 只转全角数字与全角拉丁字母。全角标点（，。！？：；（） 等）本身就是
# 正确的中文标点，一个都不许动。
_FW_ALNUM = str.maketrans(
    {
        **{0xFF10 + i: 0x30 + i for i in range(10)},   # ０-９ → 0-9
        **{0xFF21 + i: 0x41 + i for i in range(26)},   # Ａ-Ｚ → A-Z
        **{0xFF41 + i: 0x61 + i for i in range(26)},   # ａ-ｚ → a-z
    }
)

CN_ELLIPSIS = "\u2026\u2026"   # 中文省略号：两个 U+2026
CN_DASH = "\u2014\u2014"       # 中文破折号：两个 U+2014
CN_TILDE = "\uff5e"            # 中文波浪号
CN_SPACE = "\u3000"            # 中文全角空格（标题内部的分隔符统一成它）

# 归一要折到不动点：删空白可能把原本隔开的两段叠标点接成一段。
# 每条规则都只缩短不增长，所以必然收敛；上限只是防御性保险。
_MAX_PASSES = 8

# 中文省略号的两种残余形态：
# 1) 一串英文句点。紧贴英文/数字/斜杠的点列不碰（那是小数点、版本号、网址），
#    例如 "3.14" "v1.2.3" "www.example.com" "a.com/..." 全部原样保留。
_ASCII_DOT_RUN = re.compile(
    r"(?<![A-Za-z0-9/.\uff0e])(?:\.|\uff0e){2,}(?![A-Za-z0-9/.\uff0e])"
)
# 2) 连写的中文句号。"他说。。。" 是省略号的常见手误，收敛成 "……"；
#    单独一个 "。" 是正常句号，不动。
_PERIOD_RUN = re.compile(r"\u3002{2,}")
# 3) 省略号字符本身。中文省略号标准写法是两个 U+2026，"…" 与 "……" 都收敛成 "……"。
#    句号与省略号混写时（"他走了。…"）分开处理，句号是句末标点，不能吞。
_ELLIPSIS_CHARS = re.compile(r"\u2026+")

# 连写的感叹号/问号。输出保留第一个符号的语气：!? → ！？，?! → ？！
_BANG_RUN = re.compile(r"[!\uff01?\uff1f]{2,}")

# 连写波浪号。长度 4 以上多半是分隔线（diagnose 的分隔线规则要认），不碰。
_TILDE_RUN = re.compile(r"(?<![~\uff5e\u301c])[~\uff5e\u301c]{2,3}(?![~\uff5e\u301c])")
# 单个半角 "~"：只有贴着汉字时才当成中文波浪号（"十~十五年"）。
# "10~20" 这种数字区间、以及英文里的 "~" 一律不动。
_SINGLE_TILDE = re.compile(r"(?:(?<=[\u4e00-\u9fff])~|~(?=[\u4e00-\u9fff]))")
# 连写半角连字符 → 中文破折号。只认 2~3 个：4 个以上分不清是破折号连写
# 还是分隔线（"--------"），不做判定。紧贴英文/数字/斜杠的也不碰（怕伤网址）。
_HYPHEN_RUN = re.compile(r"(?<![A-Za-z0-9/-])-{2,3}(?![A-Za-z0-9/-])")
# 连写长破折号 → 中文破折号（标准长度就是两个）。长度 4 以上多半是分隔线，不碰。
_EMDASH_RUN = re.compile(r"(?<!\u2014)\u2014{2,3}(?!\u2014)")

# 中文语境里的"可紧贴字符"：汉字、中文标点、全角标点、中文引号、破折号、省略号。
# 注意特意排除了 U+3000 全角空格 —— 它用来表示缩进，不能被当成"中文之间的空白"删掉。
_NEI_BODY = (
    "\u4e00-\u9fff"      # 基本汉字
    "\u3400-\u4dbf"      # 扩展 A
    "\uf900-\ufaff"      # 兼容汉字
    "\u3001-\u303f"      # 中文标点（不含全角空格）
    "\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"  # 全角标点
    "\u2018\u2019\u201c\u201d"  # 中文单双引号
    "\u2014\u2026\u00b7"        # 破折号、省略号、间隔号
)
_NEI = f"[{_NEI_BODY}]"
_SP = r"[ \t\u3000\u00a0]+"
# 删空白只有两种情况，都是 100% 可判定的：
#   a) 两边都是中文（含贴着的数字）—— 中文之间不需要空格
#   b) 一边是数字、一边是中文 —— 同理
# 绝不删：英文单词之间、数字与单位之间（10 km）、两个数字之间（3 5）。
_CJK_SPACE = re.compile(
    rf"(?:(?<={_NEI}){_SP}(?=[{_NEI_BODY}0-9])|(?<=[0-9]){_SP}(?={_NEI}))"
)


def _bang_run(m: re.Match[str]) -> str:
    run = m.group(0)
    has_excl = "!" in run or "\uff01" in run
    has_ques = "?" in run or "\uff1f" in run
    if has_excl and has_ques:
        return "\uff01\uff1f" if run[0] in "!\uff01" else "\uff1f\uff01"
    return "\uff01" if has_excl else "\uff1f"


def normalize_punctuation(text: str, *, keep_inline_space: bool = False) -> str:
    """中文标点归一。只做能 100% 判定的几种，不确定的一律原样保留。

    做（每种都有明确规范形）：
      * "..." "。。。" "……" "…" 等连写 → 中文省略号 "……"
      * 连写 ! ? → 单个（"!!"→"！"，"!?"→"！？"，"?!""→"？！"）
      * 连写 ~ / - / — → "～" / "——"（长度 4 以上的保留，那多半是分隔线）
      * 全角数字、全角拉丁字母 → 半角（０→0、Ａ→A）
      * 中文之间、中文与数字之间的多余空白 → 删掉（"他 说"→"他说"）
        keep_inline_space=True 时跳过这一条。章节标题里的空白是**排版信息**
        （"第一回　　止淫风借淫事说法　谈色事就色欲开端" 的空格是人工分段），
        正文里的空白才是噪声。其余规则照跑，因为标题同样需要叠标点归一。

    不做（会误伤，交给 M4 或人工复核）：
      * 英文句号/逗号转全角 —— "3.14" "Hello, world" 会当场坏掉
      * 单个半角 "!" "?" 转全角 —— 可能在英文句子里
      * 单个 "。" 与单个 "." 的互相转换

    幂等：所有输出都是规范形，二次执行不再变。
    """
    if not text:
        return text
    out = text.translate(_FW_ALNUM)
    # 反复折到不动点再返回。为什么必须循环：前面删掉的空白/标点会把原本
    # 隔开的同类叠字接成一段（"……　。。" → "…………"），只跑一遍就还会再变，
    # 幂等就断了。每条规则都只缩短不增长，所以循环必然收敛，几轮内停止。
    for _ in range(_MAX_PASSES):
        prev = out
        out = _ASCII_DOT_RUN.sub(CN_ELLIPSIS, out)
        out = _PERIOD_RUN.sub(CN_ELLIPSIS, out)
        out = _ELLIPSIS_CHARS.sub(CN_ELLIPSIS, out)
        out = _BANG_RUN.sub(_bang_run, out)
        out = _TILDE_RUN.sub(CN_TILDE, out)
        out = _SINGLE_TILDE.sub(CN_TILDE, out)
        out = _HYPHEN_RUN.sub(CN_DASH, out)
        out = _EMDASH_RUN.sub(CN_DASH, out)
        # 正文：中文之间的空白是噪声，直接删（"他 说"→"他说"）。
        # 标题：同样的空白是排版分隔符，换成全角空格保留下来
        # （"第一回　　止淫风借淫事说法　谈色事就色欲开端" →
        #   "第一回　止淫风借淫事说法　谈色事就色欲开端"）。
        # 为什么是全角空格：半角空格在 HTML/EPUB 里又窄又可能被折叠，
        # 中文版式的分隔惯例本来就是全角空格。英文与数字之间的空白
        # 不在这个正则的匹配范围里（_NEI 只含中文与中文标点），不会被动。
        if keep_inline_space:
            out = _CJK_SPACE.sub(CN_SPACE, out)
        else:
            out = _CJK_SPACE.sub("", out)
        if out == prev:
            break
    return out


# ---------------------------------------------------------------------------
# 四、引号归一
# ---------------------------------------------------------------------------

# 中文引号是**成对的左右不同字符**，不需要猜朝向，直接映射即可。
# 归一后的 “”‘’ 本身也在集合里 —— 这样同一套状态机可以把"已经规范过的文本"
# 再跑一遍而结果不变，幂等才立得住（否则第二遍会数错层数）。
_QUOTE_OPEN = {
    "\u201c": "double",   # “ 开双
    "\u2018": "single",   # ‘ 开单
    "\u300c": "double",   # 「 开双
    "\u300e": "single",   # 『 开单（常见于「」里面）
}
_QUOTE_CLOSE = {
    "\u201d": "double",   # ” 闭双
    "\u2019": "single",   # ’ 闭单
    "\u300d": "double",   # 」 闭双
    "\u300f": "single",   # 』 闭单
}
_DOUBLE_OPEN, _DOUBLE_CLOSE = "\u201c", "\u201d"
_SINGLE_OPEN, _SINGLE_CLOSE = "\u2018", "\u2019"


def normalize_quotes(text: str) -> str:
    """中文引号归一：统一成 “ ” 与 ‘ ’，并处理嵌套（外层双、内层单）。

    用状态机：每遇到一个开引号就压栈（深度 +1），按**深度奇偶**决定它变成
    “（偶数层，外层）还是 ‘（奇数层，内层）；每遇到一个闭引号就出栈，
    吐出同层的闭引号。所以 「他说：『走吧』」 与 “他说：「走吧」”
    都收敛成 “他说：‘走吧’”，任意深度的混用都落到同一形态。

    闭引号没有对应开引号时（文本被截断、或只贴了右半边），不猜它原本配哪层，
    按它自带的家族直接出规范形：“ ” 」 出 ”，』 出 ’。

    不碰直引号 " 和 '，也不碰 ＂ —— 它们没有方向，判断朝哪边必须看语义，
    属于"瞎猜"，留给 M4 兜底。

    幂等：输出永远是"偶数层 “”、奇数层 ‘’"的规范形。
    """
    if not text:
        return text
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch in _QUOTE_OPEN:
            out.append(_DOUBLE_OPEN if depth % 2 == 0 else _SINGLE_OPEN)
            depth += 1
        elif ch in _QUOTE_CLOSE:
            if depth > 0:
                depth -= 1
                out.append(_DOUBLE_CLOSE if depth % 2 == 0 else _SINGLE_CLOSE)
            else:
                out.append(_DOUBLE_CLOSE if _QUOTE_CLOSE[ch] == "double" else _SINGLE_CLOSE)
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# 五、组合入口与单行特征
# ---------------------------------------------------------------------------


def normalize_line(line: str, *, keep_inline_space: bool = False) -> CleanLine:
    """单行完整规范化：零宽 → 空白/缩进 → 标点 → 引号。

    返回 CleanLine：text 是最终文本，raw 是原始行；缩进/空白的元信息
    是在删掉零宽之后量出来的（零宽会让缩进判定失效）。
    keep_inline_space=True 时保留行内空白，供章节标题使用（见 normalize_punctuation）。
    幂等：normalize_line(normalize_line(x).text).text == normalize_line(x).text。
    """
    base = clean_line(strip_zero_width(line))
    text = normalize_quotes(normalize_punctuation(base.text, keep_inline_space=keep_inline_space))
    return replace(base, raw=line, text=text)


def analyze_line(line: str) -> dict:
    """给清洗报告用的单行特征：只报"可核对的事实"，不下结论。

    广告特征与句末标点判定都复用 diagnose 的常量与函数
    （AD_KEYWORDS / _URL / TERMINAL / squeeze），不另立一套标准。
    """
    cleaned = normalize_line(line)
    zw = find_zero_width(line)
    stripped = line.strip()

    # 关键词比对先去空白与零宽：被零宽劈开的"微\u200b信"也能认出来
    lowered = squeeze(line).lower()
    keywords = [k for k in AD_KEYWORDS if k.lower() in lowered]
    has_url = bool(_URL.search(stripped))
    # 句末标点按**原始行**判定，与 diagnose 口径一致：归一化会把 』 变成 ’、
    # 把 "…" 变成 "……"，拿清洗后的形态去比 TERMINAL 会漏报。
    tail = stripped

    return {
        "raw_len": len(line),
        "clean_len": len(cleaned.text),
        "indented": cleaned.indented,
        "indent_width": cleaned.indent_width,
        "has_zero_width": zw["total"] > 0,
        "zero_width_removed": zw["removable"],
        "zero_width_kept": zw["kept_emoji_joined"],
        "trailing_space": line != line.rstrip(),
        "trailing_space_len": len(line) - len(line.rstrip()),
        "blank": not stripped,
        "ad_like": bool(keywords or has_url),
        "ad_keywords": keywords[:5],
        "has_url": has_url,
        "ends_terminal": bool(tail) and tail.endswith(tuple(TERMINAL)),
        "changed": cleaned.changed,
    }
