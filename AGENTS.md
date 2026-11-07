# 活板（huoban）

把网上随手下载的、脏乱的中文 TXT 小说，清洗成结构化文档，再打包成 EPUB / AZW3。
（AZW3 走外部 Calibre 的 `ebook-convert`，可选依赖；见 `huoban/azw3.py`。）

## 定位

- **MIT 许可证，已开源。** 不面向公众发布成产品，也不做网络服务；使用者不只有作者本人。
- 开源不改变一件事：**核心清洗逻辑仍然自己写** —— 本项目的价值在边界情况的穷举，这部分抄不来。
  规则与思路可以参考任何公开实现，但别人给的判定条件必须自己在真书上验一遍再收。
- 不提供、不引导任何小说获取渠道。输入永远由使用者自己提供。

## 铁律

1. **永不修改输入文件。** 输入只读，所有输出写到独立目录。
2. **默认先报告，后动手。** 任何破坏性操作（删行、改字符）必须先产出可核对的报告，
   人工确认后再执行。这条借鉴了 77-robot/novel-txt-cleaner 的安全模型。
3. **规则优先，AI 兜底。** 确定性的事（编码、空行、标点、零宽字符、缩进）用规则做完；
   只有需要语义判断的灰色样本才进 AI 队列。顺序反了就会又慢又贵还不准。
4. **中间态是章节树 JSON。** 清洗与打包解耦，EPUB 与 AZW3 都只是渲染器。
   没有这一层，后面每加一种输出格式都要重写清洗。EPUB 导入（G6）也落在这一层：
   导入的产物是同一份章节树，不是第二条流水线。

## 架构

    TXT 原文 / EPUB 成品
      │
      ├─ encoding.py   编码探测（GBK/Big5/UTF-8，靠常用字占比判，不靠 try/except）
      ├─ diagnose.py   体检：只读，输出"这本书是什么状况"          [M1 ✔]
      ├─ clean.py      清洗：行级分类 → 段落重组 → 规范化            [M2 ✔]
      ├─ model.py      章节树 JSON（中间态）                          [M2 ✔]
      ├─ render.py     章节树 → 干净 TXT                              [M3 ✔]
      ├─ epub.py       章节树 → EPUB3 包（zipfile 手写）              [M3 ✔]
      ├─ azw3.py       EPUB → AZW3（调外部 Calibre，可选依赖）        [M3 ✔]
      ├─ fonts.py      字体子集化（可选依赖 fontTools）               [M3 ✔]
      │                 —— 字库本体在项目根 fonts/：思源宋体完整版（24MB）
      │                 + 花园明朝 B（32MB）。两份都是 OFL，但 57MB 不入 git
      │                 （见 .gitignore）；缺了自动退回系统字体，不崩。
      ├─ epubread.py   EPUB → 章节树 + 图片目录（reflow）             [G6 ✔]
      ├─ cleanreport.py/report.py  清洗账单与 markdown 报告
      ├─ redact.py     脱敏：形状描述与代号（真书正文不进 AI 会话）
      └─ ai.py         灰色样本 AI 裁决 + 缓存                        [M4 未开始，文件尚不存在]

    gui/               桌面界面（独立于上面这条线，只调用不改动）
      ├─ app.py        窗建 + 拖放注册 + 资源路径解析（全项目唯一一处）
      ├─ api.py        js_api：import_file（TXT 走 clean.py / EPUB 走 epubread.py，
      │                两条入口两套账单）/ job_poll / review / apply_adjust /
      │                build_epub / preview_cover / pick_* / reveal_in_explorer
      ├─ ui.html       单文件八视图（导入/忙碌/结果/复核/元数据/完成/关于/错误），零构建
      ├─ cover.py      封面：九款版式 × 六套配色 / 纸质底纹 / 九张整幅背景（Pillow 可选依赖）
      │                 九张底图本身就是 AI 生成的，再要新素材也用 skill qwen-image-turbo-t2i
      │                 本机离线出（素材工序，非产品功能；见 README「素材：本地出图」）
      ├─ launcher.py   三级入口共用（python -m gui.app / gui.pyw / exe）
      └─ assets/       图标（tile 方案 + 多尺寸 ico）、textures/（底纹）、backgrounds/（整幅背景）

CLI 只有四个子命令：`diagnose` / `clean` / `epub` / `reflow`（EPUB 导入）。
GUI 是第五条入口，设计权威是 `docs/gui-design.md`（该文档 2026-11-06 已标注「部分过时」：
§4 视图数、§5 js_api 方法表以代码为准）。

## 里程碑

- **M1 体检**（已完成）：`diagnose` 子命令。编码、章节候选、重复行、广告行、段落形态，
  输出 markdown 报告或 JSON。只读，绝不改文件。
  **验收：拿 3 本真实脏书跑通，报告结论与肉眼判断一致。**
- **M2 确定性清洗**（已完成）：编码转 UTF-8、去零宽/行尾空白、压缩空行、合并碎行、
  标点归一、整行广告删除、行内广告切除、目录块剔除、数字顿号/括号分节切章。
  产出 章节树 JSON + 干净 TXT + 清洗报告。
  **验收：真实脏书全量抽检（`tools/batch_check.py`）。100 本第一轮 77 本通过、23 本复核；
  400 本第二轮异常 61 本（15.3%），逐本定性后全部是"短篇集/整本无章节标记/多部合集"这类
  本来就该原样保留的书。幂等：九本主力书两遍清洗逐字节相同；400 本抽检幂等差异 0 本。**
- **M3 打包**（已完成）：章节树 → EPUB（zipfile 手写，不引 ebooklib，它是 AGPL）。
  字体子集化、深色模式适配、诗词排版、封面。
  **AZW3 走外部 `ebook-convert`**（Calibre，可选依赖，只调用不链接其代码）：
  `epub --azw3` 或桌面界面 ④ 页的复选框。**AZW3 不是逐字节可复现的**（实测同一份 EPUB
  连转两次体积相同、sha256 不同，差异 2182 字节散布在 PalmDB 头与压缩流里）——
  要回归比对请比 EPUB，别比 AZW3。
- **G6 EPUB 导入**（已完成）：`reflow` 子命令，EPUB → 章节树 + 图片目录，之后与 TXT 那条线合流。
  重排而非转换：字符级格式丢弃，章节边界以 NCX/nav 为准。验收判据是**回环不动点**
  （导出的包再导回来，第二遍与第三遍逐段完全相同），不是"和原书一模一样"。
  **弹注走三层：认类型 → 保关系（章节树 `anchor`/`refs`）→ 渲成 `epub:type` 链接**，
  缺一层阅读器就不弹窗；跨文件落点由全书锚点表裁决，悬空的还原成纯文字。
- **M4 AI 兜底**（未开始，优先级已下调）：行内广告切除、缺失/畸形章节标题修复、
  语义层面的段落合并判断。必须带缓存与人工复核回放。
  **实测结论：主力书需要语义判断的地方是 0 处，M4 不是当前瓶颈。**

## 代码约定

- Python 3.12，**M1/M2 只用标准库**，不引第三方依赖。
- Python 包是仓库根下的 `huoban/` 目录（模块名 `huoban`），运行方式 `python -m huoban diagnose 书.txt`。
- 任何函数都不许直接 print 业务结果；由 CLI 层负责输出。
- 新增判定规则时，同时在 `tests/` 里加一条能在假数据上失败的用例。
- 中文注释，术语用中文（"碎行""整行广告""行内广告"），别造英文黑话。

### 测试怎么跑（别用 pytest）

**逐个脚本直跑**，看末行 `N/M passed`：

    python tests/test_clean.py

    python tests/test_normalize.py        其余 16 个同理（清单见 README「开发」一节）：
    python tests/test_model.py            test_model / test_diagnose / test_render /
    ...                                   test_cleanreport / test_epub / test_fonts /
                                          test_redact / test_mask / test_batchcheck /
                                          test_epubread / test_encoding / test_cli /
                                          test_cover / test_gui / test_azw3

端到端冒烟：`python tools/smoke_test.py`（9 条，不依赖 pytest）。

**`python -m pytest tests -q` 在本仓库是假绿，不要用。** 这些文件把断言写在模块级、
没有 `if __name__` 保护，pytest 只收得到 `test_all_cases` 那十几个函数 —— 十几条绿
会盖住其余五百多条用例。当前基线：17 个脚本 **586** 条 + smoke **9** = **595**
（2026-11-07 全量重跑实测；数字或清单变了顺手改 README「开发」一节）。

## 环境

- Windows 10 / Python 3.12.1（开发机；项目本身跨平台，项目根就是仓库根）
- 不往 C 盘写大文件；样本与输出一律留在项目目录内。
