"""生成"典型的脏 TXT"样本，用于验证体检规则。

模拟真实下载场景：开头大段广告、目录块、一句话一行、广告粘在正文里、
重复水印、零宽字符、行尾空格。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"

HEADER = [
    "更多精彩小说，尽在某某小说网 www.example-novel.com",
    "手机用户请浏览 m.example-novel.com 阅读，更优质的阅读体验",
    "本书由【某某整理组】整理，版权归作者所有，请勿用作商业用途",
    "如果觉得本书不错，请收藏本站，加入书签，方便下次阅读",
]

TOC = [
    "目录",
    "第一章 起始", "第二章 迷雾", "第三章 抉择",
    "第四章 归途", "第五章 断崖", "第六章 终局",
]

WATERMARK = "本章未完，点击下一页继续阅读 >>> www.example-novel.com"

BODY = [
    "天色已经暗了下来。",
    "他站在城门口，望着远处的灯火。",
    "风从北面吹来，带着一股潮湿的寒意。",
    "这让他想起了很多年前的那个冬天。",
    "那时候他还不知道，一切都将改变。",
    "脚步声在身后响起。",
]

INLINE_AD = "这章真好看，记住本站 www.example-novel.com 手机用户请浏览 m.example-novel.com"

TITLES = ["第一章 起始", "第二章 迷雾", "第三章 抉择", "第四章 归途", "第五章 断崖", "第六章 终局"]


def build() -> str:
    lines: list[str] = []
    lines.extend(HEADER)
    lines.extend(TOC)
    for idx, title in enumerate(TITLES):
        lines.append("")
        lines.append(title)
        lines.append("")
        for j, sentence in enumerate(BODY):
            if j == 2 and idx % 2 == 0:
                lines.append(sentence + INLINE_AD)
            else:
                lines.append("\u200b" + sentence + "   ")
        lines.append(WATERMARK)
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    text = build()
    (OUT / "messy.txt").write_text(text, encoding="utf-8")
    (OUT / "messy_gbk.txt").write_bytes(text.encode("gb18030"))
    print(f"已生成 {OUT / 'messy.txt'} 与 {OUT / 'messy_gbk.txt'}")


if __name__ == "__main__":
    main()
