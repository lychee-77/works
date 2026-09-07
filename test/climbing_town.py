"""
climbing_town.py
通过 Edge 的远程调试端口 (CDP) 连接到 https://www.duanwuqiufenmao.top/qpet/tower,
点击楼层 48 按钮,然后刷新页面,重复 5 次。

使用前:
1. 用以下命令以远程调试模式启动 Edge (一次性):
   start msedge.exe --remote-debugging-port=9222 --remote-allow-origins=* https://www.duanwuqiufenmao.top/qpet/tower
   (如果 Edge 已经在跑,可以省略; 端口 9222 已被占用说明 Edge 已就绪)
2. 安装依赖:  pip install websocket-client
3. 运行:       python climbing_town.py
"""

import json
import time
import urllib.request
from websocket import create_connection  # pip install websocket-client

CDP_HTTP = "http://127.0.0.1:9555/json"
TARGET_URL = "https://www.duanwuqiufenmao.top/qpet/tower"
TARGET_FLOOR = 48  # 要点击的楼层号
REPEAT_TIMES = 5
INVENTORY_URL = "https://www.duanwuqiufenmao.top/qpet/inventory"
USE_ITEM_NAME = "小瓶经验水"
REVIVE_ITEM_NAME = "还魂丹"
REVIVE_TIMES = 0


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
        TARGET_URL,
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


def find_or_open_tower_page() -> dict:
    """找到目标 URL 标签页; 没有则新建. 兼容 duanwuqiufenmao.top 任意顶层 page."""
    _ensure_edge_on_9555()
    for p in list_pages():
        u = p.get("url", "")
        if u.startswith(TARGET_URL):
            return p
    # 兜底: 任何 duanwuqiufenmao.top 顶层 page (脚本第一件事是 Page.navigate 到 tower, 所以也能用)
    for p in list_pages():
        u = p.get("url", "")
        if "duanwuqiufenmao.top" in u and u.startswith("https://www.duanwuqiufenmao.top/"):
            print(f"[INFO] 复用现有标签页: {u}")
            return p
    print("[INFO] 未在已打开标签页中找到目标 URL, 尝试用 Edge 新建标签页 ...")
    # 先尝试用 CDP 的 PUT /json/new?url (Chromium 行为)
    try:
        req = urllib.request.Request(f"{CDP_HTTP.rsplit('/', 1)[0]}/new?{TARGET_URL}", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            new_tab = json.loads(r.read().decode("utf-8"))
        if new_tab.get("webSocketDebuggerUrl"):
            return new_tab
    except Exception as e:
        print(f"[WARN] PUT /json/new 失败: {e}")
    # 兜底: 用 Target.createTarget 远程让 Edge 新开标签页
    try:
        new_tab = None
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
            tid = bcall("Target.createTarget", {"url": TARGET_URL}).get("targetId")
            bw.close()
            # 等新 tab 出现在 /json 列表里
            for _ in range(20):
                time.sleep(0.5)
                for p in list_pages():
                    if p.get("url", "").startswith(TARGET_URL):
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
        TARGET_URL,
    ])
    for _ in range(30):
        time.sleep(1.5)
        for p in list_pages():
            if p.get("url", "").startswith(TARGET_URL):
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

    def evaluate(self, expression: str):
        r = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        # r 形如 {"result": {"type": "...", "value": ...}, "remoteObject": {...}}
        result = r.get("result", {})
        if result.get("type") == "object" and "value" in result:
            return result["value"]
        return result.get("value")

    def click_floor(self, floor: int) -> bool:
        """点击指定楼层的 button, 返回是否成功找到并点击.
        限定在 .tower-floor-grid 容器下的 .tower-floor.cleared 按钮,
        文本为楼层号. 同时派发 PointerEvent / MouseEvent 以触发 Vue 监听."""
        js = (
            "(function() {"
            "  const grid = document.querySelector('div.tower-floor-grid');"
            "  if (!grid) return -1;"
            "  const btns = grid.querySelectorAll('button.tower-floor.cleared');"
            "  for (const b of btns) {"
            "    const span = b.querySelector('span');"
            "    if (span && span.textContent.trim() === '" + str(floor) + "') {"
            "      b.scrollIntoView({block: 'center'});"
            "      const opts = {bubbles: true, cancelable: true, view: window, button: 0};"
            "      b.dispatchEvent(new PointerEvent('pointerdown', opts));"
            "      b.dispatchEvent(new MouseEvent('mousedown', opts));"
            "      b.dispatchEvent(new PointerEvent('pointerup', opts));"
            "      b.dispatchEvent(new MouseEvent('mouseup', opts));"
            "      b.click();"
            "      return 1;"
            "    }"
            "  }"
            "  return 0;"
            "})()"
        )
        result = self.evaluate(js)
        return result == 1

    def wait_button(self, floor: int, timeout: float = 30.0) -> bool:
        """轮询等待指定楼层按钮出现在 .tower-floor-grid 中."""
        end = time.time() + timeout
        js = (
            "(function(){"
            "  const grid = document.querySelector('div.tower-floor-grid');"
            "  if (!grid) return false;"
            "  const bs = grid.querySelectorAll('button.tower-floor.cleared');"
            "  for (const b of bs) {"
            "    const span = b.querySelector('span');"
            "    if (span && span.textContent.trim() === '" + str(floor) + "') return true;"
            "  }"
            "  return false;"
            "})()"
        )
        while time.time() < end:
            if self.evaluate(js):
                return True
            time.sleep(0.5)
        return False

    # 弹窗中 "开始战斗/出战/挑战/进入战斗" 等确认按钮的文本关键字
    CONFIRM_KEYWORDS = ("开始战斗", "出战", "挑战", "进入战斗", "开始挑战",
                        "战斗开始", "确认", "确定", "进入", "开战", "GO")
    STAGE_DIALOG_SELECTOR = "div.el-dialog.battle-stage-dialog"

    def wait_confirm_dialog(self, timeout: float = 8.0) -> bool:
        """轮询等待 '准备战斗' 弹窗 (battle-stage-dialog) 出现."""
        end = time.time() + timeout
        js = (
            "(function(){"
            "  return !!document.querySelector('" + self.STAGE_DIALOG_SELECTOR + "');"
            "})()"
        )
        while time.time() < end:
            if self.evaluate(js):
                return True
            time.sleep(0.3)
        return False

    def wait_and_click_confirm(self, delay: float = 0.5, timeout: float = 5.0) -> bool:
        """点击 48 楼后, 等 `delay` 秒, 找 <button class='el-button--primary'> 下的
        <span>确认挑战</span> 并点击. 返回是否成功."""
        time.sleep(delay)
        end = time.time() + timeout
        js = (
            "(function(){"
            "  const btns = Array.from(document.querySelectorAll('button.el-button--primary'));"
            "  for (const b of btns) {"
            "    if (b.getAttribute('aria-disabled') === 'true') continue;"
            "    const span = b.querySelector('span');"
            "    if (span && span.textContent.trim() === '确认挑战') {"
            "      b.click();"
            "      return true;"
            "    }"
            "  }"
            "  return false;"
            "})()"
        )
        while time.time() < end:
            if self.evaluate(js):
                return True
            time.sleep(0.3)
        return False

    def navigate_and_use(self, url: str, item_name: str, use_btn_class: str = "inv-btn-use", count: int = 1):
        """跳转到 url, 找到名为 item_name 的 .inv-card 卡片, 点击其中的 使用 按钮 count 次.
        每次点击后等待 0.3s, 然后刷新页面再找下一个, 直到点击够 count 次或找不到为止."""
        self.send("Page.navigate", {"url": url})
        self.wait_load(4)
        success = 0
        for k in range(count):
            js = (
                "(function(){"
                "  const cards = document.querySelectorAll('div.inv-card');"
                "  for (const c of cards) {"
                "    const nameEl = c.querySelector('.inv-item-name');"
                "    if (nameEl && nameEl.textContent.trim() === '" + item_name + "') {"
                "      const btn = c.querySelector('button." + use_btn_class + "');"
                "      if (btn) {"
                "        btn.scrollIntoView({block: 'center'});"
                "        btn.click();"
                "        return true;"
                "      }"
                "    }"
                "  }"
                "  return false;"
                "})()"
            )
            ok = bool(self.evaluate(js))
            if not ok:
                # 偶尔前端还没更新, 多重试几次 (最多 3s)
                retry_end = time.time() + 3
                while time.time() < retry_end and not ok:
                    time.sleep(0.2)
                    ok = bool(self.evaluate(js))
                if not ok:
                    print(f"[WARN] 第 {k+1} 次: 未找到 {item_name} 卡片, 停止")
                    break
            success += 1
            time.sleep(1)
        return success


def main():
    page = find_or_open_tower_page()
    print(f"[INFO] 已找到目标标签页: id={page['id']} url={page['url']}")
    client = CdpClient(page["webSocketDebuggerUrl"])
    try:
        client.enable_page()
        # 等待初始页面渲染完毕
        client.wait_load(5)
        # 一次性: 去 inventory 使用还魂丹 72 次, 然后回到塔
        print(f"\n=== 开始循环前: 使用 {REVIVE_ITEM_NAME} {REVIVE_TIMES} 次 ===")
        used = client.navigate_and_use(INVENTORY_URL, REVIVE_ITEM_NAME, count=REVIVE_TIMES)
        print(f"[INFO] 还魂丹使用成功 {used}/{REVIVE_TIMES} 次")
        time.sleep(0.5)
        client.send("Page.navigate", {"url": TARGET_URL})
        client.wait_load(5)
        print(f"[INFO] 已回到 {TARGET_URL}")
        outer = 0
        OUTER_MAX = 5
        while outer < OUTER_MAX:
            outer += 1
            print(f"\n############ 第 {outer}/{OUTER_MAX} 轮大循环 (5 次爬塔 + 用经验水) ############")
            for i in range(1, REPEAT_TIMES + 1):
                print(f"\n=== 第 {i}/{REPEAT_TIMES} 轮 ===")
                if not client.wait_button(TARGET_FLOOR, timeout=20):
                    print(f"[WARN] 第 {i} 轮: 楼层 {TARGET_FLOOR} 按钮未出现, 跳过点击")
                    # 仍执行刷新
                else:
                    ok = client.click_floor(TARGET_FLOOR)
                    print(f"[INFO] 第 {i} 轮: 点击楼层 {TARGET_FLOOR} -> {ok}")
                    confirm = client.wait_and_click_confirm(delay=0.5, timeout=5)
                    print(f"[INFO] 第 {i} 轮: 点击确认挑战 -> {confirm}")
                    time.sleep(0.5)
                client.reload()
                print(f"[INFO] 第 {i} 轮: 已刷新")
                client.wait_load(5)
            # 5 轮爬塔结束后: 跳到背包, 使用 小瓶经验水, 然后回到塔页面
            print(f"\n=== 使用 {USE_ITEM_NAME} ===")
            used = client.navigate_and_use(INVENTORY_URL, USE_ITEM_NAME)
            print(f"[INFO] 点击使用 -> {used}")
            time.sleep(0.5)
            client.send("Page.navigate", {"url": TARGET_URL})
            client.wait_load(0.5)
            print(f"[INFO] 已回到 {TARGET_URL}")
        print(f"\n[DONE] 全部完成 (大循环 {OUTER_MAX} 次)")
    finally:
        client.close()


if __name__ == "__main__":
    main()
