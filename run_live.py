#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_live.py — 实跑：下载指定小红书账号的全部视频

流程：启动浏览器 → 打开小红书 → 未登录时二维码截图存 login_qr.png
→ 等待扫码登录（最长 15 分钟）→ 收集全部笔记 → 提取直链 → 下载。
日志同时写入 live_run.log，进度不丢。

注意：登录态失效（日志出现"登录超时"或跳转 /login）时，
需要人工在弹出的浏览器窗口扫码，或用 --cookie 注入新 Cookie。
"""
import asyncio
import logging
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))
import xhs_video_downloader as x  # noqa: E402

PROFILE_URL = (
    "https://www.xiaohongshu.com/user/profile/67a4612d000000000d009085"
    "?xsec_token=YBaTvc-8OvwdnIjhSMfZW8Qlv7lNjg7Yo6ifC38Uhn3-M="
    "&xsec_source=app_share&xhsshare=WeixinSession"
    "&appuid=59382cd282ec391d2836a092&apptime=1792326196"
    "&share_id=8826f3c4af8c4fbf8655bd243325c6cf&track_code=6OJN7wsviBs"
)


def detect_channel():
    """优先本机 Chrome（免装内核），不可用再回退 playwright 自带 chromium"""
    import shutil

    if shutil.which("chrome") or Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe").exists():
        print("[runner] 使用本机 Chrome", flush=True)
        return "chrome"
    return None


def main():
    log_file = PROJECT_DIR / "live_run.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    channel = detect_channel()
    argv = [
        "--user", PROFILE_URL,
        "--qr-screenshot", str(PROJECT_DIR / "login_qr.png"),
        "--out", str(PROJECT_DIR / "downloads"),
        "--profile-dir", str(PROJECT_DIR / ".xhs_profile"),
        "--login-timeout", "900",
        "--device-scale-factor", "3",
        "--delay", "2.5",
        "--concurrency", "2",
        "--scroll-pause", "1.5",
    ]
    if channel:
        argv += ["--channel", channel]
    else:
        argv += ["--headless"]
    print(f"[runner] 启动参数：{' '.join(argv)}", flush=True)
    x.main(argv)


if __name__ == "__main__":
    main()
