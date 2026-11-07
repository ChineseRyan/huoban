# 发试用包：流程与包内说明

试用包 = `dist/huoban-<源码版本或提交>-win64.zip`，里面是 PyInstaller 的 onedir 产物
（`huoban/huoban.exe` + `huoban/_internal/`）加一份 `使用说明.txt`。

**`dist/` 整个目录被 `.gitignore` 忽略**，所以这份流程文档放这里（`docs/`），
包内的 `使用说明.txt` 每次发版现写。

## 一、包里的说明只留一份：`使用说明.txt`

历史上 `dist/` 曾同时躺着四份说明（`README-试用包.md`、`试用包说明.md`、`使用说明.txt`、
`_zip_readme.txt`），分别对应 921a58f / a68ceba / 0.1.2 / 3aaf9be 四个不同的包，内容重复、
版本各异，发出去很容易拿错。**2026-11-06 已合并**：只保留与当前源码版本一致的那一份
`使用说明.txt`，其余三份移到 `backups/trash-20261106/dist-readmes/`（没删，需要翻旧账还能看）。

规矩：**`dist/使用说明.txt` 永远等于「当前`dist/huoban/` 和最新 zip 对应的那个版本」**。
旧包的说明留在旧 zip 里就够了，不要在 `dist/` 根上并列摆放。

## 二、`使用说明.txt` 的模板

正文用**纯文本、UTF-8 带 BOM 以外**（现在这份是 UTF-8 无 BOM，Windows 记事本也能正常打开）。
长度控制在 40 行以内 —— 用户是在解压现场读它，不是在读手册。

    ═══════════════════════════════════════════
    活板 · huoban <版本号> 试用包
    源码版本 <版本号> ／ <YYYY-MM-DD> 打包

    解压后双击 huoban\huoban.exe 就能用，不用装 Python。

    两件事必须注意：

    1. 解压到工作区之外。
       不要解压在开发目录（源码所在的那一层）里面 —— Windows 会给带「低完整性等级」
       标记的目录里的 .NET 程序集上锁，程序一闪就退，报
       Failed to resolve Python.Runtime.Loader.Initialize。
       解压到桌面、D 盘或任何普通目录都行。

    2. 别只把 exe 拖出来。
       huoban.exe 旁边的 _internal 是整个运行时，必须一起待着。
       想挪地方就整个 huoban 文件夹一起挪。

    自检：huoban.exe --selftest
    产物落在输入文件同目录的 <书名>_gui\ 里，原文件一个字都不会改。

    本版修复：<这一版改了什么，三四行以内；与 CHANGELOG.md 该版本条目对齐>

**为什么那两条要写在最前面**：这两条踩过两次，每次都像是「程序写坏了」——
一次是解压到工作区导致 .NET 拒绝加载程序集、exe 一闪就退；一次是只拖了 exe 出来、
缺 `_internal`。它们跟界面无关，但用户第一反应一定是「界面坏了」。

## 三、打包步骤

    cd <项目根>
    python -m PyInstaller build/gui.spec --noconfirm --clean

然后：

1. 确认 `dist/huoban/huoban.exe --selftest` 通过。
2. 把 `dist/huoban/` 打成 zip，**保留 `huoban/` 这一层目录**，文件名
   `huoban-<版本号或提交>-win64.zip`，放 `dist/`。
3. 生成 `.sha256`（与 zip 同名 + `.sha256`）。
4. 写 `dist/使用说明.txt`（见上面模板），确认里面的版本号 = zip 文件名里的那个。
5. 在 `CHANGELOG.md` 对应版本条目末尾补一行「桌面试用包随本次一起重新构建」。

**可用性自查**（发出去之前跑一遍）：

- `dist/huoban/_internal/fonts/` 里应当有 2 个 `.otf` **和 3 个许可证 `.txt`**
  （`OFL-NotoSerifCJK.txt` / `OFL-HanaMinB.txt` / `Hanazono-Font-License.txt`）。
  缺了就是 `build/gui.spec` 的许可证收集段出了问题 —— SIL OFL 1.1 要求分发字体副本时附带许可证。
- `dist/使用说明.txt` 的版本号与 zip 文件名、`huoban/__init__.py` 的 `__version__` 三者一致。
- 包里不要出现旧版本的说明文件（只有一份 `使用说明.txt`）。

## 四、发给别人时还要注意

- 不同版本正文里写的「已知限制」会变。当前这些属于**设计**不是 bug：AZW3 每次字节不同
  （Calibre 写时间戳）、整本糊成一行的 txt 切不出章节（归 M4）、一次只接一个文件、
  EPUB 导入是重排不是转换、封面按书名确定性轮转。写说明时别漏，否则会被当 bug 报。
- 旧 zip 不必删：`dist/` 里按版本留档，各自带各自的 `使用说明`，便于对照复现。
