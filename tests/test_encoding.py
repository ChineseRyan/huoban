"""huoban/encoding.py 的用例：编码候选池与解码失败兜底。

约定沿用 tests/test_clean.py：case_* 函数 + _CASES 列表 + main()。

背景（REPORT.md 缺陷1，高）：UTF-16LE 无 BOM 的 TXT 16/16 本全解错。
根因是 `_CANDIDATES` 没有 utf-16 家族，gb18030 硬解（score=-0.241，
strict_ok=False）仍被当最高分选中 → 整本乱码且静默伪装成「无章节书」。
修法：候选池加 utf-16-le / utf-16-be，靠**常用字占比打分**自然胜出；
判据绝不能用 strict_ok（任意偶数字节流按 UTF-16LE 几乎总能严格成功）。
另加兜底：全部候选严格解码都失败 → note 里明确「解码失败」，不得静默。

全部用例用合成假数据，不含任何真书正文。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban import encoding as enc  # noqa: E402
from huoban.encoding import decode, detect  # noqa: E402

# 合成简体正文（高频常用字为主，非任何真书内容）
SIMP = (
    "我们这个国家的人说，学习的时候会发现，后来为了一个问题还没解决。"
    "他们对着窗户说话，觉得这样比较好，因为风从那里吹过来。"
) * 40

# 合成"生僻字"文本：全是 U+4E00-9FFF 但刻意避开 COMMON_CHARS，
# 用来构造 utf-16-le 严格解码成功、常用字占比却为 0 的最坏情形
RARE = "鑫骉淼垚犇虢翀昶燚劼旻昱睿赟玥珩麤龘靐齉囍燊骊昉珺翊旸煜琛"


def _top(raw: bytes):
    return detect(raw)[0]


def case_utf16le_nobom_detected() -> None:
    """缺陷1 主用例：UTF-16LE 无 BOM 的中文书必须判到 utf-16-le。

    修复前这里选 gb18030（score=-0.241、strict=False），整本乱码。
    """
    raw = SIMP.encode("utf-16-le")
    assert len(raw) % 2 == 0
    top = _top(raw)
    assert top.encoding == "utf-16-le", (top.encoding, top.score)
    assert top.strict_ok, top.note
    assert top.score > 0.5, top.score


def case_utf16le_nobom_decode_roundtrip() -> None:
    """探测对了还不够，decode 出来的必须逐字等于原文（不是替换字符）。"""
    raw = SIMP.encode("utf-16-le")
    assert decode(raw) == SIMP
    # 乱码的直接特征：出现 U+FFFD 或汉字却几乎不是常用字
    text = decode(raw)
    assert "\ufffd" not in text
    cjk = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    common = sum(1 for ch in text if ch in enc.COMMON_CHARS)
    assert cjk and common / cjk > 0.5, (cjk, common)


def case_utf16be_nobom_detected() -> None:
    """同族对称用例：UTF-16BE 无 BOM 也要判到 utf-16-be。"""
    top = _top(SIMP.encode("utf-16-be"))
    assert top.encoding == "utf-16-be", (top.encoding, top.score)
    assert top.strict_ok


def case_gb18030_even_bytes_not_stolen() -> None:
    """最大风险回归：GB18030 书不能被 utf-16-le 抢走。

    常规场景：正常简体 GB18030 文本（偶数字节，utf-16-le 也能严格解出
    一些汉字），必须仍选 gb18030。
    """
    raw = SIMP.encode("gb18030")
    if len(raw) % 2:
        raw = raw[:-1]  # 保持"偶数字节"这个最危险的前提
    # 前提检查：utf-16-le 对这块字节"能不能严格解"都无所谓，判据只看分数
    top = _top(raw)
    assert top.encoding == "gb18030", (top.encoding, top.score)
    assert top.strict_ok

    # 最坏场景：生僻字 GB18030，utf-16-le 严格解码成功但常用字占比为 0。
    # 找一块"utf-16-le 严格成功"的偶数字节流（strict_ok 绝不能当判据，
    # 这里专门构造它成立的情形来钉死这条红线）。
    found = 0
    for n in (8, 16, 24, 32, 40, 64, 80, 100):
        text = "，".join((RARE + SIMP[: n % 20]) * (n // 8))
        r = text.encode("gb18030")
        if len(r) % 2:
            r = r[:-1]
        try:
            u = r.decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        found += 1
        cands = {c.encoding: c for c in detect(r)}
        assert cands["utf-16-le"].strict_ok, r"前提：utf-16-le 严格解码成功"
        assert cands["utf-16-le"].score <= 0.0, cands["utf-16-le"].as_dict()
        assert detect(r)[0].encoding == "gb18030", [
            c.as_dict() for c in detect(r)[:3]
        ]
    assert found >= 3, f"最坏场景构造不足：{found}"


def case_big5_not_stolen() -> None:
    """繁体 Big5 书同样不许被抢走（换池验证里 big5 得分 0.312）。"""
    trad = (
        "我們這個國家的人說，學習的時候會發現，後來為了一個問題還沒解決。"
        "他們對著窗戶說話，覺得這樣比較好，因為風從那裡吹過來。"
        "時間過得很快，當我們回頭看，發現這些事情都已經過去了。"
    ) * 40
    top = _top(trad.encode("big5"))
    assert top.encoding == "big5", (top.encoding, top.score)
    assert top.strict_ok


def case_utf16_bom_still_decisive() -> None:
    """UTF-16 有 BOM 仍走 _BOM_DECISIVE 单候选路径，不许被池内新成员干扰。"""
    for bom, family in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        raw = bom + SIMP.encode(family)
        cands = detect(raw)
        assert len(cands) == 1, [c.encoding for c in cands]
        assert cands[0].encoding == "utf-16", cands[0].encoding
        assert cands[0].strict_ok
        assert "检出 BOM" in cands[0].note, cands[0].note
        # "utf-16" 解码自带 BOM 剥离，解出来必须正好是原文
        assert decode(raw) == SIMP, (bom, family)


def case_utf8_bom_prefers_utf8sig() -> None:
    """UTF-8 BOM 依旧判 utf-8-sig；ASCII 打平时也不许落到 utf-16-le。"""
    raw = b"\xef\xbb\xbf" + SIMP.encode("utf-8")
    top = _top(raw)
    assert top.encoding == "utf-8-sig", top.encoding
    ascii_raw = b"Chapter 1\nhello world\n" * 20
    assert _top(ascii_raw).encoding == "utf-8", _top(ascii_raw).encoding


def case_odd_length_utf16le_survives() -> None:
    """奇数字节的真 UTF-16LE（被截断）：utf-16-le 严格失败，但按替换字符
    评分仍应胜出 —— 兜底 note 不许把它误标成"解码失败"。"""
    raw = SIMP.encode("utf-16-le")[:-1]
    cands = {c.encoding: c for c in detect(raw)}
    assert not cands["utf-16-le"].strict_ok
    top = detect(raw)[0]
    assert top.encoding == "utf-16-le", (top.encoding, top.score)
    assert "判定为解码失败" not in top.note, top.note


def case_all_strict_fail_warns() -> None:
    """全部候选严格解码都失败 → 最高分候选的 note 必须写明解码失败，
    这是"不得静默返回乱码"的兜底告警。"""
    junk = bytes(range(256)) * 8 + bytes([0x80, 0x81, 0xFE, 0xFF, 0x00, 0x01]) * 13
    cands = detect(junk)
    assert cands, "候选不能为空"
    assert all(not c.strict_ok for c in cands), [c.as_dict() for c in cands]
    top = cands[0]
    assert "判定为解码失败" in top.note, top.note


def case_bom_garbage_warns() -> None:
    """伪 BOM 的二进制垃圾：能严格解码也必须给出结论级警告（review 中8）。

    BOM 分支原先只在「严格失败」或「没有中日韩字符」时加 note。最阴的一种
    垃圾是：文件开头 FF FE，后面是避开代理区、随手可用的码位 —— 按 utf-16
    能严格解开，cjk_ratio 也不为 0（零星落在 U+4E00-9FFF 里），于是报告上
    看起来一切正常，实际是一本乱码书。判据是常用字占比：真书实测最低 0.473，
    这类垃圾只有 0.005（backups/corpus-20261025/ 下的标定样本）。
    """
    # 避开 U+4E00-9FFF 之外的可用码位：解出来 cjk 有零星命中，但都不常用。
    junk_chars = "".join(chr(c) for c in range(0x4E00, 0x6000, 7))
    raw = b"\xff\xfe" + junk_chars.encode("utf-16-le")
    cand = _top(raw)
    assert cand.strict_ok, "这组构造应当能严格解码"
    assert "疑似乱码或非文本文件" in cand.note, cand.note

    # 反例一：真中文 UTF-16 有 BOM → 不许出现这句（防误伤）。
    healthy = b"\xff\xfe" + SIMP.encode("utf-16-le")
    assert "疑似乱码" not in _top(healthy).note, _top(healthy).note

    # 反例二：真中文被截断（strict 失败但内容是真字）→ 也不许判成垃圾。
    chopped = b"\xff\xfe" + SIMP.encode("utf-16-le")[:-3]
    assert "疑似乱码" not in _top(chopped).note, _top(chopped).note


def case_clean_paths_still_pass() -> None:
    """三种健康编码不许出现任何"失败"字样 note（防兜底误伤）。"""
    for tag, raw in (
        ("utf8", SIMP.encode("utf-8")),
        ("gb18030", SIMP.encode("gb18030")),
        ("utf16le-nobom", SIMP.encode("utf-16-le")),
    ):
        top = _top(raw)
        assert "失败" not in top.note, (tag, top.note)


def case_detect_dict_fields_stable() -> None:
    """Candidate.as_dict 字段集是 diagnose/batch_check 的消费接口，不许漂移。"""
    d = _top(SIMP.encode("utf-16-le")).as_dict()
    assert set(d) == {
        "encoding", "score", "cjk_ratio", "common_ratio",
        "replacement_ratio", "strict_ok", "note",
    }, sorted(d)


_CASES = [
    case_utf16le_nobom_detected,
    case_utf16le_nobom_decode_roundtrip,
    case_utf16be_nobom_detected,
    case_gb18030_even_bytes_not_stolen,
    case_big5_not_stolen,
    case_utf16_bom_still_decisive,
    case_bom_garbage_warns,
    case_utf8_bom_prefers_utf8sig,
    case_odd_length_utf16le_survives,
    case_all_strict_fail_warns,
    case_clean_paths_still_pass,
    case_detect_dict_fields_stable,
]


def test_all_cases() -> None:
    for fn in _CASES:
        fn()


def main() -> int:
    failed = 0
    for fn in _CASES:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"{len(_CASES) - failed}/{len(_CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
