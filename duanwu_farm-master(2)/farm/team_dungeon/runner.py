# farm/team_dungeon/runner.py — 组队秘境：常驻监听 / 单次
from __future__ import annotations

import threading
import time
from typing import Optional

from config import (
    API_HOST,
    AUTO_TEAM_DUNGEON,
    TEAM_DUNGEON_BUY_EXP_POTION,
    TEAM_DUNGEON_BUY_POTION,
    TEAM_DUNGEON_CREATE_IF_EMPTY,
    TEAM_DUNGEON_DIFFICULTY,
    TEAM_DUNGEON_EXP_POTION,
    TEAM_DUNGEON_NO_PASSWORD_ONLY,
    TEAM_DUNGEON_ROOM_WAIT_SEC,
    TEAM_DUNGEON_RUN_TIMEOUT_SEC,
    TEAM_DUNGEON_STAMINA_POTION,
    TEAM_DUNGEON_USE_EXP_POTION,
)
from farm.auth import SignClient
from farm.team_dungeon.api import (
    dungeon_entry,
    ensure_exp_boost,
    ensure_stamina,
    get_character,
    get_dungeon_status,
    get_user_profile,
    playable_dungeon_entries,
    unlocked_dungeon_types,
)
from farm.team_dungeon.client import TeamDungeonClient


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
    is_premium = bool(data.get("isPremium") or data.get("is_premium") or data.get("vip"))
    if user_id is None:
        raise RuntimeError(f"无法解析用户资料: {resp}")
    return {
        "userId": int(user_id),
        "username": username,
        "experience": experience,
        "isPremium": is_premium,
    }


def _account_types(client: SignClient, status: dict | None = None) -> list[str]:
    """根据当前登录账号已解锁秘境动态决定可匹配类型。"""
    status = status if status is not None else get_dungeon_status(client)
    return unlocked_dungeon_types(status)


def _check_entry(client: SignClient) -> dict:
    """
    多类型：任一仍有次数即可继续；全部用完才 daily_done。
    返回 {ok, daily_done, entries, playable_types, required, remaining, message}
    """
    status = get_dungeon_status(client)
    types = _account_types(client, status)
    if not types:
        return {"ok": False, "message": "当前账号没有已解锁的常规秘境"}

    # 配置里写了但接口没有的类型
    missing = [t for t in types if not dungeon_entry(status, t)]
    playable = playable_dungeon_entries(status, types)
    if playable:
        required = max(int(e.get("staminaCost") or 0) for e in playable)
        remaining = sum(int(e.get("dailyRemaining") or 0) for e in playable)
        names = ",".join(e.get("name") or e.get("key") or "?" for e in playable)
        return {
            "ok": True,
            "daily_done": False,
            "entries": playable,
            "entry": playable[0],
            "playable_types": [e.get("key") for e in playable if e.get("key")],
            "account_types": types,
            "required": required,
            "remaining": remaining,
            "message": "ok",
            "summary": names,
        }

    # 没有可打的：区分「全未解锁」和「次数用完」
    unlocked_any = False
    for t in types:
        e = dungeon_entry(status, t)
        if e and e.get("unlocked"):
            unlocked_any = True
            break
    if not unlocked_any:
        tip = f"未解锁: {types}"
        if missing:
            tip = f"未找到类型 {missing}"
        return {"ok": False, "daily_done": False, "message": tip}

    return {
        "ok": False,
        "daily_done": True,
        "entries": [],
        "playable_types": [],
        "account_types": types,
        "message": f"今日账号秘境次数已用完 ({','.join(types)})",
    }


def _stamina_failure_is_final(message: str) -> bool:
    """仅永久性体力不足才本日停止监听；购买处理中等瞬时问题可重试。"""
    msg = message or ""
    if "处理中" in msg or "重复提交" in msg or "请勿重复" in msg:
        return False
    return any(
        key in msg
        for key in (
            "没有可用体力药",
            "体力已达上限",
            "补体力后仍不足",
            "使用体力药失败",
        )
    )


def _ensure_stamina_for(client: SignClient, required: int) -> dict:
    if required <= 0:
        char = get_character(client)
        cdata = (char.get("data") or {}).get("data") or {}
        return {"ok": True, "stamina": int(cdata.get("stamina") or 0), "used": []}
    return ensure_stamina(
        client,
        required=required,
        preferred_item=TEAM_DUNGEON_STAMINA_POTION,
        allow_buy=TEAM_DUNGEON_BUY_POTION,
    )


def _ensure_exp_for(client: SignClient) -> dict:
    return ensure_exp_boost(
        client,
        preferred_item=TEAM_DUNGEON_EXP_POTION,
        allow_buy=TEAM_DUNGEON_BUY_EXP_POTION,
        use_potion=TEAM_DUNGEON_USE_EXP_POTION,
    )


def _print_loot(result: dict, user_id: int):
    complete = result.get("complete") or {}
    for item in complete.get("lootSummaries") or []:
        if int(item.get("userId") or 0) == user_id:
            print(
                f"[team_dungeon] 掉落 experience={item.get('experience')} "
                f"items={item.get('items') or item.get('rewards')}"
            )
            return


def run_team_dungeon_once(client: SignClient | None = None) -> dict:
    """跑一次（有房才进；默认短等）。常驻请用 run_team_dungeon_watch。"""
    if not AUTO_TEAM_DUNGEON:
        return {"ok": False, "skipped": True, "message": "AUTO_TEAM_DUNGEON=False"}

    own_client = client is None
    client = client or SignClient()
    if own_client:
        client.register_signing_key()

    profile = _profile_user(client)
    print(
        f"[team_dungeon] 账号 {profile['username']}#{profile['userId']} "
        f"exp={profile['experience']} vip={profile.get('isPremium')}"
    )
    check = _check_entry(client)
    if not check.get("ok"):
        return check

    entry = check["entry"]
    required = check["required"]
    playable_types = check.get("playable_types") or check.get("account_types") or []
    print(
        f"[team_dungeon] 可打 {check.get('summary') or playable_types} "
        f"剩余合计 {check['remaining']} 体力需求>={required} "
        f"账号已解锁={check.get('account_types') or playable_types}"
    )

    exp_res = _ensure_exp_for(client)
    if not exp_res.get("ok"):
        return {"ok": False, "message": exp_res.get("message") or "经验水准备失败", "exp": exp_res}
    print(
        f"[team_dungeon] 经验水就绪 boost={exp_res.get('boost')} used={exp_res.get('used')}"
    )

    stamina_res = _ensure_stamina_for(client, required)
    if not stamina_res.get("ok"):
        return {"ok": False, "message": stamina_res.get("message") or "体力不足", "stamina": stamina_res}
    print(f"[team_dungeon] 体力就绪 stamina={stamina_res.get('stamina')}")

    ws = TeamDungeonClient(
        token=client.token,
        user_id=profile["userId"],
        username=profile["username"],
        experience=profile["experience"],
        api_host=API_HOST,
        dungeon_type=playable_types,
        difficulty=TEAM_DUNGEON_DIFFICULTY,
        no_password_only=TEAM_DUNGEON_NO_PASSWORD_ONLY,
        create_if_empty=TEAM_DUNGEON_CREATE_IF_EMPTY,
        room_wait_sec=TEAM_DUNGEON_ROOM_WAIT_SEC,
        run_timeout_sec=TEAM_DUNGEON_RUN_TIMEOUT_SEC,
    )
    try:
        ws.connect()
        result = ws.run_session(wait_forever=False)
    finally:
        ws.disconnect()

    _print_loot(result, profile["userId"])
    print(f"[team_dungeon] 结果 ok={result.get('ok')} msg={result.get('message')}")
    return result


def run_team_dungeon_watch(
    stop_event: Optional[threading.Event] = None,
    client: SignClient | None = None,
) -> dict:
    """
    常驻 WebSocket 监听大厅：有符合房间就加入，直到今日次数用完。
    返回 {ok, daily_done, runs, message}
    """
    if not AUTO_TEAM_DUNGEON:
        return {"ok": False, "skipped": True, "daily_done": True, "message": "AUTO_TEAM_DUNGEON=False"}

    own_client = client is None
    client = client or SignClient(quiet=True)
    if own_client:
        client.register_signing_key()

    profile = _profile_user(client)
    types = _account_types(client)
    print(
        f"[team_dungeon] 监听启动 {profile['username']}#{profile['userId']} "
        f"types={types}(账号已解锁) 无密码={TEAM_DUNGEON_NO_PASSWORD_ONLY}"
    )

    check = _check_entry(client)
    if check.get("daily_done"):
        print(f"[team_dungeon] {check.get('message')}")
        return {"ok": True, "daily_done": True, "runs": 0, "message": check.get("message")}
    if not check.get("ok"):
        return {"ok": False, "daily_done": False, "runs": 0, "message": check.get("message")}

    ws = TeamDungeonClient(
        token=client.token,
        user_id=profile["userId"],
        username=profile["username"],
        experience=profile["experience"],
        api_host=API_HOST,
        dungeon_type=check.get("playable_types") or types,
        difficulty=TEAM_DUNGEON_DIFFICULTY,
        no_password_only=TEAM_DUNGEON_NO_PASSWORD_ONLY,
        create_if_empty=False,  # 监听模式绝不自建
        room_wait_sec=TEAM_DUNGEON_ROOM_WAIT_SEC,
        run_timeout_sec=TEAM_DUNGEON_RUN_TIMEOUT_SEC,
    )

    runs = 0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                return {"ok": False, "stopped": True, "daily_done": False, "runs": runs, "message": "已停止"}

            check = _check_entry(client)
            if check.get("daily_done"):
                print(f"[team_dungeon] {check.get('message')}，本日监听结束")
                return {
                    "ok": True,
                    "daily_done": True,
                    "runs": runs,
                    "message": check.get("message"),
                }
            if not check.get("ok"):
                return {
                    "ok": False,
                    "daily_done": False,
                    "runs": runs,
                    "message": check.get("message"),
                }

            playable_types = check.get("playable_types") or types
            ws.set_dungeon_types(playable_types)

            required = int(check.get("required") or 0)
            print(
                f"[team_dungeon] 可打 {check.get('summary') or playable_types} "
                f"剩余合计 {check.get('remaining')}，等待公开房…"
            )

            exp_res = _ensure_exp_for(client)
            if not exp_res.get("ok"):
                msg = exp_res.get("message") or "经验水准备失败"
                print(f"[team_dungeon] {msg}，停止监听")
                return {
                    "ok": False,
                    "daily_done": True,
                    "runs": runs,
                    "message": msg,
                    "exp": exp_res,
                }
            print(
                f"[team_dungeon] 经验水就绪 boost={exp_res.get('boost')} used={exp_res.get('used')}"
            )

            stamina_res = _ensure_stamina_for(client, required)
            if not stamina_res.get("ok"):
                msg = stamina_res.get("message") or "体力不足"
                final = _stamina_failure_is_final(msg)
                print(
                    f"[team_dungeon] {msg}，"
                    f"{'停止监听' if final else '稍后重试'}"
                )
                return {
                    "ok": False,
                    "daily_done": final,
                    "runs": runs,
                    "message": msg,
                    "stamina": stamina_res,
                }

            if not ws.sio.connected:
                ws.connect()
                ws.refresh_rooms()

            result = ws.run_session(wait_forever=True, stop_event=stop_event)
            if result.get("stopped"):
                return {
                    "ok": False,
                    "stopped": True,
                    "daily_done": False,
                    "runs": runs,
                    "message": result.get("message"),
                }

            _print_loot(result, profile["userId"])
            print(f"[team_dungeon] 本局 ok={result.get('ok')} msg={result.get('message')}")
            if result.get("ok"):
                runs += 1

            # 稍作间隔再听下一房；连接保持
            time.sleep(2)
    finally:
        ws.disconnect()


if __name__ == "__main__":
    run_team_dungeon_watch()
