# farm/expedition/api.py — 远征（raid）REST
from datetime import datetime

import requests
from config import API_HOST, EXPEDITION_MIN_LEVEL
from farm.auth import SignClient

_CN_DOW = {
    "周一": 0,
    "周二": 1,
    "周三": 2,
    "周四": 3,
    "周五": 4,
    "周六": 5,
    "周日": 6,
    "周天": 6,
}


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


def get_raid_status(client: SignClient) -> dict:
    """GET /api/qpet/raid/bean-status — 含本周/今日次数与开放日。"""
    return _req(client, "GET", "/api/qpet/raid/bean-status")


def _raid_payload(status_resp: dict) -> dict:
    outer = status_resp.get("data") or {}
    if isinstance(outer, dict) and outer.get("success") is True:
        inner = outer.get("data")
        if isinstance(inner, dict):
            return inner
        return outer
    if isinstance(outer, dict) and (
        "weeklyLimit" in outer or "weeklyUsed" in outer or "dailyLimit" in outer
    ):
        return outer
    inner = outer.get("data") if isinstance(outer, dict) else {}
    return inner if isinstance(inner, dict) else {}


def _is_open_today(open_days) -> bool:
    if not open_days:
        return True
    allowed: set[int] = set()
    for item in open_days:
        if isinstance(item, int):
            allowed.add(int(item) % 7)
            continue
        text = str(item).strip()
        if text in _CN_DOW:
            allowed.add(_CN_DOW[text])
        elif text.isdigit():
            allowed.add(int(text) % 7)
    if not allowed:
        return True
    return datetime.now().weekday() in allowed


def _profile_user(client: SignClient) -> dict:
    resp = get_user_profile(client)
    data = (resp.get("data") or {}).get("data") or (resp.get("data") or {})
    if isinstance(data, dict) and "user" in data and isinstance(data["user"], dict):
        data = {**data, **data["user"]}
    user_id = data.get("id") or data.get("userId")
    username = (
        data.get("nickname")
        or data.get("nickName")
        or data.get("username")
        or data.get("userName")
        or str(user_id or "")
    )
    experience = int(data.get("experience") or data.get("exp") or 0)
    if user_id is None:
        raise RuntimeError(f"无法解析用户资料: {resp}")
    return {
        "userId": int(user_id),
        "username": username,
        "experience": experience,
    }


def check_entry(client: SignClient) -> dict:
    """
    远征是否还可参与。
    返回 {ok, daily_done, remaining, message, ...}
    """
    char = get_character(client)
    cdata = (char.get("data") or {}).get("data") or {}
    level = int(cdata.get("level") or 0)
    min_level = int(EXPEDITION_MIN_LEVEL)
    if level < min_level:
        return {
            "ok": False,
            "daily_done": False,
            "message": f"等级不足，需要 Lv.{min_level}（当前 Lv.{level}）",
        }

    status = get_raid_status(client)
    info = _raid_payload(status)
    if not info:
        return {"ok": False, "daily_done": False, "message": "无法获取远征状态"}

    open_days = info.get("openDays") or info.get("allowedWeekdayLabels") or []
    if not _is_open_today(open_days):
        days_txt = " / ".join(str(x) for x in open_days) or "周二 / 周四 / 周日"
        return {
            "ok": False,
            "daily_done": True,
            "message": f"今日非远征开放日（{days_txt}）",
        }

    weekly_used = int(info.get("weeklyUsed") or 0)
    weekly_limit = int(info.get("weeklyLimit") or 3)
    daily_used = int(info.get("dailyUsed") or 0)
    daily_limit = int(info.get("dailyLimit") or 0)

    if weekly_limit > 0 and weekly_used >= weekly_limit:
        return {
            "ok": False,
            "daily_done": True,
            "remaining": 0,
            "message": f"本周远征次数已用完 ({weekly_used}/{weekly_limit})",
        }
    if daily_limit > 0 and daily_used >= daily_limit:
        return {
            "ok": False,
            "daily_done": True,
            "remaining": 0,
            "message": f"今日远征次数已用完 ({daily_used}/{daily_limit})",
        }

    remaining = max(0, weekly_limit - weekly_used) if weekly_limit > 0 else 1
    return {
        "ok": True,
        "daily_done": False,
        "remaining": remaining,
        "weeklyUsed": weekly_used,
        "weeklyLimit": weekly_limit,
        "dailyUsed": daily_used,
        "dailyLimit": daily_limit,
        "openDays": open_days,
        "message": "ok",
    }


def profile_user(client: SignClient) -> dict:
    return _profile_user(client)
