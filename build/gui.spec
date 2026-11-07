# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：huoban 桌面版（onedir）。

为什么 onedir 不用 onefile（§1 决策表）：onefile 每次启动都要把几十 MB 解压到
`%TEMP%`（很多人那儿的 TEMP 还在慢盘或内存盘上），解压慢、还多一份重复占用。
onedir 首次启动直接把 exe 交给用户，启动快、目录里能看到 `_internal` 是什么。

构建（在项目根下）：

    python -m PyInstaller build/gui.spec --noconfirm --clean

产物：`dist/huoban/huoban.exe`（GUI 无控制台）+ 同目录 `_internal/`。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

# SPECPATH 是 spec 文件所在目录（即 build/），项目根是它的上一级。
# 踩过：这里多写一层 `.parent` 会指到项目的上一级，
# PyInstaller 随之报 script '<上一级>/gui/launcher.py' not found。
ROOT = Path(SPECPATH).resolve().parent         # 项目根
GUI = ROOT / "gui"
PKG = ROOT / "huoban"

# 方案把 gui/ 与 huoban/ 都按"包"收进去，而不是只收 ui.html：
# app.py 用 `_resource()` 在 `sys._MEIPASS/gui/assets` 下找图标，ui.html 也从
# 同一处读。整包收进来最省心，代价只多几十 KB（assets 的 4 个图标 ~930KB）。
datas = [
    (str(GUI / "ui.html"), "gui"),
    (str(GUI / "assets"), "gui/assets"),
]

# 内嵌字库（fonts/，约 57MB）：思源宋体完整版 + 花园明朝 B。fonts.py 的
# _font_dirs() 拿 __file__ 往上两级找 fonts/，冻结后在 _MEIPASS/fonts，
# 正是这里的 target。不打包不会崩 —— 只是退回系统字体、扩展 B 区汉字出缺字
# 告警；但那就等于把这个功能在 exe 里废掉了，所以还是收进来。
# 目录不存在时跳过：字库不入 git，别人 clone 下来没有这份目录也不该构建失败。
_FONTS = ROOT / "fonts"
if _FONTS.is_dir():
    datas.append((str(_FONTS), "fonts"))

# 字体的许可证原文。SIL OFL 1.1 第 2 条要求分发字体副本时附带版权声明与许可证，
# 所以许可证必须跟包走，不能只躺在仓库里。真身在 docs/licenses/（入库、可复核），
# 这里把它们并到 _internal/fonts/ 下，和两个 .otf 放一起 —— 用户翻到字体就看见许可。
# 只收 *.txt：README.md 是给人看的地图，不进发行包。
_LICENSES = ROOT / "docs" / "licenses"
# 用 glob 而不是整目录：README.md 是给人看的地图，不该进发行包。注意
# format_binaries_and_datas 在 glob 零命中时会 SystemExit，所以先确认有 .txt。
if _LICENSES.is_dir() and any(_LICENSES.glob("*.txt")):
    datas.append((str(_LICENSES / "*.txt"), "fonts"))

# webview 的 WebView2 平台层是**动态导入**的（webview/platforms/__init__.py 里
# 按 os.name 挑），静态分析看不到，必须显式点名，否则打出来的 exe 一启动就
# 报 `platforms not found`。
hiddenimports = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]

# PyInstaller 自带 webview 的 hook（webview/__pyinstaller），但不同版本覆盖
# 面不一样，这里再显式收一份 js 资源兜底 —— 缺了它窗口能开但 API 桥不工作。
datas += collect_data_files("webview")

# Pillow 的插件会拖进一堆 Tk/Qt 相关的东西。封面只用 PNG/JPEG 编解码，
# 明确排除加速包不会影响功能，但能把打包体积砍下来一大截。
excludes = [
    "tkinter",
    "unittest",
    "pydoc_data",
    "PyQt5",
    "PySide2",
    "PySide6",
    "matplotlib",
    "numpy",
    "pytest",
]

block_cipher = None

a = Analysis(
    [str(ROOT / "gui" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="huoban",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 会把 exe 报成可疑文件，个人自用也犯不上
    console=False,      # 双击不弹黑框；调错看 build/dist 里的日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(GUI / "assets" / "huoban.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="huoban",
)
