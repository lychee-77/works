#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_harvest.py
自动收获:
  1) 通过 CDP 连到 9222 端口 Edge
  2) 在 prev(广告卡前一个 article)的第二个子元素里遍历 5 个地块
  3) 判断每个地块状态:有"收获"按钮(在 success 按钮 class 上) → 点击
  4) 点击前用 getBoundingClientRect 校验节点仍在 DOM
  5) 如果点击时报"节点不存在/坐标为 0/异常",重新抓地块,重试该地块
  6) 最多重试 2 次
  7) 全部结束后输出报告
"""

import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from websocket import create_connection
except ImportError:
    raise SystemExit("缺少依赖:pip install websocket-client")

DEBUG_PORT = 9222
MAX_RETRY = 2  # 每个地块最多重试 2 次
RETRY_GAP = 1.2  # 重试前等页面刷新
# 默认 dry-run(只定位, 不真正点击); 加 --real 才派发真实点击
DRY_RUN = True
# 要处理的按钮类型(逗号分隔, 优先级从左到右): 收获 / 铲除 / 浇水 / 翻地
TARGET_ACTIONS = "收获"


# 强制 stdout/stderr 用 UTF-8 (Windows 默认 GBK, 中文 + 特殊字符会乱码/崩)
import io
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
except Exception:
    pass


# ============== CDP ==============
class CDPClient:
    def __init__(self, url):
        self.url = url
        self._id = 0
        self._sock = None

    def connect(self):
        self._sock = create_connection(self.url, timeout=30)

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass

    def send(self, m, p=None, t=30):
        self._id += 1
        self._sock.send(json.dumps({"id": self._id, "method": m, "params": p or {}}))
        self._sock.settimeout(t)
        while True:
            try:
                d = json.loads(self._sock.recv())
            except Exception:
                continue
            if d.get("id") == self._id:
                if "error" in d:
                    raise RuntimeError(d["error"])
                return d.get("result", {})


def get_target():
    with urllib.request.urlopen(f"http://localhost:{DEBUG_PORT}/json", timeout=5) as r:
        ts = json.loads(r.read().decode())
    for t in ts:
        if t.get("type") == "page" and "duanwuqiufenmao" in t.get("url", ""):
            return t
    for t in ts:
        if t.get("type") == "page":
            return t
    raise RuntimeError("no page")


def js(cdp, expr):
    r = cdp.send("Runtime.evaluate", {
        "expression": expr, "returnByValue": True, "userGesture": True,
    })
    if "exceptionDetails" in r:
        raise RuntimeError(f"JS: {r['exceptionDetails'].get('text','')}")
    return r.get("result", {}).get("value")


# ============== 抓地块 + 状态判定 ==============
FETCH_PLOTS_JS = r"""
(() => {
  const ad = document.querySelector('.farm-ad-card');
  if (!ad) return { ok: false, reason: 'no ad' };
  const prev = ad.previousElementSibling;
  if (!prev) return { ok: false, reason: 'no prev' };
  const second = prev.children[1];
  if (!second) return { ok: false, reason: 'no second child' };

  // 用 Set 去重,深度 1~2 找 class 含 _232e48d721
  const found = new Set();
  for (const c of second.children) {
    if (c.className && c.className.includes('_232e48d721')) found.add(c);
  }
  for (const c of second.children) {
    for (const g of c.children) {
      if (g.className && g.className.includes('_232e48d721')) found.add(g);
    }
  }

  const plots = [];
    for (const p of found) {
      const cls = (p.className || '').split(/\s+/);
      const state = cls.find(c => ['empty','ripe','care','success','danger'].includes(c)) || '?';
      const status = (p.querySelector('[class*="_6ac191ba6b"]')?.innerText || '').trim();
      // 去掉零宽字符再匹配地块编号
      const norm = status.replace(/[\u200b\u200c\u200d\u2060\ufeff]/g, '');
      const m = norm.match(/地块\s*(\d+)/)
             || (p.querySelector('span')?.innerText || '').replace(/[\u200b\u200c\u200d\u2060\ufeff]/g, '').match(/地块\s*(\d+)/);
      const plotNo = m ? parseInt(m[1]) : 0;

    // 找动作按钮: 优先收获, 否则 --demo 模式用浇水, 否则铲除
    const normText = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff]/g, '').trim();
    const wantActions = (typeof TARGET_ACTIONS !== 'undefined' ? TARGET_ACTIONS : '收获')
      .split(',').map(s => s.trim()).filter(Boolean);
    const btns = Array.from(p.querySelectorAll('button'));
    let actionBtn = null;
    for (const t of wantActions) {
      const b = btns.find(x => normText(x.innerText) === t);
      if (b && !b.hasAttribute('disabled')) { actionBtn = b; break; }
    }

    plots.push({
      plotNo,
      state,
      status,
      cls: p.className,
      // 用一个稳定指纹: 用 innerText 拼起来, 点击后对比是否变化
      fingerprint: (p.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
      harvestBtn: actionBtn ? {
        cls: actionBtn.getAttribute('class') || '',
        text: normText(actionBtn.innerText),
        disabled: actionBtn.hasAttribute('disabled'),
      } : null,
    });
  }
  plots.sort((a, b) => a.plotNo - b.plotNo);
  return { ok: true, plots };
})()
"""


# ============== 找 button 坐标 + 点击 ==============
CLICK_BY_TEXT_JS = r"""
((args) => {
  const { plotNo, buttonText } = args;
  const ad = document.querySelector('.farm-ad-card');
  if (!ad) return { ok: false, reason: 'no ad' };
  const prev = ad.previousElementSibling;
  const second = prev.children[1];

  // 找到对应 plotNo 的地块
  const found = new Set();
  for (const c of second.children) {
    if (c.className && c.className.includes('_232e48d721')) found.add(c);
  }
  for (const c of second.children) {
    for (const g of c.children) {
      if (g.className && g.className.includes('_232e48d721')) found.add(g);
    }
  }

  let target = null;
  for (const p of found) {
    const status = (p.querySelector('[class*="_6ac191ba6b"]')?.innerText || '').trim();
    const norm = status.replace(/[\u200b\u200c\u200d\u2060\ufeff]/g, '');
    const m = norm.match(/地块\s*(\d+)/);
    const no = m ? parseInt(m[1]) : 0;
    if (no === plotNo) {
      target = p;
      break;
    }
  }
  if (!target) return { ok: false, reason: 'plot not found' };

  // 校验 plot 还在 DOM
  if (!document.contains(target)) return { ok: false, reason: 'plot detached' };

  // 找按钮(零宽字符兼容)
  const normText2 = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff]/g, '').trim();
  const btns = Array.from(target.querySelectorAll('button'));
  const btn = btns.find(b => normText2(b.innerText) === buttonText);
  if (!btn) return { ok: false, reason: `button "${buttonText}" not in plot ${plotNo} (有 ${btns.length} 个按钮: ${btns.map(b => normText2(b.innerText)).join('|')})` };
  if (!document.contains(btn)) return { ok: false, reason: 'button detached' };

  // 取坐标
  const r = btn.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return { ok: false, reason: 'button 0-size' };
  const x = r.left + r.width / 2;
  const y = r.top + r.height / 2;
  if (x <= 0 || y <= 0) return { ok: false, reason: 'button off-screen' };

  return {
    ok: true, plotNo, buttonText,
    x, y, w: r.width, h: r.height,
    btnCls: btn.getAttribute('class') || '',
  };
})({ plotNo: PLOTNO, buttonText: BUTTONTEXT })
"""


def click_at(cdp, x, y, button="left"):
    cdp.send("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y,
        "button": button, "buttons": 0,
    })
    cdp.send("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y,
        "button": button, "buttons": 1, "clickCount": 1,
    })
    cdp.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y,
        "button": button, "buttons": 0, "clickCount": 1,
    })


def main():
    log_lines: list = []
    target = get_target()
    cdp = CDPClient(target["webSocketDebuggerUrl"])
    cdp.connect()
    cdp.send("Runtime.enable")
    cdp.send("Page.enable")
    print(f"[+] {target.get('url','')}")
    log_lines.append(f"# 自动收获日志  URL: {target.get('url','')}")
    log_lines.append(f"# 模式: {'DRY-RUN (只定位不点击)' if DRY_RUN else 'REAL (真点击)'}")
    log_lines.append(f"# 目标动作: {TARGET_ACTIONS}")

    def log(msg):
        print(msg)
        log_lines.append(msg)

    # ===== 抓地块(只抓一次) =====
    fetch_expr = FETCH_PLOTS_JS.replace("TARGET_ACTIONS", json.dumps(TARGET_ACTIONS))
    res = js(cdp, fetch_expr)
    if not res.get("ok"):
        log(f"[!] 抓地块失败: {res}")
        cdp.close()
        return 1

    plots_initial = res["plots"]
    log(f"\n[+] 共抓 {len(plots_initial)} 个地块")
    for p in plots_initial:
        log(f"  地块{p['plotNo']} state={p['state']:8s} "
            f"btn={'有('+p['harvestBtn']['text']+')' if p['harvestBtn'] else '无':6s}  "
            f"status={p['status'][:50]!r}")

    # ===== 找可收获的 =====
    harvestable = [p for p in plots_initial if p["harvestBtn"] and not p["harvestBtn"]["disabled"]]
    log(f"\n[+] 可收获地块: {len(harvestable)} 个")
    if not harvestable:
        log("  无可收获地块, 退出")
        cdp.close()
        return 0

    # ===== 逐个点击 + 失败重试 =====
    summary = []
    for p in harvestable:
        plot_no = p["plotNo"]
        btn_text = p["harvestBtn"]["text"]
        attempts = []
        for attempt in range(1, MAX_RETRY + 1):
            log(f"\n[地块 {plot_no}] 第 {attempt}/{MAX_RETRY} 次尝试 (按钮='{btn_text}')")
            # 取最新坐标(每次重新获取,因为 DOM 可能在变)
            expr = (CLICK_BY_TEXT_JS
                    .replace("PLOTNO", str(plot_no))
                    .replace("BUTTONTEXT", json.dumps(btn_text)))
            t0 = time.time()
            try:
                pos = js(cdp, expr)
            except Exception as e:
                pos = {"ok": False, "reason": f"JS 异常: {e}"}
            dt = round((time.time() - t0) * 1000, 1)

            if not pos.get("ok"):
                log(f"  ✗ 定位失败: {pos.get('reason')} (耗时 {dt}ms)")
                attempts.append({"n": attempt, "ok": False,
                                 "reason": pos.get("reason", "未知"),
                                 "elapsed_ms": dt})
                # 等一会再重抓
                time.sleep(RETRY_GAP)
                continue

            log(f"  ✓ 定位成功  plotNo={pos['plotNo']}  按钮='{pos['buttonText']}'  "
                f"@({pos['x']:.1f},{pos['y']:.1f})  {pos['w']:.0f}×{pos['h']:.0f}  "
                f"({dt}ms)")

            # 派发点击(除非 dry-run)
            if DRY_RUN:
                log(f"  → [DRY-RUN] 跳过真实派发, 仅记录坐标")
                attempts.append({"n": attempt, "ok": True, "reason": "dry-run", "elapsed_ms": dt})
                break
            try:
                click_at(cdp, pos["x"], pos["y"])
                log(f"  → 已派发 mouseMoved + Pressed + Released")
            except Exception as e:
                log(f"  ✗ 派发失败: {e}")
                attempts.append({"n": attempt, "ok": False,
                                 "reason": f"派发异常: {e}"})
                continue

            # 验证: 重新抓地块,看按钮是否消失
            time.sleep(0.6)
            verify_expr = FETCH_PLOTS_JS.replace("TARGET_ACTIONS", json.dumps(TARGET_ACTIONS))
            verify = js(cdp, verify_expr)
            if not verify.get("ok"):
                log(f"  ⚠ 验证抓取失败: {verify}")
                attempts.append({"n": attempt, "ok": False,
                                 "reason": "验证抓取失败"})
                continue
            new_plot = next((x for x in verify["plots"] if x["plotNo"] == plot_no), None)
            if not new_plot:
                log(f"  ⚠ 地块 {plot_no} 已不在(可能收获成功已被移除)")
                attempts.append({"n": attempt, "ok": True,
                                 "reason": "plot gone (可能已收获)"})
                break
            still_has_btn = new_plot["harvestBtn"] and not new_plot["harvestBtn"]["disabled"]
            new_fp = new_plot["fingerprint"]
            old_fp = p["fingerprint"]
            if not still_has_btn or new_fp != old_fp:
                log(f"  ✓ 地块 {plot_no} 状态已变化(fingerprint 改变或按钮消失)")
                attempts.append({"n": attempt, "ok": True,
                                 "reason": "state changed"})
                break
            else:
                log(f"  ⚠ 地块 {plot_no} 状态未变,可能点击未生效")
                attempts.append({"n": attempt, "ok": False,
                                 "reason": "state unchanged after click"})
                time.sleep(RETRY_GAP)
        summary.append({"plotNo": plot_no, "btnText": btn_text,
                        "attempts": attempts,
                        "success": any(a["ok"] for a in attempts)})

    # ===== 总结 =====
    log("\n" + "=" * 60)
    log("[总结]")
    succ = sum(1 for s in summary if s["success"])
    fail = len(summary) - succ
    log(f"  成功: {succ}")
    log(f"  失败: {fail}")
    for s in summary:
        ok = "✓" if s["success"] else "✗"
        log(f"  {ok} 地块{s['plotNo']} (按钮='{s['btnText']}') "
            f"尝试 {len(s['attempts'])} 次")

    cdp.close()
    return 0


if __name__ == "__main__":
    # CLI:
    #   --real           真正派发点击 (默认 dry-run)
    #   --action A,B,C   处理哪些动作, 默认"收获"
    args = ["--real"]
    if "--real" in args:
        DRY_RUN = False
        args.remove("--real")
    if "--action" in args:
        i = args.index("--action")
        if i + 1 < len(args):
            TARGET_ACTIONS = args[i + 1]
            args = args[:i] + args[i+2:]
    sys.exit(main())
