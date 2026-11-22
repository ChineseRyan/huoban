"""窗口入口：`python -m gui.app`（打包后是 `huoban.exe`）。

这个文件只做四件事，别的都不做：

1. 解析资源路径（开发期与 `sys._MEIPASS` 两种，只此一处）
2. 建窗口、把 `Api()` 挂成 js_api
3. **在 Python 端**注册 drop 监听（docs/gui-design.md 的「三个实现陷阱」一节陷阱 1：HTML 里写 ondrop 拿不到绝对路径）
4. 只 `load_html` 一次（docs/gui-design.md 的「三个实现陷阱」一节陷阱 2：二次加载会清空 DOM 元素表、拖放监听全丢）

界面的一切变化都由 `run_js` 触发，见 `ui.html` 里的 `setView()`。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .api import Api

WINDOW_TITLE = "活板"


def _resource(name: str) -> Path:
    """开发期取 `gui/` 下的文件，打包后取 PyInstaller 解包出来的临时目录。

    只判断 `sys._MEIPASS`，不用 `Path(__file__).parent` —— 后者的相对关系
    在 onedir 里和源码树里是不一样的，这类"两个地方各算一次"的路径 bug 最难查。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "gui" / name
    return Path(__file__).resolve().parent / name


def main() -> int:
    import webview

    api = Api()
    icon = _resource("assets/huoban.ico")
    window = webview.create_window(
        WINDOW_TITLE,
        html=_read_ui(),
        js_api=api,
        width=1040,
        height=760,
        min_size=(720, 520),
        # 纸色底：先画这块纯色，等界面渲染完再换上来。不加会在标题栏附近
        # 闪一下白（docs/gui-design.md 的「为什么是 pywebview」一节所列的代价 2）。和 ui.html 的 --paper 是同一个值。
        background_color="#F1EFE8",
        text_select=False,
    )
    api._window = window  # pick_file 需要窗口对象，只能在建好之后注入（见 api.py 的注释）

    window.events.loaded += lambda: _register_drop(window)
    webview.start(icon=str(icon) if icon.is_file() else None, debug=_debug())
    return 0


def _read_ui() -> str:
    """读界面 HTML。编码固定写死 utf-8：界面里有中文，用默认编码在
    Windows 上会按 cp936 解，直接乱码。"""
    ui = _resource("ui.html")
    try:
        return ui.read_text(encoding="utf-8")
    except OSError as exc:
        # 走到这里基本等于打包漏了 ui.html，给一句能定位的话，比空窗口好。
        raise SystemExit(f"界面文件读取失败：{ui}（{exc.strerror}）") from exc


def _register_drop(window) -> None:
    """注册拖放。

    **必须用 `DOMEventHandler(..., prevent_default=True)`**：裸回调不会注入
    `e.preventDefault()`，而且只有在 `Element.on` 里走 DOMEventHandler 分支时
    pywebview 才会把 `_dnd_state['num_listeners']` 加一 —— 那个计数为 0 时，
    平台层收到 FilesDropped 会直接丢弃，路径永远到不了 Python 侧。

    注意这两件事是分开的，别混：
      · **取绝对路径**只能在这里做（`pywebviewFullPath` 只在这一侧存在）；
      · **拦默认动作** JS 侧也要兜一层（见 gui/ui.html 的 drop 监听器）——
        本函数要等活动页加载完、再跑两次 evaluate_js 才生效，之前有一段窗口期。

    `get_element("body")` 可能返回 None（pywebview 的 `dom/dom.py` 是
    `return Element(...) if node_id else None`），直接 `.on()` 会抛
    AttributeError，而这个回调是在 `loaded` 事件里跑的 —— 抛出去就静默丢了，
    界面上表现为"拖了没反应"，没有任何线索。所以这里重试几次、失败留日志。
    """
    import webview

    def on_drop(event) -> None:
        paths = _dropped_paths(event)
        if not paths:
            return
        # 本期只接一个文件（docs/gui-design.md 的「三个实现陷阱」一节陷阱 3）：多个同名 basename 时 pywebview 按
        # unquote 后的文件名匹配并 remove，中途会配错。接第一个是"确定的"。
        window.evaluate_js(
            "window.__onDropPath && window.__onDropPath(%s)" % _js_str(paths[0])
        )

    handler = webview.dom.DOMEventHandler(on_drop, prevent_default=True)
    # body 理论上一定存在，但 evaluate_js 有往返延迟、loaded 也可能早于解析完，
    # 所以给它几次机会。间隔逐次拉长：第一次没拿到多半只差一两帧。
    last_error: Exception | None = None
    for delay in (0, 0.05, 0.2, 0.5):
        if delay:
            time.sleep(delay)
        try:
            element = window.dom.get_element("body")
            if element is None:
                last_error = LookupError("document.querySelector('body') 取不到节点")
                continue
            element.on("drop", handler)
            return
        except Exception as exc:  # noqa: BLE001 - 注册失败不许把程序带崩
            last_error = exc
    # 走到这里：拖放这条路没接上。写日志（不 print —— 库代码只返回数据，
    # 日志走入口侧的既有惯例，见 gui/launcher.py 的 huoban-error.log）。
    _log_drop_register_failure(last_error)


def _log_drop_register_failure(exc: Exception | None) -> None:
    """拖放注册失败时写一行可定位的日志。

    为什么必须留痕：失败的表现是"拖文件没反应"，用户没法描述得更具体，
    没有日志就完全查不出是 `body` 取不到、还是 pywebview 改了内部结构。
    日志位置与 gui/launcher.py 的 excepthook 一致（exe 旁 / 项目根）。
    """
    lines = [
        "拖放注册失败：drop 监听器没能挂到 body 上，拖文件将没有反应。",
        f"原因：{type(exc).__name__}: {exc}" if exc else "原因：未知",
        "",
    ]
    try:
        from gui.launcher import _log_dir

        target = _log_dir() / "huoban-error.log"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n" + "-" * 60 + "\n")
    except Exception:  # noqa: BLE001 - 记日志本身不能把程序再弄崩一次
        pass


def _dropped_paths(event: dict) -> list[str]:
    """从事件里取绝对路径。

    pywebview 在 `util.py` 的 pywebviewEventHandler 分支里把绝对路径写进
    `dataTransfer.files[i]['pywebviewFullPath']`，这个字段**只在这一侧存在**，
    JS 里的 File 对象上没有。取不到就用空列表，让上层静默忽略 ——
    拖进一段网页文字也会触发 drop，那不是错误。
    """
    transfer = (event or {}).get("dataTransfer") or {}
    files = transfer.get("files") or []
    out: list[str] = []
    for item in files:
        path = (item or {}).get("pywebviewFullPath") or ""
        if path:
            out.append(path)
    return out


def _js_str(value: str) -> str:
    """把 Python 字符串变成能安全塞进 evaluate_js 的字面量。

    文件名里可能有反斜杠、引号、甚至换行。`json.dumps` 输出的转义序列
    正好是合法的 JS 字符串字面量，比手写 replace 可靠。
    """
    import json

    return json.dumps(value, ensure_ascii=False)


def _debug() -> bool:
    """开发期开 Inspector；打包后关掉（用户不该看到调试器入口）。

    顺带记一条平台事实（决定 6）：pywebview 把右键菜单、浏览器快捷键、
    DevTools、状态栏全部绑在同一个 debug 开关上，所以正式包里没有右键。
    这不是界面漏绑，是平台行为，别当 bug 去修。
    """
    return bool(getattr(sys, "_MEIPASS", None) is None and "--debug" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
