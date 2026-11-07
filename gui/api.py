"""js_api 实现：把清洗跑在后台线程里，前端只轮询。

设计约束（docs/gui-design.md §5）：

1. `util.py` 的 `js_bridge_call` **每次 JS 调用都新起一个线程**，所以 Python 侧
   不能假设串行，也不能在 API 方法里直接跑清洗 —— 一本 8 MB 的书要几十秒，
   会把调用线程占住，前端连"正在处理"都画不出来。
2. 统一规则：**登记 → 起 worker → 立即返回 job_id**。前端只轮询 `job_poll`。
3. `job_poll` 只读、无副作用：前端 300ms 轮询一次，不能每轮都重算报告。

这一层不修改 `huoban/` 里的任何东西，只调用。G1 阶段只用到 `clean_file`。

关于"不把正文送到前端"：这条约束只针对 ② 结果页 —— 它只有数字和警告，一处正文
都不显示，所以 `_summary()` 明确**不转发 samples**，少送一份就少一份泄漏面。

③ 复核页是另一回事：那一页的整个任务就是让人核对自己书里的删改（决定24）。
送形状描述（`redact.shape()` 的 `〔长N 中N 西N〕`）等于让人对着一串"这是什么"
猜自己书里写了什么，根本没法核对。所以复核页给真原文，且**按需给**：列表只送
60 字摘要，点开某一行才取前后文（`review_detail`）。`redact` 那条铁律是"真书正文
不进 AI 会话"，界面不出门，不受它管。
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from huoban import encoding as enc_mod
from huoban.clean import clean_file
from huoban.diagnose import judge_ad_line, match_chapter
from huoban.fonts import missing_chars
from huoban.model import chapter_groups
from huoban.model import save as save_book
from huoban.model import stats as real_stats
from huoban.model import validate_items as validate_items

# 输入上限：50 MB。再大就别在界面里等了，先去命令行跑（设计文档 §10 验收 5）。
MAX_INPUT_BYTES = 50 * 1024 * 1024

# 两条入口共用同一条产线（§12）：TXT 走清洗，EPUB 走导入（`epubread`）。
# 导入这条路不跑 clean.py —— 章节边界由 OPF spine + nav/NCX 白送，
# 目录块与广告行基本不存在，再洗一遍只会把正常排版当脏数据。
TXT_SUFFIXES = {".txt", ".text"}
EPUB_SUFFIXES = {".epub"}
INPUT_SUFFIXES = TXT_SUFFIXES | EPUB_SUFFIXES

# 导入 EPUB 时把插图落在这个子目录里（CLI reflow 也叫 images/，§6）。
IMAGES_DIR = "images"

# 产物目录后缀。CLI 用 `<名>_clean/`，GUI 用 `<名>_gui/`，两者不撞（§6）。
OUTDIR_SUFFIX = "_gui"

# Windows 文件名非法字符。书名是从正文里猜出来的，可能带 / 或 : 之类。
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str, *, fallback: str = "未命名") -> str:
    """把书名变成能当目录名的字符串。

    为什么不用 `quote()`：产物目录是给人看的，%E9%81%93 这种没人想在里面找文件。
    只替换真正非法的字符，中文、空格、括号一律保留。
    """
    cleaned = _BAD_NAME.sub("_", (name or "").strip()).strip(" .")
    # Windows 保留名（CON/PRN/NUL...）当目录名会失败，加下划线绕开。
    if cleaned.upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned += "_"
    return cleaned[:80] or fallback


def _unique_dir(parent: Path, stem: str, suffix: str) -> Path:
    """在 parent 下找一个还不存在的目录名：`stem+suffix`、`stem (2)+suffix`、`stem (3)+suffix`…

    **为什么不直接用 `stem+suffix`**：两个不同来源的书常常解析出同一个书名
    （《全集》《无标题》这类），或者就是同一个文件名再来一次。直接复用那个目录，
    上一次留下的 `book.json` / `report.json` 会被默默覆写；更麻烦的是
    `images/` 里的旧图还在 —— 打包时 `write_epub(image_dir=…)` 会把它们**一起塞进
    新 EPUB**，成品里凭空多出别人的插图。
    目录名带序号是可见、可解释的：用户看到 `死灵之书 (2)_gui` 就知道是第二次导入。
    """
    candidate = parent / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    n = 2
    while n < 1000:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1
    # 1000 个同名目录还把名字占满了，就退回带时间戳的兜底（绝不覆写）。
    return parent / f"{stem} ({datetime.now():%Y%m%d-%H%M%S}){suffix}"


def _dir_owner(outdir: Path) -> str:
    """读产物目录里 `book.json` 的 source 字段，判断这个目录属于哪个输入文件。

    返回归一化后的输入路径（`""` 表示没有 book.json 或读不出来）。
    为什么要这一步：`_unique_dir` 只会挑**还没被占用**的名字，
    但"同一个输入连续导入两次"（这是常规操作，比如改了源文件再拖一次）
    应当**复用**原目录而不是增生 `(2)`、`(3)`…；而"两个不同文件解析出同一个书名"
    必须**另起目录**。两种情况在磁盘上长得一模一样，只能靠 book.json 里记的
    source 区分 —— `save_book` 写的就是 `book.source`（`model.to_dict` 的 "source"）。
    """
    path = outdir / "book.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    src = data.get("source")
    if not isinstance(src, str) or not src.strip():
        return ""
    try:
        return str(Path(src).resolve())
    except (OSError, ValueError):
        return ""


def _import_outdir(src: Path, title: str) -> Path:
    """给 TXT 导入挑产物目录：同源重导复用，异源撞名另起。

    三条规则，按顺序：
      1. `<书名>_gui` 不存在 → 直接用它。
      2. 存在、且 `book.json` 记的 source 正是这个输入 → **复用**（重跑/改源再导）。
      3. 存在、但属于别的输入（或 book.json 坏了/没有）→ 当成占用，找 `(2)`、`(3)`…
         并且 **`(2)` 之后也要按规则 2 检查归属** —— 否则第三次导入会一直增生。

    与 `_run_import_epub` 的差别：EPUB 那条每次导入都是一份新的暂存目录（解析过程中
    要把图片落到 `images/`，没法先问归属），所以用纯 `_unique_dir`；TXT 这条清洗
    是纯内存的，落盘前就知道书名，能先看一眼归属。
    """
    stem = safe_name(title)
    base = src.parent / f"{stem}{OUTDIR_SUFFIX}"
    mine = _resolve_or_empty(src)

    candidate = base
    if not candidate.exists():
        return candidate
    if _dir_owner(candidate) == mine:
        return candidate

    n = 2
    while n < 1000:
        candidate = src.parent / f"{stem} ({n}){OUTDIR_SUFFIX}"
        if not candidate.exists():
            return candidate
        if _dir_owner(candidate) == mine:
            return candidate
        n += 1
    return src.parent / f"{stem} ({datetime.now():%Y%m%d-%H%M%S}){OUTDIR_SUFFIX}"


def _resolve_or_empty(path: Path) -> str:
    """`Path.resolve()` 包装：文件不可达时退回落空的绝对路径，绝不抛。"""
    try:
        return str(path.resolve())
    except (OSError, ValueError):
        return str(path.absolute())


@dataclass
class Session:
    """一次导入的会话状态。

    为什么不把事情全塞进 payload：G4 复核要拿同一个 `CleanResult` 带 adjust 重跑，
    G2 打包要用同一个 `Book`。重建这两样都得再读一遍原文、再洗一遍，
    那是几十秒的事，用户会以为卡死了。
    """

    source: Path
    result: object = None  # CleanResult
    outdir: Path | None = None
    adjust: dict | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Job:
    """一个后台任务。`payload` 只有 done 之后才有值，且只算一次。

    `stage` 记这个 job 在走哪一段（"import" / "build"），
    只为了一件事：出错时知道该说"清洗失败"还是"打包失败"。
    光看异常类型判不出来 —— 打包时 `book.json` 被占用抛的是 `PermissionError`。
    """

    job_id: str
    done: bool = False
    error: str = ""
    # 同一件事的两种说法：error 是给用户看的人话，error_detail 是给开发者看的
    # 类名 + 原文。界面把后者收进「技术细节」，默认不展开 —— 一整屏 traceback
    # 糊到用户脸上，除了让人以为程序坏了没有任何用处。
    error_detail: str = ""
    error_title: str = ""
    payload: dict | None = None
    thread: threading.Thread | None = None
    stage: str = "import"
    # 用户点了取消。worker 没法被真正掐断（clean_file 是一次同步调用，
    # 中途没有检查点），所以只立旗子，由 worker 在写盘前后各看一眼：
    # 写盘前看到 = 一个字节都不写；写盘后才看到 = 把刚产出的东西收进回收站。
    cancel_requested: bool = False
    cancelled: bool = False
    # 这次失败是"默认拒收、但用户可以硬来"的那一类（目前只有整本糊行）。
    # 界面看到它就把错误页的「仍然继续」亮出来，点了走 force_import 重跑。
    can_continue: bool = False


class InputError(Exception):
    """"这个文件根本不能处理"，与"处理到一半失败"分开。

    为什么要分：标题不一样。拖进一个 .epub 说"清洗失败"是误导 —— 清洗压根
    没开始。界面按 `error_title` 决定写"这个文件不能处理"还是"清洗失败"。
    """


class Cancelled(Exception):
    """用户点了取消。**不是错误**，不往界面上报，只用来把控制流拉出 worker。"""


class OneLineBook(InputError):
    """整本书糊在一两个超长行里：原文压根没有换行，段落是硬折出来的。

    为什么拒：本项目铁律是"不给没有换行的文本猜分段/断句"。硬猜出来的段落
    每一段都可能断在句子中间，用户拿到一本通篇错断的书，比拿到一屏说明更糟。

    为什么仍然抛 InputError 的子类：清洗确实没失败，失败的是"这本书的原文形态"，
    与文件不存在/0 字节是同一类。但 error_title 要单独说，见 _spawn。
    继承而不是并列，是为了 _friendly_error 那一条 str(exc) 直接复用。
    """


# 光有类名不够用：用户看不懂 PermissionError，看得懂「文件被别的程序占着」。
# 只登记**知道该怎么办**的那几种，其余落到 _friendly_error 的最后一句。
# 键用类名字符串而不是类型对象：zlib.error 这种要从异常自身取，import 它反而多一条依赖。
_PLAIN_TRAP = {
    "PermissionError": "文件被别的程序占着，或者没有写入权限。"
                       "关掉正在读它的软件（阅读器、网盘同步、杀毒扫描），再试一次。",
    "FileNotFoundError": "找不到这个文件了 —— 它可能被挪走或改名了。重新拖一次。",
    "IsADirectoryError": "这个位置是个目录，不是文件。",
    "NotADirectoryError": "路径中间有一段不是目录，检查一下输出目录。",
    "OSError": "读写文件的时候出错了：磁盘满了、路径太长，或者那个位置不能写。",
    "UnicodeDecodeError": "这个文件的编码认不出来，解不出正文。",
    "UnicodeEncodeError": "写文件的时候有些字符存不进去。",
    "BadZipFile": "这个 EPUB 的包结构坏了，打不开。",
    "zlib.error": "这个 EPUB 里有损坏的压缩内容，解不开。",
    "ValueError": "这本书的结构在清洗过程中变得不自洽了（多半是切章切出了问题）。",
    "KeyError": "处理过程中读到了缺失的字段 —— 这多半是程序的问题，不是书的问题。",
}


def _technical(exc: BaseException) -> str:
    """给开发者看的那一行：类名 + 原文。只进「技术细节」，默认不展开。"""
    msg = str(exc)
    name = type(exc).__name__
    return f"{name}: {msg}" if msg else name

def _recycle(path: Path) -> None:
    """把这次任务刚产出的东西送进回收站 —— 取消 = 这次没发生。

    为什么不直接删：本项目铁律是"绝不删除"。这些文件确实是这次刚产出的，
    但用户手一滑点错就是不可逆的损失，所以一律走回收站，误点还能捞回来。
    非 Windows、或 shell 调用失败，就静静留着 —— 多一个目录远好过删错东西。
    """
    try:
        if not path.exists():
            return
        import ctypes
        from ctypes import wintypes

        class _SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        op = _SHFILEOPSTRUCTW()
        op.wFunc = 3  # FO_DELETE
        # pFrom 必须以双 \0 结尾
        op.pFrom = str(path) + "\0\0"
        # SILENT | NOCONFIRMATION | ALLOWUNDO | NOERRORUI
        op.fFlags = 0x0004 | 0x0010 | 0x0040 | 0x0400
        ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception:  # noqa: BLE001 - 收摊失败不值得把"取消"变成一条报错
        pass


def _content_state(chars: int, images: int) -> str:
    """这本书打开以后有没有东西可读 —— ② 页据此决定要不要顶一条提示。

    三种状态必须分开，不能合成一句"成功"：
    - `text` 有正文，正常结果；
    - `empty` 0 字 0 图：多半是拖错了文件或本来就是空壳，打包会得到一本空白 EPUB；
    - `images_only` 0 字但有图：合法（图集、漫画），但它不是小说，
      说成"拖错了文件"是假话，什么都不说又会让用户在 0 字面前发愣。

    这一层只出结论不出句子，句子在前端拼 —— 与 `kind` 的分工一致。
    """
    if chars > 0:
        return "text"
    return "images_only" if images > 0 else "empty"


# 告警的级别顺序（决定32：先丢东西的，再结构错的，最后只是难看的）。
# 级别由产生告警的那一层判定（huoban/model.py 的 LEVEL_*），这里只管排序与
# 合并 —— 界面不猜级别，也不重新分类。级别不显示成字，只决定先后。
_ALERT_LEVELS = ("loss", "order", "layout")

# 每个 code 的短说法。合并计数只能用短句（决定31：同类合并、按类计数）：
# 原句是"第 37 章「xxx」一个段落都没有"这种带具体位置的句子，同样的问题出现
# 二十次就是二十行几乎一样的话。位置留给复核页，结果页只说"哪一类、有几处"。
#
# 数词放在名词前（"有 4 章一个段落都没有"），而不是拖在句尾（"…，共 4 处"）：
# 中文里后者读起来像附表。{n} 就是 count 字段的合计。
# 简报体：**这时用户已经改不了任何东西了**。这里的每一句话都
# 只带来情绪、不带来动作，所以越短越好、越弱越好 —— 不是"把问题说清楚，责任
# 尽到"，而是"给他一张便条"。一句话不超过十几个字，能省的主语全省：
# 不写"有 N 章……"里的"有"以外的动词，不解释后果（后果就是书还能读）。
_ALERT_BRIEF = {
    # 不可不察：东西丢了，得回去看原文件
    "no_chapters": "整本没切出章节",
    "chapter_empty": "{n} 章没有正文",
    "chapter_blank": "{n} 章全是空行",
    "oneline": "整本糊成了一行",
    # 请君端详：结构错了，目录会跳错、调整没生效
    "empty_title": "{n} 章标题是空的",
    "bad_level": "{n} 章层级不合法",
    "number_dup": "{n} 章编号重复",
    "number_decrease": "{n} 章编号是倒的",
    "ad_title": "{n} 个标题夹着广告",
    "toc_unsure": "开头像目录块，没敢删",
    "toc_empty_prefix": "开头 {n} 章只有标题、没有正文",
    "adjust_ignored": "{n} 处调整没生效",
    "adjust_unmatched": "{n} 处调整没落点",
    "adjust_no_hit": "{n} 处调整一处没中",
    "front_paragraphs": "第一章前漏了 {n} 段",
    # 请您留意：排版瑕疵，书照读
    "encoding_empty": "没记下原编码",
    "suspicious": "{n} 处可疑字符",
    "bad_kinds": "{n} 段类型不认识",
    "image_name": "{n} 张图没有文件名",
    "caption_on_text": "{n} 段图说没有图",
    "bad_anchor": "{n} 个锚点不合法",
    "bad_ref": "{n} 处引用指空",
    "ad_inline": "{n} 处行内广告没切掉",
    # —— 打包期才知道的（决定30）。这些只能在 ⑤ 出现：打包之前无从知道，
    #    而且它们都还有救（装字体、换封面图、装 Calibre 再打一次），不是死账。
    "bad_level_num": "{n} 章层级不是数字",
    "bad_level_range": "{n} 章层级越界",
    "bad_xhtml": "{n} 章 XHTML 有错",
    "no_chapters_or_matter": "只打出了信息页",
    "no_chapters_split": "前言后记并成了一页",
    "cover_ext": "{n} 张封面认不出格式",
    "cover_read": "{n} 张封面读不出来",
    "cover_empty": "{n} 张封面是空文件",
    "cover_magic": "{n} 张封面文件头不对",
    "font_none": "没有可内嵌的中文字体",
    "font_subset": "{n} 套字体没子集化",
    "font_gap": "{n} 种字不在字体里",
    "font_not_ofl": "{n} 套字体不是开源的",
    "font_absent": "{n} 套字体文件不存在",
    "image_missing": "{n} 张插图没进包",
    "meta_problem": "书信息没存上",
}

# 一级里最多并列几件事，多的直接不写（不许告诉用户"另有 N 类" ——
# 界面没有第二个入口去展开它，这句话只会让人好奇又够不着；这一页的告警本来
# 也不改变任何决定，说不全就少说）。
_ALERT_MAX_PARTS = 2

# 封顶之后谁有资格被挤掉？这几条无论同类里排第几都要留在明面上 ——
# 它们说的不是"哪一章有点问题"，是"这本书整个不对"。
_ALERT_TOP = (
    "no_chapters", "no_chapters_or_matter", "no_chapters_split",
    "oneline", "bad_xhtml", "chapter_empty", "chapter_blank", "font_none",
)

# 打包层的告警是给人看的一句话，不带 code（epub.py / fonts.py 不引界面概念）。
# 这里靠关键片段认领：认出来就能并进"同类合并"，认不出来就原样带出 ——
# 宁可长一点，不可吞掉。改打包层的措辞会让对应条目退化成"原样带出"，
# 不会静默丢失。
_BUILD_ALERT_RULES = (
    ("契约外的段落类型", "bad_kinds", "layout"),
    ("不是数字（按章级处理）", "bad_level_num", "order"),
    ("不在 1/2 之内（按卷级处理）", "bad_level_range", "order"),
    ("不是格式良好的 XML", "bad_xhtml", "loss"),
    ("没有任何章节，也没有前言后记", "no_chapters_or_matter", "loss"),
    ("没有切出章节", "no_chapters_split", "order"),
    ("标题为空：目录里会出现一个空白条目", "empty_title", "order"),
    ("扩展名", "cover_ext", "layout"),
    ("读不出来（", "cover_read", "layout"),
    ("是个空文件", "cover_empty", "layout"),
    ("文件头对不上", "cover_magic", "layout"),
    ("没找到可内嵌的开源中文字体", "font_none", "layout"),
    ("字体子集化没能完成", "font_subset", "layout"),
    ("补字字体", "font_subset", "layout"),
    ("子集化失败", "font_subset", "layout"),
    ("不在内嵌字体里", "font_gap", "layout"),
    ("不是 OFL 开源字体", "font_not_ofl", "layout"),
    ("指定的字体", "font_absent", "layout"),
    ("插图没能读进来", "image_missing", "loss"),
    ("book.json", "meta_problem", "layout"),
)


def _build_alert_items(warnings: list[str], problems: list[dict] | None) -> list[dict]:
    """⑤ 完成页的告警：打包期才知道的 + 章节树里"东西真丢了"的那几条。

    决定30 是这一段的全部理由：**早就知道的不在这儿说**。章节树的层级不合法、
    标题为空、编号重复这些，在 ② 结果页就已经明摆在用户眼前过；到这里书已经
    落盘，再说一遍只是让最后一页变长，不改变任何决定。所以这里只留两类：
    打包之后才可能知道的（字体、封面、插图），以及"内容真的少了"（数据丢失级）。
    """
    items: list[dict] = []
    for w in warnings or []:
        code, level = "", ""
        for needle, c, lv in _BUILD_ALERT_RULES:
            if needle in w:
                code, level = c, lv
                break
        if not code:
            items.append({"code": "", "level": "layout", "count": 1, "text": w})
            continue
        items.append({"code": code, "level": level, "count": 1, "text": w})
    # 章节树里只挑 loss 级：一个段落都没有 / 全是空行 / 整本没切出章节。
    for it in problems or []:
        if it.get("level") != "loss":
            continue
        items.append({
            "code": it.get("code") or "",
            "level": "loss",
            "count": 1,
            "text": it.get("text") or "",
        })
    return items


def _merge_alerts(items: list[dict]) -> list[dict]:
    """把一条条 {code, level, count, text} 收成最多三行（决定31/32）。

    每级一行；同一级里同类的合并成一句带计数的短话。返回
    [{"level", "text"}]，顺序固定：先丢东西的，再结构错的，最后只是难看的。
    一级没有问题就不出这一行 —— 干净的书这里什么都不显示。
    """
    groups: dict[str, dict] = {}
    for it in items:
        code = it.get("code") or ""
        # 有的告警一条就代表好几处（"行内夹广告 3 行"），产生它的那一层知道
        # 这个数，写在 count 里；没写的按一条算。
        n = int(it.get("count") or 1)
        g = groups.get(code)
        if g is None:
            groups[code] = {
                "level": it.get("level") or "layout",
                "count": n,
                "text": it.get("text") or "",
            }
        else:
            g["count"] += n
    out = []
    for level in _ALERT_LEVELS:
        picked = []
        for code, g in groups.items():
            if g["level"] != level:
                continue
            brief = _ALERT_BRIEF.get(code)
            text = g["text"] if brief is None else brief.format(n=g["count"])
            # 先按"这本书整个不对"排，再按出问题的处数排 —— 简报只有一两行，
            # 排在前面的才有机会被看见。
            picked.append((0 if code in _ALERT_TOP else 1, -g["count"], text))
        picked.sort(key=lambda p: (p[0], p[1]))
        # 多的直接砍掉，不留"另有 N 类"这种半句话。
        parts = [p[2] for p in picked[:_ALERT_MAX_PARTS]]
        if parts:
            out.append({"level": level, "text": "；".join(parts)})
    return out


def _alerts(report: dict, chapters: dict) -> list[dict]:
    """② 结果页的告警：清洗报告的 warning_items + 章前残留段。"""
    items = list(report.get("warning_items") or [])
    # 章前残留段不是章节树的毛病，是切章形态的毛病（决定13）。清洗报告里它只是
    # 一个计数，界面上得跟"目录会跳错"那类并排，所以在这里补成一条。
    front = int(chapters.get("front_paragraphs") or 0)
    if front > 1:
        items.append({
            "code": "front_paragraphs", "level": "order", "count": front, "text": "",
        })
    return _merge_alerts(items)


def _summary(report: dict, *, outdir: Path) -> dict:
    """结果页要的那一份数据。**不含 samples**，理由见模块注释。"""
    oneline = report.get("oneline") or {}
    stats = report.get("stats") or {}
    return {
        "file": report.get("file") or "",
        "name": Path(report.get("file") or "").name,
        "outdir": str(outdir),
        # 报告与目录一起给，前端不用自己拼路径（拼接字符串最容易在 Windows
        # 上写死一个反斜杠，换个平台就散）。
        # 指向 report.json 而不是 clean-report.md：后者已经不生成了（决定17），
        # 界面上的"看报告"也没了；这份机器可读的照旧落盘，只给排查用。
        "report_path": str(outdir / "report.json"),
        "encoding": report.get("encoding") or {},
        "size_bytes": report.get("size_bytes") or 0,
        "lines": report.get("lines") or {},
        "stats": stats,
        "chapters": report.get("chapters") or {},
        "dropped": report.get("dropped") or {},
        "fixed": report.get("fixed") or {},
        "merged": report.get("merged") or {},
        "verse": report.get("verse") or {},
        "adjust": report.get("adjust") or {},
        "warnings": list(report.get("warnings") or []),
        # 结果页只看这个：按级别合并成最多三行（决定31/32）。
        "alerts": _alerts(report, report.get("chapters") or {}),
        # TXT 这条路永远没有插图，images 恒为 0。
        "content_state": _content_state(int(stats.get("chars") or 0), 0),
        # 整本糊成一行是独立病灶，结果页要能单独顶一条横幅（§4 要点）。
        "oneline": {
            "suspected": bool(oneline.get("suspected")),
            "long_line_count": oneline.get("long_line_count") or 0,
            "lines_with_multiple_chapters": oneline.get("lines_with_multiple_chapters") or 0,
        },
    }


def _book_meta(book) -> dict:
    """④ 元数据页要的初始值。

    `book.title` / `book.author` 是 clean.py 从正文首行猜出来的（猜不出会退回
    文件名），所以这里一定给得出东西 —— 但猜出来不等于对，④ 页把它当**预填值**
    而不是事实，用户可改。

    `paragraphs` 与 `_epub_summary` 必须同口径（用 model.stats），否则用户在 ②
    页看到「24 段」、翻到 ⑤ 页变成「14 段」会以为丢了内容 —— 实测就是这样：
    book.json 里的段落对象含 10 个空行占位段，而写进 EPUB 的 `paragraphs` 只数
    真正输出的段落元素，空行折叠成 `<div class="sep">` 不计。两个数字都对，
    但一个界面里不能出现两种"段"。
    """
    stats = real_stats(book)
    return {
        "title": book.title or "",
        "author": book.author or "",
        "language": "zh-CN",
        "chapters": stats.get("chapters", len(book.chapters)),
        "paragraphs": stats.get("paragraphs", 0),
        # 有没有原书封面决定 ④ 页封面区给不给「用原书封面」这个动作。
        # 由调用方按会话的 extra 覆盖（这里给默认 False，TXT 那条路永远没有）。
        "has_source_cover": False,
    }


def _meta_author(meta: dict, book) -> str:
    """④ 页作者框是唯一真相：请求里带了 author 键，就用框里的值
    （清空 = 用户明确不写作者，不回落清洗时猜的）；根本没带这个键才回落
    book.author。预览封面、成品封面、EPUB 打包三处共用这一条规矩，
    否则会出现「预览是猜的、打出来没有」这种对不上的情况。
    """
    if "author" in meta:
        return str(meta.get("author") or "").strip()
    return (getattr(book, "author", "") or "").strip()


# 付梓动画的字模取多少个字。一个屏幕同时在场的字模不到 20 枚；取 48 个是
# 绕一圈的量，再多也没人记得住顺序。48 个必须是 48 个**互不相同**的字 ——
# 同一个字印两遍，看着就像画面卡住了（试用反馈）。
GLYPH_COUNT = 48

# 从全书的哪个位置起找这段字。取 40% 处：开头是前言、目录、第一回的开场，
# 常常整段是"却说……"这类套路句；40% 处已经在正文中段，随便哪段都像"这本书"。
GLYPH_START_RATIO = 0.4


def _is_han(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _book_glyphs(book, n: int = GLYPH_COUNT) -> str:
    """从这本书的正文里抽**一段真实文字**，给付梓那一屏的字模压印用。

    为什么是一段真话而不是"高频字"：高频字每本书都是"的一了在"，两本书印
    出来看不出区别 —— 只有从书里抽一段文字，才能做到每本书都不一样。
    字模要的是"这本书在印什么"，那就印它自己的一段话。

    三条规矩：
    1. **优先只从 chapters 里取**，不碰 front_matter —— 书名页、目录、
       "本书由某某整理"都在前言区，印上去就难看了。但整本没切出章节时
       （无章节标记的短篇、或旧版认不出的装饰边框标题）正文全落在
       front_matter，这时必须退而求其次也取它：有真字可印，远好过前端
       回落《兰亭集序》那种每本书都一样的假字。
    2. **只取 body 段**：标题、诗词、注释、分隔线都不是正文的语气。
    3. **从 40% 处开始找**，从那里往后一段一段地把汉字攒起来（标点不进字模：
       字模是一枚一枚的活字，没有标点那一枚）。
    4. **不重复**：中文段落里 48 字必然撞字，所以字模是攒出来的**互不相同**
       的汉字，不是截取一段话。前端那侧在字模表里绕圈取字，表里没有重字，
       绕一圈也不会把同一个字再印第二遍。

    同一本书每次抽到的完全一样 —— 每次付梓都换一套字，会让人以为是随机装饰。
    """
    paras = [
        para.text
        for chapter in book.chapters
        for para in chapter.paragraphs
        if para.kind == "body" and para.text
    ]
    if not paras:
        # 0 章书：正文全在 front_matter，从这里也抽一段真话。
        paras = [
            para.text
            for para in getattr(book, "front_matter", []) or []
            if getattr(para, "kind", "body") == "body" and para.text
        ]
    if not paras:
        return ""
    start = int(len(paras) * GLYPH_START_RATIO)
    ordered = paras[start:] + paras[:start]

    # 全书（或前言区）没有一段够长时接着往后攒，所以两条合成一条循环：
    # 攒够 n 个互不重复的汉字就收工。
    out: list[str] = []
    seen: set[str] = set()
    for text in ordered:
        for ch in text:
            if _is_han(ch) and ch not in seen:
                seen.add(ch)
                out.append(ch)
                if len(out) >= n:
                    return "".join(out)
    return "".join(out)


def _epub_summary(
    result,
    outpath: Path,
    book,
    problems: list[dict],
    *,
    cover_path=None,
    meta_problem: str = "",
) -> dict:
    """⑤ 完成页要的那一份数据。

    `problems` 收的是 `model.validate_items()` 的原始结构（每条 {code, level,
    text}），不是 validate() 那句纯文字 —— 决定32 要求告警分级，级别在
    model.py 那一层就判好了，界面不许重新猜。传纯文字进来会让每一条都退化成
    layout 级，等于没有分级。

    `cover_path` 是这次真正打进包里的那张封面（_cover_for_build 已经落盘），
    在这里读成预览图 —— 完成页要让人看见"这本书长什么样"，而封面是这次交付里
    唯一一件看得见摸得着的东西。读不出来就是空串，页面退化成无封面。
    """
    try:
        size = outpath.stat().st_size
    except OSError:
        size = len(result.data)
    # 段数用 model.stats 口径，和 ②④ 两页一致（见 _book_meta 注释）。
    # 不用 result.paragraphs：它数的是 XHTML 里的段落元素，空行折叠后不计。
    stats = real_stats(book)
    warnings = list(result.warnings)
    if meta_problem:
        warnings.append(meta_problem)
    return {
        "epub": str(outpath),
        "name": outpath.name,
        # 文件名与扩展名分开：完成页把书名当大标题，"·epub" 单独做小字
        # （决定35：27px 的大字不该是 `xxx.epub`，扩展名对读者零信息）。
        "stem": outpath.stem,
        "size_bytes": size,
        "entries": len(result.files),
        "chapters": result.chapters,
        "paragraphs": stats.get("paragraphs", result.paragraphs),
        "title": book.title or "未命名",
        "author": book.author or "",
        "fonts": len([n for n in result.files if n.startswith("OEBPS/Fonts/")]),
        "warnings": warnings,
        # 章节树的毛病不影响打包（书能读），但要说出来：打出来了 ≠ 章节切对了。
        # 这里留纯文字给别处用；分级合并后的说法在 alerts 里（决定31/32）。
        "problems": [it.get("text") or "" for it in problems or []],
        "alerts": _merge_alerts(_build_alert_items(warnings, problems)),
        # 完成页的主角之一。空串 = 没封面（用户选了"不要"或图读不出来）。
        "cover": _cover_preview(cover_path),
        "outdir": str(outpath.parent),
        # 两个数字都留着：写进 XHTML 的段落元素数，用来发现"段落被谁吃掉了"。
        "rendered_paragraphs": result.paragraphs,
        # AZW3 是可选的第二步，由 _run_build 填进来：
        # 成功时是 {path, name, size_bytes, seconds}，失败时是 {error}。
        "azw3": None,
    }


def _import_summary(imp_report: dict, book, *, outdir: Path) -> dict:
    """导入 EPUB 的 ② 页数据。

    **不复用 `_summary`**：那份读的是清洗报告（编码、删除行、碎行合并…），
    导入的书一个都没有 —— 导入不做清洗。硬塞一堆 0 进去会让用户以为
    "清洗跑了但什么都没删"，那是假信息。这里如实给导入自己的口径，
    前端按 `kind` 分支画不同的行（§12 两条入口共用产线，但不是同一个账单）。
    """
    stats = real_stats(book)
    kept = int(imp_report.get("images_kept") or 0)
    decorative = len(imp_report.get("images_decorative") or [])
    orphan = len(imp_report.get("images_orphan") or [])
    return {
        # 前端据此决定画哪一套行：txt = 清洗账单，epub = 导入账单。
        "kind": "epub",
        "file": str(book.source or ""),
        "name": book.source.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
        "outdir": str(outdir),
        # 导入没有 clean-report.md（没洗过），报告就是 report.json。
        "report_path": str(outdir / "report.json"),
        "encoding": {"chosen": book.encoding or "", "confidence": 0.0},
        "size_bytes": 0,
        "lines": {"total": 0, "nonblank": 0},
        "stats": {
            "chapters": stats.get("chapters", 0),
            "paragraphs": stats.get("paragraphs", 0),
            "chars": stats.get("chars", 0),
        },
        "dropped": {},
        "fixed": {},
        "merged": {},
        "verse": {},
        "adjust": {},
        "warnings": list(imp_report.get("warnings") or []),
        # 0 字但有图 = 图集/漫画，与"空壳文件"是两回事，界面文案不同（§10 第 5 条）。
        "content_state": _content_state(int(stats.get("chars") or 0), kept),
        "oneline": {"suspected": False, "long_line_count": 0, "lines_with_multiple_chapters": 0},
        # 导入账单：只给计数，不给图片文件名（那些名字来自原书，没必要送进界面）。
        "import": {
            "epub_version": imp_report.get("epub_version") or "",
            "nav_entries": imp_report.get("nav_entries") or 0,
            "chapters": imp_report.get("chapters") or 0,
            "volumes": imp_report.get("volumes") or 0,
            "merged_groups": imp_report.get("merged_groups") or 0,
            "merged_pieces": imp_report.get("merged_pieces") or 0,
            "front_matter_entries": imp_report.get("front_matter_entries") or 0,
            "toc_dropped": imp_report.get("toc_dropped") or 0,
            "continuation_docs": imp_report.get("continuation_docs") or 0,
            "empty_chapters": imp_report.get("empty_chapters") or 0,
            "images_kept": kept,
            "images_decorative": decorative,
            "images_orphan": orphan,
            "cover": bool(imp_report.get("cover")),
            "fonts": imp_report.get("fonts") or 0,
        },
    }


# 复核页每类一次送多少行，滚到底再要下一批（决定27）。一本 8 MB 的书能有
# 两三千条删除行，一次塞进 DOM 在 WebView2 里会明显卡（两万个 <li> 实测能卡
# 一秒以上）。分页之后首屏只有几十个节点，滚动条也短得能看出"还有多少没看"。
REVIEW_PAGE = 40

# 列表行只做"认得出是哪一条"，60 字足够看清开头；要看全文点开那一行。
_BRIEF_LEN = 60


def _brief(text, limit: int = _BRIEF_LEN) -> str:
    """压成一行摘要：换行折成空格，超长截断加省略号。"""
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _rows_del(report: dict, adjust: dict | None) -> list[dict]:
    """删除清单：一行一条。原文全文不在这里，点开才取（见 `_detail_del`）。"""
    keep = {n for n in ((adjust or {}).get("protect_lines") or []) if isinstance(n, int)}
    rows: list[dict] = []
    for it in report.get("dropped_lines") or []:
        if not isinstance(it, dict):
            continue
        no = it.get("line")
        if not isinstance(no, int):
            continue
        rows.append(
            {
                "ln": no,
                "tx": _brief(it.get("text")),
                "tag": {"toc": "目录", "marker": "标记"}.get(it.get("kind"), "广告"),
                "kept": no in keep,
            }
        )
    rows.sort(key=lambda r: r["ln"])
    return rows


def _rows_merge(session, merges: list[dict], adjust: dict | None) -> list[dict]:
    """合并处：一处一条。

    `cut` 是"想断开的话该填哪个行号"。`merge_breaks` 的语义是"这一行别并进
    上一行"，所以候选点是除首行之外的每一行；一处只给一个勾，取第二行 ——
    那正是这段拼接发生的位置。

    断开过的那一处**会从合并清单里消失**（它已经不合并了），但用户得能把勾
    取消掉 —— 所以这里给断开的行号补一条"已断开"的合成行，勾选态由它承载。
    不补的话，「别并」就成了单向门。
    """
    brk = {n for n in ((adjust or {}).get("merge_breaks") or []) if isinstance(n, int)}
    rows: list[dict] = []
    for g in merges or []:
        if not isinstance(g, dict):
            continue
        lines = [int(n) for n in (g.get("lines") or []) if isinstance(n, int)]
        if not lines:
            continue
        pieces = [p for p in (g.get("pieces") or []) if isinstance(p, dict)]
        cut = lines[1] if len(lines) > 1 else lines[0]
        rows.append(
            {
                "ln": lines[0],
                "ln2": lines[-1],
                "cut": cut,
                "tx": _brief(g.get("result")),
                "tag": "并 %d 行" % max(2, len(pieces) or len(lines)),
                "brk": cut in brk,
            }
        )
    seen = {r["cut"] for r in rows}
    extra = sorted(n for n in brk if n not in seen)
    if extra:
        lines = _source_lines(session)
        for n in extra:
            a = lines[n - 2].strip() if 2 <= n <= len(lines) else ""
            b = lines[n - 1].strip() if 1 <= n <= len(lines) else ""
            rows.append(
                {
                    "ln": n - 1,
                    "ln2": n,
                    "cut": n,
                    "tx": _brief((a + b) or "（这两行没并起来）"),
                    "tag": "已断开",
                    "brk": True,
                }
            )
    rows.sort(key=lambda r: r["ln"])
    return rows


# 属于"切章"这一类的告警码。front_paragraphs 不在 warning_items 里 ——
# 它是 _alerts() 从 chapters.front_paragraphs 现算的，这里照同一口径补一条。
_CHAP_CODES = ("toc_unsure", "toc_empty_prefix", "no_chapters", "oneline")


def _rows_chap(report: dict) -> list[dict]:
    rows: list[dict] = []
    front = int((report.get("chapters") or {}).get("front_paragraphs") or 0)
    if front > 1:
        rows.append(
            {
                "ln": "",
                "code": "front_paragraphs",
                "tx": _brief(_ALERT_BRIEF["front_paragraphs"].format(n=front), 120),
                "tag": "切章",
            }
        )
    for it in report.get("warning_items") or []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "")
        if code not in _CHAP_CODES:
            continue
        tpl = _ALERT_BRIEF.get(code) or str(it.get("text") or "")
        try:
            text = tpl.format(n=int(it.get("count") or 1))
        except (KeyError, IndexError, ValueError):
            text = str(it.get("text") or tpl)
        rows.append({"ln": "", "code": code, "tx": _brief(text, 120), "tag": "切章"})
    return rows


def _chapter_rows(book) -> list[dict]:
    """章节树：把结构性毛病逐条列出来（空章、空标题、编号重复/倒退）。

    编号比较必须**按卷分组**：每一卷的章号都从 1 重新开始，全书一条线
    比较必然把卷二的第一章报成"编号倒退"，全是假警报。分组规则复用
    model.chapter_groups()（validate_items() 用的就是它），界面不另立标准。
    卷/部/集（level=2）是容器标题，正文都在它下面的章里，本身 0 段是
    正常形态，不报"空章"。
    """
    chapters = list(getattr(book, "chapters", []) or [])
    if not chapters:
        return []
    rows: list[dict] = []
    # 每条按 (章节下标, 同类顺序) 排，保证点开 _detail_tree 能按 idx 对回原章。
    order = {"empty_title": 0, "chapter_empty": 1, "number_dup": 2, "number_decrease": 3}

    def add(i: int, ch, code: str, tx: str, tag: str) -> None:
        num = ch.number
        label = num if num is not None else i + 1
        rows.append({"ln": label, "idx": i, "code": code, "tx": tx, "tag": tag})

    for i, ch in enumerate(chapters):
        title = (ch.title or "").strip()
        shown = title or "（无标题）"
        if not title:
            add(i, ch, "empty_title", shown, "空标题")
        # 卷容器（level=2）本身没有段落是正常的，正文在下面的章里。
        count = len([p for p in ch.paragraphs if (p.text or "").strip()])
        if count == 0 and ch.level != 2:
            add(i, ch, "chapter_empty", "%s · 0 段" % shown, "空章")

    for _group_name, items in chapter_groups(book):
        first_seen: dict[int, int] = {}
        prev: tuple[int, int] | None = None
        for i, ch in items:
            num = ch.number
            if num is None:
                continue
            shown = (ch.title or "").strip() or "（无标题）"
            if num in first_seen:
                add(i, ch, "number_dup", shown, "编号重复")
            else:
                first_seen[num] = i
            if prev is not None and num < prev[0]:
                add(i, ch, "number_decrease", shown, "编号倒退")
            prev = (num, i)

    rows.sort(key=lambda r: (r["idx"], order.get(r["code"], 9)))
    return rows


def _font_group(book) -> tuple[int, list[str], str]:
    """缺字：只算"这本书要用到的字里，内嵌字库有几个没有"。

    口径与打包时一致：正文 + 标题 + 书名 + 作者，不算导航/NCX（那些字由
    模板自带，模板自己保证可读）。粒度按简版走 —— 只报个数，不列每字上下文
    （规格定稿：缺的字必然不是多数）。
    """
    chars: set[str] = set()
    for ch in getattr(book, "chapters", []) or []:
        chars |= set(ch.title or "")
        for p in ch.paragraphs:
            chars |= set(p.text or "")
    for p in getattr(book, "front_matter", []) or []:
        chars |= set(p.text or "")
    chars |= set(getattr(book, "title", "") or "")
    chars |= set(getattr(book, "author", "") or "")
    try:
        miss, name = missing_chars(chars)
    except Exception:  # noqa: BLE001
        # 字体层出问题不该让整页复核打不开：缺字是"看一眼"级别的信息。
        return 0, [], ""
    return len(miss), miss[:12], name


def _review_groups(session) -> list[dict]:
    """复核页的五类，按界面上从左到右的顺序排（决定38 的 N 项横排）。

    每类都带 `act`：能不能勾。能勾的只有删除与合并 —— 切章、章节树、缺字
    都是"只能看"的事实，改它们要回原文改，界面里给个假开关是骗人。
    """
    result = session.result
    report = result.report
    book = result.book
    del_rows = _rows_del(report, session.adjust)
    merge_rows = _rows_merge(session, getattr(result, "merges", None) or [], session.adjust)
    chap_rows = _rows_chap(report)
    tree_rows = _chapter_rows(book)
    font_num, _font_miss, font_name = _font_group(book)

    note = ""
    if font_num:
        note = (
            "这 %d 种字不在内嵌字库里，打包时会用系统字体补上 —— "
            "电脑上看只是笔画换了个人写，手机阅读器上可能是一块空白。" % font_num
        )
    elif font_name:
        note = "这本书的字，内嵌字库都认得。"

    return [
        {
            "key": "del",
            "name": "删除",
            "unit": "行",
            "act": True,
            "hint": "默认全删，想留的勾「别删」",
            "num": len(del_rows),
            "rows": del_rows,
            "note": "",
        },
        {
            "key": "merge",
            "name": "合并",
            "unit": "处",
            "act": True,
            "hint": "默认全并，想断开的勾「别并」",
            "num": len(merge_rows),
            "rows": merge_rows,
            "note": "",
        },
        {
            "key": "chap",
            "name": "切章",
            "unit": "处",
            "act": False,
            "hint": "只能看，改不了",
            "num": len(chap_rows),
            "rows": chap_rows,
            "note": "",
        },
        {
            "key": "tree",
            "name": "章节树",
            "unit": "处",
            "act": False,
            "hint": "只能看，改不了",
            "num": len(tree_rows),
            "rows": tree_rows,
            "note": "",
        },
        {
            "key": "font",
            "name": "缺字",
            "unit": "种",
            "act": False,
            "hint": "只能看，改不了",
            "num": font_num,
            "rows": [],
            "note": note,
        },
    ]


def _groups_of(session) -> list[dict]:
    """复核页的五类清单，挂在会话上缓存。

    `apply_adjust` 会复用同一个 Session（见那个方法的注释），所以缓存不能用
    "算过没有"来判断，要认 `id(result)` —— 重跑之后 result 是个新对象，
    id 一变缓存自动作废。一本 2,000 条删除的书重算一次要几十毫秒，
    每点开一行都重算就明显了。
    """
    stamp = id(session.result)
    cached = session.extra.get("review")
    if cached and cached[0] == stamp:
        return cached[1]
    groups = _review_groups(session)
    session.extra["review"] = (stamp, groups)
    return groups


def _review_payload(session) -> dict:
    """③ 复核页要的那一份数据。

    只送每类的前 `REVIEW_PAGE` 行；`more` 为真表示还有，滚到底再调
    `Api.review_more`。类目的 `num` 是全量条数，界面上的数字用它。

    **`keys` 是全量候选的行号**（只有能勾的两类给），一次送完，与分页无关。
    为什么必须有它：界面的「全选」原来只遍历已经加载的那 40 行 —— 用户得先
    一直往下滚、把所有行都拉出来，才可能真的全选上（他原话："我得先一直
    不断地下拉，把所有的都显示出来，然后才能全选"）。只送数字不送文本，
    几千行也就几十 KB；比"再开一个 all=true 的语义"简单得多 —— 勾选态仍然
    是一个普通的行号集合，apply_adjust 那条路一行都不用改。
    """
    groups = _groups_of(session)
    out: list[dict] = []
    for g in groups:
        rows = g["rows"]
        # del 的身份是行号 ln，merge 的身份是断开点 cut（与 rvPickKey 同口径）。
        keys: list[int] = []
        if g["act"]:
            keys = [r["cut"] if g["key"] == "merge" else r["ln"] for r in rows]
        out.append(
            {
                "key": g["key"],
                "name": g["name"],
                "unit": g["unit"],
                "act": g["act"],
                "hint": g["hint"],
                "num": g["num"],
                "note": g["note"],
                "rows": rows[:REVIEW_PAGE],
                "keys": keys,
                "more": len(rows) > REVIEW_PAGE,
            }
        )
    book = session.result.book
    return {"name": (book.title or "").strip(), "groups": out, "page": REVIEW_PAGE}


def _source_lines(session) -> list[str]:
    """原文按行切开 —— 复核页要看"这一行前后是什么"。

    行号来自清洗层的 `_Line.no`，那是**原文**的行号；而 `result.text` 是清洗
    之后的文本，删掉的行已经不在里面，按它取第 n 行会一路错位。所以这里重新
    读一遍原文，用清洗层认定的编码解码。读不出来就返回空表，界面退化成只显示
    摘要 —— 比抛异常让整页打不开好。
    """
    cached = session.extra.get("raw_lines")
    if cached is not None:
        return cached
    lines: list[str] = []
    try:
        data = Path(session.source).read_bytes()
        enc = str(getattr(session.result.book, "encoding", "") or "utf-8")
        try:
            text = data.decode(enc, errors="replace")
        except LookupError:
            text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
    except OSError:
        lines = []
    session.extra["raw_lines"] = lines
    return lines


def _detail_del(session, row: dict) -> dict:
    """删掉的这一行，前后各是什么。

    左右两栏同构：左栏夹着那行（带删除线），右栏是去掉它之后的样子 ——
    对照本身说明一切，不需要一句"删掉 1 行，前后文不动"。
    """
    lines = _source_lines(session)
    n = int(row.get("ln") or 0)

    def near(step: int) -> dict | None:
        j = n + step
        while 1 <= j <= len(lines):
            s = lines[j - 1].strip()
            if s:
                return {"t": s, "ln": j}
            j += step
        return None

    before, after = near(-1), near(1)
    gone = lines[n - 1].strip() if 1 <= n <= len(lines) else str(row.get("tx") or "")
    left: list[dict] = []
    if before:
        left.append(before)
    left.append({"t": gone, "ln": n, "cls": "gone"})
    if after:
        left.append(after)
    right = [{"t": x["t"]} for x in (before, after) if x]
    return {"lcap": "原文", "rcap": "删除后", "ro": False, "left": left, "right": right}


def _detail_merge(session, row: dict) -> dict:
    """这一处合并：左边是原来的几行（带行号），右边是并出来的那一段。"""
    want = row.get("ln")
    for g in getattr(session.result, "merges", None) or []:
        if not isinstance(g, dict):
            continue
        lines = [int(n) for n in (g.get("lines") or []) if isinstance(n, int)]
        if not lines or lines[0] != want:
            continue
        left = [
            {"t": _brief(p.get("text"), 400), "ln": int(p.get("line") or 0)}
            for p in (g.get("pieces") or [])
            if isinstance(p, dict)
        ]
        return {
            "lcap": "原文",
            "rcap": "合并后",
            "ro": False,
            "left": left,
            "right": [{"t": str(g.get("result") or "")}],
        }
    return {
        "lcap": "原文",
        "rcap": "合并后",
        "ro": False,
        "left": [],
        "right": [{"t": str(row.get("tx") or "")}],
    }


def _detail_chap(session, row: dict) -> dict:
    """切章类：左边给原文/章节树的一段，右边说"这会怎样"。"""
    book = session.result.book
    report = session.result.report
    code = str(row.get("code") or "")
    chapters = list(getattr(book, "chapters", []) or [])
    first = chapters[0] if chapters else None
    first_title = ((first.title or "").strip() if first else "") or "第一章"

    if code == "front_paragraphs":
        front = [p for p in (getattr(book, "front_matter", []) or []) if (p.text or "").strip()]
        left = [{"t": _brief(p.text, 110)} for p in front[:3]]
        if len(front) > 3:
            left.append({"t": "……另有 %d 段" % (len(front) - 3), "cls": "more"})
        left.append({"t": first_title, "cls": "hot"})
        prose = (
            "这 %d 段排在「%s」前面，不属于任何一章。打包时它们会并到第一章那一页的"
            "开头 —— 正文不会丢，但读者翻开第一章，先看到的是一段没头没尾的东西。"
            % (len(front), first_title)
        )
        return {"lcap": "原文", "rcap": "会怎样", "ro": True, "left": left, "prose": prose}

    if code == "toc_unsure":
        toc = [it for it in ((report.get("samples") or {}).get("toc") or []) if isinstance(it, dict)][:3]
        left = [{"t": _brief(it.get("text"), 90), "ln": it.get("line")} for it in toc]
        prose = (
            "开头有一块看着像目录的东西，但没能确认它和正文的对应关系。现在按目录块"
            "处理 —— 万一它其实是正文，这几行就删错了。"
        )
        return {"lcap": "原文", "rcap": "怎么了", "ro": True, "left": left, "prose": prose}

    if code == "toc_empty_prefix":
        # 只列开头连续那一段空壳，与 clean.py 的 _toc_like_empty_prefix 同口径：
        # 正文中间另有空章时不该混进"目录残留"这里来。最多列 5 章 ——
        # 目录特别长的时候（几百条）没必要把整屏都填满。
        shells = []
        for ch in chapters:
            if ch.level == 2 or [p for p in ch.paragraphs if (p.text or "").strip()]:
                break
            shells.append(ch)
            if len(shells) >= 5:
                break
        left = [{"t": _brief((ch.title or "").strip() or "（无标题）", 90), "ln": ch.number or ""} for ch in shells]
        prose = (
            "开头这几章只有标题、没有正文 —— 它们是原文开头的目录条目，被当成章节标题"
            "切了出来；真正的正文在它们后面。一条都没删：那个位置上也可能放着真的"
            "前言，删错就找不回来了。成书后这几章会是几张空页，目录里也是空的。"
            "要彻底干净，请回原文把开头那段目录删掉，再导一次。"
        )
        return {"lcap": "原文", "rcap": "怎么了", "ro": True, "left": left, "prose": prose}

    if code == "no_chapters":
        src = [p for p in (getattr(book, "front_matter", []) or []) if (p.text or "").strip()]
        if not src and first is not None:
            src = [p for p in first.paragraphs if (p.text or "").strip()]
        left = [{"t": _brief(p.text, 110)} for p in src[:3]]
        prose = (
            "整本没切出章节。成书后是一个没有目录的大文件 —— 能读，但翻起来费劲。"
            "多半是这本书的标题写法不在已知的几种格式里。"
        )
        return {"lcap": "原文", "rcap": "怎么了", "ro": True, "left": left, "prose": prose}

    if code == "oneline":
        left = [{"t": _brief(row.get("tx"), 110)}]
        prose = (
            "整本被读成了一行 —— 原文可能压根没有换行，段落是硬折出来的。"
            "这种情况清洗帮不上忙，先回原文把换行修好再来。"
        )
        return {"lcap": "原文", "rcap": "怎么了", "ro": True, "left": left, "prose": prose}

    return {"lcap": "原文", "rcap": "怎么了", "ro": True, "left": [], "prose": str(row.get("tx") or "")}


_TREE_PROSE = {
    "chapter_empty": "这一章只有标题、底下没有正文。成书后是翻过去的一页空白。",
    "empty_title": "这一章没有标题，目录里会是一条空白。",
    "number_dup": "目录里会出现两条同样的编号，点哪条都只能到其中一条。",
    "number_decrease": "编号倒着走。阅读器按编号排序时，这一章会被排到前面去。",
}


def _detail_tree(session, row: dict) -> dict:
    """章节树类：左边列前后三章（带段数），右边说这一章哪里不对。"""
    chapters = list(getattr(session.result.book, "chapters", []) or [])
    i = int(row.get("idx") or 0)
    if not (0 <= i < len(chapters)):
        return {"lcap": "章节树", "rcap": "怎么了", "ro": True, "left": [], "prose": str(row.get("tx") or "")}

    def line(j: int) -> dict:
        ch = chapters[j]
        title = (ch.title or "").strip() or "（无标题）"
        count = len([p for p in ch.paragraphs if (p.text or "").strip()])
        return {
            "t": "%s · %d 段" % (title, count),
            "ln": ch.number if ch.number is not None else j + 1,
        }

    left: list[dict] = []
    if i > 0:
        left.append(line(i - 1))
    left.append({**line(i), "cls": "hot"})
    if i + 1 < len(chapters):
        left.append(line(i + 1))
    prose = _TREE_PROSE.get(str(row.get("code") or ""), str(row.get("tx") or ""))
    return {"lcap": "章节树", "rcap": "怎么了", "ro": True, "left": left, "prose": prose}


_DETAILS = {
    "del": _detail_del,
    "merge": _detail_merge,
    "chap": _detail_chap,
    "tree": _detail_tree,
}


def _detail(session, key: str, index: int) -> dict:
    groups = _groups_of(session)
    group = next((g for g in groups if g["key"] == key), None)
    if group is None:
        return {"error": "没有这一类"}
    rows = group["rows"]
    if not (0 <= index < len(rows)):
        return {"error": "这一条已经不在了"}
    fn = _DETAILS.get(key)
    if fn is None:
        return {"lcap": "", "rcap": "", "ro": True, "left": [], "prose": group.get("note") or ""}
    return fn(session, rows[index])


class Api:
    """`webview.create_window(..., js_api=Api())` 挂上去的那个对象。

    方法签名里别用 Path / dataclass：返回值会被 `json.dumps` 序列化后塞进 JS，
    带不了 Python 对象。全部用手写 dict。
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        # 由 app.py 在窗口建好后注入。pick_file / reveal 都要用它，
        # 而窗口对象在 js_api 构造时还不存在（同一个 create_window 调用里）。
        #
        # **必须是下划线开头的私有名**：pywebview 的 `inject_pywebview` 用
        # `get_functions(obj)` 递归遍历 js_api 上所有不以 `_` 开头的属性，
        # 遇到"非 callable 且带 `__module__`"的对象就继续往里钻。挂一个公开的
        # `self.window = window`（或哪怕是同名的 property）会让它去遍历整个
        # Window → native → WebView2 COM 对象，轻则满屏
        # `Error while processing window.native.AccessibilityObject...`，
        # 重则 `maximum recursion depth exceeded`（实测踩过，日志刷了几百行）。
        # 下划线开头会被 `if name.startswith('_'): continue` 直接跳过。
        self._window = None

    # ---------- 内部 ----------

    def _new_job(self, stage: str = "import") -> Job:
        job = Job(job_id=uuid.uuid4().hex[:12], stage=stage)
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def _spawn(self, job: Job, fn) -> None:
        """起 worker。异常一律落进 job.error，**不向前端抛**："""
        def run() -> None:
            try:
                fn()
            except Cancelled:
                # 取消不是失败：界面早就退回上一屏了，这里只负责别留下东西。
                job.cancelled = True
                job.payload = None
            except BaseException as exc:  # noqa: BLE001 - 线程里必须兜住一切
                job.error = self._friendly_error(exc)
                job.error_detail = _technical(exc)
                # 三种标题分开：文件不合格 / 打包失败 / 清洗失败。
                # 打 EPUB 时出问题说"清洗失败"是错的 —— 清洗早就成功了。
                #
                # **打包失败不能只看异常类型**：写盘时被占用的 `book.json` 抛的是
                # `PermissionError`（OSError 子类），地址写错抛 `OSError`，
                # 两者都不是 ValueError —— 只按类型分会落进"清洗失败"，
                # 用户看到"清洗失败"会去换文件试，而真正要处理的是占用/权限。
                if isinstance(exc, OneLineBook):
                    # 这一类"默认拒收"必须留口子：判据不可能全对，误判一次就把
                    # 用户挡在门外很难受。界面据此在错误页亮出「仍然继续」。
                    job.error_title = "这个文件不能处理"
                    job.can_continue = True
                elif isinstance(exc, InputError):
                    job.error_title = "这个文件不能处理"
                elif isinstance(exc, ValueError) or job.stage == "build":
                    job.error_title = "打包失败"
                else:
                    job.error_title = "清洗失败"
                job.payload = None
                # 控制台留一条完整栈给开发者。界面只显示人话那一行。
                traceback.print_exc()
            finally:
                job.done = True

        t = threading.Thread(target=run, name=f"job-{job.job_id}", daemon=True)
        job.thread = t
        t.start()

    @staticmethod
    def _is_one_line_book(book, report: dict) -> bool:
        """整本糊成一两个超长行、且一章都没切出来 —— 这类书默认拒收。

        判据刻意只有两条，且都来自报告里的证据、不做任何猜测：
          1. 一章都没切出来。切出了章节说明结构还在，超长行只是局部问题，
             不该拦人（正常书正文里引用几个章节号很常见，实测基线 book05
             就出现过 suspected 为真而全书正常）。
          2. oneline.suspected 为真 —— 它在 diagnose.whole_book_in_one_line
             里只认「存在超过 3000 字的行」这一条，不再看"一行里塞了几个章节号"
             （旧判据在 78 字的注释清单上误报过一次）。

        与 tools/batch_check.py 的 flags_of 同一口径（那边写作"0 章 + 超长行"）。
        """
        if list(getattr(book, "chapters", []) or []):
            return False
        oneline = (report or {}).get("oneline") or {}
        return bool(oneline.get("suspected"))

    @staticmethod
    def _friendly_error(exc: BaseException) -> str:
        """把异常翻成一句用户能懂的话。

        ★ 不许把类名与异常原文直接拼给用户：早先就是 f"{type(exc).__name__}: {exc}"，
          一个失败的 clean 会让 720×520 的整屏变成一行没有等宽字体的 traceback。
          用户不需要知道它叫 ValueError，他需要知道"现在能不能重试"。
          技术细节走 _technical()，界面收在「技术细节」折叠里。
        """
        if isinstance(exc, SystemExit):
            return str(exc.code or exc) or "处理失败"
        if isinstance(exc, MemoryError):
            return "内存不够了：这本书太大，请改用命令行 `python -m huoban clean` 处理"
        if isinstance(exc, InputError):
            return str(exc)
        plain = _PLAIN_TRAP.get(type(exc).__name__)
        if plain:
            return plain
        return ("处理这本书的时候出了个意料之外的问题。"
                "换个文件再试一次；如果每次都这样，把「技术细节」里的内容发给我。")

    def _run_import(self, job: Job, session: Session) -> None:
        """G1 的唯一一条流水线：解析路径 → 清洗 → 落产物 → 出摘要。"""
        src = session.source
        # 下面这些是"文件本身不合格"，抛 InputError 让界面写"这个文件不能处理"。
        # 真正的清洗异常不包成 InputError —— 那是清洗线的问题，得如实说"清洗失败"，
        # 否则以后 clean.py 出了 bug，用户看到"这个文件不能处理"会一直换文件试。
        if not src.exists():
            raise InputError(f"文件不存在：{src}")
        if not src.is_file():
            raise InputError(f"这不是一个文件：{src}")
        size = src.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise InputError(
                f"文件 {size / 1024 / 1024:.1f} MB，超过 {MAX_INPUT_BYTES // 1024 // 1024} MB 上限"
            )
        # 0 字节不是"很短的书"，是根本没有内容。放行会一路走完清洗、拿回 0 章 0 字，
        # 界面显示成一次成功 —— 这是 §10 第 5 条实测六种异常路径时唯一的文案缺口。
        # 刻意不写成"小于 1KB 就拒"：那会挡住真的只有几十字的占位文件，
        # 而 0 字节是事实，不是猜。
        if size == 0:
            raise InputError("这个文件是空的（0 字节），里面没有正文")
        if src.suffix.lower() not in INPUT_SUFFIXES:
            raise InputError(f"只接受 TXT 或 EPUB 文件，这个是 {src.suffix or '无后缀'}")

        if src.suffix.lower() in EPUB_SUFFIXES:
            self._run_import_epub(job, session)
            return

        result = clean_file(src, adjust=session.adjust)
        # 取消检查点：清洗是整个流程里最长的一段，用户多半是在这里点的取消。
        # 赶在 mkdir 之前拦下来，就等于这次什么都没发生，连回收站都不用麻烦。
        if job.cancel_requested:
            raise Cancelled()
        book, report = result.book, result.report

        # 「整本书糊成一两个超长行」不往下走：原文压根没有换行，本项目铁律是不给
        # 没有换行的文本猜分段/断句，硬做出来只会是一坨没有段落的字。
        # 但**不做硬拒**：规则不可能全对，误判一次就把人挡在门外很难受，
        # 所以只在这里立一道默认墙 —— 错误页给「仍然继续」，点了走 force_import 重来。
        # 位置刻意放在 mkdir 之前：这时候一个字节都还没写盘，拒收等于这次没发生。
        if not session.extra.get("force_one_line") and self._is_one_line_book(book, report):
            raise OneLineBook(
                "整本书几乎都糊在一两个超长行里 —— 原文很可能压根没有换行，"
                "段落是网页排版时硬折出来的。\n\n"
                "这种情况不做自动分段：在没有换行的文本上猜句子边界，"
                "等于替你改写正文，猜错了没法还原。\n\n"
                "建议回原文把换行修好（一般重新下载一次 TXT 就能拿到带段落断行的版本），"
                "再拖进来。\n\n"
                "如果你确认这本书就是要这么读，点下面「仍然继续」，"
                "照旧清洗、成书（成书后是一整段没有分段的文字）。"
            )

        # 产物目录：<输入同目录>/<书名>_gui/（§6）。书名从正文里猜过，
        # 猜不出来时 clean.py 会退回文件名，所以这里一定非空。
        #
        # **不能写死成 `<书名>_gui`**：两本不同的书解析出同一个书名是常事
        # （《全集》《无标题》，或者正文里都有同一句「书名：」），
        # 直接复用那个目录会把上一次的 book.json / report.json 默默覆写掉。
        # 由 `_import_outdir` 决定：同源重导复用、异源撞名另起 `(2)`。
        outdir = _import_outdir(src, book.title or src.stem)
        outdir.mkdir(parents=True, exist_ok=True)
        # 撞车保护放在写盘这一层（§6 硬约束 1）。GUI 的目录名带 _gui 后缀，
        # 正常不会撞上输入文件，但"永不修改输入"不能只靠命名约定。
        for target in (outdir / "book.json", outdir / "report.json"):
            if target.exists():
                try:
                    if target.samefile(src):
                        raise SystemExit(f"输出路径与输入文件相同，拒绝写入：{target}")
                except OSError:
                    pass

        save_book(book, outdir / "book.json")
        # 不再写 clean-report.md（决定17：报告按钮删了，报告就不生成）。
        # report.json 留着 —— 它不进界面，但"这本为什么洗成了这样"要靠它复查。
        (outdir / "report.json").write_text(
            _dumps(report), encoding="utf-8"
        )
        if session.adjust:
            (outdir / "adjust.json").write_text(_dumps(session.adjust), encoding="utf-8")

        session.result = result
        session.outdir = outdir
        payload = _summary(report, outdir=outdir)
        # ④ 页的初始值顺手带上：省得前端再问一次后端"书名是什么"。
        # 放在 payload 里而不是单开一个方法，是因为它和这次清洗严格绑定 ——
        # 换一本书就该换一套预填值，分开取会有"拿到上一本的书名"的风险。
        payload["meta"] = _book_meta(book)
        # 付梓那一屏的字模：这本书自己的常用字（见 _book_glyphs）。
        payload["glyphs"] = _book_glyphs(book)
        # 收得晚了：文件已经落盘。撤掉，让"取消"在结果上仍然是"没发生"。
        if job.cancel_requested:
            _recycle(outdir)
            raise Cancelled()
        job.payload = payload

    def _run_build(self, job: Job, session: Session, meta: dict) -> None:
        """G2 流水线：拿会话里的 Book → 生成 EPUB → 落盘 → 出完成页数据。

        全程不重新清洗：`session.result.book` 就是 G1 那份，重洗一遍要几十秒，
        而且如果原文在这期间被改了，用户拿到的会是一份他从没看过报告的包。
        """
        if session.result is None or session.outdir is None:
            raise InputError("这次会话的清洗结果不在了，请重新拖一次文件")

        book = session.result.book
        # 用户改过的书名/作者要写回 book 再打包，否则 build_epub 里
        # `title or book.title` 会退回去用清洗时猜的那个。
        meta = meta or {}
        title = str(meta.get("title") or "").strip()
        author = _meta_author(meta, book)
        language = str(meta.get("language") or "").strip() or "zh-CN"
        if title:
            book.title = title
        # 作者不再「空了就清成空串又让 write_epub 回落」：_meta_author 已经
        # 把「清空 = 不写作者」和「没带键 = 用猜的」两种情形分清楚了。
        book.author = author
        book.encoding = book.encoding or (session.result.report.get("encoding") or {}).get("chosen", "")

        # 用 validate_items 而不是 validate：每条带 code 与 level（决定32 的
        # 分级就建在这上面）。validate() 只是把 text 抽出来的薄壳，用它等于在
        # 最靠前的一步就把级别丢了。
        problems = validate_items(book)
        # 文件名跟着书名走：用户可能把"第 3 章 XXX"这种猜歪的标题改成了真书名，
        # 这时候还叫旧名字会让人在下一次找不到文件。
        stem = safe_name(title or book.title or session.source.stem, fallback="未命名")

        # 输出目录可以由 ④ 页指定（试用反馈「目标生成目录应可自定义」）。
        # 只影响成品 EPUB/AZW3 落在哪；产物目录（book.json / report.json /
        # cover.png）仍然在 session.outdir —— 那是这次会话的中间态，跟着会话走。
        # 建不出来就退回默认：为一个目录名把整次打包搞失败是本末倒置。
        out_dir = session.outdir
        raw_out = str(meta.get("outdir") or "").strip()
        if raw_out:
            try:
                cand = Path(raw_out).expanduser()
                cand.mkdir(parents=True, exist_ok=True)
                if cand.is_dir():
                    out_dir = cand
            except OSError:
                out_dir = session.outdir

        outpath = out_dir / f"{stem}.epub"

        # 撞车保护（§6 硬约束 1）：绝不覆盖输入文件。GUI 的产物目录带 _gui 后缀，
        # 正常情况下撞不上，但这条不能只靠命名约定 —— 用户可以改书名，
        # 也可能有人把产物目录设成输入所在目录。放在写盘层才是兜底的。
        if outpath.exists():
            try:
                same = outpath.samefile(session.source)
            except OSError:
                same = outpath.resolve() == session.source.resolve()
            if same:
                raise SystemExit(f"输出路径与输入文件相同，拒绝写入：{outpath}")

        # 可复现（§6 硬约束 2）：modified 取输入文件的 mtime，不用当前时间。
        # 同一个 TXT 隔一天打两次，得到的包仍然逐字节相同。
        modified = _modified_from(session.source)
        cover_path = self._cover_for_build(session, meta)
        # 封底 (2026-11-06)：跟封面同一款版式/配色，落盘 back.png。
        # 它必须走 build_epub 的参数（封底是包里的一页，不是"路径当封面"那种
        # 外围手法）；back_cover 默认 None，不产封底时这一维不影响老产物。
        back_path = self._back_for_build(session, meta)

        from huoban.epub import write_epub

        result = write_epub(
            book,
            outpath,
            title=title or None,
            author=author or None,
            language=language,
            cover_path=cover_path,
            back_cover=back_path,
            modified=modified,
            fonts=bool(meta.get("fonts", True)),
            # 导入的书可能带插图：它们在产物目录的 images/ 下（§12）。
            # TXT 那条路没有这个键，传 None 就是原来的行为。
            image_dir=session.extra.get("images_dir"),
        )
        if job.cancel_requested:
            _recycle(outpath)
            raise Cancelled()
        # 书名/作者改了要回写 book.json，否则产物目录里的中间态和成品对不上，
        # 下次用 --book-json 打包会打出一个旧的元数据。
        #
        # **失败不能让成品消失**：EPUB 已经落盘了，这一步写不进去（`book.json`
        # 被别的程序占住 / 设成只读）只影响下次 `--book-json` 打得对不对，
        # 用户要的成品已经在磁盘上。所以吞掉异常、记进 problems 让 ⑤ 页说出来，
        # 而不是往上抛成"打包失败"把 9KB 的包藏起来（实测踩过：
        # `book.json` 设只读 → 整条 build 报「清洗失败」+ payload 为 None，
        # 而 `甲书.epub` 9100 字节静静躺在目录里没人告诉用户）。
        meta_problem = ""
        try:
            save_book(book, session.outdir / "book.json")
        except OSError as exc:
            meta_problem = f"书信息没写回 book.json（{exc.__class__.__name__}）：成品不受影响，但下次用 --book-json 打包会用到旧的书名/作者"
        # meta_problem 必须在这一步之前定下来：它要进 warnings，还要参与
        # 告警合并（决定31：同类合并、按类计数），事后 append 就漏掉了合并。
        job.payload = _epub_summary(
            result, outpath, book, problems,
            cover_path=cover_path,
            meta_problem=meta_problem,
        )

        # AZW3 是可选的第二步。EPUB 已经落盘了，这一步失败不能把 EPUB 弄丢，
        # 所以错误只记进 payload 让人看见，不往上抛。
        if meta.get("azw3"):
            job.payload["azw3"] = self._export_azw3(outpath, modified)

    def _export_azw3(self, epub_path: Path, modified: str) -> dict:
        """把刚打好的 EPUB 转成 AZW3，返回 ⑤ 页要显示的那一小块数据。

        失败一律转成 {"error": 一句人话}：完成页要能同时说清"EPUB 好了"
        和"AZW3 没成"，而不是整个流程红掉。
        """
        from huoban.azw3 import Azw3Error, Azw3Unavailable
        from huoban.azw3 import convert as convert_azw3

        out = epub_path.with_suffix(".azw3")
        try:
            got = convert_azw3(epub_path, out, pubdate=modified)
        except Azw3Unavailable as exc:
            return {"error": str(exc)}
        except Azw3Error as exc:
            return {"error": str(exc)}
        return {
            "path": str(got.path),
            "name": got.path.name,
            "size_bytes": got.size_bytes,
            "seconds": round(got.seconds, 1),
        }

    @staticmethod
    def _discard_stage(stage: Path) -> None:
        """解析失败时把暂存目录撤掉（只撤本次新建的、且只撤空的或只含 images/ 的）。

        **不删**：本项目铁律是永不删除（A2）。这里做的是 `rmdir` —— 它只能成功于
        空目录，非空会抛 OSError 被吞掉，所以最坏情况是留下一个空目录（与修 M3 前
        一样），绝不会删掉用户数据。图片是解析过程中写进去的，失败时它们也不完整，
        但为稳妥起见：先试 rmdir(images) 再试 rmdir(stage)，任一步失败就停手。
        """
        images = stage / IMAGES_DIR
        for target in (images, stage):
            try:
                target.rmdir()
            except OSError:
                continue

    def _run_import_epub(self, job: Job, session: Session) -> None:
        """导入一本 EPUB：读成章节树 → 落产物 → 出摘要。

        与 TXT 那条路的关键区别（§12）：
        - **不跑 clean.py**。章节边界由 OPF spine + nav/NCX 白送，编码由 XHTML
          自带声明，目录块与广告行基本不存在 —— 再洗一遍只会把正常排版当脏数据。
        - 插图要落盘（`images/`），打包时再由 `image_dir` 找回来。
        - 产物目录里没有 `clean-report.md`（没洗过），报告是 `report.json`。
        """
        from huoban.epubread import EpubReadError, read_epub_with_report

        # 先落到一个**独一无二**的暂存目录里（M1）。
        # 早先是 `stage = 文件名_gui`，书名与文件名 safe_name 相同时 outdir == stage，
        # "目标已存在"那个检查整块被跳过 → 覆写别人的产物目录，旧图还会被打进新 EPUB。
        # 现在 stage 永远是新目录，解析完再按书名挑一个同样不存在的目录 rename 进去。
        stage = _unique_dir(
            session.source.parent, safe_name(session.source.stem), OUTDIR_SUFFIX
        )
        img_dir = stage / IMAGES_DIR
        try:
            imp = read_epub_with_report(session.source, image_dir=img_dir)
        except EpubReadError as exc:
            # 打不开 / 是 DRM 的 / 固定版式 / 包内损坏：都是"这个文件不能处理"，
            # 不是清洗失败（清洗压根没开始）。
            self._discard_stage(stage)
            raise InputError(str(exc)) from exc
        except BaseException:
            # 连我们自己没预料到的异常也一样：别在用户输入目录旁边留个半截产物。
            self._discard_stage(stage)
            raise

        if job.cancel_requested:
            _recycle(stage)
            raise Cancelled()
        book = imp.book
        # 书名可能来自 OPF 也可能来自 nav，都没有时用文件名兜底，保证目录有名字。
        if not book.title:
            book.title = session.source.stem
        book.source = str(session.source)

        outdir = _unique_dir(session.source.parent, safe_name(book.title), OUTDIR_SUFFIX)
        if outdir != stage:
            # 一次 rename 整体搬家，不做"搬图片"那种半途状态。
            try:
                stage.rename(outdir)
            except OSError:
                # 跨盘 / 被占用：宁可目录叫文件名，也不把两份产物混进一个目录里。
                outdir = stage
        outdir.mkdir(parents=True, exist_ok=True)
        img_dir = outdir / IMAGES_DIR

        for target in (outdir / "book.json", outdir / "report.json"):
            if target.exists():
                try:
                    if target.samefile(session.source):
                        raise SystemExit(f"输出路径与输入文件相同，拒绝写入：{target}")
                except OSError:
                    pass

        save_book(book, outdir / "book.json")
        (outdir / "report.json").write_text(_dumps(imp.report), encoding="utf-8")

        session.result = imp
        session.outdir = outdir
        # 打包时 write_epub 靠它把插图找回来；没有图片就不传，行为与 TXT 一致。
        session.extra["images_dir"] = img_dir if img_dir.is_dir() else None

        # 原书封面：epubread 把封面图也复制到了 images/ 下并在报告里给出
        # `cover_file`（2026-10-28 新增）。这里记下绝对路径，④ 页据此
        # 自动选「用原书封面」—— 试用反馈「输入 EPUB 时该自动用原始封面」。
        cover_file = str(imp.report.get("cover_file") or "").strip()
        session.extra["source_cover"] = (
            str(img_dir / cover_file) if cover_file and (img_dir / cover_file).is_file() else None
        )

        payload = _import_summary(imp.report, book, outdir=outdir)
        try:
            payload["size_bytes"] = session.source.stat().st_size
        except OSError:
            payload["size_bytes"] = 0
        payload["meta"] = _book_meta(book)
        payload["meta"]["has_source_cover"] = bool(session.extra.get("source_cover"))
        payload["glyphs"] = _book_glyphs(book)
        if job.cancel_requested:
            _recycle(outdir)
            raise Cancelled()
        job.payload = payload

    def _cover_for_build(self, session: Session, meta: dict) -> Path | None:
        """按 ④ 页的选择准备封面文件，返回路径给 build_epub 的 cover_path。

        §8.6 的硬要求：**不给 build_epub 加新参数**。海报写成产物目录里的
        cover.png，路径当 cover_path 传进去，自然复用 `epub._load_cover`
        的扩展名 + 魔数校验；打包层零改动，也不碰 test_epub.py 那条
        "无封面时不应产出 Images/cover.png" 的断言。

        mode 四种：
          - "auto"   ：按队列项画（`cover_look`，九款版式与整幅画同一个队列），落盘 cover.png
          - "image"  ：用户自选图，已在 pick_cover 里归一化落盘 cover_custom.jpg
          - "source" ：用**原书封面**（只有 EPUB 导入那条路有），落盘 cover_source.jpg
          - 其它/缺省 ：无封面，返回 None（阅读器走文字封面页）

        ★ 2026-10-28 修：书名要用 ④ 页用户改过的那个。
          试用反馈第一条就是「封面书名不可自定义」—— 清洗从正文首行猜出来的
          书名经常带着推荐语或广告，用户在 ④ 页改掉了，封面却还用猜的那个。
        """
        mode = str(meta.get("cover_mode") or "").strip()
        if mode not in {"auto", "image", "source"}:
            return None
        if session.outdir is None:
            return None

        # 用户在 ④ 页填的书名优先；没填才回落到清洗猜的。
        title = str(meta.get("title") or "").strip()
        if not title:
            title = session.result.book.title if session.result else ""
        # 与 preview_cover / _run_build 同一条规矩：④ 页带了这个键，
        # 框里的值就是唯一真相（清空 = 不写作者）；没带才回落清洗猜的。
        author = _meta_author(meta, session.result.book if session.result else None)

        if mode == "image":
            picked = session.extra.get("cover_source")
            if not picked:
                return None
            src = Path(picked)
            if not src.is_file():
                return None
            target = session.outdir / "cover_custom.jpg"
            try:
                if not target.exists() or target.stat().st_mtime < src.stat().st_mtime:
                    _normalize_cover_image(src, target)
            except Exception:  # noqa: BLE001 - 封面失败不该阻断打包
                return None
            return target if target.is_file() else None

        if mode == "source":
            # 原书封面：导入阶段已经复制到产物目录的 images/ 下（epubread 负责），
            # 这里只做一次归一化 —— 原图可能是 CMYK、带透明通道、尺寸超大，
            # 直接塞进 EPUB 有些阅读器会显示不出来。
            picked = session.extra.get("source_cover")
            if not picked:
                return None
            src = Path(picked)
            if not src.is_file():
                return None
            target = session.outdir / "cover_source.jpg"
            try:
                if not target.exists() or target.stat().st_mtime < src.stat().st_mtime:
                    _normalize_cover_image(src, target)
            except Exception:  # noqa: BLE001
                return None
            return target if target.is_file() else None

        # mode == "auto"
        from gui import cover as cover_mod

        if not cover_mod.available():
            return None
        look = meta.get("cover_look")
        look = dict(look) if isinstance(look, dict) and look.get("kind") else None
        try:
            if look is None:
                look = cover_mod.first_look(title)
            data = cover_mod.make_look(look, title, author)
        except Exception:  # noqa: BLE001 - 封面画砸了退化成无封面
            return None
        if not data:
            return None
        target = session.outdir / "cover.png"
        target.write_bytes(data)
        return target

    def _back_for_build(self, session: Session, meta: dict) -> Path | None:
        """按 ④ 页的选择准备封底图，返回路径给 build_epub 的 back_cover。

        封底跟封面是"同一款"的：同一套版式 + 配色（或同一张整幅背景），
        纸纹与底色全跟封面走 —— 翻到最后一页还是这本书的样子，不是另贴一张。
        所以这里的 look 必须与 _cover_for_build 的 auto 分支算出同一个值，
        三行取值逻辑一字不差地照抄（没有抽公共函数：那边还牵着 title/author
        与三种 mode，抽出来要多传四个参数，反而不如摆在一起对照着看）。

        只在"自动配色"这一支产封底。用户自选图 / 用原书封面这两种情况的封面
        是他自己的图，我们不知道它的纸纹与底色，硬画一张米白底的封底贴上去，
        翻开是"别人的封面 + 我们的封底"，比没有封底更难看 —— 所以返回 None，
        走"这一版不生成封底页"（与老版本产物完全一样）。

        失败一律返回 None：封底是锦上添花，画不出来就不产这一页，
        绝不能因为封底把整本书的打包搞砸。
        """
        if str(meta.get("cover_mode") or "").strip() != "auto":
            return None
        if session.outdir is None:
            return None

        title = str(meta.get("title") or "").strip()
        if not title:
            title = session.result.book.title if session.result else ""

        from gui import cover as cover_mod

        if not cover_mod.available():
            return None
        look = meta.get("cover_look")
        look = dict(look) if isinstance(look, dict) and look.get("kind") else None
        try:
            if look is None:
                look = cover_mod.first_look(title)
            data = cover_mod.make_back_cover(look)
        except Exception:  # noqa: BLE001 - 封底画砸了就不产这一页
            return None
        if not data:
            return None
        target = session.outdir / "back.png"
        try:
            target.write_bytes(data)
        except OSError:
            return None
        return target

    # ---------- 前端可调的方法 ----------

    # 清洗等待页要飘这本书自己的话，只挑这么几片就够了。
    EXCERPT_SCAN_BYTES = 256 * 1024
    EXCERPT_MAX_ITEMS = 8
    EXCERPT_SNIPPET = 22

    def import_excerpt(self, path: str) -> dict:
        """只读解码拖进来的 TXT 开头一小段，给「正在清洗」那一屏飘真内容。

        等待页以前飘的是 samples/messy.txt 里写死的假广告与假碎行，用户一眼
        认出「不是我的书」。这里在起清洗 worker *之前*同步读文件头：解码 →
        挑出真会被整行删掉的广告行（drop）、真断行对与相邻两行真正文（merge），
        只回送几片短文本，不跑清洗、不写盘、不改输入文件。一个字都不造：
        片断全是原文的截取。干净书没有广告也没有断行时，用相邻两行真正文
        配成一对 —— clean.py 本来就会把这两行并成一段，演的不是假碎行。

        只送几片短文本给本机界面当动画，不写报告、不进任何 AI 会话
        （redact 那条「真书正文不进 AI 会话」管的是外发，界面不出门）。
        只读前 256 KB：大书不该为了一个等待动画把整本读一遍；EPUB 没有裸
        文本行可飘，返回空清单，界面宁可这一屏安静也不拿假字凑数。
        """
        raw = (path or "").strip().strip('"')
        out: dict = {"drop": [], "merge": []}
        if not raw:
            return out
        try:
            src = Path(raw)
            if not src.is_file() or src.suffix.lower() not in TXT_SUFFIXES:
                return out
            with src.open("rb") as fh:
                data = fh.read(self.EXCERPT_SCAN_BYTES)
            if not data:
                return out
            text = enc_mod.decode(data, enc_mod.best(data).encoding)
        except Exception:
            return out

        lines = text.splitlines()
        n = self.EXCERPT_SNIPPET

        def clean_line(s: str) -> str:
            # 去零宽、压空白、截到屏幕上放得下的一小片；原文一个字不改。
            s = re.sub(r"[\u200b-\u200f\ufeff]", "", s).strip()
            return s[:n]

        def is_body(s: str) -> bool:
            return (
                4 <= len(s) <= 120
                and not match_chapter(s)
                and not judge_ad_line(s, 0)
                and not re.fullmatch(r"[\W\d_]+", s)
            )

        drop: list[str] = []
        merge: list[list[str]] = []
        used: set[int] = set()

        # 先收真广告行：复用清洗那条唯一判据，只收「整行都会删掉」的，
        # 免得等待页划掉一句其实会保留的正文。
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s or len(s) > 60:
                continue
            ev = judge_ad_line(s, i + 1)
            if ev and ev.get("whole_line") and len(s) >= 8 and s not in drop:
                drop.append(clean_line(s))
                used.add(i)
                if len(drop) >= self.EXCERPT_MAX_ITEMS:
                    break

        # 再收断行对：上一行以逗号收尾、这一行接着说，正是 clean.py 会
        # 并回去的碎行。
        for i in range(1, len(lines)):
            if len(merge) >= self.EXCERPT_MAX_ITEMS:
                break
            cur, prev = lines[i].strip(), lines[i - 1].strip()
            if (
                i not in used
                and (i - 1) not in used
                and is_body(prev)
                and is_body(cur)
                and prev[-1] in "，,、；;：:"
            ):
                pair = [clean_line(prev), clean_line(cur)]
                if all(pair):
                    merge.append(pair)
                    used.add(i)
                    used.add(i - 1)

        # 干净书没有广告、也没有断在逗号上的行：拿相邻两行真正文配对。
        # clean.py 本来就会把同一章里相邻的非空行并成一段，演的仍是真事。
        for i in range(1, len(lines)):
            if len(merge) >= self.EXCERPT_MAX_ITEMS:
                break
            cur, prev = lines[i].strip(), lines[i - 1].strip()
            if (
                i not in used
                and (i - 1) not in used
                and is_body(prev)
                and is_body(cur)
            ):
                pair = [clean_line(prev), clean_line(cur)]
                if all(pair) and pair[0] != pair[1]:
                    merge.append(pair)
                    used.add(i)
                    used.add(i - 1)

        out["drop"] = drop
        out["merge"] = merge
        return out

    def import_file(self, path: str) -> dict:
        """登记一次导入，立刻返回 job_id。真正的活在 worker 里。"""
        raw = (path or "").strip().strip('"')
        if not raw:
            return {"job_id": "", "error": "没有拿到文件路径"}
        job = self._new_job()
        session = Session(source=Path(raw), adjust=None)
        with self._lock:
            self._sessions[job.job_id] = session
        self._spawn(job, lambda: self._run_import(job, session))
        return {"job_id": job.job_id}

    def build_epub(self, job_id: str, meta: dict | None = None) -> dict:
        """④ → ⑤：拿这次会话的章节树打成 EPUB。登记 + 起 worker，立刻返回。"""
        with self._lock:
            session = self._sessions.get(job_id)
        if session is None:
            return {"job_id": "", "error": "这次会话已经不在了，请重新拖一次文件"}
        if session.result is None:
            return {"job_id": "", "error": "清洗还没完成，稍等一下"}
        job = self._new_job("build")
        payload = dict(meta or {})
        self._spawn(job, lambda: self._run_build(job, session, payload))
        return {"job_id": job.job_id}

    def cancel(self, job_id: str) -> dict:
        """取消这次任务：界面立刻退回上一屏，后台跑完自己收摊。

        线程掐不断（见 Job.cancel_requested），所以这里只是立旗子、立刻返回 ——
        界面不该等一个"正在取消"的转圈，取消本来就该是瞬间的。
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": "任务不存在（可能是界面重启了）"}
        job.cancel_requested = True
        return {"ok": True}

    def discard(self, job_id: str) -> dict:
        """结果页上的「取消」：这本书不要了，退回首页，把刚落盘的产物收进回收站。

        与 ② 处理中的取消同义 —— 取消 = 这次没发生。区别只是这边会话已经结束，
        worker 早收摊了，得由界面显式说一声。只收本程序产出的那个 _gui 目录，
        绝不碰输入文件；走回收站，误点还能捞回来（A2）。
        """
        with self._lock:
            job = self._jobs.pop(job_id, None)
            self._sessions.pop(job_id, None)
        outdir = ((job.payload or {}).get("outdir") if job else "") or ""
        if outdir:
            _recycle(Path(outdir))
        return {"ok": True}

    def job_poll(self, job_id: str) -> dict:
        """轮询。只读、无副作用、不算任何东西。"""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {
                "done": True,
                "error": "任务不存在（可能是界面重启了）",
                "error_detail": "",
                "error_title": "这个任务找不到了",
                "payload": None,
                "can_continue": False,
            }
        return {
            "done": job.done,
            "error": job.error,
            "error_detail": job.error_detail,
            "error_title": job.error_title,
            "payload": job.payload,
            # 这次失败是"默认拒收、但用户可以硬来"的那一类（目前只有整本糊行）。
            # 界面据此把错误页的「仍然继续」亮出来。
            "can_continue": job.can_continue,
        }

    def _session_of(self, job_id: str):
        """按 job_id 找会话。返回 (session, error)。"""
        with self._lock:
            session = self._sessions.get(job_id)
        if session is None:
            return None, {"error": "这次会话已经不在了，请重新拖一次文件"}
        if session.result is None:
            return None, {"error": "清洗还没完成，稍等一下"}
        return session, None

    def review(self, job_id: str) -> dict:
        """③ 复核页：五类各给前 REVIEW_PAGE 行。

        同步返回（与 preview_cover 同理）：不起 worker。一本 2,000 条删除的书
        建一遍清单实测 30ms 量级，比一次 300ms 的轮询间隔还短，为它起线程
        反而多一层状态。清单算一次就挂在会话上缓存，翻页与点开都不重算。
        """
        session, err = self._session_of(job_id)
        if err:
            return err
        return _review_payload(session)

    def review_more(self, job_id: str, key: str, offset: int = 0, count: int = REVIEW_PAGE) -> dict:
        """复核页滚到底了：再要一批行（决定27）。

        `offset` 是"已经拿到第几行"，不是页码 —— 前端把已有的行数传上来就行。
        """
        session, err = self._session_of(job_id)
        if err:
            return err
        groups = _groups_of(session)
        group = next((g for g in groups if g["key"] == str(key)), None)
        if group is None:
            return {"rows": [], "total": 0, "more": False}
        rows = group["rows"]
        try:
            off = max(0, int(offset))
            num = max(1, min(int(count), 500))
        except (TypeError, ValueError):
            off, num = 0, REVIEW_PAGE
        chunk = rows[off : off + num]
        return {"rows": chunk, "total": len(rows), "more": off + len(chunk) < len(rows)}

    def review_detail(self, job_id: str, key: str, index: int) -> dict:
        """复核页点开某一行：取那一处的原文对照（决定24/25）。

        按需取，不随列表一起送 —— 一本书两千条删除，每条都带前后两段，
        光 JSON 就几十 MB，而用户真正会点开的也就三五条。
        """
        session, err = self._session_of(job_id)
        if err:
            return err
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return {"error": "行号不对"}
        return _detail(session, str(key), idx)

    def apply_adjust(self, job_id: str, adjust: dict | None = None) -> dict:
        """③ → ②：带着人工调整重跑一遍清洗。登记 + 起 worker，立刻返回 job_id。

        「调整不丢」（§4 要点 3）：新的 adjust 会替换掉会话里那一份，重跑成功后
        ② 页看到的数字就是调整后的。撤销一条 = 前端把那一行从 adjust 里去掉
        再调一次本方法 —— 所以这里收的是**完整的**新 adjust，不是增量。
        """
        with self._lock:
            session = self._sessions.get(job_id)
        if session is None:
            return {"job_id": "", "error": "这次会话已经不在了，请重新拖一次文件"}
        if session.result is None:
            return {"job_id": "", "error": "清洗还没完成，稍等一下"}

        payload = adjust or {}
        if not isinstance(payload, dict):
            return {"job_id": "", "error": "调整必须是 JSON 对象"}

        # 形状校验放在这里而不是等 worker 抛：`_parse_adjust` 的结构错会抛
        # ValueError，前端要能立刻看到"你的请求本身不对"，而不是转一圈回来
        # 看到一个"清洗失败"。行号数据错仍然由清洗层忽略并记账（那是设计）。
        try:
            from huoban.clean import _parse_adjust

            _parse_adjust(payload, len(session.result.text.splitlines()))
        except ValueError as exc:
            return {"job_id": "", "error": f"人工调整有问题：{exc}"}

        session.adjust = payload or None
        job = self._new_job()
        # **同一个 Session 要在新 job_id 下再登记一次**：前端拿到新 job_id 之后
        # 会用新 id 继续调 review / build_epub / preview_cover，而这些方法都按
        # job_id 查会话表。这里漏登记的后果是"重跑成功、但下一步全说会话不在了"
        # （实测踩过：review 返回 {"error": ...}，前端读取 chosen 时 KeyError）。
        # 旧 id 一起留着：撤销重跑时前端可能还拿着旧 id。
        with self._lock:
            self._sessions[job.job_id] = session
        self._spawn(job, lambda: self._run_import(job, session))
        return {"job_id": job.job_id}

    def force_import(self, job_id: str) -> dict:
        """② 页错误出口上的「仍然继续」：明知糊行也照旧清洗一遍。

        默认拒收（OneLineBook）是给大多数人的一道墙 —— 原文没有换行，做出来的
        书没有段落。但规则不可能全对，误判一次就把人挡在门外很难受，所以留这个口子。

        旗子挂在 **Session** 上而不是 adjust 里：重跑走的还是同一条 _run_import，
        而 clean.py 的 adjust 只认 protect_lines / protect_ranges / merge_breaks 三个键，
        多一个就 ValueError。挂上之后同一次会话里的后续重跑（apply_adjust）也不会
        再被拦第二次。
        """
        with self._lock:
            session = self._sessions.get(job_id)
        if session is None:
            return {"job_id": "", "error": "这次会话已经不在了，请重新拖一次文件"}
        # 这里**不能**像 import_file / build_epub 那样先查 session.result：
        # 拒收发生在 clean_file 之后、写盘之前（那时一个字节都还没落），
        # session.result 根本还没赋值。这个口子本来就是给"没成功那一次"用的，
        # 拿成功态当门槛会把它自己挡在门外。
        session.extra["force_one_line"] = True

        job = self._new_job()
        # 同一个 Session 要在新 job_id 下再登记一次，理由同 apply_adjust：
        # 前端拿到新 id 之后还要用新 id 继续调 review / build_epub，漏登记就全说会话不在。
        with self._lock:
            self._sessions[job.job_id] = session
        self._spawn(job, lambda: self._run_import(job, session))
        return {"job_id": job.job_id}

    def pick_cover(self, job_id: str) -> dict:
        """选一张自己的图当封面（§8.7）。返回归一化后的预览 data_url。

        为什么在导入阶段就归一化：`epub._load_cover` 完全没有缩放与 EXIF 处理，
        手机拍的 4000×3000 JPEG 会原样进包（几十 MB），而且 Orientation 不归一化
        时部分阅读器会把封面横过来。这一层做「EXIF 方向归一 + 保比例缩到
        1650×2200 以内 + 重编码」，清洗/打包层仍然不引 Pillow，纯 CLI 行为不变。
        """
        import webview

        if self._window is None:
            return {"error": "窗口还没准备好，稍等一下再点"}
        try:
            picked = self._window.create_file_dialog(
                dialog_type=webview.FileDialog.OPEN,
                allow_multiple=False,
                file_types=("图片 (*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.gif)", "所有文件 (*.*)"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not picked:
            return {"cancelled": True}
        src = Path(picked[0])
        if not src.is_file():
            return {"error": f"文件不存在：{src}"}

        with self._lock:
            session = self._sessions.get(job_id)
        if session is None or session.outdir is None:
            return {"error": "这次会话已经不在了，请重新拖一次文件"}

        try:
            data_url = _normalize_cover_image(src, session.outdir / "cover_custom.jpg")
        except Exception as exc:  # noqa: BLE001
            return {"error": f"这张图读不了：{type(exc).__name__}: {exc}"}
        session.extra["cover_source"] = str(src)
        return {"data_url": data_url, "name": src.name}

    def preview_cover(self, job_id: str, opts: dict | None = None) -> dict:
        """画一张封面预览，返回 data_url（约 100KB PNG，同步算完）。

        §5 说 preview_cover 可以同步算：一张 1200×1600 约 119ms，远低于
        "必须起 worker" 的门槛，起线程反而会让用户连点「换一个」时看到错序。

        ★ opts 必须是**位置参数第二项**，不能写成 keyword-only：
          pywebview 的 `js_bridge_call` 把 JS 传的对象当成 `func_params` 里的
          第二个位置参数直接 `func(*func_params)`，写成 `*, template=None`
          会报「takes 2 positional arguments but 3 were given」——
          这是实测踩出来的（`webview/util.py:255`）。

        ★ 2026-10-28 按试用反馈重做：不再分「程序化 / 整幅画」两条路。
          两者合成一个确定性队列（`cover.all_looks`），界面只看见
          「自动生成 / 换一个 / 不要封面 / 选一张图」四个动作。
          opts 里：
            - `look`     ：当前那项（含有 kind/template/theme/background），画它。
                          改书名/作者触发的重画必须带上它：不带就落到
                          下面 `first_look(title)` 那条路，画面跳回队列第一张
            - `step`     ：要"下一个"，配合 `seen` 跳过这次已经给过的
            - `template`/`theme`/`background`：兼容老调用方式，显式指定
            - `title`    ：④ 页用户改过的书名（封面要用它，不是清洗猜的那个）
            - `author`   ：④ 页用户改过的作者，同理
        """
        from gui import cover as cover_mod

        opts = dict(opts or {})
        step = opts.get("step")
        step = bool(step)
        seen = opts.get("seen")
        seen = [str(x) for x in seen] if isinstance(seen, (list, tuple)) else []
        look = opts.get("look")
        look = dict(look) if isinstance(look, dict) and look.get("kind") else None

        if not cover_mod.available():
            return {"error": "本机 Pillow 不可用，画不了程序化封面"}
        with self._lock:
            session = self._sessions.get(job_id)
        if session is None or session.result is None:
            return {"error": "清洗还没完成，稍等一下"}

        # 「用原书封面」：EPUB 导入时原包自带的那张图。
        # 它不是"画"出来的，所以不走 cover_mod，直接缩一张预览。
        # 这里只做预览，真正落盘由 `_cover_for_build` 的 source 分支负责
        # （那条路会做 EXIF 摆正与 1650×2200 归一化）。
        if opts.get("source"):
            src = session.extra.get("source_cover")
            if not src or not Path(src).is_file():
                return {"error": "这本书没有自带封面"}
            try:
                from PIL import Image, ImageOps

                with Image.open(src) as im:
                    im = ImageOps.exif_transpose(im)
                    if im.mode in ("RGBA", "LA", "P"):
                        im = im.convert("RGBA")
                        bg = Image.new("RGB", im.size, (255, 255, 255))
                        bg.paste(im, mask=im.split()[-1])
                        im = bg
                    else:
                        im = im.convert("RGB")
                    im.thumbnail((600, 800), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, format="PNG", optimize=True)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"原书封面读不出来：{type(exc).__name__}: {exc}"}
            return {
                "data_url": "data:image/png;base64," + _b64(buf.getvalue()),
                "look": None,
                "look_key": "",
                "look_name": "原书封面",
                "source": True,
            }

        book = session.result.book
        # 与 _cover_for_build / _run_build 同一条规矩：④ 页带了 author 键，
        # 框里的值就是唯一真相（清空 = 不写作者）；没带才回落清洗猜的。
        # 三处共用 _meta_author，否则预览和成品会出现"预览是我改的、打出来是猜的"。
        title = str(opts.get("title") or "").strip() or (book.title or "")
        author = _meta_author(opts, book)
        try:
            if look is None and not step:
                # 走到这里说明界面没带当前那张 look。④ 页改书名/作者触发的重画
                # 现在都会带上（试用反馈第 1 条：换了几个封面再改字，画面跳回
                # 队列第一张），所以这条只剩"第一次进 ④ 页"和一个都不认识的
                # look 两种情况 —— 那就回确定性起点，与打包时用的那张保持一致。
                look = cover_mod.first_look(title)
            elif step:
                cur = str(opts.get("current") or (cover_mod.look_key(look) if look else "")) or None
                look = cover_mod.next_look(title, cur, seen)
            # 预览用 audit=False：审计在正式打包的 make_cover(audit=True) 里跑，
            # 每次点「换一个」再算一遍对比度只是白白多花 CPU。
            data = cover_mod.make_look(look, title, author,
                                       width=600, height=800, audit=False)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"封面没画出来：{type(exc).__name__}: {exc}"}
        if not data:
            return {"error": "封面没画出来"}
        # 右下角「第几张 / 共几张」那句小字按试用反馈删了。那个数除了这句
        # 小字没有别的读者，后端也就不再整轮扫队列去凑它：index/total 两个键
        # 留着给老调用方，恒为 0，界面拿到 0 不编分母。
        key = cover_mod.look_key(look)
        index = 0
        total = 0
        return {
            "data_url": "data:image/png;base64," + _b64(data),
            "look": look,
            "look_key": key,
            "look_name": look.get("label") or "",
            "index": index,
            "total": total,
            # 下面三个键是给"换一个"与打包回传用的，界面别自己拼
            "template": look.get("template"),
            "theme": look.get("theme"),
            "background": look.get("background"),
        }

    def pick_file(self) -> dict:
        """点"选文件"时调用。返回单元素列表或空。"""
        import webview

        if self._window is None:
            return {"paths": [], "error": "窗口还没准备好，稍等一下再点"}
        try:
            picked = self._window.create_file_dialog(
                dialog_type=webview.FileDialog.OPEN,
                allow_multiple=False,
                # TXT 与 EPUB 都是入口（§12），文件类型过滤也要跟上，
                # 否则文件对话框默认筛掉 .epub，用户得手动改成"所有文件"。
                file_types=("TXT 或 EPUB (*.txt;*.text;*.epub)", "所有文件 (*.*)"),
            )
        except Exception as exc:  # 对话框在个别环境下会抛，别让它把界面卡住
            return {"paths": [], "error": f"{type(exc).__name__}: {exc}"}
        return {"paths": [str(p) for p in (picked or [])]}

    def pick_dir(self, current: str = "") -> dict:
        """选一个输出目录（④ 页「输出到」用）。

        `current` 是当前输入框里的值：对话框从那儿开始找，用户不必每次从
        驱动器根目录点下来。值不合法（被删了/打错了）就退回不指定。
        """
        import webview

        if self._window is None:
            return {"path": "", "error": "窗口还没准备好，稍等一下再点"}
        start = ""
        try:
            cand = Path((current or "").strip()).expanduser()
            if cand.is_dir():
                start = str(cand)
        except (OSError, ValueError):
            start = ""
        kwargs = dict(dialog_type=webview.FileDialog.FOLDER)
        if start:
            kwargs["directory"] = start
        try:
            picked = self._window.create_file_dialog(**kwargs)
        except Exception as exc:  # 对话框在个别环境下会抛
            return {"path": "", "error": f"{type(exc).__name__}: {exc}"}
        if not picked:
            return {"path": ""}          # 用户取消了，不算错误
        # FOLDER 模式下 pywebview 各平台返回形态不一（str / list / tuple），统一取一个。
        if isinstance(picked, (list, tuple)):
            picked = picked[0] if picked else ""
        return {"path": str(picked)}

    def reveal_in_explorer(self, path: str) -> dict:
        """打开文件所在的目录（§5）。给的是文件就打开它所在目录，给目录就直接开。"""
        target = Path((path or "").strip())
        if not target.exists():
            return {"ok": False, "error": f"路径不存在：{target}"}
        folder = target if target.is_dir() else target.parent
        try:
            os.startfile(str(folder))  # noqa: S606 - Windows 专用，正是想要的行为
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True}

    def open_file(self, path: str) -> dict:
        """用系统默认程序打开文件（② 页"查看清洗报告"用）。"""
        target = Path((path or "").strip())
        if not target.is_file():
            return {"ok": False, "error": f"文件不存在：{target}"}
        try:
            os.startfile(str(target))  # noqa: S606
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True}

    def app_info(self) -> dict:
        """启动时调一次：版本号之类的静态信息。

        `azw3` 是"这台机器能不能导 AZW3"——界面据此决定那个复选框是能勾
        还是置灰。探测本身是几次 exists() 检查，不启子进程，开销可以忽略。
        """
        from huoban import __version__ as version  # 有就用，没有就空串
        from huoban.azw3 import available as azw3_available

        return {
            "version": version,
            "max_input_mb": MAX_INPUT_BYTES // 1024 // 1024,
            "azw3": azw3_available() is not None,
        }


def _dumps(data) -> str:
    """写报告用：ensure_ascii=False 让人能直接看，indent=2 便于 diff。"""
    import json

    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _modified_from(src: Path) -> str:
    """dcterms:modified 取**输入文件的修改时间**，不取当前时间（§6 硬约束 2）。

    这是"同样输入打两次得到同一份字节"的关键。取当前时间的话，同一个 TXT
    隔一秒打两次就是两个不同的包，任何"构建产物可复现"都无从谈起。

    与 cli.py 的 `_resolve_modified(None, src)` 同一口径，但这里不提供 "now"
    分支 —— GUI 不给"用当前时间"的开关（§6），界面上没有地方表达这个意图。
    """
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(src), tz=timezone.utc)
    except OSError:
        mtime = datetime.now(timezone.utc)
    return mtime.strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64(data: bytes) -> str:
    """给 data_url 用。放模块级是因为 redact/预览两处都要，重复实现会漂。"""
    import base64

    return base64.b64encode(data).decode("ascii")


# 自选封面图的归一化上限（§8.7 对等 Calibre 的 maximum_cover_size）
COVER_MAX_W, COVER_MAX_H = 1650, 2200
# 归一化后统一存 JPEG：手机原图动辄 4000×3000，压到 1650×2200 的 PNG 反而更大
COVER_JPEG_QUALITY = 88


def _cover_preview(src, *, max_w: int = 700, max_h: int = 933) -> str:
    """把**已经落盘**的成品封面读成一张预览图（data_url）。

    完成页要给用户看一眼"这本书长什么样" —— 封面是这次交付里唯一一件看得见
    摸得着的东西，而它此刻就在产物目录里躺着。所以这里**只读**：不像
    _normalize_cover_image 那样顺手写一遍 JPEG 覆盖回去（那会把 PNG 换成
    JPEG，成品包的封面跟着变，同一个输入就不再逐字节可复现了）。

    读不出来就返回空串 —— 完成页会退化成"没有封面的那本书"的样子，
    而不是整页崩掉。封面读不出来这件事本身已经由 epub.py 报过告警。
    """
    if not src:
        return ""
    try:
        from PIL import Image, ImageOps
    except Exception:  # noqa: BLE001 - 没装 Pillow 时整块降级
        return ""
    path = Path(src)
    try:
        if not path.is_file():
            return ""
        import io

        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            # 带透明通道的 PNG 直接 convert("RGB") 会按黑底压平（§8.5 同源）
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            prev = im.copy()      # copy 之后才能脱离 with 的上下文再写
            prev.thumbnail((max_w, max_h), Image.LANCZOS)
            buf = io.BytesIO()
            prev.save(buf, "JPEG", quality=84, optimize=True)
    except Exception:  # noqa: BLE001 - 一张封面读不出来不该影响完成页
        return ""
    return "data:image/jpeg;base64," + _b64(buf.getvalue())


def _normalize_cover_image(src: Path, target: Path) -> str:
    """EXIF 方向归一 + 保比例缩放 + 重编码，返回 data_url 供界面预览。

    §8.7：`epub._load_cover` 完全没有缩放与 EXIF 处理，手机竖拍的图会原样进包
    （几十 MB），Orientation 不归一化时部分阅读器还会把封面横过来。这一层
    在**导入阶段**做一次，清洗/打包层仍然不引 Pillow，纯 CLI 行为不变。

    ★ PIL alpha 陷阱（§8.5 同源）：带透明通道的 PNG 直接 convert("RGB")
      会按黑底压平，白底图变成黑底。这里先铺白底再贴上去。
    """
    try:
        from PIL import Image, ImageOps
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Pillow 不可用，读不了图片") from exc

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)      # 按 EXIF Orientation 摆正
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        # thumbnail 保比例，且它只在超限时缩 —— 比原图小的不会被放大
        im.thumbnail((COVER_MAX_W, COVER_MAX_H), Image.LANCZOS)
        target.parent.mkdir(parents=True, exist_ok=True)
        im.save(target, "JPEG", quality=COVER_JPEG_QUALITY, optimize=True)

    # 预览用缩略图：1650×2200 的 JPEG 直接 base64 进 data_url 有好几 MB，
    # WebView 塞得下但界面会明显卡一下。预览 480 宽足够。
    with Image.open(target) as im:
        prev = im.copy()
        prev.thumbnail((480, 640), Image.LANCZOS)
        import io

        buf = io.BytesIO()
        prev.save(buf, "JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + _b64(buf.getvalue())

