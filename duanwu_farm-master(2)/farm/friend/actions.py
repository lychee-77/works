# friend.actions — 好友农场：偷菜 / 翻地 / 浇水(help)
import random
import string
import time
import uuid
from datetime import datetime
import requests
from config import API_HOST, FRIEND_ID, STEAL_MIN_EXP_REWARD
from farm.auth import SignClient
from farm.friend.farm import get_friend_farm
from farm.own.farm import (
    is_careable,
    minutes_until_careable,
    slot_remaining_minutes,
    computed_slot_state,
    parse_farm_time,
)


RIPE_STATES = {"ripe", "ready", "mature", "harvestable"}


def _rand_alnum(n=8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def _build_mouse_trail(started: int, ended: int, points: int = 40) -> list:
    x, y = random.randint(520, 700), random.randint(260, 340)
    trail = []
    for i in range(points):
        t = started + int((ended - started) * i / max(points - 1, 1))
        x = max(400, min(900, x + random.randint(-8, 5)))
        y = max(250, min(420, y + random.randint(-3, 6)))
        trail.append({"x": x, "y": y, "t": t})
        trail.append({"x": x, "y": y, "t": t})
    return trail


def meets_steal_exp_threshold(slot: dict) -> bool:
    """经验过低则不值得偷（按 config.STEAL_MIN_EXP_REWARD）。"""
    if STEAL_MIN_EXP_REWARD is None or int(STEAL_MIN_EXP_REWARD) <= 0:
        return True
    exp = slot.get("expReward")
    if exp is None:
        return True
    return int(exp) >= int(STEAL_MIN_EXP_REWARD)


# 业务上应跳过本块、继续偷下一块（黑土地等不可偷）
_STEAL_SKIP_KEYWORDS = (
    "黑土地",
    "黑土",
    "无法偷",
    "不能偷",
    "不可偷",
    "不许偷",
    "禁止偷",
    "受保护",
    "保护中",
    "已被偷满",
    "已经偷过",
    "今日已偷",
)


def is_steal_skip_message(message: str | None) -> bool:
    """接口业务失败文案是否表示「这地块跳过即可」。"""
    text = str(message or "").strip()
    if not text:
        return False
    return any(k in text for k in _STEAL_SKIP_KEYWORDS)


def land_blocks_steal(slot: dict) -> bool:
    """土地信息已知且为黑土地等不可偷类型时，本地先跳过。"""
    land = slot.get("land")
    if not isinstance(land, dict):
        return False
    name = str(land.get("name") or land.get("stageTitle") or "")
    return "黑土地" in name or "黑土" in name


def _is_mature_like(slot: dict) -> bool:
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


def steal_skip_reason(slot: dict) -> str | None:
    """不可偷时的过滤理由；可偷返回 None。"""
    if is_stealable(slot):
        return None
    if not slot.get("cropId"):
        return "空地"
    if land_blocks_steal(slot):
        return "黑土地"
    if slot.get("alreadyStolenByVisitor"):
        return "今日已偷过"
    if slot.get("stolenFull"):
        return "已被偷满"
    if slot.get("isProtected"):
        return "受保护"
    if not meets_steal_exp_threshold(slot):
        exp = slot.get("expReward")
        return f"经验低于阈值({exp}<{STEAL_MIN_EXP_REWARD})"
    if not _is_mature_like(slot):
        rem = slot_remaining_minutes(slot)
        if rem is not None and rem > 0:
            shown = int(rem) if rem >= 1 else round(rem, 1)
            return f"未成熟(约{shown}分钟)"
        return "未成熟"
    return "不可偷"


def explore_skip_reason(slot: dict) -> str | None:
    """不可翻时的过滤理由；可翻返回 None。"""
    if is_explorable(slot):
        return None
    if not slot.get("cropId"):
        return "空地"
    if slot.get("alreadyExploredByVisitor"):
        return "今日已翻过"
    if not _is_mature_like(slot):
        rem = slot_remaining_minutes(slot)
        if rem is not None and rem > 0:
            shown = int(rem) if rem >= 1 else round(rem, 1)
            return f"未成熟(约{shown}分钟)"
        return "未成熟"
    return "不可翻"


def help_skip_reason(slot: dict) -> str | None:
    """不可浇时的过滤理由；可浇返回 None。"""
    if is_helpable(slot):
        return None
    if not slot.get("cropId"):
        return "空地"
    state = (slot.get("state") or "").lower()
    if state in RIPE_STATES or slot.get("canHarvest") is True:
        return "成熟无需浇水"
    if slot.get("canCare") is False:
        return "暂不可浇/已浇过"
    rem = slot_remaining_minutes(slot)
    needs = slot.get("needsCareAt")
    if needs or (rem is not None and rem > 0):
        return "未到浇水时间"
    return "不可浇"


def summarize_slot_filters(slots: list) -> list[str]:
    """汇总本页地块偷/翻/浇被过滤的原因，便于日志展示。"""
    steal_reasons: dict[str, int] = {}
    explore_reasons: dict[str, int] = {}
    help_reasons: dict[str, int] = {}
    for s in slots:
        r = steal_skip_reason(s)
        if r:
            steal_reasons[r] = steal_reasons.get(r, 0) + 1
        r = explore_skip_reason(s)
        if r:
            explore_reasons[r] = explore_reasons.get(r, 0) + 1
        r = help_skip_reason(s)
        if r:
            help_reasons[r] = help_reasons.get(r, 0) + 1

    lines = []
    for label, reasons in (
        ("偷", steal_reasons),
        ("翻", explore_reasons),
        ("浇", help_reasons),
    ):
        if not reasons:
            continue
        parts = [f"{why} x{cnt}" for why, cnt in reasons.items()]
        lines.append(f"{label}: " + "；".join(parts))
    return lines


def is_stealable(slot: dict) -> bool:
    """判断访客是否可偷该地块"""
    if not slot.get("cropId"):
        return False
    if land_blocks_steal(slot):
        return False
    if slot.get("alreadyStolenByVisitor"):
        return False
    if slot.get("stolenFull"):
        return False
    if slot.get("isProtected"):
        return False
    if not meets_steal_exp_threshold(slot):
        return False

    if _is_mature_like(slot):
        return True
    return False


def is_explorable(slot: dict) -> bool:
    """判断访客是否可翻地（仅成熟作物，且本日未翻过）"""
    if not slot.get("cropId"):
        return False
    if slot.get("alreadyExploredByVisitor"):
        return False
    if _is_mature_like(slot):
        return True
    return False


def minutes_until_stealable(slot: dict):
    """
    距离该地块预计可偷的分钟数。
    只给「真能偷」的地排期：非黑土地、经验达阈值、未被自己偷过/未偷满。
    已可偷 -> 0；生长中 -> 成熟剩余；保护罩未结束 -> 等到保护结束。
    不满足条件 -> None（不参与偷菜唤醒，避免空转）。
    """
    if not slot.get("cropId"):
        return None
    if land_blocks_steal(slot):
        return None
    if slot.get("alreadyStolenByVisitor") or slot.get("stolenFull"):
        return None
    if not meets_steal_exp_threshold(slot):
        return None
    if is_stealable(slot):
        return 0

    rem = slot_remaining_minutes(slot)
    if rem is None:
        if _is_mature_like(slot):
            ripe_wait = 0.0
        else:
            return None
    elif rem < 0:
        ripe_wait = 0.0
    else:
        ripe_wait = float(rem)

    prot_wait = 0.0
    until = parse_farm_time(slot.get("protectedUntil"))
    if until is not None:
        prot_wait = max(0.0, (until - datetime.now()).total_seconds() / 60.0)
    elif slot.get("isProtected") is True:
        return None

    wait = max(ripe_wait, prot_wait)
    # 现在并不能偷：禁止返回 0，否则会立刻再查好友列表空转
    if wait <= 0:
        return None
    return round(float(wait), 2)


def minutes_until_explorable(slot: dict):
    """距离可翻地分钟数；已可翻 -> 0"""
    if not slot.get("cropId"):
        return None
    if slot.get("alreadyExploredByVisitor"):
        return None
    if is_explorable(slot):
        return 0
    rem = slot_remaining_minutes(slot)
    if rem is None:
        return None
    if rem < 0:
        return None
    if computed_slot_state(slot) == "growing" or rem > 0:
        return round(float(rem), 2)
    return None


def build_steal_body(action_token: str = None) -> dict:
    now = int(time.time() * 1000)
    duration = random.randint(80, 200)
    started = now - duration
    ended = now
    return {
        "_farmProof": {
            "action": "friend-steal",
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


def build_explore_body(action_token: str = None) -> dict:
    """构造翻地请求体：action=friend-explore"""
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
        "clientRequestId": f"farm-friend-explore:{ended}:{_rand_alnum(8)}",
    }


def build_help_body(action_token: str = None) -> dict:
    """构造好友浇水请求体：POST .../help，action=friend-help"""
    now = int(time.time() * 1000)
    duration = random.randint(800, 1200)
    started = now - duration
    ended = now
    return {
        "_farmProof": {
            "action": "friend-help",
            "token": action_token or "",
            "clientActionId": str(uuid.uuid4()),
            "startedAt": started,
            "endedAt": ended,
            "durationMs": duration,
            "visibility": "visible",
            "pointerType": "mouse",
            "trail": _build_mouse_trail(started, ended, points=random.randint(35, 45)),
        },
        "clientRequestId": f"farm-friend-help:{ended}:{_rand_alnum(8)}",
    }


def is_helpable(slot: dict) -> bool:
    """好友地块是否可帮忙浇水（与 canCare 一致）。"""
    return is_careable(slot)


def minutes_until_helpable(slot: dict):
    """距离好友地块可浇水的分钟数。"""
    return minutes_until_careable(slot)


def steal_slot(client: SignClient, friend_id: str, slot_index: int, action_token: str = None) -> dict:
    """
    POST 偷取指定地块。
    返回:
      status, data, ok, error_kind(None|network|business|http|unknown),
      message, skip(业务上应跳过本块，如黑土地)
    """
    api_path = f"/api/farm/friend/{friend_id}/slots/{slot_index}/steal"
    url = API_HOST + api_path
    body = build_steal_body(action_token)
    try:
        headers = client.get_signed_headers(method="POST", api_path=api_path, body=body)
        resp = requests.post(url, json=body, headers=headers, timeout=15)
    except requests.Timeout as e:
        msg = f"网络超时: {e}"
        print(f"\n[POST steal] friend={friend_id} slot={slot_index} kind=network {msg}")
        return {
            "status": None,
            "data": None,
            "ok": False,
            "error_kind": "network",
            "message": msg,
            "skip": False,
        }
    except requests.RequestException as e:
        msg = f"网络异常: {e}"
        print(f"\n[POST steal] friend={friend_id} slot={slot_index} kind=network {msg}")
        return {
            "status": None,
            "data": None,
            "ok": False,
            "error_kind": "network",
            "message": msg,
            "skip": False,
        }

    try:
        data = resp.json()
    except Exception:
        data = {"raw": (resp.text or "")[:500]}

    msg = None
    success = None
    if isinstance(data, dict):
        msg = data.get("message")
        success = data.get("success")
        # 有些失败信息在 data.data.message
        if not msg and isinstance(data.get("data"), dict):
            msg = data["data"].get("message")

    ok = resp.status_code == 200 and success is True
    if ok:
        try:
            print(
                f"\n[POST steal] friend={friend_id} slot={slot_index} "
                f"status={resp.status_code} success={success} message={msg}"
            )
        except UnicodeEncodeError:
            print(
                f"\n[POST steal] friend={friend_id} slot={slot_index} "
                f"status={resp.status_code} success={success}"
            )
        return {
            "status": resp.status_code,
            "data": data,
            "ok": True,
            "error_kind": None,
            "message": msg,
            "skip": False,
        }

    # HTTP 网关层失败也算网络/基础设施
    if resp.status_code in (408, 425, 429, 500, 502, 503, 504) or resp.status_code is None:
        kind = "network"
        skip = False
    else:
        kind = "business"
        skip = is_steal_skip_message(msg)

    tip = "跳过" if skip else ("网络" if kind == "network" else "业务")
    try:
        print(
            f"\n[POST steal] friend={friend_id} slot={slot_index} "
            f"status={resp.status_code} success={success} kind={kind}/{tip} message={msg}"
        )
    except UnicodeEncodeError:
        print(
            f"\n[POST steal] friend={friend_id} slot={slot_index} "
            f"status={resp.status_code} success={success} kind={kind}/{tip}"
        )
    return {
        "status": resp.status_code,
        "data": data,
        "ok": False,
        "error_kind": kind,
        "message": msg,
        "skip": skip,
    }


def explore_slot(client: SignClient, friend_id: str, slot_index: int, action_token: str = None) -> dict:
    """POST /api/farm/friend/{friendId}/slots/{slotIndex}/explore"""
    api_path = f"/api/farm/friend/{friend_id}/slots/{slot_index}/explore"
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
    try:
        print(f"\n[POST explore] friend={friend_id} slot={slot_index} status={resp.status_code} success={success} message={msg}")
    except UnicodeEncodeError:
        print(f"\n[POST explore] friend={friend_id} slot={slot_index} status={resp.status_code} success={success}")
    return {"status": resp.status_code, "data": data}


def help_slot(client: SignClient, friend_id: str, slot_index: int, action_token: str = None) -> dict:
    """POST /api/farm/friend/{friendId}/slots/{slotIndex}/help 好友浇水"""
    api_path = f"/api/farm/friend/{friend_id}/slots/{slot_index}/help"
    url = API_HOST + api_path
    body = build_help_body(action_token)
    headers = client.get_signed_headers(method="POST", api_path=api_path, body=body)
    resp = requests.post(url, json=body, headers=headers, timeout=15)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    msg = (data or {}).get("message") if isinstance(data, dict) else None
    success = (data or {}).get("success") if isinstance(data, dict) else None
    try:
        print(f"\n[POST help] friend={friend_id} slot={slot_index} status={resp.status_code} success={success} message={msg}")
    except UnicodeEncodeError:
        print(f"\n[POST help] friend={friend_id} slot={slot_index} status={resp.status_code} success={success}")
    return {"status": resp.status_code, "data": data}


def main(friend_id: str = None):
    friend_id = friend_id or FRIEND_ID
    client = SignClient()
    client.register_signing_key(force=False)

    farm = get_friend_farm(client, friend_id)
    data = farm.get("data") or {}
    slots = data.get("slots") or []
    print(f"exploration={data.get('explorationStatus')}")
    for s in slots:
        print(
            f"  slot={s.get('slotIndex')} {s.get('cropName')} state={s.get('state')} "
            f"stealable={is_stealable(s)} explorable={is_explorable(s)} "
            f"helpable={is_helpable(s)} careEta={minutes_until_helpable(s)} "
            f"explored={s.get('alreadyExploredByVisitor')}"
        )


if __name__ == "__main__":
    main()
