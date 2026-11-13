# 项目框架

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
                       ├─ about_seal_light.png   关于页印面·浅色（印色 #A83A2E，264×264 RGBA）
                       ├─ about_seal_dark.png    关于页印面·深色（印色 #D65E4D，264×264 RGBA）
                       └─ about_seal_mask.png    手 P 透明底母版（ui.html 内嵌 base64 的独立留存）
CLI 只有四个子命令：`diagnose` / `clean` / `epub` / `reflow`（EPUB 导入）。
GUI 是第五条入口，设计权威是 `docs/gui-design.md`（视图数、js_api 方法表以代码为准）。

## 运行方式

- Python 包是仓库根下的 `huoban/`，运行方式 `python -m huoban diagnose 书.txt`。

### 测试怎么跑
**逐个脚本直跑**，看末行 `N/M passed`；端到端冒烟：`python tools/smoke_test.py`。
基线条数以实跑为准，不要抄进文档。

## 环境
- Windows 10 / Python 3.12.1（开发机；项目本身跨平台，项目根就是仓库根）
- 样本与输出一律留在项目目录内。
