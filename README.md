# 活板（huoban）

优雅的一键式精排电子书工具。

把脏乱的中文 TXT 小说排成能读的 EPUB：广告行、重复水印、乱掉的编码、断成一句一行的段落，甚至整本糊成一行的文本——先按规则拣干净，再把每一处改动摆给你看，**删了哪一行、合并了哪一段、为什么这么判**，都能逐条复核，勾一下就是「这行别删」。

即将正式支持 AZW3 输入/输出。

## 从源码运行

### 桌面版

**目前只有 Windows 版。** 需要 Python 3 和 WebView2 运行时——Windows 11 自带，Windows 10 若从没装过，得先装一次，否则窗口起不来。

    pip install pywebview Pillow
    python -m gui.app

### 命令行

只用标准库，克隆下来就能跑。Windows 上实测过；macOS 与 Linux 没跑过，代码里也没有平台专有的部分。

    git clone https://github.com/ChineseRyan/huoban.git
    cd huoban
    python -m huoban diagnose samples/messy_gbk.txt   # 只读体检：这本书什么状况
    python -m huoban clean    书.txt -o out            # 章节树 JSON + 干净 TXT + 清洗报告
    python -m huoban epub     书.txt                   # 直接打包成 EPUB
    python -m huoban reflow   成品.epub                # 现成 EPUB 拆回章节树 + 图片

四个子命令：`diagnose` 体检 / `clean` 清洗 / `epub` 打包 / `reflow` 导入，每个都能加 `--help` 看完整参数。

产物默认都落在输入文件旁边：`clean` 与 `epub` 落到 `<书名>_clean/`，里面有 `book.json`（章节树）、`book.txt`（干净正文）、`clean-report.md`（清洗报告）、`book.epub`；加 `--azw3` 再多一个同名 `.azw3`。`reflow` 落到 `<书名>_reflow/`，里面是 `book.json` 与 `images/`——拆回来的是章节与图，字符级格式（字体、颜色、行内样式）会丢。

### 可选依赖

- `fontTools` 把中文字体子集化内嵌进 EPUB，不装就只写系统字体回退链（包小得多，但正文走阅读器默认宋体）。
- Calibre 的 `ebook-convert`，用于导出 AZW3。
- `Pillow`，给桌面版画封面用——命令行完全不需要它，桌面版缺了则画不出封面。

## 应用界面

![首屏：把书拖到这里](docs/images/ui-drop.png)

![复核页：左边分类导航，中间原文与删除后并排](docs/images/ui-review.png)

## 设计原则

- **完全本地。** 不联网、不上传：清洗、打包、画封面全在本机做完，书稿不出这台机器。
- **输入只读。** 只读打开：不写、不改名、不移动；输出一律另建目录，原文件一个字节都不动。
- **规则透明化。** 每一处删改都摆得出来——删了哪一行、合并了哪一段、为什么这么判；规则判不准的留给你复核，不让模型猜着改。

## 给想改代码的人

- 架构、入口、运行方式、测试怎么跑、环境：`docs/project-framework.md`。文档地图：`docs/README.md`。
- 项目契约（代码约定 / 测试约定 / 文档约定）：`AGENTS.md`。版本记录：`CHANGELOG.md`。界面设计说明：`docs/gui-design.md`（部分章节以代码为准）。
- 测试逐个脚本直跑，看末行 `N/M passed`：

      python -B tests/test_clean.py
      python -B tools/smoke_test.py

  别用 `pytest`：用例是 `main()` 里按序调的 `case_*`，它一个都收集不到。

## 许可证

- 项目本体：[MIT](LICENSE)。
- `fonts/` 下两份中文字库——思源宋体按 [OFL 1.1](docs/licenses/OFL-NotoSerifCJK.txt)，花园明朝 B 按 [OFL 1.1](docs/licenses/OFL-HanaMinB.txt) 与 [Hanazono Font License](docs/licenses/Hanazono-Font-License.txt) 双许可——仓库不提供，发行包中包含，许可证原文一并打在 `_internal/fonts/` 下。它们**不是** MIT。

## 致谢

- [LINUX DO](https://linux.do)
