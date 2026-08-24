# own.farm — 自己农场地块：查询 / 收菜 / 种植 / 浇水 / 翻地
import random
import string
import time
import uuid
from datetime import datetime, timedelta, timezone
import requests
from config import API_HOST, DAY_CROP_NORMAL
from farm.auth import SignClient

RIPE_STATES = {"ripe", "ready", "mature", "harvestable"}
# 前端 Ce()/vt()：成熟超过此时长视为枯萎（小时）
WITHER_HOURS = 24


def active_slots(data: dict) -> list:
    """
    取当前可用地块：优先以 unlockedSlots 为准过滤。
    接口地块数会变（如 5→3），不写死数量。
    """
    slots = list(data.get("slots") or [])
    unlocked = data.get("unlockedSlots")
    if unlocked is None:
        return slots
    try:
        unlocked = int(unlocked)
    except Exception:
        return slots
    filtered = []
    for s in slots:
        idx = s.get("slotIndex")
        if idx is None:
            continue
        try:
            if int(idx) < unlocked:
                filtered.append(s)
        except Exception:
            filtered.append(s)
    return filtered


def get_my_farm(client: SignClient) -> dict:
    """GET /api/farm 查询自己的农场地块"""
    api_path = "/api/farm"
    url = API_HOST + api_path
    headers = client.get_signed_headers(method="GET", api_path=api_path)
    resp = requests.get(url, headers=headers, timeout=15)
    print(f"\n[GET] my farm status={resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        print("GET raw:", resp.text[:500])
        raise
    if resp.status_code != 200 or not data.get("success"):
        raise Exception(f"查询自己农场失败: {data}")
    payload = data.get("data")
    if isinstance(payload, dict):
        enrich_farm_payload(payload)
    return data


def _to_local_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().replace(tzinfo=None)


def parse_farm_time(value):
    """
    解析 plantedAt / needsCareAt / serverTime。
    无时区字符串按 UTC（与前端 ct() 给 naive 时间补 Z 一致），返回本地 naive datetime。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return _to_local_naive(value)
    if isinstance(value, (int, float)) or (isinstance(value, str) and str(value).strip().isdigit()):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    iso = text.replace(" ", "T", 1) if "T" not in text[:20] else text
    if iso.endswith("Z") or iso.endswith("z"):
        iso = iso[:-1] + "+00:00"
    has_tz = len(iso) >= 6 and iso[-6] in "+-" and iso[-3] == ":"
    if not has_tz:
        iso = iso + "+00:00"
    try:
        return _to_local_naive(datetime.fromisoformat(iso))
    except Exception:
        pass
    text2 = text.replace("Z", "").replace("z", "").replace("T", " ")
    if "." in text2:
        text2 = text2.split(".", 1)[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text2[:19], fmt)
            return _to_local_naive(dt.replace(tzinfo=timezone.utc))
        except Exception:
            continue
    return None


def slot_remaining_minutes(slot: dict, now: datetime = None):
    """
    距离成熟的分钟数（可负：已过成熟点）。
    优先 plantedAt + growthMinutes（接口已不再下发 remainingMinutes）。
    """
    if not slot.get("cropId"):
        return None
    now = now or datetime.now()
    planted = parse_farm_time(slot.get("plantedAt"))
    growth = slot.get("growthMinutes")
    if planted is not None and growth is not None:
        try:
            ripe = planted + timedelta(minutes=float(growth))
        except (TypeError, ValueError):
            ripe = None
        if ripe is not None:
            return (ripe - now).total_seconds() / 60.0
    rem = slot.get("remainingMinutes")
    if rem is None:
        return None
    try:
        return float(rem)
    except (TypeError, ValueError):
        return None


def computed_slot_state(slot: dict, now: datetime = None) -> str:
    """empty / growing / mature / withered。

    接口已标明成熟/枯萎时以接口为准：浇水、土地加速会使实际成熟早于
    plantedAt+growthMinutes，只信公式会把已熟作物仍当成生长中。
    """
    if not slot.get("cropId"):
        return "empty"
    api_state = (slot.get("state") or "").lower()
    if api_state in RIPE_STATES:
        return "mature"
    if api_state in ("withered", "dead"):
        return "withered"
    rem = slot_remaining_minutes(slot, now=now)
    if rem is None:
        return api_state or "empty"
    if rem > 0:
        return "growing"
    hours_after = max(0.0, -float(rem) / 60.0)
    if hours_after > WITHER_HOURS:
        return "withered"
    return "mature"


def enrich_farm_payload(payload: dict, now: datetime = None) -> dict:
    """
    补齐接口不再下发的 remainingMinutes / canHarvest / canCare / canPlant。
    就地修改 slots，调度与日志可直接用。
    """
    if not isinstance(payload, dict):
        return payload
    now = now or parse_farm_time(payload.get("serverTime")) or datetime.now()
    for slot in payload.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        state = computed_slot_state(slot, now=now)
        rem = slot_remaining_minutes(slot, now=now)
        if slot.get("remainingMinutes") is None and rem is not None:
            slot["remainingMinutes"] = 0 if (state == "mature" or rem <= 0) else round(float(rem), 2)
        if slot.get("canHarvest") is None:
            slot["canHarvest"] = state == "mature"
        if slot.get("canPlant") is None:
            slot["canPlant"] = state == "empty"
        if slot.get("canCare") is None:
            if state == "growing":
                needs = parse_farm_time(slot.get("needsCareAt"))
                if needs and now >= needs:
                    slot["canCare"] = True
            else:
                slot["canCare"] = False
        if slot.get("canRemove") is None and state == "withered":
            slot["canRemove"] = True
        if slot.get("canManualRemove") is None and state in ("growing", "mature"):
            slot["canManualRemove"] = True
        if slot.get("isProtected") is None and slot.get("protectedUntil"):
            prot = parse_farm_time(slot.get("protectedUntil"))
            slot["isProtected"] = bool(prot and now < prot)
        if not slot.get("state"):
            slot["state"] = state
    return payload


def is_harvestable(slot: dict) -> bool:
    """自己地块是否可收菜"""
    if not slot.get("cropId"):
        return False
    if slot.get("canHarvest") is True:
        return True
    if computed_slot_state(slot) == "mature":
        return True
    state = (slot.get("state") or "").lower()
    if state in RIPE_STATES:
        return True
    rem = slot_remaining_minutes(slot)
    if rem is not None and rem <= 0 and state not in ("empty", "withered", "dead", "growing"):
        return True
    return False


def is_plantable(slot: dict) -> bool:
    """空地是否可种植"""
    if slot.get("canPlant") is True:
        return True
    if computed_slot_state(slot) == "empty":
        return True
    state = (slot.get("state") or "").lower()
    return state in ("empty", "") and not slot.get("cropId")


def is_removable(slot: dict) -> bool:
    """地块作物是否可铲除（枯萎 canRemove，或生长/成熟 canManualRemove）"""
    if not slot.get("cropId"):
        return False
    if slot.get("canRemove") is True:
        return True
    if slot.get("canManualRemove") is True:
        return True
    return False


def is_careable(slot: dict) -> bool:
    """生长中地块是否可浇水（缺水）。接口可能不再下发 canCare。"""
    if not slot.get("cropId"):
        return False
    if slot.get("canCare") is True:
        return True
    if slot.get("canCare") is False:
        return False
    if computed_slot_state(slot) != "growing":
        return False
    needs = parse_farm_time(slot.get("needsCareAt"))
    if needs is None:
        return False
    return datetime.now() >= needs


_CARE_LIMIT_NEEDLES = (
    "浇水次数",
    "浇水上限",
    "浇水已满",
    "浇水已达",
    "今日浇水已",
    "已达今日浇水",
    "达到今日浇水",
    "每日浇水",
    "照料次数",
    "今日照料",
    "达到次数",
)


def is_care_limit_message(message: str | None) -> bool:
    """接口文案是否表示今日浇水次数已用完。"""
    text = str(message or "").strip()
    if not text:
        return False
    if "不需要浇水" in text or "暂不需要" in text or "暂时不需要" in text:
        return False
    if any(k in text for k in _CARE_LIMIT_NEEDLES):
        return True
    if ("浇水" in text or "照料" in text) and any(
        k in text for k in ("上限", "次数已", "已达", "用完", "已满", "达到次数")
    ):
        return True
    return False


def response_indicates_care_limit(resp: dict | None) -> bool:
    """care/help 等接口返回是否表示今日浇水已达次数。"""
    if not isinstance(resp, dict):
        return False
    texts = []

    def _collect(obj, depth=0):
        if not isinstance(obj, dict) or depth > 3:
            return
        for key in ("message", "error", "code", "reason"):
            val = obj.get(key)
            if val is not None:
                texts.append(str(val))
        inner = obj.get("data")
        if isinstance(inner, dict):
            _collect(inner, depth + 1)

    _collect(resp)
    return any(is_care_limit_message(x) for x in texts)


def minutes_until_harvestable(slot: dict):
    """
    距离该地块预计可收的分钟数。
    已可收 -> 0；生长中 -> plantedAt+growthMinutes；空地/不可达 -> None
    """
    if not slot.get("cropId"):
        return None
    if is_harvestable(slot):
        return 0
    rem = slot_remaining_minutes(slot)
    if rem is None:
        return None
    if rem < 0:
        return None
    state = computed_slot_state(slot)
    if state == "growing" or rem > 0:
        return round(float(rem), 2)
    return None


def minutes_until_careable(slot: dict):
    """
    距离该地块预计可浇水的分钟数。
    已可浇 -> 0；未到 needsCareAt -> 差值；已浇过/不可浇 -> None
    """
    if not slot.get("cropId"):
        return None
    if is_careable(slot):
        return 0
    if computed_slot_state(slot) != "growing":
        return None
    needs = parse_farm_time(slot.get("needsCareAt"))
    if needs is None:
        return None
    delta_min = (needs - datetime.now()).total_seconds() / 60.0
    if delta_min <= 0:
        # 已到期但 canCare=false：通常已浇过或服务端暂不可浇，不立刻重试
        return None
    return round(float(delta_min), 2)


def _rand_alnum(n=8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def _build_mouse_trail(started: int, ended: int, points: int = 40) -> list:
    """生成一段近似前端的鼠标轨迹"""
    x, y = random.randint(800, 980), random.randint(330, 400)
    trail = []
    for i in range(points):
        t = started + int((ended - started) * i / max(points - 1, 1))
        x = max(400, x - random.randint(0, 8))
        y = max(300, y + random.randint(-2, 1))
        trail.append({"x": x, "y": y, "t": t})
        trail.append({"x": x, "y": y, "t": t})
    return trail


def build_harvest_body(action_token: str = None) -> dict:
    """构造收菜请求体（对齐前端 _farmProof，action=harvest）"""
    now = int(time.time() * 1000)
    duration = random.randint(80, 200)
    started = now - duration
    ended = now
    return {
        "_farmProof": {
            "action": "harvest",
            "token": action_token or "",
            "clientActionId": str(uuid.uuid4()),
            "startedAt": started,
            "endedAt": ended,
            "durationMs": duration,
            "visibility": "visible",
            "pointerType": "mouse",
            "trail": [{"x": random.randint(400, 900), "y": random.randint(100, 500), "t": started}],
        },
        "clientRequestId": str(uuid.uuid4()),
        "mouseTrail": [
            {"x": random.randint(400, 900), "y": random.randint(100, 500), "t": started - random.randint(1000, 8000)}
        ],
    }


def build_plant_body(crop_id: str = None, action_token: str = None) -> dict:
    """构造种植请求体：POST /api/farm/slots/{i}/plant"""
    crop_id = crop_id or DAY_CROP_NORMAL
    now = int(time.time() * 1000)
    duration = random.randint(2500, 4000)
    started = now - duration
    ended = now
    trail = _build_mouse_trail(started, ended)
    return {
        "cropId": crop_id,
        "_farmProof": {
            "action": "plant",
            "token": action_token or "",
            "clientActionId": str(uuid.uuid4()),
            "startedAt": started,
            "endedAt": ended,
            "durationMs": duration,
            "visibility": "visible",
            "pointerType": "mouse",
            "trail": trail,
        },
        "clientRequestId": f"farm-plant:{ended}:{_rand_alnum(8)}",
    }


def build_care_body(action_token: str = None) -> dict:
    """构造浇水请求体：POST /api/farm/slots/{i}/care，action=care"""
    now = int(time.time() * 1000)
    duration = random.randint(800, 1200)
    started = now - duration
    ended = now
    trail = _build_mouse_trail(started, ended, points=random.randint(35, 45))
    return {
        "_farmProof": {
            "action": "care",
            "token": action_token or "",
            "clientActionId": str(uuid.uuid4()),
            "startedAt": started,
            "endedAt": ended,
            "durationMs": duration,
            "visibility": "visible",
            "pointerType": "mouse",
            "trail": trail,
        },
        "clientRequestId": f"farm-care:{ended}:{_rand_alnum(8)}",
    }


def build_remove_body(action_token: str = None) -> dict:
    """构造铲除请求体：POST /api/farm/slots/{i}/remove，action=remove"""
    now = int(time.time() * 1000)
    duration = random.randint(500, 900)
    started = now - duration
    ended = now
    trail = _build_mouse_trail(started, ended, points=random.randint(30, 40))
    return {
        "_farmProof": {
            "action": "remove",
            "token": action_token or "",
            "clientActionId": str(uuid.uuid4()),
            "startedAt": started,
            "endedAt": ended,
            "durationMs": duration,
            "visibility": "visible",
            "pointerType": "mouse",
            "trail": trail,
        },
        "clientRequestId": f"farm-remove:{ended}:{_rand_alnum(8)}",
    }


def _safe_print_resp(tag: str, **kwargs):
    """避免 Windows 控制台因 emoji 崩溃"""
    parts = [f"\n[{tag}]"]
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")
    text = " ".join(parts)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))


def harvest_slot(client: SignClient, slot_index: int, action_token: str = None) -> dict:
    """POST /api/farm/slots/{slotIndex}/harvest"""
    api_path = f"/api/farm/slots/{slot_index}/harvest"
    url = API_HOST + api_path
    body = build_harvest_body(action_token)
    headers = client.get_signed_headers(method="POST", api_path=api_path, body=body)
    resp = requests.post(url, json=body, headers=headers, timeout=15)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    msg = (data or {}).get("message") if isinstance(data, dict) else None
    success = (data or {}).get("success") if isinstance(data, dict) else None
    _safe_print_resp("POST harvest", slot=slot_index, status=resp.status_code, success=success, message=msg)
    return {"status": resp.status_code, "data": data}


def plant_slot(client: SignClient, slot_index: int, crop_id: str = None, action_token: str = None) -> dict:
    """POST /api/farm/slots/{slotIndex}/plant"""
    crop_id = crop_id or DAY_CROP_NORMAL
    api_path = f"/api/farm/slots/{slot_index}/plant"
    url = API_HOST + api_path
    body = build_plant_body(crop_id=crop_id, action_token=action_token)
    headers = client.get_signed_headers(method="POST", api_path=api_path, body=body)
    resp = requests.post(url, json=body, headers=headers, timeout=15)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    msg = (data or {}).get("message") if isinstance(data, dict) else None
    success = (data or {}).get("success") if isinstance(data, dict) else None
    _safe_print_resp("POST plant", slot=slot_index, cropId=crop_id, status=resp.status_code, success=success, message=msg)
    return {"status": resp.status_code, "data": data}


def care_slot(client: SignClient, slot_index: int, action_token: str = None) -> dict:
    """POST /api/farm/slots/{slotIndex}/care 自己浇水"""
    api_path = f"/api/farm/slots/{slot_index}/care"
    url = API_HOST + api_path
    body = build_care_body(action_token)
    headers = client.get_signed_headers(method="POST", api_path=api_path, body=body)
    resp = requests.post(url, json=body, headers=headers, timeout=15)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    msg = (data or {}).get("message") if isinstance(data, dict) else None
    success = (data or {}).get("success") if isinstance(data, dict) else None
    _safe_print_resp("POST care", slot=slot_index, status=resp.status_code, success=success, message=msg)
    return {"status": resp.status_code, "data": data}


def remove_slot(client: SignClient, slot_index: int, action_token: str = None) -> dict:
    """POST /api/farm/slots/{slotIndex}/remove 铲除作物（枯萎/手动）"""
    api_path = f"/api/farm/slots/{slot_index}/remove"
    url = API_HOST + api_path
    body = build_remove_body(action_token)
    headers = client.get_signed_headers(method="POST", api_path=api_path, body=body)
    resp = requests.post(url, json=body, headers=headers, timeout=15)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    msg = (data or {}).get("message") if isinstance(data, dict) else None
    success = (data or {}).get("success") if isinstance(data, dict) else None
    _safe_print_resp("POST remove", slot=slot_index, status=resp.status_code, success=success, message=msg)
    return {"status": resp.status_code, "data": data}


def build_explore_body(action_token: str = None) -> dict:
    """构造自己翻地请求体：action=friend-explore（与前端 exploreOwnLand 一致）"""
    now = int(time.time() * 1000)
    duration = random.randint(500, 900)
    started = now - duration
    ended = now
    return {
        "_farmProof": {
            "action": "friend-explore",
            "token": action_token or "",
            "clientActionId": str(uuid.uuid4()),
            "startedAt": started,
            "endedAt": ended,
            "durationMs": duration,
            "visibility": "visible",
            "pointerType": "mouse",
            "trail": _build_mouse_trail(started, ended, points=random.randint(35, 45)),
        },
        "clientRequestId": f"farm-own-explore:{ended}:{_rand_alnum(8)}",
    }


def explore_own_slot(client: SignClient, slot_index: int, action_token: str = None) -> dict:
    """POST /api/farm/slots/{slotIndex}/explore 翻自己地（与好友共用每日翻地上限）"""
    api_path = f"/api/farm/slots/{slot_index}/explore"
    url = API_HOST + api_path
    body = build_explore_body(action_token)
    headers = client.get_signed_headers(method="POST", api_path=api_path, body=body)
    resp = requests.post(url, json=body, headers=headers, timeout=15)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    msg = (data or {}).get("message") if isinstance(data, dict) else None
    success = (data or {}).get("success") if isinstance(data, dict) else None
    _safe_print_resp("POST explore-own", slot=slot_index, status=resp.status_code, success=success, message=msg)
    return {"status": resp.status_code, "data": data}


def main():
    client = SignClient()
    client.register_signing_key(force=False)
    farm = get_my_farm(client)
    data = farm.get("data") or {}
    slots = data.get("slots") or []
    print(f"地块数: {len(slots)} level={data.get('level')} exp={data.get('experience')}")
    print(
        f"todayCareCount={data.get('todayCareCount')} "
        f"DAILY_CARE_CAP={(data.get('constants') or {}).get('DAILY_CARE_CAP')}"
    )
    for s in slots:
        ok = is_harvestable(s)
        eta = minutes_until_harvestable(s)
        care_ok = is_careable(s)
        care_eta = minutes_until_careable(s)
        print(
            f"  [{'可收' if ok else '跳过'}] slot={s.get('slotIndex')} {s.get('cropName') or '-'} "
            f"state={s.get('state')} rem={s.get('remainingMinutes')} "
            f"canHarvest={s.get('canHarvest')} canCare={care_ok} careEta={care_eta} eta={eta}"
        )


if __name__ == "__main__":
    main()
