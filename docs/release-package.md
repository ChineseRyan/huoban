# 发试用包：流程

试用包 = `dist/huoban-<源码版本或提交>-win64.zip`，里面是 PyInstaller 的 onedir 产物
（`huoban/huoban.exe` + `huoban/_internal/`）。

**`dist/` 整个目录被 `.gitignore` 忽略**，所以这份流程文档放 `docs/`。

## 包里不带任何说明文件（2026-11-17 起）

2026-11-17 开发者定：**测试包里不放 `使用说明.txt`，`dist/` 里也不放，两边都不要**；
此要求适用于此后所有版本。包内只有 PyInstaller 产物，`dist/` 里同版只留 zip 与它的 `.sha256`。

历史沿革（只作背景）：`dist/` 曾同时躺着四份说明（`README-试用包.md`、`试用包说明.md`、
`使用说明.txt`、`_zip_readme.txt`），2026-11-06 收敛为与当前版本一致的一份
`使用说明.txt`（旧三份移到 `backups/trash-20261106/dist-readmes/`，没删）；2026-11-17 连这一份
也取消。**旧 zip 里带的说明是历史产物，别拿它当现行口径。**

## 两个坑仍在（原来写在说明里的那两条）

说明文件取消了，但这两件事仍然成立，用户遇到时按此解释：

1. **解压到工作区之外**。不要解压在开发目录（源码所在的那一层）里面 —— Windows 会给带
   「低完整性等级」标记的目录里的 .NET 程序集上锁，程序一闪就退，报
   `Failed to resolve Python.Runtime.Loader.Initialize`。解压到桌面、D 盘或任何普通目录都行。
2. **别只把 exe 拖出来**。`huoban.exe` 旁边的 `_internal` 是整个运行时，必须一起待着；
   想挪地方就整个 `huoban` 文件夹一起挪。

这两条踩过两次，每次都像是「程序写坏了」——一次是解压到工作区导致 .NET 拒绝加载程序集、
exe 一闪就退；一次是只拖了 exe 出来、缺 `_internal`。它们跟界面无关，但用户第一反应一定是
「界面坏了」。

## 打包步骤

    cd <项目根>
    python -m PyInstaller build/gui.spec --noconfirm --clean

然后：

1. 确认 `dist/huoban/huoban.exe --selftest` 通过。
2. 把 `dist/huoban/` 打成 zip，**保留 `huoban/` 这一层目录**，文件名
   `huoban-<版本号或提交>-win64.zip`，放 `dist/`。
3. 生成 `.sha256`（与 zip 同名 + `.sha256`）。
4. 在 `CHANGELOG.md` 对应版本条目末尾补一行「桌面试用包随本次一起重新构建」。

**可用性自查**（发出去之前跑一遍）：

- `dist/huoban/_internal/fonts/` 里应当有 2 个 `.otf` **和 3 个许可证 `.txt`**
  （`OFL-NotoSerifCJK.txt` / `OFL-HanaMinB.txt` / `Hanazono-Font-License.txt`）。
  缺了就是 `build/gui.spec` 的许可证收集段出了问题 —— SIL OFL 1.1 要求分发字体副本时附带许可证。
- zip 文件名里的版本号与 `huoban/__init__.py` 的 `__version__` **两方一致**。
- 包内与 `dist/` 根下都没有任何说明文件（2026-11-17 新口径）。

## 发版后同步到 GitHub Release

**时间节点**：只在开发者**明确说「发版」之后**才动 GitHub。打包、提交、推送都不算发版，
不许自己判断「差不多了」就发。

**动作**（顺序固定）：

1. 打 tag：`v<版本号>`（如 `v0.3.0`），与 release 名「活板 <版本号>」对应；
2. 用 GitHub CLI 建 release：`gh release create`，说明取自 `CHANGELOG.md` 对应版本段落；
3. 上传两个文件：zip 与 `.sha256`。

**硬约束**：上传到 GitHub 的 zip **不许包含 `使用说明.txt`**，与本地包同一口径——
包内只有 `huoban/` 一层目录。

**发版前核对**：`__version__` 与 tag、release 名三者一致；`git status` 干净（除忽略项）。

**工具现状**（少踩坑）：本机 `gh` 已登录 `ChineseRyan`、有 `repo` 权限，可直接
`gh release create`；项目 remote 是 SSH，推送与 release 走两套通道，互不影响。

## 发给别人时还要注意

- 不同版本的「已知限制」会变。当前这些属于**设计**不是 bug：AZW3 每次字节不同
  （Calibre 写时间戳）、整本糊成一行的 txt 切不出章节（归 M4）、一次只接一个文件、
  EPUB 导入是重排不是转换、封面按书名确定性轮转。用户问到别当 bug，也别按旧的说明文本答复。
- 旧 zip 不必删：`dist/` 里按版本留档，各自的产物原样保留，便于对照复现。
