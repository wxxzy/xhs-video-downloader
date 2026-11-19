# -*- coding: utf-8 -*-
"""
gui/render_preview.py — 用 Playwright 渲染 index.html 生成界面预览图
（WebView2 内容无法用 PrintWindow 截图，改用同内核的 Chromium 渲染同一份 HTML）
"""
import asyncio
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from playwright.async_api import async_playwright  # noqa: E402

OUT = PROJECT / "docs" / "gui-preview.png"

# 注入到页面的模拟 API，让界面呈现"运行中"的真实样子
STUB_JS = """
window.pywebview = {
  api: {
    get_updates: async (since) => window.__nextUpdates(since),
    get_env: async () => ({
      project_dir: 'C:/workspaces/xhs-video-downloader',
      python: '3.11.9',
      default_out: 'C:/workspaces/xhs-video-downloader/downloads',
      profile_dir: 'C:/workspaces/xhs-video-downloader/.xhs_profile',
      logged_in_hint: true, chrome: true, edge: true, channel: 'chrome',
      running: false,
    }),
    check_login: async () => ({ logged_in: true }),
    start: async () => ({ ok: true }),
    login_only: async () => ({ ok: true }),
    cancel: async () => ({ ok: true }),
    pick_folder: async () => 'C:/workspaces/xhs-video-downloader/downloads',
    open_folder: async () => ({ ok: true }),
  },
};
"""

DEMO_LOGS = [
    ("INFO", "用户 ID：661392a7000000000700600f"),
    ("INFO", "输出目录：C:/workspaces/xhs-video-downloader/downloads/661392a7000000000700600f"),
    ("INFO", "已检测到保存的登录状态，直接使用"),
    ("INFO", "打开用户主页：https://www.xiaohongshu.com/user/profile/661392a7000000000700600f"),
    ("INFO", "SSR 数据提取到 10 篇笔记（新版主页首屏内嵌）"),
    ("INFO", "主页收集完成，共 10 篇笔记"),
    ("INFO", "笔记统计：视频 10 篇，待确认 0 篇，已确认图文 0 篇"),
    ("INFO", "待处理 10 篇（已下载的自动跳过）"),
    ("INFO", "[1/10] 打开详情页 6ad6beea000000002803d513"),
    ("INFO", "    ✓ 当我试图驯服ai做短剧（11）"),
    ("INFO", "获取到 10 个视频直链，开始下载（并发 2）"),
    ("INFO", "  ↓ 20261020_6ad6beea000000002803d513_当我试图驯服ai做短剧（11）.mp4（第 1/3 次尝试）"),
    ("INFO", "  ✓ 20261020_6ad6beea000000002803d513_当我试图驯服ai做短剧（11）.mp4（35.9 MB）"),
    ("INFO", "  ↓ 20260927_6ab8c8e2000000001a03208e_当我试图驯服 ai 做短剧（10）.mp4（第 1/3 次尝试）"),
    ("WARNING", "  × 下载失败：ReadTimeout: 连接超时，正在重试"),
    ("INFO", "  ✓ 20260927_6ab8c8e2000000001a03208e_当我试图驯服 ai 做短剧（10）.mp4（32.7 MB）"),
]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1060, "height": 760},
                                     device_scale_factor=2)
        await page.route("**/index.html", lambda r: r.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body=(PROJECT / "gui" / "index.html").read_text(encoding="utf-8"),
        ))
        await page.goto((PROJECT / "gui" / "index.html").as_uri())
        await page.evaluate(STUB_JS)

        # 提供模拟数据源
        await page.evaluate("""
        (() => {
          const logs = %s;
          let sent = 0;
          let state = {stage:'downloading', percent: 78, detail: '下载进度 7/10', total: 10};
          window.__nextUpdates = (since) => {
            const slice = logs.slice(since);
            const out = {logs: slice, total: logs.length, state: state,
                         qr: null, running: true, finish: null};
            return out;
          };
          window.__demoLogs = logs;
        })();
        """ % __import__("json").dumps(
            [{"level": lv, "message": m, "ts": 1792470000 + i} for i, (lv, m) in enumerate(DEMO_LOGS)],
            ensure_ascii=False))

        # 填入示例值并触发初始化
        await page.evaluate("""
          document.getElementById('url').value =
            'https://www.xiaohongshu.com/user/profile/661392a7000000000700600f';
          document.getElementById('status').textContent = '运行中';
          document.getElementById('status').className = 'status-pill run';
          document.getElementById('progress-bar').style.width = '78%';
          document.getElementById('progress-pct').textContent = '78%';
          document.getElementById('progress-detail').textContent = '下载进度 7/10';
          window.dispatchEvent(new Event('pywebviewready'));
        """)
        await page.wait_for_timeout(2600)
        await page.screenshot(path=str(OUT))
        await browser.close()
    print("预览图已生成:", OUT)


asyncio.run(main())
