"""双击启动（不弹控制台）：`pythonw gui.pyw`。

它只是 `gui/launcher.py` 的一层薄壳 —— 真正的入口逻辑（异常落日志、sys.path、
显卡兜底开关）都在 launcher 里，这样 `python -m gui.app`、`gui.pyw`、
`huoban.exe` 三条路走的是同一份代码，不会出现"开发时好、打包后坏"。

放根目录而不是 gui/ 里：双击的人从资源管理器看过来，根目录多一个 huoban
相关的图标最直观；gui/ 内部只放代码。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.launcher import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
