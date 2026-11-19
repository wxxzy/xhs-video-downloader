# -*- mode: python ; coding: utf-8 -*-
"""
build_exe.spec — PyInstaller 打包配置（小红书视频下载器 GUI）

支持平台：Windows / macOS / Linux（按当前构建平台自动选择产物形态）

设计原则
--------
1. **不打包浏览器内核**：chromium 内核约 437MB，打进包会让体积失控。
   改为运行时探测本机 Chrome / Edge（代码里已有 channel 支持）。
   playwright 的 driver（node 运行时 + CLI，约 103MB）必须打包。
2. **打包 playwright driver**：通过 collect_data_files 收进 _MEIPASS，
   并在运行时设置 PLAYWRIGHT_BROWSERS_PATH 与驱动路径环境变量。
3. **pywebview 后端按平台区分**：
   - Windows: pythonnet + clr_loader + edgechromium/winforms
   - macOS:   自带的 cocoa 后端（无需 pythonnet）
   - Linux:   gtk/qt 后端
4. **macOS 需要 BUNDLE 段**才会产出 .app；Windows/Linux 只要 COLLECT。

构建（不要跨平台交叉编译，各平台各自构建）：
    Windows:  .venv\\Scripts\\python.exe -m PyInstaller build_exe.spec --noconfirm
    macOS:    .venv/bin/python -m PyInstaller build_exe.spec --noconfirm
    或直接用: ./build.sh

产物：
    Windows:  dist/xhs-video-downloader/xhs-video-downloader.exe
    macOS:    dist/xhs-video-downloader.app
    Linux:    dist/xhs-video-downloader/xhs-video-downloader
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT = Path(SPECPATH).resolve()
GUI_DIR = PROJECT / "gui"

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

APP_NAME = "xhs-video-downloader"

# --- 目标架构（仅 macOS 有意义）---
# 默认跟随当前解释器架构；可通过环境变量 TARGET_ARCH 覆盖：
#   TARGET_ARCH=universal2 python -m PyInstaller build_exe.spec
# 注意：universal2 要求所有二进制依赖都含双架构代码。
# 本项目依赖中 PyObjC 是 universal2、playwright 官方有 universal2 版本，
# 但 cffi 等是分架构的 —— 因此 universal2 构建需配合 universal2 版 Python，
# 且务必在本机实测通过后再分发。
TARGET_ARCH = None
if IS_MACOS:
    _env_arch = os.environ.get("TARGET_ARCH", "").strip()
    if _env_arch in ("x86_64", "arm64", "universal2"):
        TARGET_ARCH = _env_arch
        print(f"[spec] TARGET_ARCH = {TARGET_ARCH}")

# --- 图标（跨平台：.ico 给 Windows，.icns 给 macOS）---
# 生成方式：python assets/make_icon.py
# 注意：不使用小红书官方 logo（商标风险），采用同色系原创设计
_ICON_ICO = PROJECT / "assets" / "icon.ico"
_ICON_ICNS = PROJECT / "assets" / "icon.icns"
ICON_FILE = None
if IS_WINDOWS and _ICON_ICO.exists():
    ICON_FILE = str(_ICON_ICO)
elif IS_MACOS and _ICON_ICNS.exists():
    ICON_FILE = str(_ICON_ICNS)
elif _ICON_ICO.exists():
    ICON_FILE = str(_ICON_ICO)   # Linux 也接受 png/ico，PyInstaller 会自行处理

# --- playwright driver（必需，含 node 运行时；各平台 driver 不同）---
pw_datas = collect_data_files("playwright")
pw_hidden = collect_submodules("playwright")

# --- pywebview：数据文件通用，隐式导入按平台区分 ---
webview_datas = []
for pkg in ("webview",):
    try:
        webview_datas += collect_data_files(pkg)
    except Exception:
        pass

webview_hidden = ["webview"]

if IS_WINDOWS:
    # Windows 走 WebView2（edgechromium），依赖 pythonnet / clr
    for pkg in ("pythonnet", "clr_loader"):
        try:
            webview_datas += collect_data_files(pkg)
        except Exception:
            pass
    webview_hidden += [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr", "pythonnet", "clr_loader",
        "proxy_tools",
    ]
elif IS_MACOS:
    # macOS 走系统的 WKWebView（cocoa 后端），不需要 pythonnet
    webview_hidden += ["webview.platforms.cocoa"]
    webview_hidden += ["objc", "Foundation", "AppKit", "WebKit", "PyObjCTools"]
else:
    # Linux 优先 gtk，其次 qt
    webview_hidden += ["webview.platforms.gtk", "webview.platforms.qt"]
    webview_hidden += ["gi", "PyQt5", "PyQt6"]

# --- 界面与静态资源 ---
gui_datas = [
    (str(GUI_DIR / "index.html"), "gui"),
]

hiddenimports = sorted(set(pw_hidden + webview_hidden + [
    "playwright", "playwright.async_api",
    "httpx", "httpcore", "anyio", "h11", "certifi", "idna", "sniffio",
    "xhs_video_downloader", "platform_utils",
]))

a = Analysis(
    [str(GUI_DIR / "app.py")],
    pathex=[str(PROJECT), str(GUI_DIR)],
    binaries=[],
    datas=pw_datas + webview_datas + gui_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "matplotlib", "numpy", "pandas", "scipy", "PIL",
        "tkinter", "test", "unittest",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序，不弹黑窗
    disable_windowed_traceback=False,
    icon=ICON_FILE,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
    target_arch=TARGET_ARCH,   # macOS 有效；Windows/Linux 为 None
)

# ---------------------------------------------------------------------------
# macOS：额外产出 .app bundle
# ---------------------------------------------------------------------------
# Windows/Linux 下 BUNDLE 会被忽略（PyInstaller 只在 darwin 上处理）。
# .app 需要 Info.plist 的 CFBundleIdentifier 等信息；
# 若要分发给别人，还需 codesign + notarytool 公证，否则会被 Gatekeeper 拦截。
if IS_MACOS:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=str(_ICON_ICNS) if _ICON_ICNS.exists() else None,
        bundle_identifier="com.local.xhs-video-downloader",
        info_plist={
            "CFBundleName": "小红书视频下载器",
            "CFBundleDisplayName": "小红书视频下载器",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            # GUI 程序：不在 Dock 显示控制台图标
            "LSBackgroundOnly": False,
            # 允许访问用户选择的文件/下载目录
            "NSDownloadsFolderUsageDescription": "用于保存下载的小红书视频",
        },
    )
