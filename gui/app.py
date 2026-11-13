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

    window.dom.get_element("body").on(
        "drop", webview.dom.DOMEventHandler(on_drop, prevent_default=True)
    )


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
