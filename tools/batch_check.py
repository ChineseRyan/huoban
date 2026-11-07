"""批量真书抽检：拿一批没跑过的书跑一遍清洗，只把可疑的挑出来。

为什么不写成测试用例：测试里的假数据是我自己想的边界，真书会给出我没想到的形态。
这个脚本的作用是"让真书替我出题" —— 每本书只做确定性清洗（不出 EPUB），量几个
指标，凡是数字长得不像一本书的，就单独拎出来人工看，再决定要不要改规则。

用法：

    python tools/batch_check.py                       # 默认抽 40 本
    python tools/batch_check.py --limit 60 --root D:/txt
    python tools/batch_check.py --all                 # 不抽样，全跑（很慢）
    python tools/batch_check.py --redact --mask-map backups/mask/map.json
        # 脱敏跑：书名/路径/题名/作者/异常消息全换成代号与形状描述，
        # 这样 stdout、stderr 和 report.json 都可以整份贴进 AI 会话。

判据都写在 flags_of() 里，每条都对应"人一眼能看出不对"的形态，不做玄学打分。

为什么要 --redact：这批记录原本会把每本书的书名和路径打进日志（实测 run100c.log
133 行里 125 行含 .txt），书名连着正文片段一起进 AI 会话，就会撞上提供方的入口
内容审查（406 data_inspection_failed），而且审查在模型运行之前 —— 整条历史每次
重放每次被拒，会话救不回来。调试需要的是"第 17 本切出 0 章"，不是书名。
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.clean import clean_bytes, clean_file, clean_text  # noqa: E402
from huoban.encoding import detect  # noqa: E402
from huoban.model import stats  # noqa: E402
from huoban.redact import alias_table, load_name_map, redact_batch_record, shape  # noqa: E402
from huoban.render import render_txt  # noqa: E402

# 默认抽样目录：本机放小说的地方
DEFAULT_ROOT = Path(r"E:\文档\txt\小说")
# 太小的文件不值得跑（多半是单章短篇或说明文件）
MIN_BYTES = 300 * 1024
# 默认抽多少本
DEFAULT_LIMIT = 40

# 判"不像一本书"的阈值。都是"差一个数量级"才会撞上的值，不做精细调节。
MIN_LINES_FOR_FEW_CHAPTERS = 3000   # 行数过这么多、却切不出 5 章 → 没认出标题
FRONT_MATTER_RATIO = 0.6            # 前言后记占了六成段落 → 正文没切进章节
MAX_CHAPTER_PARAGRAPHS = 5000       # 单章五千段 → 没切章，整本塞进一章
DUPLICATE_TITLE_RATIO = 0.3         # 三成以上标题重名 → 水印或空壳章节


def sample_books(root: Path, limit: int, min_bytes: int = MIN_BYTES) -> list[Path]:
    """从 root 下按路径排序后等距抽 limit 本。

    为什么等距抽而不是随机抽：抽样要能复现。同一个目录两次跑必须拿到同一批书，
    否则"上次那本有问题的书这次没抽到"，规则改没改好就无从对比。
    """
    files = sorted(
        p for p in root.rglob("*.txt") if p.is_file() and p.stat().st_size >= min_bytes
    )
    if limit <= 0 or len(files) <= limit:
        return files
    step = len(files) / limit
    return [files[int(i * step)] for i in range(limit)]


def _diff_lines(a: str, b: str) -> int:
    """两段文本的差异行数（不含 ---/+++ 头）。"""
    n = 0
    for line in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++")):
            continue
        if line[:1] in "+-":
            n += 1
    return n


def _duplicate_stats(chapters: list) -> tuple[int, int, int, int]:
    """算出 (全书重名数, 同卷重名数, 卷数, 编号重启次数)。

    卷 = level>=2 的章节。每遇到一个卷标题就换一卷，卷内标题各自记账。
    「编号重启」= 章级（level 1）里序号为 1 的章节出现次数：一本书里出现
    两个以上「第一章」，说明它的编号本来就是按部/按篇重数的（多部合集、
    短篇集），这类书的书名重复是形态而不是缺陷。
    """
    seen_all: set[str] = set()
    seen_in_volume: set[str] = set()
    dup_all = 0
    dup_same = 0
    volumes = 0
    restarts = 0
    for ch in chapters:
        if ch.level >= 2:
            # 新的一卷：卷内记账清零（卷标题自己也算进本卷）
            volumes += 1
            seen_in_volume = set()
        if ch.title in seen_all:
            dup_all += 1
        if ch.title in seen_in_volume:
            dup_same += 1
        seen_all.add(ch.title)
        seen_in_volume.add(ch.title)
        if ch.level < 2 and ch.number == 1:
            restarts += 1
    return dup_all, dup_same, volumes, restarts


def check_one(path: Path) -> dict:
    """跑一本书，返回它的指标。跑崩了也返回一条记录，不让单本书打断整批。"""
    started = time.time()
    rec: dict = {"path": str(path), "name": path.name, "ok": False}
    try:
        # 自己读字节，而不是直接 clean_file：编码候选的 strict_ok 要留下来。
        # 为什么重要：UTF-16LE 无 BOM 的书，detect() 会选 gb18030 且严格解码失败，
        # 解出来是乱码 —— 这种书在旧版记录里只会表现为「0章/章太少/重名标题」，
        # 看起来像"书没章节标记"，其实是"解错了"。判据不同，处置完全不同。
        raw = path.read_bytes()
        cand = detect(raw)[0]
        res = clean_bytes(raw, source=str(path))
        book = res.book
        st = stats(book)
        text1 = render_txt(book)
        # 幂等：把干净产物再洗一遍，正文不该再变
        again = clean_text(text1, source=str(path))
        text2 = render_txt(again.book)
        titles = [c.title for c in book.chapters]
        n = len(titles)
        # 重名标题要按「卷内」算，不能全书一把梭。
        # 实测 400 本里被标「重名标题」的 21 本，19 本是编号按部/按篇重数
        # （跨卷重数，或整本是多部合集、原文里根本没有卷标记），那是书的
        # 本来形态、不是清洗缺陷；只有「同一卷里出现两个同名标题」才是缺陷。
        # 拿全书重名率当判据，等于把合集当故障，噪声九成。
        dup_all, dup_same, volumes, restarts = _duplicate_stats(book.chapters)
        rec.update(
            ok=True,
            chapters=st["chapters"],
            paragraphs=st["paragraphs"],
            chars=st["chars"],
            front_matter=len(book.front_matter),
            max_chapter_paragraphs=max(
                (len(c.paragraphs) for c in book.chapters), default=0
            ),
            front_matter_ratio=round(
                len(book.front_matter) / max(st["paragraphs"], 1), 3
            ),
            duplicate_titles=dup_all,
            duplicate_ratio=round(dup_all / n, 3) if n else 0.0,
            duplicate_same_volume=dup_same,
            duplicate_same_volume_ratio=round(dup_same / n, 3) if n else 0.0,
            volumes=volumes,
            number_restarts=restarts,
            longest_title=st["longest_title"],
            title=book.title,
            author=book.author,
            idempotent_diff=_diff_lines(text1, text2),
            # 原始行数：判"行数很多却切不出章"要用它
            source_lines=len(res.text.splitlines()),
            # 编码证据：chosen 是探测结果，strict 为 False 说明"所有候选都严格
            # 解不通，只能按替换字符打分硬解" —— 解出来必是乱码。
            encoding=cand.encoding,
            encoding_strict=bool(cand.strict_ok),
            encoding_score=round(cand.score, 3),
            # 整本糊成一行的证据（clean 的报告里本来就算好了，这里只是提出来）。
            # 为什么必须提出来：「0 章」有三个完全不同的原因 —— 编码解错、书本来
            # 没有章节标记（短篇集/合集）、整本糊成一行。旧版三者输出一模一样，
            # 复核时得逐本重新体检才能分开。
            # 判「整本一行」仍只信 long_line_count（见下面 flags_of）。
            # suspected 早已收紧成「存在 >3000 字的超长行」才为真，与行数同源；
            # 「一行里出现多个章节编号」只留在 lines_with_multiple_chapters 里
            # 当证据，不参与 suspected —— 正文引用几个章节号很常见，旧口径在
            # 78 字的注释清单上误报过一次。这两个字段都留着给报告用。
            oneline_suspected=bool((res.report.get("oneline") or {}).get("suspected")),
            oneline_long_lines=int(
                (res.report.get("oneline") or {}).get("long_line_count", 0)
            ),
        )
    except Exception as exc:  # noqa: BLE001 —— 批量脚本要跑完整批
        rec["error"] = f"{type(exc).__name__}: {exc}"
    rec["seconds"] = round(time.time() - started, 1)
    return rec


def flags_of(rec: dict) -> list[str]:
    """一条记录该不该人工看。返回命中的标记，空列表表示"看着像本正常书"。"""
    if not rec.get("ok"):
        return ["跑崩"]
    flags: list[str] = []
    n = rec["chapters"]
    # 编码解错排在第一位：它是根因，后面的「0章/章太少/重名标题」都是它的症状。
    # 判据只有「所有候选都严格解码失败」这一条。为什么不再加一条分数阈值：
    # 能严格解开就不会是解码错误 —— 解错必然产生替换符或罕见汉字，那种情况
    # 一定伴随 strict 失败。实测 400 本真实书里，strict 成功且 score<0.2 的是
    # 0 本，加阈值等于加一条永远为假的分支（报告里看着像判据，实际从不生效）。
    # 所以分数只作为记录字段写进 JSON 供人工排查，不参与判定。
    if not rec.get("encoding_strict", True):
        flags.append(f"编码可疑({rec.get('encoding', '?')})")
    if n == 0:
        flags.append("0章")
    # 整本一行：只认"真有超长行"（一行 >3000 字）。0 章 + 超长行 = 结构是糊在
    # 一行里丢的，要重切；0 章但没超长行 = 书本来没章节标记，按约定原样保留。
    # 有章节却也有超长行 → 单独报一个"超长行N"，提示去看那几行，不算 0 章那一档。
    long_lines = rec.get("oneline_long_lines", 0)
    if long_lines > 0:
        flags.append("整本一行" if n == 0 else f"超长行{long_lines}")
    if n < 5 and rec["source_lines"] >= MIN_LINES_FOR_FEW_CHAPTERS:
        flags.append(f"章太少({n})")
    if rec["front_matter_ratio"] > FRONT_MATTER_RATIO and rec["paragraphs"] > 50:
        flags.append(f"前言占比{rec['front_matter_ratio']:.0%}")
    if rec["max_chapter_paragraphs"] > MAX_CHAPTER_PARAGRAPHS:
        flags.append(f"单章{rec['max_chapter_paragraphs']}段")
    # 重名标题：只有「同一卷里出现两个同名标题」才是缺陷。
    # 有卷标记的书按卷内重名率判；没有卷标记但编号明显按部重数（两个以上
    # 「第一章」）的，是合集形态，走 notes 不当异常。
    same_ratio = rec.get("duplicate_same_volume_ratio", 0.0)
    if rec.get("volumes", 0) >= 2:
        if same_ratio > DUPLICATE_TITLE_RATIO and n >= 10:
            flags.append(f"卷内重名{same_ratio:.0%}")
    elif rec.get("number_restarts", 0) < 2:
        # 没有卷标记、编号也没重启：这种重名只能是水印或空壳章节造成的
        if rec["duplicate_ratio"] > DUPLICATE_TITLE_RATIO and n >= 10:
            flags.append(f"重名标题{rec['duplicate_ratio']:.0%}")
    if rec["idempotent_diff"] > 0:
        flags.append(f"幂等差异{rec['idempotent_diff']}行")
    return flags


def notes_of(rec: dict) -> list[str]:
    """不算缺陷、但值得写进报告让人知道一句的形态。

    目前只有一种：整本没有卷标记、编号却按部/按篇重数（两个以上「第一章」）。
    这类书多半是多部合集或短篇集 —— 按项目约定（合集不拆分、无章节书不切章），
    清洗结果就是它该有的样子，不该占用「异常」名额，但也不该悄悄消失。
    """
    if not rec.get("ok"):
        return []
    n = rec["chapters"]
    if (
        n >= 10
        and rec.get("volumes", 0) < 2
        and rec.get("number_restarts", 0) >= 2
        and rec["duplicate_ratio"] > DUPLICATE_TITLE_RATIO
    ):
        return [f"编号按部重数{rec['duplicate_ratio']:.0%}(重启{rec['number_restarts']}次)"]
    return []


def run(paths: list[Path], *, quiet: bool = False,
        name_map: dict[str, str] | None = None, redact: bool = False) -> list[dict]:
    """逐个跑，边跑边报进度（进度走 stderr，结果走返回值）。

    redact=True 时每本记录在进列表**之前**就脱敏：进度行、汇总、report.json
    读的都是同一个 list，只要列表里已经没有原文，三个出口就同时干净，
    不用在每个打印点各判一次（判漏一处就泄一处）。
    """
    out: list[dict] = []
    total = len(paths)
    for i, p in enumerate(paths, 1):
        rec = check_one(p)
        rec["flags"] = flags_of(rec)
        rec["notes"] = notes_of(rec)
        if redact:
            rec = redact_batch_record(rec, name_map=name_map)
        out.append(rec)
        if not quiet:
            mark = "!" if rec["flags"] else "."
            shown = rec["name"] if redact else p.name[:40]
            print(f"[{i:>3}/{total}] {mark} {shown:<42} {rec['seconds']:>5}s",
                  file=sys.stderr, flush=True)
    return out


def summarize(records: list[dict]) -> str:
    """人读的汇总表。异常书排在前面，正常书只留一行统计。"""
    bad = [r for r in records if r["flags"]]
    lines = [f"共 {len(records)} 本，异常 {len(bad)} 本", ""]
    if bad:
        lines.append("异常书：")
        for r in sorted(bad, key=lambda r: r["name"]):
            if r.get("ok"):
                lines.append(
                    f"  {r['name'][:44]:<46} 章{r['chapters']:>4} 段{r['paragraphs']:>6}"
                    f" 前言{r['front_matter']:>4}  | {'、'.join(r['flags'])}"
                )
            else:
                lines.append(f"  {r['name'][:44]:<46} {r['flags'][0]}  {r.get('error', '')[:70]}")
        lines.append("")
    noted = [r for r in records if r.get("notes")]
    if noted:
        lines.append(f"提示 {len(noted)} 本（计入正常书；是书的形态，不是清洗缺陷）：")
        for r in sorted(noted, key=lambda r: r["name"]):
            lines.append(
                f"  {r['name'][:44]:<46} 章{r['chapters']:>4} | {'、'.join(r['notes'])}"
            )
        lines.append("")
    ok = [r for r in records if not r["flags"]]
    if ok:
        avg_ch = sum(r["chapters"] for r in ok) / len(ok)
        lines.append(f"正常书 {len(ok)} 本，平均 {avg_ch:.0f} 章，"
                     f"总耗时 {sum(r['seconds'] for r in records):.0f}s")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="批量真书抽检：找出清洗结果可疑的书")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="扫描目录")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="抽多少本")
    ap.add_argument("--all", action="store_true", help="不抽样，全部跑")
    ap.add_argument("--out", default="", help="报告 JSON 写到哪（默认 backups/ 下带日期）")
    ap.add_argument(
        "--redact", action="store_true",
        help="脱敏：书名/路径/题名/作者/异常消息换成代号与形状描述，输出可整份贴进 AI 会话",
    )
    ap.add_argument(
        "--mask-map", default="",
        help="别名表 JSON（tools/mask.py 产出）。--redact 时不给就退化成内容哈希，"
             "同一本书在不同批次里代号会不同，跨批就对不上账",
    )
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2
    paths = sample_books(root, 0 if args.all else args.limit)
    if not paths:
        print(f"{root} 下没有大于 {MIN_BYTES // 1024}KB 的 TXT", file=sys.stderr)
        return 2

    name_map: dict[str, str] | None = None
    if args.redact:
        if args.mask_map:
            try:
                name_map = load_name_map(args.mask_map)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
        else:
            # 没给表就现场按这批书的顺序编一张，并落到报告旁边：
            # 宁可多写一个文件，也不要"代号每次变"。
            name_map = alias_table([p.name for p in paths], prefix="B")
        # root 路径本身也含中文目录名，一样不能进日志
        print(f"抽样 {len(paths)} 本（已脱敏，代号见报告同目录的 name-map.json）",
              file=sys.stderr)
    else:
        print(f"抽样 {len(paths)} 本（{root}）", file=sys.stderr)

    records = run(paths, name_map=name_map, redact=args.redact)

    out = Path(args.out) if args.out else (
        ROOT / "backups" / f"batch-{time.strftime('%Y%m%d')}" / "report.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "records": records,
        "count": len(records),
        "redacted": bool(args.redact),
    }
    if args.redact:
        # 脱敏模式不落 root 绝对路径：那串字符里就有书名所在的中文目录名。
        payload["root"] = shape(str(root))
        (out.parent / "name-map.json").write_text(
            json.dumps(name_map, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    else:
        payload["root"] = str(root)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(summarize(records))
    print(f"\n明细：{out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
