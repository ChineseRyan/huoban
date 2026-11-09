# 文档地图（docs/）

> 2026-11-09 更新。**这个目录只有六份现役文档**（界面设计、发版流程、字体许可证、项目框架、bug 修复流程、设计资产）。
> 要改代码/界面时读现役的那几份。

## 现役（有效）

| 文件 | 是什么 | 什么时候读 |
|---|---|---|
| [gui-design.md](gui-design.md) | 桌面界面的设计权威 | 要改 `gui/` 下任何东西之前读它 |
| [release-package.md](release-package.md) | 发试用包的流程：包内 `使用说明.txt` 的模板、打包步骤、发出去之前的三条自查 | 准备发版/打包给别人的时候 |
| [licenses/](licenses/README.md) | 两个内嵌字体的许可证原文（OFL 1.1 + Hanazono Font License）与来源说明 | 改字体、发版、或有人问「这字库能商用吗」的时候 |
| [project-framework.md](project-framework.md) | 架构树、入口、运行方式、测试怎么跑、环境 | 改代码或跑测试之前读它 |
| [bugfix-workflow.md](bugfix-workflow.md) | 修 bug 的流程：怎么定位、怎么定版本、修完怎么算完、决策笔记什么时候写 | 接到一个 bug、或修完一个 bug 的时候 |
| [design-assets.md](design-assets.md) | 设计资产清单：图标 / 底纹 / 背景 / 印面在哪、谁在用、母版与生成关系、新增资产怎么登记 | 要找素材、换素材、加素材之前 |

**`gui-design.md` 是带病上岗的**：2026-11-06 复核发现 §4 视图数、§5 方法表、§10 测试计数三处与代码不符，
已在原文逐处标注订正块（不是悄悄改掉，因为要看得出「设计当初怎么想的」与「代码现在是什么」的差）。
**§4 / §5 一律以 `gui/ui.html` / `gui/api.py` 为准。**

## 项目根的另外三份

- `README.md` —— 面向使用者的全貌（能用什么、怎么跑、每个模块在干什么、测试怎么跑）。
- `AGENTS.md` —— 面向改代码的人的契约：铁律 / 代码约定 / 版本更新原则（架构与测试怎么跑见 `docs/project-framework.md`）。
- `CHANGELOG.md` —— 每个版本改了什么。
