#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xhs_video_downloader.py — 小红书账号视频批量下载工具

功能
----
1. 扫码登录小红书网页版，登录态保存在本地 profile 目录，之后自动复用；
2. 打开用户主页，自动滚动加载，收集该账号全部笔记（拦截 user_posted 接口 + DOM 兜底）；
3. 逐个打开视频笔记详情页，从 __INITIAL_STATE__ 提取无水印视频直链；
4. httpx 并发下载到 downloads/<用户ID>/ 目录；
5. state.json 记录下载/失败/跳过进度，支持断点续传，重复运行自动跳过。

合规边界（重要）
----------------
本工具仅限下载【你自己账号】或【已获得作者明确授权】的视频，
用于个人备份、学习与研究。请勿用于批量搬运、二次分发或任何商业用途。

环境要求
--------
Python 3.10+，依赖见 requirements.txt：
    pip install playwright httpx
    playwright install chromium

用法示例
--------
    # 下载指定账号的全部视频（首次运行会弹出浏览器窗口扫码登录）
    python xhs_video_downloader.py --user https://www.xiaohongshu.com/user/profile/67a4612d000000000d009085

    # 先只列出视频笔记，不下载
    python xhs_video_downloader.py --user 67a4612d000000000d009085 --list-only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
XHS_HOME = "https://www.xiaohongshu.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1366, "height": 900}
LOG = logging.getLogger("xhs")

COMPLIANCE_BANNER = (
    "==============================================================\n"
    " 小红书账号视频批量下载工具\n"
    " 合规边界：仅限下载【你自己账号】或【已获作者明确授权】的视频，\n"
    " 用于个人备份、学习与研究；请勿批量搬运、二次分发或商用。\n"
    "=============================================================="
)


# ---------------------------------------------------------------------------
# 纯工具函数
# ---------------------------------------------------------------------------
_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def sanitize_filename(name: str, maxlen: int = 60) -> str:
    """把笔记标题清理成安全的文件名"""
    name = (name or "").strip()
    name = _UNSAFE_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" ._")
    return name or "untitled"


def parse_user_id(text: str) -> str:
    """从个人主页链接或原始文本中解析 24 位用户 ID"""
    text = (text or "").strip()
    m = re.search(r"/user/profile/([0-9a-fA-F]{24})", text)
    if m:
        return m.group(1).lower()
    m = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{24})(?![0-9a-fA-F])", text)
    if m:
        return m.group(1).lower()
    raise ValueError(
        f"无法解析用户 ID：{text!r}\n"
        "请传入个人主页链接（如 https://www.xiaohongshu.com/user/profile/67a4612d...）"
        "或 24 位用户 ID"
    )


def pick_best_stream(stream: dict) -> Optional[dict]:
    """
    从 note.video.media.stream 中挑选最合适的视频流。

    优先 h264（兼容性最好），同编码取清晰度最高的；返回的 dict 中含
    master_url / backup_urls / width / height 等字段。
    兼容 snake_case（接口数据）与 camelCase（SSR 数据）两种命名。
    """
    if not isinstance(stream, dict):
        return None
    candidates: list[tuple[str, dict]] = []
    for codec in ("h264", "h265", "av1", "h266"):
        items = stream.get(codec)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("master_url") or item.get("masterUrl")
            if not url:
                backups = item.get("backup_urls") or item.get("backupUrls") or []
                url = backups[0] if backups else None
            if url:
                # 统一转成 snake_case 键，调用方只按 master_url / backup_urls 取值
                normalized = {
                    "master_url": url,
                    "backup_urls": item.get("backup_urls") or item.get("backupUrls") or [],
                    "width": item.get("width"),
                    "height": item.get("height"),
                    "size": item.get("size"),
                }
                candidates.append((codec, normalized))
    if not candidates:
        # 无已知编解码键名（如混淆键名 EF4 等）时，按清晰度从全部条目中挑选
        for codec, items in stream.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = item.get("master_url") or item.get("masterUrl")
                if not url:
                    backups = item.get("backup_urls") or item.get("backupUrls") or []
                    url = backups[0] if backups else None
                if url:
                    candidates.append((codec, {
                        "master_url": url,
                        "backup_urls": item.get("backup_urls") or item.get("backupUrls") or [],
                        "width": item.get("width"),
                        "height": item.get("height"),
                        "size": item.get("size"),
                    }))
        if not candidates:
            return None
        candidates.sort(key=lambda c: -(c[1].get("height") or 0))
        return candidates[0][1]
    codec_rank = {"h264": 0, "h265": 1, "av1": 2, "h266": 3}
    candidates.sort(key=lambda c: (codec_rank.get(c[0], 9), -(c[1].get("height") or 0)))
    return candidates[0][1]


def ms_to_date(ms: Optional[int]) -> str:
    """毫秒时间戳转 YYYYMMDD，失败时返回今天"""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y%m%d")
    except Exception:
        return datetime.now().strftime("%Y%m%d")


def find_in_json(obj: Any, predicate, _depth: int = 0) -> Optional[dict]:
    """在任意 JSON 结构里递归查找第一个满足条件的 dict（深度上限 12，防爆栈）"""
    if _depth > 12:
        return None
    if isinstance(obj, dict):
        try:
            if predicate(obj):
                return obj
        except Exception:
            return None
        for value in obj.values():
            found = find_in_json(value, predicate, _depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_in_json(value, predicate, _depth + 1)
            if found is not None:
                return found
    return None


class ApiSniffer:
    """
    捕获页面上所有 XHR/fetch 的 JSON 响应。
    不依赖具体接口路径（小红书改版时接口名会变），供详情提取与列表收集使用。
    """

    def __init__(self, page, log_urls: bool = False):
        self.page = page
        self.captured: list[tuple[str, Any]] = []
        self.log_urls = log_urls
        self._seen_paths: set[str] = set()
        page.on("response", self._on_response)

    async def _on_response(self, response) -> None:
        try:
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            if response.status != 200:
                return
            if "json" not in (response.headers.get("content-type") or "").lower():
                return
            payload = await response.json()
        except Exception:
            return
        self.captured.append((response.url, payload))
        if self.log_urls:
            path = response.url.split("?")[0]
            if path not in self._seen_paths:
                self._seen_paths.add(path)
                LOG.info("[API 巡检] %s", path[:130])

    def clear(self) -> None:
        self.captured.clear()

    def _payloads_with(self, note_id: str) -> list[Any]:
        """只保留包含目标 note_id 的响应（避开详情页「相关推荐」里别人的视频）"""
        result = []
        for _url, payload in reversed(self.captured):
            try:
                blob = json.dumps(payload, ensure_ascii=False)
            except Exception:
                continue
            if note_id and note_id in blob:
                result.append(payload)
        return result

    def find_video_for_note(self, note_id: str) -> Optional[tuple[dict, dict]]:
        """在包含该 note_id 的响应里找 (note, 最佳视频流)"""
        for payload in self._payloads_with(note_id):
            # 候选 1：note 对象自带匹配的 id（兼容 noteId / note_id / id）
            note = find_in_json(
                payload,
                lambda d: isinstance(d.get("video"), dict)
                and note_id in (d.get("note_id"), d.get("noteId"), d.get("id")),
            )
            # 候选 2：noteDetailMap 结构 {note_id: {"note": {...}}}
            if note is None:
                container = find_in_json(
                    payload,
                    lambda d: isinstance(d.get(note_id), dict)
                    and isinstance(d[note_id].get("note"), dict),
                )
                if container:
                    note = container[note_id]["note"]
            if note is None:
                continue
            stream = pick_best_stream(
                ((note.get("video") or {}).get("media") or {}).get("stream") or {}
            )
            if stream:
                return note, stream
        return None

    def find_note(self, note_id: str) -> Optional[dict]:
        """在包含该 note_id 的响应里找 note 对象本身"""
        for payload in self._payloads_with(note_id):
            note = find_in_json(
                payload,
                lambda d: note_id in (d.get("note_id"), d.get("noteId"), d.get("id"))
                and isinstance(d.get("type"), str),
            )
            if note:
                return note
        return None


def select_pending(notes: list[dict], downloaded: dict, skipped: Optional[dict] = None) -> list[dict]:
    """
    挑出需要打开详情页核验/下载的笔记。
    视频 + 类型待确认（unknown，来自 DOM 兜底）都要看；
    已下载的、已确认是图文的跳过；解析失败的下次会重试。
    """
    skipped = skipped or {}
    return [
        n for n in notes
        if n.get("type") in ("video", "unknown")
        and n["id"] not in (downloaded or {})
        and skipped.get(n["id"]) != "非视频笔记"
    ]


# ---------------------------------------------------------------------------
# 下载进度状态
# ---------------------------------------------------------------------------
@dataclass
class DownloadState:
    """断点续传状态：已下载 / 失败 / 跳过 + 元数据索引"""

    path: Path
    downloaded: dict = field(default_factory=dict)   # note_id -> 文件名
    failed: dict = field(default_factory=dict)       # note_id -> 失败原因
    skipped: dict = field(default_factory=dict)      # note_id -> 跳过原因
    index: list = field(default_factory=list)        # 元数据索引

    @classmethod
    def load(cls, path: Path) -> "DownloadState":
        state = cls(path=path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                state.downloaded = data.get("downloaded") or {}
                state.failed = data.get("failed") or {}
                state.skipped = data.get("skipped") or {}
                state.index = data.get("index") or []
            except Exception as exc:
                LOG.warning("读取 state.json 失败，将按空状态开始：%s", exc)
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "downloaded": self.downloaded,
                    "failed": self.failed,
                    "skipped": self.skipped,
                    "index": self.index,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# 浏览器会话（扫码登录 + 登录态持久化）
# ---------------------------------------------------------------------------
class BrowserSession:
    """
    基于 Playwright 持久化上下文：
    - 首次运行弹出浏览器窗口，用户扫码登录，登录态写在 profile_dir；
    - 之后运行直接复用登录态，可加 --headless。
    """

    def __init__(self, profile_dir: Path, headless: bool = False, channel: Optional[str] = None,
                 device_scale_factor: float = 1.0):
        self.profile_dir = profile_dir
        self.headless = headless
        self.channel = channel
        self.device_scale_factor = device_scale_factor
        self._pw = None
        self.context = None
        self.page = None

    async def __aenter__(self) -> "BrowserSession":
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SystemExit(
                "缺少 playwright 依赖，请先执行：\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            ) from exc
        self._pw = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            channel=self.channel,
            device_scale_factor=self.device_scale_factor,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context.set_default_timeout(30_000)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self.context:
                await self.context.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _logged_in(self) -> bool:
        """
        登录状态检测。
        注意：web_session Cookie 对匿名访客也会下发，不能作为判据。
        判据优先级：是否停留在 /login 页 > 登录弹窗是否可见 > user.loggedIn。
        （实测 2026-10：user.loggedIn 已变为响应式对象而非 boolean，
          因此以 userInfo.userId / webId 是否存在为主要判据）
        """
        try:
            if "/login" in (self.page.url or ""):
                return False
            flag = await self.page.evaluate(
                "() => { try { const s = window.__INITIAL_STATE__;"
                " if (!s || !s.user) return 'unknown';"
                " const u = s.user;"
                " const info = u.userInfo;"
                " const uid = info && (info.userId || info.user_id || info.id);"
                " if (uid) return 'yes';"
                " if (typeof u.loggedIn === 'boolean') return u.loggedIn ? 'yes' : 'no';"
                " const f = u.loggedIn && typeof u.loggedIn === 'object' ? JSON.parse(JSON.stringify(u.loggedIn)) : u.loggedIn;"
                " if (f === true || f === 'true') return 'yes';"
                " return 'unknown'; } catch(e) { return 'unknown'; } }"
            )
            if flag == "yes":
                return True
            if flag == "no":
                return False
            # 状态尚未水合：按页面特征判断
            locator = self.page.locator(".login-container")
            if await locator.count() > 0 and await locator.first.is_visible():
                return False
            # 登录页特征：出现二维码登录组件
            qr = self.page.locator(".login-container .qrcode, .qrcode img, canvas.code-box")
            if await qr.count() > 0 and await qr.first.is_visible():
                return False
            return True
        except Exception:
            return False

    async def save_qr_screenshot(self, path: Optional[Path]) -> Optional[str]:
        """
        截取登录二维码。path 为 None 时只返回 base64，不落盘（GUI 用）。
        返回 base64 PNG 字符串（失败返回 None）。
        """
        await self.page.wait_for_timeout(2500)
        png_bytes = None
        for selector in (".login-container .qrcode img", ".qrcode img",
                         "img[src*='qr']", ".login-container canvas", "canvas"):
            try:
                locator = self.page.locator(selector).first
                if await locator.count() > 0 and await locator.first.is_visible():
                    png_bytes = await locator.screenshot(timeout=5000)
                    break
            except Exception:
                continue
        if png_bytes is None:
            try:
                png_bytes = await self.page.screenshot()
            except Exception:
                return None
        if path is not None:
            path = Path(path)
            if str(path.parent):
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png_bytes)
            LOG.warning("二维码截图已保存：%s（请用小红书 App 扫描）", path.resolve())
        try:
            import base64
            return base64.b64encode(png_bytes).decode("ascii")
        except Exception:
            return None

    async def ensure_login(self, timeout_s: int = 300, qr_screenshot: Optional[Path] = None,
                           qr_callback=None, on_tick=None) -> None:
        """
        确保处于登录态。
        qr_callback: callable(base64_png) —— 二维码就绪时回调（GUI 内显示二维码）
        on_tick    : callable(waited_seconds) —— 每秒轮询回调，用于 GUI 显示等待进度
        """
        await self.page.goto(XHS_HOME, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)
        if await self._logged_in():
            LOG.info("已检测到保存的登录状态，直接使用")
            return
        qr_b64 = await self.save_qr_screenshot(qr_screenshot)
        if qr_callback and qr_b64:
            try:
                qr_callback(qr_b64)
                LOG.info("二维码已推送到界面，请用小红书 App 扫描")
            except Exception as exc:
                LOG.warning("二维码回调失败：%s", exc)
        LOG.warning("-" * 60)
        LOG.warning("请在 %d 秒内，用小红书 App 扫描二维码完成登录", timeout_s)
        LOG.warning("-" * 60)
        deadline = time.monotonic() + timeout_s
        waited = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            waited += 2
            if on_tick:
                try:
                    on_tick(waited)
                except Exception:
                    pass
            if await self._logged_in():
                LOG.info("登录成功，登录态已保存到 %s（下次运行自动复用）", self.profile_dir)
                return
        raise RuntimeError(f"登录超时：{timeout_s} 秒内未检测到登录成功")

    async def cookies_httpx(self) -> dict:
        """导出 xiaohongshu / xhscdn 域下的 Cookie，供 httpx 下载使用"""
        cookies = await self.context.cookies()
        result = {}
        for c in cookies:
            domain = (c.get("domain") or "").lstrip(".")
            if domain.endswith("xiaohongshu.com") or domain.endswith("xhscdn.com"):
                result[c["name"]] = c["value"]
        return result

    async def inject_cookies(self, cookie_str: str) -> int:
        """
        注入浏览器 Cookie（形如 'web_session=xxx; a1=yyy'）。
        适用于不便扫码的场景：从自己已登录的浏览器复制 Cookie 粘贴使用。
        返回成功注入的条数。
        """
        cookies = []
        for part in re.split(r"[;,]", cookie_str or ""):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            name, value = name.strip(), value.strip()
            if name:
                cookies.append(
                    {"name": name, "value": value, "domain": ".xiaohongshu.com", "path": "/"}
                )
        if not cookies:
            raise ValueError("Cookie 字符串解析失败：未找到 name=value 形式的条目")
        await self.context.add_cookies(cookies)
        return len(cookies)


# ---------------------------------------------------------------------------
# 笔记列表收集
# ---------------------------------------------------------------------------
class NoteLister:
    """
    打开用户主页，边滚动边拦截接口响应收集笔记；
    滚动结束后再做一次 DOM 兜底抓取，两条路径互补。
    """

    def __init__(self, page):
        self.page = page
        self.notes: dict[str, dict] = {}
        self._has_more = True
        page.on("response", self._handle_response)

    async def _handle_response(self, response) -> None:
        try:
            if response.status != 200:
                return
            # 不限定接口路径：小红书笔记列表接口改名过（user_posted 等），
            # 只要响应里有 items[].note_card 就认，避免改名后静默失效
            if "json" not in (response.headers.get("content-type") or "").lower():
                return
            payload = await response.json()
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        items = data.get("items") or []
        note_items = [
            it for it in items
            if isinstance(it, dict) and (it.get("note_card") or it.get("id"))
        ]
        if note_items:
            LOG.info(
                "接口命中 %s → %d 条笔记（has_more=%s）",
                response.url.split("?")[0][-55:], len(note_items), data.get("has_more"),
            )
        for item in note_items:
            card = item.get("note_card") or {}
            note_id = card.get("note_id") or card.get("id")
            if not note_id:
                continue
            token = card.get("xsec_token") or item.get("xsec_token")
            existing = self.notes.get(note_id) or {}
            token = token or existing.get("xsec_token")
            url = f"{XHS_HOME}/explore/{note_id}"
            if token:
                url += f"?xsec_token={token}&xsec_source=pc_user"
            stream = pick_best_stream(((card.get("video") or {}).get("media") or {}).get("stream") or {})
            self.notes[note_id] = {
                "id": note_id,
                "type": card.get("type") or existing.get("type") or "unknown",
                "title": (card.get("display_title") or existing.get("title") or "").strip(),
                "url": url,
                "xsec_token": token,
                "time": card.get("time") or existing.get("time"),
                "video": stream or existing.get("video"),
            }
        if data.get("has_more") is False:
            self._has_more = False

    async def _ssr_scrape(self) -> int:
        """
        SSR 数据提取（新版主页首屏不走 user_posted 接口，笔记内嵌在
        window.__INITIAL_STATE__.user.notes 里，结构为 notes[tabIndex][item]，
        item 含 id / noteCard（camelCase：xsecToken/noteId/type/displayTitle）。
        返回本次新增条数。响应式对象不能 JSON.stringify（循环引用），
        因此逐字段用基本类型拼接后带回。
        """
        try:
            raw = await self.page.evaluate(
                r"""() => {
                  try {
                    const s = window.__INITIAL_STATE__;
                    if (!s || !s.user) return '';
                    let notesRaw = s.user.notes;
                    if (notesRaw && notesRaw._value) notesRaw = notesRaw._value;
                    if (!Array.isArray(notesRaw)) return '';
                    const out = [];
                    for (const group of notesRaw) {
                      if (!Array.isArray(group)) continue;
                      for (const it of group) {
                        if (!it || typeof it !== 'object' || !it.id) continue;
                        const card = it.noteCard && typeof it.noteCard === 'object' ? it.noteCard : {};
                        const video = card.video && typeof card.video === 'object' ? card.video : null;
                        let streamSummary = '';
                        if (video) {
                          const media = video.media && typeof video.media === 'object' ? video.media : {};
                          const st = media.stream && typeof media.stream === 'object' ? media.stream : {};
                          const parts = [];
                          for (const codec of Object.keys(st)) {
                            const arr = st[codec];
                            if (!Array.isArray(arr)) continue;
                            for (const it2 of arr) {
                              if (!it2 || typeof it2 !== 'object') continue;
                              parts.push([
                                codec,
                                String(it2.masterUrl || it2.master_url || ''),
                                JSON.stringify(it2.backupUrls || it2.backup_urls || []),
                                String(it2.width || ''), String(it2.height || ''),
                                String(it2.size || '')
                              ].join('`'));
                            }
                          }
                          streamSummary = parts.join('~');
                        }
                        out.push([
                          String(it.id),
                          String(card.type || it.type || ''),
                          String(card.displayTitle || card.display_title || ''),
                          String(card.xsecToken || it.xsecToken || card.xsec_token || ''),
                          String(card.time || it.time || ''),
                          String(video ? (video.duration || 0) : ''),
                          streamSummary,
                        ].join('`'));
                      }
                    }
                    return out.join('\u0001');
                  } catch (e) { return 'ERR:' + e.message; }
                }"""
            )
        except Exception:
            return 0
        if not raw or raw.startswith("ERR:"):
            if raw:
                LOG.debug("SSR 提取失败：%s", raw[:120])
            return 0
        added = 0
        for line in raw.split("\x01"):
            if not line.strip():
                continue
            parts = line.split("`")
            if len(parts) < 5:
                continue
            note_id, ntype, title, token, ts = parts[0], parts[1], parts[2], parts[3], parts[4]
            duration = parts[5] if len(parts) > 5 else ""
            stream_summary = parts[6] if len(parts) > 6 else ""
            if not note_id or note_id in self.notes:
                continue
            stream = None
            if stream_summary:
                try:
                    parsed = []
                    for chunk in stream_summary.split("~"):
                        f = chunk.split("`")
                        if len(f) >= 6 and f[1]:
                            parsed.append({
                                "codec": f[0], "master_url": f[1],
                                "backup_urls": json.loads(f[2]) if f[2] else [],
                                "width": int(float(f[3])) if f[3] else None,
                                "height": int(float(f[4])) if f[4] else None,
                                "size": int(float(f[5])) if f[5] else None,
                            })
                    if parsed:
                        by_codec: dict[str, list] = {}
                        for p in parsed:
                            by_codec.setdefault(p["codec"], []).append(p)
                        stream = pick_best_stream(by_codec)
                except Exception:
                    stream = None
            url = f"{XHS_HOME}/explore/{note_id}"
            if token:
                url += f"?xsec_token={token}&xsec_source=pc_user"
            self.notes[note_id] = {
                "id": note_id,
                "type": ntype or "unknown",
                "title": title.strip(),
                "url": url,
                "xsec_token": token or None,
                "time": int(ts) if ts.isdigit() else None,
                "video": stream,
            }
            added += 1
        if added:
            LOG.info("SSR 数据提取到 %d 篇笔记（新版主页首屏内嵌）", added)
        return added

    async def _dom_scrape(self) -> None:
        """DOM 兜底：直接解析主页里的笔记链接（部分数据接口没拿到时靠它）"""
        try:
            items = await self.page.eval_on_selector_all(
                "section.note-item a[href*='/explore/']",
                "els => els.map(e => ({"
                "url: e.href, "
                "id: (e.getAttribute('href') || '').split('/').pop().split('?')[0]"
                "}))",
            )
        except Exception:
            return
        for item in items or []:
            note_id = item.get("id")
            if not note_id or note_id in self.notes:
                continue
            self.notes[note_id] = {
                "id": note_id,
                "type": "unknown",
                "title": "",
                "url": item.get("url"),
                "xsec_token": None,
                "time": None,
                "video": None,
            }

    async def collect(
        self,
        user_id: str,
        profile_query: str = "",
        max_notes: int = 0,
        scroll_pause: float = 1.2,
        max_idle_rounds: int = 4,
    ) -> list[dict]:
        url = f"{XHS_HOME}/user/profile/{user_id}"
        if profile_query:
            url += "?" + profile_query
        LOG.info("打开用户主页：%s", url)
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(3000)
        if "/login" in (self.page.url or ""):
            raise RuntimeError(
                "登录状态已失效（被重定向到登录页）。\n"
                "请删除 profile 目录（默认 ./.xhs_profile）后重新运行，再次扫码登录。"
            )
        idle = 0
        await self._ssr_scrape()   # 首屏 SSR 数据（新版主页不走列表接口）
        while self._has_more:
            if max_notes and len(self.notes) >= max_notes:
                LOG.info("已达到 --max 上限 %d，停止滚动", max_notes)
                break
            before = len(self.notes)
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.page.wait_for_timeout(int(scroll_pause * 1000))
            await self.page.evaluate("window.scrollBy(0, -300)")
            await self.page.wait_for_timeout(500)
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.page.wait_for_timeout(int(scroll_pause * 1000))
            await self._ssr_scrape()   # 翻页后 SSR 数据更新
            if len(self.notes) > before:
                idle = 0
                LOG.info("已加载 %d 篇笔记…", len(self.notes))
            else:
                idle += 1
                if idle >= max_idle_rounds:
                    break
        await self._dom_scrape()
        LOG.info("主页收集完成，共 %d 篇笔记", len(self.notes))
        if not self.notes:
            LOG.warning(
                "未收集到任何笔记。可能原因：账号已私密、触发风控验证、登录失效或页面结构变化。"
            )
        return list(self.notes.values())


# ---------------------------------------------------------------------------
# 笔记详情提取
# ---------------------------------------------------------------------------
def _build_detail(note: dict) -> dict:
    """把 note 对象整理成统一的详情结构（兼容 snake_case / camelCase）"""
    result: dict[str, Any] = {
        "type": note.get("type"),
        "title": (note.get("title") or note.get("display_title")
                  or note.get("displayTitle") or ""),
        "time": note.get("time"),
        "video": None,
        "video_meta": {},
    }
    if note.get("type") == "video":
        video = note.get("video") or {}
        media = video.get("media") or {}
        stream = pick_best_stream(media.get("stream") or {})
        if stream is None:
            LOG.warning("该视频未找到可下载流（video 字段：%s）", sorted(video.keys()))
        result["video"] = stream
        result["video_meta"] = {
            "duration_ms": video.get("duration"),
            "width": (stream or {}).get("width"),
            "height": (stream or {}).get("height"),
            "size": (stream or {}).get("size"),
        }
    return result


async def fetch_note_detail(
    page, note_id: str, note_url: str, sniffer: Optional[ApiSniffer] = None
) -> Optional[dict]:
    """
    打开笔记详情页提取信息。三条路径互补：
    1) window.__INITIAL_STATE__（SSR 内嵌数据，站点未改版时走这条）；
    2) ApiSniffer 捕获的接口响应（SSR 改版后走这条，不依赖具体接口名）；
    3) 识别 300031 风控重定向（"当前笔记暂时无法浏览"），自动带原参数重试。
    返回 None 表示两种方式都没拿到数据（可能无权限/已删除/被风控）。
    """
    if sniffer is not None:
        sniffer.clear()

    async def _goto(target_url: str) -> str:
        """打开页面并返回最终 URL（可能被重定向到 404/sec_ 风控页）"""
        await page.goto(target_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        return page.url or ""

    final_url = await _goto(note_url)

    # 300031 风控：详情页被重定向到 /404/sec_...?error_code=300031
    # 常见原因：xsec_token 缺失/过期，或短时间高频访问触发风控。
    if "error_code=300031" in final_url or "/404/sec_" in final_url:
        LOG.warning("详情页触发风控 300031：%s → 尝试刷新重试", note_id)
        # 解析 redirectPath 里的原始地址，直接再访问一次（风控常为瞬时）
        m = re.search(r"redirectPath=([^&]+)", final_url)
        if m:
            from urllib.parse import unquote
            original = unquote(m.group(1))
            await page.wait_for_timeout(2000)
            final_url = await _goto(original)
        if "error_code=300031" in final_url or "/404/sec_" in final_url:
            LOG.warning("重试仍被风控拦截：%s", note_id)
            return None

    # 路径 1：SSR 状态
    # 关键（实测 2026-10）：__INITIAL_STATE__ 是 Vue 响应式对象，
    # page.evaluate 返回整个对象会因循环引用序列化失败而得到 None。
    # 因此在页面内逐字段提取基本类型（\x01 分隔），再在 Python 侧解析。
    _SSR_DETAIL_JS = r"""() => {
      try {
        const s = window.__INITIAL_STATE__;
        if (!s || !s.note) return 'no-state';
        let dm = s.note.noteDetailMap;
        if (dm && dm._value) dm = dm._value;
        if (!dm) return 'no-dm';
        const ids = Object.keys(dm);
        if (!ids.length) return 'empty-dm';
        // 优先取目标 note_id，否则取第一个
        let entry = dm[ids[0]];
        for (const k of ids) { if (k === '__note_id__') { entry = dm[k]; break; } }
        if (!entry) return 'no-entry';
        let note = entry.note;
        if (note && note._value) note = note._value;
        if (!note || typeof note !== 'object') return 'no-note';
        const esc = (v) => String(v === null || v === undefined ? '' : v)
          .replace(/`/g, '\\u0060').replace(/\u0001/g, '');
        let out = 'OK`' + esc(note.type) + '`' + esc(note.title || note.displayTitle || note.display_title)
          + '`' + esc(note.time || '');
        const video = note.video;
        if (video && typeof video === 'object') {
          out += '`' + esc(video.duration || '');
          let media = video.media;
          if (media && media._value) media = media._value;
          if (media && typeof media === 'object') {
            let stream = media.stream;
            if (stream && stream._value) stream = stream._value;
            if (stream && typeof stream === 'object') {
              const parts = [];
              for (const codec of Object.keys(stream)) {
                const arr = stream[codec];
                if (!Array.isArray(arr)) continue;
                for (const it of arr) {
                  if (!it || typeof it !== 'object') continue;
                  parts.push([esc(codec), esc(it.masterUrl || it.master_url || ''),
                              esc(JSON.stringify(it.backupUrls || it.backup_urls || [])),
                              esc(it.width || ''), esc(it.height || ''), esc(it.size || '')].join('~'));
                }
              }
              out += '`' + parts.join('^');
            }
          }
        }
        return out;
      } catch (e) { return 'ERR`' + String(e.message || e); }
    }"""

    detail_raw = None
    for _ in range(10):
        try:
            detail_raw = await page.evaluate(_SSR_DETAIL_JS)
        except Exception:
            detail_raw = None
        if detail_raw and detail_raw.startswith("OK"):
            break
        await page.wait_for_timeout(600)
    if detail_raw and detail_raw.startswith("OK"):
        parts = detail_raw.split("`")
        # type`title`time[`duration`streams]
        ntype = parts[1] if len(parts) > 1 else ""
        title = parts[2] if len(parts) > 2 else ""
        ts = parts[3] if len(parts) > 3 else ""
        duration = parts[4] if len(parts) > 4 else ""
        stream_summary = parts[5] if len(parts) > 5 else ""
        stream = None
        if stream_summary:
            try:
                by_codec: dict[str, list] = {}
                for chunk in stream_summary.split("^"):
                    f = chunk.split("~")
                    if len(f) >= 6 and f[1]:
                        by_codec.setdefault(f[0], []).append({
                            "master_url": f[1],
                            "backup_urls": json.loads(f[2]) if f[2] else [],
                            "width": int(float(f[3])) if f[3] else None,
                            "height": int(float(f[4])) if f[4] else None,
                            "size": int(float(f[5])) if f[5] else None,
                        })
                stream = pick_best_stream(by_codec)
            except Exception:
                stream = None
        if ntype == "video" and stream:
            return {
                "type": "video",
                "title": title,
                "time": int(float(ts)) if ts.replace(".", "").isdigit() else None,
                "video": stream,
                "video_meta": {
                    "duration_ms": int(float(duration)) if duration.replace(".", "").isdigit() else None,
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "size": stream.get("size"),
                },
            }
        if ntype and ntype not in ("video",) and stream is None and ntype != "video":
            # 图文笔记：确认后不再重试
            return {
                "type": ntype,
                "title": title,
                "time": int(float(ts)) if ts.replace(".", "").isdigit() else None,
                "video": None,
                "video_meta": {},
            }
        # video 类型但流还没异步加载好 → 继续轮询（流数据晚于 state 就绪）

    # 路径 2：接口响应兜底
    if sniffer is not None:
        await page.wait_for_timeout(3000)
        found = sniffer.find_video_for_note(note_id)
        if found:
            note, stream = found
            return {
                "type": "video",
                "title": note.get("title") or note.get("display_title") or "",
                "time": note.get("time"),
                "video": stream,
                "video_meta": {
                    "duration_ms": (note.get("video") or {}).get("duration"),
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "size": stream.get("size"),
                },
            }
        note = sniffer.find_note(note_id)
        if note:
            # 拿到了 note 但没有视频流 → 图文笔记
            return {
                "type": note.get("type") or "normal",
                "title": note.get("title") or note.get("display_title") or "",
                "time": note.get("time"),
                "video": None,
                "video_meta": {},
            }

    LOG.warning("详情页未能提取到数据：%s（最终地址 %s）", note_url, page.url)
    if "error_code=300031" in (page.url or "") or "/404/sec_" in (page.url or ""):
        LOG.warning("提示：300031 通常意味着 xsec_token 失效或触发风控。"
                    "建议：① 重新从 App 分享主页链接获取新 token；② 调大 --delay；"
                    "③ 换网络环境。详见 README「常见问题」。")
    return None


# ---------------------------------------------------------------------------
# 视频下载
# ---------------------------------------------------------------------------
class VideoDownloader:
    """httpx 并发下载，带重试、临时文件和断点续传

    on_progress / cancel_event 为可选参数（GUI 用），CLI 不传时行为完全不变。
    """

    def __init__(self, out_dir: Path, cookies: dict, concurrency: int, state: DownloadState,
                 retries: int = 3, on_progress=None, cancel_event=None):
        self.out_dir = out_dir
        self.state = state
        self.retries = max(1, retries)
        self.on_progress = on_progress          # callable(dict) 可选
        self.cancel_event = cancel_event        # 有 .is_set() 的对象，可选
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Referer": XHS_HOME + "/",
                "Accept": "*/*",
            },
            cookies=cookies,
            timeout=httpx.Timeout(120.0, connect=30.0),
            follow_redirects=True,
        )

    def _cancelled(self) -> bool:
        try:
            return bool(self.cancel_event and self.cancel_event.is_set())
        except Exception:
            return False

    def _emit(self, **payload) -> None:
        if not self.on_progress:
            return
        try:
            self.on_progress(payload)
        except Exception:
            pass

    async def close(self) -> None:
        await self.client.aclose()

    async def download_note(self, note: dict) -> str:
        note_id = note["id"]
        if note_id in self.state.downloaded:
            return "cached"
        stream = note.get("video") or {}
        urls = [u for u in [stream.get("master_url"), *(stream.get("backup_urls") or [])] if u]
        if not urls:
            self.state.failed[note_id] = "无可用视频地址"
            self.state.save()
            return "failed"
        title = note.get("title") or ""
        filename = f"{ms_to_date(note.get('time'))}_{note_id}_{sanitize_filename(title)}.mp4"
        target = self.out_dir / filename
        if target.exists() and target.stat().st_size > 0:
            self.state.downloaded[note_id] = filename
            self.state.save()
            return "cached"

        if self._cancelled():
            return "failed"

        async with self.sem:
            last_err = "unknown"
            for attempt in range(1, self.retries + 1):
                url = urls[(attempt - 1) % len(urls)]
                tmp = target.with_suffix(".part")
                try:
                    LOG.info("  ↓ %s（第 %d/%d 次尝试）", filename, attempt, self.retries)
                    self._emit(event="file_start", note_id=note_id, title=title,
                               filename=filename, attempt=attempt, retries=self.retries)
                    total = 0
                    async with self.client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        with open(tmp, "wb") as fh:
                            async for chunk in resp.aiter_bytes(1024 * 256):
                                if self._cancelled():
                                    raise asyncio.CancelledError("用户取消")
                                fh.write(chunk)
                                total += len(chunk)
                    if total <= 0:
                        raise IOError("下载内容为空")
                    tmp.replace(target)
                    self.state.downloaded[note_id] = filename
                    self.state.index.append(
                        {
                            "note_id": note_id,
                            "title": title,
                            "file": filename,
                            "size_bytes": total,
                            "downloaded_at": datetime.now().isoformat(timespec="seconds"),
                            "duration_ms": (note.get("video_meta") or {}).get("duration_ms"),
                            "width": (note.get("video_meta") or {}).get("width"),
                            "height": (note.get("video_meta") or {}).get("height"),
                        }
                    )
                    self.state.save()
                    LOG.info("  ✓ %s（%.1f MB）", filename, total / 1024 / 1024)
                    self._emit(event="file_done", note_id=note_id, title=title,
                               filename=filename, size_bytes=total)
                    return "ok"
                except asyncio.CancelledError:
                    if tmp.exists():
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
                    self._emit(event="cancelled", note_id=note_id, title=title)
                    raise
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    LOG.warning("  × 下载失败：%s", last_err)
                    self._emit(event="file_failed", note_id=note_id, title=title, error=last_err)
                    if tmp.exists():
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
                    if attempt < self.retries:
                        await asyncio.sleep(min(8.0, 1.5 * attempt) + random.random())
            self.state.failed[note_id] = last_err
            self.state.save()
            return "failed"

    async def download_all(self, notes: list[dict]) -> dict:
        results = {"ok": 0, "cached": 0, "failed": 0}
        total = len(notes)
        done = 0
        tasks = [self.download_note(n) for n in notes]
        for coro in asyncio.as_completed(tasks):
            try:
                outcome = await coro
            except asyncio.CancelledError:
                results["cancelled"] = results.get("cancelled", 0) + 1
                continue
            results[outcome] = results.get(outcome, 0) + 1
            done += 1
            self._emit(event="file_count", done=done, total=total, outcome=outcome)
        return results


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def resolve_profile(text: str) -> tuple[str, str]:
    """
    解析输入：个人主页链接 / 24 位用户 ID / App 分享短链。
    返回 (user_id, profile_query)，query 中保留 xsec_token 等必要参数。
    """
    text = (text or "").strip()
    if text.startswith("http") and ("xhslink.com" in text or "xiaohongshu.com" not in text):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=15, headers={"User-Agent": USER_AGENT}
            ) as client:
                resp = await client.get(text)
                text = str(resp.url)
        except Exception as exc:
            LOG.warning("短链跳转解析失败（%s），将按原始输入继续解析", exc)
    user_id = parse_user_id(text)
    keep = {}
    query = urlparse(text).query
    if query:
        qs = parse_qs(query)
        for key in ("xsec_token", "xsec_source"):
            if qs.get(key):
                keep[key] = qs[key][0]
    return user_id, urlencode(keep)


def print_video_list(videos: list[dict]) -> None:
    print(f"\n共 {len(videos)} 个视频笔记：\n")
    for i, note in enumerate(videos, 1):
        print(f"  {i:3d}. {note['id']}  {(note.get('title') or '')[:50]}")
    print()


# ---------------------------------------------------------------------------
# 任务配置与可复用执行核心（CLI 与 GUI 共用）
# ---------------------------------------------------------------------------
@dataclass
class JobConfig:
    """一次下载任务的全部参数。CLI 由 argparse 构造，GUI 由界面构造。"""

    user: str = ""                        # 主页链接 / 用户 ID / 短链；单篇模式下为笔记链接
    out: str = "./downloads"
    profile_dir: str = "./.xhs_profile"
    headless: bool = False
    channel: Optional[str] = None
    list_only: bool = False
    max_notes: int = 0
    delay: float = 1.5
    concurrency: int = 2
    retries: int = 3
    scroll_pause: float = 1.2
    login_timeout: int = 300
    qr_screenshot: Optional[str] = None
    device_scale_factor: float = 1.0
    cookie: Optional[str] = None
    cookie_file: Optional[str] = None
    dump_api: bool = False
    # 单篇模式：跳过主页收集，直接解析该笔记
    single_note: Optional[str] = None

    @classmethod
    def from_args(cls, args) -> "JobConfig":
        return cls(
            user=getattr(args, "user", "") or "",
            out=getattr(args, "out", "./downloads"),
            profile_dir=getattr(args, "profile_dir", "./.xhs_profile"),
            headless=getattr(args, "headless", False),
            channel=getattr(args, "channel", None),
            list_only=getattr(args, "list_only", False),
            max_notes=getattr(args, "max", 0) or 0,
            delay=getattr(args, "delay", 1.5),
            concurrency=getattr(args, "concurrency", 2),
            retries=getattr(args, "retries", 3),
            scroll_pause=getattr(args, "scroll_pause", 1.2),
            login_timeout=getattr(args, "login_timeout", 300),
            qr_screenshot=getattr(args, "qr_screenshot", None),
            device_scale_factor=getattr(args, "device_scale_factor", 1.0),
            cookie=getattr(args, "cookie", None),
            cookie_file=getattr(args, "cookie_file", None),
            dump_api=getattr(args, "dump_api", False),
        )


@dataclass
class JobResult:
    ok: int = 0
    cached: int = 0
    failed: int = 0
    cancelled: int = 0
    out_dir: Optional[str] = None
    error: Optional[str] = None

    @property
    def exit_code(self) -> int:
        if self.error:
            return 1
        return 0 if self.failed == 0 else 2


async def run_job(
    cfg: JobConfig,
    on_log=None,
    on_progress=None,
    cancel_event=None,
    qr_callback=None,
    login_only: bool = False,
) -> JobResult:
    """
    可复用的任务执行核心（CLI / GUI 共用）。

    参数
    ----
    cfg         : JobConfig
    on_log      : callable(level: str, message: str) 日志回调，可选
    on_progress : callable(dict) 进度回调，可选
    cancel_event: 有 is_set() 的对象（如 threading.Event），可选
    qr_callback : callable(base64_png: str) 二维码就绪回调（GUI 显示二维码），可选
    login_only  : True 时仅完成登录后返回，不收集笔记
    """
    def emit_log(level: str, message: str) -> None:
        if on_log:
            try:
                on_log(level, message)
            except Exception:
                pass

    def emit_progress(**payload) -> None:
        if on_progress:
            try:
                on_progress(payload)
            except Exception:
                pass

    def cancelled() -> bool:
        try:
            return bool(cancel_event and cancel_event.is_set())
        except Exception:
            return False

    result = JobResult()

    # 单篇模式
    if cfg.single_note:
        return await _run_single(cfg, emit_log, emit_progress, cancelled, cancel_event,
                                 qr_callback=qr_callback)

    # 仅登录模式：不需要用户 ID，登录后即返回
    if login_only and not cfg.user:
        async with BrowserSession(
            Path(cfg.profile_dir), headless=cfg.headless, channel=cfg.channel,
            device_scale_factor=cfg.device_scale_factor,
        ) as session:
            if cfg.cookie:
                await session.inject_cookies(cfg.cookie)
            qr_path = Path(cfg.qr_screenshot) if cfg.qr_screenshot else None
            await session.ensure_login(cfg.login_timeout, qr_screenshot=qr_path,
                                       qr_callback=None if cfg.headless else qr_callback)
            LOG.info("登录已完成（仅登录模式）")
            emit_progress(stage="login_only_done")
        return result

    user_id, profile_query = await resolve_profile(cfg.user)
    out_dir = Path(cfg.out).expanduser().resolve() / user_id
    out_dir.mkdir(parents=True, exist_ok=True)
    state = DownloadState.load(out_dir / "state.json")
    result.out_dir = str(out_dir)
    LOG.info("用户 ID：%s", user_id)
    LOG.info("输出目录：%s", out_dir)
    emit_progress(stage="init", user_id=user_id, out_dir=str(out_dir))

    async with BrowserSession(
        Path(cfg.profile_dir), headless=cfg.headless, channel=cfg.channel,
        device_scale_factor=cfg.device_scale_factor,
    ) as session:
        if cfg.cookie_file:
            cookie_str = Path(cfg.cookie_file).read_text(encoding="utf-8").strip()
        elif cfg.cookie:
            cookie_str = cfg.cookie
        else:
            cookie_str = ""
        if cookie_str:
            count = await session.inject_cookies(cookie_str)
            LOG.info("已注入 %d 条 Cookie", count)

        qr_path = Path(cfg.qr_screenshot) if cfg.qr_screenshot else None
        if qr_callback and not cfg.headless:
            # GUI 模式：二维码就绪后回调给前端显示
            await session.ensure_login(cfg.login_timeout, qr_screenshot=qr_path,
                                       qr_callback=qr_callback)
        else:
            await session.ensure_login(cfg.login_timeout, qr_screenshot=qr_path)
        emit_progress(stage="logged_in")
        if login_only:
            LOG.info("登录已完成（仅登录模式）")
            emit_progress(stage="login_only_done")
            return result

        if cancelled():
            result.cancelled += 1
            emit_progress(stage="cancelled")
            return result

        # 1. 收集全部笔记
        sniffer = ApiSniffer(session.page, log_urls=cfg.dump_api)
        lister = NoteLister(session.page)
        notes = await lister.collect(
            user_id,
            profile_query=profile_query,
            max_notes=cfg.max_notes,
            scroll_pause=cfg.scroll_pause,
        )
        videos = [n for n in notes if n.get("type") == "video"]
        unknowns = [n for n in notes if n.get("type") == "unknown"]
        confirmed_other = [n for n in notes if n.get("type") not in ("video", "unknown")]
        LOG.info(
            "笔记统计：视频 %d 篇，待确认 %d 篇（将逐个打开详情页核验），已确认图文 %d 篇",
            len(videos), len(unknowns), len(confirmed_other),
        )
        emit_progress(stage="collected", total=len(videos) + len(unknowns))

        if cfg.list_only:
            listing = videos + unknowns
            print_video_list(listing)
            # GUI 场景下 print 不可见，逐条走日志推送
            if on_log or on_progress:
                LOG.info("视频清单（共 %d 篇）：", len(listing))
                for i, n in enumerate(listing, 1):
                    LOG.info("  %2d. %s  %s", i, n["id"], (n.get("title") or "")[:50])
            if on_progress:
                emit_progress(stage="list_done", videos=[
                    {"id": n["id"], "title": n.get("title") or ""} for n in listing
                ])
            return result

        # 2. 逐个打开视频/待确认笔记详情页，提取无水印直链
        pending = select_pending(notes, state.downloaded, state.skipped)
        LOG.info("待处理 %d 篇（已下载的自动跳过）", len(pending))
        targets: list[dict] = []
        for i, note in enumerate(pending, 1):
            if cancelled():
                LOG.warning("已取消，停止解析剩余笔记")
                result.cancelled += 1
                break
            note_id = note["id"]
            emit_progress(stage="resolving", index=i, total=len(pending), note_id=note_id)
            if note.get("video"):
                targets.append(note)
                LOG.info("[%d/%d] %s 列表接口已含直链，直接下载", i, len(pending), note_id)
                continue
            LOG.info("[%d/%d] 打开详情页 %s", i, len(pending), note_id)
            try:
                detail = await fetch_note_detail(session.page, note["id"], note["url"], sniffer)
            except Exception as exc:
                LOG.warning("详情页打开失败：%s", exc)
                detail = None
            if detail and detail.get("type") == "video" and detail.get("video"):
                targets.append(
                    {
                        **note,
                        "title": note.get("title") or detail.get("title"),
                        "time": detail.get("time") or note.get("time"),
                        "video": detail["video"],
                        "video_meta": detail.get("video_meta"),
                    }
                )
                LOG.info("    ✓ %s", (detail.get("title") or "")[:40])
            elif detail and detail.get("type") != "video":
                state.skipped[note_id] = "非视频笔记"
                state.save()
            else:
                state.failed[note_id] = "详情解析失败（可能已删除、被风控 300031 或暂无权限）"
                state.save()
            if cfg.delay > 0:
                await asyncio.sleep(cfg.delay)

        # 3. 下载
        LOG.info("获取到 %d 个视频直链，开始下载（并发 %d）", len(targets), cfg.concurrency)
        emit_progress(stage="downloading", total=len(targets))
        cookies = await session.cookies_httpx()
        downloader = VideoDownloader(
            out_dir,
            cookies=cookies,
            concurrency=cfg.concurrency,
            state=state,
            retries=cfg.retries,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        try:
            results = await downloader.download_all(targets)
        except asyncio.CancelledError:
            results = {"ok": 0, "cached": 0, "failed": 0, "cancelled": len(targets)}
        finally:
            await downloader.close()

    result.ok = results.get("ok", 0)
    result.cached = results.get("cached", 0)
    result.failed = results.get("failed", 0)
    result.cancelled += results.get("cancelled", 0)
    if cancelled():
        result.cancelled = max(result.cancelled, 1)
    LOG.info("=" * 55)
    if result.cancelled:
        LOG.warning("任务已取消：新下载 %d，已存在跳过 %d，失败 %d",
                    result.ok, result.cached, result.failed)
    else:
        LOG.info(
            "全部完成：新下载 %d，已存在跳过 %d，失败 %d",
            result.ok, result.cached, result.failed,
        )
    LOG.info("失败与跳过明细见 %s", out_dir / "state.json")
    emit_progress(stage="cancelled" if result.cancelled else "finished",
                  ok=result.ok, cached=result.cached, failed=result.failed)
    return result


async def _run_single(cfg: JobConfig, emit_log, emit_progress, cancelled, cancel_event,
                      qr_callback=None) -> JobResult:
    """单篇模式：直接从笔记链接提取直链并下载"""
    result = JobResult()
    note_url = cfg.single_note or ""
    m = re.search(r"/explore/([0-9a-fA-F]{24})", note_url)
    if not m:
        m = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{24})(?![0-9a-fA-F])", note_url)
    if not m:
        result.error = f"无法从链接解析笔记 ID：{note_url}"
        LOG.error(result.error)
        return result
    note_id = m.group(1).lower()
    out_dir = Path(cfg.out).expanduser().resolve() / "single"
    out_dir.mkdir(parents=True, exist_ok=True)
    result.out_dir = str(out_dir)
    state = DownloadState.load(out_dir / "state.json")
    LOG.info("单篇模式，笔记 ID：%s", note_id)

    async with BrowserSession(
        Path(cfg.profile_dir), headless=cfg.headless, channel=cfg.channel,
        device_scale_factor=cfg.device_scale_factor,
    ) as session:
        if cfg.cookie:
            count = await session.inject_cookies(cfg.cookie)
            LOG.info("已注入 %d 条 Cookie", count)
        qr_path = Path(cfg.qr_screenshot) if cfg.qr_screenshot else None
        await session.ensure_login(cfg.login_timeout, qr_screenshot=qr_path,
                                   qr_callback=qr_callback)
        emit_progress(stage="logged_in")

        sniffer = ApiSniffer(session.page)
        LOG.info("打开笔记详情页：%s", note_id)
        emit_progress(stage="resolving", index=1, total=1, note_id=note_id)
        detail = await fetch_note_detail(session.page, note_id, note_url, sniffer)
        if not detail or detail.get("type") != "video" or not detail.get("video"):
            result.error = "未能提取到视频直链（可能是图文笔记、已被删除或暂无权限）"
            LOG.error(result.error)
            return result

        note = {
            "id": note_id,
            "title": detail.get("title") or "",
            "time": detail.get("time"),
            "video": detail["video"],
            "video_meta": detail.get("video_meta"),
        }
        size = (detail["video"].get("size") or 0) / 1024 / 1024
        LOG.info("解析成功：%s（%.1f MB）", note["title"][:40], size)
        cookies = await session.cookies_httpx()
        emit_progress(stage="downloading", total=1)
        downloader = VideoDownloader(
            out_dir, cookies=cookies, concurrency=1, state=state,
            retries=cfg.retries, on_progress=None, cancel_event=cancel_event,
        )
        try:
            res = await downloader.download_all([note])
        finally:
            await downloader.close()
        result.ok = res.get("ok", 0)
        result.cached = res.get("cached", 0)
        result.failed = res.get("failed", 0)

    emit_progress(stage="finished", ok=result.ok, cached=result.cached, failed=result.failed)
    return result


async def run(args) -> int:
    """CLI 入口：把 argparse 结果转成 JobConfig 后调用 run_job"""
    result = await run_job(JobConfig.from_args(args))
    if result.error:
        LOG.error("运行失败：%s", result.error)
    return result.exit_code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xhs_video_downloader",
        description="小红书账号视频批量下载工具（仅限个人备份或已获授权的内容）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--user", required=True, help="个人主页链接 / 24 位用户 ID / App 分享短链")
    parser.add_argument("--out", default="./downloads", help="下载目录")
    parser.add_argument("--profile-dir", default="./.xhs_profile", help="浏览器用户数据目录（保存登录态）")
    parser.add_argument("--headless", action="store_true", help="无头模式运行（需已保存登录态）")
    parser.add_argument("--channel", default=None, help="使用本机 Chrome 浏览器时传 chrome")
    parser.add_argument("--list-only", action="store_true", help="只列出视频笔记，不下载")
    parser.add_argument("--max", type=int, default=0, help="最多处理多少篇笔记（0 = 全部，调试用）")
    parser.add_argument("--delay", type=float, default=1.5, help="每篇详情页之间的间隔秒数（防风控）")
    parser.add_argument("--concurrency", type=int, default=2, help="下载并发数")
    parser.add_argument("--retries", type=int, default=3, help="单文件下载重试次数")
    parser.add_argument("--scroll-pause", type=float, default=1.2, help="主页滚动停顿秒数")
    parser.add_argument("--login-timeout", type=int, default=300, help="扫码登录等待秒数")
    parser.add_argument("--qr-screenshot", default=None, help="登录二维码截图保存路径（无头模式推荐，保存后可发给用户扫描）")
    parser.add_argument("--device-scale-factor", type=float, default=1.0,
                        help="浏览器渲染缩放，二维码截图不清晰时设为 2")
    parser.add_argument("--cookie", default=None,
                        help="直接注入浏览器 Cookie（如 'web_session=xxx; a1=yyy'），跳过扫码")
    parser.add_argument("--cookie-file", default=None,
                        help="从文件读取 Cookie 字符串（内容同上，适合超长 Cookie）")
    parser.add_argument("--dump-api", action="store_true",
                        help="巡检并打印页面发起的所有 API 请求路径（小红书改版时用于诊断）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


class _CallbackLogHandler(logging.Handler):
    """把日志转发到回调（供 GUI 实时显示）。"""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(record.levelname, self.format(record))
        except Exception:
            pass


def attach_log_callback(callback) -> logging.Handler:
    """挂载一个日志回调处理器，返回该 handler（便于后续移除）。"""
    handler = _CallbackLogHandler(callback)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
    return handler


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    print(COMPLIANCE_BANNER)
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        LOG.warning("用户中断，进度已保存到 state.json")
        code = 130
    except Exception as exc:
        LOG.error("运行失败：%s", exc)
        if args.verbose:
            raise
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
