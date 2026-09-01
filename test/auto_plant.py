#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_plant.py
收完菜后, 在地块上种新菜的逻辑:
  1) 遍历 5 个地块, 对每个空地块(empty) 点 "种植" 按钮
  2) 弹出 div[aria-label="选择种子"] 弹窗
  3) 嵌套两层的 div 里, 找菜单项的文本是"胡萝卜"
  4) 在该菜单项里找 button 文本是"种植", 点击
  5) 失败重试: 节点不存在 / 坐标 0 / 派发异常 → 重新抓弹窗, 重试

用法:
  python auto_plant.py                  # dry-run, 只定位不点
  python auto_plant.py --real           # 真正点击
  python auto_plant.py --seed 胡萝卜   # 改种别的, 默认胡萝卜
  python auto_plant.py --plot 1         # 只处理地块 1
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
    raise SystemExit("缺少依赖: pip install websocket-client")

DEBUG_PORT = int(os.environ.get("FARM_DEBUG_PORT", "9222"))
MAX_RETRY = 2
RETRY_GAP = 1.0
DRY_RUN = False
SEED_NAME = "胡萝卜"  # 默认种子
ONLY_PLOT: Optional[int] = None  # 只处理某个 plot

# 按时段选种子:
#   键 = (起始小时, 结束小时) 半开区间, 起始包含, 结束不包含
#   值 = 种子名
# 时钟走到该区间时, 当次执行自动选对应种子; 其余时段用默认 SEED_NAME
# (CLI --seed 显式指定会覆盖下面所有规则)
SEED_BY_HOUR: Dict[tuple, str] = {
    (0, 1): "菠萝",  # 0:00–0:59 种菠萝 (就这一次), 其他时段种胡萝卜
}

# 种子的下次启动间隔(秒): 走完一次种菜后, 等多久再种下一轮
# 没列出的种子用默认 30 分钟 (1800s)
NEXT_INTERVAL_BY_SEED: Dict[str, int] = {
    "菠萝": 405 * 60,  # 6 小时 45 分 = 405 分钟 = 24300 秒
    # "胡萝卜": 30 * 60,  # 默认 1800, 不写也行
}
DEFAULT_NEXT_INTERVAL = 30 * 60  # 30 分钟


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
        self.url = url; self._id = 0; self._sock = None
    def connect(self): self._sock = create_connection(self.url, timeout=30)
    def close(self):
        try: self._sock.close()
        except: pass
    def send(self, m, p=None, t=30):
        self._id += 1
        self._sock.send(json.dumps({"id": self._id, "method": m, "params": p or {}}))
        self._sock.settimeout(t)
        while True:
            try: d = json.loads(self._sock.recv())
            except: continue
            if d.get("id") == self._id:
                if "error" in d: raise RuntimeError(d["error"])
                return d.get("result", {})


def get_target():
    with urllib.request.urlopen(f"http://localhost:{DEBUG_PORT}/json", timeout=5) as r:
        ts = json.loads(r.read().decode())
    for t in ts:
        if t.get("type") == "page" and "duanwuqiufenmao" in t.get("url", ""):
            return t
    for t in ts:
        if t.get("type") == "page": return t
    raise RuntimeError("no page")


def js(cdp, expr):
    r = cdp.send("Runtime.evaluate", {
        "expression": expr, "returnByValue": True, "userGesture": True,
    })
    if "exceptionDetails" in r:
        raise RuntimeError(f"JS: {r['exceptionDetails'].get('text','')}")
    return r.get("result", {}).get("value")


def build_dispatch_plot_plant_btn_js(plot_no: int) -> str:
    return DISPATCH_PLOT_PLANT_BTN_JS.replace("__PLOTNO__", str(plot_no))


def click_at(cdp, x, y, button="left"):
    cdp.send("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y, "button": button, "buttons": 0,
    })
    cdp.send("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y,
        "button": button, "buttons": 1, "clickCount": 1,
    })
    cdp.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y,
        "button": button, "buttons": 0, "clickCount": 1,
    })


# 直接派发 DOM 事件: click 事件 (绕过鼠标坐标)
# Element UI button 监听 click, 用 dispatchEvent 触发
DISPATCH_CLICK_JS = r"""
((args) => {
  const { selectorXPath, text } = args;
  // 找按钮
  let btn = null;
  // 1) 按 xpath 找
  if (selectorXPath) {
    const r = document.evaluate(selectorXPath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
    btn = r.singleNodeValue;
  }
  // 2) 按 text 找
  if (!btn && text) {
    const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
    // 在 el-dialog 里找含"胡萝卜"菜单项的"种植"按钮
    const overlays = document.querySelectorAll('div.el-overlay');
    for (const ov of overlays) {
      const d = ov.querySelector('div.el-dialog');
      if (!d) continue;
      const rr = d.getBoundingClientRect();
      if (rr.width === 0 || rr.height === 0) continue;
      // 找 span 文本 = "胡萝卜"
      const spans = Array.from(d.querySelectorAll('*'))
        .filter(e => norm(e.innerText) === text && e.children.length === 0);
      for (const span of spans) {
        let el = span;
        for (let i = 0; i < 6; i++) {
          el = el.parentElement;
          if (!el || !d.contains(el)) break;
          const btns = Array.from(el.querySelectorAll(':scope > button'));
          const b = btns.find(b => norm(b.innerText) === '种植' && !b.hasAttribute('disabled'));
          if (b) { btn = b; break; }
        }
        if (btn) break;
      }
      if (btn) break;
    }
  }
  if (!btn) return { ok: false, reason: '未找到目标 button' };
  if (!document.body.contains(btn)) return { ok: false, reason: 'button detached' };

  // 派发完整 click 事件序列 (mousedown / mouseup / click)
  btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window, button: 0 }));
  btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window, button: 0 }));
  btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window, button: 0 }));
  // 同步触发 pointer 事件 (Element UI 也监听 pointer)
  btn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, button: 0 }));
  btn.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true, button: 0 }));

  return { ok: true, tag: btn.tagName, text: btn.innerText, cls: btn.className.slice(0, 60) };
})(CLICKARGS)
"""


# 地块"种植"按钮 DOM 派发: 按 plotNo 找按钮, 派发 click + pointer 事件
# 占位符: __PLOTNO__
DISPATCH_PLOT_PLANT_BTN_JS = r"""
(() => {
  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
  const ad = document.querySelector('.farm-ad-card');
  if (!ad) return { ok: false, reason: 'no ad' };
  const prev = ad.previousElementSibling;
  const second = prev.children[1];
  if (!second) return { ok: false, reason: 'no second' };

  // 不依赖 hash: 从 button("种植") 向上找含"地块 plotNo"的容器
  const plantBtns = Array.from(second.querySelectorAll('button'))
    .filter(b => norm(b.innerText) === '种植' && !b.hasAttribute('disabled'));
  for (const b of plantBtns) {
    let el = b;
    for (let i = 0; i < 8 && el && second.contains(el); i++) {
      const t = norm(el.innerText || '');
      const re = new RegExp(`地块\\s*${__PLOTNO__}(?!\\d)`);
      if (re.test(t)) {
        b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window, button: 0 }));
        b.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
        b.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true }));
        return { ok: true, no: __PLOTNO__ };
      }
      el = el.parentElement;
    }
  }
  return { ok: false, reason: 'plot plant btn not found' };
})()
"""


# ============== JS 片段 ==============

FETCH_PLOTS_JS = r"""
(() => {
  const ad = document.querySelector('.farm-ad-card');
  if (!ad) return { ok: false, reason: 'no ad' };
  const prev = ad.previousElementSibling;
  if (!prev) return { ok: false, reason: 'no prev' };
  const second = prev.children[1];
  if (!second) return { ok: false, reason: 'no second' };

  // 统一去零宽字符 + trim
  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();

  // 不依赖任何 hash. 策略:
  //   1) 找所有 button 文本 = "种植"
  //   2) 从 button 向上找最近的容器: 含"地块N"文本 + 含 button
  //   3) plotNo 从容器 innerText 抓 "地块N"
  const plantBtns = Array.from(second.querySelectorAll('button'))
    .filter(b => norm(b.innerText) === '种植');
  const seen = new Set();
  const plots = [];
  for (const btn of plantBtns) {
    if (btn.hasAttribute('disabled')) continue;

    // 找 plotBox: 向上找同时含"地块N"和 button 的最近容器
    let plotBox = null;
    let el = btn;
    for (let i = 0; i < 8 && el && second.contains(el); i++) {
      const t = norm(el.innerText || '');
      if (/地块\s*\d+/.test(t) && el.querySelectorAll('button').length > 0) {
        plotBox = el;
        break;
      }
      el = el.parentElement;
    }
    if (!plotBox) continue;

    // 从 plotBox 抓 plotNo (取最小数字, 避免 "地块N：地块N" 重复)
    const boxText = norm(plotBox.innerText || '');
    const m = boxText.match(/地块\s*(\d+)/);
    if (!m) continue;
    const plotNo = parseInt(m[1]);
    if (seen.has(plotNo)) continue;
    seen.add(plotNo);

    const cls = (plotBox.className || '').split(/\s+/);
    // 状态文本优先取"地块N：xxx"这种, 没有就用"地块 N"作为标识
    let status = '';
    for (const e of plotBox.querySelectorAll('*')) {
      const t = norm(e.innerText || '');
      if (/地块\s*\d+[：:]\s*\S/.test(t) && e.children.length <= 1) { status = t; break; }
    }
    if (!status) status = `地块 ${plotNo}`;
    const isEmpty = cls.includes('empty') || /空地/.test(status);
    plots.push({
      plotNo,
      state: cls.find(c => ['empty','ripe','care','success','danger'].includes(c)) || '?',
      isEmpty,
      status,
      plantBtn: { cls: btn.getAttribute('class') || '' },
    });
  }
  plots.sort((a, b) => a.plotNo - b.plotNo);
  return { ok: true, plots };
})()
"""


# 找种子弹窗(div[aria-label="选择种子"])的结构
# 弹窗内: 嵌套两层的 div 里, 找含目标 SEED 文本的菜单项
# 菜单项内找 button 文本 = "种植"
# 输出: 菜单项内 button 的坐标 + 菜单项的简短文本(给日志)
LOCATE_SEED_BTN_JS = r"""
((args) => {
  const { seedName } = args;
  // 统一去零宽字符 + trim: \u200b ZWSP, \u200c ZWNJ, \u200d ZWJ, \u2060 WJ, \ufeff ZWNBSP, \u00ad SOFT HYPHEN
  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();

  // 找弹窗
  // 关键: 必须找 el-dialog 节点, 不是 el-overlay-dialog (那是空壳)
  // 策略: 找 [aria-label="选择种子"] 后, 在它往上找 el-dialog 祖先(不一定存在)
  //       再找 el-overlay 容器下所有 el-dialog, 取有 bounding rect 的
  const ariaDiv = document.querySelector('div[aria-label="选择种子"]');

  // 找"真的"弹窗: el-dialog 节点, 且有非 0 bounding rect
  let dialog = null;
  const allOverlays = document.querySelectorAll('div.el-overlay');
  for (const ov of allOverlays) {
    const d = ov.querySelector('div.el-dialog');
    if (!d) continue;
    const r = d.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      // 必须有 button + 文本含菜名
      if (d.querySelectorAll('button').length > 0) {
        dialog = d;
        break;
      }
    }
  }
  if (!dialog && ariaDiv) {
    // fallback: ariaDiv 自身 (有 button 且有非 0 rect)
    const r = ariaDiv.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && ariaDiv.querySelectorAll('button').length > 0) {
      dialog = ariaDiv;
    }
  }
  if (!dialog) return {
    ok: false,
    reason: '弹窗未真正出现(无 el-dialog 有内容)',
    debug: {
      ariaExists: !!ariaDiv,
      ariaRect: ariaDiv ? (() => { const r = ariaDiv.getBoundingClientRect(); return {x: r.left, y: r.top, w: r.width, h: r.height}; })() : null,
      overlaysCount: allOverlays.length,
      overlayDialogsInfo: Array.from(allOverlays).map(ov => {
        const d = ov.querySelector('div.el-dialog');
        if (!d) return { hasDialog: false };
        const r = d.getBoundingClientRect();
        return { hasDialog: true, rect: {x: r.left, y: r.top, w: r.width, h: r.height}, btnCount: d.querySelectorAll('button').length };
      }),
    },
  };
  if (!document.body.contains(dialog)) return { ok: false, reason: '弹窗已 detached' };

  // 调试: 列出弹窗里所有 norm 后的文本
  const allTexts = new Set();
  for (const e of dialog.querySelectorAll('span, div, p')) {
    const t = norm(e.innerText);
    if (t && t.length <= 30) allTexts.add(t);
  }
  const seedTexts = Array.from(allTexts).filter(t => t.includes('胡') || t.includes('萝') || t.includes('卜'));

  // 找含目标 SEED 文本的菜单项: 弹窗内嵌套两层的 div -> 菜单项
  // 实际结构: SPAN(种子名) -> DIV -> SECTION(菜单项)
  const candidates = Array.from(dialog.querySelectorAll('*'))
    .filter(e => norm(e.innerText) === seedName && e.children.length === 0);
  if (candidates.length === 0) {
    // 调试: 弹窗里所有 span/div 的文本长度 == seedName 长度的
    const seedLen = seedName.length;
    const similar = Array.from(dialog.querySelectorAll('*'))
      .filter(e => {
        const t = norm(e.innerText);
        return t.length === seedLen && t.length > 0 && e.children.length === 0;
      })
      .map(e => ({ tag: e.tagName, text: norm(e.innerText), codes: norm(e.innerText).split('').map(c => c.charCodeAt(0).toString(16)).join(',') }))
      .slice(0, 5);
    return { ok: false,
      reason: `未找到文本="${seedName}"的元素`,
      debug: {
        seedLen, seedName,
        allTextsSample: Array.from(allTexts).slice(0, 30),
        similarTexts: similar,
        carrotRelated: seedTexts.slice(0, 10),
        rawInnerTextSample: (dialog.innerText || '').slice(0, 500),
        dialogState: {
          tag: dialog.tagName,
          cls: (dialog.getAttribute('class') || '').slice(0, 100),
          display: getComputedStyle(dialog).display,
          visibility: getComputedStyle(dialog).visibility,
          opacity: getComputedStyle(dialog).opacity,
          childCount: dialog.children.length,
          grandChildrenCount: dialog.querySelectorAll('*').length,
        },
        // 整个页面的弹窗数量(可能有多个)
        allDialogs: document.querySelectorAll('div[aria-label="选择种子"]').length,
      } };
  }

  let menuItem = null;
  let plantBtn = null;
  for (const span of candidates) {
    let el = span;
    for (let i = 0; i < 6; i++) {
      el = el.parentElement;
      if (!el || !dialog.contains(el)) break;
      // SECTION 标签且直接子含 button 文本="种植", 即为菜单项
      const btns = Array.from(el.querySelectorAll(':scope > button'));
      const btn = btns.find(b => norm(b.innerText) === '种植' && !b.hasAttribute('disabled'));
      if (btn) { menuItem = el; plantBtn = btn; break; }
    }
    if (menuItem) break;
  }

  if (!menuItem || !plantBtn) {
    return { ok: false, reason: `找到 ${candidates.length} 个"${seedName}"文本, 但都没找到对应"种植"按钮` };
  }
  if (!document.body.contains(plantBtn)) {
    return { ok: false, reason: '种植按钮已 detached' };
  }

  // 不再判断 off-screen, 直接返回坐标 + 是否在视口内
  const r = plantBtn.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) {
    return { ok: false,
      reason: '种植按钮 0-size',
      debug: {
        menuItemRect: (() => { const m = menuItem.getBoundingClientRect(); return {x: m.left, y: m.top, w: m.width, h: m.height}; })(),
        plantBtnRect: {x: r.left, y: r.top, w: r.width, h: r.height},
        plantBtnCls: plantBtn.getAttribute('class'),
        menuItemCls: menuItem.getAttribute('class'),
        plantBtnStyle: (() => { const s = getComputedStyle(plantBtn); return {display: s.display, visibility: s.visibility, opacity: s.opacity, position: s.position}; })(),
        parentChain: (() => {
          const chain = [];
          let e = plantBtn;
          for (let i = 0; i < 4; i++) {
            e = e.parentElement;
            if (!e) break;
            const rr = e.getBoundingClientRect();
            const s = getComputedStyle(e);
            chain.push({tag: e.tagName, cls: (e.getAttribute('class') || '').slice(0, 60), rect: {x: rr.left, y: rr.top, w: rr.width, h: rr.height}, overflow: s.overflow});
          }
          return chain;
        })(),
        dialogCls: dialog.getAttribute('class'),
        dialogRect: (() => { const d = dialog.getBoundingClientRect(); return {x: d.left, y: d.top, w: d.width, h: d.height}; })(),
      } };
  }

  const x = r.left + r.width / 2;
  const y = r.top + r.height / 2;
  const inViewport = r.top >= 0 && r.bottom <= window.innerHeight
                  && r.left >= 0 && r.right <= window.innerWidth;

  return {
    ok: true, seedName,
    x, y, w: r.width, h: r.height,
    inViewport,
    btnCls: plantBtn.getAttribute('class') || '',
    menuItemCls: menuItem.getAttribute('class') || '',
    menuItemText: norm(menuItem.innerText).replace(/\s+/g, ' ').slice(0, 100),
  };
})(SEEDARGS)
"""


# 滚动: 把 menuItem 滚到视口中心
SCROLL_TO_BTN_JS = r"""
(() => {
  const ariaDiv = document.querySelector('div[aria-label="选择种子"]');
  if (!ariaDiv) return { ok: false, reason: 'no dialog' };
  // 优先 el-dialog
  let dialog = ariaDiv;
  if (ariaDiv.querySelectorAll('button').length === 0) {
    const overlay = ariaDiv.parentElement;
    if (overlay) {
      const realDialog = overlay.querySelector('div.el-dialog');
      if (realDialog) dialog = realDialog;
    }
  }
  if (dialog.querySelectorAll('button').length === 0) {
    return { ok: false, reason: '弹窗内容为空' };
  }
  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
  const seedName = SEEDNAME;

  // 找菜单项
  const spans = Array.from(dialog.querySelectorAll('*'))
    .filter(e => norm(e.innerText) === seedName && e.children.length === 0);
  if (!spans.length) return { ok: false, reason: 'no seed span' };
  const span = spans[0];

  // 向上找菜单项
  let menuItem = null;
  let el = span;
  for (let i = 0; i < 6; i++) {
    el = el.parentElement;
    if (!el || !dialog.contains(el)) break;
    const btns = Array.from(el.querySelectorAll(':scope > button'));
    const btn = btns.find(b => norm(b.innerText) === '种植' && !b.hasAttribute('disabled'));
    if (btn) { menuItem = el; break; }
  }
  if (!menuItem) return { ok: false, reason: 'no menu item' };

  // 弹窗的总高度超过父容器, window.scrollTo 不影响 menuItem 位置
  // 唯一有效: menuItem.scrollIntoView({block:'center'}) —— 它会逐级找可滚祖先并滚
  // 弹窗内部也可能需要滚
  const internalScroller = menuItem.closest('.el-dialog__body');
  // 多次 scrollIntoView 累加, 避免滚过头
  for (let i = 0; i < 3; i++) {
    menuItem.scrollIntoView({ block: 'center' });
    // 弹窗内部: 把 button 滚到内部容器中心
    if (internalScroller) {
      const btn = menuItem.querySelector(':scope > button');
      if (btn) {
        const bRect = btn.getBoundingClientRect();
        if (bRect.height > 0 && bRect.width > 0) {
          // 让 button 顶部在 internalScroller 视口中心
          const spRect = internalScroller.getBoundingClientRect();
          const offsetTop = bRect.top - spRect.top + internalScroller.scrollTop;
          internalScroller.scrollTop = Math.max(0, offsetTop - (internalScroller.clientHeight / 2) + (bRect.height / 2));
        }
      }
    }
    // 校验: button 还在视口 + 有大小?
    const btn2 = menuItem.querySelector(':scope > button');
    if (btn2) {
      const r = btn2.getBoundingClientRect();
      if (r.height > 0 && r.width > 0 && r.top >= 0 && r.bottom <= window.innerHeight) {
        break;  // 完美
      }
    }
  }

  return { ok: true, scrolled: true, scrollY: window.scrollY, internalST: internalScroller ? internalScroller.scrollTop : 0 };
})()
"""


# 道具弹窗里找"双倍经验卡"的"使用"按钮
# 弹窗结构: el-dialog 标题"使用道具", 多个菜单项, 每项含"道具名"+"消耗/等级"+"使用"按钮
# 不依赖 hash, 按文本匹配
LOCATE_PROP_USE_BTN_JS = r"""
(() => {
  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();

  // 找"真的"弹窗: el-overlay > el-dialog, 有非 0 rect + 有 button
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
  if (!document.body.contains(dialog)) return { ok: false, reason: '道具弹窗 detached' };

  // 找含"双倍经验卡"文本的叶子节点
  const candidates = Array.from(dialog.querySelectorAll('*'))
    .filter(e => norm(e.innerText) === '双倍经验卡' && e.children.length === 0);
  if (candidates.length === 0) {
    return {
      ok: false,
      reason: '道具弹窗里没找到"双倍经验卡"文本',
      debug: {
        rawText: (dialog.innerText || '').slice(0, 500),
        allTexts: Array.from(new Set(Array.from(dialog.querySelectorAll('span, div, p, button'))
          .map(e => norm(e.innerText)).filter(t => t && t.length <= 30))).slice(0, 30),
      },
    };
  }

  // 从"双倍经验卡"向上找最近的菜单项: 含直接子 button 文本 = "使用"
  let menuItem = null;
  let useBtn = null;
  for (const span of candidates) {
    let el = span;
    for (let i = 0; i < 6; i++) {
      el = el.parentElement;
      if (!el || !dialog.contains(el)) break;
      const btns = Array.from(el.querySelectorAll(':scope > button'));
      const btn = btns.find(b => norm(b.innerText) === '使用' && !b.hasAttribute('disabled'));
      if (btn) { menuItem = el; useBtn = btn; break; }
    }
    if (menuItem) break;
  }
  if (!menuItem || !useBtn) {
    return { ok: false, reason: `找到 ${candidates.length} 个"双倍经验卡"文本但都没找到"使用"按钮` };
  }
  if (!document.body.contains(useBtn)) return { ok: false, reason: '使用按钮 detached' };

  const r = useBtn.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return { ok: false, reason: '使用按钮 0-size' };
  const x = r.left + r.width / 2;
  const y = r.top + r.height / 2;
  const inViewport = r.top >= 0 && r.bottom <= window.innerHeight
                  && r.left >= 0 && r.right <= window.innerWidth;
  return {
    ok: true,
    x, y, w: r.width, h: r.height,
    inViewport,
    btnCls: useBtn.getAttribute('class') || '',
    menuItemText: norm(menuItem.innerText).replace(/\s+/g, ' ').slice(0, 100),
  };
})()
"""


# 找地块"道具"按钮的坐标(用来触发弹窗)
# 逻辑和 LOCATE_PLOT_PLANT_BTN_JS 一模一样: 文本=道具 的 button, 向上找含"地块 plotNo"的容器
LOCATE_PLOT_PROP_BTN_JS = r"""
((args) => {
  const { plotNo } = args;
  const ad = document.querySelector('.farm-ad-card');
  if (!ad) return { ok: false, reason: 'no ad' };
  const prev = ad.previousElementSibling;
  const second = prev.children[1];
  if (!second) return { ok: false, reason: 'no second' };

  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();

  // 关键: 不能直接遍历所有按钮, 因为祖先链里可能含更高级别含"地块 plotNo" 的祖先(整个 ARTICLE 都有所有地块号)
  // 思路: 先找每个按钮所属的地块(plotBox = 向上找最近的同时含"地块 N"+含 button 的容器, N 最小),
  //      然后按 plotBox 内的按钮文本="道具" 且 plotBox 的 plotNo 匹配 来定位
  const allBtns = Array.from(second.querySelectorAll('button'))
    .filter(b => norm(b.innerText) === '道具' && !b.hasAttribute('disabled'));
  let btn = null;
  const seenPlotBoxes = new Set();
  // 收集所有"按钮 → 地块号"映射 (同一地块可能有多个按钮, 只记一次 plotBox)
  const buttonToPlotNo = new Map();
  for (const b of allBtns) {
    let plotBox = null;
    let el = b;
    for (let i = 0; i < 6 && el && second.contains(el); i++) {
      const t = norm(el.innerText || '');
      if (/地块\s*\d+/.test(t) && el.querySelectorAll('button').length > 0) {
        plotBox = el; break;
      }
      el = el.parentElement;
    }
    if (!plotBox) continue;
    if (seenPlotBoxes.has(plotBox)) continue;  // 同一地块多个按钮只算一次
    seenPlotBoxes.add(plotBox);
    const boxText = norm(plotBox.innerText || '');
    // 找 plotBox 内**最小**的地块号 (避免外层 ARTICLE 含所有地块)
    let minPlotNo = Infinity;
    for (const m of boxText.matchAll(/地块\s*(\d+)/g)) {
      const n = parseInt(m[1]);
      if (n < minPlotNo) minPlotNo = n;
    }
    if (minPlotNo === Infinity) continue;
    buttonToPlotNo.set(b, minPlotNo);
  }
  // 按 plotNo 找匹配按钮
  for (const [b, n] of buttonToPlotNo) {
    if (n === plotNo) { btn = b; break; }
  }
  if (!btn) {
    const dbg = Array.from(buttonToPlotNo.entries()).map(([b, n]) => ({ btnText: norm(b.innerText), plotNo: n }));
    return { ok: false, reason: `plot ${plotNo} 无"道具"按钮(候选: ${JSON.stringify(dbg)})` };
  }
  if (!document.body.contains(btn)) return { ok: false, reason: '道具按钮 detached' };

  const r = btn.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return { ok: false, reason: '按钮 0-size' };
  return {
    ok: true, plotNo,
    x: r.left + r.width / 2, y: r.top + r.height / 2,
    w: r.width, h: r.height,
  };
})(PROPARGS)
"""


# 找地块"种植"按钮的坐标(用来触发弹窗)
LOCATE_PLOT_PLANT_BTN_JS = r"""
((args) => {
  const { plotNo } = args;
  const ad = document.querySelector('.farm-ad-card');
  if (!ad) return { ok: false, reason: 'no ad' };
  const prev = ad.previousElementSibling;
  const second = prev.children[1];
  if (!second) return { ok: false, reason: 'no second' };

  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();

  // 不依赖 hash: 从 button("种植") 向上找含"地块 plotNo"的容器
  const plantBtns = Array.from(second.querySelectorAll('button'))
    .filter(b => norm(b.innerText) === '种植' && !b.hasAttribute('disabled'));
  let btn = null;
  for (const b of plantBtns) {
    let el = b;
    for (let i = 0; i < 8 && el && second.contains(el); i++) {
      const t = norm(el.innerText || '');
      const re = new RegExp(`地块\\s*${plotNo}(?!\\d)`);
      if (re.test(t)) { btn = b; break; }
      el = el.parentElement;
    }
    if (btn) break;
  }
  if (!btn) {
    // 调试: 列出每个"种植"按钮祖先链的 innerText
    const dbg = plantBtns.map((b, idx) => {
      const chain = [];
      let e = b;
      for (let i = 0; i < 6; i++) {
        e = e.parentElement;
        if (!e || !second.contains(e)) break;
        chain.push({ depth: i+1, tag: e.tagName, text: norm(e.innerText).slice(0, 60) });
      }
      return { idx, btnText: norm(b.innerText), chain };
    });
    return { ok: false, reason: `plot ${plotNo} 无"种植"按钮(共 ${plantBtns.length} 个候选种植按钮)`, debug: dbg };
  }
  if (!document.body.contains(btn)) return { ok: false, reason: '种植按钮 detached' };

  const r = btn.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return { ok: false, reason: '按钮 0-size' };
  return {
    ok: true, plotNo,
    x: r.left + r.width / 2, y: r.top + r.height / 2,
    w: r.width, h: r.height,
  };
})(PLOTARGS)
"""


def main():
    log_lines: list = []
    target = get_target()
    cdp = CDPClient(target["webSocketDebuggerUrl"])
    cdp.connect()
    cdp.send("Runtime.enable")
    cdp.send("Page.enable")
    print(f"[+] {target.get('url','')}")
    log_lines.append(f"# 自动种菜日志  URL: {target.get('url','')}")
    log_lines.append(f"# 模式: {'DRY-RUN' if DRY_RUN else 'REAL'}  种子: {SEED_NAME}  地块过滤: {ONLY_PLOT or '全部'}")

    def log(msg):
        print(msg); log_lines.append(msg)

    # 1) 抓地块
    plots_res = js(cdp, FETCH_PLOTS_JS)
    if not plots_res.get("ok"):
        log(f"[!] 抓地块失败: {plots_res}")
        cdp.close(); return 1

    log(f"\n[+] 共 {len(plots_res['plots'])} 个地块:")
    for p in plots_res["plots"]:
        log(f"  地块{p['plotNo']} state={p['state']:6s} isEmpty={p['isEmpty']}  "
            f"种植={'有' if p['plantBtn'] else '无':3s}  status={p['status'][:50]!r}")

    # 2) 筛选可种的地块(empty + 有"种植"按钮)
    to_plant = [p for p in plots_res["plots"] if p["isEmpty"] and p["plantBtn"]]
    if ONLY_PLOT is not None:
        to_plant = [p for p in to_plant if p["plotNo"] == ONLY_PLOT]
    log(f"\n[+] 需种菜地块: {len(to_plant)} 个 {[p['plotNo'] for p in to_plant]}")
    if not to_plant:
        log("  没有空地块, 退出")
        # 没种菜, 默认 30 分钟后再启动
        nxt = DEFAULT_NEXT_INTERVAL
        log(f"[+] NEXT_INTERVAL={nxt}  (没空地块, 下次 {nxt/60:.0f} 分钟后启动)")
        # 没种出菜, 下次收菜动作也用默认
        log(f"[+] NEXT_HARVEST_ACTIONS=翻地,收获")
        cdp.close(); return 0

    summary = []
    for p in to_plant:
        plot_no = p["plotNo"]
        log(f"\n========== 地块 {plot_no} 流程 ==========")
        # --- 步骤 A0: 强制重置弹窗状态 ---
        # 每块地之前先确保没有残留弹窗(防止 Vue 组件实例混乱)
        # 通过多次触发 ESC 关闭 Element UI 弹窗
        def force_close_dialogs():
            js(cdp, r'''
(() => {
  // 模拟 ESC 关闭
  for (let i = 0; i < 3; i++) {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true, cancelable: true }));
    document.dispatchEvent(new KeyboardEvent('keyup', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true, cancelable: true }));
  }
  // 找所有 el-overlay-dialog(残留空壳) 主动点击 mask 关闭
  const allOverlays = document.querySelectorAll('div.el-overlay');
  for (const ov of allOverlays) {
    // 找 .el-overlay-dialog 节点(就是 mask)
    const mask = ov.querySelector('div.el-overlay-dialog');
    if (mask) {
      // 点 mask
      mask.click();
    }
    // 找 dialog 内的关闭按钮 (.el-dialog__headerbtn 或 close)
    const dlg = ov.querySelector('div.el-dialog');
    if (dlg) {
      const closeBtn = dlg.querySelector('.el-dialog__headerbtn, .el-dialog__close');
      if (closeBtn) closeBtn.click();
      // 也找文本 = "取消" 的按钮
      const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
      const cancelBtns = Array.from(dlg.querySelectorAll('button')).filter(b => {
        const t = norm(b.innerText);
        return t === '取消' || t === '关闭' || t === 'Cancel';
      });
      for (const b of cancelBtns) b.click();
    }
  }
  return true;
})()
            ''')
        force_close_dialogs()
        time.sleep(0.5)

        # --- 步骤 A: 触发弹窗 ---
        # 检查弹窗是否"真的开": 有 el-dialog 节点, 且有 button, 且 rect 非 0
        def dialog_real_open():
            return js(cdp, r'''
(() => {
  const overlays = document.querySelectorAll('div.el-overlay');
  for (const ov of overlays) {
    const d = ov.querySelector('div.el-dialog');
    if (!d) continue;
    const r = d.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && d.querySelectorAll('button').length > 0) {
      return true;
    }
  }
  return false;
})()
            ''')
        def has_stale_overlay():
            """即使没真弹窗, 只要有 el-overlay 残留空壳也算"需要重置" """
            return js(cdp, r'''
(() => {
  return document.querySelectorAll('div.el-overlay').length > 0;
})()
            ''')
        if dialog_real_open():
            # 弹窗已开, 不一定是为本块地开的(可能是上次的/为别的地块开的)
            # 强制关闭 + 重置, 然后强制重开本块地的弹窗
            log(f"[A] 弹窗已开, 不确定是为本块地, 强制重置后重开")
            force_close_dialogs()
            time.sleep(0.5)
            # 走触发流程
            for attempt in range(1, MAX_RETRY + 1):
                expr_a = LOCATE_PLOT_PLANT_BTN_JS.replace("PLOTARGS", json.dumps({"plotNo": plot_no}))
                pos = js(cdp, expr_a)
                if not pos.get("ok"):
                    log(f"  ✗ 定位失败: {pos.get('reason')} (尝试 {attempt})")
                    if pos.get("debug"):
                        log(f"  ── 种植按钮祖先链 ──")
                        for d in pos["debug"]:
                            log(f"     btn[{d['idx']}] '{d['btnText']}' 链:")
                            for c in d.get("chain", []):
                                log(f"       depth={c['depth']} <{c['tag']}> text={c['text']!r}")
                    if attempt < MAX_RETRY: time.sleep(RETRY_GAP); continue
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "A", "reason": pos.get("reason")})
                    break
                log(f"  ✓ 定位地块{plot_no}种植按钮 @({pos['x']:.1f},{pos['y']:.1f})")
                if not DRY_RUN:
                    a_click2 = js(cdp, build_dispatch_plot_plant_btn_js(plot_no))
                    if a_click2.get("ok"):
                        log(f"  → DOM 派发地块{plot_no}'种植'成功")
                    else:
                        click_at(cdp, pos["x"], pos["y"])
                        log(f"  → 坐标点击已派发")
                time.sleep(0.6)
                if dialog_real_open():
                    log(f"  ✓ 弹窗已出现且有内容")
                    break
                else:
                    log(f"  ⚠ 弹窗未出现或仍为空 (尝试 {attempt})")
                    if attempt < MAX_RETRY: time.sleep(RETRY_GAP); continue
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "A", "reason": "弹窗未出现"})
                    break
            else:
                continue
        elif has_stale_overlay():
            log(f"[A] 有残留 el-overlay 空壳, 强制重置后重开弹窗")
            force_close_dialogs()
            time.sleep(0.5)
            # 递归 / 后续走触发流程
            for attempt in range(1, MAX_RETRY + 1):
                expr_a = LOCATE_PLOT_PLANT_BTN_JS.replace("PLOTARGS", json.dumps({"plotNo": plot_no}))
                pos = js(cdp, expr_a)
                if not pos.get("ok"):
                    log(f"  ✗ 定位失败: {pos.get('reason')} (尝试 {attempt})")
                    if attempt < MAX_RETRY: time.sleep(RETRY_GAP); continue
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "A", "reason": pos.get("reason")})
                    break
                log(f"  ✓ 定位地块{plot_no}种植按钮 @({pos['x']:.1f},{pos['y']:.1f})")
                if not DRY_RUN:
                    a_click2 = js(cdp, build_dispatch_plot_plant_btn_js(plot_no))
                    if a_click2.get("ok"):
                        log(f"  → DOM 派发地块{plot_no}'种植'成功")
                    else:
                        click_at(cdp, pos["x"], pos["y"])
                        log(f"  → 坐标点击已派发")
                time.sleep(0.6)
                if dialog_real_open():
                    log(f"  ✓ 弹窗已出现且有内容")
                    break
                else:
                    log(f"  ⚠ 弹窗未出现或仍为空 (尝试 {attempt})")
                    if attempt < MAX_RETRY: time.sleep(RETRY_GAP); continue
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "A", "reason": "弹窗未出现"})
                    break
            else:
                continue
        else:
            log(f"[A] 弹窗未开/为空, 点地块{plot_no}的'种植'按钮触发")
            for attempt in range(1, MAX_RETRY + 1):
                expr_a = LOCATE_PLOT_PLANT_BTN_JS.replace("PLOTARGS", json.dumps({"plotNo": plot_no}))
                pos = js(cdp, expr_a)
                if not pos.get("ok"):
                    log(f"  ✗ 定位失败: {pos.get('reason')} (尝试 {attempt})")
                    if attempt < MAX_RETRY: time.sleep(RETRY_GAP); continue
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "A", "reason": pos.get("reason")})
                    break
                log(f"  ✓ 定位地块{plot_no}种植按钮 @({pos['x']:.1f},{pos['y']:.1f})")
                if not DRY_RUN:
                    a_click2 = js(cdp, build_dispatch_plot_plant_btn_js(plot_no))
                    if a_click2.get("ok"):
                        log(f"  → DOM 派发地块{plot_no}'种植'成功")
                    else:
                        click_at(cdp, pos["x"], pos["y"])
                        log(f"  → 坐标点击已派发")
                # 等弹窗(用 real_open 判断)
                time.sleep(0.6)
                if dialog_real_open():
                    log(f"  ✓ 弹窗已出现且有内容")
                    break
                else:
                    log(f"  ⚠ 弹窗未出现或仍为空 (尝试 {attempt})")
                    if attempt < MAX_RETRY: time.sleep(RETRY_GAP); continue
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "A", "reason": "弹窗未出现"})
                    break
            else:
                continue  # 全部尝试都失败

        # --- 步骤 B: 找"胡萝卜"菜单项的"种植"按钮 ---
        ok_seed = False
        for attempt in range(1, MAX_RETRY + 1):
            log(f"[B] 第 {attempt}/{MAX_RETRY} 次定位 '{SEED_NAME}' 菜单项的种植按钮")
            expr_b = LOCATE_SEED_BTN_JS.replace("SEEDARGS", json.dumps({"seedName": SEED_NAME}))
            seed_pos = js(cdp, expr_b)
            if not seed_pos.get("ok"):
                log(f"  ✗ 失败: {seed_pos.get('reason')}")
                if seed_pos.get("debug"):
                    dbg = seed_pos["debug"]
                    log(f"  ── debug ──")
                    # candidates 找不到时的 debug
                    if "dialogState" in dbg:
                        ds = dbg.get("dialogState", {})
                        log(f"     弹窗状态: display={ds.get('display')} visibility={ds.get('visibility')} opacity={ds.get('opacity')}  kids={ds.get('childCount')}/{ds.get('grandChildrenCount')}")
                        log(f"     弹窗 class: {ds.get('cls')!r}")
                        log(f"     弹窗 raw innerText (前 500):")
                        for line in (dbg.get("rawInnerTextSample") or "").split("\n"):
                            log(f"       {line}")
                        log(f"     弹窗里 norm 后含'胡/萝/卜'的文本: {dbg.get('carrotRelated')}")
                        log(f"     弹窗里前 30 个 norm 文本: {dbg.get('allTextsSample')}")
                        log(f"     与种子名等长的样本(前 5):")
                        for s in dbg.get("similarTexts", []):
                            log(f"       <{s['tag']}> text={s['text']!r}  codes={s['codes']}")
                    # 0-size 时的 debug
                    elif "plantBtnRect" in dbg:
                        log(f"     菜单项: cls={dbg.get('menuItemCls')!r}  rect={dbg.get('menuItemRect')}")
                        log(f"     按钮:   cls={dbg.get('plantBtnCls')!r}  rect={dbg.get('plantBtnRect')}")
                        log(f"     按钮样式: {dbg.get('plantBtnStyle')}")
                        log(f"     弹窗 cls: {dbg.get('dialogCls')!r}  rect={dbg.get('dialogRect')}")
                        log(f"     按钮父链 (往上 4 层):")
                        for i, p in enumerate(dbg.get("parentChain", [])):
                            log(f"       [{i}] <{p['tag']}> cls={p['cls']!r}  rect={p['rect']}  overflow={p['overflow']}")
                    else:
                        log(f"     (无 debug 字段)")
                if attempt < MAX_RETRY:
                    time.sleep(RETRY_GAP)
                    continue
                summary.append({"plotNo": plot_no, "ok": False, "stage": "B",
                                "reason": seed_pos.get("reason")})
                break
            log(f"  ✓ 找到  菜单='{seed_pos['menuItemText'][:60]}'  "
                f"inViewport={seed_pos.get('inViewport')}")
            log(f"     按钮='种植' @({seed_pos['x']:.1f},{seed_pos['y']:.1f}) "
                f"{seed_pos['w']:.0f}×{seed_pos['h']:.0f}")

            # 不在视口内 → 滚动
            if not seed_pos.get("inViewport"):
                log(f"  → 按钮不在视口内, 滚到中心")
                scroll_expr = SCROLL_TO_BTN_JS.replace("SEEDNAME", json.dumps(SEED_NAME))
                scroll_res = js(cdp, scroll_expr)
                if not scroll_res.get("ok"):
                    log(f"  ⚠ 滚动失败: {scroll_res.get('reason')}")
                else:
                    log(f"  ✓ 已滚动, scrollTop={scroll_res.get('scrollTop')}")
                    time.sleep(0.3)
                    # 重新拿坐标
                    seed_pos2 = js(cdp, expr_b)
                    if seed_pos2.get("ok"):
                        seed_pos = seed_pos2
                        log(f"  → 重新定位  inViewport={seed_pos.get('inViewport')}  "
                            f"@({seed_pos['x']:.1f},{seed_pos['y']:.1f})")
                    else:
                        log(f"  ⚠ 重新定位失败: {seed_pos2.get('reason')}")

            if DRY_RUN:
                log(f"  → [DRY-RUN] 跳过真实派发")
                ok_seed = True
                break
            # 优先用 DOM 派发 click (绕过坐标遮挡问题)
            click_res = js(cdp, DISPATCH_CLICK_JS.replace("CLICKARGS",
                json.dumps({"text": SEED_NAME, "selectorXPath": None})))
            if click_res.get("ok"):
                log(f"  → DOM 派发 click 成功  btn.text='{click_res.get('text')}'")
            else:
                log(f"  ⚠ DOM 派发失败: {click_res.get('reason')}, 回退到坐标点击")
                try:
                    click_at(cdp, seed_pos["x"], seed_pos["y"])
                    log(f"  → 坐标点击已派发")
                except Exception as e:
                    log(f"  ✗ 派发失败: {e}")
                    if attempt < MAX_RETRY: time.sleep(RETRY_GAP); continue
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "B-pickup",
                                    "reason": str(e)})
                    break
            # 强制休眠 2s, 等 Vue 处理 click + 弹窗关闭动画 + API 响应
            log(f"  → 强制休眠 2s, 等 Vue 关闭弹窗...")
            time.sleep(2.0)
            # 验证: 弹窗是否消失(说明点中了)
            still_open = dialog_real_open()
            if not still_open:
                log(f"  ✓ 弹窗已消失, 种菜成功")
                ok_seed = True
                break
            else:
                log(f"  ⚠ 弹窗还在, 可能点错了或被遮挡 (尝试 {attempt})")
                if attempt < MAX_RETRY: time.sleep(RETRY_GAP); continue
                summary.append({"plotNo": plot_no, "ok": False, "stage": "B-verify",
                                "reason": "弹窗未消失"})
                break
        if ok_seed:
            summary.append({"plotNo": plot_no, "ok": True, "stage": "B"})

            # ==== 阶段 C: 用道具(双倍经验卡) — 仅本次种菠萝时触发 ====
            if SEED_NAME == "菠萝":
                log(f"\n[C] 地块{plot_no} 种菠萝成功, 开始用双倍经验卡")

                # C0: 关掉残留弹窗(可能还有 el-overlay 空壳)
                force_close_dialogs()

                # C1: 找地块 plotNo 的"道具"按钮
                c_attempt = 0
                c_locate_ok = False
                while c_attempt < MAX_RETRY:
                    c_attempt += 1
                    expr_c1 = LOCATE_PLOT_PROP_BTN_JS.replace(
                        "PROPARGS", json.dumps({"plotNo": plot_no})
                    )
                    prop_pos = js(cdp, expr_c1)
                    if not prop_pos.get("ok"):
                        log(f"  ✗ 定位道具按钮失败: {prop_pos.get('reason')} (尝试 {c_attempt})")
                        time.sleep(RETRY_GAP); continue
                    log(f"  ✓ 定位地块{plot_no}道具按钮 @({prop_pos['x']:.1f},{prop_pos['y']:.1f})")
                    c_locate_ok = True; break
                if not c_locate_ok:
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "C-locate",
                                    "reason": "道具按钮定位失败"})
                    break

                if DRY_RUN:
                    log(f"  → [DRY-RUN] 跳过 C2 派发 / C3-C5 真弹窗流程")
                    continue  # 阶段 B 已成功记, 阶段 C 视为 OK, 进入下一地块

                # C2: 点击道具按钮 (派发 click)
                c_dispatched = False
                for _ in range(MAX_RETRY):
                    expr_c2 = (r"""
                    (() => {
                      const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
                      const ad = document.querySelector('.farm-ad-card');
                      const second = ad.previousElementSibling.children[1];
                      const allBtns = Array.from(second.querySelectorAll('button'))
                        .filter(b => norm(b.innerText) === '道具' && !b.hasAttribute('disabled'));
                      const seen = new Set();
                      for (const b of allBtns) {
                        let plotBox = null, el = b;
                        for (let i = 0; i < 6 && el && second.contains(el); i++) {
                          const t = norm(el.innerText || '');
                          if (/地块\s*\d+/.test(t) && el.querySelectorAll('button').length > 0) {
                            plotBox = el; break;
                          }
                          el = el.parentElement;
                        }
                        if (!plotBox || seen.has(plotBox)) continue;
                        seen.add(plotBox);
                        const boxText = norm(plotBox.innerText || '');
                        let minN = Infinity;
                        for (const m of boxText.matchAll(/地块\s*(\d+)/g)) {
                          const n = parseInt(m[1]); if (n < minN) minN = n;
                        }
                        if (minN === """ + str(plot_no) + r""") {
                          b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window, button: 0 }));
                          b.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
                          b.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true }));
                          return { ok: true };
                        }
                      }
                      return { ok: false, reason: 'not found' };
                    })()
                    """)
                    disp = js(cdp, expr_c2)
                    if not disp.get("ok"):
                        log(f"  ✗ 派发道具点击失败: {disp.get('reason')}")
                        time.sleep(RETRY_GAP); continue
                    c_dispatched = True; break
                if not c_dispatched:
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "C-dispatch",
                                    "reason": "道具按钮派发失败"})
                    break

                # C3: 等弹窗 + 找"双倍经验卡"+"使用"按钮
                time.sleep(1.5)
                c_use_pos = js(cdp, LOCATE_PROP_USE_BTN_JS)
                if not c_use_pos.get("ok"):
                    log(f"  ✗ 道具弹窗里没找到双倍经验卡: {c_use_pos.get('reason')}")
                    if c_use_pos.get("debug"):
                        log(f"  ── 弹窗文本: {c_use_pos['debug'].get('rawText','')!r}")
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "C-pickup",
                                    "reason": c_use_pos.get("reason", "双倍经验卡未找到")})
                    break
                log(f"  ✓ 找到双倍经验卡: {c_use_pos.get('menuItemText','')!r}")
                log(f"  ✓ 使用按钮 @({c_use_pos['x']:.1f},{c_use_pos['y']:.1f}) inViewport={c_use_pos.get('inViewport')}")

                # C4: 点击"使用"按钮 (派发 click)
                expr_c4 = (r"""
                (() => {
                  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
                  // 找"双倍经验卡"菜单项里的"使用"按钮
                  const candidates = Array.from(document.querySelectorAll('*'))
                    .filter(e => norm(e.innerText) === '双倍经验卡' && e.children.length === 0);
                  for (const span of candidates) {
                    let el = span;
                    for (let i = 0; i < 6; i++) {
                      el = el.parentElement;
                      if (!el) break;
                      const btns = Array.from(el.querySelectorAll(':scope > button'));
                      const btn = btns.find(b => norm(b.innerText) === '使用' && !b.hasAttribute('disabled'));
                      if (btn) {
                        btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window, button: 0 }));
                        btn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
                        btn.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true }));
                        return { ok: true };
                      }
                    }
                  }
                  return { ok: false, reason: 'not found' };
                })()
                """)
                c_use_dispatched = False
                for _ in range(MAX_RETRY):
                    disp = js(cdp, expr_c4)
                    if not disp.get("ok"):
                        log(f"  ✗ 派发使用按钮失败: {disp.get('reason')}")
                        time.sleep(RETRY_GAP); continue
                    c_use_dispatched = True; break
                if not c_use_dispatched:
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "C-dispatch-use",
                                    "reason": "使用按钮派发失败"})
                    break

                # C5: 等 2s + 验证弹窗消失(说明道具被用掉了)
                time.sleep(2)
                verify_expr = r"""
                (() => {
                  const norm = (s) => (s || '').replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').trim();
                  // 找真弹窗
                  const allOverlays = document.querySelectorAll('div.el-overlay');
                  for (const ov of allOverlays) {
                    const d = ov.querySelector('div.el-dialog');
                    if (!d) continue;
                    const r = d.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && d.querySelectorAll('button').length > 0) {
                      // 真弹窗还在
                      return { gone: false, texts: Array.from(d.querySelectorAll('span,div,p')).slice(0, 5).map(e => norm(e.innerText).slice(0, 30)) };
                    }
                  }
                  return { gone: true };
                })()
                """
                verify = js(cdp, verify_expr)
                if verify.get("gone"):
                    log(f"  ✓ 道具弹窗已消失, 双倍经验卡使用成功")
                else:
                    log(f"  ⚠ 道具弹窗还在, 可能点错或被遮挡")
                    summary.append({"plotNo": plot_no, "ok": False, "stage": "C-verify",
                                    "reason": "弹窗未消失"})
                    break

    # ===== 总结 =====
    log("\n" + "=" * 60)
    log("[总结]")
    succ = sum(1 for s in summary if s["ok"])
    fail = len(summary) - succ
    log(f"  成功: {succ}  失败: {fail}")
    for s in summary:
        log(f"  {'✓' if s['ok'] else '✗'} 地块{s['plotNo']}  stage={s.get('stage')}  reason={s.get('reason','-')}")

    # 决定下次启动间隔:
    #   本次至少种成功 1 个 → 按 SEED_NAME 查 NEXT_INTERVAL_BY_SEED
    #   本次没种出菜 (没空地 / 全失败) → 默认 30 分钟
    if succ > 0:
        nxt = NEXT_INTERVAL_BY_SEED.get(SEED_NAME, DEFAULT_NEXT_INTERVAL)
    else:
        nxt = DEFAULT_NEXT_INTERVAL
    log(f"[+] NEXT_INTERVAL={nxt}  (种 {SEED_NAME} {'成功 '+str(succ)+' 块' if succ>0 else '本次没种出菜'}, 下次 {nxt/60:.0f} 分钟后启动)")

    # 决定下次收菜要处理的动作: 仅本次种菠萝时, 加"道具"动作
    # (scheduler.py 会读这行, 加到下次 auto_harvest.py 的 --action 里)
    if succ > 0 and SEED_NAME == "菠萝":
        # 收菜时如果页面有"道具"按钮就派发点击; 没有就跳过(不会硬找)
        log(f"[+] NEXT_HARVEST_ACTIONS=道具,翻地,收获  (种了菠萝, 下次收菜顺带按道具按钮)")
    else:
        log(f"[+] NEXT_HARVEST_ACTIONS=翻地,收获")

    cdp.close()
    return 0


if __name__ == "__main__":
    args = ["--real"]
    if "--real" in args:
        DRY_RUN = False; args.remove("--real")
    explicit_seed = None  # CLI 显式指定时, 跳过按时间选种子
    if "--seed" in args:
        i = args.index("--seed")
        if i + 1 < len(args):
            explicit_seed = args[i + 1]
            SEED_NAME = explicit_seed
            args = args[:i] + args[i+2:]
    if "--plot" in args:
        i = args.index("--plot")
        if i + 1 < len(args):
            ONLY_PLOT = int(args[i + 1])
            args = args[:i] + args[i+2:]

    # CLI 没显式 --seed 时, 按当前小时查 SEED_BY_HOUR
    if explicit_seed is None:
        from datetime import datetime
        h = datetime.now().hour
        for (lo, hi), seed in SEED_BY_HOUR.items():
            if lo <= h < hi:
                SEED_NAME = seed
                print(f"[+] 时段 {lo:02d}:00–{hi:02d}:59 → 自动选种子: {SEED_NAME}")
                break

    sys.exit(main())
