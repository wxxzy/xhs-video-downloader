# -*- coding: utf-8 -*-
"""
app.py — 小红书视频下载器 GUI 启动入口

用法：
    python gui/app.py
或（打包后）直接双击 exe。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 兼容三种运行方式：源码运行 / 打包后 _MEIPASS / 打包后 exe 同级
_HERE = Path(__file__).resolve().parent
PROJECT_DIR = _HERE.parent
_MEIPASS = Path(getattr(sys, "_MEIPASS", _HERE))

for _p in (PROJECT_DIR, _HERE, _MEIPASS, _MEIPASS / "gui"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)


def _patch_frozen_paths() -> None:
    """
    PyInstaller 打包后：playwright 的 driver 被收进 _MEIPASS，
    但 playwright 默认按 site-packages 相对路径找，需显式指向。
    浏览器内核不打包，仍用本机缓存的 ms-playwright 目录（跨平台探测）。
    """
    if not getattr(sys, "frozen", False):
        return
    meipass = Path(getattr(sys, "_MEIPASS", PROJECT_DIR))
    # driver 目录（node 运行时 + cli.js）
    for rel in ("playwright/driver", "playwright\\driver"):
        driver = meipass / rel
        if driver.exists():
            os.environ.setdefault("PLAYWRIGHT_DRIVER_PATH", str(driver))
            break
    # 内核目录：指向用户本机缓存（Windows / macOS / Linux 路径各不相同）
    try:
        from platform_utils import playwright_browsers_dir
        browsers = playwright_browsers_dir()
    except Exception:
        browsers = None
    if browsers:
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))


WINDOW_TITLE = "小红书视频下载器"
MIN_SIZE = (940, 680)


def resolve_index_html() -> str:
    """定位 index.html（兼容 PyInstaller 打包后的 _MEIPASS）"""
    candidates = [
        Path(__file__).resolve().parent / "index.html",
        Path(getattr(sys, "_MEIPASS", PROJECT_DIR)) / "gui" / "index.html",
        PROJECT_DIR / "gui" / "index.html",
    ]
    for c in candidates:
        if c.exists():
            return c.as_uri()
    raise FileNotFoundError("找不到 gui/index.html")


def main() -> int:
    _patch_frozen_paths()
    try:
        import webview
    except ImportError:
        print("缺少 pywebview 依赖，请执行：pip install pywebview", file=sys.stderr)
        return 1

    from bridge import GuiBridge

    holder: dict = {}
    bridge = GuiBridge(holder)
    window = webview.create_window(
        WINDOW_TITLE,
        url=resolve_index_html(),
        js_api=bridge,
        width=1060,
        height=760,
        min_size=MIN_SIZE,
        text_select=True,
    )
    holder["window"] = window

    # 关闭前若任务在跑，先请求取消，避免留下 .part 临时文件
    def on_closing():
        try:
            if bridge._running:
                bridge.cancel()
        except Exception:
            pass

    try:
        window.events.closing += on_closing
    except Exception:
        pass

    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
