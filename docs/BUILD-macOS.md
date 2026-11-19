# macOS 构建与自检清单

> 本文档用于在 **macOS 机器上**构建并验证 `.app`。
> 前提认知：**PyInstaller 不支持交叉编译**——Windows 上打不出 mac 包，必须在 mac 上构建。

---

## 零、先看：要不要分架构打包？

**决定产物架构的是【Python 解释器】，不是机器硬件。** 这点最容易搞混。

先确认：

```bash
python3 -c "import platform; print(platform.machine())"
```

| 解释器架构 | 产物能跑在哪 | 要分开打包吗 |
| --- | --- | --- |
| `x86_64`（Intel） | Intel + Apple Silicon（M 系列经 Rosetta 2） | ❌ 一个包通吃 |
| `arm64`（M 系列） | **仅** Apple Silicon，Intel Mac 跑不了 | ⚠️ 要分发给 Intel 用户则需处理 |
| `universal2` | Intel + Apple Silicon 原生 | ❌ 一个包通吃（最佳） |

**本项目依赖的架构情况**（已核实）：

| 依赖 | 是否含平台二进制 |
| --- | --- |
| `pywebview` / `httpx` / `httpcore` | 纯 Python，跨架构通用 |
| `PyObjC` 全套（Cocoa / WebKit 等） | **universal2**（同时含双架构） |
| `playwright` | **平台专属**，官方提供 x86_64 / arm64 / **universal2** 三种 |
| `pythonnet` | 无 mac wheel（mac 走 cocoa 后端，不需要） |

**所以唯一需要留意的是 `playwright`**，其余都能自适应。

### 三个可行方案（按省事程度排序）

**方案 A：用 Intel 版 Python 构建（最省事）**

产物能同时跑在 Intel 和 M 系列上。M 系列机器上可以这样装：

```bash
# 让终端用 Rosetta 模式运行
arch -x86_64 zsh
# 然后在此终端里创建虚拟环境
python3 -m venv .venv       # 此时会装 Intel 版 Python
source .venv/bin/activate
```

**方案 B：用 universal2 的 Python（最正统）**

从 [python.org](https://www.python.org/downloads/macos/) 下载的官方安装包本身就是 universal2。
装完确认：

```bash
python3 -c "import platform; print(platform.machine())"   # 期望输出 universal2
```

配合 PyInstaller 的 `target_arch="universal2"` 即可产出双架构 `.app`。

**方案 C：打两个包（最直接）**

分别在 Intel 机器和 M 系列机器上各构建一次，得到两个 `.app` 分发。
`build.sh` 会自动在产物名里标注架构（如 `-x64.app` / `-arm64.app`），不会搞混。

---

## 一、准备环境

```bash
# 1. 确认 Python 版本（推荐 3.11）
python3 --version          # 需要 >= 3.10

# 2. 进入项目目录
cd /path/to/xhs-video-downloader

# 3. 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 4. 安装依赖 + 打包工具
pip install -r requirements.txt
pip install pyinstaller

# 5. 安装 playwright 浏览器内核（可选）
#    如果 mac 上已装 Chrome，可以跳过；否则需要装内核兜底
python -m playwright install chromium
```

**检查点 1**：`python -c "import webview"` 无报错。
macOS 上 pywebview 走系统自带的 WKWebView（cocoa 后端），**不需要** pythonnet。

---

## 二、先跑源码版验证（强烈建议）

在打包之前先确认源码能跑通，避免把问题带进打包环节：

```bash
# 启动 GUI
python gui/app.py
```

**检查点 2**：窗口正常弹出，标题「小红书视频下载器」，左侧有表单、右侧有日志区。

**检查点 3**：左下角「浏览器内核」显示
- 有 Chrome → `将使用本机 Chrome（免装内核）`
- 无 Chrome 但有内核 → `将使用内置 playwright 内核`
- 都没有 → `未检测到 Chrome / Edge，请先安装其中之一`

**检查点 4**：填一个账号链接，点「开始下载」，能看到实时日志滚动、进度条推进。

> 若这一步就出问题，先别打包，把报错发出来一起看。

---

## 三、执行构建

### 先准备图标（可选，仓库已带生成好的）

仓库里 `assets/icon.ico` 已生成好可直接用。若要重新生成或调整配色：

```bash
pip install pillow
python assets/make_icon.py          # 生成 icon.ico / icon.png
```

macOS 还需要把 iconset 转成 `.icns`（`.app` 图标需要这个格式）：

```bash
iconutil -c icns assets/icon.iconset -o assets/icon.icns
```

> **关于图标**：本项目**不使用小红书官方 logo**（避免商标风险），
> 而是采用同色系原创设计（正红 #ff2442 + 白色下载箭头），
> 视觉上贴近小红书风格但不涉及品牌侵权。打包时 spec 会自动选
> Windows 用 `.ico`、macOS 用 `.icns`。

### 方式 A：用构建脚本（推荐）

```bash
chmod +x build.sh
./build.sh --check      # 只自检，不构建 —— 先看这一步行不行
./build.sh              # 自检 + 构建
./build.sh --clean      # 清掉旧产物重新构建
```

### 方式 B：手动构建

```bash
.venv/bin/python -m PyInstaller build_exe.spec --noconfirm --clean
```

**检查点 5**：构建结束无 `ERROR`，且出现：

```
dist/xhs-video-downloader.app
dist/xhs-video-downloader/        # 中间目录，可忽略
```

---

## 四、验证构建产物

```bash
# 1. 看体积（预期 130-200MB，若超过 500MB 说明内核被误打包）
du -sh dist/xhs-video-downloader.app

# 2. 首次运行（用命令行启动，方便看报错）
./dist/xhs-video-downloader.app/Contents/MacOS/xhs-video-downloader
```

**检查点 6**：窗口正常弹出，功能与源码版一致。

**检查点 7**：`get_env` 探测正确（界面左下角显示浏览器信息正确）。

**检查点 8**：点「打开」按钮能唤出 Finder 并定位到下载目录。

**检查点 9**：完整跑一次真实下载（至少 1 条视频落地）。

---

## 五、常见问题

### Q1：双击 .app 提示「已损坏」或「无法验证开发者」

macOS 的 Gatekeeper 拦截了未签名应用。三种处理方式：

```bash
# 方式 1（最简单）：移除隔离属性
xattr -cr dist/xhs-video-downloader.app

# 方式 2：右键 → 打开 → 在弹窗中选「打开」
# 方式 3（正式分发）：签名 + 公证
codesign --deep --force --sign "Developer ID Application: 你的名字 (TEAMID)" \
         dist/xhs-video-downloader.app
xcrun notarytool submit dist/xhs-video-downloader.app \
      --apple-id "你的AppleID" --team-id "TEAMID" --password "App专用密码" --wait
xcrun stapler staple dist/xhs-video-downloader.app
```

### Q2：报错 `No module named 'objc'` 或 `No module named 'Foundation'`

macOS 的 pywebview 依赖 PyObjC。补装：

```bash
pip install pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-WebKit
```

然后重新构建（spec 里已把这几个列为 hiddenimports）。

### Q3：报错 `playwright` 找不到浏览器

说明本机没装 Chrome 且没装内核。两种解法：

```bash
# 解法 A：装 playwright 内核（推荐，用户无需装 Chrome）
python -m playwright install chromium

# 解法 B：装 Chrome
brew install --cask google-chrome
```

注意 `.app` **不包含**浏览器内核（会让体积膨胀到 1GB+），
所以运行环境必须有 Chrome 或 playwright 内核二者之一。

### Q4：界面空白 / 白屏

WKWebView 加载 `file://` 资源有时受限制。检查：

```bash
# 确认 index.html 被打进去了
ls dist/xhs-video-downloader.app/Contents/Frameworks/gui/ 2>/dev/null \
  || find dist/xhs-video-downloader.app -name "index.html"
```

若缺失，说明 spec 的 `gui_datas` 没生效——检查 `build_exe.spec` 里
`datas` 是否包含 `index.html`。

### Q5：构建很慢或内存占用高

macOS 上 PyInstaller 打包 playwright driver（含 node 运行时）会比较慢，
属正常现象。首次构建通常 2-5 分钟。

### Q6：报错 `ImportError: dlopen ... mach-o file, but is an incompatible architecture`

架构不匹配（Apple Silicon M 系列 vs Intel）。
在 M 系列机器上：

```bash
# 查看当前 Python 架构
python -c "import platform; print(platform.machine())"

# 若要兼容两种架构，用 universal2 的 Python 构建
# 简单做法：只在目标架构上构建
```

### Q7：构建时报 `Permission denied` 或无法删除 `dist/`

PyInstaller 的 COLLECT 阶段会先删除已存在的 `dist/` 目录。
如果该目录被占用（例如 `.app` 还在运行、或 Finder 打开着），会失败。

```bash
# 1. 确认没有正在运行的实例
pkill -f xhs-video-downloader

# 2. 关掉可能占用该目录的 Finder 窗口

# 3. 手动删除后重建
rm -rf build dist
./build.sh
```

> 补充：若在受限沙箱 / 企业安全策略环境（批量删除被拦截）下构建，
> 可能会看到类似 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 的提示。
> 这种情况手动删除 `dist/`、`build/` 后再构建即可。

---

## 六、分发给别人时

| 项 | 说明 |
|---|---|
| **必须一起分发** | 整个 `.app`（是目录，不是单文件） |
| **无需装 Python** | 依赖已打包在内 |
| **需要装 Chrome** | 或让用户装 playwright 内核（见 Q3） |
| **首次打开提示** | 见 Q1，建议在说明里附上 `xattr -cr` 命令 |
| **登录态位置** | 保存在 `.app` 同级目录的 `.xhs_profile`（首次运行自动创建） |

---

## 七、自检结果记录表

构建完成后，把结果填在这里，便于排查问题：

| # | 检查项 | 结果 | 备注 |
|---|---|---|---|
| 1 | `import webview` 成功 | ☐ | |
| 2 | 源码版 GUI 能启动 | ☐ | |
| 3 | 浏览器探测提示正确 | ☐ | |
| 4 | 源码版能完成一次下载 | ☐ | |
| 5 | 构建无 ERROR，产出 .app | ☐ | |
| 6 | .app 能启动，界面正常 | ☐ | |
| 7 | 界面环境信息显示正确 | ☐ | |
| 8 | 「打开目录」能唤出 Finder | ☐ | |
| 9 | .app 能完成一次下载 | ☐ | |
| 10 | **产物名带正确架构标识** | ☐ | 如 `-arm64.app` / `-x64.app` |
| 11 | **目标机型实测可运行** | ☐ | Intel 用户需在 Intel 机器验证 |
| — | 体积（预期 130-200MB） | ___ MB | |
| — | macOS 版本 | ___ | |
| — | 架构（arm64/x86_64/universal2） | ___ | `python3 -c "import platform;print(platform.machine())"` |
| — | Python 版本 | ___ | |

---

## 八、若构建失败，需要提供的信息

把以下内容发出来，我能更快定位：

1. `./build.sh --check` 的完整输出
2. 构建失败的**最后 50 行**日志
3. 系统信息：
   ```bash
   sw_vers                        # macOS 版本
   python3 -c "import platform; print(platform.machine(), platform.python_version())"
   ls -d /Applications/Google\ Chrome.app 2>/dev/null && echo "Chrome 已装" || echo "Chrome 未装"
   python -c "import playwright, webview; print('依赖 OK')" 2>&1
   ```
