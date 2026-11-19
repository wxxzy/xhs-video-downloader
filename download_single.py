# -*- coding: utf-8 -*-
"""download_single.py — 下载单条笔记视频（URL 带 xsec_token）"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
import xhs_video_downloader as x  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

NOTE_URL = (
    "https://www.xiaohongshu.com/explore/6ac323d2000000001a02b95f"
    "?xsec_token=CBo2dRE4OSnRhYwbLs7Ah-mWYCxyutl3TeSAJBz0XhlDg=&xsec_source=app_share"
)
NOTE_ID = "6ac323d2000000001a02b95f"
OUT_DIR = PROJECT / "downloads" / "single"


async def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = x.DownloadState.load(OUT_DIR / "state.json")
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROJECT / ".xhs_profile"),
            headless=False,
            viewport=x.VIEWPORT,
            user_agent=x.USER_AGENT,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        sniffer = x.ApiSniffer(page)

        print(f"打开详情页：{NOTE_ID}")
        detail = await x.fetch_note_detail(page, NOTE_ID, NOTE_URL, sniffer)
        await ctx.close()

    if not detail or detail.get("type") != "video" or not detail.get("video"):
        print("提取失败：", detail and detail.get("type"))
        sys.exit(1)

    title = detail.get("title") or "untitled"
    stream = detail["video"]
    note = {
        "id": NOTE_ID,
        "title": title,
        "time": detail.get("time"),
        "video": stream,
        "video_meta": detail.get("video_meta"),
    }
    print(f"标题：{title}")
    print(f"清晰度：{stream.get('width')}x{stream.get('height')}  大小：{(stream.get('size') or 0)/1024/1024:.1f} MB")
    print(f"直链：{str(stream.get('master_url'))[:90]}...")

    dl = x.VideoDownloader(OUT_DIR, cookies={}, concurrency=1, state=state, retries=3)
    try:
        results = await dl.download_all([note])
    finally:
        await dl.close()
    print("结果：", results)
    for f in OUT_DIR.glob("*.mp4"):
        if NOTE_ID in f.name:
            print("已保存：", f)


asyncio.run(main())
