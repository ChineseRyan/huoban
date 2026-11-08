# 活板（huoban）
把网上随手下载的、脏乱的中文 TXT 小说，清洗成结构化文档，再打包成 EPUB / AZW3。
（AZW3 走外部 Calibre 的 `ebook-convert`，可选依赖；见 `huoban/azw3.py`。）
## 定位
- **MIT 许可证，已开源。** 不面向公众发布成产品，也不做网络服务；使用者不只有作者本人。
- 开源不改变一件事：**核心清洗逻辑仍然自己写** —— 本项目的价值在边界情况的穷举，这部分抄不来。规则与思路可以参考任何公开实现，但别人给的判定条件必须自己在真书上验一遍再收。
- 不提供、不引导任何小说获取渠道。输入永远由使用者自己提供。
## 铁律
1. **永不修改输入文件。** 输入只读，所有输出写到独立目录。
2. **默认先报告，后动手。** 任何破坏性操作（删行、改字符）必须先产出可核对的报告，人工确认后再执行。这条借鉴了 77-robot/novel-txt-cleaner 的安全模型。
3. **规则优先，AI 兜底。** 确定性的事（编码、空行、标点、零宽字符、缩进）用规则做完；只有需要语义判断的灰色样本才进 AI 队列。顺序反了就会又慢又贵还不准。
4. **中间态是章节树 JSON。** 清洗与打包解耦，EPUB 与 AZW3 都只是渲染器。没有这一层，后面每加一种输出格式都要重写清洗。EPUB 导入（G6）也落在这一层：导入的产物是同一份章节树，不是第二条流水线。
5. **涉及语义判断的活一律不做，而且要主动拒绝。** 不同人对同一段文字理解不同，工具不去猜，
   直接告诉用户「这类书我不处理」。理由与被否方案见 `docs/notes/no-semantic-judgment.md`。
## 架构
    TXT 原文 / EPUB 成品
      ├─ encoding.py   编码探测（靠常用字占比判，不靠 try/except）
      ├─ diagnose.py   体检：只读，输出"这本书是什么状况"
      ├─ clean.py      清洗：行级分类 → 段落重组 → 规范化
      ├─ model.py      章节树 JSON（中间态）
      ├─ render.py     章节树 → 干净 TXT
      ├─ epub.py       章节树 → EPUB3 包（zipfile 手写）
      ├─ azw3.py       EPUB → AZW3（调外部 Calibre，可选依赖）
      ├─ fonts.py      字体子集化；字库本体与许可见 docs/notes/fonts-57mb-not-in-git.md
      ├─ epubread.py   EPUB → 章节树 + 图片目录（reflow）
      ├─ cleanreport.py/report.py  清洗账单与 markdown 报告
      └─ redact.py     脱敏：形状描述与代号（真书正文不进 AI 会话）

    gui/               桌面界面（独立于上面这条线，只调用不改动）
      ├─ app.py        窗建 + 拖放注册 + 资源路径解析（全项目唯一一处）
      ├─ api.py        js_api：import_file / job_poll / review / apply_adjust / build_epub / preview_cover / pick_* / reveal_in_explorer
      ├─ ui.html       单文件八视图（导入/忙碌/结果/复核/元数据/完成/关于/错误），零构建
      ├─ cover.py      封面：九款版式 × 六套配色 / 底纹 / 九张整幅背景（Pillow 可选依赖）
      │                 素材工序，非产品功能；见 docs/notes/
      ├─ launcher.py   三级入口共用（python -m gui.app / gui.pyw / exe）
      └─ assets/       图标 / textures/（底纹）/ backgrounds/（整幅背景）
CLI 只有四个子命令：`diagnose` / `clean` / `epub` / `reflow`（EPUB 导入）。
GUI 是第五条入口，设计权威是 `docs/gui-design.md`（§4 视图数、§5 js_api 方法表以代码为准）。
## 代码约定
- Python 包是仓库根下的 `huoban/`，运行方式 `python -m huoban diagnose 书.txt`。
- 任何函数都不许直接 print 业务结果；由 CLI 层负责输出。
- **任何文档不得手抄行号、章节号、测试条数。** 定位以章节名为准，基线以实跑为准，不抄进文档。
- 新增判定规则时，同时在 `tests/` 里加一条能在假数据上失败的用例。
- 中文注释，术语用中文（"碎行""整行广告""行内广告"），别造英文黑话。
### 测试怎么跑（别用 pytest）
**逐个脚本直跑**，看末行 `N/M passed`；端到端冒烟：`python tools/smoke_test.py`。
基线条数以实跑为准，不要抄进文档。
## 环境
- Windows 10 / Python 3.12.1（开发机；项目本身跨平台，项目根就是仓库根）
- 样本与输出一律留在项目目录内。
## 为什么这么定
`docs/notes/` 是本机的决策记录，**不随仓库发布**。凡遇「为什么这么定」先在那里查；
查不到就问作者，**不要自己推断**。往被约束的代码处留一行指回对应笔记，是本项目惯例。
