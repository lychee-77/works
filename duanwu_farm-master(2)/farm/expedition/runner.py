# farm/expedition/runner.py — 远征（raid）常驻 WebSocket 监听
from __future__ import annotations

import threading
import time
from typing import Optional

from config import (
    API_HOST,
    AUTO_EXPEDITION,
    EXPEDITION_CREATE_IF_EMPTY,
    EXPEDITION_NO_PASSWORD_ONLY,
    EXPEDITION_ROOM_WAIT_SEC,
    EXPEDITION_RUN_TIMEOUT_SEC,
)
from farm.auth import SignClient
from farm.expedition.api import check_entry, profile_user
from farm.team_dungeon.client import TeamDungeonClient


def _entry_limit_message(message: str) -> bool:
    msg = message or ""
    return any(
        key in msg
        for key in (
            "次数已用完",
            "次数已尽",
            "非远征开放日",
            "本周远征",
            "今日远征",
        )
    )


def run_expedition_watch(
    stop_event: Optional[threading.Event] = None,
    client: SignClient | None = None,
) -> dict:
    """
    常驻 WebSocket 监听大厅：gameType=raid，有公开房即加入。
    返回 {ok, daily_done, runs, message}
    """
    if not AUTO_EXPEDITION:
        return {"ok": False, "skipped": True, "daily_done": True, "message": "AUTO_EXPEDITION=False"}

    own_client = client is None
    client = client or SignClient(quiet=True)
    if own_client:
        client.register_signing_key()

    profile = profile_user(client)
    print(
        f"[expedition] 监听启动 {profile['username']}#{profile['userId']} "
        f"gameType=raid 无密码={EXPEDITION_NO_PASSWORD_ONLY}"
    )

    check = check_entry(client)
    if check.get("daily_done"):
        print(f"[expedition] {check.get('message')}")
        return {"ok": True, "daily_done": True, "runs": 0, "message": check.get("message")}
    if not check.get("ok"):
        return {"ok": False, "daily_done": False, "runs": 0, "message": check.get("message")}

    ws = TeamDungeonClient(
        token=client.token,
        user_id=profile["userId"],
        username=profile["username"],
        experience=profile["experience"],
        api_host=API_HOST,
        dungeon_type=[],
        difficulty="",
        no_password_only=EXPEDITION_NO_PASSWORD_ONLY,
        create_if_empty=EXPEDITION_CREATE_IF_EMPTY,
        room_wait_sec=EXPEDITION_ROOM_WAIT_SEC,
        run_timeout_sec=EXPEDITION_RUN_TIMEOUT_SEC,
        game_type="raid",
        log_tag="expedition",
        enable_host_settle=False,
        filter_dungeon_types=False,
        max_players_default=8,
        min_players=2,
        room_name=f"{profile['username']}的远征团本",
    )

    runs = 0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                return {"ok": False, "stopped": True, "daily_done": False, "runs": runs, "message": "已停止"}

            check = check_entry(client)
            if check.get("daily_done"):
                print(f"[expedition] {check.get('message')}，本日监听结束")
                return {
                    "ok": True,
                    "daily_done": True,
                    "runs": runs,
                    "message": check.get("message"),
                }
            if not check.get("ok"):
                msg = check.get("message") or "远征暂不可参与"
                daily_done = _entry_limit_message(msg)
                return {
                    "ok": False,
                    "daily_done": daily_done,
                    "runs": runs,
                    "message": msg,
                }

            print(
                f"[expedition] 可参与 本周剩余约 {check.get('remaining')} 次，等待公开房…"
            )

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

            msg = result.get("message") or ""
            print(f"[expedition] 本局 ok={result.get('ok')} msg={msg}")
            if _entry_limit_message(msg):
                return {
                    "ok": False,
                    "daily_done": True,
                    "runs": runs,
                    "message": msg,
                }
            if result.get("ok"):
                runs += 1

            time.sleep(2)
    finally:
        ws.disconnect()


if __name__ == "__main__":
    run_expedition_watch()
