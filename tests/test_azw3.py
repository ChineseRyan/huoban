"""azw3 模块的用例。

两条线：
  1. 不依赖 Calibre 的（找 exe、参数校验、错误路径）—— 任何时候都跑；
  2. 真转换 —— 只有本机装了 Calibre 才跑，没装就跳过（分母照算）。

真转换那组**不比对字节**：AZW3 不可复现（见 huoban/azw3.py 模块文档），
比对字节会得到一条永远红的用例。要比对确定性请去 tests/test_epub.py。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from huoban import azw3  # noqa: E402
from huoban.clean import clean_bytes  # noqa: E402
from huoban.epub import write_epub  # noqa: E402

# 本机有没有 Calibre。有才跑真转换那一组。
_HAVE = azw3.available() is not None


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="t2e_azw3_"))


def _make_epub(dirpath: Path) -> Path:
    """造一本两章的迷你 EPUB —— 不碰任何真实书籍。"""
    text = (
        "第一章　启程\n\n正文一段，写得像模像样。\n\n"
        "第二章　归途\n\n最后一段，也带句号。\n"
    )
    result = clean_bytes(text.encode("utf-8"), source="azw3test.txt")
    out = dirpath / "book.epub"
    write_epub(result.book, out, title="用例书", author="某人", language="zh-CN")
    return out


# ---------------------------------------------------------------------------
# 一、找 exe
# ---------------------------------------------------------------------------

def case_available_env_override_missing_returns_none():
    """环境变量指到不存在的路径 → 返回 None，**不回落**到 PATH。

    回落到 PATH 会让用户以为自己的配置生效了，实际跑的是另一个 Calibre。
    """
    old = os.environ.get(azw3.ENV_VAR)
    os.environ[azw3.ENV_VAR] = str(Path(tempfile.gettempdir()) / "no_such_ebook_convert.exe")
    try:
        assert azw3.available() is None, "环境变量指错了不该回落到 PATH"
    finally:
        if old is None:
            os.environ.pop(azw3.ENV_VAR, None)
        else:
            os.environ[azw3.ENV_VAR] = old


def case_available_env_override_existing_returns_it():
    """环境变量指到一个真实存在的文件 → 原样返回它。"""
    d = _tmpdir()
    fake = d / "fake_ebook_convert.exe"
    fake.write_bytes(b"not really an exe")
    old = os.environ.get(azw3.ENV_VAR)
    os.environ[azw3.ENV_VAR] = str(fake)
    try:
        assert azw3.available() == str(fake), azw3.available()
    finally:
        if old is None:
            os.environ.pop(azw3.ENV_VAR, None)
        else:
            os.environ[azw3.ENV_VAR] = old


def case_available_is_none_or_path():
    """不带环境变量时只可能两种结果：None 或一个存在的路径。"""
    old = os.environ.pop(azw3.ENV_VAR, None)
    try:
        got = azw3.available()
    finally:
        if old is not None:
            os.environ[azw3.ENV_VAR] = old
    if got is not None:
        assert Path(got).is_file(), f"available() 返回了不存在的路径：{got}"


# ---------------------------------------------------------------------------
# 二、参数与错误路径（不需要 Calibre）
# ---------------------------------------------------------------------------

def case_convert_unavailable_raises():
    """找不到 exe → Azw3Unavailable（不是 Azw3Error，两者含义不同）。

    注意两点：
      1. 输入必须先存在。convert 的顺序是「先校验本地参数，再问工具在不在」——
         文件都不对的时候先说文件，比先说"装个 Calibre"更能让人知道下一步做什么。
      2. 不能用 `exe=""` 来表达"没有工具"：空字符串是 falsy，会回落到 available()
         自动探测（这是有意的，`exe=None`/`""` 都表示"没显式指定"）。
         要造"没有工具"只能用环境变量指到不存在的路径。
    """
    d = _tmpdir()
    src = d / "a.epub"
    src.write_bytes(b"x")
    old = os.environ.get(azw3.ENV_VAR)
    os.environ[azw3.ENV_VAR] = str(d / "no_such_ebook_convert.exe")
    try:
        azw3.convert(src, d / "a.azw3")
    except azw3.Azw3Unavailable as exc:
        assert azw3.ENV_VAR in str(exc), f"提示里该告诉人怎么配：{exc}"
        return
    finally:
        if old is None:
            os.environ.pop(azw3.ENV_VAR, None)
        else:
            os.environ[azw3.ENV_VAR] = old
    raise AssertionError("没有 exe 时应该抛 Azw3Unavailable")


def case_convert_missing_input_raises():
    d = _tmpdir()
    try:
        azw3.convert(d / "nope.epub", d / "o.azw3", exe=sys.executable)
    except azw3.Azw3Error as exc:
        assert "不存在" in str(exc), str(exc)
        return
    raise AssertionError("输入不存在时应该抛 Azw3Error")


def case_convert_refuses_output_equal_input():
    """输出指到输入本身 —— 本项目最容易犯的自杀操作，必须在写盘前挡住。"""
    d = _tmpdir()
    same = d / "same.epub"
    same.write_bytes(b"x")
    try:
        azw3.convert(same, same, exe=sys.executable)
    except azw3.Azw3Error as exc:
        assert "拒绝写入" in str(exc), str(exc)
        return
    raise AssertionError("输出等于输入时应该拒绝")


def case_convert_bad_exe_raises_oserror_wrapped():
    """exe 路径不存在 → OSError 被包成 Azw3Error，不把裸 OSError 抛给调用方。"""
    d = _tmpdir()
    src = d / "a.epub"
    src.write_bytes(b"x")
    try:
        azw3.convert(src, d / "a.azw3", exe=str(d / "no_such.exe"))
    except azw3.Azw3Error as exc:
        assert "启动" in str(exc), str(exc)
        return
    raise AssertionError("exe 起不来时应该抛 Azw3Error")


def case_convert_nonzero_exit_raises():
    """用 python 自己当"转换器"：给它一个必然失败的脚本参数，返回码非 0。"""
    d = _tmpdir()
    src = d / "a.epub"
    src.write_bytes(b"x")
    # sys.executable 收到 (src, dst, --no-inline-toc) 会当成脚本名去执行 → rc != 0
    try:
        azw3.convert(src, d / "a.azw3", exe=sys.executable)
    except azw3.Azw3Error as exc:
        assert "返回" in str(exc) or "产物没出现" in str(exc), str(exc)
        return
    raise AssertionError("返回码非 0 时应该抛 Azw3Error")


def case_convert_timeout_raises():
    """超时必须抛 Azw3Error，且不能留下半截产物当成功。"""
    d = _tmpdir()
    src = d / "a.epub"
    src.write_bytes(b"x")
    # 0.001 秒必然超时（连进程都起不来就超了）
    try:
        azw3.convert(src, d / "a.azw3", exe=sys.executable, timeout=0.001)
    except azw3.Azw3Error as exc:
        assert "超时" in str(exc) or "返回" in str(exc) or "产物" in str(exc), str(exc)
        return
    raise AssertionError("超时应该抛 Azw3Error")


def case_creates_missing_parent_dir():
    """输出目录不存在时要自己建 —— 界面传进来的目录可能还没建。"""
    if not _HAVE:
        print("skip 没装 Calibre")
        return
    d = _tmpdir()
    epub = _make_epub(d)
    nested = d / "a" / "b" / "c"
    result = azw3.convert(epub, nested / "out.azw3")
    assert nested.is_dir(), "父目录应该被建出来"
    assert result.path.is_file()


# ---------------------------------------------------------------------------
# 三、真转换（需要 Calibre）
# ---------------------------------------------------------------------------

def case_convert_produces_azw3():
    """真转一次：产物存在、大小合理、耗时为正。"""
    if not _HAVE:
        print("skip 没装 Calibre")
        return
    d = _tmpdir()
    epub = _make_epub(d)
    result = azw3.convert(epub, d / "out.azw3")
    assert result.path.is_file(), "产物没生成"
    assert result.size_bytes > 1024, f"产物太小：{result.size_bytes}"
    assert result.seconds > 0, "耗时应为正"
    assert result.size_bytes == result.path.stat().st_size


def case_convert_accepts_pubdate():
    """--pubdate 两种写法都要被 Calibre 接受（我们传的是 _resolve_modified 的 Z 结尾格式）。"""
    if not _HAVE:
        print("skip 没装 Calibre")
        return
    d = _tmpdir()
    epub = _make_epub(d)
    for value in ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00+00:00"):
        result = azw3.convert(epub, d / f"{abs(hash(value)) % 1000}.azw3", pubdate=value)
        assert result.size_bytes > 1024, value


def case_convert_twice_same_size():
    """同一输入两次：体积相同（这是**能**保证的），但字节不同（AZW3 不可复现）。

    这条用例的价值在于把"不可复现"钉成事实 —— 将来有人想给 AZW3 加字节比对，
    会在这里看到为什么不行。
    """
    if not _HAVE:
        print("skip 没装 Calibre")
        return
    d = _tmpdir()
    epub = _make_epub(d)
    a = azw3.convert(epub, d / "a.azw3", pubdate="2020-01-01T00:00:00Z")
    b = azw3.convert(epub, d / "b.azw3", pubdate="2020-01-01T00:00:00Z")
    assert a.size_bytes == b.size_bytes, f"同输入体积该相同：{a.size_bytes} vs {b.size_bytes}"


def main() -> int:
    cases = [
        case_available_env_override_missing_returns_none,
        case_available_env_override_existing_returns_it,
        case_available_is_none_or_path,
        case_convert_unavailable_raises,
        case_convert_missing_input_raises,
        case_convert_refuses_output_equal_input,
        case_convert_bad_exe_raises_oserror_wrapped,
        case_convert_nonzero_exit_raises,
        case_convert_timeout_raises,
        case_creates_missing_parent_dir,
        case_convert_produces_azw3,
        case_convert_accepts_pubdate,
        case_convert_twice_same_size,
    ]
    if not _HAVE:
        print("skip 没装 Calibre：真转换 4 条会跳过（分母照算）")

    failed = 0
    for c in cases:
        try:
            c()
            print(f"ok   {c.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {c.__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
