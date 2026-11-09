"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .azw3 import ENV_VAR as AZW3_ENV_VAR
from .azw3 import Azw3Error, Azw3Unavailable
from .azw3 import convert as convert_azw3
from .clean import CleanResult, clean_file
from .cleanreport import render_clean_markdown
from .diagnose import diagnose
from .epub import write_epub
from .epubread import read_epub_with_report
from .model import load as load_book
from .model import save as save_book
from .model import validate
from .render import render_txt
from .redact import load_name_map, redact_diagnose, redact_report, scrub_line, shape
from .report import render_markdown

# clean 子命令的产物文件名。固定名字而不是拼输入文件名：
# 输出目录本来就是按书分的（默认 <书名>_clean/），在里面再用书名当前缀，
# 只会让后续脚本的路径变长。
BOOK_JSON = "book.json"
BOOK_TXT = "book.txt"
BOOK_EPUB = "book.epub"
CLEAN_REPORT = "clean-report.md"
IMAGE_DIR = "images"

# 异常消息里可能出现的"书文件名"词：reflow 的报错会拼输入文件名（打不开 <名>.epub）
# 与包内文档名（DRM 那条会拼例如 ch.xhtml）。redact.py 的 _TXT_PATH 只认 .txt，
# 这几类后缀靠这里兜。匹配到冒号/空格为止，不会吃掉句子主体。
_BOOK_FILENAME = re.compile(
    r"[^\s，。；：、<>|\"'*?]+(?:\.epub|\.xhtml|\.html?|\.opf|\.ncx|\.xml)",
    re.IGNORECASE,
)


def _load_name_map(path: str | None) -> dict[str, str] | None:
    """读别名表（tools/mask.py 产出的 name-map.json）。

    为什么要一张外置的表而不是每次都算哈希：同一本书在批量报告里叫 B07、
    在单本清洗报告里也得叫 B07，AI 才能把两边对上。代号本身不可读，
    可读性靠这张表（表留在本地，AI 不读）。

    给了路径却读不到就直接报错退出，不静默回退成哈希 —— 静默回退会让
    "我以为脱敏过了"和"其实没脱敏"两种状态看起来一模一样。
    """
    if not path:
        return None
    try:
        return load_name_map(path)
    except ValueError as exc:
        raise SystemExit(str(exc))


def _load_adjust(value: str | None) -> dict | None:
    """读人工调整（docs/gui-design.md §7 的 adjust）。

    两种写法：直接给 JSON 文本，或者给一个装着 JSON 的文件路径。
    以 "{" 开头当 JSON 文本，否则当路径 —— 复核界面把勾选状态写成临时文件
    丢过来是最常见的用法；人手工试一条时直接贴 JSON 更省事。

    解析失败一律报错退出，**不静默忽略**：adjust 静默失效意味着"我以为保护了
    这一行"，结果它照样被删，而且没有任何痕迹。宁可整条命令失败。

    失败一律抛 ValueError（不是 SystemExit）：调用方把它和清洗阶段
    `_parse_adjust` 抛的 ValueError 一起接住，统一成
    「人工调整有问题：…」+ 返回码 2。用户不该因为"错在读文件"还是
    "错在键名"看到两套说法。
    """
    if not value:
        return None
    raw = value.strip()
    # 以 "{" 或 "[" 开头都当 JSON 文本：'[1,2]' 是一眼能看出的"想贴 JSON 却
    # 贴错了形状"，当路径处理会报「读不到文件」，把人引到错误的方向上去。
    # 形状错由下面的 isinstance 检查负责，报「应该是 JSON 对象」。
    if raw.startswith(("{", "[")):
        text, where = raw, "--adjust 的值"
    else:
        p = Path(raw)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"读不到 adjust 文件：{p}（{exc.strerror or exc}）")
        where = str(p)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where} 不是合法 JSON：第 {exc.lineno} 行 {exc.msg}")
    if not isinstance(data, dict):
        raise ValueError(
            f"{where} 应该是 JSON 对象（形如 {{\"protect_lines\": [12]}}），"
            f"实际是 {type(data).__name__}"
        )
    return data


def _write_clean_outputs(
    result: CleanResult,
    outdir: Path,
    src: Path,
    *,
    report: dict | None = None,
) -> dict[str, Path]:
    """把三个产物写进 outdir，返回 {名字: 路径}。

    这里集中做一次"绝不覆盖输入"的检查。检查放在写盘这一层而不是上游：
    上游只负责算，算完了路径才存在，路径存在了才谈得上撞车。

    report 可传脱敏后的那份：--redact 时 clean-report.md 也要能整份贴进
    AI 会话，所以 markdown 必须由同一份脱敏数据渲染，不能在渲染层再判一次。
    book.txt / book.json 照旧是原文 —— 那是给人看的交付物，AI 一律不读。
    """
    outdir.mkdir(parents=True, exist_ok=True)
    targets = {
        "book_json": outdir / BOOK_JSON,
        "book_txt": outdir / BOOK_TXT,
        "clean_report": outdir / CLEAN_REPORT,
    }
    src_resolved = src.resolve()
    for path in targets.values():
        if path.resolve() == src_resolved:
            raise SystemExit(f"输出路径与输入文件相同，拒绝写入：{path}")

    save_book(result.book, targets["book_json"])
    targets["book_txt"].write_text(render_txt(result.book), encoding="utf-8")
    targets["clean_report"].write_text(
        render_clean_markdown(report if report is not None else result.report),
        encoding="utf-8",
    )
    return targets


def _write_epub_output(
    book,
    outpath: Path,
    src: Path,
    *,
    title: str | None = None,
    author: str | None = None,
    cover: str | None = None,
    back_cover: str | None = None,
    language: str = "zh-CN",
    modified: str | None = None,
    fonts: bool = True,
    images: str | Path | None = None,
):
    """把章节树打成 EPUB 并落盘，返回 EpubResult。

    与 clean 的 _write_clean_outputs 一样，撞车检查放在写盘这一层：
    -o 指到输入文件上时必须当场拒绝 —— 输入永远只读是项目的铁律。
    """
    # 用 samefile 而不是只比 resolve()：同一份文件可以有两个路径（硬链接），
    # 也可以有大小写不同的写法。resolve() 只解决后者，硬链接会把输入覆盖掉，
    # 而"永不修改输入文件"是这个项目的铁律。
    if outpath.exists():
        try:
            same = outpath.samefile(src)
        except OSError:
            same = outpath.resolve() == src.resolve()
        if same:
            raise SystemExit(f"输出路径与输入文件相同，拒绝写入：{outpath}")
    return write_epub(
        book,
        outpath,
        title=title,
        author=author,
        cover_path=cover,
        back_cover=back_cover,
        language=language,
        modified=modified,
        fonts=fonts,
        image_dir=images,
    )

def _resolve_modified(value: str | None, src: Path) -> str:
    """算出 dcterms:modified 该写什么。

    三种情况，都是为了让"同样输入打两次得到同一份字节"成立：
    - 显式给了 "now"：用当前时间（明确表达"我要现在"的人才拿现在）；
    - 给了其它值：原样交给 epub 层校验格式，认不出来就当场报错；
    - 没给：取**输入文件的修改时间**。这一条是关键 —— 之前默认取当前时间，
      同一个 TXT 隔一秒打两次就是两个不同的包，任何"构建产物可复现"的想法都
      无从谈起。输入没变，书的修改时间就没变，这才是事实。
    """
    if value is None:
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(src), tz=timezone.utc)
        except OSError:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
    if value.strip().lower() == "now":
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def _print_clean_summary(
    result: CleanResult, outdir: Path, targets: dict[str, Path], *, stream=None,
    report: dict | None = None,
) -> None:
    """把最关键的数字打出来。

    为什么摘要里也要重复"原始文件未改动"：清洗会删掉几千行，
    用户看到输出后第一反应是"我的原文件还在吗"，这句话要出现在他眼前，
    而不是埋进报告文件的第一行。

    stream 可换：--json 时摘要走 stderr，标准输出必须只剩一份能直接
    json.loads() 的报告 —— 混在一起的输出没法被脚本消费。

    report 传脱敏后的那份时，摘要里的文件名与警告也一起脱敏：--redact
    的语义是"这条命令的全部输出可以放心贴进 AI 会话"，只脱 stdout 会留下
    stderr 泄原文的空子。
    """
    out = stream if stream is not None else sys.stdout
    r = report if report is not None else result.report
    d = r["dropped"]
    f = r["fixed"]
    s = r["stats"]
    print(f"输入  {r['file']}", file=out)
    print(
        f"      编码 {r['encoding']['chosen']}（置信度 {r['encoding']['confidence']}），"
        f"{r['size_bytes']} 字节，{r['lines']['total']} 行",
        file=out,
    )
    print(f"输出  {outdir}", file=out)
    print(f"      章节 {s['chapters']}，段落 {s['paragraphs']}，字数 {s['chars']}", file=out)
    lead = f.get("lead_marker_lines", 0)
    print(
        f"删除  整行广告 {d['ad']} 行 / 目录 {d['toc']} 行"
        + (f" / 段首占位符 {lead} 行" if lead else "")
        + f" / 压掉空行 {d['extra_blank']} 行（排版用，不算删内容）",
        file=out,
    )
    print(
        f"改写  零宽字符 {f['zero_width_lines']} 行 / 行尾空白 {f['trailing_space_lines']} 行 "
        f"/ 标点 {f['punctuation_lines']} 行",
        file=out,
    )
    if r["merged"]["absorbed_lines"]:
        print(
            f"合并  碎行 {r['merged']['groups']} 组，吸收 {r['merged']['absorbed_lines']} 行",
            file=out,
        )
    # 诗词只换排版不改字，单独一行说明，避免用户看到 EPUB 里某几段居中换字体
    # 却不知道是程序的决定。
    verse_lines = (r.get("verse") or {}).get("lines", 0)
    if verse_lines:
        print(f"排版  诗词/韵文 {verse_lines} 段（居中、楷体、不缩进）", file=out)
    for w in r["warnings"]:
        print(f"注意  {w}", file=out)
    print("原始文件未改动。", file=out)
    if r.get("redacted"):
        # 输入文件显示的是代号（或哈希）而不是路径：贴给人看时靠 --mask-map
        # 那张表回去定位。不打印表本身。
        print("      已脱敏：样例正文/书名/路径/警告里的引文均换成形状描述。", file=out)
    for name, path in targets.items():
        print(f"  {name}: {path}", file=out)


def _mask_path(path, masked: bool) -> str:
    """--redact 模式下路径的显示口径：一律形状描述，非 masked 原样。

    为什么路径也算泄漏：reflow/epub 的默认输出目录就叫「<输入文件名>_reflow」，
    而输入文件名通常就是书名 —— 打路径等于白脱敏。
    """
    s = str(path)
    return shape(s) if masked else s


def _mask_error(text: object, names, masked: bool) -> str:
    """异常消息在 --redact 下的显示口径：句子主体留着，已知的敏感词换成形状描述。

    为什么要单独处理：epubread 的报错会把输入文件名拼进句子里
    （`打不开 <文件名>：它不是一个合法的 ZIP/EPUB`），DRM 那条还会拼包内文档名，
    而这类文件名通常就是书名。scrub_line 只认书名号引文与 *.txt 路径词，
    认不出 .epub/.xhtml，所以这里再按「像书文件名的词」兜一遍。
    句子主体是给人照做的规则说明（"固定版式，无法导入"），不能整条糊掉。
    """
    s = str(text)
    if not masked:
        return s
    s = scrub_line(s)
    for name in names:
        if name and name in s:
            s = s.replace(str(name), shape(str(name)))
    return _BOOK_FILENAME.sub(lambda m: shape(m.group(0)), s)


def _print_epub_summary(
    result,
    outpath: Path,
    book,
    *,
    stream=None,
    masked: bool = False,
) -> None:
    """EPUB 打包结果摘要。

    和 clean 的摘要同一个口径：先说清输入没被动过，再说包里有什么，
    最后把 warnings 原样打出来 —— 打包会丢掉一些东西（比如无章节时的前言），
    丢东西必须让人看见。

    masked=True 时书名/作者换成形状描述：那两个字段就是书名本身，
    --redact 的语义要求整条命令的输出都不含书里的字。
    输出路径同样是泄漏点（默认目录名就带书名），masked 下走形状描述。
    """
    out = stream if stream is not None else sys.stdout
    print(f"输出  {_mask_path(outpath, masked)}", file=out)
    print(
        f"      {len(result.files)} 个条目，{len(result.data)} 字节"
        f"（{len(result.data) / 1024:.0f} KB）",
        file=out,
    )
    title_disp = shape(book.title) if masked else (book.title or "未命名")
    author_disp = shape(book.author) if masked else (book.author or "（未识别）")
    print(
        f"      书名 {title_disp} / 作者 {author_disp} / "
        f"章节 {result.chapters}，段落 {result.paragraphs}",
        file=out,
    )
    # 内嵌了几套字体直接看包里有没有 Fonts/*.ttf —— 不额外传参，
    # 摘要说的就是包里的实际情况。
    embedded = [n for n in result.files if n.startswith("OEBPS/Fonts/")]
    if embedded:
        print(
            f"      字体 内嵌 {len(embedded)} 套（{', '.join(n.rsplit('/', 1)[-1] for n in embedded)}）",
            file=out,
        )
    else:
        print("      字体 未内嵌，只写系统字体回退链", file=out)
    for w in result.warnings:
        # 打包警告会带上封面/插图的路径（epub.py 里 f"封面 {path} 读不出来"），
        # masked 下和 reflow 警告同一个口径：先 scrub 书名号与 *.txt 词。
        print(f"注意  {scrub_line(w) if masked else w}", file=out)
    print("原始文件未改动。", file=out)

def _print_reflow_summary(
    report: dict,
    book_json: Path,
    img_dir: Path,
    problems: list[str],
    *,
    stream=None,
    masked: bool = False,
) -> None:
    """EPUB 导入结果摘要。

    口径与 clean/epub 两个摘要一致：先说输入没被动过，再说拆出了什么，
    最后把 warnings 与 validate 的问题原样打出来 —— 导入会丢东西
    （装饰图、目录页、字符级格式），丢东西必须让人看见。
    """
    out = stream if stream is not None else sys.stdout
    src_disp = shape(report["source"]) if masked else report["source"]
    title_disp = shape(report["title"]) if masked else (report["title"] or "未命名")
    author_disp = shape(report["author"]) if masked else (report["author"] or "（未识别）")
    print(f"输入  {src_disp}", file=out)
    # "片 → 章"只算章，不含卷：合并只发生在章这一层，卷不参与（§12.7 要求
    # 报出「合并 285 组：2240 片 → 620 章」这种能对上原书的数字）。
    merged_chapters = report["chapters"] - report["volumes"]
    print(
        f"      目录条目 {report['nav_entries']}，章节 {report['chapters']}"
        f"（其中卷 {report['volumes']}），合并 {report['merged_groups']} 组："
        f"{report['merged_pieces']} 片 → {merged_chapters} 章",
        file=out,
    )
    print(
        f"      前言 {report['front_matter_paragraphs']} 段"
        f"（前附条目 {report['front_matter_entries']}，跳过目录页 {report['toc_dropped']}，"
        f"续页 {report['continuation_docs']}）",
        file=out,
    )
    print(f"      书名 {title_disp} / 作者 {author_disp}", file=out)
    print(f"输出  {_mask_path(book_json, masked)}", file=out)
    print(
        f"      图片 {report['images_kept']} 张落盘到 {_mask_path(img_dir, masked)}"
        f"（装饰图丢弃 {len(report['images_decorative'])} 张，"
        f"孤儿图 {len(report['images_orphan'])} 张未打包）",
        file=out,
    )
    cover_disp = report["cover"] or "（无）"
    if masked and report["cover"]:
        cover_disp = shape(report["cover"])
    # 真实样本里有 80 字符的混淆文件名（`**::**::::…`），摘要里只留开头。
    if len(cover_disp) > 24:
        cover_disp = cover_disp[:23] + "…"
    print(f"      封面 {cover_disp} / 原书字体 {report['fonts']} 套（不导入）", file=out)
    for w in report["warnings"]:
        print(f"注意  {scrub_line(w) if masked else w}", file=out)
    if problems:
        print(f"章节树有 {len(problems)} 条问题（已照现状写出）：")
        for p in problems[:5]:
            print(f"  - {scrub_line(p) if masked else p}")
    print("原始文件未改动。", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="huoban", description="脏 TXT 小说清洗与电子书打包")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_redact_args(p: argparse.ArgumentParser) -> None:
        """三个子命令共用的一对开关。

        为什么默认关：现有测试与日常人读报告要的是原文样例 —— 人眼核对时
        "这一行是不是被误删的广告"必须看见字才能判断，默认脱敏反而让工具
        对人没法用了。脱敏是给"贴进 AI 会话"这一个场景准备的。

        为什么必须两个开关一起给：只 --redact 不给 --mask-map，代号退化成
        内容哈希，同一本书在不同命令里代号不同，AI 就对不上账。
        """
        p.add_argument(
            "--redact",
            action="store_true",
            help="脱敏输出：样例正文/书名/路径/警告引文换成形状描述，用于贴进 AI 会话",
        )
        p.add_argument(
            "--mask-map",
            help="别名表 JSON（tools/mask.py 产出），配合 --redact 让代号跨命令一致",
        )

    d = sub.add_parser("diagnose", help="只读体检：编码 / 章节 / 广告 / 段落形态")
    d.add_argument("path", help="TXT 文件路径")
    d.add_argument("--json", action="store_true", help="输出原始 JSON")
    d.add_argument("-o", "--out", help="写入文件（默认打印到标准输出）")
    _add_redact_args(d)

    c = sub.add_parser("clean", help="确定性清洗：输出章节树 JSON + 干净 TXT + 清洗报告")
    c.add_argument("path", help="TXT 文件路径（只读，不会被修改）")
    c.add_argument(
        "-o", "--out",
        help=f"输出目录（默认：输入文件旁的“<文件名>_clean”），内含 {BOOK_JSON} / {BOOK_TXT} / {CLEAN_REPORT}",
    )
    c.add_argument("--json", action="store_true", help="把清洗报告以 JSON 打到标准输出")
    c.add_argument(
        "--adjust",
        metavar="JSON|文件",
        help=(
            "人工调整（复核界面勾选的结果）：JSON 文本，或装着 JSON 的文件路径。"
            "形如 {\"protect_lines\": [1024], \"protect_ranges\": [[200,240]], "
            "\"merge_breaks\": [1052]}；行号指清洗前的原文行号"
        ),
    )
    _add_redact_args(c)

    e = sub.add_parser("epub", help="清洗并打包成 EPUB：一条命令从脏 TXT 到能读的电子书")
    e.add_argument("path", nargs="?", help="TXT 文件路径（只读，不会被修改）；用 --book-json 时可省略")
    e.add_argument(
        "-o", "--out",
        help=f"输出文件或目录（默认：输入文件旁的“<文件名>_clean”目录，内含 {BOOK_EPUB}）",
    )
    e.add_argument("--title", help="书名（默认从正文最前面几段猜）")
    e.add_argument("--author", help="作者（默认从正文最前面几段猜）")
    e.add_argument("--cover", help="封面图路径（jpg/png/gif，不给就生成纯文字封面页）")
    e.add_argument(
        "--back-cover",
        help="封底图路径（jpg/png/gif，不给就不生成封底页）",
    )
    e.add_argument(
        "--images",
        help=(
            "章节内图片所在目录（reflow 导入的章节树用得上；"
            f"默认取 --book-json 旁边的 {IMAGE_DIR}/）"
        ),
    )
    e.add_argument("--language", default="zh-CN", help="语言标签，默认 zh-CN")
    e.add_argument(
        "--book-json",
        help="不读 TXT，直接拿已有的章节树 JSON 打包（跳过清洗）",
    )
    e.add_argument(
        "--modified",
        help=(
            "元数据里的修改时间（默认取输入文件的修改时间，保证同样输入两次打包"
            "字节完全相同）；要“现在”就显式写 --modified now"
        ),
    )
    e.add_argument(
        "--no-fonts",
        action="store_true",
        help=(
            "不内嵌字体，只写系统字体回退链（包从 2 MB 掉到 150 KB，"
            "但正文会走阅读器默认宋体）"
        ),
    )
    e.add_argument("--json", action="store_true", help="把打包摘要以 JSON 打到标准输出")
    e.add_argument(
        "--azw3",
        action="store_true",
        help=(
            "顺带导出 AZW3（Kindle 用），产物放在 EPUB 旁边、同名换后缀。"
            "需要本机装了 Calibre 的 ebook-convert（可选依赖）。"
            "注意 AZW3 不是逐字节可复现的，要回归比对请比 EPUB"
        ),
    )
    e.add_argument(
        "--adjust",
        metavar="JSON|文件",
        help=(
            "清洗时的人工调整（同 clean --adjust）；给了 --book-json 时无效，"
            "因为那一步跳过了清洗"
        ),
    )
    _add_redact_args(e)

    r = sub.add_parser(
        "reflow",
        help="导入现成 EPUB：拆回章节树 JSON + 图片目录（重排，丢字符级格式）",
    )
    r.add_argument("path", help="EPUB 文件路径（只读，不会被修改）")
    r.add_argument(
        "-o", "--out",
        help=f"输出目录（默认：输入文件旁的“<文件名>_reflow”），内含 {BOOK_JSON} 与 {IMAGE_DIR}/",
    )
    r.add_argument(
        "--images",
        help=f"图片落盘目录（默认：输出目录下的 {IMAGE_DIR}/）",
    )
    r.add_argument("--json", action="store_true", help="把导入报告以 JSON 打到标准输出")
    _add_redact_args(r)
    args = parser.parse_args(argv)
    name_map = _load_name_map(getattr(args, "mask_map", None))
    try:
        adjust = _load_adjust(getattr(args, "adjust", None))
    except ValueError as exc:
        # 与清洗阶段抛出的 adjust 错误用同一句话、同一个返回码：用户看到的
        # "人工调整有问题"只该有一种口径，不该因为错在读文件、解析 JSON
        # 还是错在键名而变成两套说法（docs/gui-design.md §7.3 就写了这一条）。
        print(f"人工调整有问题：{exc}")
        return 2

    if args.cmd == "diagnose":
        target = Path(args.path)
        if not target.is_file():
            print(f"找不到文件：{target}")
            return 2
        result = diagnose(target)
        shown = redact_diagnose(result, name_map=name_map) if args.redact else result
        text = (
            json.dumps(shown, ensure_ascii=False, indent=2)
            if args.json
            else render_markdown(shown)
        )
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"已写入 {args.out}")
        else:
            print(text)
        return 0

    if args.cmd == "epub":
        if args.book_json:
            src = Path(args.book_json)
            if not src.is_file():
                print(f"找不到文件：{src}")
                return 2
            book = load_book(src)
            if adjust:
                # 说清楚，别让人以为"保护了但没生效"是 bug。
                print("提示：--adjust 只在清洗那一步生效；--book-json 跳过了清洗，本次忽略。")
        else:
            if not args.path:
                print("要给一个 TXT 文件路径，或者用 --book-json 指定已有的章节树")
                return 2
            src = Path(args.path)
            if not src.is_file():
                print(f"找不到文件：{src}")
                return 2
            try:
                book = clean_file(src, adjust=adjust).book
            except ValueError as exc:
                # adjust 里有不认识的键（或整体形状不对）。这是用户输入问题，
                # 不是程序问题，所以只给一句话，不给 traceback。
                print(f"人工调整有问题：{exc}")
                return 2

        if args.out:
            out = Path(args.out)
            # 「看起来像目录」的三种写法都按目录处理：已存在的目录、以分隔符结尾、
            # 没有后缀名。其余一律当成目标文件名 —— 用户说 -o a.epub 就是想要 a.epub。
            looks_dir = out.is_dir() or str(args.out).endswith(("/", "\\")) or not out.suffix
            outpath = out / BOOK_EPUB if looks_dir else out
        else:
            stem = src.stem
            outpath = src.parent / f"{stem}_clean" / BOOK_EPUB

        problems = validate(book)
        # 章节树里若有 image 段，图片本体得从磁盘找回来。reflow 的产物是
        # "<目录>/book.json + <目录>/images/"，所以 --book-json 旁边那个 images/
        # 就是默认值；显式给了 --images 就听显式的（找不到就只警告不报错）。
        image_dir = args.images
        if image_dir is None and args.book_json:
            sibling = Path(args.book_json).parent / IMAGE_DIR
            if sibling.is_dir():
                image_dir = str(sibling)
        try:
            result = _write_epub_output(
                book,
                outpath,
                src,
                title=args.title,
                author=args.author,
                cover=args.cover,
                back_cover=args.back_cover,
                language=args.language,
                modified=_resolve_modified(args.modified, src),
                fonts=not args.no_fonts,
                images=image_dir,
            )
        except ValueError as exc:
            # 打包层会为"元数据不合规"和"骨架不是良构 XML"抛错。这两类错误
            # 都不该以 traceback 的形式糊到用户脸上 —— 它要的是一句能照做的话。
            # 报错里可能夹着输入文件名/封面名（都是书名），--redact 下走掩码。
            names = (src.name, outpath.name)
            if args.cover:
                names += (Path(args.cover).name,)
            if args.back_cover:
                names += (Path(args.back_cover).name,)
            print(f"打包失败：{_mask_error(exc, names, args.redact)}")
            return 2
        except OSError as exc:
            # 写盘失败（目录不可建、磁盘满、没有权限、路径过长）：同样不给 traceback。
            # 异常自带的文件名就是路径，可能带书名，所以只取 strerror 那一半。
            reason = exc.strerror or exc.__class__.__name__
            print(f"打包失败：{reason}（目标：{_mask_path(outpath, args.redact)}）")
            return 2
        # AZW3 是可选的一步：EPUB 已经落盘了，它成不成功都不该把 EPUB 弄丢。
        # 所以这里不抛异常，只把结果或错误记下来，最后一起说、用返回码表态。
        azw3_result = None
        azw3_error = ""
        if args.azw3:
            azw3_out = outpath.with_suffix(".azw3")
            try:
                azw3_result = convert_azw3(
                    outpath,
                    azw3_out,
                    pubdate=_resolve_modified(args.modified, src),
                )
            except (Azw3Unavailable, Azw3Error) as exc:
                names = (src.name, outpath.name, azw3_out.name)
                azw3_error = _mask_error(exc, names, args.redact)
        if args.json:
            _print_epub_summary(result, outpath, book, stream=sys.stderr,
                                masked=args.redact)
            if args.redact:
                # --json 的 payload 也要同一口径：书名/作者换成形状描述，
                # 警告语里可能夹了引文，走 scrub_line。validate 的问题串是
                # 我们自己拼的（章节序号/空章节），不含正文，原样保留。
                payload = {
                    # 输出路径默认是「<书名>_clean/book.epub」，和摘要同一个口径。
                    "epub": _mask_path(outpath, True),
                    "files": len(result.files),
                    "bytes": len(result.data),
                    "chapters": result.chapters,
                    "paragraphs": result.paragraphs,
                    "title": shape(book.title),
                    "author": shape(book.author),
                    "warnings": [scrub_line(w) for w in result.warnings],
                    # validate 的问题串里带「章节标题」，那是书里的字，
                    # 引号本身要留着（人靠它判断"这是标题"），内容换形状描述。
                    "validate": [scrub_line(p) for p in problems],
                    "redacted": True,
                }
                if args.azw3:
                    payload["azw3"] = (
                        {"path": _mask_path(azw3_result.path, True),
                         "bytes": azw3_result.size_bytes,
                         "seconds": round(azw3_result.seconds, 1)}
                        if azw3_result else {"error": azw3_error}
                    )
            else:
                payload = {
                    "epub": str(outpath),
                    "files": len(result.files),
                    "bytes": len(result.data),
                    "chapters": result.chapters,
                    "paragraphs": result.paragraphs,
                    "title": book.title,
                    "author": book.author,
                    "warnings": result.warnings,
                    "validate": problems,
                }
                if args.azw3:
                    payload["azw3"] = (
                        {"path": str(azw3_result.path),
                         "bytes": azw3_result.size_bytes,
                         "seconds": round(azw3_result.seconds, 1)}
                        if azw3_result else {"error": azw3_error}
                    )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_epub_summary(result, outpath, book, masked=args.redact)
            # 章节树本身的毛病（空章节、序号重复）不影响打包，但要说出来：
            # 打出来的书能读，不代表章节切对了。
            if problems:
                print(f"章节树有 {len(problems)} 条问题（已照现状打包）：")
                for p in problems[:5]:
                    # 问题串里带「章节标题」——书里的字，masked 下走 scrub_line。
                    print(f"  - {scrub_line(p) if args.redact else p}")
            if args.azw3:
                if azw3_result:
                    kb = azw3_result.size_bytes / 1024
                    print(
                        f"AZW3 已生成：{_mask_path(azw3_result.path, args.redact)}"
                        f"（{kb:.0f} KB，{azw3_result.seconds:.1f} 秒）"
                    )
                else:
                    print(f"AZW3 没转成：{azw3_error}")
                    print(
                        "  EPUB 已经生成好了，不影响阅读；"
                        "要 AZW3 就装一个 Calibre（或在环境变量 "
                        f"{AZW3_ENV_VAR} 里指到 ebook-convert.exe）再跑一次"
                    )
        # AZW3 失败时用返回码表态：用户明确要了它，没给到就不能装作成功。
        return 2 if (args.azw3 and azw3_result is None) else 0
    if args.cmd == "reflow":
        target = Path(args.path)
        if not target.is_file():
            # 这里是把用户自己打的路径回显一遍 —— 掩了既不减少泄漏（命令行本身
            # 已经带着这个名字），又让人看不出自己打错了什么，所以保持原样。
            print(f"找不到文件：{target}")
            return 2
        outdir = Path(args.out) if args.out else target.parent / f"{target.stem}_reflow"
        if outdir.exists() and not outdir.is_dir():
            print(f"输出路径已存在且不是目录：{_mask_path(outdir, args.redact)}")
            return 2
        book_json = outdir / BOOK_JSON
        img_dir = Path(args.images) if args.images else outdir / IMAGE_DIR
        # 撞车检查放在写盘之前：输入永远只读是项目的铁律。
        # 要挡的是"把输出写成输入本身"或"往输入所在的地方塞东西"两种：
        # 后者更隐蔽 —— --images 指到输入文件所在的目录并不会覆盖它，
        # 但会把图片撒进用户的原始书库，还是等于动了不该动的地方。
        def _collides(out_path: Path) -> bool:
            try:
                return out_path.exists() and out_path.samefile(target)
            except OSError:
                return False

        for label, out_path in (("章节树", book_json), ("图片目录", img_dir)):
            if _collides(out_path):
                print(f"{label}的输出路径与输入文件相同，拒绝写入："
                      f"{_mask_path(out_path, args.redact)}")
                return 2
        # 图片目录指到已存在的普通文件：写盘时才炸（mkdir 抛 FileExistsError），
        # 那时已经建了章节树、留了半套产物。提前一句话说清，不留垃圾。
        if img_dir.exists() and not img_dir.is_dir():
            print(f"图片目录已存在且不是目录：{_mask_path(img_dir, args.redact)}")
            return 2
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            reason = exc.strerror or exc.__class__.__name__
            print(f"建不了输出目录：{reason}（目标：{_mask_path(outdir, args.redact)}）")
            return 2
        try:
            result = read_epub_with_report(target, image_dir=img_dir)
        except ValueError as exc:
            # 打不开 / 有 DRM / 固定版式：都是"用户得换一本书"的错，
            # 以一句能照做的话报出来，不糊 traceback。
            # --redact 时这套话里夹了输入文件名与包内文档名（就是书名），走掩码。
            print(f"导入失败：{_mask_error(exc, (target.name,), args.redact)}")
            return 2
        except OSError as exc:
            # 落图片时的写盘错误（目录被占、磁盘满、路径过长）：同样给一句人话。
            # 异常自带的文件名可能带书名，只取 strerror。
            reason = exc.strerror or exc.__class__.__name__
            print(f"导入失败：写不了图片文件（{reason}），检查图片目录是否可写："
                  f"{_mask_path(img_dir, args.redact)}")
            return 2
        book = result.book
        report = result.report
        save_book(book, book_json)
        problems = validate(book)
        if args.json:
            _print_reflow_summary(report, book_json, img_dir, problems,
                                  stream=sys.stderr, masked=args.redact)
            if args.redact:
                payload = {
                    "epub": shape(report["source"]),
                    "book_json": _mask_path(book_json, True),
                    "images_dir": _mask_path(img_dir, True),
                    "nav_entries": report["nav_entries"],
                    "chapters": report["chapters"],
                    "volumes": report["volumes"],
                    "merged_groups": report["merged_groups"],
                    "merged_pieces": report["merged_pieces"],
                    "front_matter_paragraphs": report["front_matter_paragraphs"],
                    "images_kept": report["images_kept"],
                    "images_decorative": len(report["images_decorative"]),
                    "images_orphan": len(report["images_orphan"]),
                    "title": shape(report["title"]),
                    "author": shape(report["author"]),
                    "warnings": [scrub_line(w) for w in report["warnings"]],
                    "validate": [scrub_line(p) for p in problems],
                    "redacted": True,
                }
            else:
                payload = {
                    "epub": report["source"],
                    "book_json": str(book_json),
                    "images_dir": str(img_dir),
                    "nav_entries": report["nav_entries"],
                    "chapters": report["chapters"],
                    "volumes": report["volumes"],
                    "merged_groups": report["merged_groups"],
                    "merged_pieces": report["merged_pieces"],
                    "front_matter_paragraphs": report["front_matter_paragraphs"],
                    "images_kept": report["images_kept"],
                    "images_decorative": len(report["images_decorative"]),
                    "images_orphan": len(report["images_orphan"]),
                    "title": report["title"],
                    "author": report["author"],
                    "warnings": report["warnings"],
                    "validate": problems,
                }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_reflow_summary(report, book_json, img_dir, problems, masked=args.redact)
        return 0

    if args.cmd == "clean":
        target = Path(args.path)
        if not target.is_file():
            print(f"找不到文件：{target}")
            return 2
        outdir = Path(args.out) if args.out else target.parent / f"{target.stem}_clean"
        if outdir.exists() and not outdir.is_dir():
            print(f"输出路径已存在且不是目录：{outdir}")
            return 2
        try:
            result = clean_file(target, adjust=adjust)
        except ValueError as exc:
            # adjust 里有不认识的键（或整体形状不对）。用户输入问题，一句话说清。
            print(f"人工调整有问题：{exc}")
            return 2
        shown = redact_report(result.report, name_map=name_map) if args.redact else result.report
        targets = _write_clean_outputs(result, outdir, target, report=shown)
        if args.json:
            _print_clean_summary(result, outdir, targets, stream=sys.stderr,
                                 report=shown)
            print(json.dumps(shown, ensure_ascii=False, indent=2))
        else:
            _print_clean_summary(result, outdir, targets, report=shown)
        return 0

    return 1
