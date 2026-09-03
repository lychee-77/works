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
# 默认 REAL (真正点击); 加 --dry-run 才只定位不点
DRY_RUN = False
# 要处理的按钮类型(逗号分隔, 优先级从左到右): 收获 / 铲除 / 浇水 / 翻地
# 默认改为"翻地,收获": 同一地块先翻地, 再收获
TARGET_ACTIONS = "翻地,收获"


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
# 改: 不再按 TARGET_ACTIONS 过滤, 而是收集地块内所有按钮, 决策在 Python 端做
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

  // 策略: 从所有 button 出发, 向上找含"地块N"+含 button 的容器 (plotBox)
  // 同一 plotBox 内只记一次, 但收集 plotBox 内**所有**按钮文本
  const allBtns = Array.from(second.querySelectorAll('button'));
  const seen = new Set();
  const plots = [];

  for (const btn of allBtns) {
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

    // 收集 plotBox 内所有按钮
    const btnsInBox = Array.from(plotBox.querySelectorAll('button'));
    const btnInfo = {};
    for (const b of btnsInBox) {
      const t = norm(b.innerText);
      if (!t) continue;
      if (!btnInfo[t]) btnInfo[t] = { cls: (b.getAttribute('class')||'').slice(0, 60), disabled: b.hasAttribute('disabled') };
    }

    // 状态文本
    let status = '';
    for (const e of plotBox.querySelectorAll('*')) {
      const tt = norm(e.innerText || '');
      if (/地块\s*\d+[：:]\s*\S/.test(tt) && e.children.length <= 1) { status = tt; break; }
    }
    if (!status) status = `地块 ${plotNo}`;

    // 识别地块里种的是什么菜: 遍历 boxText 找已知菜名
    // 找法: 在 plotBox 内先找 (短的) 文本节点, 等于"菠萝"/"胡萝卜"/"白菜"/"小麦"/"玉米"等
    const KNOWN_CROPS = ['菠萝', '胡萝卜', '白菜', '小麦', '玉米', '土豆', '番茄', '茄子', '辣椒', '南瓜', '西瓜', '草莓', '葡萄'];
    let crop = '';
    for (const c of KNOWN_CROPS) {
      // 必须有纯文本叶节点 = 该菜名, 避免误匹配 "土地上长着胡萝卜的图标" 这种长文本
      const leaves = Array.from(plotBox.querySelectorAll('*'))
        .filter(e => norm(e.innerText) === c && e.children.length === 0);
      if (leaves.length > 0) { crop = c; break; }
    }
    // fallback: boxText 含 "菠萝" 但没匹配上叶节点, 也认
    if (!crop) {
      for (const c of KNOWN_CROPS) { if (boxText.includes(c)) { crop = c; break; } }
    }

    plots.push({
      plotNo,
      state,
      status,
      crop,           // 种的是什么 (空字符串 = 没种)
      cls: plotBox.className,
      fingerprint: (plotBox.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
      btns: btnInfo,  // {"翻地": {...}, "收获": {...}, "种植": {...}}
      // 兼容老调用方 (auto_harvest 自己旧逻辑): 第一个"收获"按钮当作 harvestBtn
      harvestBtn: btnInfo['收获'] ? { text: '收获', disabled: btnInfo['收获'].disabled, cls: btnInfo['收获'].cls } : null,
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

    # ===== 解析 actions 列表 (仅供调度兼容, 真正的执行逻辑在 execute_plot 里) =====
    # 保留 CLI --action 仅影响下面"是否执行翻地/收获"两个动作;
    # 双倍经验卡 / 道具按钮: 完全由地块实际种的是不是菠萝决定, 与 --action 无关
    actions = [a.strip() for a in TARGET_ACTIONS.split(",") if a.strip()]
    log(f"\n[+] 动作列表 (CLI 传入, 仅决定翻地/收获是否跑): {actions}")
    do_tilling = "翻地" in actions
    do_harvest = "收获" in actions
    log(f"[+] 实际执行: 翻地={do_tilling}  收获={do_harvest}  (双倍卡: 地块种菠萝时自动触发, 与 actions 无关)")

    # 提取所有"含目标按钮"的地块: 一个地块只要任一 action 按钮存在就算候选
    def plot_has_action(plot, action):
        # 优先用 btns 字典 (新格式, 包含地块内所有按钮)
        if "btns" in plot and plot["btns"]:
            info = plot["btns"].get(action)
            return info is not None and not info.get("disabled")
        # fallback: harvestBtn 字段 (老格式, 只对应"收获")
        if "harvestBtn" in plot and plot["harvestBtn"]:
            return (not plot["harvestBtn"].get("disabled")
                    and plot["harvestBtn"].get("text") == action)
        return False

    def plot_action_pair(plot, action):
        # 取按钮信息 (兼容两种返回格式)
        if "harvestBtn" in plot and plot["harvestBtn"] and plot["harvestBtn"].get("text") == action:
            return plot["harvestBtn"]
        if "btns" in plot:
            return plot["btns"].get(action)
        return None

    # 生成候选地块: 只要地块里有翻地或收获按钮, 就进候选
    # 每个地块只执行一次循环 (execute_plot 内部按 翻地→（菠萝）道具+双倍卡→收获 顺序)
    tasks = []
    for p in plots_initial:
        # 只在以下情况拉入: 有翻地按钮 或 有收获按钮
        if (do_tilling and plot_has_action(p, "翻地")) or (do_harvest and plot_has_action(p, "收获")):
            tasks.append(p)
    log(f"\n[+] 待执行地块: {len(tasks)} 个 (按地块循环, 每地块内: 翻地 →（菠萝则）双倍卡 → 收获)")
    for p in tasks:
        log(f"  地块{p['plotNo']}  crop={p.get('crop','?')!r}  state={p['state']}  "
            f"翻地={'✓' if plot_has_action(p,'翻地') else '✗'}  收获={'✓' if plot_has_action(p,'收获') else '✗'}")

    if not tasks:
        if len(plots_initial) > 0:
            states = {}
            for p in plots_initial:
                states[p["state"]] = states.get(p["state"], 0) + 1
            log(f"  无可执行任务 (抓到 {len(plots_initial)} 个, state 分布: {states}), 等菜熟")
        else:
            log(f"  ⚠ 抓地块返回 ok 但 plots=[], 页面可能未渲染完或选择器漂移")
        log("  退出")
        cdp.close()
        return 0

    # ===== 复用的"做一次动作"函数 (含重试 + 验证) =====
    def do_action(cdp, plot_no, btn_text):
        """对单地块单动作做点击+重试+验证, 返回 {"ok": bool, "reason": str, "attempts": [...]}"""
        attempts = []
        for attempt in range(1, MAX_RETRY + 1):
            log(f"\n[地块 {plot_no}] 第 {attempt}/{MAX_RETRY} 次尝试 (动作='{btn_text}')")
            t0 = time.time()
            pos = dispatch_click_at(cdp, plot_no, btn_text)
            dt = round((time.time() - t0) * 1000, 1)

            if not pos.get("ok"):
                log(f"  ✗ 派发失败: {pos.get('reason')} (耗时 {dt}ms)")
                attempts.append({"n": attempt, "ok": False, "reason": pos.get("reason", "未知"), "elapsed_ms": dt})
                time.sleep(RETRY_GAP)
                continue

            log(f"  ✓ 已派发 click  plotNo={pos['plotNo']}  按钮='{pos['buttonText']}'  "
                f"({dt}ms)")

            # DIAG: 派发后立刻抓一次同名按钮状态
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
  };
})()
""")
                if snap and snap.get("ok"):
                    log(f"  [DIAG] 派发后即时: 同名按钮={snap['sameTextCount']} disabled={snap['disabledCount']}")
            except Exception as e:
                log(f"  [DIAG] 即时抓取失败: {e}")

            if DRY_RUN:
                log(f"  → [DRY-RUN] 跳过真实派发, 仅记录")
                attempts.append({"n": attempt, "ok": True, "reason": "dry-run", "elapsed_ms": dt})
                return {"ok": True, "reason": "dry-run", "attempts": attempts}

            # 自旋等按钮消失 (上限 1.5s)
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

            # 重新抓一次 plots, 判断该按钮是否还在
            verify = js(cdp, fetch_expr)
            if not verify.get("ok"):
                log(f"  ⚠ 验证抓取失败: {verify}")
                attempts.append({"n": attempt, "ok": False, "reason": "验证抓取失败"})
                continue
            new_plot = next((x for x in verify["plots"] if x["plotNo"] == plot_no), None)
            if not new_plot:
                log(f"  ⚠ 地块 {plot_no} 已不在(可能动作已完成)")
                attempts.append({"n": attempt, "ok": True, "reason": "plot gone"})
                return {"ok": True, "reason": "plot gone", "attempts": attempts}
            if not plot_has_action(new_plot, btn_text):
                log(f"  ✓ 地块 {plot_no} 动作 '{btn_text}' 已生效 (按钮消失)")
                attempts.append({"n": attempt, "ok": True, "reason": "state changed"})
                return {"ok": True, "reason": "state changed", "attempts": attempts}
            log(f"  ⚠ 地块 {plot_no} 按钮 '{btn_text}' 还在, 可能点击未生效")
            attempts.append({"n": attempt, "ok": False, "reason": "state unchanged after click"})
            time.sleep(RETRY_GAP)
        log(f"  ✗ 地块 {plot_no} 动作 '{btn_text}' 全部尝试失败")
        return {"ok": False, "reason": "max retries", "attempts": attempts}

    # ===== 菠萝的 "道具 → 双倍经验卡" 流程 =====
    # 仅在地块 crop == '菠萝' 且地块有"收获"按钮(意味着马上要收) 时执行
    # 顺序: 派发点击地块的"道具"按钮 → 等道具弹窗 → 找"双倍经验卡"的"使用"按钮 → 派发
    LOCATE_PLOT_PROP_BTN_JS_HARV = r"""
((args) => {
  const { plotNo } = args;
  const ad = document.querySelector('.farm-ad-card');
  if (!ad) return { ok: false, reason: 'no ad' };
  const prev = ad.previousElementSibling;
  const second = prev.children[1];
  if (!second) return { ok: false, reason: 'no second' };
  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
  const allBtns = Array.from(second.querySelectorAll('button'))
    .filter(b => norm(b.innerText) === '道具' && !b.hasAttribute('disabled'));
  const seen = new Set();
  for (const b of allBtns) {
    let plotBox = null, el = b;
    for (let i = 0; i < 6 && el && second.contains(el); i++) {
      const t = norm(el.innerText || '');
      if (/地块\s*\d+/.test(t) && el.querySelectorAll('button').length > 0) { plotBox = el; break; }
      el = el.parentElement;
    }
    if (!plotBox || seen.has(plotBox)) continue;
    seen.add(plotBox);
    const boxText = norm(plotBox.innerText || '');
    let minN = Infinity;
    for (const m of boxText.matchAll(/地块\s*(\d+)/g)) {
      const n = parseInt(m[1]); if (n < minN) minN = n;
    }
    if (minN === plotNo) {
      b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window, button: 0 }));
      b.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
      b.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true }));
      return { ok: true, plotNo };
    }
  }
  return { ok: false, reason: `plot ${plotNo} 无"道具"按钮` };
})(PROPARGS)
"""
    LOCATE_PROP_USE_BTN_JS_HARV = r"""
(() => {
  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
  let dialog = null;
  const allOverlays = document.querySelectorAll('div.el-overlay');
  for (const ov of allOverlays) {
    const d = ov.querySelector('div.el-dialog');
    if (!d) continue;
    const r = d.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && d.querySelectorAll('button').length > 0) {
      dialog = d; break;
    }
  }
  if (!dialog) return { ok: false, reason: '道具弹窗未真正出现' };
  const candidates = Array.from(dialog.querySelectorAll('*'))
    .filter(e => norm(e.innerText) === '双倍经验卡' && e.children.length === 0);
  if (candidates.length === 0) return { ok: false, reason: '道具弹窗里没找到"双倍经验卡"' };
  let useBtn = null;
  for (const span of candidates) {
    let el = span;
    for (let i = 0; i < 6; i++) {
      el = el.parentElement;
      if (!el || !dialog.contains(el)) break;
      const btns = Array.from(el.querySelectorAll(':scope > button'));
      const btn = btns.find(b => norm(b.innerText) === '使用' && !b.hasAttribute('disabled'));
      if (btn) { useBtn = btn; break; }
    }
    if (useBtn) break;
  }
  if (!useBtn) return { ok: false, reason: '找不到"双倍经验卡"的"使用"按钮' };
  // 派发 click + pointer
  useBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window, button: 0 }));
  useBtn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
  useBtn.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true }));
  return { ok: true };
})()
"""

    def use_double_xp_card(cdp, plot_no):
        """对种菠萝的地块: 点'道具'按钮触发弹窗, 再找'双倍经验卡'并点'使用'."""
        if DRY_RUN:
            log(f"\n[道具·双倍卡] 地块 {plot_no} (菠萝) — [DRY-RUN] 跳过")
            return {"ok": True, "reason": "dry-run"}
        for attempt in range(1, MAX_RETRY + 1):
            log(f"\n[道具·双倍卡] 地块 {plot_no} (菠萝) 第 {attempt}/{MAX_RETRY} 次")
            # 1) 点 道具 按钮
            d1 = js(cdp, LOCATE_PLOT_PROP_BTN_JS_HARV.replace("PROPARGS", json.dumps({"plotNo": plot_no})))
            if not d1.get("ok"):
                log(f"  ✗ 派发'道具'按钮失败: {d1.get('reason')}")
                time.sleep(RETRY_GAP); continue
            log(f"  ✓ 已点'道具'按钮, 等弹窗...")
            # 2) 等弹窗 + 找双倍卡并点使用
            time.sleep(1.0)
            d2 = js(cdp, LOCATE_PROP_USE_BTN_JS_HARV)
            if not d2.get("ok"):
                log(f"  ✗ 找/点 双倍卡失败: {d2.get('reason')}")
                time.sleep(RETRY_GAP); continue
            # 3) 自旋等弹窗消失
            t_c0 = time.time(); c_gone = False; c_poll = 0
            while time.time() - t_c0 < 3.0:
                c_poll += 1
                v = js(cdp, r"""
(() => {
  const overlays = document.querySelectorAll('div.el-overlay');
  for (const ov of overlays) {
    const d = ov.querySelector('div.el-dialog');
    if (!d) continue;
    const r = d.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && d.querySelectorAll('button').length > 0) {
      return { gone: false };
    }
  }
  return { gone: true };
})()
""")
                if v and v.get("gone"): c_gone = True; break
                time.sleep(0.15)
            if c_gone:
                log(f"  ✓ 双倍卡已使用, 弹窗消失 (poll={c_poll}, {int((time.time()-t_c0)*1000)}ms)")
                return {"ok": True, "reason": "use card ok"}
            log(f"  ⚠ 弹窗 3s 仍未消失")
        return {"ok": False, "reason": "max retries"}

    # ===== 按地块循环执行: 翻地 → （菠萝）道具+双倍卡 → 收获 =====
    summary = []
    ACTION_GAP = 0.5  # 每步之间停顿, 让前端反应
    def gap():
        time.sleep(ACTION_GAP)

    for plot_no in [p["plotNo"] for p in tasks]:
        log(f"\n{'='*60}\n[地块 {plot_no}] 开始")
        # 拉一份最新 plots_data, 取该地块最新状态
        latest = None
        for p in plots_initial:
            if p["plotNo"] == plot_no:
                latest = p; break
        if latest is None:
            log(f"  ⚠ 地块 {plot_no} 找不到 (跳过)"); continue
        cur = latest
        log(f"  crop={cur.get('crop','?')!r}  state={cur['state']}  "
            f"翻地={'有' if plot_has_action(cur,'翻地') else '无'}  收获={'有' if plot_has_action(cur,'收获') else '无'}  "
            f"道具={'有' if plot_has_action(cur,'道具') else '无'}")

        # 步骤 1: 翻地 (如需要)
        if do_tilling and plot_has_action(cur, "翻地"):
            gap()
            r1 = do_action(cdp, plot_no, "翻地")
            summary.append({"plotNo": plot_no, "action": "翻地", "ok": r1["ok"], "reason": r1["reason"]})
            # 翻地后重抓一次 (供下一步判断)
            time.sleep(0.3)
            v = js(cdp, fetch_expr)
            if v.get("ok"):
                np = next((x for x in v["plots"] if x["plotNo"] == plot_no), None)
                if np: cur = np; log(f"  [刷新] crop={cur.get('crop','?')!r}  state={cur['state']}  "
                    f"翻地={'有' if plot_has_action(cur,'翻地') else '无'}  收获={'有' if plot_has_action(cur,'收获') else '无'}  "
                    f"道具={'有' if plot_has_action(cur,'道具') else '无'}")
        else:
            log(f"  [跳过翻地] (do_tilling={do_tilling} 或地块无翻地按钮)")

        # 步骤 2: 仅菠萝 → 道具+双倍卡 (在收获前)
        use_card_done = False
        if cur.get("crop") == "菠萝" and plot_has_action(cur, "收获"):
            if plot_has_action(cur, "道具"):
                gap()
                rc = use_double_xp_card(cdp, plot_no)
                summary.append({"plotNo": plot_no, "action": "双倍卡", "ok": rc["ok"], "reason": rc["reason"]})
                use_card_done = rc["ok"]
                # 重抓, 确保收获按钮还在
                time.sleep(0.3)
                v = js(cdp, fetch_expr)
                if v.get("ok"):
                    np = next((x for x in v["plots"] if x["plotNo"] == plot_no), None)
                    if np: cur = np
            else:
                log(f"  [跳过双倍卡] 地块无'道具'按钮 (可能是已用完/页面变体)")
        else:
            log(f"  [跳过双倍卡] crop={cur.get('crop','?')!r}, 收获按钮={'有' if plot_has_action(cur,'收获') else '无'}")

        # 步骤 3: 收获 (如需要)
        if do_harvest and plot_has_action(cur, "收获"):
            gap()
            r3 = do_action(cdp, plot_no, "收获")
            summary.append({"plotNo": plot_no, "action": "收获", "ok": r3["ok"], "reason": r3["reason"]})
        else:
            log(f"  [跳过收获] (do_harvest={do_harvest} 或地块无收获按钮)")

    # ===== 总结 =====
    log("\n" + "=" * 60)
    log("[总结]")
    succ = sum(1 for s in summary if s["ok"])
    fail = len(summary) - succ
    log(f"  成功: {succ}")
    log(f"  失败: {fail}")
    for s in summary:
        ok = "✓" if s["ok"] else "✗"
        log(f"  {ok} 地块{s['plotNo']} (动作='{s['action']}') "
            f"reason={s.get('reason','?')!r}")

    cdp.close()
    return 0


if __name__ == "__main__":
    # CLI:
    #   --dry-run        只定位不点击 (默认 REAL)
    #   --action A,B,C   处理哪些动作, 默认"翻地,收获" (同一地块先翻地再收获)
    args = sys.argv[1:]
    if "--dry-run" in args:
        DRY_RUN = True
        args.remove("--dry-run")
    if "--action" in args:
        i = args.index("--action")
        if i + 1 < len(args):
            TARGET_ACTIONS = args[i + 1]
            args = args[:i] + args[i+2:]
    # 收菜不决定下次间隔, 始终 30 分钟
    print(f"[+] NEXT_INTERVAL={30*60}  (收菜固定 30 分钟)")
    print(f"[+] TARGET_ACTIONS={TARGET_ACTIONS!r}  DRY_RUN={DRY_RUN}")
    sys.exit(main())
