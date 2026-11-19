# 04 · 架构演进：CLI 与 GUI 共用核心

> 目标：理解 `JobConfig` / `run_job()` 这一层适配层为什么存在，以及改核心时怎么不破坏 CLI。
> 这是“从能跑”到“好维护”的关键一跳。

---

## 1. 问题是怎么来的

项目最初只有 CLI：`main()` 解析 argparse，直接调一个 `run(args)`，一路跑到底。
日志 `print` 到终端，进度靠用户自己看屏幕，取消就是 `Ctrl+C`。

后来要加 GUI，立刻撞上四个新需求：

| GUI 需要 | CLI 原本怎么做 |
| --- | --- |
| 实时日志显示在窗口里 | `print` 到 stdout，GUI 里看不见 |
| 进度条 | 没有进度事件 |
| 二维码显示在窗口内 | 保存成图片文件，用户得去文件夹找 |
| 点按钮取消任务 | 只能 `Ctrl+C` |

最省事的做法是**复制一份核心逻辑改成 GUI 版**。但那意味着以后每修一个 bug 要改两遍，
三遍之后必然分叉。

项目选择的方案是：**把核心改成可配置的执行器，入口只负责“构造参数 + 接收事件”**。

```
CLI  ──┐
       ├──► JobConfig ──► run_job(cfg, on_log, on_progress, cancel_event, qr_callback) ──► 核心组件
GUI  ──┘                            ↑ 全是可选参数
```

---

## 2. `JobConfig`：一次任务的全部参数

源码 `:1187-1231`。一个纯数据类：

```python
@dataclass
class JobConfig:
    user: str = ""
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
    single_note: Optional[str] = None
```

它把“用户意图”和“UI 形态”解耦了。两个入口各自构造：

**CLI**（`from_args`，`:1212`）：

```python
@classmethod
def from_args(cls, args) -> "JobConfig":
    return cls(
        user=getattr(args, "user", "") or "",
        out=getattr(args, "out", "./downloads"),
        max_notes=getattr(args, "max", 0) or 0,
        ...
    )
```

用 `getattr` 逐个取值，好处是参数缺失时不会直接崩，而是落回默认值。

**GUI**（`GuiBridge._build_config`，`gui/bridge.py:320`）：

```python
cfg = core.JobConfig(
    out=o.get("out") or str(PROJECT_DIR / "downloads"),
    headless=False,          # GUI 必须有头，才能显示二维码
    delay=num("delay", 1.5),
    concurrency=int(num("concurrency", 2, int)),
    ...
)
```

GUI 有个硬约束：`headless=False`。因为二维码要显示在窗口里，无头模式会被风控重定向到登录页。

### 参数映射表

| 用户看到的 | CLI 参数 | GUI 字段 | `JobConfig` 字段 |
| --- | --- | --- | --- |
| 主页链接 | `--user` | `url` | `user` |
| 保存目录 | `--out` | `out` | `out` |
| 仅列清单 | `--list-only` | `list_only` | `list_only` |
| 最大篇数 | `--max` | （GUI 不暴露） | `max_notes` |
| 请求间隔 | `--delay` | `delay` | `delay` |
| 并发数 | `--concurrency` | `concurrency` | `concurrency` |

注意 `max_notes`：GUI 没暴露这个高级选项，但核心仍然支持——**入口可以少给，核心不能少能力**。

---

## 3. 四个可选回调：核心不依赖 UI

`run_job()` 的回调全部走统一的“安全发射”包装（`:1270-1288`）：

```python
def emit_log(level: str, message: str) -> None:
    if on_log:
        try:
            on_log(level, message)
        except Exception:
            pass            # 回调绝不能把主任务搞崩

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
```

三个设计要点：

1. **空值友好**：`None` 直接跳过，CLI 不需要改任何行为
2. **异常隔离**：UI 回调抛异常不能中断下载
3. **鸭子类型取消**：只要求 `cancel_event` 有 `.is_set()`，`threading.Event` 直接能用

实际调用点遍布主流程：

```python
emit_progress(stage="init", user_id=user_id, out_dir=str(out_dir))
emit_progress(stage="logged_in")
emit_progress(stage="collected", total=len(videos) + len(unknowns))
emit_progress(stage="resolving", index=i, total=len(pending), note_id=note_id)
emit_progress(stage="downloading", total=len(targets))
emit_progress(stage="cancelled" if result.cancelled else "finished", ...)
```

`VideoDownloader` 也遵守同一约定（`on_progress` / `cancel_event` 可选），
发的是更细的 `file_start` / `file_done` / `file_failed` / `file_count`。

### 日志怎么变成回调

`attach_log_callback()`（`:1590`）挂一个 logging Handler：

```python
class _CallbackLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        self.callback(record.levelname, self.format(record))

def attach_log_callback(callback) -> logging.Handler:
    handler = _CallbackLogHandler(callback)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
    return handler
```

好处是核心代码里所有 `LOG.info/warning` 自动流向 GUI，不用把日志调用改成回调调用。
GUI 用完要 `removeHandler`，否则重复挂载会导致日志翻倍。

---

## 4. `JobResult`：统一的返回值

源码 `:1234-1247`：

```python
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
```

CLI 用它决定进程退出码（0 成功 / 1 异常 / 2 有失败项），
GUI 把它序列化成完成提示的数据：

```python
payload = {
    "ok": result.ok, "cached": result.cached,
    "failed": result.failed, "cancelled": result.cancelled,
    "out_dir": result.out_dir, "error": result.error,
}
```

同一个结果对象，两种呈现。这就是适配层的核心价值。

---

## 5. CLI 是薄封装：改核心时别破坏它

`run(args)`（`:1534`）现在只有三行：

```python
async def run(args) -> int:
    result = await run_job(JobConfig.from_args(args))
    if result.error:
        LOG.error("运行失败：%s", result.error)
    return result.exit_code
```

`main()` 负责 `logging.basicConfig`、合规横幅、异常兜底和 `sys.exit(code)`。

> **维护纪律**：任何对核心的改动，都要保证“不传任何回调时行为与旧 CLI 完全一致”。
> 具体做法：
> - 新回调参数一律给默认值 `None`
> - 回调调用全部放在 `if xxx:` 里
> - 不要用回调替代必要的状态写入（`state.json` 必须照常写）
> - 改完跑一遍 `python xhs_video_downloader.py --user <id> --list-only`

---

## 6. 单篇模式也是一条独立分支

`cfg.single_note` 非空时，`run_job()` 第一行就转给 `_run_single()`：

```python
if cfg.single_note:
    return await _run_single(cfg, emit_log, emit_progress, cancelled, cancel_event,
                             qr_callback=qr_callback)
```

它同样接收所有回调，所以它也是“共用核心”的一部分，不是另一套实现。
区别在于：输出目录固定为 `downloads/single/`，并发固定为 1。

---

## 动手实验

1. 写一个 `my_runner.py`，不碰 CLI 也不碰 GUI，直接：
   ```python
   cfg = core.JobConfig(user="<你的主页链接>", list_only=True, max_notes=3)
   result = asyncio.run(core.run_job(
       cfg,
       on_progress=lambda p: print("STAGE", p.get("stage")),
   ))
   ```
   观察 stage 事件的顺序和 GUI 里看到的进度是否一致。
2. 故意让 `on_progress` 回调抛异常（`raise RuntimeError`），确认下载仍然成功完成——
   这验证“回调异常隔离”。
3. 给 `JobConfig` 加一个新字段（如 `skip_existing`），只在 GUI 里设置，
   确认 CLI 不设置时行为不变。

---

## 小结

- `JobConfig` 是入口与核心之间的契约，入口可以少给字段，核心不能少能力
- 四个可选回调（日志/进度/取消/二维码）让核心完全不依赖 UI 技术栈
- 回调必须异常隔离、空值友好；取消只用 `.is_set()` 鸭子类型
- `JobResult` 统一返回值，CLI 转成退出码，GUI 转成完成提示
- `run()` 是薄封装——**这是改核心时最不能破坏的一条链路**

下一篇进入 GUI 内部：这些回调到了前端之后，为什么非要用轮询而不是直接推。
