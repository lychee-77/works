# farm/team_dungeon/api.py — 组队秘境相关 REST
import time
import uuid
import requests
from config import API_HOST
from farm.auth import SignClient

STAMINA_ITEMS = [
    {"itemId": "stamina_bread", "name": "体力面包", "restore": 10},
    {"itemId": "stamina_small", "name": "小瓶体力药水", "restore": 20},
    {"itemId": "stamina_medium", "name": "中瓶体力药水", "restore": 50},
    {"itemId": "stamina_large", "name": "大瓶体力药水", "restore": 100},
]

EXP_ITEMS = [
    {"itemId": "exp_medium", "name": "中瓶经验水"},
    {"itemId": "exp_small", "name": "小瓶经验水"},
]


def _req(client: SignClient, method: str, api_path: str, body: dict = None) -> dict:
    url = API_HOST + api_path
    headers = client.get_signed_headers(method=method, api_path=api_path, body=body)
    if method.upper() == "GET":
        resp = requests.get(url, headers=headers, timeout=20)
    else:
        resp = requests.post(url, json=body or {}, headers=headers, timeout=20)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    return {"status": resp.status_code, "data": data}


def get_user_profile(client: SignClient) -> dict:
    return _req(client, "GET", "/api/user/profile")


def get_character(client: SignClient) -> dict:
    return _req(client, "GET", "/api/qpet/character")


def get_dungeon_status(client: SignClient) -> dict:
    return _req(client, "GET", "/api/qpet/dungeon/status")


def get_inventory(client: SignClient) -> dict:
    return _req(client, "GET", "/api/qpet/inventory")


def get_shop_status(client: SignClient) -> dict:
    return _req(client, "GET", "/api/qpet/shop/status")


def shop_buy(client: SignClient, item_id: str) -> dict:
    return _req(client, "POST", "/api/qpet/shop/buy", body={"itemId": item_id})


def use_consumable(client: SignClient, item_id: str, quantity: int = 1) -> dict:
    body = {
        "itemType": "consumable",
        "itemId": item_id,
        "quantity": int(quantity),
        "clientRequestId": f"qpet-use-item:{uuid.uuid4().hex[:12]}",
    }
    return _req(client, "POST", "/api/qpet/inventory/use", body=body)


def _shop_message(resp: dict) -> str:
    return str((resp.get("data") or {}).get("message") or "")


def _is_shop_processing(message: str) -> bool:
    msg = message or ""
    return "处理中" in msg or "重复提交" in msg or "请勿重复" in msg


def _fetch_stamina(client: SignClient) -> tuple[int, int]:
    char = get_character(client)
    cdata = (char.get("data") or {}).get("data") or {}
    stamina = int(cdata.get("stamina") or 0)
    max_stamina = int(cdata.get("max_stamina") or cdata.get("maxStamina") or 0)
    return stamina, max_stamina


def inventory_counts(inv_resp: dict) -> dict:
    data = (inv_resp.get("data") or {}).get("data") or {}
    items = data.get("items") or []
    out = {}
    for it in items:
        iid = it.get("item_id") or it.get("itemId")
        if not iid:
            continue
        out[iid] = int(it.get("quantity") or 0)
    return out


def dungeon_entry(status_resp: dict, dungeon_type: str) -> dict | None:
    data = (status_resp.get("data") or {}).get("data") or {}
    for d in data.get("dungeons") or []:
        if d.get("key") == dungeon_type or d.get("id") == dungeon_type:
            return d
    return None


def normalize_dungeon_types(dungeon_type) -> list:
    if dungeon_type is None:
        return []
    if isinstance(dungeon_type, str):
        return [dungeon_type] if dungeon_type else []
    return [str(x) for x in dungeon_type if x]


def unlocked_dungeon_types(status_resp: dict) -> list[str]:
    """按当前账号已解锁秘境动态返回类型列表（不含票券关）。"""
    data = (status_resp.get("data") or {}).get("data") or {}
    out = []
    for d in data.get("dungeons") or []:
        if not d.get("unlocked"):
            continue
        key = d.get("key")
        if not key:
            continue
        # 票券秘境（如深渊）即使出现也不自动纳入匹配
        if int(d.get("ticketCost") or 0) > 0:
            continue
        out.append(str(key))
    return out


def playable_dungeon_entries(status_resp: dict, dungeon_types) -> list:
    """返回仍可打的秘境条目（已解锁且 dailyRemaining>0），按配置顺序。"""
    out = []
    for key in normalize_dungeon_types(dungeon_types):
        entry = dungeon_entry(status_resp, key)
        if not entry:
            continue
        if not entry.get("unlocked"):
            continue
        if int(entry.get("dailyRemaining") or 0) <= 0:
            continue
        out.append(entry)
    return out


def _shop_remaining(shop_resp: dict, item_id: str) -> int | None:
    data = (shop_resp.get("data") or {}).get("data") or {}
    info = data.get(item_id)
    if not isinstance(info, dict):
        return None
    if "remaining" in info:
        return int(info.get("remaining") or 0)
    return None


def _stamina_buy_order(preferred_item: str) -> list[str]:
    """体力药商城购买顺序：restore 从大到小；preferred_item 同档时排最前。"""
    ranked = sorted(STAMINA_ITEMS, key=lambda z: -int(z["restore"]))
    order = [x["itemId"] for x in ranked]
    if preferred_item in order:
        order.remove(preferred_item)
        order.insert(0, preferred_item)
    return order


def _stamina_shop_can_buy(shop_resp: dict, item_id: str) -> bool:
    remain = _shop_remaining(shop_resp, item_id)
    if remain is None:
        return True
    return remain > 0


def _buy_stamina_from_shop(
    client: SignClient,
    shop_resp: dict,
    *,
    preferred_item: str,
    used: list,
) -> tuple[bool, str]:
    """
    按商城列表从大到小买体力药；大瓶今日次数用完再试中瓶/小瓶。
    返回 (是否已处理购买/等待, 最后消息)
    """
    last_msg = ""
    for buy_id in _stamina_buy_order(preferred_item):
        if not _stamina_shop_can_buy(shop_resp, buy_id):
            continue
        buy = shop_buy(client, buy_id)
        ok = buy.get("status") == 200 and (buy.get("data") or {}).get("success") is True
        msg = _shop_message(buy)
        last_msg = msg or last_msg
        print(f"[team_dungeon] 购买 {buy_id} success={ok} msg={msg}")
        if ok:
            used.append({"action": "buy", "itemId": buy_id})
            return True, msg
        if _is_shop_processing(msg):
            print("[team_dungeon] 购买处理中，等待后刷新体力…")
            time.sleep(2.0)
            return True, msg
        # 该档位买不了（次数用完等）→ 尝试下一档
    return False, last_msg or "没有可用体力药且无法购买"


def _has_active_exp_boost(cdata: dict) -> bool:
    charges = int(cdata.get("exp_boost_charges") or 0)
    if charges > 0:
        return True
    rate = float(cdata.get("exp_boost_rate") or 1)
    expires = cdata.get("exp_boost_expires_at")
    return rate > 1 and bool(expires)


def ensure_exp_boost(
    client: SignClient,
    *,
    preferred_item: str = "exp_medium",
    allow_buy: bool = True,
    use_potion: bool = True,
) -> dict:
    """
    秘境前准备经验水：背包没有则先买，再使用以激活经验加成。
    返回 {ok, used, boost, message}
    """
    used = []
    char = get_character(client)
    cdata = (char.get("data") or {}).get("data") or {}
    if not use_potion:
        return {
            "ok": True,
            "skipped": True,
            "used": used,
            "boost": {
                "rate": cdata.get("exp_boost_rate"),
                "charges": cdata.get("exp_boost_charges"),
                "expires_at": cdata.get("exp_boost_expires_at"),
            },
            "message": "未启用经验水",
        }

    if _has_active_exp_boost(cdata):
        return {
            "ok": True,
            "used": used,
            "boost": {
                "rate": cdata.get("exp_boost_rate"),
                "charges": cdata.get("exp_boost_charges"),
                "expires_at": cdata.get("exp_boost_expires_at"),
            },
            "message": "经验加成已激活",
        }

    inv = get_inventory(client)
    counts = inventory_counts(inv)
    order = [preferred_item] + [
        x["itemId"] for x in EXP_ITEMS if x["itemId"] != preferred_item
    ]

    picked = None
    for iid in order:
        if counts.get(iid, 0) > 0:
            picked = iid
            break

    if picked is None and allow_buy:
        shop = get_shop_status(client)
        for iid in order:
            remain = _shop_remaining(shop, iid)
            if remain is not None and remain <= 0:
                continue
            buy = shop_buy(client, iid)
            ok = buy.get("status") == 200 and (buy.get("data") or {}).get("success") is True
            msg = _shop_message(buy)
            print(f"[team_dungeon] 购买经验水 {iid} success={ok} msg={msg}")
            if ok:
                used.append({"action": "buy", "itemId": iid})
            elif _is_shop_processing(msg):
                print("[team_dungeon] 经验水购买处理中，等待后刷新背包…")
                time.sleep(2.0)
            else:
                continue
            inv = get_inventory(client)
            counts = inventory_counts(inv)
            if counts.get(iid, 0) > 0:
                picked = iid
                break

    if picked is None:
        return {
            "ok": False,
            "used": used,
            "boost": {
                "rate": cdata.get("exp_boost_rate"),
                "charges": cdata.get("exp_boost_charges"),
                "expires_at": cdata.get("exp_boost_expires_at"),
            },
            "message": "没有可用经验水且无法购买",
        }

    use = use_consumable(client, picked, 1)
    ok = use.get("status") == 200 and (use.get("data") or {}).get("success") is True
    print(f"[team_dungeon] 使用经验水 {picked} success={ok} msg={(use.get('data') or {}).get('message')}")
    if not ok:
        return {
            "ok": False,
            "used": used,
            "boost": {
                "rate": cdata.get("exp_boost_rate"),
                "charges": cdata.get("exp_boost_charges"),
                "expires_at": cdata.get("exp_boost_expires_at"),
            },
            "message": (use.get("data") or {}).get("message") or "使用经验水失败",
        }
    used.append({"action": "use", "itemId": picked})

    after = ((use.get("data") or {}).get("data") or {})
    if isinstance(after, dict) and (
        after.get("exp_boost_rate") is not None
        or after.get("exp_boost_charges") is not None
        or after.get("exp_boost_expires_at") is not None
    ):
        cdata = {**cdata, **after}
    else:
        char = get_character(client)
        cdata = (char.get("data") or {}).get("data") or {}

    return {
        "ok": True,
        "used": used,
        "boost": {
            "rate": cdata.get("exp_boost_rate"),
            "charges": cdata.get("exp_boost_charges"),
            "expires_at": cdata.get("exp_boost_expires_at"),
        },
        "message": "ok",
    }


def ensure_stamina(
    client: SignClient,
    *,
    required: int,
    preferred_item: str = "stamina_large",
    allow_buy: bool = True,
) -> dict:
    """
    体力不足时使用/购买体力药水。
    返回 {ok, stamina, required, used: [...], message}
    """
    used = []
    char = get_character(client)
    cdata = (char.get("data") or {}).get("data") or {}
    stamina = int(cdata.get("stamina") or 0)
    max_stamina = int(cdata.get("max_stamina") or cdata.get("maxStamina") or 0)
    required = int(required or 0)
    if required <= 0 or stamina >= required:
        return {"ok": True, "stamina": stamina, "required": required, "used": used, "message": "体力足够"}

    inv = get_inventory(client)
    counts = inventory_counts(inv)

    order = [preferred_item] + [
        x["itemId"] for x in sorted(STAMINA_ITEMS, key=lambda z: -z["restore"])
        if x["itemId"] != preferred_item
    ]

    for _ in range(20):
        if stamina >= required:
            break
        picked = None
        for iid in order:
            meta = next((x for x in STAMINA_ITEMS if x["itemId"] == iid), None)
            if not meta:
                continue
            if counts.get(iid, 0) > 0:
                picked = meta
                break
        if picked is None and allow_buy:
            shop = get_shop_status(client)
            handled, buy_msg = _buy_stamina_from_shop(
                client,
                shop,
                preferred_item=preferred_item,
                used=used,
            )
            if not handled:
                return {
                    "ok": False,
                    "stamina": stamina,
                    "required": required,
                    "used": used,
                    "message": buy_msg,
                }
            # 商店购买可能直接恢复体力，也可能入背包；两种都刷新后再判断
            stamina, max_stamina = _fetch_stamina(client)
            inv = get_inventory(client)
            counts = inventory_counts(inv)
            continue

        if picked is None:
            return {
                "ok": False,
                "stamina": stamina,
                "required": required,
                "used": used,
                "message": "没有可用体力药且无法购买",
            }

        use = use_consumable(client, picked["itemId"], 1)
        ok = use.get("status") == 200 and (use.get("data") or {}).get("success") is True
        print(f"[team_dungeon] 使用 {picked['itemId']} success={ok} msg={(use.get('data') or {}).get('message')}")
        if not ok:
            return {
                "ok": False,
                "stamina": stamina,
                "required": required,
                "used": used,
                "message": (use.get("data") or {}).get("message") or "使用体力药失败",
            }
        used.append({"action": "use", "itemId": picked["itemId"]})
        after = ((use.get("data") or {}).get("data") or {})
        if isinstance(after, dict) and after.get("stamina") is not None:
            stamina = int(after.get("stamina") or stamina)
            cdata = after
        else:
            char = get_character(client)
            cdata = (char.get("data") or {}).get("data") or {}
            stamina = int(cdata.get("stamina") or 0)
        counts[picked["itemId"]] = max(0, counts.get(picked["itemId"], 0) - 1)
        if max_stamina and stamina >= max_stamina and stamina < required:
            return {
                "ok": False,
                "stamina": stamina,
                "required": required,
                "used": used,
                "message": f"体力已达上限 {stamina}/{max_stamina}，仍不足 {required}",
            }

    return {
        "ok": stamina >= required,
        "stamina": stamina,
        "required": required,
        "used": used,
        "message": "ok" if stamina >= required else "补体力后仍不足",
    }
