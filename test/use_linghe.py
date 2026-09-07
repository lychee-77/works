"""
use_linghe.py
通过 Edge 的远程调试端口 (CDP) 连接到 https://www.duanwuqiufenmao.top/qpet/inventory,
1) 点击 "灵核" 标签页
2) 反复点击所有 inv-card 中的 "使用" 按钮, 直至没有可点为止 (每 1.5 秒一次)

使用前:
1. 用以下命令以远程调试模式启动 Edge (一次性):
   start msedge.exe --remote-debugging-port=9555 --remote-allow-origins=* https://www.duanwuqiufenmao.top/qpet/inventory
   (如果 Edge 已经在跑, 可以省略; 端口 9555 已被占用说明 Edge 已就绪)
2. 安装依赖:  pip install websocket-client
3. 运行:       python use_linghe.py
"""

import json
import sys
import time
import urllib.request
from websocket import create_connection  # pip install websocket-client

CDP_HTTP = "http://127.0.0.1:9555/json"
INVENTORY_URL = "https://www.duanwuqiufenmao.top/qpet/inventory"
CLICK_INTERVAL = 1.5  # 两次点击之间的间隔 (秒)
# 灵核 Tab 选择器 (HTML 原样, 与页面一致)
LINGHE_TAB_SELECTOR = 'button.inv-tab.active'
# 灵核 Tab 文本前缀 (用于确认切换正确)
LINGHE_TAB_TEXT = "灵核"
# 灵核卡片容器
INV_GRID_SELECTOR = "div.inv-grid"
# 单张灵核卡片 (bead = 灵核碎片)
INV_CARD_SELECTOR = "div.inv-card-bead"
# 卡片中的 "使用" 按钮
USE_BTN_SELECTOR = "button.inv-btn-use"
# 上架按钮 (用于判断是否还有可使用物品; 上架按钮存在说明物品未使用完)
SELL_BTN_SELECTOR = "button.inv-btn-sell"


def list_pages() -> list[dict]:
    """列出 Edge 当前所有顶层标签页 (type=page 且没有 parentId)."""
    try:
        with urllib.request.urlopen(CDP_HTTP, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] 读取 {CDP_HTTP} 失败: {e}")
        return []
    return [p for p in data if p.get("type") == "page" and "parentId" not in p and p.get("webSocketDebuggerUrl")]


def _ensure_edge_on_9555() -> None:
    """如果 9555 端口没 Edge 监听, 直接启动一个."""
    import socket, shutil, subprocess
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", 9555))
        s.close()
        return  # 端口有人在
    except Exception:
        pass
    print("[INFO] 9555 端口无 Edge, 启动新 Edge 进程 ...")
    edge_path = (shutil.which("msedge") or shutil.which("msedge.exe")
                 or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    profile_dir = r"d:\works\test\edge-farm-profile-4"
    subprocess.Popen([
        edge_path,
        "--remote-debugging-port=9555",
        f"--user-data-dir={profile_dir}",
        "--remote-allow-origins=*",
        "--no-first-run", "--no-default-browser-check",
        INVENTORY_URL,
    ])
    # 等端口就绪
    for _ in range(30):
        time.sleep(1)
        s = socket.socket()
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", 9555))
            s.close()
            return
        except Exception:
            pass
    raise RuntimeError("Edge 启动后 9555 端口仍无法连接")


def find_or_open_inventory_page() -> dict:
    """找到 inventory 标签页; 没有则新建."""
    _ensure_edge_on_9555()
    for p in list_pages():
        u = p.get("url", "")
        if u.startswith(INVENTORY_URL):
            return p
    # 兜底: 任何 duanwuqiufenmao.top 顶层 page (脚本会先把页面 navigate 到 inventory)
    for p in list_pages():
        u = p.get("url", "")
        if "duanwuqiufenmao.top" in u and u.startswith("https://www.duanwuqiufenmao.top/"):
            print(f"[INFO] 复用现有标签页: {u}")
            return p
    print("[INFO] 未在已打开标签页中找到目标 URL, 尝试用 Edge 新建标签页 ...")
    # 先尝试用 CDP 的 PUT /json/new?url (Chromium 行为)
    try:
        req = urllib.request.Request(f"{CDP_HTTP.rsplit('/', 1)[0]}/new?{INVENTORY_URL}", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            new_tab = json.loads(r.read().decode("utf-8"))
        if new_tab.get("webSocketDebuggerUrl"):
            return new_tab
    except Exception as e:
        print(f"[WARN] PUT /json/new 失败: {e}")
    # 兜底: 用 Target.createTarget 远程让 Edge 新开标签页
    try:
        with urllib.request.urlopen(CDP_HTTP, timeout=15) as r:
            tabs = json.loads(r.read().decode("utf-8"))
        browser_ws = next((p["webSocketDebuggerUrl"] for p in tabs if p.get("type") == "browser"), None)
        if browser_ws:
            print(f"[INFO] 通过浏览器级 CDP ({browser_ws}) 新建标签页 ...")
            bw = create_connection(browser_ws, timeout=15)
            bw.settimeout(15)
            bid = 0
            def bcall(method, params=None):
                nonlocal bid
                bid += 1
                bw.send(json.dumps({"id": bid, "method": method, "params": params or {}}))
                while True:
                    obj = json.loads(bw.recv())
                    if obj.get("id") == bid:
                        return obj.get("result", {})
            bcall("Target.setDiscoverTargets", {"discover": True})
            bcall("Target.createTarget", {"url": INVENTORY_URL})
            bw.close()
            for _ in range(20):
                time.sleep(0.5)
                for p in list_pages():
                    if p.get("url", "").startswith(INVENTORY_URL):
                        return p
    except Exception as e:
        print(f"[WARN] Target.createTarget 失败: {e}")
    # 最终兜底: 直接启动一个 Edge 进程, 绑定 9555 + profile-4
    print("[INFO] 启动一个新的 Edge 进程 (9555) ...")
    import shutil, subprocess
    edge_path = (shutil.which("msedge") or shutil.which("msedge.exe")
                 or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    profile_dir = r"d:\works\test\edge-farm-profile-4"
    subprocess.Popen([
        edge_path,
        "--remote-debugging-port=9555",
        f"--user-data-dir={profile_dir}",
        "--remote-allow-origins=*",
        "--no-first-run", "--no-default-browser-check",
        INVENTORY_URL,
    ])
    for _ in range(30):
        time.sleep(1.5)
        for p in list_pages():
            if p.get("url", "").startswith(INVENTORY_URL):
                return p
    raise RuntimeError("未能在 Edge 中打开目标 URL")


class CdpClient:
    """极简 CDP 客户端: 仅支持 Page.* 与 Runtime.* 子集."""

    def __init__(self, ws_url: str):
        self.ws = create_connection(ws_url, timeout=30)
        self.ws.settimeout(20)
        self._id = 0

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        my_id = self._id
        msg = {"id": my_id, "method": method, "params": params or {}}
        self.ws.send(json.dumps(msg))
        # 跳过 CDP 事件, 直到拿到匹配 id 的响应
        while True:
            raw = self.ws.recv()
            obj = json.loads(raw)
            if obj.get("id") == my_id:
                if "error" in obj:
                    raise RuntimeError(f"CDP error {method}: {obj['error']}")
                return obj.get("result", {})

    def enable_page(self):
        self.send("Page.enable")
        self.send("Runtime.enable")

    def wait_load(self, seconds: float = 5.0):
        """固定等待, 让 DOM 完成渲染."""
        time.sleep(seconds)

    def reload(self):
        self.send("Page.reload", {"ignoreCache": True})

    def navigate(self, url: str):
        self.send("Page.navigate", {"url": url})

    def evaluate(self, expression: str):
        r = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        result = r.get("result", {})
        if result.get("type") == "object" and "value" in result:
            return result["value"]
        return result.get("value")

    # ---------- 灵核页面专属方法 ----------

    def click_linghe_tab(self) -> bool:
        """点击 '灵核' 标签页. 若当前已经 active, 也返回 True.
        匹配所有 button.inv-tab, 取文本含 '灵核' 的那个."""
        js = (
            "(function(){"
            "  const tabs = document.querySelectorAll('button.inv-tab');"
            "  for (const t of tabs) {"
            "    if (t.textContent && t.textContent.indexOf('" + LINGHE_TAB_TEXT + "') >= 0) {"
            "      t.scrollIntoView({block: 'center'});"
            "      const opts = {bubbles: true, cancelable: true, view: window, button: 0};"
            "      t.dispatchEvent(new PointerEvent('pointerdown', opts));"
            "      t.dispatchEvent(new MouseEvent('mousedown', opts));"
            "      t.dispatchEvent(new PointerEvent('pointerup', opts));"
            "      t.dispatchEvent(new MouseEvent('mouseup', opts));"
            "      t.click();"
            "      return true;"
            "    }"
            "  }"
            "  return false;"
            "})()"
        )
        return bool(self.evaluate(js))

    def is_linghe_tab_active(self) -> bool:
        """检查灵核 Tab 当前是否处于 active 状态."""
        js = (
            "(function(){"
            "  const tabs = document.querySelectorAll('button.inv-tab');"
            "  for (const t of tabs) {"
            "    if (t.textContent && t.textContent.indexOf('" + LINGHE_TAB_TEXT + "') >= 0) {"
            "      return t.classList.contains('active');"
            "    }"
            "  }"
            "  return false;"
            "})()"
        )
        return bool(self.evaluate(js))

    def get_use_button_count(self) -> int:
        """返回灵核 Tab 下, 当前所有 .inv-card-bead 中可点的 '使用' 按钮数量.
        跳过 disabled / aria-disabled / 隐藏 / 0 个数的卡片."""
        js = (
            "(function(){"
            "  const grid = document.querySelector('" + INV_GRID_SELECTOR + "');"
            "  if (!grid) return 0;"
            "  const cards = grid.querySelectorAll('" + INV_CARD_SELECTOR + "');"
            "  let n = 0;"
            "  for (const c of cards) {"
            "    const btn = c.querySelector('" + USE_BTN_SELECTOR + "');"
            "    if (!btn) continue;"
            "    if (btn.disabled) continue;"
            "    if (btn.getAttribute('aria-disabled') === 'true') continue;"
            "    const style = window.getComputedStyle(btn);"
            "    if (style.display === 'none' || style.visibility === 'hidden') continue;"
            # 跳过数量为 0 的卡片
            "    const qtyEl = c.querySelector('.inv-item-qty');"
            "    if (qtyEl) {"
            "      const m = (qtyEl.textContent || '').match(/x\\s*(\\d+)/i);"
            "      if (m && parseInt(m[1], 10) <= 0) continue;"
            "    }"
            "    n++;"
            "  }"
            "  return n;"
            "})()"
        )
        v = self.evaluate(js)
        try:
            return int(v)
        except Exception:
            return 0

    def get_total_card_count(self) -> int:
        """返回灵核 Tab 下, .inv-grid 里的卡片总数 (用于诊断)."""
        js = (
            "(function(){"
            "  const grid = document.querySelector('" + INV_GRID_SELECTOR + "');"
            "  if (!grid) return 0;"
            "  return grid.querySelectorAll('" + INV_CARD_SELECTOR + "').length;"
            "})()"
        )
        v = self.evaluate(js)
        try:
            return int(v)
        except Exception:
            return 0

    def click_all_use_buttons(self) -> int:
        """点击当前灵核 Tab 下, 所有可见的 '使用' 按钮 (按 DOM 顺序).
        返回成功点击的次数. 已 disabled / 隐藏 / 数量 0 的按钮会被跳过."""
        js = (
            "(function(){"
            "  const grid = document.querySelector('" + INV_GRID_SELECTOR + "');"
            "  if (!grid) return 0;"
            "  const cards = grid.querySelectorAll('" + INV_CARD_SELECTOR + "');"
            "  let clicked = 0;"
            "  for (const c of cards) {"
            "    const btn = c.querySelector('" + USE_BTN_SELECTOR + "');"
            "    if (!btn) continue;"
            "    if (btn.disabled) continue;"
            "    if (btn.getAttribute('aria-disabled') === 'true') continue;"
            "    const style = window.getComputedStyle(btn);"
            "    if (style.display === 'none' || style.visibility === 'hidden') continue;"
            "    const qtyEl = c.querySelector('.inv-item-qty');"
            "    if (qtyEl) {"
            "      const m = (qtyEl.textContent || '').match(/x\\s*(\\d+)/i);"
            "      if (m && parseInt(m[1], 10) <= 0) continue;"
            "    }"
            "    btn.scrollIntoView({block: 'center'});"
            "    const opts = {bubbles: true, cancelable: true, view: window, button: 0};"
            "    btn.dispatchEvent(new PointerEvent('pointerdown', opts));"
            "    btn.dispatchEvent(new MouseEvent('mousedown', opts));"
            "    btn.dispatchEvent(new PointerEvent('pointerup', opts));"
            "    btn.dispatchEvent(new MouseEvent('mouseup', opts));"
            "    btn.click();"
            "    clicked++;"
            "  }"
            "  return clicked;"
            "})()"
        )
        v = self.evaluate(js)
        try:
            return int(v)
        except Exception:
            return 0


def main():
    page = find_or_open_inventory_page()
    print(f"[INFO] 已找到目标标签页: id={page['id']} url={page['url']}")
    client = CdpClient(page["webSocketDebuggerUrl"])
    try:
        client.enable_page()
        # 跳到背包页并等待渲染
        client.navigate(INVENTORY_URL)
        client.wait_load(4)

        # 1) 点击 "灵核" 标签页
        print("\n=== 切换到 '灵核' 标签页 ===")
        # 等待 Tab 出现
        for _ in range(20):
            if client.is_linghe_tab_active():
                print("[INFO] '灵核' Tab 已是激活态, 无需点击")
                break
            if client.click_linghe_tab():
                print("[INFO] 已点击 '灵核' Tab")
                break
            time.sleep(0.3)
        else:
            print("[WARN] 未找到 '灵核' Tab, 继续执行 (可能已在灵核页)")
        # 切 Tab 后等 DOM 更新
        client.wait_load(2)

        total = client.get_total_card_count()
        print(f"[INFO] 灵核 Tab 当前卡片数: {total}")

        # 2) 反复点击所有 '使用' 按钮, 直至没有为止 (每 1.5 秒一次)
        idle_rounds = 0       # 连续没有可点按钮的轮数
        max_idle_rounds = 3   # 连续 N 轮没有按钮 / 没有可点按钮, 认为完成
        max_total_rounds = 5000  # 兜底总轮数, 防止死循环
        total_clicked = 0

        print(f"\n=== 开始每 {CLICK_INTERVAL}s 一次, 点击所有 '使用' 按钮 ===")
        for round_idx in range(1, max_total_rounds + 1):
            cnt = client.get_use_button_count()
            total_cards = client.get_total_card_count()
            if cnt <= 0:
                idle_rounds += 1
                print(f"[INFO] 第 {round_idx} 轮: 没有可点的 '使用' 按钮 "
                      f"(卡片={total_cards}, 空闲={idle_rounds}/{max_idle_rounds})")
                if idle_rounds >= max_idle_rounds:
                    print(f"[DONE] 连续 {max_idle_rounds} 轮无按钮, 判定已完成, 停止")
                    break
            else:
                idle_rounds = 0
                clicked = client.click_all_use_buttons()
                total_clicked += clicked
                print(f"[INFO] 第 {round_idx} 轮: 卡片={total_cards}, "
                      f"可点={cnt}, 实际点击={clicked}, 累计={total_clicked}")
            time.sleep(CLICK_INTERVAL)

        print(f"\n[DONE] 全部完成, 累计点击 '使用' 按钮 {total_clicked} 次")
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] 用户中断, 退出")
        sys.exit(0)
