"""normalize.py 的用例。直接跑：python tests/test_normalize.py

风格照抄 tools/smoke_test.py：不用 pytest，一个 case 一个 assert，
末尾打印 ok/FAIL 汇总并设置退出码。全部是假数据，不读任何真实书。

覆盖：零宽清除、缩进保留、省略号各写法、中英混排空格、全角数字、
引号嵌套、幂等性、以及几处"绝对不能动"的敏感边界（3.14 / Hello World）。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban import diagnose, normalize  # noqa: E402
from huoban.normalize import (  # noqa: E402
    INDENT_STR,
    analyze_line,
    clean_line,
    find_zero_width,
    normalize_line,
    normalize_punctuation,
    normalize_quotes,
    strip_zero_width,
)

# 幂等性用这份语料反复跑；random 部分再补一份随机组合。
CORPUS = [
    "",
    "   ",
    "\u3000\u3000",
    "\u3000\u3000他说：“你好。”",
    "\u3000\u3000他说。。。  ",
    "   四个半角空格缩进",
    "\t制表符缩进",
    "\ufeff行首 BOM",
    "行\u200b中零宽",
    "他说...然后走了",
    "他说。。。然后走了",
    "他说……然后走了",
    "他说…然后走了",
    "什么!!",
    "什么!?",
    "什么?!",
    "真的？？？",
    "十~十五年",
    "10~20",
    "a~b",
    "他说~~~",
    "——分隔——",
    "------分隔------",
    "他 说 话",
    "10 km",
    "第 12 章",
    "Hello World",
    "He said: 3.14 ok",
    "v1.2.3",
    "www.example.com",
    "a.com/...",
    "０１２ＡＢＣ，！？",
    "「他说：「走吧」」",
    "『他说：『走吧』』",
    "“他说：‘走吧’”",
    "”孤立的闭引号",
    "‘顶层单引号’",
    "👨\u200d👩\u200d👧 一家",
]


# ---------------------------------------------------------------------------
# 一、零宽字符
# ---------------------------------------------------------------------------


def case_strip_zero_width_basic():
    """U+200B/200C/200D/2060/FEFF 等一律删掉。"""
    raw = "他\u200b说\u200c好\u200d的\u2060。\ufeff"
    assert strip_zero_width(raw) == "他说好的。", repr(strip_zero_width(raw))
    assert strip_zero_width("正常文本") == "正常文本"


def case_strip_zero_width_bom_positions():
    """BOM 不只出现在文件头：行首、行中、出现多次，都是残留，都删。"""
    assert strip_zero_width("\ufeff第一章") == "第一章"
    assert strip_zero_width("他说\ufeff不定") == "他说不定"
    assert strip_zero_width("\ufeff\ufeff\ufeff开头") == "开头"
    # 删除后不该再有可删字符
    assert find_zero_width(strip_zero_width("\ufeffa\u200bb"))["removable"] == 0


def case_strip_zero_width_keeps_emoji_zwj():
    """emoji 组合靠 ZWJ 连接，删了 emoji 会散架 —— 必须保留。"""
    family = "👨\u200d👩\u200d👧"
    assert strip_zero_width(family) == family
    # 但同一行里的普通 ZWSP 照删
    assert strip_zero_width("👨\u200d👩\u200d👧\u200b一家") == family + "一家"


def case_find_zero_width_accounts():
    """删除必须可核对：删了几个、各是什么码位，都要报得出来。"""
    info = find_zero_width("\ufeffa\u200bb\u200bc")
    assert info["removable"] == 3, info
    assert info["by_char"] == {"U+FEFF": 1, "U+200B": 2}, info
    assert info["total"] == 3
    # emoji 连接符算"保留"，不算删除
    info2 = find_zero_width("👨\u200d👩")
    assert info2["kept_emoji_joined"] == 1 and info2["removable"] == 0, info2


# ---------------------------------------------------------------------------
# 二、空白与缩进
# ---------------------------------------------------------------------------


def case_clean_line_indent_preserved():
    """两个全角空格 / 4 个半角空格 / Tab 都是段首缩进，归一成两个全角空格。"""
    for raw in ("\u3000\u3000他说", "    他说", "\t他说"):
        got = clean_line(raw)
        assert got.indented is True, (raw, got)
        assert got.text == INDENT_STR + "他说", (raw, got.text)
    # 全角缩进和半角缩进归一到同一形态 —— 这是"两态归一"的意思
    assert clean_line("\u3000\u3000他说").text == clean_line("    他说").text


def case_clean_line_no_indent():
    """1 格空白不算缩进，去掉就好；正文一个字都不能少。"""
    got = clean_line(" 他说")
    assert got.indented is False and got.text == "他说", got
    assert got.indent_width == 1
    got2 = clean_line("他说")
    assert got2.indented is False and got2.text == "他说" and got2.indent_width == 0


def case_clean_line_trailing_removed():
    """行尾空白（含全角空格、Tab）删掉，删了什么要记下来。"""
    got = clean_line("他说\u3000\u3000  ")
    assert got.text == "他说", repr(got.text)
    assert got.trailing_removed == "\u3000\u3000  ", repr(got.trailing_removed)
    got2 = clean_line("他说\t")
    assert got2.text == "他说" and got2.trailing_removed == "\t"


def case_clean_line_collapses_runs():
    """行内连续空白压成一个 —— 但单个空格留着（可能是英文词间空格）。"""
    got = clean_line("他    说")
    assert got.text == "他 说", repr(got.text)
    assert got.spaces_collapsed == 3, got.spaces_collapsed
    # 单个空格不动，交给 normalize_punctuation 按语境决定
    assert clean_line("他 说").text == "他 说"
    assert clean_line("Hello World").text == "Hello World"


def case_clean_line_records_metadata():
    """原始长度、原始缩进原文都在返回值里，报告能逐项核对。"""
    raw = "\u3000\u3000他 说 话   "
    got = clean_line(raw)
    assert got.raw == raw
    assert got.indent_raw == "\u3000\u3000"
    assert got.indent_width == 4
    assert got.trailing_removed == "   "
    assert got.changed is True
    info = got.as_dict()
    assert info["raw_len"] == len(raw) and info["clean_len"] == len(got.text), info


def case_deletions_are_accountable():
    """不许吞正文：清洗后的长度必须能被"删掉的部分"逐项解释清楚。

    len(raw) == len(clean) + 行尾空白 + 原始缩进 + 压掉的空格数 - 归一缩进的 2 格
    （缩进被归一成固定两个全角空格，所以按 indented 换算）。
    """
    rng = random.Random(20261025)
    pieces = ["他", "说", "话", "\u3000", "  ", "\u00a0", "\t", "。", "！", "Hello", "3.14"]
    for _ in range(2000):
        raw = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 6)))
        got = clean_line(raw)
        base = strip_zero_width(raw)
        explained = (
            len(got.text)
            + len(got.trailing_removed)
            + len(got.indent_raw)
            + got.spaces_collapsed
            - (len(INDENT_STR) if got.indented else 0)
        )
        assert explained == len(base), (raw, got, explained, len(base))


def case_clean_line_blank_line():
    """空行/纯空白行不崩，归成空串且不算缩进行。"""
    for raw in ("", "   ", "\u3000\u3000", "\t\t"):
        got = clean_line(raw)
        assert got.text == "", (raw, got.text)
        assert got.indented is False, (raw, got)


# ---------------------------------------------------------------------------
# 三、标点归一
# ---------------------------------------------------------------------------


def case_punct_ellipsis():
    """各种省略号写法收敛到中文省略号"……"。"""
    for raw in ("他说。。。", "他说...", "他说……", "他说…", "他说。。。"):
        assert normalize_punctuation(raw) == "他说……", (raw, normalize_punctuation(raw))
    # 单个句号是正常句号，不碰
    assert normalize_punctuation("他说。") == "他说。"
    assert normalize_punctuation("……") == "……"


def case_punct_bang_question():
    """连续 ! ? 归一；!? 两个语气都留，顺序按原顺序。"""
    assert normalize_punctuation("什么!!") == "什么！"
    assert normalize_punctuation("真的？？") == "真的？"
    assert normalize_punctuation("什么!?") == "什么！？"
    assert normalize_punctuation("什么?!") == "什么？！"
    # 单个半角 ! 可能是英文感叹，不转
    assert normalize_punctuation("Hello!") == "Hello!"


def case_punct_tilde_and_dash():
    """连写波浪号 → ～；连写 - / — → ——；长度 4 以上当分隔线不碰。"""
    assert normalize_punctuation("他说~~~") == "他说～"
    assert normalize_punctuation("十~十五年") == "十～十五年"
    # "--" → "——"，但连写 4 个以上多半是分隔线，长度分不清就不猜，原样保留
    assert normalize_punctuation("他说--然后") == "他说——然后"
    assert normalize_punctuation("——") == "——"
    # 数字区间与英文里的 ~ 不碰
    assert normalize_punctuation("10~20") == "10~20"
    assert normalize_punctuation("a~b") == "a~b"
    # 4 连写以上分不清是破折号还是分隔线，不猜，原样保留
    assert normalize_punctuation("--------") == "--------"
    assert normalize_punctuation("————————") == "————————"


def case_punct_cjk_space():
    """中文之间、中文与数字之间的多余空格删掉；英文/单位/数字之间的保留。"""
    assert normalize_punctuation("他 说") == "他说"
    assert normalize_punctuation("第 12 章") == "第12章"
    assert normalize_punctuation("（ 括号 ）") == "（括号）"
    # 关键边界：这三种一个空格都不许动
    assert normalize_punctuation("Hello World") == "Hello World"
    assert normalize_punctuation("10 km") == "10 km"
    assert normalize_punctuation("3 5") == "3 5"


def case_punct_fullwidth_alnum():
    """全角数字、全角拉丁字母转半角；全角标点是中文标点，绝不转。"""
    assert normalize_punctuation("０１２") == "012"
    assert normalize_punctuation("ＡＢＣａｂｃ") == "ABCabc"
    assert normalize_punctuation("０１２ＡＢＣ，！？：；（）") == "012ABC，！？：；（）"


def case_punct_keeps_english_punct():
    """"英文转全角"是错误方向 —— 小数点、版本号、网址必须原样。"""
    for raw in ("3.14", "v1.2.3", "www.example.com", "a.com/...", "3.5.1"):
        assert normalize_punctuation(raw) == raw, (raw, normalize_punctuation(raw))


# ---------------------------------------------------------------------------
# 四、引号归一
# ---------------------------------------------------------------------------


def case_quotes_nested():
    """外层双、内层单；「」『』也收敛到 “”‘’。"""
    assert normalize_quotes("「他说：「走吧」」") == "“他说：‘走吧’”"
    assert normalize_quotes("『走吧』") == "“走吧”"           # 顶层单层 → 双
    assert normalize_quotes("“他说：‘走吧’”") == "“他说：‘走吧’”"  # 已规范，不动
    assert normalize_quotes("「他说：『走吧』」") == "“他说：‘走吧’”"
    # 三层
    assert normalize_quotes("「a「b「c」」」") == "“a‘b“c”’”"


def case_quotes_orphan_close():
    """闭引号没有开引号配时，按它自己的家族出规范形，不瞎猜层级。"""
    assert normalize_quotes("”孤立的") == "”孤立的"
    assert normalize_quotes("』孤立的") == "’孤立的"


def case_quotes_leaves_straight_quotes():
    """直引号 " ' 没方向，判断朝哪边必须看语义 —— 属于瞎猜，原样保留。"""
    for raw in ('He said "hi"', "it's fine"):
        assert normalize_quotes(raw) == raw, (raw, normalize_quotes(raw))


# ---------------------------------------------------------------------------
# 五、组合入口
# ---------------------------------------------------------------------------


def case_normalize_line_order():
    """四步串起来的顺序不能乱：先删零宽，缩进判定才准。"""
    raw = "\ufeff\u3000\u3000他说：“你好”。。。  "
    got = normalize_line(raw)
    assert got.indented is True, got
    assert got.text == "\u3000\u3000他说：“你好”……", repr(got.text)
    assert got.raw == raw


def case_normalize_line_keeps_english():
    """整行组合处理后，英文与数字依旧完好。"""
    raw = "   版本 v1.2.3 发布于 2024.10 年"
    got = normalize_line(raw)
    assert got.indented is True
    assert "v1.2.3" in got.text and "2024.10" in got.text, got.text
    assert "Hello World" == normalize_line("Hello World").text


# ---------------------------------------------------------------------------
# 六、幂等
# ---------------------------------------------------------------------------


def case_idempotent_corpus():
    """f(f(x)) == f(x)：每个函数、整份语料都要成立。"""
    funcs = [
        ("strip_zero_width", strip_zero_width),
        ("normalize_punctuation", normalize_punctuation),
        ("normalize_quotes", normalize_quotes),
        ("clean_line", lambda s: clean_line(s).text),
        ("normalize_line", lambda s: normalize_line(s).text),
    ]
    for name, f in funcs:
        for raw in CORPUS:
            once = f(raw)
            twice = f(once)
            assert once == twice, (name, raw, once, twice)


def case_idempotent_random():
    """随机组合语料再跑一遍，防止只对写死的样例幂等。"""
    pieces = list("他说什么走吧好的") + [
        "。。", "...", "……", "…", "!!", "!?", "~~~", "--", "——", "～",
        "「", "」", "『", "』", "“", "”", "‘", "’", "\u3000", "  ",
        "\u200b", "\ufeff", "\t", "3.14", "Hello World", "10 km",
        "０１２", "ＡＢＣ", "a.com", " ", "!",
    ]
    rng = random.Random(20261024)
    for _ in range(2000):
        raw = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 8)))
        for name, f in (
            ("strip_zero_width", strip_zero_width),
            ("normalize_punctuation", normalize_punctuation),
            ("normalize_quotes", normalize_quotes),
            ("clean_line", lambda s: clean_line(s).text),
            ("normalize_line", lambda s: normalize_line(s).text),
        ):
            once = f(raw)
            assert f(once) == once, (name, raw, once, f(once))


def case_deterministic():
    """纯函数：同样输入永远同样输出（跑两次比对）。"""
    for raw in CORPUS:
        assert normalize_line(raw).text == normalize_line(raw).text
        assert analyze_line(raw) == analyze_line(raw)


# ---------------------------------------------------------------------------
# 七、单行特征
# ---------------------------------------------------------------------------


def case_analyze_line_fields():
    """报告字段齐全，且长度口径与 normalize_line 一致。"""
    raw = "\u3000\u3000他走进屋子。。。  "
    info = analyze_line(raw)
    for key in (
        "raw_len", "clean_len", "indented", "indent_width", "has_zero_width",
        "zero_width_removed", "zero_width_kept", "trailing_space",
        "trailing_space_len", "blank", "ad_like", "ad_keywords",
        "has_url", "ends_terminal", "changed",
    ):
        assert key in info, key
    assert info["raw_len"] == len(raw)
    assert info["clean_len"] == len(normalize_line(raw).text)
    assert info["indented"] is True
    assert info["trailing_space"] is True and info["trailing_space_len"] == 2
    assert info["blank"] is False
    assert info["ends_terminal"] is True   # 归成 "……" 后结尾是省略号


def case_analyze_line_ad():
    """广告特征复用 diagnose 的 AD_KEYWORDS 与 _URL，不另立一套。"""
    info = analyze_line("下载本书请访问 www.example.com")
    assert info["ad_like"] is True
    assert info["has_url"] is True
    assert "www." in info["ad_keywords"]
    # 被零宽劈开的关键词也要认出来
    assert "微信" in analyze_line("微\u200b信")["ad_keywords"]
    # 正文不该被误判成广告
    assert analyze_line("他走进屋子，坐下。")["ad_like"] is False


def case_analyze_line_terminal():
    """句末标点判定复用 diagnose.TERMINAL。"""
    for tail in diagnose.TERMINAL:
        info = analyze_line("他说" + tail)
        assert info["ends_terminal"] is True, (tail, info)
    assert analyze_line("他说")["ends_terminal"] is False
    assert analyze_line("　　")["blank"] is True


def case_analyze_line_zero_width():
    """零宽特征要分别给出"删了几个"和"保留了几个"。"""
    info = analyze_line("\ufeff微\u200b信")
    assert info["has_zero_width"] is True
    assert info["zero_width_removed"] == 2
    assert info["zero_width_kept"] == 0
    plain = analyze_line("微信")
    assert plain["has_zero_width"] is False and plain["zero_width_removed"] == 0


# ---------------------------------------------------------------------------
# 八、规矩：复用常量、不 print、不碰文件
# ---------------------------------------------------------------------------


def case_reuses_diagnose_constants():
    """必须是"同一个对象"，不是复制一份值 —— 否则报告与清洗会有两套标准。"""
    assert normalize.AD_KEYWORDS is diagnose.AD_KEYWORDS
    assert normalize.TERMINAL is diagnose.TERMINAL
    assert normalize._URL is diagnose._URL
    assert normalize.squeeze is diagnose.squeeze


def case_module_is_pure():
    """业务函数不许 print，模块不许读写文件。"""
    src = (ROOT / "huoban" / "normalize.py").read_text(encoding="utf-8")
    assert "print(" not in src, "normalize.py 里出现了 print("
    assert "open(" not in src, "normalize.py 里出现了 open("


def case_accepts_odd_input():
    """边界输入不崩：空串、纯空白、单个字符、超长行。"""
    for raw in ("", " ", "\u3000", "。", "a", "他" * 5000):
        assert isinstance(strip_zero_width(raw), str)
        assert isinstance(normalize_punctuation(raw), str)
        assert isinstance(normalize_quotes(raw), str)
        assert isinstance(clean_line(raw).text, str)
        assert isinstance(analyze_line(raw), dict)


# ---------------------------------------------------------------------------


def main() -> int:
    cases = [v for k, v in sorted(globals().items()) if k.startswith("case_")]
    failed = 0
    for case in cases:
        try:
            case()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {case.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 测试脚本要兜住意外异常
            failed += 1
            print(f"FAIL {case.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {case.__name__}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
