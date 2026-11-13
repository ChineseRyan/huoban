"""图形界面层。

与 `huoban/` 的关系：**只调用，不修改**。G1–G3 全程不碰 `clean.py`（见
docs/gui-design.md 的「里程碑」一节）。这一层的价值是"把同一套清洗跑到人眼前"，不是再造一遍清洗。

模块划分（与 docs/gui-design.md 的「后端：`gui/api.py`」「打包与运行」两节对齐）：

- `api.py`  js_api 对象：登记 → 起 worker → 立即返回 job_id；前端只轮询一个方法
- `app.py`  窗口、拖放注册、资源路径解析（唯一会被 `python -m gui.app` 执行的文件）
- `ui.html` 单文件界面，零构建、零 npm

为什么 `__init__.py` 是空的却还要存在：`python -m gui.app` 需要一个包；
用 `gui/app.py` 直接跑也可以，但那样相对导入与 `sys.path` 会随工作目录变化，
而打包后的 onedir 结构与开发期不同，路径问题必须只有一处（app.py 里的 `_resource`）。
"""
