# 字体许可证（font licenses）

本目录收存项目随包分发/内嵌的**字体**的许可证原文。

项目根 `fonts/` 下的两个字体文件不入 git（57 MB），但**它们的许可证必须入库** ——
这是 SIL OFL 1.1 第 2 条的要求：分发字体时每一份副本都要带版权声明与本许可证。
`fonts/` 被 `.gitignore` 忽略，所以许可证放这里，构建时再复制一份进 `fonts/`。

| 字体文件 | 用在哪 | 许可证 | 本目录的文本 |
|---|---|---|---|
| `fonts/NotoSerifCJKsc-Regular.otf` | 正文（宋体） | SIL OFL 1.1 | [`OFL-NotoSerifCJK.txt`](OFL-NotoSerifCJK.txt) |
| `fonts/HanaMinB.otf` | 扩展 B 区补字（日式字形） | 双许可：SIL OFL 1.1 **或** Hanazono Font License（二选一） | [`OFL-HanaMinB.txt`](OFL-HanaMinB.txt) + [`Hanazono-Font-License.txt`](Hanazono-Font-License.txt) |

## 文本从哪来（可复核）

**OFL 1.1 正文**（两份 OFL 文件里那 4300 字符的正文段落）取自
[`notofonts/noto-cjk` 的 `Serif/LICENSE`](https://github.com/notofonts/noto-cjk/blob/main/Serif/LICENSE)
原文，逐字节未改。这也是思源宋体 / Noto Serif CJK 的官方许可文本。

**Noto Serif CJK SC 的版权头**取自字体文件自身的 name 表（nameID 0 / 7 / 14）
与 [Google Fonts 的 `ofl/notoserifsc/OFL.txt`](https://github.com/google/fonts/blob/main/ofl/notoserifsc/OFL.txt)。
字体文件内记录的版权是 `© 2017-2024 Adobe (http://www.adobe.com/).`，
Google Fonts 的分发版头写 `Copyright 2012 Google Inc. All Rights Reserved.`，两者都列上。

**花园明朝 B 的版权头**取自 Debian `fonts-hanazono` 包的
[`debian/copyright`](https://sources.debian.org/src/fonts-hanazono/20170904-3/debian/copyright/)
（上游 `http://fonts.jp/hanazono/` 与 `http://glyphwiki.org/license.html` 现在都已 404，
Debian 包是能拿到原文的权威副本）。字体文件自身 nameID 0 写的是
`2007-2018 GlyphWiki. All Rights Reserved.`，与 Debian 记录的上游版权年份
（2008-2011）不一致 —— 两处都照原样保留在文本里，不做统一。

## 一点说明

- 项目**内嵌的只是子集**（按每本书实际用到的字裁出来的），不是完整字库；
  但 OFL 管的是"Font Software 的副本"，子集仍然是副本，所以许可证照样要带。
- 项目的 `huoban/fonts.py` 只认白名单字体（文件名含 Noto / SourceHan / HanaMin），
  系统里的商业字体（SimSun、华文中宋等）只能进回退链、绝不内嵌。
- 构建时（`build/gui.spec`）`fonts/` 整目录被打进 `_internal/fonts/`，
  所以这三份文本也会跟着进 exe 的发行包。
