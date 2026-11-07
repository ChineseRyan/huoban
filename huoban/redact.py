"""脱敏层：把报告里的"原文"换成可核对的**形状描述**。

为什么要有这一层（本项目实测）：清洗与体检的报告会带样例正文，批量抽检会把
书名、路径、正文片段写进 JSON 和日志；这些数据一旦被贴进 AI 会话，就可能触发
提供方的入口内容审查（HTTP 406 / data_inspection_failed），而且审查发生在请求
入口、模型运行之前 —— 整条历史每次全量重放，每次都被拒，会话救不回来。

本模块的立场：**AI 只看元数据，原文的核对留在本地人眼。**
- 删掉的：正文文本、书名/作者、文件路径、异常消息原文。
- 保留的：行号、命中词（来自我们自己的 AD_KEYWORDS，不是书里的字）、布尔标记、
  一切计数、字符数、字符类分布、短指纹。
- 指纹的作用：同一行文本两次脱敏得到同一个描述，可以判断"这行和上次那条是不是
  同一个"，也可以拿它回到本地文件里按行号定位。人拿 path:line 看原文，AI 永远不看。

纯函数：不读文件、不打印。渲染层（cleanreport/report）与 CLI 共用这一份实现，
保证 markdown 与 --json 两条出口脱敏口径一致。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path

# 样例桶里存"原文"的键名。clean.py 的取样逻辑只往这几个键写正文，
# 其余键（line / lines / keywords / has_url）全是可公开的元数据。
_TEXT_KEYS = ("text", "cut", "kept", "result")

# 成对的标题标记。警告语里出现过「第三章 抉择」这种被书名号/直角引号包住的
# 章节标题，脱敏时保留括号本身、只把里面的字换成形状描述 —— 保留括号是为了让
# 人一眼看出"这里原本是个标题"，而不是"这里被程序吞了一段"。
_TITLE_PAIRS = (("「", "」"), ("『", "』"), ("《", "》"), ("〈", "〉"),
                ("“", "”"), ("‘", "’"))

# 路径样子的词：警告与建议动作里可能出现 "打开 xxx.txt 看看"。
_TXT_PATH = re.compile(r"[^\s，。；：、<>|\"'*?]+\.txt", re.IGNORECASE)

_DESC_PREFIX = "〔"
_DESC_SUFFIX = "〕"


def _digest(text: str) -> str:
    """短指纹：8 位十六进制。同一个字符串永远得到同一个值。"""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]


def _classes(text: str) -> str:
    """字符类分布：中文 / 西文数字 / 空白 / 其余符号，各多少个。

    为什么留着这个：判断"这行到底是正文还是分隔线/水印"，靠的往往就是
    字符类构成（正文中文占九成，水印全是 ASCII 与数字），不需要看见字。
    """
    cjk = ascii_word = space = other = 0
    for ch in text:
        if ch.isspace():
            space += 1
        elif ch.isascii() and ch.isalnum():
            ascii_word += 1
        elif "\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff":
            cjk += 1
        else:
            other += 1
    return f"中{cjk} 西{ascii_word} 空{space} 符{other}"


def shape(text: object) -> str:
    """把一段可能敏感的文本换成形状描述：长度 + 字符类 + 指纹。

    空文本要说清楚是"本来就空"，不是"被抹掉了"——否则核对时长度 0 的行会
    看起来像丢了内容。
    """
    s = "" if text is None else str(text)
    if not s:
        return f"{_DESC_PREFIX}空{_DESC_SUFFIX}"
    return (
        f"{_DESC_PREFIX}长{len(s)} {_classes(s)} #{_digest(s)}{_DESC_SUFFIX}"
    )


def _replace_span(match: re.Match, open_ch: str, close_ch: str) -> str:
    inner = match.group(1)
    return f"{open_ch}{shape(inner)}{close_ch}"


def scrub_line(text: object) -> str:
    """一句话里可能夹着书名、章节标题、txt 路径：逐个换成形状描述。

    用在警告语与建议动作这类"我们自己写的句子夹了书里的字"的场合。
    句子主体保留 —— 那部分是规则说明，是 AI 调试时最需要读的东西。
    """
    s = "" if text is None else str(text)
    for open_ch, close_ch in _TITLE_PAIRS:
        pat = re.compile(re.escape(open_ch) + r"([^" + re.escape(close_ch) + r"\n]{1,60})"
                         + re.escape(close_ch))
        s = pat.sub(lambda m, a=open_ch, b=close_ch: f"{a}{shape(m.group(1))}{b}", s)
    s = _TXT_PATH.sub(lambda m: shape(m.group(0)), s)
    return s


def alias_for(text: object, prefix: str = "A") -> str:
    """没有别名表时的确定性代号：同一个路径两次调用得到同一个代号。"""
    s = "" if text is None else str(text)
    return f"{prefix}{_digest(s)[:6]}"


def alias_table(values: Iterable[object], prefix: str = "B") -> dict[str, str]:
    """批量场景的别名表：按传入顺序编号 B01、B02……

    为什么不用纯哈希：人工核对时要能说"B07 那本"，短序号比哈希好念；
    映射关系另存文件，AI 不读那个文件。
    """
    out: dict[str, str] = {}
    for i, v in enumerate(values, 1):
        s = "" if v is None else str(v)
        if s and s not in out:
            out[s] = f"{prefix}{i:02d}"
    return out


def _lookup_alias(name: str, name_map: dict[str, str] | None) -> str | None:
    """按整串、再按 basename 查别名；查不到返回 None。"""
    if not name_map or not name:
        return None
    if name in name_map:
        return name_map[name]
    base = Path(name).name
    if base in name_map:
        return name_map[base]
    return None


def load_name_map(path: object) -> dict[str, str]:
    """读别名表 JSON（名字→代号）。读不到/格式不对就抛 ValueError，绝不静默返回空表。

    为什么不容忍：静默回退会让"我以为脱敏过了"和"其实没脱敏"两种状态
    看起来一模一样 —— 而后者恰恰是要防的事故。
    """
    p = Path(str(path))
    if not p.is_file():
        raise ValueError(f"别名表不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"别名表读不出来：{p}（{type(exc).__name__}）")
    if not isinstance(data, dict):
        raise ValueError("别名表要是一个对象（名字→代号）")
    return {str(k): str(v) for k, v in data.items()}


def redact_diagnose(result: dict, *, name_map: dict[str, str] | None = None) -> dict:
    """M1 体检报告的脱敏：结构跟 clean 的 report 不同，字段各自点名。

    体检结果里带正文的地方（report.py 会原样排进表格）：
    chapters.samples / chapters.first_lines / chapters.last_lines 是 (行号, 文本)
    或纯文本；repeated_lines[].text；ads 两个样例桶的 text。
    actions 是我们自己写的建议句，可能夹了 .txt 路径，走 scrub_line。
    """
    out = dict(result)
    name = str(result.get("file") or "")
    alias = _lookup_alias(name, name_map)
    out["file"] = alias if alias else alias_for(name, "F")

    chapters = out.get("chapters")
    if isinstance(chapters, dict):
        nc = dict(chapters)
        samples = nc.get("samples")
        if isinstance(samples, list):
            nc["samples"] = [
                [pair[0], shape(pair[1])]
                if isinstance(pair, (list, tuple)) and len(pair) == 2
                else shape(pair)
                for pair in samples
            ]
        for key in ("first_lines", "last_lines"):
            if isinstance(nc.get(key), list):
                nc[key] = [shape(x) for x in nc[key]]
        out["chapters"] = nc

    reps = out.get("repeated_lines")
    if isinstance(reps, list):
        out["repeated_lines"] = [
            {**x, "text": shape(x.get("text"))} if isinstance(x, dict) else x
            for x in reps
        ]

    ads = out.get("ads")
    if isinstance(ads, dict):
        na = dict(ads)
        for key in ("whole_line_samples", "inline_samples"):
            if isinstance(na.get(key), list):
                na[key] = [
                    {**x, "text": shape(x.get("text"))} if isinstance(x, dict) else x
                    for x in na[key]
                ]
        out["ads"] = na

    if isinstance(out.get("actions"), list):
        out["actions"] = [scrub_line(a) for a in out["actions"]]
    if isinstance(out.get("warnings"), list):
        out["warnings"] = [scrub_line(w) for w in out["warnings"]]

    out["redacted"] = True
    return out


def redact_batch_record(record: dict, *, name_map: dict[str, str] | None = None) -> dict:
    """批量抽检的一条记录：书名/路径/题名/作者/异常消息全部换成代号或形状描述。

    tools/batch_check.py 的 rec 里同时有 path 与 name（都含书名），还有
    title / author / longest_title（书名与作者名本身）、error（异常消息可能带原文）。
    flags / ok / 各项计数一字不动 —— 那正是 AI 调试要读的全部信息。
    """
    out = dict(record)
    path = str(record.get("path") or "")
    name = str(record.get("name") or "")
    alias = _lookup_alias(name, name_map) or _lookup_alias(path, name_map)
    if alias is None:
        alias = _lookup_alias(Path(path).name if path else name, name_map)
    code = alias if alias else alias_for(path or name, "B")

    if "path" in out:
        out["path"] = code
    if "name" in out:
        out["name"] = code
    # 题名/作者/最长标题换成形状描述而不是文件代号：脱敏后仍要能看出
    # "解析出来的题名是不是空的、跟上次跑是不是同一个"（同 digest = 同一结果）。
    for key in ("title", "author", "longest_title"):
        if key in out:
            out[key] = shape(out[key])
    if "error" in out:
        err = str(out["error"] or "")
        head = err.split(":", 1)[0]  # 异常类型名是可读的 ASCII，保留
        out["error"] = f"{head}: 〔原文已脱敏 #{_digest(err)[:8]}〕"

    out["redacted"] = True
    return out


def redact_report(report: dict, *, name_map: dict[str, str] | None = None) -> dict:
    """返回一份**新** report：所有原文字段换成形状描述，所有计数原样保留。

    入参不被修改（渲染层与调用方都拿着同一份 dict，就地改会把后面的
    人读报告也一起毁了）。样例桶的条数保持不变，所以 cleanreport 的
    "共 N 条，此处展示前 M 条" 这类对账语言在脱敏后依然成立。
    """
    out = dict(report)
    alias = _lookup_alias(str(report.get("file") or ""), name_map)

    files = out.get("file")
    out["file"] = alias if alias else alias_for(files, "F")

    samples = out.get("samples")
    if isinstance(samples, dict):
        new_samples: dict = {}
        for bucket, items in samples.items():
            if not isinstance(items, list):
                new_samples[bucket] = items
                continue
            new_items = []
            for item in items:
                if not isinstance(item, dict):
                    new_items.append(item)
                    continue
                ni = dict(item)
                for key in _TEXT_KEYS:
                    if key in ni:
                        ni[key] = shape(ni[key])
                new_items.append(ni)
            new_samples[bucket] = new_items
        out["samples"] = new_samples

    # 全量删除清单（§7 adjust 实装时新增）：每条都带"前 60 字"，是真书正文，
    # 必须与 samples 同口径换形状描述。漏了这一处，--redact 的输出里就有正文了。
    dropped = out.get("dropped_lines")
    if isinstance(dropped, list):
        new_dropped = []
        for item in dropped:
            if not isinstance(item, dict):
                new_dropped.append(item)
                continue
            ni = dict(item)
            for key in _TEXT_KEYS:
                if key in ni:
                    ni[key] = shape(ni[key])
            new_dropped.append(ni)
        out["dropped_lines"] = new_dropped

    stats = out.get("stats")
    if isinstance(stats, dict) and "longest_title" in stats:
        ns = dict(stats)
        ns["longest_title"] = shape(ns["longest_title"])
        out["stats"] = ns

    # adjust.ignored[].line：行号数据错时这里存的是 str(用户贴进来的东西)，
    # 可能是 "abc"、"[[1,2]]"、一段被粘错的文本。它不是书里的字，是用户自己
    # 输入的垃圾，但 --redact 的口径是"整条命令的输出都不含可疑字串"，
    # 所以非纯数字的一律换成形状描述。field/reason 是模板文案，不动。
    # 同一条垃圾还会被"没命中"警告逐行点名带进 warnings（形如
    # 「保护 第 abc 行」），所以先把它从 ignored 里收集出来，scrub 时一并替换。
    junk_lines: list[str] = []
    adjust = out.get("adjust")
    if isinstance(adjust, dict):
        ignored = adjust.get("ignored")
        if isinstance(ignored, list):
            na = dict(adjust)
            new_ignored = []
            for item in ignored:
                if not isinstance(item, dict):
                    new_ignored.append(item)
                    continue
                ni = dict(item)
                line = ni.get("line")
                if isinstance(line, str) and not line.isdigit():
                    ni["line"] = shape(line)
                    junk_lines.append(line)
                new_ignored.append(ni)
            na["ignored"] = new_ignored
            out["adjust"] = na

    def _scrub_warning(text: str) -> str:
        for junk in junk_lines:
            if junk in text:
                text = text.replace(junk, shape(junk))
        return scrub_line(text)

    if isinstance(out.get("warnings"), list):
        out["warnings"] = [_scrub_warning(w) for w in out["warnings"]]

    # 同一句话还从第二条路出去：clean.py 会同时产出 warnings（字符串列表）
    # 与 warning_items（带 code/level 的字典列表），界面上用的是后者。
    # 只洗 warnings 的话，--redact 的报告里 warning_items[].text 原样带着
    # 用户粘错的那段正文 —— 这是唯一一处真泄漏点（tests/test_redact.py:218）。
    items = out.get("warning_items")
    if isinstance(items, list):
        out["warning_items"] = [
            dict(it, text=_scrub_warning(it["text"]))
            if isinstance(it, dict) and isinstance(it.get("text"), str)
            else it
            for it in items
        ]

    out["redacted"] = True
    return out
