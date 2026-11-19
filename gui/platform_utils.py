# -*- coding: utf-8 -*-
"""
platform_utils.py — 跨平台探测与适配工具

集中处理 Windows / macOS / Linux 的差异，避免平台判断散落各处。

覆盖三类差异：
1. 浏览器可执行文件位置（Chrome / Edge）
2. 打开目录 / 文件的方式
3. playwright 浏览器内核缓存目录
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# ---------------------------------------------------------------------------
# 浏览器探测
# ---------------------------------------------------------------------------
# 每个候选路径都写清楚所属平台，便于排查
_CHROME_CANDIDATES: dict[str, list[str]] = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ],
    "linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ],
}

_EDGE_CANDIDATES: dict[str, list[str]] = {
    "win32": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "darwin": [
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
    "linux": [
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
    ],
}


def _platform_key() -> str:
    if IS_WINDOWS:
        return "win32"
    if IS_MACOS:
        return "darwin"
    return "linux"


def _first_existing(paths: list[str]) -> Optional[Path]:
    for raw in paths:
        p = Path(os.path.expanduser(raw))
        if p.exists():
            return p
    return None


def find_browser() -> tuple[Optional[Path], Optional[str]]:
    """
    探测本机浏览器，返回 (可执行文件路径, playwright channel 名)。
    channel 取值为 'chrome' / 'msedge' / None（None 表示回退到 playwright 内核）。
    """
    chrome = _first_existing(_CHROME_CANDIDATES.get(_platform_key(), []))
    if chrome:
        return chrome, "chrome"
    edge = _first_existing(_EDGE_CANDIDATES.get(_platform_key(), []))
    if edge:
        # macOS/Linux 上 Edge 也能被 playwright 以 channel="msedge" 驱动
        return edge, "msedge"
    # 兜底：走 PATH 查找
    for name, channel in (("google-chrome", "chrome"), ("chrome", "chrome"),
                          ("chromium", "chrome"), ("microsoft-edge", "msedge")):
        found = shutil.which(name)
        if found:
            return Path(found), channel
    return None, None


# ---------------------------------------------------------------------------
# 打开目录 / 文件
# ---------------------------------------------------------------------------
def open_in_file_manager(path: str | Path) -> tuple[bool, str]:
    """
    用系统文件管理器打开指定路径。
    返回 (是否成功, 错误信息)。
    """
    p = Path(path)
    if not p.exists():
        return False, "目录不存在"
    try:
        if IS_WINDOWS:
            # explorer 对路径中的正斜杠不友好，转成反斜杠
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif IS_MACOS:
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True, ""
    except Exception as exc:
        # 兜底：尝试通用方式
        try:
            import webbrowser
            webbrowser.open(p.as_uri())
            return True, ""
        except Exception:
            return False, str(exc)


# ---------------------------------------------------------------------------
# playwright 浏览器内核目录
# ---------------------------------------------------------------------------
def playwright_browsers_dir() -> Optional[Path]:
    """
    返回 playwright 浏览器内核缓存目录（若存在）。
    - Windows: %LOCALAPPDATA%\\ms-playwright
    - macOS:   ~/Library/Caches/ms-playwright
    - Linux:   ~/.cache/ms-playwright
    若已通过环境变量 PLAYWRIGHT_BROWSERS_PATH 指定，则优先使用。
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0":
        p = Path(env)
        if p.exists():
            return p

    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        candidates = [Path(local) / "ms-playwright"] if local else []
    elif IS_MACOS:
        candidates = [Path.home() / "Library" / "Caches" / "ms-playwright"]
    else:
        candidates = [Path.home() / ".cache" / "ms-playwright"]

    for c in candidates:
        if c.exists():
            return c
    return None


def describe_platform() -> str:
    """用于日志/自检输出的平台描述"""
    if IS_WINDOWS:
        return f"Windows ({sys.platform})"
    if IS_MACOS:
        return f"macOS ({sys.platform})"
    if IS_LINUX:
        return f"Linux ({sys.platform})"
    return sys.platform
