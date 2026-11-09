"""命令行层：参数读入、错误出口、返回码。

为什么单独一个脚本：cli.py 是唯一面向人的一层，它的契约是"出错要说人话、
返回码要能脚本化"。这层原先零测试覆盖 —— review 就是在这一层抓到
`--adjust '[1,2]'` 报「读不到 adjust 文件」这种把人引到错误方向去的说法，
以及失败路径和清洗层各说各话（一个 exit 1、一个 exit 2）。

测的是**契约**，不是措辞：断言里只认"有没有报错、返回码是几、输出目录有没有
被创建"这类稳定事实，文案只取一两个关键片段。

全部在进程内调用 main(argv)，不起子进程：起子进程要处理路径、编码、缓冲，
测出来的多半是操作系统的脾气，不是 cli 的行为。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 起别名：本文件自己也有一个 main()（跑用例的），不别名会被盖掉。
from huoban.cli import _load_adjust, main as cli_main  # noqa: E402

# 一份能正常清洗的小样本：第一章 + 三段正文 + 一条整行广告（第 5 行）
_SAMPLE = (
    "第一章 开始\n"
    "\n"
    "他在山里走了三天。\n"
    "\n"
    "天才一秒记住本站\n"
    "\n"
    "终于看见了灯火。\n"
)


def _tmp_text(name: str = "book.txt", text: str = _SAMPLE) -> Path:
    d = Path(tempfile.mkdtemp(prefix="huoban-cli-"))
    p = d / name
    p.write_bytes(text.encode("utf-8"))
    return p


def _run(argv: list[str]) -> tuple[int, str, str]:
    """跑一次 cli，返回 (返回码, 标准输出, 标准错误)。

    两条流都接住：`--json` 走标准输出（给机器读），人看的摘要走标准错误 ——
    这是 cli 的分工，测试跟着分开接，不然跑测试时满屏人话。
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(argv)
    return code, out.getvalue(), err.getvalue()


def case_load_adjust_none() -> None:
    """没给 --adjust：返回 None，不是空字典。

    None 和 {} 在清洗层是同一个意思，但 None 能让"用户到底给没给"这件事
    在调用链上一直可分辨 —— 报告里的 adjust 段据此决定要不要出现。
    """
    assert _load_adjust(None) is None
    assert _load_adjust("") is None


def case_load_adjust_json_text() -> None:
    """直接贴 JSON 文本。"""
    got = _load_adjust('{"protect_lines": [3], "merge_breaks": [4]}')
    assert got == {"protect_lines": [3], "merge_breaks": [4]}, got


def case_load_adjust_bracket_text_is_not_a_path() -> None:
    """`[1,2]` 是"想贴 JSON 却贴错形状"，不是文件路径。

    当路径处理会报「读不到 adjust 文件：[1,2]」—— 用户手里明明没有这个文件，
    这句话只会把人引到"文件放哪了"上去，而真正的问题是形状。
    """
    try:
        _load_adjust("[1,2]")
    except ValueError as exc:
        msg = str(exc)
    else:
        raise AssertionError("[1,2] 应该报错")
    assert "JSON 对象" in msg, msg
    assert "读不到" not in msg, msg


def case_load_adjust_from_file() -> None:
    """给文件路径：读出来当 adjust。

    复核界面把勾选状态写成临时文件丢过来，这是最常见的用法。
    """
    d = Path(tempfile.mkdtemp(prefix="huoban-cli-"))
    p = d / "adjust.json"
    p.write_bytes(json.dumps({"protect_ranges": [[2, 3]]}).encode("utf-8"))
    got = _load_adjust(str(p))
    assert got == {"protect_ranges": [[2, 3]]}, got


def case_load_adjust_bad_json_raises() -> None:
    """半截 JSON：报"不是合法 JSON"，并指出第几行。"""
    try:
        _load_adjust('{"protect_lines": [3,}')
    except ValueError as exc:
        msg = str(exc)
    else:
        raise AssertionError("坏 JSON 应该报错")
    assert "不是合法 JSON" in msg, msg


def case_load_adjust_missing_file_raises() -> None:
    """路径不存在：报"读不到"，而不是当空 adjust 静默放过。"""
    try:
        _load_adjust("G:/no/such/adjust-file-4f2a.json")
    except ValueError as exc:
        msg = str(exc)
    else:
        raise AssertionError("不存在的文件应该报错")
    assert "读不到 adjust 文件" in msg, msg


def case_clean_bad_adjust_exits_2_and_writes_nothing() -> None:
    """形状错：返回码 2、同一句话、**不创建输出目录**。

    为什么必须不建目录：用户看到命令失败会改完重跑，目录里留半份上一次的
    产物，他下次分不清哪份是新的。宁可什么都不留。
    """
    src = _tmp_text()
    out = src.parent / "out"
    code, stdout, _ = _run(["clean", str(src), "--adjust", '{"bogus": 1}', "-o", str(out)])
    assert code == 2, (code, stdout)
    assert "人工调整有问题" in stdout, stdout
    assert not out.exists(), f"失败时不该创建输出目录：{out}"


def case_clean_adjust_protects_line() -> None:
    """正常给 adjust：返回码 0，报告里账记得对。

    第 5 行是整行广告；保护它之后它该留下、账该记在 protected.ad 上。
    """
    src = _tmp_text()
    out = src.parent / "out"
    code, stdout, _ = _run(
        ["clean", str(src), "--adjust", '{"protect_lines": [5]}', "-o", str(out), "--json"]
    )
    assert code == 0, (code, stdout)
    report = json.loads(stdout)
    adj = report["adjust"]
    assert adj["protected"]["ad"] == 1, adj
    assert adj["protected"]["lines"] == 1, adj
    assert report["dropped"]["ad"] == 0, report["dropped"]
    assert (out / "book.json").exists(), list(out.iterdir())


def case_clean_out_of_range_adjust_warns_but_succeeds() -> None:
    """行号越界是**数据**错：忽略 + 警告 + 返回码 0，不是整条命令失败。

    结构错（键名不对、类型不对）才该让命令失败 —— 那种错是代码或界面写错了；
    行号对不上只说明这份 adjust 不是这一本书的，清洗本身没问题。
    """
    src = _tmp_text()
    out = src.parent / "out"
    code, stdout, _ = _run(
        ["clean", str(src), "--adjust", '{"protect_lines": [99999]}', "-o", str(out), "--json"]
    )
    assert code == 0, (code, stdout)
    report = json.loads(stdout)
    assert len(report["adjust"]["ignored"]) == 1, report["adjust"]
    assert any("没生效" in w for w in report["warnings"]), report["warnings"]


def case_epub_book_json_ignores_adjust_with_hint() -> None:
    """`--book-json` 跳过了清洗，adjust 就没地方生效 —— 必须说一句。

    静默忽略最坏：用户以为勾选生效了，打开电子书发现该删的还在。
    """
    src = _tmp_text()
    out = src.parent / "out"
    code, _, _ = _run(["clean", str(src), "-o", str(out)])
    assert code == 0, code
    book_json = out / "book.json"
    assert book_json.exists(), list(out.iterdir())

    epub_out = src.parent / "epub"
    code, stdout, _ = _run(
        [
            "epub",
            str(src),
            "--book-json",
            str(book_json),
            "--adjust",
            '{"protect_lines": [5]}',
            "-o",
            str(epub_out),
        ]
    )
    assert code == 0, (code, stdout)
    assert "只在清洗那一步生效" in stdout, stdout


def case_epub_back_cover_enters_package() -> None:
    """`--back-cover` 要真透传到打包层：产物里必须有封底页 OEBPS/Text/back.xhtml。

    为什么不能只断言"返回码 0"：参数没接住时打包照样成功，只是悄悄少了这一页
    （封底图不可用的降级方向就是不产页 + 一条 warning），命令行的输出里看不出
    区别。只有翻产物里的条目名才能证明这条线是通的。
    """
    src = _tmp_text()
    back = src.parent / "fengdi.png"
    # 只带文件头与 IHDR 的最小 PNG：打包层只验文件头魔数，够用。
    back.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + (600).to_bytes(4, "big")
        + (800).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )
    out = src.parent / "out-back"
    code, stdout, stderr = _run(["epub", str(src), "--back-cover", str(back), "-o", str(out)])
    both = stdout + stderr
    assert code == 0, (code, both)
    with zipfile.ZipFile(out / "book.epub") as zf:
        names = zf.namelist()
    assert "OEBPS/Text/back.xhtml" in names, names


def case_bad_adjust_exits_2_on_both_subcommands() -> None:
    """三类"读不进来"的 adjust（坏 JSON / 顶层是 list / 文件不存在）在
    `clean` 与 `epub` 上都必须是**返回码 2 + 一句人话**。

    为什么单独测返回码而不是只测 `_load_adjust` 抛 ValueError：抛异常是内部
    事实，用户看到的是 cli 层接住之后打印的那句和退出码。review 的变异测试
    把顶层 catch 的 `return 2` 改成 `return 1`、把 `except ValueError` 改成
    `except KeyError`（后者会连 traceback 一起漏出来），全套测试照样绿 ——
    说明之前只测了"会抛"，没测"抛完之后用户看到什么"。返回码是 GUI 判断
    "这条命令失败了、该显示哪句"的唯一依据，退化成 1 或者带 traceback
    都是把这层契约丢了。
    """
    bad_inputs = [
        '{"protect_lines": [3,',  # 半截 JSON
        "[1,2]",  # 顶层是 list，不是对象
        "G:/no/such/adjust-file-9c31.json",  # 文件不存在
    ]
    for n, raw in enumerate(bad_inputs):
        src = _tmp_text()
        for cmd in ("clean", "epub"):
            out = src.parent / f"out-{cmd}-{n}"
            code, stdout, stderr = _run(
                [cmd, str(src), "--adjust", raw, "-o", str(out)]
            )
            both = stdout + stderr
            assert code == 2, (cmd, raw, code, both)
            assert "人工调整有问题" in both, (cmd, raw, both)
            assert "Traceback" not in both, (cmd, raw, both)


def case_epub_clean_time_adjust_error_exits_2() -> None:
    """epub 走清洗那条路时，adjust 的**结构错**也必须是 2、不带 traceback。

    和上一条的区别：上面三类错在"读文件 / 解析 JSON"就被拦住了；这条要走到
    `clean_file(...)` 里面才由清洗层抛出来。两处 except 是两个不同的落点，
    少测一处，那一处的返回码就能被改坏而没人发现（review 的变异 c02 正是
    把这里的 `except ValueError` 改成 `except KeyError`）。
    """
    src = _tmp_text()
    out = src.parent / "out-epub-struct"
    code, stdout, stderr = _run(
        ["epub", str(src), "--adjust", '{"bogus": 1}', "-o", str(out)]
    )
    both = stdout + stderr
    assert code == 2, (code, both)
    assert "人工调整有问题" in both, both
    assert "不认识的键" in both, both
    assert "Traceback" not in both, both


_CASES = [
    case_load_adjust_none,
    case_load_adjust_json_text,
    case_load_adjust_bracket_text_is_not_a_path,
    case_load_adjust_from_file,
    case_load_adjust_bad_json_raises,
    case_load_adjust_missing_file_raises,
    case_clean_bad_adjust_exits_2_and_writes_nothing,
    case_clean_adjust_protects_line,
    case_clean_out_of_range_adjust_warns_but_succeeds,
    case_epub_book_json_ignores_adjust_with_hint,
    case_epub_back_cover_enters_package,
    case_bad_adjust_exits_2_on_both_subcommands,
    case_epub_clean_time_adjust_error_exits_2,
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
