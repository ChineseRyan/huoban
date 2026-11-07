"""AZW3 导出：调用外部 Calibre 的 `ebook-convert`，不链接它的任何代码。

## 为什么是外部命令

Calibre 是 GPLv3。本项目只**调用**它的可执行文件（用户自己装的），
不 import、不 vendored、不复制其源码 —— 这是 AGPL/GPL 边界上最干净的做法，
也是 AGENTS.md「M3 打包」一节原本就定下的方案：AZW3 是**可选依赖**，
没装 Calibre 就不提供这个能力，EPUB 那条线完全不受影响。

## 不可复现，如实说明

EPUB 是逐字节可复现的（同样的书 + 同样的 `modified` → 同样的 sha256，
见 docs/gui-design.md §8.5）。**AZW3 不是。** 实测：同一份 1.6 MB 的 EPUB
连转两次，产物体积完全相同（1,431,507 B），但 sha256 不同，差异 2182 字节
散布在 PalmDB 头与正文压缩流里。`--pubdate` 能固定元数据里的发布日期，
但压不住这个差异 —— Calibre 内部有与时间/随机相关的因素，那不是我们
能控制的。所以：**不要拿 AZW3 做回归比对**，要比对就比 EPUB。

## 为什么固定 `--no-inline-toc`

`ebook-convert -h` 对这个选项的原文是「不要为书籍添加目录。如果书籍本身
就有目录，则应启用本选项。」本项目的 EPUB 一定带 nav.xhtml + toc.ncx
（两级目录），再加一份 Calibre 生成的 HTML 目录页就是重复的。

## 为什么不传 `--output-profile kindle`

那个 profile 会为 Kindle 的小屏重新缩放图片（正文文字不受影响）。本项目的
书大多是纯文字，但 reflow 导入的书可能带插图 —— 缩了就是白白丢分辨率。
宁可让 Calibre 用默认值，也不替用户做这个决定。

## 为什么自己给 Calibre 指定临时目录

Calibre 会在系统临时目录下建 `calibre-xxxxxxxx` 干活，系统临时目录取自
环境变量 `TEMP`（`calibre.ptempfile.get_windows_temp_path()` 就是读它）。

实测（本机，2026-10-27）：同一份代码，直接跑 `python -m gui.launcher --selftest`
时 Calibre 一切正常；**打包成 exe 之后再跑就失败**：

    Azw3Error: ebook-convert 返回 1：...
    PermissionError: [WinError 5] 拒绝访问。:
        '<临时目录>\\calibre-ezyxmo2a'

（某些机器上 `TEMP` 被重定向到别处、且路径带 8.3 短名；
该目录是普通目录、不是重解析点，ACL 也正常。用同样的 TEMP 让 python 自己
`mkdtemp` 能成功、让 Calibre 以 python 为父进程启动也能成功 —— 只在"父进程是
打包后的 exe"时失败。根因没有查到底，也不值得为了它去猜。）

所以这里不依赖环境里的 `TEMP`：`convert()` 在**输出文件旁边**建一个临时目录
（那个目录一定可写 —— 我们刚把 EPUB 写在那里），通过 `env` 把 `TEMP`/`TMP`
指过去，Calibre 就用它。转换结束由 `TemporaryDirectory` 清掉。
顺带的好处：Calibre 的中间文件与输入输出同盘，不会因为系统临时目录在别的盘
（甚至内存盘）而多一次整本拷贝。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Azw3Error",
    "Azw3Result",
    "Azw3Unavailable",
    "ENV_VAR",
    "available",
    "convert",
]

# 转换超时（秒）。真书实测 1.4–1.8 秒（1.6 MB EPUB）；给 300 秒是留给
# 图片特别多的书 —— 一本 200 MB 的漫画集在慢盘上可能要几分钟。
DEFAULT_TIMEOUT = 300

# 找 exe 的顺序：环境变量 > PATH > 两个标准安装目录。
# 不遍历注册表、不扫全盘 —— 那些都会让"打开一个功能"变成"等十秒"。
_ENV_VAR = "HUOBAN_EBOOK_CONVERT"
# 公开一份给 CLI/界面用来拼提示语（"在环境变量 X 里指到 …"），
# 免得同一个字符串在两个地方各写一遍然后漂移。
ENV_VAR = _ENV_VAR
_PROGRAM_DIRS = (
    r"C:\Program Files\Calibre2",
    r"C:\Program Files (x86)\Calibre2",
)


class Azw3Unavailable(RuntimeError):
    """没装 Calibre 的 ebook-convert。这是"这个功能不可用"，不是"转换失败"。"""


class Azw3Error(RuntimeError):
    """转换真的失败了：超时、返回码非 0、产物没生成。"""


@dataclass
class Azw3Result:
    """一次成功的转换。`seconds` 是墙钟时间，写进摘要给人看。"""

    path: Path
    size_bytes: int
    seconds: float


def available() -> str | None:
    """找得到 ebook-convert 就返回它的路径，找不到返回 None。

    这个函数不抛异常：界面要先问"能不能用"再决定要不要显示这个按钮。
    """
    override = os.environ.get(_ENV_VAR)
    if override:
        p = Path(override)
        if p.is_file():
            return str(p)
        # 环境变量指错了不该静默回落到 PATH —— 那样用户会以为自己配置生效了。
        return None
    found = shutil.which("ebook-convert") or shutil.which("ebook-convert.exe")
    if found:
        return found
    for d in _PROGRAM_DIRS:
        candidate = Path(d) / "ebook-convert.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _tail(text: str, lines: int = 3) -> str:
    """取输出末尾几行做错误说明。Calibre 会往 stdout 打几十行进度。"""
    parts = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return " / ".join(parts[-lines:]) if parts else ""


def convert(
    epub_path: Path | str,
    out_path: Path | str,
    *,
    pubdate: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    exe: str | None = None,
) -> Azw3Result:
    """把 EPUB 转成 AZW3。成功返回 Azw3Result，失败抛异常。

    参数：
      epub_path  输入 EPUB（通常是本项目刚打出来的那个）
      out_path   输出 AZW3；父目录不存在会自动建
      pubdate    元数据里的发布日期（ISO 8601）。给了就固定下来，
                 不给则 Calibre 用"当前时间"，每次转换的元数据都不一样。
                 注意：这**只**固定元数据，产物字节仍然不可复现（见模块文档）。
      timeout    秒；超时会 kill 掉子进程再抛 Azw3Error
      exe        显式指定 ebook-convert 路径（测试用）；不给就自己找

    不打印任何东西 —— 摘要与报错由 CLI / 界面层负责。
    """
    exe = exe or available()
    if not exe:
        raise Azw3Unavailable(
            "没找到 Calibre 的 ebook-convert：装一个 Calibre，或者把 "
            f"{_ENV_VAR} 指到 ebook-convert.exe 的完整路径"
        )
    src = Path(epub_path)
    dst = Path(out_path)
    if not src.is_file():
        raise Azw3Error(f"输入 EPUB 不存在：{src}")
    # 输入永远只读。输出指到输入本身是最容易犯的自杀操作。
    try:
        if dst.exists() and dst.samefile(src):
            raise Azw3Error(f"输出路径与输入文件相同，拒绝写入：{dst}")
    except OSError:
        pass
    if dst.parent and not dst.parent.is_dir():
        dst.parent.mkdir(parents=True, exist_ok=True)

    cmd = [exe, str(src), str(dst), "--no-inline-toc"]
    if pubdate:
        cmd += ["--pubdate", pubdate]

    started = time.monotonic()
    # Calibre 的临时目录由我们自己给（见模块文档最后一节）：放在输出文件旁边，
    # 那个目录一定可写，也不必依赖环境里的 TEMP 是否正常。用完自动清掉。
    try:
        scratch = tempfile.TemporaryDirectory(prefix=".huoban-azw3-", dir=dst.parent)
    except OSError as exc:
        # 建不出临时目录就直接让它用系统 TEMP，能跑就跑 —— 这不该成为失败理由。
        scratch = None
        env = None
    else:
        env = dict(os.environ)
        env["TEMP"] = env["TMP"] = scratch.name
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            # 不弹控制台窗口：GUI 里调用时闪一个黑框是很糟的观感。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        # 子进程已被 subprocess 杀掉（run 的 timeout 会 kill 再 wait）
        raise Azw3Error(f"转换超时（{timeout:g} 秒）—— 书太大或磁盘太慢") from None
    except OSError as exc:
        raise Azw3Error(f"启动 ebook-convert 失败：{exc.strerror or exc}") from exc
    finally:
        if scratch is not None:
            scratch.cleanup()

    seconds = time.monotonic() - started
    if proc.returncode != 0:
        detail = _tail(proc.stderr) or _tail(proc.stdout)
        raise Azw3Error(
            f"ebook-convert 返回 {proc.returncode}"
            + (f"：{detail}" if detail else "")
        )
    if not dst.is_file():
        raise Azw3Error(f"转换声称成功，但产物没出现：{dst}")
    return Azw3Result(path=dst, size_bytes=dst.stat().st_size, seconds=seconds)
