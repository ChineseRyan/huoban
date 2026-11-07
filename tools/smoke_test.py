"""冒烟测试：用合成样本覆盖几种"典型坏样本"，断言工具能做出正确的事。

M1 部分只断言"能否发现问题"，M2 的 clean 部分断言"产物对不对 + 输入动没动"。
不断言报告措辞 —— 措辞会变，判据不该变。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.diagnose import diagnose  # noqa: E402
from huoban.encoding import best, decode  # noqa: E402


def _tmp(name: str, text: str, encoding: str = "utf-8") -> Path:
    d = Path(tempfile.mkdtemp(prefix="huoban-"))
    p = d / name
    p.write_bytes(text.encode(encoding))
    return p


def case_plain() -> None:
    """干净文本：不该报出一堆问题。"""
    text = "第一章 开始\n\n他在山里走了三天。\n终于看见了灯火。\n"
    r = diagnose(_tmp("clean.txt", text))
    assert r["chapters"]["count"] >= 1, r["chapters"]
    assert r["ads"]["whole_line_count"] == 0, r["ads"]
    assert r["single_line_book"]["suspected"] is False


def case_one_line_book() -> None:
    """整本书糊成一行：必须能判出来。"""
    # 真的把它拼成一行 —— 模拟网页下载没分段的产物
    sentence = "他还记得那一天的情景，风很大，吹得窗棂一直在响。"
    body = "".join(f"第{i}章 标题　{sentence * 8}" for i in range(1, 30))
    r = diagnose(_tmp("oneline.txt", body))
    assert r["single_line_book"]["suspected"] is True, r["single_line_book"]
    assert r["single_line_book"]["long_line_count"] >= 1, r["single_line_book"]

    # 另一种形态：行不算超长，但一行里塞了多个章节标记
    multi = "第1章 甲 他走进屋子。第2章 乙 他坐了下来。第3章 丙 天亮了。"
    r2 = diagnose(_tmp("multich.txt", multi))
    assert r2["single_line_book"]["lines_with_multiple_chapters"] >= 1, r2["single_line_book"]


def case_ads() -> None:
    """整行广告 + 行内广告 + 重复水印都要各自认出来。"""
    lines = []
    for i in range(1, 11):
        lines.append(f"第{i}章 测试")
        lines.append("他慢慢走到窗前，外面正在下雨。")
        lines.append("下载本书请访问 www.example.com 手机用户请浏览本站")
        lines.append("第一章内容未完，点击下一页 >>> www.example.com")
    lines.append("他走到门口。想看更多请加QQ群123456789")
    r = diagnose(_tmp("ads.txt", "\n".join(lines)))
    assert r["ads"]["whole_line_count"] >= 8, r["ads"]["whole_line_count"]
    assert r["ads"]["inline_count"] >= 1, r["ads"]["inline_count"]
    assert any(x["ad_like"] for x in r["repeated_lines"]), r["repeated_lines"]


def case_encoding() -> None:
    """GB18030 与 UTF-8 都必须判对。"""
    text = "第一章 开始\n\n他站在城门口，望着远处的灯火。风从北面吹来。\n" * 5
    for enc in ("utf-8", "gb18030"):
        p = _tmp(f"enc_{enc}.txt", text, enc)
        raw = p.read_bytes()
        got = best(raw).encoding
        assert got in ("utf-8", "gb18030"), got
        if enc == "gb18030":
            assert got == "gb18030", (enc, got)
        else:
            assert got == "utf-8", (enc, got)
        assert "他站在城门口" in decode(raw)


def case_gbk_bom_utf8() -> None:
    """带 BOM 的 UTF-8 要认成 utf-8-sig。"""
    p = _tmp("bom.txt", "第一章 开始\n正文内容在这里。\n" * 3)
    raw = b"\xef\xbb\xbf" + p.read_bytes()
    assert best(raw).encoding == "utf-8-sig", best(raw).encoding


def case_toc() -> None:
    """开头一簇目录要判出来；散落的章标题不能误判。"""
    toc = ["目录"] + [f"第{i}章 标题{i}" for i in range(1, 13)]
    body = []
    for i in range(1, 13):
        body.append(f"第{i}章 标题{i}")
        body.append("正文正文正文正文正文正文正文正文正文正文。")
        body.append("")
    r_toc = diagnose(_tmp("toc.txt", "\n".join(toc + body)))
    assert r_toc["toc"]["suspected"] is True, r_toc["toc"]

    r_none = diagnose(_tmp("notoc.txt", "\n".join(body)))
    assert r_none["toc"]["suspected"] is False, r_none["toc"]


def case_cli() -> None:
    """CLI 的 json 输出必须可解析，且和库函数结果一致。"""
    p = _tmp("cli.txt", "第一章 开始\n\n正文。\n")
    out = subprocess.run(
        [sys.executable, "-m", "huoban", "diagnose", str(p), "--json"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["file"].endswith("cli.txt")
    assert "encoding" in data and "chapters" in data


def case_clean_cli() -> None:
    """clean 子命令端到端：三个产物都落盘、输入不动、--json 可解析。"""
    titles = [f"第{i}章 标题{i}" for i in range(1, 6)]
    lines = ["更多精彩小说，尽在某某小说网 www.example-novel.com", "目录"]
    lines.extend(titles)                    # 目录 5 条：少于 5 条体检不会认成目录
    lines.append("")
    for i, t in enumerate(titles):
        # 正文不能以"第N章"开头：那会被 match_chapter 认成又一个标题
        lines.extend([t, "", f"这是标题{i + 1}下面的正文。", ""])
    src = _tmp("clean_me.txt", "\n".join(lines) + "\n")

    before = src.read_bytes()
    outdir = src.parent / "out"

    out = subprocess.run(
        [sys.executable, "-m", "huoban", "clean", str(src), "-o", str(outdir)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert out.returncode == 0, out.stderr
    assert "原始文件未改动" in out.stdout, out.stdout
    for name in ("book.json", "book.txt", "clean-report.md"):
        assert (outdir / name).is_file(), f"缺少产物 {name}"
    assert src.read_bytes() == before, "输入文件被改了"

    from huoban.model import load as load_book

    book = load_book(outdir / "book.json")
    want = [t.replace(" ", "\u3000") for t in titles]
    assert [c.title for c in book.chapters] == want, [c.title for c in book.chapters]

    # --json：标准输出必须只有一份能直接 loads 的报告（摘要走 stderr）
    out2 = subprocess.run(
        [sys.executable, "-m", "huoban", "clean", str(src), "-o", str(outdir), "--json"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert out2.returncode == 0, out2.stderr
    report = json.loads(out2.stdout)
    assert report["dropped"]["ad"] == 1, report["dropped"]
    assert report["stats"]["chapters"] == 5, report["stats"]


def case_epub_cli() -> None:
    """epub 子命令端到端：一条命令从脏 TXT 出书，包能回读、输入不动。"""
    titles = [f"第{i}章 标题{i}" for i in range(1, 6)]
    lines = ["更多精彩小说，尽在某某小说网 www.example-novel.com", "目录"]
    lines.extend(titles)
    lines.append("")
    for i, t in enumerate(titles):
        lines.extend([t, "", f"这是标题{i + 1}下面的正文。", ""])
    src = _tmp("epub_me.txt", "\n".join(lines) + "\n")

    before = src.read_bytes()
    outdir = src.parent / "book"

    out = subprocess.run(
        [sys.executable, "-m", "huoban", "epub", str(src), "-o", str(outdir)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert out.returncode == 0, out.stderr
    assert "原始文件未改动" in out.stdout, out.stdout
    epub = outdir / "book.epub"
    assert epub.is_file(), "缺少 book.epub"
    assert src.read_bytes() == before, "输入文件被改了"

    import zipfile
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        assert names[0] == "mimetype", names[:3]
        assert z.read("mimetype") == b"application/epub+zip"
        info = z.getinfo("mimetype")
        assert info.compress_type == zipfile.ZIP_STORED, info.compress_type
        assert "OEBPS/Text/chap0005.xhtml" in names, names
        # 包内每个 xhtml / opf / ncx 都必须能被 XML 解析
        for n in names:
            if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ET.fromstring(z.read(n))

    # --json：标准输出必须只有一份能直接 loads 的摘要（人读的走 stderr）
    out2 = subprocess.run(
        [sys.executable, "-m", "huoban", "epub", str(src), "-o", str(outdir), "--json"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert out2.returncode == 0, out2.stderr
    payload = json.loads(out2.stdout)
    assert payload["chapters"] == 5, payload
    assert payload["bytes"] > 0, payload

    # 确定性：同一个输入打两次包，字节必须完全相同。
    # CLI 默认拿输入文件的修改时间当 dcterms:modified，所以这条才成立；
    # 早先默认取"当前时间"，同一份输入隔一秒就是两个不同的包。
    first = epub.read_bytes()
    out3 = subprocess.run(
        [sys.executable, "-m", "huoban", "epub", str(src), "-o", str(outdir)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert out3.returncode == 0, out3.stderr
    assert epub.read_bytes() == first, "同样的输入两次打包字节不同（不确定）"

    # -o 指到输入文件上必须被拦下（铁律：永不改输入）
    out4 = subprocess.run(
        [sys.executable, "-m", "huoban", "epub", str(src), "-o", str(src)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert out4.returncode != 0, "输出指向输入文件却没被拦下"
    assert src.read_bytes() == before, "输入文件被改了"


def main() -> int:
    cases = [
        case_plain, case_one_line_book, case_ads, case_encoding,
        case_gbk_bom_utf8, case_toc, case_cli, case_clean_cli, case_epub_cli,
    ]
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
