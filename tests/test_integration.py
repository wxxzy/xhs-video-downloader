# -*- coding: utf-8 -*-
"""
集成测试：用 Playwright page.route mock 掉小红书页面与接口，
在本地验证「详情页直链提取」「主页列表收集」「下载落盘与断点续传」三条核心链路。
运行：python tests/test_integration.py
"""
import asyncio
import json
import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xhs_video_downloader as x  # noqa: E402

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None


async def _launch_browser(p):
    """优先用 playwright 自带 chromium；没下载成功则依次尝试本机 Chrome / Edge"""
    last_err = None
    for channel in (None, "chrome", "msedge"):
        try:
            if channel:
                return await p.chromium.launch(
                    channel=channel, args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
            return await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"没有可用的浏览器内核：{last_err}")


def _detail_html(note: dict, note_id: str = "n1") -> str:
    state = {"note": {"noteDetailMap": {note_id: {"note": note}}}}
    return (
        "<html><body><script>window.__INITIAL_STATE__ = "
        + json.dumps(state, ensure_ascii=False)
        + ";</script></body></html>"
    )


def _video_note() -> dict:
    return {
        "type": "video",
        "title": "测试视频标题",
        "time": 1729234567000,
        "video": {
            "duration": 30000,
            "media": {
                "stream": {
                    "h264": [{"master_url": "https://cdn.example.com/720.mp4",
                              "backup_urls": [], "width": 720, "height": 1280}],
                    "h265": [{"master_url": "https://cdn.example.com/1080.mp4",
                              "backup_urls": ["https://cdn.example.com/1080b.mp4"],
                              "width": 1080, "height": 1920}],
                }
            },
        },
    }


PROFILE_HTML = """
<html><head><meta charset="UTF-8"></head><body>
  <section class="note-item">
    <a class="cover" href="/explore/domnote0000000000000001?xsec_token=domtk1">
      <div class="title">DOM兜底笔记</div>
    </a>
  </section>
  <section class="note-item">
    <a class="cover" href="/explore/domnote0000000000000002?xsec_token=domtk2">
      <div class="title">第二个DOM笔记</div>
    </a>
  </section>
  <script>
    // 模拟真实主页的 JS：进入页面后自动请求笔记列表接口
    fetch('/api/sns/web/v1/user_posted?num=30&cursor=&user_id=test').then(r => r.json());
  </script>
</body></html>
"""

USER_POSTED_PAYLOAD = {
    "code": 0,
    "success": True,
    "data": {
        "cursor": "end",
        "has_more": False,
        "items": [
            {
                "xsec_token": "apitk1",
                "note_card": {
                    "note_id": "apinote0000000000000001",
                    "type": "video",
                    "display_title": "接口视频笔记A",
                    "time": 1729234567000,
                    "video": {"media": {"stream": {"h264": [
                        {"master_url": "https://cdn.example.com/a.mp4",
                         "width": 1080, "height": 1920}
                    ]}}},
                },
            },
            {
                "xsec_token": "apitk2",
                "note_card": {
                    "note_id": "apinote0000000000000002",
                    "type": "normal",
                    "display_title": "接口图文笔记B",
                },
            },
        ],
    },
}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _LocalFileServer:
    def __init__(self, directory: Path):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(directory)))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipIf(async_playwright is None, "playwright 未安装")
class TestDetailExtraction(unittest.TestCase):
    """详情页提取：SSR 路径 + 接口嗅探兜底路径"""

    def test_video_note_stream(self):
        async def scenario():
            async with async_playwright() as p:
                browser = await _launch_browser(p)
                page = await browser.new_page()
                await page.route(
                    "**/explore/**",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html; charset=utf-8",
                        body=_detail_html(_video_note()),
                    ),
                )
                detail = await x.fetch_note_detail(
                    page, "n1", "https://www.xiaohongshu.com/explore/n1"
                )
                await browser.close()
                return detail

        detail = asyncio.run(scenario())
        self.assertIsNotNone(detail)
        self.assertEqual(detail["type"], "video")
        self.assertEqual(detail["title"], "测试视频标题")
        # h264 优先于更高清的 h265
        self.assertTrue(detail["video"]["master_url"].endswith("/720.mp4"))
        self.assertEqual(detail["video_meta"]["height"], 1280)
        self.assertEqual(detail["video_meta"]["duration_ms"], 30000)

    def test_image_note_skipped(self):
        async def scenario():
            async with async_playwright() as p:
                browser = await _launch_browser(p)
                page = await browser.new_page()
                await page.route(
                    "**/explore/**",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html; charset=utf-8",
                        body=_detail_html({"type": "normal", "title": "图文笔记"}),
                    ),
                )
                detail = await x.fetch_note_detail(
                    page, "img1", "https://www.xiaohongshu.com/explore/img1"
                )
                await browser.close()
                return detail

        detail = asyncio.run(scenario())
        self.assertEqual(detail["type"], "normal")
        self.assertIsNone(detail["video"])

    def test_empty_state_returns_none(self):
        async def scenario():
            async with async_playwright() as p:
                browser = await _launch_browser(p)
                page = await browser.new_page()
                await page.route(
                    "**/explore/**",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html; charset=utf-8",
                        body="<html><body>空页面</body></html>",
                    ),
                )
                detail = await x.fetch_note_detail(
                    page, "none", "https://www.xiaohongshu.com/explore/none"
                )
                await browser.close()
                return detail

        self.assertIsNone(asyncio.run(scenario()))

    # ---- 接口嗅探兜底路径（SSR 改版后的主路径）----

    def _api_fallback_html(self, api_path: str) -> str:
        return (
            "<html><head><meta charset='UTF-8'></head><body><script>"
            f"fetch('{api_path}', {{method: 'POST'}}).then(r => r.json());"
            "</script></body></html>"
        )

    def test_video_via_api_fallback(self):
        """页面无 __INITIAL_STATE__，视频信息改由接口返回"""
        async def scenario():
            async with async_playwright() as p:
                browser = await _launch_browser(p)
                page = await browser.new_page()
                await page.route(
                    "**/explore/**",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html; charset=utf-8",
                        body=self._api_fallback_html("/api/sns/web/v1/feed"),
                    ),
                )
                await page.route(
                    "**/api/**",
                    lambda route: route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({
                            "data": {
                                "note_id": "n1",
                                "type": "video",
                                "title": "接口兜底视频",
                                "time": 1729234567000,
                                "video": {"duration": 5000, "media": {"stream": {
                                    "h264": [{"master_url": "https://cdn.example.com/api.mp4",
                                              "width": 1080, "height": 1920}],
                                }}},
                            }
                        }, ensure_ascii=False),
                    ),
                )
                sniffer = x.ApiSniffer(page)
                detail = await x.fetch_note_detail(
                    page, "n1", "https://www.xiaohongshu.com/explore/n1", sniffer
                )
                await browser.close()
                return detail

        detail = asyncio.run(scenario())
        self.assertIsNotNone(detail)
        self.assertEqual(detail["type"], "video")
        self.assertEqual(detail["title"], "接口兜底视频")
        self.assertTrue(detail["video"]["master_url"].endswith("/api.mp4"))
        self.assertEqual(detail["video_meta"]["height"], 1920)

    def test_image_note_via_api_fallback(self):
        """接口返回图文笔记 → 正确识别为非视频"""
        async def scenario():
            async with async_playwright() as p:
                browser = await _launch_browser(p)
                page = await browser.new_page()
                await page.route(
                    "**/explore/**",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html; charset=utf-8",
                        body=self._api_fallback_html("/api/sns/web/v1/feed"),
                    ),
                )
                await page.route(
                    "**/api/**",
                    lambda route: route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({
                            "data": {"note_id": "img9", "type": "normal", "title": "接口图文"}
                        }, ensure_ascii=False),
                    ),
                )
                sniffer = x.ApiSniffer(page)
                detail = await x.fetch_note_detail(
                    page, "img9", "https://www.xiaohongshu.com/explore/img9", sniffer
                )
                await browser.close()
                return detail

        detail = asyncio.run(scenario())
        self.assertEqual(detail["type"], "normal")
        self.assertIsNone(detail["video"])

    def test_api_fallback_ignores_other_notes(self):
        """接口响应里只有别人的视频（相关推荐）→ 不能误取"""
        async def scenario():
            async with async_playwright() as p:
                browser = await _launch_browser(p)
                page = await browser.new_page()
                await page.route(
                    "**/explore/**",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html; charset=utf-8",
                        body=self._api_fallback_html("/api/sns/web/v1/feed"),
                    ),
                )
                await page.route(
                    "**/api/**",
                    lambda route: route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({
                            "data": {"items": [
                                {"note_card": {"note_id": "someoneelse", "type": "video",
                                               "title": "别人的视频",
                                               "video": {"media": {"stream": {"h264": [
                                                   {"master_url": "https://cdn.example.com/other.mp4",
                                                    "height": 1080}]}}}}},
                            ]}
                        }, ensure_ascii=False),
                    ),
                )
                sniffer = x.ApiSniffer(page)
                detail = await x.fetch_note_detail(
                    page, "n1", "https://www.xiaohongshu.com/explore/n1", sniffer
                )
                await browser.close()
                return detail

        self.assertIsNone(asyncio.run(scenario()))


@unittest.skipIf(async_playwright is None, "playwright 未安装")
class TestNoteLister(unittest.TestCase):
    """主页列表收集：接口拦截 + DOM 兜底合并"""

    def test_collect_merges_api_and_dom(self):
        async def scenario():
            async with async_playwright() as p:
                browser = await _launch_browser(p)
                page = await browser.new_page()
                await page.route(
                    "**/user/profile/**",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html; charset=utf-8", body=PROFILE_HTML
                    ),
                )
                await page.route(
                    "**/user_posted**",
                    lambda route: route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps(USER_POSTED_PAYLOAD),
                    ),
                )
                lister = x.NoteLister(page)
                notes = await lister.collect(
                    "0" * 24, profile_query="xsec_token=faketk",
                    scroll_pause=0.05, max_idle_rounds=2,
                )
                await browser.close()
                return notes

        notes = asyncio.run(scenario())
        by_id = {n["id"]: n for n in notes}
        # 接口来源：视频笔记带直链，图文笔记标记 normal
        self.assertIn("apinote0000000000000001", by_id)
        self.assertEqual(by_id["apinote0000000000000001"]["type"], "video")
        self.assertIsNotNone(by_id["apinote0000000000000001"]["video"])
        self.assertIn("xsec_token=apitk1", by_id["apinote0000000000000001"]["url"])
        self.assertEqual(by_id["apinote0000000000000002"]["type"], "normal")
        # DOM 兜底补充了接口里没有的笔记
        self.assertIn("domnote0000000000000001", by_id)
        self.assertEqual(by_id["domnote0000000000000001"]["type"], "unknown")
        self.assertIn("xsec_token=domtk1", by_id["domnote0000000000000001"]["url"])
        # 接口 2 篇 + DOM 兜底 2 篇 = 4 篇
        self.assertEqual(len(notes), 4)


class TestVideoDownloader(unittest.TestCase):
    """下载落盘、重试与断点续传（本地 HTTP 服务器，无需浏览器）"""

    def test_download_and_resume(self):
        async def scenario(tmp: Path):
            note = {
                "id": "n1",
                "title": "下载测试",
                "time": 1729234567000,
                "video": {"master_url": "http://127.0.0.1:PORT/video.mp4"},
                "video_meta": {"duration_ms": 1000, "width": 720, "height": 1280},
            }
            with _LocalFileServer(tmp) as srv:
                note["video"]["master_url"] = f"http://127.0.0.1:{srv.port}/video.mp4"
                out_dir = tmp / "out"
                out_dir.mkdir()
                state = x.DownloadState.load(out_dir / "state.json")
                dl = x.VideoDownloader(out_dir, cookies={}, concurrency=2, state=state)
                try:
                    results = await dl.download_all([note])
                finally:
                    await dl.close()
                files = list(out_dir.glob("*.mp4"))
                return results, files, state

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = tmp_path / "video.mp4"
            payload.write_bytes(b"0123456789" * 10000)
            results, files, state = asyncio.run(scenario(tmp_path))

            self.assertEqual(results["ok"], 1)
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_size, 100000)
            self.assertIn("n1", state.downloaded)
            self.assertTrue(files[0].name.startswith("20241018_n1_"))

            # 二次运行：命中缓存，不重复下载
            async def rerun(tmp: Path):
                note = {
                    "id": "n1", "title": "下载测试", "time": 1729234567000,
                    "video": {"master_url": "http://127.0.0.1:0/video.mp4"},
                    "video_meta": {},
                }
                out_dir = tmp / "out"
                state = x.DownloadState.load(out_dir / "state.json")
                dl = x.VideoDownloader(out_dir, cookies={}, concurrency=1, state=state)
                try:
                    return await dl.download_all([note])
                finally:
                    await dl.close()

            results2 = asyncio.run(rerun(tmp_path))
            self.assertEqual(results2["cached"], 1)
            self.assertEqual(results2["ok"], 0)

    def test_download_failure_recorded(self):
        async def scenario(tmp: Path):
            note = {
                "id": "bad1", "title": "坏链接", "time": None,
                "video": {"master_url": "http://127.0.0.1:1/missing.mp4"},
                "video_meta": {},
            }
            out_dir = tmp / "out"
            out_dir.mkdir()
            state = x.DownloadState.load(out_dir / "state.json")
            dl = x.VideoDownloader(out_dir, cookies={}, concurrency=1, state=state, retries=1)
            try:
                return await dl.download_all([note])
            finally:
                await dl.close()

        with tempfile.TemporaryDirectory() as tmp:
            results = asyncio.run(scenario(Path(tmp)))
            self.assertEqual(results["failed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
