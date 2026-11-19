# -*- coding: utf-8 -*-
"""
gui/test_finish_once.py — 验证「任务结束后提示只弹出一次」

回归 bug：原先用 d.finish 对象做 !== 比较，pywebview 每轮重新序列化，
JS 按引用比较永远为 true → handleFinish 每 400ms 被调用一次
→ 提示不消失且闪烁。

本测试不启动 GUI 窗口，直接模拟前端轮询逻辑：
1. 启动一个快速任务
2. 在任务结束后连续多次调用 get_updates，统计 finish_run_id 变化次数
3. 断言：只有一次 rid 递增，即 handleFinish 只会被调用一次
"""
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "gui"))

from bridge import GuiBridge  # noqa: E402


def main() -> int:
    b = GuiBridge({})

    # 用一个会立刻失败的链接，让任务快速结束（不走网络）
    print("[test] 启动任务（预期快速失败）…", flush=True)
    b.start({"mode": "single", "url": "invalid-link-xxx",
             "out": str(PROJECT / "downloads")})

    # 等待任务结束
    for _ in range(60):
        time.sleep(0.5)
        if not b._running:
            break
    print("[test] 任务已结束", flush=True)

    # 模拟前端连续轮询 10 次（每次 400ms 的心跳周期）
    seen_rids = []
    finish_seen_count = 0
    last_rid = 0
    for i in range(10):
        d = b.get_updates(0)
        rid = d.get("finish_run_id") or 0
        # 复刻前端判断逻辑
        if rid > 0 and d.get("finish") and rid != last_rid:
            last_rid = rid
            finish_seen_count += 1
        seen_rids.append(rid)
        time.sleep(0.1)

    print(f"[test] 10 轮轮询中 finish_run_id 序列: {seen_rids}", flush=True)
    print(f"[test] handleFinish 被触发次数: {finish_seen_count}（期望 1）", flush=True)

    ok = finish_seen_count == 1
    if ok:
        print("[test] ✓ 通过：提示只会弹出一次，不会重复/闪烁", flush=True)
    else:
        print("[test] ✗ 失败：finish 被重复触发，会导致提示不消失 + 闪烁", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
