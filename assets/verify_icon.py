# -*- coding: utf-8 -*-
"""verify_icon.py — 从 exe 中提取图标并保存为 PNG，用于确认图标生效"""
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from PIL import Image

PROJECT = Path(__file__).resolve().parent.parent
EXE = PROJECT / "dist" / "xhs-video-downloader" / "xhs-video-downloader.exe"
OUT = PROJECT / "assets" / "_verify_extracted.png"


def main() -> int:
    if not EXE.exists():
        print("exe 不存在:", EXE)
        return 1

    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    # 提取大图标（SHGFI_ICON=0x100, SHGFI_LARGEICON=0x0）
    class SHFILEINFO(ctypes.Structure):
        _fields_ = [
            ("hIcon", wintypes.HICON),
            ("iIcon", ctypes.c_int),
            ("dwAttributes", wintypes.DWORD),
            ("szDisplayName", wintypes.WCHAR * 260),
            ("szTypeName", wintypes.WCHAR * 80),
        ]

    info = SHFILEINFO()
    flags = 0x000000100 | 0x000000000   # SHGFI_ICON | SHGFI_LARGEICON
    res = shell32.SHGetFileInfoW(str(EXE), 0, ctypes.byref(info),
                                 ctypes.sizeof(info), flags)
    if not res or not info.hIcon:
        print("无法提取图标（res=%s hIcon=%s）" % (res, info.hIcon))
        return 1

    # 把 HICON 画到 DIB 再取像素
    hicon = info.hIcon
    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    ii = ICONINFO()
    user32.GetIconInfo(hicon, ctypes.byref(ii))

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                    ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                    ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    bm = BITMAP()
    gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm))
    w, h = bm.bmWidth, bm.bmHeight
    print(f"提取到图标：{w}×{h}")

    hdc = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(hdc)

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
    bi.biPlanes, bi.biBitCount = 1, 32
    bufsize = w * h * 4
    buf = ctypes.create_string_buffer(bufsize)
    gdi32.GetDIBits(memdc, ii.hbmColor, 0, h, buf, ctypes.byref(bi), 0)

    # BGRA -> RGBA
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
    img.save(OUT)
    print("已保存:", OUT)

    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(0, hdc)
    user32.DestroyIcon(hicon)
    return 0


if __name__ == "__main__":
    sys.exit(main())
