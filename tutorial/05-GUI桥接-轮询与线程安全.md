# 05 · GUI 桥接：轮询与线程安全

> 目标：搞懂 pywebview 的两个经典坑，以及本项目用“前端轮询”怎么绕开它们。
> 这篇里的每个设计都是从真实 bug 里长出来的，不是凭空架构。

---

## 1. 坑一：`evaluate_js` 必须从 UI 主线程调用

pywebview 的 `window.evaluate_js()` 看似是个普通的 Python 方法，
实际上它要跟 WebView 的消息循环打交道。**在后台工作线程里调用会挂起**——
表现特别有迷惑性：任务其实跑完了，但窗口卡死、进度不更新。

而下载任务必须放后台线程，否则点“开始”之后整个界面冻结。

于是形成一个死锁式的矛盾：

```
后台线程：要跑耗时任务，但不能碰 evaluate_js
UI 线程 ：能碰 evaluate_js，但不能跑耗时任务
```

### 解法：前端轮询 + 线程安全缓冲区

```mermaid
flowchart LR
    W["后台工作线程<br/>asyncio.run(run_job)"] -->|写| B["线程安全缓冲区<br/>(lock 保护)"]
    F["前端 setInterval 400ms"] -->|get_updates(since)| B
    B -->|增量 logs/state/qr/finish| F
```

后台只往缓冲区写基本数据，一个 UI API 都不碰；前端自己定时来拉。
`gui/bridge.py` 开头的注释把这条原则写得很直白（`:5-19`）。

---

## 2. 缓冲区长什么样

`GuiBridge.__init__`（`gui/bridge.py:36-58`）：

```python
self._lock = threading.Lock()

self._buf_lock = threading.Lock()
self._logs: list[dict] = []      # [{level, message, ts}]
self._state: dict = {"stage": "idle", "percent": 0, "detail": "等待开始"}
self._qr: Optional[str] = None   # base64 PNG
self._finish: Optional[dict] = None

self._run_id: int = 0
self._finish_run_id: int = 0
```

所有写操作都进 `_buf_lock`：

```python
def _log(self, level, message):
    with self._buf_lock:
        self._logs.append({"level": level, "message": message, "ts": time.time()})
        if len(self._logs) > 3000:
            self._logs = self._logs[-1500:]   # 防止长时间跑内存爆炸

def _set_state(self, **kw):
    with self._buf_lock:
        self._state.update(kw)
```

读接口 `get_updates(since)`（`:134`）做增量：

```python
with self._buf_lock:
    total = len(self._logs)
    start = max(0, min(since, total))
    logs = self._logs[start:]
    return {
        "logs": logs, "total": total,
        "state": dict(self._state),
        "qr": self._qr, "running": self._running,
        "finish": self._finish,
        "finish_run_id": self._finish_run_id,
    }
```

`since` 是前端已消费的日志条数。前端每次带上它，只拿新增部分：

```js
const d = await window.pywebview.api.get_updates(logCursor);
logCursor = d.total;
d.logs.forEach(x => appendLog(x.level, x.message));
```

好处：不需要复杂消息队列，也不会重复渲染旧日志；前端刷新页面后从 0 开始就能重放全部日志。

---

## 3. 进度事件到百分比的映射

`_progress()`（`:76-129`）把核心的 `stage` 事件翻译成前端可读状态：

| stage | percent | detail |
| --- | --- | --- |
| `init` | 2% | 账号 ID |
| `logged_in` | 4% | 登录成功，开始收集笔记 |
| `collected` | 8% | 收集到 N 篇 |
| `resolving` | 8% + 42×i/total | 解析直链 i/total |
| `downloading` | 50% | 开始下载 N 个视频 |
| `file_done` / `file_start` | 保持 | 当前文件名与大小 |
| `file_count` | 50% + 50×done/total | 下载进度 done/total |
| `list_done` / `login_only_done` | 100% | 清单/登录完成 |
| `finished` | 100% | 新下载 / 已存在 / 失败统计 |

把“解析直链”和“实际下载”各分一段 42% / 50%，是因为前者通常较慢且无法按字节计量。
进度条不必绝对精确，**单调不倒退**比精确更重要。

---

## 4. 坑二：pywebview 传对象永远是新引用

这是导致“任务结束提示不消失、一直闪”的元凶。

旧逻辑大概是：

```python
# 后端
self._finish = payload

# 前端
if (d.finish !== lastFinish) {
    lastFinish = d.finish;
    handleFinish(d.finish);
}
```

看起来没毛病。但 pywebview 每次跨语言调用都会把 Python dict **重新序列化成一个全新的 JS 对象**。
JS 的 `!==` 对对象是**按引用比较**，所以：

```js
{} !== {}   // true，永远 true
```

结果就是每 400ms 触发一次 `handleFinish()`，提示反复弹出、动画反复播放，永远不消失。

### 修复：用自增的数字序号

后端（`bridge.py:57-58, 241-250, 311-318`）：

```python
self._run_id: int = 0
self._finish_run_id: int = 0

def _reset(self):
    ...
    self._run_id += 1
    self._finish_run_id = 0     # 新任务开始，清零

def _finalize(self, payload):
    with self._buf_lock:
        self._finish = payload
        self._finish_run_id = self._run_id   # 标记属于哪次任务
```

前端（`gui/index.html:350, 371-374`）：

```js
let lastFinishRunId = 0;
...
const rid = d.finish_run_id || 0;
if (rid > 0 && d.finish && rid !== lastFinishRunId) {
  lastFinishRunId = rid;
  handleFinish(d.finish);
}
```

**数字按值比较**，不受引用序列化影响。
`_reset()` 里把 `finish_run_id` 清零，是为了防止新任务刚启动、前端还没轮询到新状态时，
误读到上一次的 finish 触发旧提示。

> **可复用原则**：跨语言边界不要比较对象身份。传一个单调递增的整数作为事件版本号。

---

## 5. 提示不消失的第二个原因：定时器累积

即使 finish 去重做对了，toast 也可能不消失。原因在 `toast()`：

```js
// 错误写法：每次调用都新建一个定时器
function toast(msg) {
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}
```

多次调用会累积多个定时器：旧的先到点，把刚显示的提示关掉；新的又打开……
表现为闪烁和“不消失”。

修复（`gui/index.html:317-324`）——**复用同一个句柄**：

```js
let toastTimer = null;

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);   // 先撤销旧的
  toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}
```

项目里为这个 bug 写了专门的回归测试 `gui/test_toast_flash.py`，
在真实 JS 环境里模拟多次调用，验证最终只留一个定时器。

---

## 6. 二维码怎么到界面上

核心层的 `qr_callback` 收到 base64 PNG 后（`bridge.py:288-292`）：

```python
def qr_cb(b64: str) -> None:
    with self._buf_lock:
        self._qr = b64
    self._set_state(detail="请用小红书 App 扫描登录二维码")
    self._log("INFO", "二维码已生成，请用小红书 App 扫描")
```

前端轮询到 `d.qr` 变化就更新弹层：

```js
if (d.qr && d.qr !== lastQR) {
  lastQR = d.qr;
  $('qr-img').src = 'data:image/png;base64,' + d.qr;
  $('qr-mask').classList.add('show');
}
```

这里 `d.qr` 是**字符串**，按值比较是安全的——和前面 finish 对象是新的 JS 对象形成对照。

登录成功后 `handleFinish()` / 状态更新会移除 `qr-mask`。

---

## 7. 取消与任务生命周期

```python
def start(self, options: dict) -> dict:
    with self._lock:
        if self._running:
            return {"ok": False, "error": "已有任务在运行"}
        self._running = True
    self._reset()
    self._thread = threading.Thread(target=self._worker, args=(options,), daemon=True)
    self._thread.start()
    return {"ok": True}

def cancel(self) -> dict:
    if not self._running:
        return {"ok": False, "error": "当前没有运行中的任务"}
    self._cancel.set()
    self._log("WARNING", "已发送取消请求，正在停止…")
    return {"ok": True}
```

`threading.Event` 直接传给 `run_job(cancel_event=self._cancel)`，
核心在三个地方检查它：收集循环、解析循环、下载 chunk 写入。

关闭窗口时 `gui/app.py:92-97` 的 `on_closing` 会先请求取消，
避免留下 `.part` 临时文件。

---

## 8. 另一个隐坑：GUI 不走 `main()`，日志级别要自己配

CLI 的 `main()` 里有 `logging.basicConfig(level=...)`。
GUI 直接调 `run_job()`，绕过了这段，root logger 默认 WARNING，
**所有 INFO 日志全被丢掉**——界面右侧只剩零星警告，用户以为程序没在干活。

`_run_async()`（`bridge.py:274-286`）里补上：

```python
root = logging.getLogger()
if root.level == logging.NOTSET or root.level > logging.INFO:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
```

---

## 动手实验

1. 把轮询间隔从 400ms 改成 100ms 和 2000ms，分别感受日志延迟与 CPU 占用。
2. 在 `get_updates()` 里把 `"finish_run_id": self._finish_run_id` 那行注释掉，
   运行 `python gui/test_finish_once.py`，看回归测试是否抓到问题。
3. 把 `toast()` 里的 `clearTimeout` 去掉，跑 `python gui/test_toast_flash.py`。
4. 搜一下 `gui/app.py` 里所有以 `_` 开头的公开属性，理解为什么不能再多一个返回
   `webview.Window` 的 public property——那会触发无限反射递归，窗口起不来。

---

## 小结 / 自检题

- 为什么后台线程不能调 `evaluate_js`？轮询方案如何绕开？
- `get_updates(since)` 的 `since` 是什么？为什么能防重复渲染？
- 为什么不能用 `d.finish !== lastFinish` 判断是否新任务？`finish_run_id` 为什么必须是数字？
- toast 为什么要复用定时器句柄？
- GUI 为什么不显示 INFO 日志？怎么修？

这五个问题的答案都在上面。能讲清楚，就已经掌握了 pywebview 最常见的坑。
下一篇把视角拉到工程交付：跨平台与打包。
