"""编码探测。

为什么不能用 try/except 判编码：GB18030 是双字节超集，几乎任意字节序列都能"成功"
解码。所以判定不能靠"能不能解"，只能靠"解出来的文本像不像中文"。
常用汉字占比是最可靠的判据：编码猜错时解出来看着也是汉字，但绝大多数是生僻字或错字。
"""

from __future__ import annotations

from dataclasses import dataclass

COMMON_CHARS = frozenset(
    "的一是了我不人在他有这上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家"
    "可下而过天去能对小多然于心学么之都好看起发当没成只如事把还用第样道想作种开美总从无"
    "情己面最女但现前些所同日手又行意动方期它头经长儿回位分爱老因很给名法间斯知世什两次"
    "使身者被高已亲其进此话常与活正感见明问力理点业外将干都还个那"
)

# utf-16-le/be 放在池尾，不是随便排的：
# 1) 判据只用常用字占比，不用"能不能严格解码"。任意偶数字节的流按 UTF-16LE
#    几乎总能严格解码成功（一本 GB18030 的书也是），strict_ok 在这里没有区分力；
#    真 UTF-16LE 的书按 gb18030 解出来同样是汉字（错字）且常常严格成功，
#    唯一能把它分开的是解出来像不像"常用"中文。
# 2) 分数打平时 sort 是稳定的，池尾意味着纯 ASCII（三种解法都 0 分）仍选 utf-8，
#    不会把英文/数字书抢给 utf-16-le。LE 在 BE 之前：中文乱码场景 BE 几乎不出现。
_CANDIDATES = ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16-le", "utf-16-be")

# BOM 顺序有意义：UTF-32 的 BOM 以 UTF-16 的 BOM 开头，必须先匹长的。
_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

# 只靠"解出来像不像中文"分不出 UTF-16：双字节文本按 gb18030 解出来也全是
# 汉字（错字），分数甚至更高。这类编码**只能靠 BOM 认**，一旦 BOM 命中就
# 直接采信，不再参与打分排序 —— 否则一本 UTF-16 的书会按 gb18030 解成满篇
# 乱码，章节一个都切不出来，而报告里只会写"编码 gb18030，置信度 -0.296"。
_BOM_DECISIVE = ("utf-16", "utf-32")


@dataclass
class Candidate:
    encoding: str
    score: float
    cjk_ratio: float
    common_ratio: float
    replacement_ratio: float
    strict_ok: bool
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "encoding": self.encoding,
            "score": round(self.score, 4),
            "cjk_ratio": round(self.cjk_ratio, 4),
            "common_ratio": round(self.common_ratio, 4),
            "replacement_ratio": round(self.replacement_ratio, 4),
            "strict_ok": self.strict_ok,
            "note": self.note,
        }


def _score_text(text: str) -> tuple[float, float, float, float]:
    n = max(len(text), 1)
    repl = cjk = common = rare = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            cjk += 1
            if ch in COMMON_CHARS:
                common += 1
        elif 0x3400 <= o <= 0x4DBF or 0xE000 <= o <= 0xF8FF:
            rare += 1
        elif ch == "\ufffd":
            repl += 1
    cjk_ratio = cjk / n
    common_ratio = common / cjk if cjk else 0.0
    replacement_ratio = repl / n
    rare_ratio = rare / n
    score = common_ratio - 4.0 * replacement_ratio - 2.0 * rare_ratio
    return score, cjk_ratio, common_ratio, replacement_ratio


def _looks_like_garbage(common_ratio: float, cjk_ratio: float) -> bool:
    """BOM 分支专用：解出来的东西像不像"根本不是中文文本"。

    只看常用字占比 —— 它是唯一能拉开两个数量级的量。
    BOM 分支不参与打分排名（BOM 是文件自己的声明，比启发式可靠），
    所以这里要判断的不是"该选哪个编码"，而是"这个文件到底是不是文本"。

    实测标定（都是 backups/corpus-20261025/ 下的样本）：
    - 真实 UTF-16 有 BOM 的中文书 16 本：common_ratio 最低 0.473；
    - 真中文 utf16le 被截断 1/3/5 字节：common_ratio 0.458（replacement 极低）；
    - 伪 BOM + 随机二进制：common_ratio 0.005~0.007；
    - 伪 BOM + 能严格解码的可用码位：common_ratio 0.005（strict_ok，最阴的一种）。
    真书与垃圾之间有 3 倍以上余量，取 0.15 落在空档里。

    cjk_ratio == 0 的情形（整篇没有中日韩字符）不归这条管：那可能是
    一本英文书，轮不到我们判它是垃圾。
    """
    return cjk_ratio > 0.0 and common_ratio < 0.15


def detect(raw: bytes, sample_bytes: int = 4_000_000) -> list[Candidate]:
    """返回按可信度降序排列的编码候选。"""
    head = raw[:sample_bytes]
    bom_encoding = None
    for bom, enc in _BOMS:
        if head.startswith(bom):
            bom_encoding = enc
            break

    # BOM 已经指明了 UTF-16/UTF-32 时不再打分：这类编码的"像不像中文"没有
    # 区分力，硬凑一个排名只会把正确答案排到后面去。BOM 是文件自己声明的
    # 事实，比任何启发式都可靠。
    if bom_encoding in _BOM_DECISIVE:
        try:
            text = head.decode(bom_encoding)
            strict_ok = True
        except (UnicodeDecodeError, LookupError):
            strict_ok = False
            try:
                text = head.decode(bom_encoding, errors="replace")
            except LookupError:
                text = ""
        score, cjk_ratio, common_ratio, replacement_ratio = _score_text(text)
        note = "检出 BOM，按 BOM 指定的编码解码"
        if not strict_ok:
            note += "；严格解码失败，按替换字符评分"
        if cjk_ratio == 0.0:
            note += "；无中日韩字符，疑似非中文文本"
        elif _looks_like_garbage(common_ratio, cjk_ratio):
            # BOM 只说明"前两个字节写着 UTF-16"，不保证后面是文本：
            # 二进制文件被别的程序强行加上 BOM 也会走到这里，而且能严格解开。
            # 真书的常用字占比实测最低 0.47（400 本语料），伪 BOM 的随机内容
            # 只有 0.005 —— 差两个数量级，所以这条判据不靠猜。
            note += "；常用字占比极低，疑似乱码或非文本文件"
        return [
            Candidate(
                bom_encoding, score + 0.5, cjk_ratio, common_ratio, replacement_ratio, strict_ok, note
            )
        ]

    out: list[Candidate] = []
    for enc in _CANDIDATES:
        # 没有 BOM 时 utf-8-sig 与 utf-8 解出来完全一样，留着只会让报告里
        # 出现两条打平的候选，无法判断"到底有没有 BOM"
        if enc == "utf-8-sig" and bom_encoding != "utf-8-sig":
            continue
        strict_ok = True
        try:
            text = head.decode(enc)
        except (UnicodeDecodeError, LookupError):
            strict_ok = False
            try:
                text = head.decode(enc, errors="replace")
            except LookupError:
                continue

        score, cjk_ratio, common_ratio, replacement_ratio = _score_text(text)
        note = "" if strict_ok else "严格解码失败，按替换字符评分"
        if bom_encoding == enc:
            score += 0.5
            note = (note + " 检出 BOM").strip()
        if cjk_ratio == 0.0:
            note = (note + " 无中日韩字符，疑似非中文文本").strip()
        out.append(
            Candidate(enc, score, cjk_ratio, common_ratio, replacement_ratio, strict_ok, note)
        )

    out.sort(key=lambda c: c.score, reverse=True)
    # 兜底告警：所有候选都严格解码失败**且最高分仍为负**（替换字符/生僻字的
    # 罚分压过了常用字收益），说明这不是"编码猜错"而是二进制/截断/损坏文件，
    # 解出来的就是乱码。以前这种情况静默返回最高分乱码，下游只会得到"0 章的
    # 无章节书"，没人知道是解码失败。写进最高分候选的 note，diagnose 的报告
    # 与 batch_check 的告警都能看到。
    # 分数为正的例外必须放过：真 UTF-16LE 被截掉半个字时，utf-16-le 严格
    # 失败但替换解码仍是干净中文（score>0），此时"严格解码失败"的 note 已经
    # 够了，再喊"解码失败"就是误伤 —— 那本书解得基本没问题。
    top = out[0] if out else None
    if top is not None and top.score < 0.0 and all(not cand.strict_ok for cand in out):
        top.note = (top.note + " 所有编码候选都严格解码失败，判定为解码失败，文本可能损坏").strip()
    return out


def best(raw: bytes) -> Candidate:
    return detect(raw)[0]


def decode(raw: bytes, encoding: str | None = None) -> str:
    if encoding is None:
        encoding = best(raw).encoding
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return raw.decode(encoding, errors="replace")
