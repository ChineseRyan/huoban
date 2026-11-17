# 文档地图（docs/）

> 2026-11-17 更新。**这个目录只有七份现役文档**（界面设计、发版流程、字体许可证、项目框架、bug 修复流程、设计管理流程、设计资产）。
> 要改代码/界面时读现役的那几份。

## 现役（有效）

| 文件 | 是什么 | 什么时候读 |
|---|---|---|
| [gui-design.md](gui-design.md) | 桌面界面的设计权威 | 要改 `gui/` 下任何东西之前读它 |
| [release-package.md](release-package.md) | 发试用包的流程：打包步骤、发出去之前的自查（2026-11-17 起包内与 `dist/` 都不带说明文件） | 准备发版/打包给别人的时候 |
| [licenses/](licenses/README.md) | 两个内嵌字体的许可证原文（OFL 1.1 + Hanazono Font License）与来源说明 | 改字体、发版、或有人问「这字库能商用吗」的时候 |
| [project-framework.md](project-framework.md) | 架构树、入口、运行方式、测试怎么跑、环境 | 改代码或跑测试之前读它 |
| [bugfix-workflow.md](bugfix-workflow.md) | 修 bug 的流程：怎么定位、怎么定版本、修完怎么算完、决策笔记什么时候写 | 接到一个 bug、或修完一个 bug 的时候 |
| [design-workflow.md](design-workflow.md) | 设计需求的流程：需求边界、三稿对比、开发者评判、定稿留痕、落地验收、设计资产同步 | 接到一个设计需求、或出稿/落地的时候 |
| [design-assets.md](design-assets.md) | 设计资产清单：图标 / 底纹 / 背景 / 印面在哪、谁在用、母版与生成关系、新增资产怎么登记 | 要找素材、换素材、加素材之前 |

**`gui-design.md` 是带病上岗的**：2026-11-06 复核发现视图数、js_api 方法表、验收里的测试计数三处与代码不符，
已在原文逐处标注订正块（不是悄悄改掉，因为要看得出「设计当初怎么想的」与「代码现在是什么」的差）。
**「视图」与「后端：`gui/api.py`（js_api）」两节一律以 `gui/ui.html` / `gui/api.py` 为准。**

## 归档与素材

- `archive/` —— 历轮界面交接与逐屏挑刺的历史记录，共五份：`ui-handoff.md`（前端交接）、
  `ui-review-round2.md`（第二轮挑刺记录）、`ui-handover.md`（第 3 轮交接）、
  `ui-handover-round3.md`（第 3 轮收尾交接）、`round3-closing.md`（round3 判据账本）。
  每份开头都有「已归档」警示块：**不是现行规范，别照着它判**。本目录不随公开仓库发布
  （`.gitignore` 忽略，本地留存）。
- `images/` —— README 与文档里的展示图。

## 项目根的另外三份

- `README.md` —— 面向使用者的全貌（能用什么、怎么跑、每个模块在干什么、测试怎么跑）。
- `AGENTS.md` —— 面向改代码的人的契约：铁律 / 代码约定 / 测试约定 / 文档约定 / 版本更新原则 / 项目开发管理（架构与测试怎么跑见 `docs/project-framework.md`）。
- `CHANGELOG.md` —— 每个版本改了什么。
