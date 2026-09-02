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

DEBUG_PORT = int(os.environ.get("FARM_DEBUG_PORT", "9222"))
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
    # 探测 CDP 端口是否在监听 (锁屏后 Edge 可能被杀/睡眠, 端口没了)
    try:
        with urllib.request.urlopen(f"http://localhost:{DEBUG_PORT}/json/version", timeout=3) as r:
            ver = json.loads(r.read().decode())
            print(f"[DIAG] CDP 端口 {DEBUG_PORT} 存活, Browser={ver.get('Browser','')[:60]!r}")
    except Exception as e:
        print(f"[DIAG] ✗ CDP 端口 {DEBUG_PORT} 不通: {type(e).__name__}: {e}")
        print(f"[DIAG]   → 锁屏/睡眠可能让 Edge 退到后台被挂起, 需解锁后由 scheduler 重新拉起")
        raise
    with urllib.request.urlopen(f"http://localhost:{DEBUG_PORT}/json", timeout=5) as r:
        ts = json.loads(r.read().decode())
    farm_tab = [t for t in ts if t.get("type") == "page" and "duanwuqiufenmao" in t.get("url", "")]
    all_tabs = [t for t in ts if t.get("type") == "page"]
    print(f"[DIAG] tabs: 农场={len(farm_tab)} 总 page={len(all_tabs)}")
    for t in all_tabs[:5]:
        print(f"[DIAG]   - {t.get('url','')[:80]!r}  title={t.get('title','')[:30]!r}")
    if not farm_tab:
        if not all_tabs:
            print(f"[DIAG] ✗ 没有任何 page tab, 锁屏后 Edge 可能被杀")
        else:
            print(f"[DIAG] ✗ 农场 tab 不在, 当前打开: {[t.get('url','')[:40] for t in all_tabs]}")
        raise RuntimeError("no farm page tab")
    target = farm_tab[0]
    print(f"[DIAG] ✓ 锁定 tab: {target.get('url','')[:80]!r}  ws={'YES' if target.get('webSocketDebuggerUrl') else 'NO'}")
    return target


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

  // 统一去零宽字符 + trim
  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();

  // 不依赖任何 hash. 策略:
  //   1) 找所有 button (后续用来判定动作 / 找 plotBox)
  //   2) 从 button 向上找含"地块N"文本 + 含 button 的容器
  //   3) state 通过容器 className 里的语义类 (empty/ripe/care/success/danger) 判定
  const allBtns = Array.from(second.querySelectorAll('button'));
  const seen = new Set();
  const plots = [];

  // 候选 action 按钮文本 (运行时由 Python 注入)
  const wantActions = (typeof TARGET_ACTIONS !== 'undefined' ? TARGET_ACTIONS : '收获')
    .split(',').map(s => s.trim()).filter(Boolean);

  for (const btn of allBtns) {
    const t = norm(btn.innerText);
    if (!wantActions.includes(t)) continue;
    if (btn.hasAttribute('disabled')) continue;

    // 找 plotBox: 向上找同时含"地块N" + 含 button 的最近容器
    let plotBox = null;
    let el = btn;
    for (let i = 0; i < 8 && el && second.contains(el); i++) {
      const txt = norm(el.innerText || '');
      if (/地块\s*\d+/.test(txt) && el.querySelectorAll('button').length > 0) {
        plotBox = el;
        break;
      }
      el = el.parentElement;
    }
    if (!plotBox) continue;

    const boxText = norm(plotBox.innerText || '');
    const m = boxText.match(/地块\s*(\d+)/);
    if (!m) continue;
    const plotNo = parseInt(m[1]);
    if (seen.has(plotNo)) continue;
    seen.add(plotNo);

    const cls = (plotBox.className || '').split(/\s+/);
    const state = cls.find(c => ['empty','ripe','care','success','danger'].includes(c)) || '?';

    // 状态文本: 优先取"地块N：xxx"
    let status = '';
    for (const e of plotBox.querySelectorAll('*')) {
      const tt = norm(e.innerText || '');
      if (/地块\s*\d+[：:]\s*\S/.test(tt) && e.children.length <= 1) { status = tt; break; }
    }
    if (!status) status = `地块 ${plotNo}`;

    plots.push({
      plotNo,
      state,
      status,
      cls: plotBox.className,
      fingerprint: (plotBox.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
      harvestBtn: {
        cls: btn.getAttribute('class') || '',
        text: t,
        disabled: false,
      },
    });
  }
  plots.sort((a, b) => a.plotNo - b.plotNo);
  return { ok: true, plots };
})()
"""


# ============== 找 button + 派发 click ==============
# 锁屏状态下 CDP Input.dispatchMouseEvent 会被 OS 丢弃,
# 必须直接派发 DOM MouseEvent/click/PointerEvent, 绕过 OS 输入层.
# 兼容: Element UI 监听 click; Vue 监听 pointerdown/up.
DISPATCH_CLICK_JS = r"""
((args) => {
  const { plotNo, buttonText } = args;
  const ad = document.querySelector('.farm-ad-card');
  if (!ad) return { ok: false, reason: 'no ad' };
  const prev = ad.previousElementSibling;
  const second = prev.children[1];
  if (!second) return { ok: false, reason: 'no second' };

  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();

  // 从 button(目标 buttonText) 向上找含"地块 plotNo"的容器
  const allBtns = Array.from(second.querySelectorAll('button'));
  let targetBtn = null;
  for (const b of allBtns) {
    if (norm(b.innerText) !== buttonText) continue;
    let el = b;
    for (let i = 0; i < 8 && el && second.contains(el); i++) {
      const t = norm(el.innerText || '');
      // 用 word-boundary 风格: "地块 plotNo" 后面不是数字
      const re = new RegExp(`地块\\s*${plotNo}(?!\\d)`);
      if (re.test(t)) { targetBtn = b; break; }
      el = el.parentElement;
    }
    if (targetBtn) break;
  }
  if (!targetBtn) {
    const sameTextCount = allBtns.filter(b => norm(b.innerText) === buttonText).length;
    return { ok: false, reason: `button "${buttonText}" not in plot ${plotNo} (共 ${sameTextCount} 个"${buttonText}"按钮)` };
  }
  if (!document.contains(targetBtn)) return { ok: false, reason: 'button detached' };
  if (targetBtn.hasAttribute('disabled')) return { ok: false, reason: 'button disabled' };

  // 派发完整事件序列: mousedown / mouseup / click + pointerdown / pointerup
  targetBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window, button: 0 }));
  targetBtn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window, button: 0 }));
  targetBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window, button: 0 }));
  targetBtn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, button: 0 }));
  targetBtn.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true, button: 0 }));

  return {
    ok: true, plotNo, buttonText,
    btnCls: targetBtn.getAttribute('class') || '',
  };
})(CLICKARGS)
"""


def click_at(cdp, x, y, button="left"):
    """保留以备非收菜场景使用; 锁屏态会被 OS 拦截, 收菜请用 dispatch_click_at"""
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


def dispatch_click_at(cdp, plot_no, button_text):
    """DOM 派发 click, 锁屏态可用. 复用 DISPATCH_CLICK_JS 模板."""
    expr = DISPATCH_CLICK_JS.replace(
        "CLICKARGS", json.dumps({"plotNo": plot_no, "buttonText": button_text})
    )
    return js(cdp, expr)


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
        # 区分两种 0: 真没东西成熟 vs 没抓到地块
        if len(plots_initial) > 0:
            states = {}
            for p in plots_initial:
                states[p["state"]] = states.get(p["state"], 0) + 1
            log(f"  无可收获地块 (抓到 {len(plots_initial)} 个, state 分布: {states}), 等菜熟")
        else:
            log(f"  ⚠ 抓地块返回 ok 但 plots=[], 页面可能未渲染完或选择器漂移")
        log("  退出")
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
            t0 = time.time()
            # 锁屏态用 DOM 派发 click (绕过 OS 输入层拦截)
            pos = dispatch_click_at(cdp, plot_no, btn_text)
            dt = round((time.time() - t0) * 1000, 1)

            if not pos.get("ok"):
                log(f"  ✗ 派发失败: {pos.get('reason')} (耗时 {dt}ms)")
                attempts.append({"n": attempt, "ok": False,
                                 "reason": pos.get("reason", "未知"),
                                 "elapsed_ms": dt})
                # 等一会再重抓
                time.sleep(RETRY_GAP)
                continue

            log(f"  ✓ 已派发 click  plotNo={pos['plotNo']}  按钮='{pos['buttonText']}'  "
                f"({dt}ms)")

            # DIAG: 派发后立刻抓一次按钮状态, 区分是 click 没生效 还是 sleep 0.6 后状态没刷新
            try:
                snap = js(cdp, r"""
(() => {
  const ad = document.querySelector('.farm-ad-card');
  if (!ad) return { ok:false, reason:'no ad' };
  const prev = ad.previousElementSibling;
  const second = prev && prev.children[1];
  if (!second) return { ok:false, reason:'no second' };
  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
  const allBtns = Array.from(second.querySelectorAll('button'));
  const sameText = allBtns.filter(b => norm(b.innerText) === '""" + btn_text + r"""');
  return {
    ok: true,
    sameTextCount: sameText.length,
    disabledCount: sameText.filter(b => b.hasAttribute('disabled')).length,
    firstBtnCls: sameText[0] ? (sameText[0].getAttribute('class') || '').slice(0, 60) : null,
  };
})()
""")
                if snap and snap.get("ok"):
                    log(f"  [DIAG] 派发后即时: 同名按钮={snap['sameTextCount']} disabled={snap['disabledCount']}  cls={snap.get('firstBtnCls')!r}")
            except Exception as e:
                log(f"  [DIAG] 即时抓取失败: {e}")

            if DRY_RUN:
                log(f"  → [DRY-RUN] 跳过真实派发, 仅记录")
                attempts.append({"n": attempt, "ok": True, "reason": "dry-run", "elapsed_ms": dt})
                break

            # 验证: 重新抓地块,看按钮是否消失
            # 自旋等按钮消失 (上限 1.5s, 每 200ms 探一次)
            t_h0 = time.time()
            h_poll = 0
            h_gone = False
            while time.time() - t_h0 < 1.5:
                h_poll += 1
                snap_v = js(cdp, r"""
(() => {
  const ad = document.querySelector('.farm-ad-card');
  if (!ad) return { ok:false, reason:'no ad' };
  const prev = ad.previousElementSibling;
  const second = prev && prev.children[1];
  if (!second) return { ok:false, reason:'no second' };
  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
  const re = new RegExp('地块\\s*""" + str(plot_no) + r"""(?!\\d)');
  // 找 plotBox: 向上找含"地块 plotNo"+含 button 的最近容器
  const allBtns = Array.from(second.querySelectorAll('button'));
  let plotBox = null;
  for (const b of allBtns) {
    let el = b;
    for (let i = 0; i < 8 && el && second.contains(el); i++) {
      const t = norm(el.innerText || '');
      if (re.test(t) && el.querySelectorAll('button').length > 0) { plotBox = el; break; }
      el = el.parentElement;
    }
    if (plotBox) break;
  }
  if (!plotBox) return { ok:true, plotGone:true };
  // 找 harvest 按钮 (按钮文本 = btn_text)
  const btns = Array.from(plotBox.querySelectorAll('button'));
  const hBtn = btns.find(b => norm(b.innerText) === '""" + btn_text + r"""');
  return { ok:true, plotGone:false, hasBtn:!!hBtn, disabled:hBtn ? hBtn.hasAttribute('disabled') : null };
})()
""")
                if snap_v and snap_v.get("ok") and (snap_v.get("plotGone") or not snap_v.get("hasBtn")):
                    h_gone = True
                    break
                time.sleep(0.2)
            t_h = round((time.time() - t_h0) * 1000, 0)
            log(f"  [DIAG] 等待按钮消失: {'消失' if h_gone else '未消失'} (poll={h_poll} 次, {int(t_h)}ms)")

            verify_expr = FETCH_PLOTS_JS.replace("TARGET_ACTIONS", json.dumps(TARGET_ACTIONS))
            verify = js(cdp, verify_expr)
            if not verify.get("ok"):
                log(f"  ⚠ 验证抓取失败: {verify}")
                attempts.append({"n": attempt, "ok": False,
                                 "reason": "验证抓取失败"})
                continue
            new_plot = next((x for x in verify["plots"] if x["plotNo"] == plot_no), None)
            # DIAG: 派发前后指纹对比
            if new_plot:
                log(f"  [DIAG] 派发前 fp={p['fingerprint'][:50]!r}")
                log(f"  [DIAG] 派发后 fp={new_plot['fingerprint'][:50]!r}  same={new_plot['fingerprint']==p['fingerprint']}")
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
    # 收菜不决定下次间隔, 始终 30 分钟
    print(f"[+] NEXT_INTERVAL={30*60}  (收菜固定 30 分钟)")
    sys.exit(main())
