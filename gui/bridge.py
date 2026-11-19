# -*- coding: utf-8 -*-
"""
bridge.py — JS ↔ Python 桥接层

架构说明（重要）
----------------
pywebview 的 `window.evaluate_js()` **必须从 UI 主线程调用**，
在后台工作线程里调用会挂起（实测：任务本身跑完，但推送时死锁）。

因此本项目采用**前端轮询**模式：

    后台工作线程 ──写队列──► 线程安全缓冲区 ◄──轮询读取── 前端 (setInterval)
         │                                                    │
         └─ asyncio.run(run_job(...))                     get_updates()

优点：
- 完全不依赖 evaluate_js，规避跨线程 UI 调用
- 日志/进度天然有序，断线可重放
- 前端刷新频率可调，不阻塞后台任务
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import xhs_video_downloader as core  # noqa: E402


class GuiBridge:
    """暴露给前端的 API 集合"""

    def __init__(self, window_holder: dict | None = None):
        # window_holder: {"window": <webview.Window>}，仅用于目录选择对话框
        self._holder = window_holder or {}
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._running = False
        self._lock = threading.Lock()

        # —— 前端轮询缓冲区（线程安全）——
        self._buf_lock = threading.Lock()
        self._logs: list[dict] = []      # [{level, message, ts}]
        self._state: dict = {"stage": "idle", "percent": 0, "detail": "等待开始"}
        self._qr: Optional[str] = None   # base64 PNG
        self._finish: Optional[dict] = None
        # 自增任务序号：前端靠它判断"是否是新一次结束"。
        # 不能用 finish 对象本身做比较 —— pywebview 每次轮询都会把 dict
        # 重新序列化成全新的 JS 对象，而 JS 的对象比较是按引用，
        # 新对象永不等于旧对象，会导致 handleFinish 每轮都被调用（提示闪烁）。
        self._run_id: int = 0
        self._finish_run_id: int = 0     # 该 finish 属于哪一次任务

    # ------------------------------------------------------------------
    # 内部：写缓冲区（后台线程安全调用，绝不触碰 UI）
    # ------------------------------------------------------------------
    def _get_window(self):
        return self._holder.get("window")

    def _log(self, level: str, message: str) -> None:
        with self._buf_lock:
            self._logs.append({"level": level, "message": message, "ts": time.time()})
            if len(self._logs) > 3000:
                self._logs = self._logs[-1500:]

    def _set_state(self, **kw) -> None:
        with self._buf_lock:
            self._state.update(kw)

    def _progress(self, payload: dict) -> None:
        """把核心的 progress 事件翻译成前端可读的进度状态"""
        stage = payload.get("stage")
        patch: dict[str, Any] = {}
        if stage:
            patch["stage"] = stage
        cur = self._state.get("percent", 0)

        if stage == "init":
            patch["percent"] = 2
            patch["detail"] = f"账号 {payload.get('user_id', '')}"
        elif stage == "logged_in":
            patch["percent"] = 4
            patch["detail"] = "登录成功，开始收集笔记"
        elif stage == "collected":
            patch["percent"] = 8
            patch["total"] = payload.get("total", 0)
            patch["detail"] = f"收集到 {payload.get('total', 0)} 篇，准备解析直链"
        elif stage == "resolving":
            i, total = payload.get("index", 0), payload.get("total", 1) or 1
            patch["percent"] = 8 + int(42 * i / total)
            patch["detail"] = f"解析直链 {i}/{total}"
        elif stage == "downloading":
            patch["percent"] = 50
            patch["total"] = payload.get("total", 0)
            patch["detail"] = f"开始下载 {payload.get('total', 0)} 个视频"
        elif stage == "file_start":
            patch["detail"] = f"下载中：{(payload.get('title') or '')[:26]}"
        elif stage == "file_done":
            mb = (payload.get("size_bytes") or 0) / 1024 / 1024
            patch["detail"] = f"已完成：{(payload.get('title') or '')[:22]}（{mb:.1f}MB）"
        elif stage == "file_failed":
            patch["detail"] = f"失败重试中：{(payload.get('title') or '')[:22]}"
        elif stage == "file_count":
            done, total = payload.get("done", 0), payload.get("total", 1) or 1
            patch["percent"] = 50 + int(50 * done / total)
            patch["detail"] = f"下载进度 {done}/{total}"
        elif stage == "list_done":
            n = len(payload.get("videos") or [])
            patch["percent"] = 100
            patch["detail"] = f"清单已列出，共 {n} 篇视频"
        elif stage == "login_only_done":
            patch["percent"] = 100
            patch["detail"] = "登录成功，登录态已保存"
        elif stage == "cancelled":
            patch["detail"] = "任务已取消"
        elif stage == "finished":
            patch["percent"] = 100
            ok, cached, failed = (payload.get("ok", 0), payload.get("cached", 0),
                                  payload.get("failed", 0))
            patch["detail"] = f"完成：新下载 {ok}，已存在 {cached}，失败 {failed}"
        else:
            patch["percent"] = cur
        self._set_state(**patch)

    # ------------------------------------------------------------------
    # 前端轮询接口
    # ------------------------------------------------------------------
    def get_updates(self, since: int = 0) -> dict:
        """
        前端定时调用，增量拉取日志与状态。
        since: 前端已消费到的日志条数
        返回 {logs, total, state, qr, running, finish, finish_run_id}
        finish_run_id 是数字，前端用 !== 比较即可正确去重。
        """
        with self._buf_lock:
            total = len(self._logs)
            start = max(0, min(since, total))
            logs = self._logs[start:]
            return {
                "logs": logs,
                "total": total,
                "state": dict(self._state),
                "qr": self._qr,
                "running": self._running,
                "finish": self._finish,
                "finish_run_id": self._finish_run_id,
            }

    # ------------------------------------------------------------------
    # 上层业务接口
    # ------------------------------------------------------------------
    def get_env(self) -> dict:
        """返回运行环境信息（供界面初始化显示）"""
        from platform_utils import (describe_platform, find_browser,
                                    playwright_browsers_dir)
        info = {
            "project_dir": str(PROJECT_DIR),
            "python": sys.version.split()[0],
            "platform": describe_platform(),
            "default_out": str(PROJECT_DIR / "downloads"),
            "profile_dir": str(PROJECT_DIR / ".xhs_profile"),
            "logged_in_hint": (PROJECT_DIR / ".xhs_profile" / "Default").exists(),
            "running": self._running,
        }
        browser, channel = find_browser()
        info["browser_path"] = str(browser) if browser else None
        info["channel"] = channel
        info["chrome"] = channel == "chrome"
        info["edge"] = channel == "msedge"
        # 无本机浏览器时，看是否装了 playwright 内核可兜底
        pw_dir = playwright_browsers_dir()
        info["playwright_bundled"] = bool(pw_dir and any(pw_dir.glob("chromium*")))
        return info

    def open_downloads(self, path: str = "") -> dict:
        """在系统文件管理器中打开目录（跨平台）"""
        from platform_utils import open_in_file_manager
        target = path or str(PROJECT_DIR / "downloads")
        ok, err = open_in_file_manager(target)
        return {"ok": ok, "error": err}

    def pick_folder(self) -> Optional[str]:
        """打开系统目录选择对话框。注意：由前端主线程调用，安全。"""
        w = self._get_window()
        if w is None:
            return None
        try:
            result = w.create_file_dialog(dialog_type=20, directory=str(PROJECT_DIR))
            if result:
                return result if isinstance(result, str) else str(result[0])
        except Exception as exc:
            self._log("WARNING", f"选择目录失败：{exc}")
        return None

    def open_folder(self, path: str) -> dict:
        """在系统文件管理器中打开目录（跨平台：explorer / open / xdg-open）"""
        from platform_utils import open_in_file_manager
        ok, err = open_in_file_manager(path)
        return {"ok": ok, "error": err}

    def start(self, options: dict) -> dict:
        """启动下载任务"""
        with self._lock:
            if self._running:
                return {"ok": False, "error": "已有任务在运行"}
            self._running = True
        self._reset()
        self._thread = threading.Thread(target=self._worker, args=(options,), daemon=True)
        self._thread.start()
        return {"ok": True}

    def login_only(self, options: dict) -> dict:
        """仅登录（界面内显示二维码），不下载"""
        opts = dict(options or {})
        opts["_login_only"] = True
        return self.start(opts)

    def cancel(self) -> dict:
        """请求取消当前任务"""
        if not self._running:
            return {"ok": False, "error": "当前没有运行中的任务"}
        self._cancel.set()
        self._log("WARNING", "已发送取消请求，正在停止…")
        return {"ok": True}

    def check_login(self) -> dict:
        """检查是否已有登录态（读 profile 目录是否有 Cookie）"""
        ck = PROJECT_DIR / ".xhs_profile" / "Default" / "Network" / "Cookies"
        ok = ck.exists() and ck.stat().st_size > 0
        return {"logged_in": ok}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _reset(self) -> None:
        with self._buf_lock:
            self._logs = []
            self._state = {"stage": "starting", "percent": 0, "detail": "正在启动…"}
            self._qr = None
            self._finish = None
            # 新任务开始：递增序号，并把 finish_run_id 清零，
            # 这样前端在本次任务结束前不会误触发上次的完成处理。
            self._run_id += 1
            self._finish_run_id = 0
        self._cancel.clear()

    def _worker(self, options: dict) -> None:
        login_only = bool(options.pop("_login_only", False))
        try:
            cfg = self._build_config(options)
        except Exception as exc:
            self._log("ERROR", f"参数错误：{exc}")
            self._finalize({"error": str(exc)})
            return

        try:
            result = self._run_async(cfg, login_only=login_only)
            payload = {
                "ok": result.ok, "cached": result.cached,
                "failed": result.failed, "cancelled": result.cancelled,
                "out_dir": result.out_dir, "error": result.error,
            }
        except Exception as exc:
            self._log("ERROR", f"任务异常：{type(exc).__name__}: {exc}")
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        self._finalize(payload)

    def _run_async(self, cfg: core.JobConfig, login_only: bool = False) -> core.JobResult:
        import asyncio
        import logging

        # GUI 路径不走 main()，必须显式配置日志级别，
        # 否则 root logger 默认 WARNING，INFO 日志（进度、统计）会被丢弃。
        root = logging.getLogger()
        if root.level == logging.NOTSET or root.level > logging.INFO:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )

        def qr_cb(b64: str) -> None:
            with self._buf_lock:
                self._qr = b64
            self._set_state(detail="请用小红书 App 扫描登录二维码")
            self._log("INFO", "二维码已生成，请用小红书 App 扫描")

        def tick(waited: int) -> None:
            self._set_state(detail=f"等待扫码登录…（已等待 {waited} 秒）")

        handler = core.attach_log_callback(self._log)
        try:
            coro = core.run_job(
                cfg,
                on_log=None,
                on_progress=self._progress,
                cancel_event=self._cancel,
                qr_callback=None if cfg.headless else qr_cb,
                login_only=login_only,
            )
            return asyncio.run(coro)
        finally:
            logging.getLogger().removeHandler(handler)

    def _finalize(self, payload: dict) -> None:
        with self._buf_lock:
            self._finish = payload
            # 关键：把当前 run_id 一起带上，前端靠这个数字判断"是不是新的一次结束"。
            # 不能用 payload 对象本身比较（每次序列化都是新对象，JS 按引用比永不相等）。
            self._finish_run_id = self._run_id
        with self._lock:
            self._running = False

    def _build_config(self, options: dict) -> core.JobConfig:
        o = options or {}
        mode = o.get("mode", "account")

        def num(key, default, cast=float):
            v = o.get(key)
            if v in (None, ""):
                return default
            try:
                return cast(v)
            except (TypeError, ValueError):
                return default

        cfg = core.JobConfig(
            out=o.get("out") or str(PROJECT_DIR / "downloads"),
            profile_dir=o.get("profile_dir") or str(PROJECT_DIR / ".xhs_profile"),
            headless=False,          # GUI 模式必须有头（才能截二维码 / 显示浏览器）
            delay=num("delay", 1.5),
            concurrency=int(num("concurrency", 2, int)),
            retries=int(num("retries", 3, int)),
            scroll_pause=num("scroll_pause", 1.2),
            login_timeout=int(num("login_timeout", 300, int)),
            cookie=(o.get("cookie") or None),
        )
        # 优先使用本机浏览器（免装 playwright 内核）；跨平台探测
        from platform_utils import find_browser
        _browser, channel = find_browser()
        if channel:
            cfg.channel = channel
        # 若指定了内核目录（打包后由 app.py 设置），沿用即可，无需处理

        url = (o.get("url") or "").strip()
        # 仅登录模式不需要链接
        if o.get("_login_only") or url in ("", "login"):
            cfg.user = ""
            return cfg
        if not url:
            raise ValueError("请输入链接")
        if mode == "single":
            cfg.single_note = url
        else:
            cfg.user = url
            cfg.list_only = bool(o.get("list_only"))
        return cfg
