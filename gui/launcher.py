"""打包版入口：`dist/huoban/huoban.exe` 跑的就是它。

为什么单独一个 launcher 而不是直接把 `gui/app.py` 当入口：

1. **无控制台意味着没有地方接崩溃**。`console=False` 时异常只能进
   `sys.stderr`（不存在的东西），用户看到的是"双击没反应"。这里装一个
   excepthook，把完整栈写进 exe 旁边的 `huoban-error.log`，出事了有据可查。
2. 开发期的 `python -m gui.app` 与打包版要走同一条 `main()`，但打包版需要
   先补 `sys.path`（onedir 里包在 `_internal/`，PyInstaller 已经处理，但
   直接跑源码时不处理会 import 不到）。
3. 双击 `gui.pyw` 与双击 exe 的行为必须一致 —— 两者都调这个 `main()`。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _log_dir() -> Path:
    """日志放哪儿：打包版放 exe 同目录；开发期放项目根。

    不写 TEMP —— 用户按"出问题了"时会去 exe 旁边找日志，不会想到系统
    临时目录（有些机器上它还在别的盘或内存盘）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _install_excepthook() -> None:
    def hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            target = _log_dir() / "huoban-error.log"
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(text)
                fh.write("\n" + "-" * 60 + "\n")
        except Exception:  # noqa: BLE001 - 记日志本身不能把程序再弄崩一次
            pass
        # 有控制台（开发期 / 调试）就照常打出来。
        if sys.stderr is not None:
            sys.stderr.write(text)

    sys.excepthook = hook


def _selftest() -> int:
    """打包版自检：不开窗口，直接把产线跑一遍，结果写进 exe 旁边的 json。

    为什么需要它：exe 是 `console=False`，双击没反应时**没有任何地方**能说明
    是哪一层坏了 —— 是 PyInstaller 少收了 huoban 包、Pillow 编解码不在、
    还是字体子集化那头失败。这里把「清洗 → 打包 → 校验」整条链路跑一遍，
    每步各写一条，出问题能直接看出断在哪。
    """
    import json
    import tempfile
    import time
    import traceback

    report: dict = {"frozen": bool(getattr(sys, "frozen", False)), "steps": []}

    def step(name: str, fn):
        t0 = time.perf_counter()
        try:
            detail = fn()
        except BaseException as exc:  # noqa: BLE001
            report["steps"].append(
                {
                    "step": name,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-1200:],
                }
            )
            return None
        report["steps"].append(
            {"step": name, "ok": True, "ms": round((time.perf_counter() - t0) * 1000), "detail": detail}
        )
        return detail

    def s_version():
        from huoban import __version__

        return {"huoban": __version__}

    def s_imports():
        import importlib

        # `huoban.azw3` 与 `huoban.epubread` 一定要在这里点名：它们在
        # gui/api.py 里都是**函数内 import**（只有真去导 AZW3 / 真去导 EPUB
        # 时才走到），打包漏掉的话界面平时好好的、用户拖了 .epub 或勾了
        # 「同时导出 AZW3」才炸 —— console=False 的 exe 上那是最难查的一类。
        mods = (
            "webview", "PIL", "PIL.Image", "fontTools",
            "huoban.clean", "huoban.epub", "huoban.azw3",
            "huoban.epubread",
            "gui.api", "gui.cover",
        )
        out = {}
        for mod in mods:
            try:
                importlib.import_module(mod)
                out[mod] = True
            except Exception as exc:  # noqa: BLE001
                out[mod] = f"{type(exc).__name__}: {exc}"
        return out

    def s_resources():
        from gui.app import _resource

        ui = _resource("ui.html")
        ico = _resource("assets/huoban.ico")
        return {
            "ui_html": [str(ui), ui.is_file(), ui.stat().st_size if ui.is_file() else 0],
            "icon": [str(ico), ico.is_file(), ico.stat().st_size if ico.is_file() else 0],
        }

    def s_fonts():
        """封面用的字体是从哪儿来的。

        用户报"封面字跟我这边不一样"时，这一条能直接回答是包内自带那份、还是
        系统里装的另一份（字重/字形会有差异）。两条都空 = 这台机器出不了封面。
        """
        from gui import cover

        out = {}
        for kind in ("serif", "sans"):
            path = cover._font_path(kind)
            bundled = Path(cover.__file__).resolve().parents[1] / "fonts"
            out[kind] = {
                "path": path,
                "bundled": bool(path) and Path(path).parent == bundled,
            }
        out["font_dirs"] = [str(d) for d in cover._font_dirs()[:3]]
        return out

    def s_pipeline():
        from gui.api import Api

        book = "第一章 开始\n\n正文一段。\n\n天才一秒记住本站\n\n正文二段。\n\n第二章 结束\n\n又一段。\n"
        with tempfile.TemporaryDirectory(prefix="huoban-selftest-") as tmp:
            src = Path(tmp) / "selftest.txt"
            src.write_bytes(book.encode("gbk"))
            api = Api()
            jid = api.import_file(str(src))["job_id"]
            for _ in range(600):
                poll = api.job_poll(jid)
                if poll["done"]:
                    break
                time.sleep(0.05)
            if poll.get("error"):
                raise RuntimeError(f"清洗失败：{poll['error']}")
            payload = poll["payload"]
            built = api.build_epub(
                jid, {"title": "自检", "author": "", "language": "zh-CN", "fonts": False}
            )
            for _ in range(1200):
                poll2 = api.job_poll(built["job_id"])
                if poll2["done"]:
                    break
                time.sleep(0.05)
            if poll2.get("error"):
                raise RuntimeError(f"打包失败：{poll2['error']}")
            ep = poll2["payload"]
            out = {
                "chapters": payload["stats"]["chapters"],
                "dropped_ad": payload["dropped"]["ad"],
                "epub_name": ep["name"],
                "epub_bytes": ep["size_bytes"],
                "epub_entries": ep["entries"],
            }
            # 顺手验一下 AZW3：打包版里 spawn 外部进程最容易出问题的地方
            # （PATH、CREATE_NO_WINDOW、frozen 下的 cwd）。没装 Calibre 不算失败 ——
            # 那是可选依赖，只如实记 available: false。
            from huoban.azw3 import available as azw3_available
            from huoban.azw3 import convert as convert_azw3

            exe = azw3_available()
            if exe is None:
                out["azw3"] = {"available": False}
            else:
                az = Path(ep["epub"]).with_suffix(".azw3")
                got = convert_azw3(Path(ep["epub"]), az)
                out["azw3"] = {
                    "available": True,
                    "exe": exe,
                    "name": az.name,
                    "bytes": got.size_bytes,
                    "seconds": round(got.seconds, 2),
                }

            # 再把刚打出来的 EPUB **拖回去导一遍** —— 这是唯一能证明打包版里
            # epubread 真的可用的办法（它在 gui/api.py 里是函数内 import，
            # 静态分析漏掉时 exe 只在用户拖 .epub 那一刻才崩）。
            # 用自己刚生成的包，不依赖任何样本文件，自检可以脱离仓库跑。
            jid2 = api.import_file(ep["epub"])["job_id"]
            for _ in range(600):
                poll3 = api.job_poll(jid2)
                if poll3["done"]:
                    break
                time.sleep(0.05)
            if poll3.get("error"):
                raise RuntimeError(f"EPUB 导入失败：{poll3['error']}")
            p3 = poll3["payload"]
            out["epub_import"] = {
                "kind": p3.get("kind"),
                "chapters": p3["stats"]["chapters"],
                "epub_version": p3["import"]["epub_version"],
                "images_kept": p3["import"]["images_kept"],
            }
            return out

    step("version", s_version)
    step("imports", s_imports)
    step("resources", s_resources)
    step("fonts", s_fonts)
    step("pipeline", s_pipeline)

    report["ok"] = all(s["ok"] for s in report["steps"])
    target = _log_dir() / "huoban-selftest.json"
    try:
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["written_to"] = str(target)
    except Exception:  # noqa: BLE001
        pass
    if sys.stdout is not None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def main() -> int:
    _install_excepthook()

    # 开发期（非打包）直接跑源码时，把项目根加进 sys.path，
    # 否则 `import gui` / `import huoban` 都找不到。
    if not getattr(sys, "frozen", False):
        root = str(Path(__file__).resolve().parents[1])
        if root not in sys.path:
            sys.path.insert(0, root)

    if "--selftest" in sys.argv:
        return _selftest()

    # WebView2 在部分机器上会因为显卡驱动问题白屏。设这个开关后它走软件
    # 渲染，慢一点但一定能画出来。只在用户显式要求时开，别默认牺牲性能。
    if os.environ.get("HUOBAN_SOFTWARE_RENDER"):
        os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu")

    from gui.app import main as run_app

    run_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
