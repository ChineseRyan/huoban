# 设计资产

本文件回答三个问题：**有哪些资产、哪段程序在用它、新资产放哪并怎么登记。**
起因：重设计「关于」页时，肥鱼找不到应用图标母版，自己另画了一枚。根因是资产没有清单、母版不在仓库里。

## 一、资产放在哪

| 位置 | 是什么 | 进不进仓库 |
|---|---|---|
| `gui/assets/` | **成品资产**，程序运行时只从这里读 | 进（随包发布） |
| `designs/` | 设计稿与候选稿（HTML 原型），备查，不参与构建 | 进 |
| `docs/images/` | README 与文档里的展示图 | 进 |
| `design/` | 封面 / 动效的候选稿现场；`03-终稿` 只由开发者挪 | 不进（`.gitignore`） |
| `_shot/` | 截图与原型中间产物；只放行 `build.js` | 不进（`.gitignore`） |
| `promo/` | 宣传片现场 | 不进（`.gitignore`） |
| `fonts/` | 思源宋体 + 花园明朝，57 MB | 不进（`.gitignore`，见 `docs/notes/fonts-57mb-not-in-git.md`） |

## 二、成品清单（`gui/assets/`）

### 图标

| 文件 | 用在哪 |
|---|---|
| `huoban.ico` | 窗口图标（`gui/app.py`、`gui/launcher.py` 里的 `_resource("assets/huoban.ico")`）、exe 图标 |
| `icon_light_1024.png` / `icon_dark_1024.png` | 浅底 / 深底两套 1024 方图 |
| `icon_tile_1024.png` | tile 方案 |
| `icon_mask_264.png` | **母版**：264×264 的印面蒙版，生成上面四件用 |

**母版 → 生成物**：`tools/make_icon.py` 读母版，出 `huoban.ico`（8 档尺寸）+ 上面三张 1024 PNG。
注意：脚本现在读的是 `_shot/about_mock/mask_264.png`（该目录被 `.gitignore` 忽略）。
仓库里那份同源副本是 `gui/assets/icon_mask_264.png`，两者逐字节相同 —— **母版丢了就用它**，别重画。

### 纸质底纹（`gui/assets/textures/`）

9 张，命名 `<族>_<light|dark>.png`，族为 `paper` / `grain` / `ink` / `metal` / `cloth`（合计约 19 KB）。
`gui/cover.py` 用 `_TEXTURE_DIR` 读目录，按 `_TEXTURE_MAP` 把九款版式指到纹理族，浅底取 `*_light`、深底取 `*_dark`；
某张缺席按 `_TEXTURE_FALLBACK` 顺序回落（grain → paper → ink → metal）。

| 版式 | 纹理族 |
|---|---|
| 章 / 框 / 疏 | paper（宣纸） |
| 简 / 对 | grain（麻纸） |
| 栏 | metal |
| 碑 / 夜 | ink（墨洇） |
| 印 | cloth（布） |

质检不合格、**故意不入库**的两张：`cloth_light`（浅底文字带对比度不足）、`light_03`（景物压上方 60%，与大字留白冲突）。

### 整幅背景（`gui/assets/backgrounds/`）

9 张整幅画，`gui/cover.py` 的 `_BACKGROUNDS` / `_BACKGROUND_DIR` 用。名字是工程记号，界面显示 `label`：

| 文件 | 界面名 | 深浅 | 文字落点 |
|---|---|---|---|
| `light_01` | 远山 | 浅 | 上方·左 |
| `light_02` | 海平线 | 浅 | 上方·中 |
| `light_04` | 梅枝 | 浅 | 上方·左 |
| `light_05` | 云雾 | 浅 | 中央·中 |
| `dark_01` | 山影 | 深 | 上方·中 |
| `dark_02` | 水波 | 深 | 上方·左 |
| `dark_03` | 松枝 | 深 | 上方·左（限宽 55%） |
| `dark_04` | 夜梅 | 深 | 上方·中 |
| `dark_05` | 暗云 | 深 | 中央·中 |

`light_03` 同纹理那边的理由，未入库。

### 印面

| 文件 | 是什么 |
|---|---|
| `seal_plate_615x576.png` | **封底**右下角那枚印的印面，`gui/cover.py` 的 `_BACK_PLATE` 读它 |
| `about_seal_light.png` / `about_seal_dark.png` | **关于页**印面（浅 / 深） |
| `about_seal_mask.png` | 关于页印面蒙版；与 `gui/ui.html` 里内嵌的 base64 蒙版逐字节相同 |

关于页那三张目前**没有被代码引用**（`gui/ui.html` 用的是内嵌 base64，尺寸 264×264，底色吃 `var(--seal)`：
浅 `#A83A2E`、深 `#E07A6C`），作为素材与依据留存。

**两枚「印」不是同一份**：图标母版 `_shot/about_mock/mask_264.png`（= `gui/assets/icon_mask_264.png`）
与关于页的 `about_seal_mask.png` 墨迹不同，别互相替代。

## 三、设计稿

- `designs/about-redesign.html` —— 关于页重设计的候选稿合集，敲定的是「乙 · 终稿一 · 朱印细框」。已入库。
- `design/` —— 封面与动效的候选稿（不进仓库）。`03-终稿` 只由开发者挪，肥鱼不动。

## 四、新增或替换资产时必须做的

1. **放进 `gui/assets/` 对应子目录**，不要散落在别处。
2. **在本文档登记一行**：文件名、用途、被哪段程序读、由什么生成。
3. **有母版的一并入库存档**（同一份字节），别只留在 `_shot/` 这类被忽略的目录里。
4. 改完跑对应测试：`python -B tests/test_cover.py`（封面）/ `python -B tests/test_gui.py`（界面），
   再 `python -B tools/smoke_test.py`。逐个直跑，不用 pytest。
