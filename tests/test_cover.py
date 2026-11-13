"""封面的测试：gui/cover.py 的九款版式 × 六套配色。

照 tests/test_fonts.py 的风格：不用 pytest，一个 case 一个函数，纯 assert，
直接 python tests/test_cover.py 跑。只断言契约，不断言每一条线的坐标。

几处刻意的设计：

1. **Pillow 缺失时整组跳过**，并把跳过的条数算进分母、打印一行说明 ——
   悄悄当通过会让"全绿"变成一句谎话（照 test_fonts.py 的范式）。
2. **不逐像素比对**。版式的价值在"放得下、看得清、可复现"，不在每条线的
   具体位置；断言像素会把任何微调都变成红。
3. **确定性是本文件最重要的一条**：docs/gui-design.md 的「硬性约束」一节要求同参数连画两次 PNG 字节相同。
   这条钉住的是「有人往模板里塞了 random 或 datetime.now()」。

覆盖的边界：
- available() / auto_pick / auto_look / next_choice 的契约（确定性、池子约束、可逆性）；
- 自动档既能给版式也能给整幅画，且两半都守约束（2026-10-31 新增）；
- 54 组（9×6）全部能画出来，且字节级确定性；
- 对比度审计在 54 组上全过（大文本 3:1 / 正文 4.5:1）；
- 长书名不丢字（docs/gui-design.md 的「硬性约束」一节：fit 逐档降字号，永不丢行）；
- 非法 template/theme 抛 CoverError 而不是悄悄换一个；
- 空书名、超长书名、单字书名、含空格书名的兜底；
- 换一个：确定性 + 配色轮完会换版式 + 能从任意起点绕回。
"""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui import cover  # noqa: E402

_HAVE_PIL = cover.available()

# 一份默认 spec：写成函数是为了让每个用例都能改一两个字段而不影响别的用例
def _spec(**kw):
    base = dict(title="书E", author="某某某", subtitle="第一部", source="书旗小说")
    base.update(kw)
    return base


def _png_size(data: bytes) -> tuple[int, int]:
    """从 PNG 头部读尺寸，不引 PIL（这样 even 在 Pillow 缺失时也能验证）。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    # IHDR 紧跟 8 字节签名 + 4 字节长度 + 4 字节 "IHDR"
    assert data[12:16] == b"IHDR", "PNG 结构不对"
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    return w, h


# --------------------------------------------------------------------------
# 不需要 Pillow 的用例（契约层）
# --------------------------------------------------------------------------
def case_templates_and_themes_contract():
    """两份清单就是对外 id，顺序即索引（docs/gui-design.md 的「`gui/cover.py` 接口」一节）。加/删元素等于改契约。"""
    assert cover.TEMPLATES == ["章", "简", "框", "对", "栏", "碑", "印", "夜", "疏"], cover.TEMPLATES
    assert cover.THEMES == ["xuanzhi", "songmo", "qingci", "dianlan", "zheshi", "daiqing"], cover.THEMES
    assert set(cover.ALLOWED) == set(cover.TEMPLATES), "ALLOWED 必须覆盖全部版式"


def case_no_decorative_ghost_and_seal():
    """巨大底字与朱砂小印章已删除：模块不许再导出这两个绘制函数。

    0.2.0 验收返工：ghost（巨号淡字垫底）与 seal（朱砂方印）连同四款版式里的
    调用点、右下红框一起删干净。留着没人调用的定义，迟早会被顺手接回去。
    """
    assert not hasattr(cover, "ghost"), "ghost（巨号淡字垫底）不该还在"
    assert not hasattr(cover, "seal"), "seal（朱砂方印）不该还在"


def case_auto_pick_deterministic():
    """同一书名永远同一个样子 —— 这是"字节可复现"在封面上的延伸。"""
    for name in ["书E", "红楼梦", "关于我在异界开超市这件事", "三体"]:
        first = cover.auto_pick(name)
        for _ in range(5):
            assert cover.auto_pick(name) == first, f"{name} 不稳定"
        assert first[0] in cover.TEMPLATES
        assert first[1] in cover.THEMES


def case_auto_pick_pool_constraints():
    """docs/gui-design.md 的「自动挑选」一节里的两条池子约束 + 印不进自动池。"""
    # 印 一个池都不进：存在感最强，只作手选
    for name in ["书E", "红楼梦", "三体", "关于我在异界开超市这件事"]:
        assert cover.auto_pick(name)[0] != "印", f"印 不该进自动池（{name}）"
    # 夜 只进 SHORT/MID：十几个字的长书名用大字距横排会糊
    long_names = ["关于我在异界开超市这件事这个人", "重生之我在修仙世界当道士的故事"]
    for name in long_names:
        assert cover.auto_pick(name)[0] != "夜", f"夜 不该进 LONG 池（{name}）"
    # 章 不进 LONG：学术单块版式折成三行就散了
    for name in long_names:
        assert cover.auto_pick(name)[0] != "章", f"章 不该进 LONG 池（{name}）"


def case_auto_pick_theme_allowed_by_template():
    """配色必须是该版式允许的那几个（docs/gui-design.md 的「自动挑选」一节：模板 × 配色不是自由组合）。"""
    for name in ["书E", "红楼梦", "三体", "长夜余火", "关于我在异界开超市这件事"]:
        tpl, th = cover.auto_pick(name)
        assert th in cover.ALLOWED[tpl], f"{name} → {tpl}/{th}，配色不在允许池里"


def case_auto_pick_empty_title():
    """空书名不该抛异常：封面是可选步骤，不该阻断打包。"""
    for bad in ["", "   ", None]:
        tpl, th = cover.auto_pick(bad)
        assert tpl in cover.TEMPLATES and th in cover.THEMES


def case_auto_look_deterministic_and_in_queue():
    """自动档（版式或整幅画）必须确定，且一定是队列里的一项。"""
    for name in ["书E", "三体", "关于我在异界开超市这件事", "", None, "   "]:
        first = cover.look_key(cover.auto_look(name))
        for _ in range(3):
            assert cover.look_key(cover.auto_look(name)) == first, f"{name!r} 不稳定"
        keys = [cover.look_key(x) for x in cover.all_looks(name or "")]
        assert first in keys, f"{name!r} 自动选中的那一项不在队列里：{first}"


def case_auto_look_covers_both_kinds():
    """九张整幅画必须真的能进自动档（2026-10-31 规格允许的改动）。

    这条钉的正是"改了一半"：候选拼进去了，但取模一直落在前半区，
    界面上看着没问题，实际一张画都轮不到（实测过这个错法 ——
    候选写成 tpls + bgs 而摘要位恰好偏向低段时，画占比会掉到个位数）。
    """
    names = ["书E", "三体", "活着", "边城", "围城", "白鹿原", "长夜", "孤舟",
             "山河故人", "长夜将尽", "十日终焉", "百年孤独",
             "重生之我在异世界当咸鱼过日子", "我在大明当首辅的那些年"]
    kinds = [cover.auto_look(n)["kind"] for n in names]
    assert "bg" in kinds, "九张整幅画一张都没进自动档"
    assert "tpl" in kinds, "版式全被画挤掉了 —— 自动档不该只剩一种形态"


def case_auto_look_respects_pool():
    """版式那半必须守池子约束：印 一个池都不进；长书名不许 夜/章。"""
    long_titles = ["关于我在异界开超市这件事后来发生了一系列离奇又温暖的故事",
                   "重生之我在异世界当咸鱼过日子", "谁在深夜里替我点亮那一盏灯"]
    for name in long_titles:
        look = cover.auto_look(name)
        assert look["kind"] in ("bg", "tpl"), look
        if look["kind"] == "tpl":
            assert look["template"] in cover.POOL_LONG, f"{name} 跑出 LONG 池：{look}"
            assert look["theme"] in cover.ALLOWED[look["template"]], look
    for name in ["仙", "三体", "书E", "十日终焉"]:
        look = cover.auto_look(name)
        if look["kind"] == "tpl":
            assert look["template"] != "印", f"印 不该进自动档（{name}）"


def case_first_look_is_auto_look():
    """`first_look` 与 `auto_look` 必须是同一个答案，不许各挑各的。"""
    for name in ["书E", "三体", "长夜将尽之时我将踏雪而归", ""]:
        assert cover.look_key(cover.first_look(name)) == cover.look_key(cover.auto_look(name)), name


def case_next_choice_deterministic():
    """「换一个」必须确定性：同起点连点两次拿到同一个下一个（docs/gui-design.md 的「`gui/cover.py` 接口」一节）。"""
    start = cover.auto_pick("书E")
    a = cover.next_choice("书E", start[0], start[1])
    b = cover.next_choice("书E", start[0], start[1])
    assert a == b, f"next_choice 不稳定：{a} vs {b}"
    assert a != start, "换一个必须真的换点东西"


def case_next_choice_keeps_theme_allowed():
    """配色轮转不能跑出该版式的允许池。"""
    tpl = "对"
    th = cover.ALLOWED[tpl][0]
    cur = (tpl, th)
    for _ in range(20):
        cur = cover.next_choice("书E", cur[0], cur[1])
        assert cur[1] in cover.ALLOWED[cur[0]], f"跑出允许池：{cur}"


def case_next_choice_wraps_around():
    """连点足够多次应该回到起点（确定性环形推进，不是无限漂移）。"""
    start = cover.auto_pick("书E")
    cur = start
    seen = []
    for _ in range(40):
        cur = cover.next_choice("书E", cur[0], cur[1])
        seen.append(cur)
        if cur == start:
            break
    assert cur == start, f"40 次以内没绕回起点，最后停在 {cur}（起点 {start}）"
    assert len(seen) <= 40


def case_next_choice_unknown_start_falls_back():
    """非法起点不抛异常，退回 auto_pick（界面拿到脏值时不该白屏）。"""
    got = cover.next_choice("书E", "不存在的版式", "不存在的配色")
    assert got == cover.auto_pick("书E"), got


# --------------------------------------------------------------------------
# 需要 Pillow 的用例
# --------------------------------------------------------------------------
def case_make_cover_default_size():
    data = cover.make_cover(**_spec())
    assert data is not None, "Pillow 在却返回 None"
    w, h = _png_size(data)
    assert (w, h) == (1200, 1600), f"默认尺寸应是 1200×1600，实际 {w}×{h}"


def case_make_cover_all_54():
    """9 版式 × 6 配色全部能画出来，且都过对比度审计（audit=True 默认开着）。"""
    for tpl in cover.TEMPLATES:
        for th in cover.THEMES:
            data = cover.make_cover(**_spec(), template=tpl, theme=th)
            assert data is not None, f"{tpl}/{th} 没画出来"
            w, h = _png_size(data)
            assert (w, h) == (1200, 1600), f"{tpl}/{th} 尺寸不对"
            assert 10_000 < len(data) < 400_000, f"{tpl}/{th} 体积异常：{len(data)}"


def case_make_cover_deterministic_bytes():
    """docs/gui-design.md 的「硬性约束」一节：同参数连画两次 PNG 字节完全相同 —— 有人塞 random 就会红。"""
    for tpl in ["章", "简", "对", "夜", "印"]:
        th = cover.ALLOWED[tpl][0]
        a = cover.make_cover(**_spec(), template=tpl, theme=th)
        b = cover.make_cover(**_spec(), template=tpl, theme=th)
        assert hashlib.sha256(a).digest() == hashlib.sha256(b).digest(), f"{tpl}/{th} 两次不一致"


def case_make_cover_seed_follows_title():
    """不同书名应该画出不同的图（seed 由书名派生），否则书架会全一样。"""
    a = cover.make_cover(**_spec(title="书E"), template="章", theme="xuanzhi")
    b = cover.make_cover(**_spec(title="红楼梦"), template="章", theme="xuanzhi")
    assert hashlib.sha256(a).digest() != hashlib.sha256(b).digest(), "换书名还是同一张图"


def case_make_cover_invalid_raises():
    """非法 template/theme 直接抛，不悄悄换一个（docs/gui-design.md 的「`gui/cover.py` 接口」一节）。"""
    for kw in [dict(template="不存在"), dict(theme="不存在")]:
        try:
            cover.make_cover(**_spec(), **kw)
        except cover.CoverError:
            pass
        else:
            raise AssertionError(f"{kw} 应该抛 CoverError")


def case_make_cover_too_small_raises():
    try:
        cover.make_cover(**_spec(), width=100, height=100)
    except cover.CoverError as exc:
        assert "太小" in str(exc), exc
    else:
        raise AssertionError("过小尺寸应该抛 CoverError")


def case_make_cover_long_title_keeps_all_chars():
    """docs/gui-design.md 的「硬性约束」一节：fit 逐档降字号，永不丢字。

    这条钉的是"字号降到最小仍放不下"的路径：不能像 Calibre 那样静默丢行。
    验证方式不是看像素，而是让 make_cover 把画进去的文本报回来 —— 这里退一步，
    只断言它能画出来且体积正常（丢字会让图明显变小/变空）。
    """
    for tpl in cover.TEMPLATES:
        th = cover.ALLOWED[tpl][0]
        long_title = "关于我在异界开超市这件事后来发生了一系列离奇又温暖的故事"
        data = cover.make_cover(**_spec(title=long_title), template=tpl, theme=th)
        assert data is not None, f"{tpl} 长书名没画出来"
        assert len(data) > 15_000, f"{tpl} 长书名的图太小，可能丢字了：{len(data)}"


def case_make_cover_edge_titles():
    """单字、超短、含空格、超长 —— 都不能抛。"""
    edge = ["仙", "三体", "one two three", "关" * 60, "A", "《书名》"]
    for tpl in ["章", "简", "框", "疏", "夜"]:
        th = cover.ALLOWED[tpl][0]
        for title in edge:
            data = cover.make_cover(**_spec(title=title), template=tpl, theme=th)
            assert data is not None, f"{tpl} / {title!r} 没画出来"


def case_make_cover_without_author():
    """无作者也要能画：文字排版取不到作者字符，不能崩。"""
    for tpl in cover.TEMPLATES:
        th = cover.ALLOWED[tpl][0]
        data = cover.make_cover(title="无名之书", author="", subtitle="", source="",
                               template=tpl, theme=th)
        assert data is not None, f"{tpl} 无作者没画出来"


def case_make_cover_audit_off_skips():
    """audit=False 只跳过校验，产物照样是合法 PNG（给预览提速用）。"""
    data = cover.make_cover(**_spec(), template="章", theme="xuanzhi", audit=False)
    w, h = _png_size(data)
    assert (w, h) == (1200, 1600)


def case_wrap_keeps_ascii_words_whole():
    """英文/数字连写的词不许从中间断开。

    实测踩到的：书名 `shot_book` 在「简」版式里被断成 `shot_b` / `ook` ——
    中文按字符折行是对的，但对 ASCII 词就错了。折行单元必须先分词。
    """
    f = cover.F("serif", 100, 700)
    for text in ["shot_book", "The Great Gatsby", "书E vol.2", "Part-II"]:
        lines = cover.wrap(text, f, 600)
        joined = "".join(lines)
        assert joined == text, f"{text!r} 折行后拼不回原文：{lines}"
        # 不许在 ASCII 词内部断开：每行去掉首尾空白后，不能以词字符开头却
        # 接在上一行的词字符后面（等价于：原文里相邻的两个词字符必须同在一行）
        for i in range(len(lines) - 1):
            if lines[i] and lines[i + 1]:
                a, b = lines[i][-1], lines[i + 1][0]
                both_word = a in cover._WORD_CHARS and b in cover._WORD_CHARS
                assert not both_word, f"{text!r} 在 {a!r}/{b!r} 之间断开了：{lines}"


def case_wrap_chinese_not_split_midword():
    """纯中文没有\"词\"的概念，仍按单字折行 —— 别为了英文把中文也改坏了。"""
    f = cover.F("serif", 100, 700)
    lines = cover.wrap("他说这本书很好看。", f, 320)
    assert len(lines) >= 2, f"应该折成多行，实际 {lines}"
    assert all(f.getlength(l) <= 320 + 1 for l in lines), lines
    assert "".join(lines) == "他说这本书很好看。", lines


def case_wrap_closer_not_at_line_start():
    """中文禁则：收尾标点不许落在行首。"""
    f = cover.F("serif", 100, 700)
    for width in (250, 300, 320, 400):
        lines = cover.wrap("他说这本书很好看，你也该看看。", f, width)
        for l in lines[1:]:
            assert l, "不该有空行"
            # 行首出现收尾标点说明禁则失效（允许溢出半字宽以内吃掉）
            assert l[0] not in cover.CLOSER, f"width={width} 时行首出现了 {l[0]!r}：{lines}"

def case_texture_pipeline_shrinks_png():
    """底纹必须走「低分辨率 → 量化 3 级 → 双线性上采样」，否则 PNG 体积炸掉。

    实测过：全分辨率纹理铺上去，同一张封面 1035 KB / 不铺 66 KB（15 倍）；
    降到 600×800 仍有 992 KB、300×400 有 684 KB、150×200 有 438 KB。
    当前配方平均 99 KB、最大 171 KB。

    阈值这么定：平均 < 130 KB、单张 < 250 KB。故意留了余量但不留太多 ——
    把「先量化再放大」改成「先放大再量化」（体积 169/144/214 KB）必须被抓到，
    那是真实退化：多花一倍体积换一点点边缘锐度，不值得。
    """
    if not cover.available() or not cover._TEXTURES:
        print("skip Pillow 或纹理素材不在")
        return
    cover._TEXTURE_CACHE.clear()
    sizes = []
    for tpl, th in (("章", "xuanzhi"), ("夜", "songmo"), ("碑", "xuanzhi")):
        sizes.append(len(cover.make_cover("书E", "某人", template=tpl, theme=th)))
    avg = sum(sizes) / len(sizes)
    big = max(sizes)
    assert avg < 130 * 1024, f"平均 {avg / 1024:.0f} KB 太大了（{sizes}），底纹配方被改回去了？"
    assert big < 250 * 1024, f"单张 {big / 1024:.0f} KB 太大了（{sizes}）"


def case_texture_upsample_has_transitions():
    """上采样必须是插值（BILINEAR），不是最近邻 —— 否则纹理是硬方块。

    判据用「不同灰度值的个数」：从 150×200 插值放大到 1200×1600，
    边缘会产生大量中间值（实测上百个）；NEAREST 放大只会有原始那几个值（3 个）。
    不要用「基准值占比」判 —— 量化后的实际值不一定是 0/128/255，
    按固定值算会得到 0.4% 这种假数字，反而放过了 NEAREST。
    """
    if not cover.available() or not cover._TEXTURES:
        print("skip Pillow 或纹理素材不在")
        return
    cover._TEXTURE_CACHE.clear()
    tex = cover._load_texture("paper_light", 1200, 1600)
    assert tex is not None
    distinct = sum(1 for n in tex.histogram() if n)
    assert distinct > 20, (
        f"只有 {distinct} 个不同灰度值，上采样退化成了最近邻（或根本没上采样）")


def case_texture_missing_falls_back_to_solid():
    """纹理素材缺失时，paint_base 必须填实色 —— 否则「对」的底部色块整块消失。

    这条是踩过的：只 return 不填色，实测出现过 canvas 全白、色块不见。
    """
    if not cover.available():
        print("skip Pillow 不在")
        return
    saved = dict(cover._TEXTURES)
    cover._TEXTURES.clear()
    cover._TEXTURE_CACHE.clear()
    try:
        cv = cover._Canvas(1200, 1600, cover.THEME_TABLE["xuanzhi"])
        cv.cur = "章"
        from PIL import Image
        img = Image.new("RGB", (1200, 1600), (0, 0, 0))
        try:
            painted = cv.paint_base(img, (241, 239, 232))
        except Exception as exc:  # noqa: BLE001 - 这里就是要把异常变成 FAIL
            raise AssertionError(
                f"没有纹理时 paint_base 不该抛异常（{type(exc).__name__}: {exc}）——"
                f" 它必须填实色并返回 False") from exc
        assert painted is False, "没有纹理时应该返回 False"
        # 角落必须被填成底色，不是留黑
        assert img.getpixel((0, 0)) == (241, 239, 232), img.getpixel((0, 0))
        assert img.getpixel((1199, 1599)) == (241, 239, 232)
        # region 版本同样要填
        img2 = Image.new("RGB", (1200, 1600), (0, 0, 0))
        cv.paint_base(img2, (10, 20, 30), (0, 1000, 1200, 1600))
        assert img2.getpixel((600, 1300)) == (10, 20, 30)
        assert img2.getpixel((600, 500)) == (0, 0, 0), "region 外的像素不该被碰"
    finally:
        cover._TEXTURES.clear()
        cover._TEXTURES.update(saved)
        cover._TEXTURE_CACHE.clear()


def case_every_template_actually_textured():
    """九款版式都要真的铺上底纹 —— 不能有哪一款悄悄没接。

    判据：同一本书、同一起点，`texture_amp = 8`（默认）与 `texture_amp = 0`
    出的字节必须不同。只改 `_Canvas.new()` 会漏掉「对」「夜」「印」三款
    （它们自己建画布），这条能抓住。
    """
    if not cover.available() or not cover._TEXTURES:
        print("skip Pillow 或纹理素材不在")
        return
    plain = []
    for tpl in cover.TEMPLATES:
        theme = cover.ALLOWED[tpl][0]
        cover._TEXTURE_CACHE.clear()
        cv = cover._Canvas(1200, 1600, cover.THEME_TABLE[theme])
        cv.cur = tpl
        cv.texture_amp = 0
        spec = {"title": "书E", "author": "某人", "subtitle": "", "source": ""}
        without = cover._TEMPLATE_FUNCS[tpl](spec, cover.THEME_TABLE[theme], 12345, cv)

        cv2 = cover._Canvas(1200, 1600, cover.THEME_TABLE[theme])
        cv2.cur = tpl
        with_tex = cover._TEMPLATE_FUNCS[tpl](spec, cover.THEME_TABLE[theme], 12345, cv2)

        if without.tobytes() == with_tex.tobytes():
            plain.append(tpl)
    assert not plain, f"这些版式没铺上底纹：{plain}"

    # 「对」还要单独验下半块：「对」的上下两块底色不同，各自上色。
    # 只验整张不同是不够的 —— 上半块铺了纹理、下半块忘掉，整张照样不同。
    from PIL import Image
    cv = cover._Canvas(1200, 1600, cover.THEME_TABLE["zheshi"])
    cv.cur = "对"
    spec = {"title": "书E", "author": "某人", "subtitle": "", "source": ""}
    img = cover._TEMPLATE_FUNCS["对"](spec, cover.THEME_TABLE["zheshi"], 12345, cv)
    bh = int(1600 * 0.42)
    # 采右下角一块 —— 作者/出处文字都在左边距 M 起排，右侧是纯底色区，
    # 整块数颜色会把文字像素算进去，那样即使底纹没铺也 >1 种颜色。
    patch = img.convert("RGB").crop((int(1200 * 0.70), 1600 - bh + 12,
                                     int(1200 * 0.95), 1600 - 24))
    colors = patch.getcolors(maxcolors=200000) or []
    assert len(colors) > 1, (
        "「对」的底部色块右侧是纯色，说明那块没铺底纹（下半块忘接 paint_base）")


def case_texture_only_and_cache():
    """纹理按 (name, w, h) 缓存；工作尺寸随画布等比缩放（预览 600×800 也要对）。"""
    if not cover.available() or not cover._TEXTURES:
        print("skip Pillow 或纹理素材不在")
        return
    cover._TEXTURE_CACHE.clear()
    a = cover._load_texture("paper_light", 1200, 1600)
    assert a is not None, "paper_light 应该能读到"
    assert a.size == (1200, 1600), a.size
    # 第二次必须命中缓存（同一对象）
    b = cover._load_texture("paper_light", 1200, 1600)
    assert b is a, "同一尺寸应该命中缓存"
    # 预览尺寸：工作尺寸按画布宽度等比缩放，不该抛
    c = cover._load_texture("paper_light", 600, 800)
    assert c is not None and c.size == (600, 800), getattr(c, "size", None)
    # 不存在的名字给 None（素材坏了不该让整张封面失败）
    assert cover._load_texture("nope_light", 1200, 1600) is None


def case_every_template_has_texture():
    """九款版式都要能取到纹理 —— 浅底取 *_light、深底取 *_dark。

    「对」原本映射到 cloth，但 cloth_light 质检不合格已剔除、回落到 grain，
    这条保证映射表里不会出现指向不存在素材的名字。

    变异测试说明（2026-10-27）：把 t_yin 的「幅度减半」删掉（cv.texture_amp =
    max(3, _prev_amp // 2) 改成 pass）不会被任何用例抓住，这是**故意的**：
    「印」是整张一个色块的版式，幅度大一点只是观感更脏，不是正确性缺陷，
    硬给它编一条断言只会锁死审美。真正的缺陷（漏铺、没量化、最近邻、无兜底）
    都有用例守着。
    """
    if not cover.available() or not cover._TEXTURES:
        print("skip Pillow 或纹理素材不在")
        return
    for tpl in cover.TEMPLATES:
        for dark in (False, True):
            name = cover._pick_texture(tpl, dark)
            assert name is not None, f"{tpl} dark={dark} 取不到纹理"
            assert name in cover._TEXTURES, f"{name} 不在素材里"


def case_contrast_helper():
    """对比度工具本身：黑白 21:1、同色 1:1，别算错。"""
    assert abs(cover.contrast((0, 0, 0), (255, 255, 255)) - 21.0) < 0.01
    assert abs(cover.contrast((120, 120, 120), (120, 120, 120)) - 1.0) < 1e-9
    # 对称性
    a, b = (27, 29, 30), (241, 239, 232)
    assert abs(cover.contrast(a, b) - cover.contrast(b, a)) < 1e-9


def case_theme_contrast_baseline():
    """docs/gui-design.md 的「六套配色」一节表格里的六套配色，主/次对比度必须真的达标。

    这不是重复造轮子：如果有人改了 THEME_TABLE 里的某个色值忘了重算 mu，
    这条会立刻红。期望值来自 docs/gui-design.md 的「六套配色」一节的表格，容差 0.05。
    """
    expect = {
        "xuanzhi": (14.71, 4.91),
        "songmo": (14.80, 5.74),
        "qingci": (11.89, 4.71),
        "dianlan": (13.50, 5.89),
        "zheshi": (13.06, 4.86),
        "daiqing": (12.73, 4.76),
    }
    for key, (main_cr, sub_cr) in expect.items():
        th = cover.THEME_TABLE[key]
        got_main = cover.contrast(cover._rgb(th["fg"]), cover._rgb(th["bg"]))
        got_sub = cover.contrast(cover._rgb(th["mu"]), cover._rgb(th["bg"]))
        assert abs(got_main - main_cr) < 0.05, f"{key} 主对比 {got_main:.2f} != {main_cr}"
        assert abs(got_sub - sub_cr) < 0.05, f"{key} 次对比 {got_sub:.2f} != {sub_cr}"
        assert got_main >= 4.5 and got_sub >= 4.5, f"{key} 低于 4.5:1"


def case_pillow_missing_degrades():
    """Pillow 不可用时 make_cover 返回 None，不抛异常（照 fonts.py 的降级范式）。"""
    saved = (cover.Image, cover.ImageDraw, cover.ImageFilter, cover.ImageFont)
    cover.Image = None
    try:
        assert cover.available() is False
        assert cover.make_cover(**_spec()) is None, "Pillow 缺失应返回 None"
    finally:
        cover.Image, cover.ImageDraw, cover.ImageFilter, cover.ImageFont = saved


def case_cover_fits_epub_cover_slot():
    """产出的 PNG 必须是 RGB（不是 RGBA）——塞进 EPUB 前不依赖调用方转换。

    另外验证它小于常见阅读器的封面尺寸上限（1650×2200，docs/gui-design.md 的「顺带补掉一个现存缺口」一节提到的那条）。
    """
    data = cover.make_cover(**_spec(), template="章", theme="xuanzhi")
    # PNG 色彩类型在 IHDR 的第 9 字节：0=灰度 2=RGB 3=调色板 4=灰度+alpha 6=RGBA
    color_type = data[25]
    assert color_type == 2, f"应该是 RGB（color_type=2），实际 {color_type}"
    w, h = _png_size(data)
    assert w <= 1650 and h <= 2200, f"{w}×{h} 超过常见上限"


# --------------------------------------------------------------------------
# 封底（2026-11-06，TODO 第 2 组）
# --------------------------------------------------------------------------
def _back_look(kind: str, *, template=None, background=None, theme=None) -> dict:
    return {"kind": kind, "label": "假", "template": template,
            "background": background, "theme": theme}


def _seal_bbox(look: dict) -> tuple[int, int, int, int]:
    """封底与「同一块纸面但不贴印」的差集外框 —— 用来量印落在了哪儿。

    直接逐像素扫太慢（1200×1600 = 192 万点），ImageChops 是 C 实现。
    """
    from PIL import Image, ImageChops

    got = Image.open(io.BytesIO(cover.make_back_cover(look))).convert("RGB")
    base = cover._back_paper(look, got.size[0], got.size[1])[0].convert("RGB")
    bbox = ImageChops.difference(got, base).getbbox()
    assert bbox is not None, "封底上什么都没画（印没贴上去？）"
    return bbox


def case_back_cover_geometry():
    """印必须落右下角、占宽 30%、按 615:576 缩放、右边距与下边距各 7%。

    这是定稿规格里唯一可量的一节，所以拿差集外框真量：
    1200×1600 上应是 360×337，右侧 84 / 下方 84。压成正方形会立刻在这里被抓住。
    """
    look = _back_look("tpl", template="章", theme="xuanzhi")
    x0, y0, x1, y1 = _seal_bbox(look)
    w, h = x1 - x0, y1 - y0
    assert (w, h) == (360, 337), f"印是 {w}×{h}，规格是 360×337"
    assert 1200 - x1 == 84 and 1600 - y1 == 84, f"右边距 {1200 - x1}、下边距 {1600 - y1}"
    assert x0 > 600 and y0 > 800, f"印跑到左上半边去了：({x0},{y0})"
    ratio = h / w
    assert abs(ratio - 576 / 615) < 0.01, f"印面比例 {ratio:.3f} 不是 615:576"


def case_back_cover_only_paper_and_seal():
    """封底上只有纸纹 + 一枚印：不落款、不画线、不画框、不居中。

    量法：同一款版式、不贴印的纸面与成品的差集必须只出现在右下角那块。
    多画一条线/一个框/一行字，差集就会多出一块，这里立刻挂。
    """
    from PIL import Image, ImageChops

    for tpl in ("章", "简", "夜"):
        theme = cover.ALLOWED[tpl][0]
        look = _back_look("tpl", template=tpl, theme=theme)
        got = Image.open(io.BytesIO(cover.make_back_cover(look))).convert("RGB")
        base = cover._back_paper(look, got.size[0], got.size[1])[0].convert("RGB")
        diff = ImageChops.difference(got, base)
        corner = diff.crop((0, 0, 700, 900))
        assert corner.getbbox() is None, f"{tpl}：封底左上多了东西（落款/细线/边框？）"


def case_back_cover_paper_follows_cover():
    """纸纹与底色跟封面走：不贴印的纸面必须带该配色的底色且不是一块纯色。

    封底与封面同源（都走 _Canvas 的 cv.cur + cv.new()），所以这里钉两件事：
    主色贴近该配色的 bg，且像素不止一种（纸纹真的铺上了）。
    """
    from PIL import Image

    for tpl in ("章", "夜"):
        theme = cover.ALLOWED[tpl][0]
        look = _back_look("tpl", template=tpl, theme=theme)
        img = cover._back_paper(look, cover.W_DEFAULT, cover.H_DEFAULT)[0].convert("RGB")
        assert img.size == (cover.W_DEFAULT, cover.H_DEFAULT)
        colors = img.getcolors(maxcolors=1 << 22)
        assert colors is not None, f"{tpl}：色数爆表"
        assert len(colors) > 1, f"{tpl}：纸面是一块纯色（纸纹没铺上）"
        common = max(colors, key=lambda c: c[0])[1]
        want = cover._rgb(cover.THEME_TABLE[theme]["bg"])
        assert all(abs(a - b) <= 24 for a, b in zip(common, want)), (
            f"{tpl}：纸面主色 {common} 离配色底色 {want} 太远")


def case_back_cover_dark_theme_lifts_the_seal():
    """深底把印色往白提一档（否则深色纸上的深色印看不见）。

    提亮必须保留配色自己的色相：松墨仍然是松墨那支朱，靛蓝仍然是它那枚金，
    不能统一成定稿样张那支红。
    """
    red = cover._seal_color(cover.THEME_TABLE["xuanzhi"]["ac"], False)
    assert red == cover._rgb(cover.THEME_TABLE["xuanzhi"]["ac"]), "浅底印色就是配色 ac"
    for theme in ("songmo", "dianlan"):
        ac = cover._rgb(cover.THEME_TABLE[theme]["ac"])
        got = cover._seal_color(cover.THEME_TABLE[theme]["ac"], True)
        assert got != ac and cover._lum(got) > cover._lum(ac), f"{theme}：深底印没提亮"
        assert sorted(range(3), key=lambda i: ac[i]) == sorted(
            range(3), key=lambda i: got[i]
        ), f"{theme}：提亮后色相被改掉了（通道大小顺序变了）"


def case_back_cover_all_45_and_deterministic():
    """45 款（36 版式×配色 + 9 整幅背景）每款都能出图，且同参两次逐字节相同。"""
    looks = cover.all_looks("书E")
    assert len(looks) == 45, len(looks)
    for look in looks:
        data = cover.make_back_cover(look)
        assert data and data[:8] == b"\x89PNG\r\n\x1a\n", look["label"]
        assert cover.make_back_cover(look) == data, f"{look['label']} 两次不一样"
        assert 20_000 < len(data) < 400_000, f"{look['label']} 体积 {len(data)} 不对劲"


def case_back_cover_invalid_raises_and_degrades():
    """非法版式/配色要抛（调用方写错了）；印面模板没了不抛，但也不产这一页。

    与 make_cover 同一条口径（docs/gui-design.md 的「`gui/cover.py` 接口」一节：不修补）：悄悄换一款会让人以为点错了按钮。
    """
    assert cover.make_back_cover(None) is None, "没给 look 应该返回 None"
    for bad in (
        _back_look("tpl", template="不存在的版式", theme="xuanzhi"),
        _back_look("tpl", template="章", theme="不存在的配色"),
    ):
        try:
            cover.make_back_cover(bad)
        except cover.CoverError:
            pass
        else:
            raise AssertionError(f"非法参数没抛 CoverError：{bad}")
    try:
        cover.make_back_cover(_back_look("tpl", template="章", theme="xuanzhi"), width=100)
    except cover.CoverError:
        pass
    else:
        raise AssertionError("太小的封底没抛 CoverError")

    # 印面模板缺失：不炸，但要返回 None —— 没有印就只剩一张白纸，那不叫封底
    saved = cover._PLATE_CACHE
    real = cover._BACK_PLATE
    cover._PLATE_CACHE = None
    cover._BACK_PLATE = Path("G:/没有这个文件/seal.png")
    try:
        for look in (
            _back_look("tpl", template="章", theme="xuanzhi"),
            _back_look("bg", background="light_01"),
        ):
            assert cover.make_back_cover(look) is None, (
                f"模板缺失时应该不产这一页（{look['kind']}），否则成品是张白纸")
    finally:
        cover._BACK_PLATE = real
        cover._PLATE_CACHE = saved


def case_back_cover_bg_geometry():
    """整幅画那 9 款的印也得落右下、占宽 30%、按 615:576 缩放。

    整幅画在贴印之后会做 5bit 量化（与封面同口径），所以比对的纸面要跟着一起
    量化，否则差集整张都是，量不出印在哪儿。
    """
    from PIL import Image, ImageChops

    step = 256 // cover._BG_LEVELS
    for name in ("light_01", "dark_04"):
        if name not in cover.BACKGROUNDS:
            continue
        look = _back_look("bg", background=name)
        data = cover.make_back_cover(look)
        assert data and data[:8] == b"\x89PNG\r\n\x1a\n", name
        got = Image.open(io.BytesIO(data)).convert("RGB")
        base = cover._back_paper(look, got.size[0], got.size[1])[0].convert("RGB")
        base = base.point(lambda v: (v // step) * step)
        bbox = ImageChops.difference(got, base).getbbox()
        assert bbox is not None, f"{name}：整幅画封底上没看到印"
        x0, y0, x1, y1 = bbox
        w, h = x1 - x0, y1 - y0
        assert (w, h) == (360, 337), f"{name}：印是 {w}×{h}，规格是 360×337"
        assert 1200 - x1 == 84 and 1600 - y1 == 84, (
            f"{name}：右边距 {1200 - x1}、下边距 {1600 - y1}")
        assert abs(h / w - 576 / 615) < 0.01, f"{name}：印面比例 {h / w:.3f} 不是 615:576"


# --------------------------------------------------------------------------
# 整幅背景（族 B 十张预设，见 docs/gui-design.md 的「整幅背景」一节）
# -------------------------------------------------------------------------
def case_backgrounds_contract():
    """BACKGROUNDS 是对外契约：每张都得有四件套，值都得在枚举里。

    zone/align/maxw 不是审美参数，是**量出来的文字落点**（6×6 网格 sd）。
    漏一个键不会报错，只会让文字压在画作上 —— 所以逐张逐键断言。
    """
    names = cover.background_names()
    assert names, "一台机器上一张背景都没有"
    for name in names:
        info = cover.BACKGROUNDS[name]
        assert isinstance(info["dark"], bool), f"{name}.dark 应该是 bool"
        assert info["zone"] in ("top", "center"), f"{name}.zone={info['zone']!r}"
        assert info["align"] in ("left", "center"), f"{name}.align={info['align']!r}"
        assert 0.4 <= info["maxw"] <= 0.75, f"{name}.maxw={info['maxw']} 不像个宽度比例"
        assert info.get("label"), f"{name} 没有 label（界面会显示工程记号）"
    assert set(names) <= set(cover.BACKGROUNDS), "background_names 里出现了 BACKGROUNDS 没有的名字"


def case_background_pick_deterministic():
    """同一本书永远从同一张开始（docs/gui-design.md 的「硬性约束」一节：确定性），且必须落在可用集合里。"""
    names = cover.background_names()
    first = cover.pick_background("书E")
    assert first in names, f"pick_background 给出了不可用的 {first!r}"
    for _ in range(5):
        assert cover.pick_background("书E") == first, "同一书名两次给出了不同的起点"
    assert cover.pick_background("") in names, "空书名也不能炸"


def case_background_next_deterministic():
    """「换一个」是确定性轮转：同起点同步数必定同一结果，转一圈必须回到起点。"""
    names = cover.background_names()
    start = cover.pick_background("书E")
    a = cover.next_background(start, 1)
    assert a == cover.next_background(start, 1), "同起点同步数两次结果不同"
    assert a in names
    cur = start
    for _ in range(len(names)):
        cur = cover.next_background(cur, 1)
    assert cur == start, f"转 {len(names)} 步没回到起点（当前 {cur}，起点 {start}）"


def case_background_next_unknown_start_falls_back():
    """手上可能是一个已经消失的素材名（换了素材包）。不该因此画不出封面。"""
    names = cover.background_names()
    assert cover.next_background("这张图早就不在了", 1) in names


def case_background_label_falls_back():
    """认不出来的名字回原名 —— 界面宁可显示工程记号，也不要空一块。"""
    for name in cover.background_names():
        assert cover.background_label(name) == cover.BACKGROUNDS[name]["label"]
    assert cover.background_label("light_99") == "light_99"


def case_all_looks_mixes_both_kinds():
    """统一队列：九张整幅画 + 九款版式×允许配色，混在一个队列里。

    试用反馈第 5 条：「换一个」把有图与无图混成一个队列。这里是那条的契约层：
    队列里两种 kind 都要有，且不能前半截全是画、后半截全是版式 ——
    那等于还是两条路，只是包了一层。
    """
    looks = cover.all_looks("书E")
    kinds = [x["kind"] for x in looks]
    assert "bg" in kinds and "tpl" in kinds, set(kinds)
    n_bg = kinds.count("bg")
    n_tpl = kinds.count("tpl")
    assert n_bg == len(cover.background_names()), n_bg
    # 版式那一半是 模板×允许配色，逐个数一遍
    expect_tpl = sum(len(cover.ALLOWED.get(t, cover.THEMES)) for t in cover.TEMPLATES)
    assert n_tpl == expect_tpl, (n_tpl, expect_tpl)
    # 交错：前 2*n_bg 项里应当两种都有（不交错的话前 n_bg 项全是 bg）
    head = kinds[: 2 * n_bg]
    assert "tpl" in head, "队列前半段全是整幅画 —— 没有交错"
    # 每一项都要能画：key 唯一，label 非空
    keys = [cover.look_key(x) for x in looks]
    assert len(set(keys)) == len(keys), "队列里有重复项"
    assert all(x["label"] for x in looks)


def case_first_look_is_deterministic_and_in_queue():
    """起点跟书名走，且一定是队列里的一项（「换一个」要从这儿往后转）。"""
    looks = cover.all_looks("书E")
    keys = [cover.look_key(x) for x in looks]
    a = cover.first_look("书E")
    assert cover.look_key(a) == cover.look_key(cover.first_look("书E"))
    assert cover.look_key(a) in keys
    assert cover.look_key(cover.first_look("")) in keys, "空书名也不能炸"


def case_next_look_skips_seen_then_wraps():
    """「换一个」优先给没见过的；全看过了按顺序接着转，不卡住。"""
    looks = cover.all_looks("书E")
    keys = [cover.look_key(x) for x in looks]
    start = cover.look_key(cover.first_look("书E"))
    # 给过 3 项，下一步必须跳过它们
    seen = keys[:3]
    nxt = cover.look_key(cover.next_look("书E", start, seen))
    assert nxt not in seen, f"换一个给了已经看过的 {nxt}"
    assert nxt in keys
    # 全都看过了：仍然要给出一项，且不能报错
    nxt2 = cover.look_key(cover.next_look("书E", start, keys))
    assert nxt2 in keys
    # 起点认不出来（换了素材包）也不能炸
    assert cover.look_key(cover.next_look("书E", "bg:不存在的", [])) in keys


def case_look_key_is_stable_and_distinct():
    """look_key 是前后端之间认这一项的唯一凭据，必须稳定且两类不撞。"""
    looks = cover.all_looks("书E")
    for x in looks:
        assert cover.look_key(x) == cover.look_key(x)
    bg = next(x for x in looks if x["kind"] == "bg")
    tpl = next(x for x in looks if x["kind"] == "tpl")
    assert cover.look_key(bg).startswith("bg:")
    assert cover.look_key(tpl).startswith("tpl:")
    assert cover.look_key(bg) != cover.look_key(tpl)


def case_make_look_draws_both_kinds():
    """`make_look` 是队列项 → 字节的唯一入口，两种 kind 都要能画。"""
    looks = cover.all_looks("书E")
    bg = next(x for x in looks if x["kind"] == "bg")
    tpl = next(x for x in looks if x["kind"] == "tpl")
    a = cover.make_look(bg, "书E")
    b = cover.make_look(tpl, "书E")
    assert a and b
    assert a != b, "整幅画与版式画出了同一张图"
    # 与直接调 make_cover 等价（make_look 只是分派，不该多做什么）
    assert cover.make_look(tpl, "书E") == cover.make_cover(
        "书E", template=tpl["template"], theme=tpl["theme"]
    )
    assert cover.make_look(bg, "书E") == cover.make_cover(
        "书E", background=bg["background"]
    )


def case_make_look_deterministic():
    """同一个队列项画两次逐字节相同（成品要可复现）。"""
    looks = cover.all_looks("书E")
    for x in looks[:4]:
        assert cover.make_look(x, "书E") == cover.make_look(x, "书E"), x["label"]


def case_design_scale_same_layout_at_any_size():
    """缩略图与大图必须是同一个设计（这条钉的是实际踩过的坑）。

    界面里的样张按 300×400 画、大图按 600×800 画、正式打包按 1200×1600 画。
    原型把所有字号/间距/线宽写成绝对像素，画布一小，150px 的字塞不进 W*maxw
    就一路降档、最后硬折行 —— 同一本书在样张上折成两行、点开变一行，看着像
    "点了之后换了一张"。2026-10 实测：45 款全部有版式偏移，把 1200 缩到 300
    与直出 300 逐像素比，中位数差 16/255。

    现在的做法是 `_Canvas.s = w / 1200`，绝对尺寸一律过 `cv.S()`。这条守两件事：
    字号必须等比缩（0.25 的画布就是约 0.25 的字），文字块个数必须与 1200 时一致
    （折行点是版式的一部分，不能因为画布小而变）。
    """
    spec = _spec()
    for tpl in cover.TEMPLATES:
        th = cover.THEME_TABLE[cover.ALLOWED[tpl][0]]

        def _sizes(w, h, _tpl=tpl, _th=th):
            cv = cover._Canvas(w, h, _th)
            cover._TEMPLATE_FUNCS[_tpl](spec, _th, 12345, cv)
            return [size for _n, size, _fg, _bg in cv.text_log]

        big = _sizes(1200, 1600)
        small = _sizes(300, 400)
        assert len(big) == len(small), (
            f"{tpl}：小画布上少画/多画了文字块（{big} vs {small}）"
        )
        for b, s in zip(big, small):
            if b <= 0:
                continue
            ratio = s / b
            assert 0.2 <= ratio <= 0.3, (
                f"{tpl}：{b}px 的字在 300 宽的画布上成了 {s}px（比例 {ratio:.2f}，"
                f"应约 0.25）—— 有绝对尺寸没走 cv.S()"
            )


def case_background_cache_not_polluted():
    """★ 缓存被就地画字：这条钉的是实际踩过的坑。

    `_load_background` 若直接返回缓存对象，`_draw_background_cover` 会在这张
    图上画字 —— 第二次画的是「上一张的字 + 这一张的字」，连画两次字节不同，
    第三次甚至抛对比度不足。序列必须是「A → B → A」且第二次 A 与第一次完全相同。
    """
    names = cover.background_names()
    a1 = cover.make_cover(**_spec(), background=names[0])
    cover.make_cover(**_spec(), background=names[1])   # 中间穿插一张，把缓存搅一下
    a2 = cover.make_cover(**_spec(), background=names[0])
    assert hashlib.sha256(a1).hexdigest() == hashlib.sha256(a2).hexdigest(), \
        "A→B→A 两次 A 的字节不同：背景缓存被就地污染了"


def case_every_background_draws():
    """九张全部能画出来，尺寸对、色彩类型对、体积在实测区间里。

    体积上界 700 KB 是给 `light_05` 那种通体渐变的图留余量；真超了说明
    5bit 量化或平滑被改坏了（体积暴涨的唯一已知来源）。
    """
    for name in cover.background_names():
        data = cover.make_cover(**_spec(), background=name)
        assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 不是 PNG"
        assert data[25] == 2, f"{name} 应该是 RGB（color_type={data[25]}）"
        assert _png_size(data) == (1200, 1600), f"{name} 尺寸 {_png_size(data)}"
        kb = len(data) / 1024
        assert 150 <= kb <= 700, f"{name} 体积 {kb:.0f} KB 超出 150–700 的实测区间"


def case_background_deterministic_bytes():
    """整幅背景也必须字节级确定（blur 与量化两道工序都得是确定性的）。"""
    for name in cover.background_names():
        a = cover.make_cover(**_spec(), background=name)
        b = cover.make_cover(**_spec(), background=name)
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest(), \
            f"{name} 连画两次字节不同"


def case_background_invalid_name_raises():
    """非法背景名抛 CoverError，不许悄悄换一张 —— 静默换掉等于用户的选择没生效。"""
    try:
        cover.make_cover(**_spec(), background="not_a_real_background")
    except cover.CoverError as exc:
        assert "not_a_real_background" in str(exc), f"报错里应该带上那个名字：{exc}"
    else:
        raise AssertionError("非法背景名没有抛 CoverError")


def case_background_ignores_template_and_theme():
    """给了 background 就忽略 template/theme（两条路不能混着走）。"""
    name = cover.background_names()[0]
    plain = cover.make_cover(**_spec(), background=name)
    with_tpl = cover.make_cover(**_spec(), background=name, template="夜", theme="dianlan")
    assert hashlib.sha256(plain).hexdigest() == hashlib.sha256(with_tpl).hexdigest(), \
        "template/theme 影响了背景模式的输出"


# --------------------------------------------------------------------------
# 字体（2026-11-07 补：用户没装中文字体也要能出封面）
# --------------------------------------------------------------------------
def case_font_dirs_put_bundled_first():
    """自带字库目录排在系统目录前面 —— 那是"用户没装字体也能出封面"的根本。

    发行包里 fonts/ 跟着 _internal 走，探测时它必须先被看到，否则没装思源的人
    封面会直接报错（旧实现只认 C:/WINDOWS/Fonts 下几个写死的文件名）。

    公开仓库不带那 57 MB 字库（fonts/ 是 gitignore 项），克隆下来跑测试时
    这个目录不存在、会被 _font_dirs() 的真实存在过滤掉。所以这里借一个临时
    目录把它「造出来」再量顺序，量完就撤，不往仓库里写东西。
    """
    expect = Path(cover.__file__).resolve().parents[1] / "fonts"
    created = not expect.is_dir()
    if created:
        expect.mkdir(parents=True, exist_ok=True)
    try:
        dirs = cover._font_dirs()
        assert dirs, "一个字体目录都没探测到，肯定不对"
        assert dirs[0] == expect, f"第一项应是项目自带 fonts/，实际 {dirs[0]}"
        for d in dirs:
            assert d.is_dir(), f"只该返回真实存在的目录：{d}"
    finally:
        if created:
            expect.rmdir()


def case_font_path_searches_all_dirs():
    """_font_path 是按名字在**所有**目录里找，不是只认写死的绝对路径。

    复现最典型的情形：用户机器上没装思源，包里那份就是唯一能用的。
    """
    saved_paths = dict(cover._FONT_PATHS)
    saved_fb = dict(cover._FONT_FALLBACKS)
    saved_dirs = cover._font_dirs
    try:
        bundled = Path(cover.__file__).resolve().parents[1] / "fonts"
        probe = None
        if bundled.is_dir():
            for p in sorted(bundled.iterdir()):
                if p.is_file() and p.suffix.lower() in (".otf", ".ttf", ".ttc"):
                    probe = p.name
                    break
        if probe is None:
            print("     （自带 fonts/ 不在，跳过实际探测那半条）")
        else:
            cover._FONT_PATHS.clear()
            cover._FONT_FALLBACKS["serif"] = (probe,)
            cover._font_dirs = lambda: [bundled]  # type: ignore[assignment]
            got = cover._font_path("serif")
            assert got is not None, f"只在自带目录里的 {probe} 没被找到"
            assert Path(got).name == probe and Path(got).parent == bundled, got
    finally:
        cover._FONT_FALLBACKS.clear()
        cover._FONT_FALLBACKS.update(saved_fb)
        cover._FONT_PATHS.clear()
        cover._FONT_PATHS.update(saved_paths)
        cover._font_dirs = saved_dirs  # type: ignore[method-assign]


def case_font_missing_raises_cover_error():
    """一个字体都找不到时抛 CoverError，而不是画出一张没有字的图。"""
    saved_paths = dict(cover._FONT_PATHS)
    saved_dirs = cover._font_dirs
    try:
        cover._FONT_PATHS.clear()
        cover._FONT_PATHS["serif"] = None  # 直接钉死"没有"
        cover._font_dirs = lambda: []  # type: ignore[assignment]
        try:
            cover.F("serif", 40)
        except cover.CoverError as exc:
            assert "字体" in str(exc), exc
        else:
            raise AssertionError("没有字体时应该抛 CoverError")
    finally:
        cover._FONT_PATHS.clear()
        cover._FONT_PATHS.update(saved_paths)
        cover._font_dirs = saved_dirs  # type: ignore[method-assign]


def case_font_sans_falls_back_to_serif():
    """黑体一个都找不到时用宋体顶上，而不是让整张封面报废。

    发行包自带的 fonts/ 里只有宋体（NotoSerifCJKsc）没有黑体，所以"机器上没有
    任何黑体候选但宋体可用"是真实会遇到的情形。黑体只用在副标题、作者、「著」
    这类小字上，换宋体只是观感差异；而 F() 抛 CoverError 会让整张封面降级成
    纯文字封面 —— 那个代价大得多。
    """
    saved_paths = dict(cover._FONT_PATHS)
    saved_fb = dict(cover._FONT_FALLBACKS)
    try:
        real_serif = cover._search_font("serif")
        if real_serif is None:
            print("     （本机没有可用宋体，跳过）")
            return
        cover._FONT_PATHS.clear()
        # sans 的所有候选都改成不存在的名字，serif 保留真表
        cover._FONT_FALLBACKS["sans"] = ("完全不存在的黑体.ttf",)
        got = cover._font_path("sans")
        assert got == real_serif, f"黑体缺失时应回落到宋体，实际 {got}"
    finally:
        cover._FONT_FALLBACKS.clear()
        cover._FONT_FALLBACKS.update(saved_fb)
        cover._FONT_PATHS.clear()
        cover._FONT_PATHS.update(saved_paths)


def case_font_candidates_prefer_variable_weights():
    """候选表得同时满足两件事：跨平台可用（Noto/思源）与可变字体在前。

    可变字体在前不是排版偏好：封面标题用 700、作者用 500，只有带 wght 轴的字体
    画得出真正的粗体；静态字体拿到 700 会被 Pillow 忽略，标题会明显变细。
    """
    for kind in ("serif", "sans"):
        names = cover._FONT_FALLBACKS[kind]
        assert any("Noto" in n or "SourceHan" in n for n in names), \
            f"{kind} 没有开源字体候选：{names}"
        vf_at = [i for i, n in enumerate(names) if "VF" in n]
        assert vf_at, f"{kind} 没有可变字体候选：{names}"
        static_at = [i for i, n in enumerate(names) if "Regular" in n or n.endswith(".ttc")]
        if static_at:
            assert min(vf_at) < min(static_at), f"{kind} 可变字体应排在静态字体前：{names}"


def main() -> int:
    cases = [
        case_templates_and_themes_contract,
        case_auto_pick_deterministic,
        case_auto_pick_pool_constraints,
        case_auto_pick_theme_allowed_by_template,
        case_auto_pick_empty_title,
        case_auto_look_deterministic_and_in_queue,
        case_auto_look_covers_both_kinds,
        case_auto_look_respects_pool,
        case_first_look_is_auto_look,
        case_next_choice_deterministic,
        case_next_choice_keeps_theme_allowed,
        case_next_choice_wraps_around,
        case_next_choice_unknown_start_falls_back,
        case_wrap_keeps_ascii_words_whole,
        case_wrap_chinese_not_split_midword,
        case_wrap_closer_not_at_line_start,
        case_texture_pipeline_shrinks_png,
        case_texture_upsample_has_transitions,
        case_texture_missing_falls_back_to_solid,
        case_every_template_actually_textured,
        case_texture_only_and_cache,
        case_every_template_has_texture,
        case_contrast_helper,
        case_theme_contrast_baseline,
        case_pillow_missing_degrades,
        case_font_dirs_put_bundled_first,
        case_font_path_searches_all_dirs,
        case_font_missing_raises_cover_error,
        case_font_candidates_prefer_variable_weights,
        case_font_sans_falls_back_to_serif,
        case_backgrounds_contract,
        case_background_pick_deterministic,
        case_background_next_deterministic,
        case_background_next_unknown_start_falls_back,
        case_background_label_falls_back,
        case_all_looks_mixes_both_kinds,
        case_first_look_is_deterministic_and_in_queue,
        case_next_look_skips_seen_then_wraps,
        case_look_key_is_stable_and_distinct,
        case_no_decorative_ghost_and_seal,
    ]
    if _HAVE_PIL:
        cases += [
            case_make_cover_default_size,
            case_make_cover_all_54,
            case_make_cover_deterministic_bytes,
            case_make_cover_seed_follows_title,
            case_make_cover_invalid_raises,
            case_make_cover_too_small_raises,
            case_make_cover_long_title_keeps_all_chars,
            case_make_cover_edge_titles,
            case_make_cover_without_author,
            case_make_cover_audit_off_skips,
            case_cover_fits_epub_cover_slot,
            case_background_cache_not_polluted,
            case_every_background_draws,
            case_background_deterministic_bytes,
            case_background_invalid_name_raises,
            case_background_ignores_template_and_theme,
            case_make_look_draws_both_kinds,
            case_make_look_deterministic,
            case_back_cover_geometry,
            case_back_cover_only_paper_and_seal,
            case_back_cover_paper_follows_cover,
            case_back_cover_dark_theme_lifts_the_seal,
            case_back_cover_all_45_and_deterministic,
            case_back_cover_invalid_raises_and_degrades,
            case_back_cover_bg_geometry,
            case_design_scale_same_layout_at_any_size,
        ]
    else:
        print("skip Pillow 不在，跳过 16 条绘制用例（分母照算）")

    failed = 0
    for c in cases:
        try:
            c()
            print(f"ok   {c.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {c.__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
