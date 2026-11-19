# -*- coding: utf-8 -*-
"""
gui/test_toast_flash.py — 在真实 JS 环境中验证 toast 不再闪烁

背景：原代码用 `d.finish !== lastFinishToken` 比较，但 pywebview 每轮轮询
都会把 Python dict 重新序列化成**全新的 JS 对象**，JS 的 !== 按引用比较
永远为 true → handleFinish 每 400ms 被调用 → toast 反复 add/remove('show')
→ 用户看到"提示不消失且一直在闪"。

本测试用 Playwright 加载真实 index.html，mock 掉 pywebview API，
模拟"任务结束 + 持续轮询"，统计 toast 的 class 变化次数与最终状态。
"""
import asyncio
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from playwright.async_api import async_playwright  # noqa: E402

INDEX = PROJECT / "gui" / "index.html"

# mock 的 pywebview API：模拟"任务已结束，finish 持续存在"的状态
MOCK_JS = """
(() => {
  let finishCount = 0;
  const logs = [{level:'INFO', message:'全部完成：新下载 3，已存在跳过 0，失败 0', ts: 1792544000}];
  window.pywebview = {
    api: {
      get_updates: async (since) => {
        // 每次都返回"新构造"的 finish 对象（模拟 pywebview 的序列化行为）
        return {
          logs: since === 0 ? logs : [],
          total: logs.length,
          state: {stage:'finished', percent:100, detail:'完成：新下载 3，已存在 0，失败 0'},
          qr: null,
          running: false,
          finish: {ok:3, cached:0, failed:0, cancelled:0, error:null},
          finish_run_id: 1,
        };
      },
      get_env: async () => ({default_out:'/tmp', chrome:true, edge:true, playwright_bundled:false}),
      check_login: async () => ({logged_in:true}),
      start: async () => ({ok:true}),
      login_only: async () => ({ok:true}),
      cancel: async () => ({ok:true}),
      pick_folder: async () => null,
      open_folder: async () => ({ok:true}),
    }
  };
  // 记录 toast 的 class 变更次数，用于检测闪烁
  window.__toastFlips = 0;
  window.__toastStates = [];
})();
"""

PROBE_JS = """
(() => {
  const t = document.getElementById('toast');
  return {cls: t.className, text: t.textContent};
})();
"""

INSTRUMENT_JS = """
(() => {
  const t = document.getElementById('toast');
  const obs = new MutationObserver(() => {
    window.__toastFlips++;
    window.__toastStates.push(t.className.includes('show') ? 'show' : 'hide');
  });
  obs.observe(t, {attributes: true, attributeFilter: ['class']});
  window.__stopObserve = () => obs.disconnect();
  return true;
})();
"""


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1060, "height": 760})
        # 先注入 mock，再加载页面（让 pywebviewready 能正常触发）
        await page.add_init_script(MOCK_JS)
        await page.goto(INDEX.as_uri())

        # 等页面初始化完成
        await page.wait_for_timeout(800)
        await page.evaluate(INSTRUMENT_JS)

        # 触发初始化（poll 已在 pywebviewready 时启动）
        await page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")

        # 观察 5 秒（约 12 个轮询周期）
        print("[test] 开始观察 5 秒（约 12 个轮询周期，每轮 400ms）…", flush=True)
        await page.wait_for_timeout(5000)

        flips = await page.evaluate("window.__toastFlips")
        states = await page.evaluate("window.__toastStates")
        final = await page.evaluate(PROBE_JS)

        await browser.close()

    # 分析
    show_count = sum(1 for s in states if s == "show")
    print(f"[test] toast class 变更次数: {flips}", flush=True)
    print(f"[test] 其中变为 show 的次数: {show_count}", flush=True)
    print(f"[test] 最终状态: {final['cls'] or '(无 class)'}", flush=True)
    print(f"[test] 最终文本: {final['text'][:40]}", flush=True)

    # 判定：修复后 handleFinish 只应触发一次，所以 show 只加一次
    # （之后由 setTimeout 移除，class 变更总次数应 <= 2）
    ok = show_count <= 1 and flips <= 2
    print()
    if ok:
        print("[test] ✓ 通过：提示只弹出一次，无闪烁", flush=True)
    else:
        print(f"[test] ✗ 失败：提示反复弹出（show {show_count} 次，变更 {flips} 次）", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
