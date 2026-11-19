# -*- coding: utf-8 -*-
"""
gui/selftest.py — GUI 端到端自测

启动真实窗口 → 触发任务 → 通过轮询接口校验日志/进度回推 → 截图 → 退出

用法：
    python gui/selftest.py            # 单条下载
    python gui/selftest.py account    # 账号 list-only（快）
"""
import os
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "gui"))

import webview  # noqa: E402
from bridge import GuiBridge  # noqa: E402

# 使用前请替换成你自己的笔记分享链接（需含有效的 xsec_token）。
# 出于隐私与合规考虑，仓库中不保留真实的 token。
NOTE_URL = (
    "https://www.xiaohongshu.com/explore/<你的笔记ID>"
    "?xsec_token=<你的xsec_token>&xsec_source=app_share"
)
ACCOUNT = "<你的用户ID>"
SHOT = PROJECT / "gui" / "_selftest.png"
MODE = sys.argv[1] if len(sys.argv) > 1 else "single"


def log(msg):
    print(msg, flush=True)


def shot_window(title_part: str, out_path: Path) -> bool:
    """用 Win32 API 截取指定窗口（pywebview 6.2 无 screenshot 方法）"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hwnd_found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_cb(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if title_part in buf.value:
                    hwnd_found.append(hwnd)
                    return False
            return True

        user32.EnumWindows(enum_cb, 0)
        if not hwnd_found:
            return False
        hwnd = hwnd_found[0]
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        hdc = user32.GetWindowDC(hwnd)
        memdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(memdc, bmp)
        user32.PrintWindow(hwnd, memdc, 2)   # PW_RENDERFULLCONTENT

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = w, -h
        bi.biPlanes, bi.biBitCount = 1, 24
        buf_size = w * h * 3
        buffer = ctypes.create_string_buffer(buf_size)
        gdi32.GetDIBits(memdc, bmp, 0, h, buffer, ctypes.byref(bi), 0)

        # 写 PNG（纯 stdlib：用 zlib + struct 手写 PNG）
        import struct
        import zlib
        raw = bytearray()
        row_bytes = w * 3
        pad = (4 - (row_bytes % 4)) % 4
        stride = row_bytes + pad
        for y in range(h):
            raw.append(0)
            raw.extend(buffer[y * stride: y * stride + row_bytes])
        compressed = zlib.compress(bytes(raw), 6)

        def chunk(tag, data):
            c = struct.pack(">I", len(data)) + tag + data
            return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", compressed)
        png += chunk(b"IEND", b"")
        out_path.write_bytes(png)

        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(hwnd, hdc)
        return True
    except Exception as exc:
        print(f"[selftest] 截图失败: {exc}", flush=True)
        return False


def main():
    holder = {}
    bridge = GuiBridge(holder)
    index = (PROJECT / "gui" / "index.html").as_uri()
    window = webview.create_window("GUI 自测", url=index, js_api=bridge,
                                   width=1060, height=760)
    holder["window"] = window
    done = threading.Event()

    def worker():
        time.sleep(3)
        try:
            if MODE == "single":
                window.evaluate_js(
                    f"document.getElementById('url').value = {NOTE_URL!r};"
                    "switchTab('single');"
                )
                opts = {"mode": "single", "url": NOTE_URL,
                        "out": str(PROJECT / "downloads")}
            else:
                window.evaluate_js(
                    f"document.getElementById('url').value = {ACCOUNT!r};"
                )
                opts = {"mode": "account", "url": ACCOUNT,
                        "out": str(PROJECT / "downloads"), "list_only": True}

            log(f"[selftest] 模式: {MODE}")
            log(f"[selftest] start -> {bridge.start(opts)}")

            # 轮询直到任务结束
            t0 = time.time()
            while time.time() - t0 < 240:
                time.sleep(1)
                if not bridge._running:
                    break

            upd = bridge.get_updates(0)
            log(f"[selftest] 耗时 {time.time()-t0:.1f}s")
            log(f"[selftest] 进度状态: {upd['state']}")
            log(f"[selftest] 日志 {upd['total']} 条：")
            for item in upd["logs"][-14:]:
                log(f"   [{item['level']}] {item['message']}")
            log(f"[selftest] finish: {upd['finish']}")

            try:
                ok = shot_window("GUI 自测", SHOT)
                log(f"[selftest] 截图{'已保存' if ok else '失败'}: {SHOT}")
            except Exception as exc:
                log(f"[selftest] 截图异常: {exc}")
        except Exception as exc:
            import traceback
            log(f"[selftest] 异常: {exc}")
            traceback.print_exc()
        finally:
            log("[selftest] 结束")
            done.set()
            time.sleep(0.5)
            os._exit(0)   # WebView2 下 destroy 可能挂起，强制退出

    threading.Thread(target=worker, daemon=True).start()
    webview.start(debug=False)


if __name__ == "__main__":
    main()
