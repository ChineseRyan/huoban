"""体检：只回答问题，不改任何东西。

输出的每一条都必须是"可核对的事实"（第几行、命中什么、出现几次），
而不是"感觉这本书有点乱"。后面的清洗全依赖这份事实清单。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from .encoding import decode, detect

# 句末标点。行尾没有这些 => 这一行很可能只是被硬折断的半句。
#
# 收尾的**右引号**必须在这里：中文小说的段落有一大半是对话，结尾是
# “……” —— 句号在引号里面，行尾字符是 ”（U+201D）。早先这张表只有
# ASCII 的 ' 和两个同码位的 '（实际是 U+0027 重复了两次），漏了全角
# 双引号，于是"以对话收尾的段落"全被判成没写完，下一段被焊上来 ——
# 实测一本 3976 段的书里有 1702 段（43%）是这么收尾的，这就是这里说的
# "正常书被误伤"。单引号 ' ' 一并补齐（U+2018/U+2019）。
TERMINAL = "。！？…—\u201c\u201d\u2018\u2019'\"＂）)】]』」》〉｝}"

# 顺序有意义：先具体后宽松
CHAPTER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("第N卷部类", re.compile(r"^\s*第\s*[0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,8}\s*[卷部集篇]")),
    # 「话」也是章级量词（《书N》全书 31 章都写「第一话·海底偶遇」）。
    # 不收它，这本书 0 章、2500 段全掉进 front_matter。
    ("第N章类", re.compile(r"^\s*第\s*[0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,8}\s*[章回节幕话話]")),
    # 「1、掳君（1）」式的阿拉伯数字 + 顿号分节（《书M》全书 66 节）。
    # 单行判据挡不住正文里的编号清单，必须在 clean.py 里过书级闸门
    # （_numbered_section_is_structure）—— 「1、2、3、」挤成一块的资料清单
    # 与真分节的唯一区别是"节与节之间隔得开不开"。
    ("数字顿号分节", re.compile(r"^\s*(?:[0-9０-９]{1,4}|[一二三四五六七八九十百]{1,4})\s*[、.．]\s*\S")),
    ("Chapter N", re.compile(r"^\s*[Cc][Hh][Aa][Pp][Tt][Ee][Rr]\s*[0-9]{1,4}")),
    ("卷首特殊章", re.compile(r"^\s*(?:序章|序言|楔子|引子|前言|引言|后记|尾声|终章|大结局|番外|外传|附录|目录)")),
    # 单独一个「序」也是一章（《书G》正文第 33 行就是裸「序」）。
    # 必须整行匹配：卷首特殊章上面那条是前缀匹配，直接往里加「序」会把
    # 「序幕拉开，好戏登场。」这种正文句当成标题。
    ("卷首特殊章", re.compile(r"^\s*(?:序|自序|小序)\s*$")),
    # 「（一）」「（二）」式的括号分节（《书F》全书 130 章都用这个）。
    # 整行匹配：正文里「（一）他说」这种行不能算标题。
    ("括号分节", re.compile(r"^\s*[（(]\s*[0-9０-９〇零一二三四五六七八九十百]{1,4}\s*[）)]\s*$")),
    ("Markdown 标题", re.compile(r"^\s*#{1,6}\s+\S")),
    ("纯数字行", re.compile(r"^\s*[0-9]{1,4}\s*$")),
    ("分隔线", re.compile(r"^\s*[-=*_~—·☆★]{3,}\s*$")),
]

AD_KEYWORDS = (
    "http", "www.", ".com", ".net", ".cc", ".org", ".xyz", ".top", ".vip",
    "qq群", "书友群", "加群", "企鹅号", "公众号", "微信", "vx号",
    "笔趣阁", "顶点小说", "无弹窗", "手机用户请", "手机版阅读",
    "请记住本站", "记住本站", "本站域名", "最新网址", "收藏本站", "加入书签",
    "求收藏", "求推荐", "求月票", "求打赏", "求订阅", "求鲜花", "求评价票",
    "天才一秒记住", "一秒记住", "章节错误", "点此举报",
    "首发地址", "手打", "校对", "本章未完", "点击下一页", "更多精彩",
    "纯文字更新", "更新最快", "全文阅读", "免费阅读", "txt下载",
)

_WS = re.compile(r"\s+")
# 域名/链接。广告行几乎必带，正文几乎不带
_URL = re.compile(
    r"(?:https?://)?(?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z]{2,6})(?:/[^\s，。！？、；）】]*)?",
    re.IGNORECASE,
)
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"

# 全角 ASCII（！ 到 ～）→ 半角。**长度一一对应**，所以折叠后的下标在原文上
# 依然有效 —— find_ad_signal 返回的切点可以直接拿去切原始行。
#
# 为什么要折叠：中文输入法下打出来的网址常常是全角（'ｈｔｔｐ：／／ｗｗｗ．ｘ．ｃｏｍ'），
# 清洗流水线里 normalize 会把它折成半角再进书，但**广告判据看到的是原文**。
# 于是同一行"原文判不出、渲染后再判一次就判出来了"，两遍清洗结果不同
# （实测《书S》一条全角网址广告，第一遍留着、第二遍删掉）。
# 判据看折叠后的文本，与最终进书的形态一致，两遍就一致了。
_FOLD_WIDE = str.maketrans({chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)})

# 中文数字。小写与大写（壹贰叁…）都要认：《书A》原文第三回写作
# "第叁回"，只认小写会让这一章整章丢失（它不会命中任何章节家族，
# 直接掉进上一章的正文里）。繁体异体（貳參陸）一起收，成本极低。
_CN_NUM = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "壹": 1, "贰": 2, "貳": 2, "叁": 3, "參": 3, "肆": 4, "伍": 5,
    "陆": 6, "陸": 6, "柒": 7, "捌": 8, "玖": 9,
}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "拾": 10, "佰": 100, "仟": 1000}
_DIGIT_MAP = str.maketrans("０１２３４５６７８９", "0123456789")


def squeeze(line: str) -> str:
    """去掉所有空白与零宽字符，用于比较两行是不是"同一行"。"""
    out = _WS.sub("", line)
    for z in _ZERO_WIDTH:
        out = out.replace(z, "")
    return out


def cn_to_int(s: str) -> int | None:
    """把"一百二十三"这类中文数字转成整数；纯阿拉伯数字直接转。"""
    s = s.strip().translate(_DIGIT_MAP)
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = 0
    section = 0
    num = 0
    for ch in s:
        if ch in _CN_NUM:
            num = _CN_NUM[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if num == 0:
                num = 1
            section += num * unit
            num = 0
        elif ch in "万":
            section = (section + num) * 10000
            total += section
            section = 0
            num = 0
        else:
            return None
    return total + section + num


# 章节标题外面套一层装饰边框，是下载站 TXT 的常见写法：
#     ========== 第001章 ==========
#     ──── 第一章 ────
#     ★★★ 第三章 ★★★
# 边框把行首锚点堵死了，match_chapter / _NUM_TITLE 全都挂空。实测《书K》
# 全书 197 章一个都认不出，5320 段正文全掉进 front_matter，付梓页也跟着取不到
# 真字。只认「行首行尾各一串同一个装饰字符、中间还留着内容」的对称边框，
# 剥掉两侧留下中间那截；整行纯装饰的分隔线原样保留（它归「分隔线」家族，
# 体检报告要记这条事实，清洗那边本来就挡掉，见 clean.py 的 NOT_A_CHAPTER_FAMILY）。
_FRAME_CHARS = "=＝*☆★※◇◆○●□■△▲▽▼♦〓▬▭~—－＿_＋+·∙⋅～﹡×﹢﹣─━│┃═║-"
_FRAME_RUN = "[" + re.escape(_FRAME_CHARS) + "]"
_FRAME_RE = re.compile(
    r"^\s*((?:" + _FRAME_RUN + r"){3,})\s*(.*?)\s*((?:" + _FRAME_RUN + r"){3,})\s*$"
)


def strip_frame(line: str) -> str:
    """剥掉章节标题两侧的对称装饰边框；没有边框就原样返回。

    只剥「两侧同一字符各连写 >=3、中间还有内容」的行，不碰整行纯装饰的
    分隔线。体检（match_chapter / _numbering）和清洗（model.parse_heading /
    标题取值）都走这一份，避免报告说有 197 章、清洗切出 0 章。
    """
    m = _FRAME_RE.match(line)
    if m is None:
        return line
    left, right = m.group(1), m.group(3)
    # 两侧必须是同一个装饰字符连写（=== 第1章 --- 不算对称边框）。
    if len(set(left)) != 1 or len(set(right)) != 1 or left[0] != right[0]:
        return line
    core = m.group(2).strip()
    # 中间得剩点像样的内容；纯装饰线、剥完只剩符号的原样保留。
    if not core or not core.strip(_FRAME_CHARS + " \u3000\t"):
        return line
    return core


def match_chapter(line: str) -> str | None:
    line = strip_frame(line)
    for name, pat in CHAPTER_PATTERNS:
        if pat.match(line):
            return name
    return None


_NUM_TITLE = re.compile(r"^\s*第\s*([0-9０-９〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]{1,10})\s*[章回节]")


def _numbering(lines: list[str]) -> dict:
    """章节序号是否连续递增 —— 判断"章节结构是否可信"的最强信号。"""
    nums: list[int] = []
    bad: list[int] = []
    for i, line in enumerate(lines):
        m = _NUM_TITLE.match(strip_frame(line))
        if not m:
            continue
        v = cn_to_int(m.group(1))
        if v is None:
            bad.append(i + 1)
            continue
        nums.append(v)
    if len(nums) < 3:
        return {"numeric_titles": len(nums), "monotonic": None, "unparsed": len(bad)}
    increases = sum(1 for a, b in zip(nums, nums[1:]) if b > a)
    return {
        "numeric_titles": len(nums),
        "monotonic": increases >= len(nums) - 1 - max(1, len(nums) // 10),
        "increasing_pairs": increases,
        "pairs": len(nums) - 1,
        "duplicate_numbers": len(nums) - len(set(nums)),
        "range": [min(nums), max(nums)],
        "unparsed": len(bad),
    }


def find_chapters(lines: list[str], limit: int = 15) -> dict:
    families: Counter[str] = Counter()
    hits: list[int] = []
    samples: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        fam = match_chapter(line)
        if fam:
            families[fam] += 1
            hits.append(i + 1)
            if len(samples) < limit:
                samples.append((i + 1, line.strip()[:60]))
    return {
        "count": len(hits),
        "families": dict(families.most_common()),
        "first_lines": hits[:5],
        "last_lines": hits[-3:],
        "samples": samples,
        "numbering": _numbering(lines),
    }


def find_repeated(lines: list[str], min_count: int = 3, min_len: int = 4, max_len: int = 200) -> list[dict]:
    """整行重复 >= min_count 次 —— 广告水印最可靠、最不需要 AI 的信号。"""
    counter: Counter[str] = Counter()
    where: dict[str, list[int]] = defaultdict(list)
    for i, line in enumerate(lines):
        s = squeeze(line)
        if min_len <= len(s) <= max_len:
            counter[s] += 1
            if len(where[s]) < 8:
                where[s].append(i + 1)
    out = []
    for s, n in counter.most_common(50):
        if n < min_count:
            break
        # 重复的正文句子（例如每章都有同一句景物描写）不是广告，
        # 但"整行重复且带广告特征"几乎必然是水印。分开标注，别让
        # 后面的清洗规则一视同仁地删掉文学性重复。
        has_url = bool(_URL.search(s))
        hit = [k for k in AD_KEYWORDS if k.lower() in s.lower()]
        ad = bool(has_url or hit or _cjk_count(_strip_ad_noise(s)) <= 6)
        out.append(
            {
                "text": s[:120],
                "count": n,
                "lines": where[s],
                "ad_like": ad,
                "keywords": hit[:3],
            }
        )
    return out


def _cjk_count(s: str) -> int:
    return sum(1 for c in s if "\u4e00" <= c <= "\u9fff")


def _strip_ad_noise(text: str) -> str:
    """把链接和广告词从行里摘掉，剩下的才是"正文"。"""
    t = _URL.sub("", text)
    for k in AD_KEYWORDS:
        if k.lower() in t.lower():
            t = re.sub(re.escape(k), "", t, flags=re.IGNORECASE)
    return squeeze(t)


# 判断"广告是不是粘在一句正文后面"时用的句末标点。
# 刻意比 TERMINAL 窄：TERMINAL 里的引号、右括号会出现在广告词内部
# （例如「记住本站」前后带引号），拿它们当"前文已成句"的证据会误降级。
_SENTENCE_END = "。！？…"


# 行尾可能是"句子写完了"的字符：句末标点，外加收尾的右引号/右括号。
# 用来区分"正文句里提到微信"（他加了她的微信。）和"整行就是广告"（求收藏！）。
_LINE_END = _SENTENCE_END + "」』”’）)"


def _real_content_len(text: str) -> int:
    """摘掉广告成分后，还剩多少个"真正的字"。

    只数字母、数字、汉字，标点与空白一律不算：广告行摘完之后往往只剩标点
    （"求收藏！" → "！"），把标点算进正文会让整行广告被判成正文。
    """
    return sum(1 for c in text if c.isalnum())


# AD_KEYWORDS 里有一半其实只是"网址片段"，不是广告词本身。判定"这一行有几个
# 广告词"时必须先把它们剔出去：链接出现一次就能带出 http/.com/.net 三四个命中，
# 混着数会把"这一行提到两次广告词"算成"四个广告词叠在一起"。
_URL_HINTS = ("http", "www.", ".com", ".net", ".cc", ".org", ".xyz", ".top", ".vip")

# 除网址片段之外的广告词：命中它才是"这一行真的在打广告"。
_STRONG = tuple(k for k in AD_KEYWORDS if k not in _URL_HINTS)

# 成对的引号：整行被一对引号包住，就是一句台词/一段引用。台词里提到广告词
# （"「微信联系。」"）不是广告，一律不许整行删。
_QUOTE_PAIRS = tuple(zip("「『\"'“‘", "」』\"'”’"))

# 成对的括号：括号里只剩广告词才算"引用"（《微信》、（微信）），
# 里面还成句的（"（未完待续，请收藏本站）"）照旧按广告处理 —— 括号是
# 广告最爱的容器，不能像引号那样一律豁免。
_BRACKET_PAIRS = tuple(zip("（(【[《〈", "）)】]》〉"))

# 叙述性用字。摘掉广告成分后残留这些字，说明剩下的还成句（"微信响了"→"响了"），
# 不能当广告碎片删掉。这张表只能用来**阻止删除**，不能拿来证明"这是正文"：
# 表越宽，越容易把广告词自己的残留（"联系我"里的"我"、"天才一秒"里的"一"）
# 认成正文，导致真广告漏删。所以刻意不收数字、"我/你"这类在广告里也常见的字。
_NARRATIVE = "他她它您咱的了着过吗呢吧啊呀哦就在还都也把被让给很太是有说看想问走开关来去"


def _is_quoted_line(text: str) -> bool:
    """整行是不是恰好被一对引号包住 —— 也就是一句台词。

    必须放在判定链最前面、并且**直接返回 False**（不是只挡某一条分支）：
    "「微信联系。」" 摘掉广告词后还剩"联系"两个字，旧判据会把它当广告碎片删掉，
    可它是角色说的话，删掉就是删正文。
    """
    s = text.strip()
    for left, right in _QUOTE_PAIRS:
        if len(s) >= 3 and s[0] == left and s[-1] == right:
            return True
    return False


def _bracketed_ad_only(text: str) -> bool:
    """整行被一对括号包住、里面除广告词没别的 —— 书名、词条，不是广告。

    《微信》／（微信）摘掉广告词后正文长度是 0，旧判据"摘完只剩标点 =>
    整行广告"会把它们删掉，可它们是一本书名、一个词条。

    只认**整行被一对括号包住**这一种形态：括号出现在行中间时
    （"一秒记住【笔趣阁www.biquge.com】"）括号外还有广告成分，不能保护。
    """
    s = text.strip()
    for left, right in _BRACKET_PAIRS:
        if len(s) < 3 or s[0] != left or s[-1] != right:
            continue
        inner = s[1:-1]
        if inner and _real_content_len(_strip_ad_noise(inner)) == 0:
            return True
    return False


def _has_narrative(residual: str) -> bool:
    """残留里有没有叙述性用字。"""
    return any(ch in _NARRATIVE for ch in residual)


# 比 _NARRATIVE 更窄的一张表，只用在"长广告行"那一支（⑦）。
# 为什么不能直接复用 _NARRATIVE：那张表里收了"问、看、是、有、就、在、还、把、给"
# 这类高频字，广告里到处都是（"欢迎访问"的"问"、"回馈读者"的"者"）——
# 拿它拦长广告行，"馨香E书…欢迎访问…高速免费下载E书" 会被"问"字整个救下来。
# 这里只留"几乎只出现在叙述句里"的字：人称代词、结构助词、趋向动词。
# 这一支真正的防线是"同一行两个以上链接"，这张表只是防叙述行顺手带两个网址。
_STRONG_STORY = "他她我你您咱的了着过吗呢吧啊呀哦说走开关来去"


def _has_story_word(residual: str) -> bool:
    """残留里有没有"只出现在叙述句里"的字（见 _STRONG_STORY）。"""
    return any(ch in _STRONG_STORY for ch in residual)


def _body_hits(hits: list[str]) -> list[str]:
    """只留真正的广告词，剔掉网址片段。"""
    return [k for k in hits if k not in _URL_HINTS]


# 行首广告后面还跟着正文句时，用来判断"后面那句是不是叙述"。刻意只收代词：
# 这张表越宽越容易把广告文案（"更优质的阅读体验"里的"的"）也当成叙述，
# 真广告就漏删了。收窄到"代词开头"只是漏掉一部分该删的广告，方向是对的。
_AD_SUBJECT = "他她它我你您咱"


def _ad_signal_spans(text: str) -> list[tuple[int, int]]:
    """这一行里所有广告信号的 [起, 止) 区间（取首尾两次出现就够用）。"""
    spans = [(m.start(), m.end()) for m in _URL.finditer(text)]
    lowered = text.lower()
    for k in AD_KEYWORDS:
        kl = k.lower()
        first = lowered.find(kl)
        if first < 0:
            continue
        spans.append((first, first + len(kl)))
        last = lowered.rfind(kl)
        spans.append((last, last + len(kl)))
    return spans


def _ad_head_then_body(text: str) -> bool:
    """行首是广告成分、行尾还跟着一句独立的叙述句 —— 这是「广告 + 正文」。

    整行广告的残留是**不成句的碎片**（"方便下次阅读！"）；一旦广告成分后面
    还接着一句有主语、有句号的话（"求收藏 www.x.com 他走进屋里，坐下喝茶。"），
    整行删就是把正文一起删了。这一支只放行、不判定，越过它继续走后面的判据。

    三个条件缺一不可：信号在行首附近、信号之后那段以句末标点收尾且够长、
    那段里还带着人称代词。第三条是保守阀 —— 宁可有广告漏删，也不能删正文。
    """
    spans = _ad_signal_spans(text)
    if not spans:
        return False
    if min(s for s, _ in spans) > 2:
        return False
    tail = text[max(e for _, e in spans) :]
    if not tail.endswith(tuple(_SENTENCE_END)):
        return False
    if _real_content_len(tail) < 6:
        return False
    if _ad_signal_spans(tail):
        return False
    return any(ch in _AD_SUBJECT for ch in tail)


def _ad_follows_sentence(text: str) -> bool:
    """广告成分是不是跟在"已经写完的一句正文"后面。

    只看广告成分**第一次出现的位置之前**有没有句末标点。广告里的逗号
    （"本章未完，点击下一页"）不算，所以只认 _SENTENCE_END。

    还有个坑：广告词自己也可能带句末标点。"求收藏！求推荐！求月票！" 里
    第二个词之前的"求收藏！"看着像一句写完的正文，其实是同一句广告的前半截。
    所以光看标点不够，还要看前半截**摘掉广告成分之后还剩不剩字**：只剩标点
    说明前半截也是广告，不能拿它当"正文已成句"的证据。
    这一条不能收紧到"剩余至少 3 个字"：那样 "走。记住本站 www.x.com"
    里那一句真句子（"走。"）就护不住了，整行会被连正文一起删掉。
    """
    lowered = text.lower()
    positions: list[int] = []
    m = _URL.search(text)
    if m is not None:
        positions.append(m.start())
    for k in AD_KEYWORDS:
        idx = lowered.find(k.lower())
        if idx > 0:
            positions.append(idx)
    if not positions:
        return False
    for p in sorted(positions):
        head = text[:p]
        if not any(ch in _SENTENCE_END for ch in head):
            continue
        if _real_content_len(_strip_ad_noise(head)) >= 1:
            return True
    return False


def _judge_whole_line(
    stripped: str,
    hits: list[str],
    body_hits: list[str],
    has_url: bool,
    residual: str,
    residual_real: int,
) -> bool:
    """这一行是不是「整行都是广告」，可以整行删掉。

    判定顺序就是优先级，前四条全是"不许删"的守卫。现在的取法是
    **宁可漏删、不许误删**：漏掉的广告留在正文里，报告会点名，M4 再处理；
    误删的正文没人能找回来。所以每条放行判据都刻意窄，未知情况一律不放行。
    """
    # ① 整行被一对引号包住（台词），或整行是一对括号包着的广告词（书名词条）：
    #    这是在"提到"广告词，不是广告。
    if _is_quoted_line(stripped):
        return False
    if _bracketed_ad_only(stripped):
        return False
    # ② 广告成分跟在写完的正文句后面：正文行尾粘广告，整行删会把正文删掉。
    if _ad_follows_sentence(stripped):
        return False
    # ③ 行首是广告成分、行尾还跟着一句独立的正文（"求收藏 www.x.com 他走进屋里，
    #    坐下喝茶。"）：这是"广告在前、正文在后"，跟②是同一种病的另一头。
    #    只看残留字数分不出来 —— 广告在前面时残留是整句正文，④ 的 <=12 拦不住
    #    短句（"淘宝 www.taobao.com 上买。"残留"淘宝上买。"只 4 个字），整行删就把
    #    正文删了。这一支只放行、不判定。
    if _ad_head_then_body(stripped):
        return False
    # ④ 摘完只剩标点：这一行除了广告词没有别的内容。
    if residual_real == 0:
        return True
    # ⑤ 带链接 + 至少一个真的广告词 + 残留不成句。要求 body_hits 非空是关键：
    #    只看"有链接"会把 "他把 Mr.Lee 的信折好放进口袋"（Mr.Lee 被 _URL 当成
    #    域名）、"我打开 readme.md 看了眼" 这类正文当广告删掉。
    if has_url and body_hits and residual_real <= 12 and len(stripped) <= 80:
        return True
    # ⑥ 命中广告词、残留极短、而且残留里没有叙述性用字。
    #    "更多精彩内容"（残留"内容"）、"微信：abc123" 走这条。
    #    残留一出现"了/他/就"这类字就说明还成句（"微信响了"→"响了"），不能删。
    if hits and residual_real <= 8 and not _has_narrative(residual):
        return True
    # ⑦ 两个以上真的广告词叠在一起，残留不成句。
    #    只数 body_hits：链接一次性能带出三四个网址片段，混着数会把单词命中
    #    算成"多个广告词叠加"，把 "他把校对好的稿子手打了一遍" 删掉。
    if (
        len(body_hits) >= 2
        and residual_real <= 15
        and len(stripped) <= 60
        and not _has_narrative(residual)
    ):
        return True
    # ⑧ 长广告行：链接在同一行里出现两次以上、整行连一个句末标点都没有、
    #    残留里也没有叙述性用字。
    #    这一支是给"整行都是广告但广告词不在词表里"的长行准备的 ——
    #    "馨香E书 www.135f.com  倾情奉献      欢迎访问馨香E书www.135f.com 高速免费下载E书"
    #    摘掉链接后还剩 24 个真字，④⑤⑥ 三个短残留分支全兜不住，
    #    可它连一句完整的话都不成（24 个字没一个句末标点）。
    #    为什么必须"链接出现两次以上"：正文里提到一次网址很常见
    #    （"他打开手机输入 www.example.com 然后关掉"），那种行绝不能删；
    #    正经叙述把同一个网址写两遍几乎不发生，而广告行反复刷同一域名是常态。
    if (
        len(_URL.findall(stripped)) >= 2
        and not any(ch in _SENTENCE_END for ch in stripped)
        and not _has_story_word(residual)
        and residual_real <= 60
    ):
        return True
    # ⑨ 其余一律留着：算行内广告，报告里点名，M4 处理。
    return False


def judge_ad_line(line: str, line_no: int = 0) -> dict | None:
    """判定单独一行是不是广告：是广告返回证据字典，否则返回 None。

    为什么要把这条判据抽成独立函数：体检只需要前几条样本（报告里展示不下几百条），
    清洗却必须逐行判定每一条。判据如果两边各写一份，迟早出现
    "体检说有 9 行广告、清洗删了 11 行"这种对不上的情况。抽出来之后
    find_ads 与 clean.py 调的是同一段代码，判据只有一份。

    判据（沿用体检口径，不是"含没含广告词"—— 正文里也可能出现"微信"）：
    **把广告成分摘掉之后还剩多少正文**。剩不下正文 => 整行都是广告，可删；
    还剩下成句的正文 => 行内夹广告，不能整行删（那是 M4 的活）。
    返回值里的 whole_line 字段就是"整行都是广告"这个结论。
    """
    stripped = line.strip()
    if not stripped:
        return None
    # 全角先折成半角再判（见 _FOLD_WIDE）：判据要对齐"最终进书的那一行"。
    stripped = stripped.translate(_FOLD_WIDE)
    lowered = stripped.lower()
    hits = [k for k in AD_KEYWORDS if k.lower() in lowered]
    has_url = bool(_URL.search(stripped))
    if not hits and not has_url:
        return None
    residual = _strip_ad_noise(stripped)
    residual_cjk = _cjk_count(residual)
    residual_real = _real_content_len(residual)
    body_hits = _body_hits(hits)
    whole = _judge_whole_line(
        stripped, hits, body_hits, has_url, residual, residual_real
    )
    return {
        "line": line_no,
        "keywords": hits[:4],
        "has_url": has_url,
        "residual_cjk": residual_cjk,
        "text": stripped[:100],
        "whole_line": whole,
    }


def find_ads(lines: list[str], samples: int = 10) -> dict:
    """汇总广告行：区分「整行都是广告」和「广告粘在正文行中间」。

    逐行判据全部见 judge_ad_line()，本函数只负责分类、计数、取样。
    samples 默认 10：体检报告只展示前几条，返回全量会让 JSON 报告变得很大。
    清洗要全量结果时直接逐行调 judge_ad_line()，不要指望这个函数返回全量。
    """
    whole_line: list[dict] = []
    inline: list[dict] = []
    for i, line in enumerate(lines):
        entry = judge_ad_line(line, i + 1)
        if entry is None:
            continue
        if entry.pop("whole_line"):
            whole_line.append(entry)
        else:
            inline.append(entry)
    return {
        "whole_line_count": len(whole_line),
        "inline_count": len(inline),
        "whole_line_samples": whole_line[:samples],
        "inline_samples": inline[:samples],
    }


def paragraph_shape(lines: list[str]) -> dict:
    nonblank = [ln.strip() for ln in lines if ln.strip()]
    if not nonblank:
        return {"nonblank": 0}
    lengths = [len(ln) for ln in nonblank]
    short_break = sum(1 for ln in nonblank if len(ln) <= 45 and not ln.rstrip().endswith(tuple(TERMINAL)))
    over_long = sum(1 for ln in nonblank if len(ln) > 800)
    return {
        "nonblank": len(nonblank),
        "avg_len": round(sum(lengths) / len(lengths), 1),
        "max_len": max(lengths),
        "short_break_count": short_break,
        "short_break_ratio": round(short_break / len(nonblank), 3),
        "over_long_count": over_long,
        "zero_width_count": sum(1 for ln in lines if any(z in ln for z in _ZERO_WIDTH)),
        "trailing_space_count": sum(1 for ln in lines if ln != ln.rstrip()),
    }


def detect_toc(lines: list[str], scan: int = 400, max_gap: int = 4) -> dict:
    """开头成片出现章节标题 => 八成是目录块，删掉或转成 nav。

    判据不是"开头有多少个标题"（正常小说开头也有标题），而是
    **开头是否存在一段连续密集、相邻标题之间几乎没有正文的簇**。
    目录块的典型形态：标题一行接一行，间距 <= max_gap。
    """
    head = lines[:scan]
    hits = [i for i, ln in enumerate(head) if match_chapter(ln)]
    # 找出"间距都很小"的最长连续段
    best_run: list[int] = []
    run: list[int] = []
    for h in hits:
        if run:
            between = [head[i].strip() for i in range(run[-1] + 1, h) if head[i].strip()]
            # 中间夹着成句正文 => 不属于同一簇（这是正常章节之间的过渡）
            if h - run[-1] > max_gap or any(t.endswith(tuple(TERMINAL)) for t in between):
                run = []
        run.append(h)
        if len(run) > len(best_run):
            best_run = list(run)

    # 簇锁定之后往后延伸，直到不再命中为止。
    #
    # 为什么要延伸：scan 只是"先看多少行"的成本闸，不是"目录不会超过这么多行"
    # 的事实。400 章以上的长目录（条目之间还夹空行，总行数超 400）会被腰斩，
    # 后一半条目留在正文里变成一串空章节，而报告里报的是一个"看起来正常"的
    # 目录长度。簇一旦立住，继续往后找同间距的标题是安全的：判据没变，只是
    # 不再受 scan 限制。
    if best_run:
        last = best_run[-1]
        for h in range(last + 1, len(lines)):
            if not match_chapter(lines[h]):
                continue
            between = [lines[i].strip() for i in range(last + 1, h) if lines[i].strip()]
            if h - last > max_gap or any(t.endswith(tuple(TERMINAL)) for t in between):
                break
            best_run.append(h)
            last = h
    density = len(hits) / max(len(head), 1)

    # 第二个必要条件：**这一簇里标题之间几乎没有正文**。
    # 正常小说章节之间是成段的叙述；短段落也可能凑成"密集簇"，
    # 所以还要看夹在标题中间的行像不像句子。
    prose_ratio = None
    if best_run:
        lo, hi = best_run[0], best_run[-1]
        # 这里必须用 lines 而不是 head：簇可能已经延伸到 scan 之外（见上面的
        # 延伸逻辑），拿 head 取下标会 IndexError。
        picked = set(best_run)
        between = [
            lines[i].strip()
            for i in range(lo, hi + 1)
            if i not in picked and lines[i].strip()
        ]
        if between:
            ends_sentence = sum(1 for t in between if t.endswith(tuple(TERMINAL)))
            prose_ratio = round(ends_sentence / len(between), 3)
    suspected = len(best_run) >= 5 and (prose_ratio is None or prose_ratio < 0.5)

    return {
        "scanned": len(head),
        "hits": len(hits),
        "density": round(density, 3),
        "cluster_len": len(best_run),
        "cluster_first_lines": [i + 1 for i in best_run[:10]],
        # 完整簇（不限前 10 条）：清洗要按它划出目录块的行号范围，
        # 只给 10 条样本会导致长目录只删掉一半。
        "cluster_lines": [i + 1 for i in best_run],
        "prose_between_ratio": prose_ratio,
        "suspected": suspected,
        "first_hits": [i + 1 for i in hits[:10]],
    }


# 为什么：见 docs/notes/no-semantic-judgment.md
def whole_book_in_one_line(lines: list[str]) -> dict:
    """整本书糊成一个超长行，是"下载时网页没分段"的典型症状。

    判据只有一条：**存在超过 3000 字的行**。book05 那种正常书正文里引用几个
    章节号（"参见第 3 章、第 7 章……"）是家常便饭，一行里出现两个以上章节标记
    根本不能说明什么 —— 这条旧判据在 78 字的注释清单上就误报过一次
    （backups/mask-20261025/twins40/Bba2cae.txt:16225）。所以
    "一行里塞了多个章节标记"只作为证据留档，不参与 suspected。

    字段名是跨模块契约（report.py / tools/batch_check.py / tools/smoke_test.py
    都读），三个都得留。
    """
    long_lines = [(i + 1, len(ln)) for i, ln in enumerate(lines) if len(ln) > 3000]
    pat = re.compile(r"第\s*[0-9〇零一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾]{1,8}\s*[章回节]")
    chapter_marks_inside = 0
    for ln in lines:
        if len(pat.findall(ln)) >= 3:
            chapter_marks_inside += 1
    return {
        "long_line_count": len(long_lines),
        "long_line_samples": long_lines[:5],
        "lines_with_multiple_chapters": chapter_marks_inside,
        "suspected": len(long_lines) > 0,
    }


def diagnose(path: Path) -> dict:
    raw = path.read_bytes()
    candidates = detect(raw)
    encoding = candidates[0].encoding
    text = decode(raw, encoding)
    lines = text.splitlines()

    result = {
        "file": str(path),
        "size_bytes": len(raw),
        "line_count": len(lines),
        "blank_count": sum(1 for ln in lines if not ln.strip()),
        "encoding": {
            "chosen": encoding,
            "confidence": round(candidates[0].score, 3),
            "candidates": [c.as_dict() for c in candidates[:3]],
        },
        "chapters": find_chapters(lines),
        "repeated_lines": find_repeated(lines),
        "ads": find_ads(lines),
        "paragraphs": paragraph_shape(lines),
        "toc": detect_toc(lines),
        "single_line_book": whole_book_in_one_line(lines),
    }
    result["actions"] = _suggest(result)
    return result


def _suggest(r: dict) -> list[str]:
    out: list[str] = []
    enc = r["encoding"]["chosen"]
    if not enc.startswith("utf-8"):
        out.append(f"先转码：{enc} → UTF-8")
    if r["single_line_book"]["suspected"]:
        out.append("疑似整本书糊成超长行，需要先按章节标记重新切行（M2 重点）")
    if r["toc"]["suspected"]:
        out.append(
            f"开头有一簇 {r['toc']['cluster_len']} 行连续章节标题（目录块），清洗时删除或转成电子书目录"
        )
    if r["repeated_lines"]:
        ad_like = [x for x in r["repeated_lines"] if x["ad_like"]]
        plain = len(r["repeated_lines"]) - len(ad_like)
        if ad_like:
            top = ad_like[0]
            out.append(f"{len(ad_like)} 条重复行带广告特征（最多重复 {top['count']} 次），可整行删除")
        if plain:
            out.append(f"另有 {plain} 条重复行不带广告特征，可能只是作者重复用句，建议先预览再决定")
    ads = r["ads"]
    if ads["whole_line_count"]:
        out.append(f"{ads['whole_line_count']} 行整行广告，规则可直接删")
    if ads["inline_count"]:
        out.append(f"{ads['inline_count']} 行广告粘在正文里，必须做行内切除（M4 AI 兜底的主力场景）")
    ch = r["chapters"]
    if ch["count"] == 0:
        out.append("完全没识别到章节标题，需要 AI 或更强的启发式来推断章节边界")
    elif ch["numbering"].get("monotonic") is False:
        out.append("章节序号不连续，可能有缺章或标题被污染")
    p = r["paragraphs"]
    if p.get("short_break_ratio", 0) >= 0.3:
        out.append(f"{p['short_break_ratio']:.0%} 的行是短碎裂行，需要合并段落")
    if p.get("zero_width_count"):
        out.append(f"{p['zero_width_count']} 行含零宽字符，需清除")
    if not out:
        out.append("未发现明显问题，可以直接进打包流程")
    return out


def find_ad_signal(text: str) -> int | None:
    """返回这一行里**最早出现的广告信号字符下标**；没有信号返回 None。

    为什么需要这个函数：judge_ad_line 只回答"整行是不是广告"，答不了
    "广告是从第几个字开始的"。而行内切除必须先知道广告从哪儿起，才能
    往前找句末标点、把尾巴切掉。判据必须与 judge_ad_line **同源**
    （同一份 _URL、同一份 AD_KEYWORDS）—— 两处判据各自演化，这个项目
    已经出过一次"体检说 9 行广告、清洗删了 11 行"的事故。

    为什么只收 idx > 0 的命中（行首命中一律不算信号）：
    本函数的用途是「从信号处往前找句末标点，把后面的广告尾巴切掉」。
    信号落在行首时前面一个字都没有，无正文可切、无切点可言；
    而且行首命中往往正是「「微信！」他喊了一声。」这类台词/正文句，
    把行首命中当信号只会诱导调用方去切它。

    返回**字符下标**（不是字节下标），调用方可以直接拿去切片。
    """
    if not text:
        return None
    # 全角折半角：只影响匹配，长度不变，返回的下标对原文仍然成立。
    text = text.translate(_FOLD_WIDE)
    hits: list[int] = []
    # 网址：用 finditer 把所有命中都收进来，而不是只看第一个 ——
    # 第一个命中落在行首时不该当成信号，但第二个命中照样是信号，
    # 只取 search() 会把它漏掉。
    for m in _URL.finditer(text):
        if m.start() > 0:
            hits.append(m.start())
    for k in AD_KEYWORDS:
        mk = re.search(re.escape(k), text, re.IGNORECASE)
        if mk is not None and mk.start() > 0:
            hits.append(mk.start())
    if not hits:
        return None
    return min(hits)
