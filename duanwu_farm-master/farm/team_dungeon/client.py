# farm/team_dungeon/client.py — Socket.IO 游戏大厅客户端
from __future__ import annotations

import threading
import time
from datetime import date
from typing import Optional

import socketio

from config import TEAM_DUNGEON_ROOM_TIMEOUT_BLOCK
from farm.team_dungeon.battle import build_settle_summary, run_team_dungeon
from farm.team_dungeon.secure import build_secure_payload, settle_delay_seconds, stamp_received

# 按自然日记录各房间超时次数，达上限后本日不再加入
_room_timeout_day: date | None = None
_room_timeout_counts: dict[str, int] = {}


def _roll_room_timeout_day() -> None:
    global _room_timeout_day, _room_timeout_counts
    today = date.today()
    if _room_timeout_day != today:
        _room_timeout_day = today
        _room_timeout_counts = {}


def is_room_blocked_today(room_id: str) -> bool:
    _roll_room_timeout_day()
    limit = max(1, int(TEAM_DUNGEON_ROOM_TIMEOUT_BLOCK))
    return _room_timeout_counts.get(str(room_id), 0) >= limit


def record_room_timeout_today(room_id: str, *, log_tag: str = "team_dungeon") -> int:
    """记录房间超时并返回当日累计次数。"""
    _roll_room_timeout_day()
    limit = max(1, int(TEAM_DUNGEON_ROOM_TIMEOUT_BLOCK))
    rid = str(room_id)
    count = _room_timeout_counts.get(rid, 0) + 1
    _room_timeout_counts[rid] = count
    if count >= limit:
        print(f"[{log_tag}] 房间 {rid} 今日超时 {count} 次，本日不再加入")
    else:
        print(f"[{log_tag}] 房间 {rid} 今日超时 {count}/{limit} 次")
    return count


def ws_url_from_api_host(api_host: str) -> str:
    base = (api_host or "").rstrip("/")
    if base.endswith("/api"):
        return base[:-4]
    return base


class TeamDungeonClient:
    """青铜组队秘境：匹配公开房加入 → 准备 → 开局 → 结算(仅房主) → 退房"""

    def __init__(
        self,
        *,
        token: str,
        user_id: int | str,
        username: str,
        experience: int = 0,
        api_host: str,
        dungeon_type: str | list[str] = "bronze",
        difficulty: str = "normal",
        no_password_only: bool = True,
        create_if_empty: bool = False,
        room_wait_sec: float = 180,
        run_timeout_sec: float = 600,
        room_name: str = "",
        min_players: int = 2,
        game_type: str = "team_dungeon",
        log_tag: str | None = None,
        enable_host_settle: bool = True,
        filter_dungeon_types: bool = True,
        max_players_default: int = 4,
    ):
        self.token = token
        self.user_id = int(user_id)
        self.username = username or str(user_id)
        self.experience = int(experience or 0)
        self.ws_url = ws_url_from_api_host(api_host)
        self.game_type = str(game_type or "team_dungeon")
        self.log_tag = log_tag or (
            "expedition" if self.game_type == "raid" else "team_dungeon"
        )
        self.enable_host_settle = bool(enable_host_settle)
        self.filter_dungeon_types = bool(filter_dungeon_types)
        self.max_players_default = int(max_players_default)
        self.complete_event = f"{self.game_type}_complete"
        self.error_event = f"{self.game_type}_error"
        self.set_dungeon_types(dungeon_type)
        self.difficulty = difficulty
        self.no_password_only = bool(no_password_only)
        self.create_if_empty = bool(create_if_empty)
        self.room_wait_sec = float(room_wait_sec)
        self.run_timeout_sec = float(run_timeout_sec)
        self.room_name = room_name or (
            f"{self.username}的远征团本"
            if self.game_type == "raid"
            else f"{self.username}的组队秘境"
        )
        self.min_players = int(min_players)

        self.sio = socketio.Client(
            reconnection=True,
            reconnection_attempts=5,
            reconnection_delay=1,
            reconnection_delay_max=5,
            logger=False,
            engineio_logger=False,
        )
        self._lock = threading.RLock()
        self._rooms: dict[str, dict] = {}
        self._room: Optional[dict] = None
        self._auth_ok = threading.Event()
        self._rooms_ready = threading.Event()
        self._match_event = threading.Event()
        self._joined = threading.Event()
        self._left = threading.Event()
        self._started = threading.Event()
        self._complete = threading.Event()
        self._error: Optional[str] = None
        self._complete_payload: Optional[dict] = None
        self._settle_done = False
        self._settle_error: Optional[str] = None
        self._ready_flag = False
        self._settle_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._last_rooms_log = 0.0

        self._wire_handlers()

    def _log(self, msg: str):
        print(f"[{self.log_tag}] {msg}")

    # ---------- helpers ----------
    @staticmethod
    def normalize_dungeon_types(dungeon_type: str | list[str] | tuple | None) -> list[str]:
        if dungeon_type is None:
            return []
        if isinstance(dungeon_type, str):
            return [dungeon_type] if dungeon_type else []
        return [str(x) for x in dungeon_type if x]

    def set_dungeon_types(self, dungeon_type: str | list[str] | tuple | None):
        """更新可加入的秘境类型列表（配置顺序即优先顺序）。"""
        types = self.normalize_dungeon_types(dungeon_type)
        self.dungeon_types = types
        # 兼容旧字段：创建房间时用第一个
        self.dungeon_type = types[0] if types else "bronze"

    def _set_error(self, msg: str, *, fatal: bool = True):
        with self._lock:
            self._error = msg
        prefix = "错误" if fatal else "提示"
        self._log(f"{prefix}: {msg}")

    def _clear_error(self):
        with self._lock:
            self._error = None

    def _update_room(self, room: Optional[dict]):
        with self._lock:
            self._room = room
            if room and room.get("id"):
                self._rooms[str(room["id"])] = room

    def _upsert_room_list_item(self, room: dict):
        if not room or not room.get("id"):
            return
        rid = str(room["id"])
        with self._lock:
            prev = self._rooms.get(rid) or {}
            self._rooms[rid] = {**prev, **room}
            if self._room and str(self._room.get("id")) == rid:
                self._room = {**self._room, **room}

    def current_room(self) -> Optional[dict]:
        with self._lock:
            return dict(self._room) if self._room else None

    def is_host(self, room: Optional[dict] = None) -> bool:
        room = room or self.current_room()
        if not room:
            return False
        if room.get("hostId") is not None:
            return int(room.get("hostId")) == self.user_id
        for p in room.get("players") or []:
            if int(p.get("id") or 0) == self.user_id:
                return bool(p.get("isHost"))
        return False

    def _player_count(self, room: Optional[dict] = None) -> int:
        room = room or self.current_room()
        if not room:
            return 0
        players = room.get("players") or []
        if players:
            return len(players)
        return int(room.get("currentPlayers") or 0)

    def _can_start(self, room: Optional[dict] = None) -> bool:
        room = room or self.current_room()
        if not room or not self.is_host(room):
            return False
        if room.get("status") not in (None, "waiting"):
            return False
        players = room.get("players") or []
        if self._player_count(room) < self.min_players:
            return False
        for p in players:
            if bool(p.get("isHost")):
                continue
            if int(p.get("id") or 0) == self.user_id:
                continue
            if not p.get("isReady"):
                return False
        return True

    def _match_room(self, room: dict, *, require_difficulty: bool = True) -> bool:
        rid = str(room.get("id") or "")
        if rid and is_room_blocked_today(rid):
            return False
        if (room.get("gameType") or "") != self.game_type:
            return False
        if self.filter_dungeon_types:
            room_type = room.get("dungeonType") or ""
            if room_type not in self.dungeon_types:
                return False
            if require_difficulty and self.difficulty:
                if (room.get("difficulty") or "normal") != self.difficulty:
                    return False
        if (room.get("status") or "waiting") != "waiting":
            return False
        cur = int(room.get("currentPlayers") or len(room.get("players") or []) or 0)
        mx = int(room.get("maxPlayers") or self.max_players_default)
        if cur >= mx:
            return False
        if self.no_password_only and room.get("hasPassword"):
            return False
        return True

    def pick_join_target(self) -> Optional[dict]:
        with self._lock:
            rooms = list(self._rooms.values())
        if not self.filter_dungeon_types:
            cands = [r for r in rooms if self._match_room(r, require_difficulty=False)]
            cands.sort(
                key=lambda r: (
                    -int(r.get("currentPlayers") or len(r.get("players") or []) or 0),
                    str(r.get("id") or ""),
                )
            )
            return cands[0] if cands else None
        # 先匹配指定难度，没有则放宽到同秘境任意难度（仍无密码）
        cands = [r for r in rooms if self._match_room(r, require_difficulty=True)]
        if not cands and self.difficulty:
            cands = [r for r in rooms if self._match_room(r, require_difficulty=False)]

        def _type_rank(r: dict) -> int:
            t = r.get("dungeonType") or ""
            try:
                return self.dungeon_types.index(t)
            except ValueError:
                return 999

        cands.sort(
            key=lambda r: (
                _type_rank(r),
                0 if (not self.difficulty or (r.get("difficulty") or "normal") == self.difficulty) else 1,
                -int(r.get("currentPlayers") or len(r.get("players") or []) or 0),
                str(r.get("id") or ""),
            )
        )
        return cands[0] if cands else None

    def _lobby_snapshot(self) -> str:
        with self._lock:
            rooms = list(self._rooms.values())
        typed = [r for r in rooms if (r.get("gameType") or "") == self.game_type]
        open_ok = [
            r for r in typed
            if self._match_room(r, require_difficulty=not bool(self.difficulty))
        ]
        if self.filter_dungeon_types:
            types_txt = ",".join(self.dungeon_types) or "-"
            return (
                f"大厅 {self.game_type}={len(typed)} "
                f"可进[{types_txt}]公开房={len(open_ok)}"
            )
        return f"大厅 {self.game_type}={len(typed)} 可进公开房={len(open_ok)}"

    def _notify_rooms_changed(self):
        self._rooms_ready.set()
        # 仅在不在房时响应当前匹配
        if self.current_room() is None and self.pick_join_target():
            self._match_event.set()
        else:
            self._match_event.clear()

    def reset_run_state(self):
        """一局结束后清状态，继续监听下一局。"""
        self._joined.clear()
        self._left.clear()
        self._started.clear()
        self._complete.clear()
        self._clear_error()
        self._complete_payload = None
        self._settle_done = False
        self._settle_error = None
        self._ready_flag = False
        self._settle_thread = None
        self._update_room(None)
        self._match_event.clear()
        self._notify_rooms_changed()
    # ---------- socket handlers ----------
    def _wire_handlers(self):
        sio = self.sio

        @sio.event
        def connect():
            self._log("WS 已连接，发送 authenticate")
            sio.emit(
                "authenticate",
                {"token": self.token, "userId": self.user_id, "username": self.username},
            )

        @sio.event
        def disconnect():
            self._log("WS 断开")
            self._auth_ok.clear()

        @sio.on("authenticated")
        def on_authenticated(data):
            if data and data.get("success"):
                self._log("认证成功，加入大厅")
                sio.emit("join_game_lobby")
                self._auth_ok.set()
            else:
                self._set_error((data or {}).get("message") or "认证失败")

        @sio.on("connect_error")
        def on_connect_error(data):
            self._set_error(f"连接失败: {data}")

        @sio.on("game_rooms_list")
        def on_rooms_list(data):
            rooms = (data or {}).get("rooms") or []
            with self._lock:
                self._rooms = {str(r["id"]): r for r in rooms if r and r.get("id")}
            self._notify_rooms_changed()
            # 大厅刷房很频繁，节流日志，避免刷屏卡控制台拖慢农场线程
            now = time.time()
            if now - self._last_rooms_log >= 15:
                self._last_rooms_log = now
                self._log(f"房间列表 {len(rooms)} 个 ({self._lobby_snapshot()})")

        @sio.on("game_rooms_delta")
        def on_rooms_delta(data):
            for r in (data or {}).get("upserts") or []:
                self._upsert_room_list_item(r)
            for rid in (data or {}).get("deletes") or []:
                with self._lock:
                    self._rooms.pop(str(rid), None)
            self._notify_rooms_changed()

        @sio.on("game_room_created")
        def on_room_created(data):
            room = (data or {}).get("room")
            self._update_room(room)
            self._joined.set()
            self._log(f"已创建房间 {room and room.get('id')}")

        @sio.on("game_room_joined")
        def on_room_joined(data):
            room = (data or {}).get("room")
            self._update_room(room)
            self._joined.set()
            self._log(f"已加入房间 {room and room.get('id')}")

        @sio.on("game_room_updated")
        def on_room_updated(data):
            room = (data or {}).get("room")
            if not room:
                return
            self._upsert_room_list_item(room)
            with self._lock:
                cur = self._room
            if cur and str(cur.get("id")) == str(room.get("id")):
                self._update_room(room)
            else:
                # 可能被同步进房间
                players = room.get("players") or []
                if any(int(p.get("id") or 0) == self.user_id for p in players):
                    self._update_room(room)
                    self._joined.set()

        @sio.on("game_player_joined")
        def on_player_joined(data):
            room = self.current_room()
            if not room or str(room.get("id")) != str((data or {}).get("roomId")):
                return
            player = (data or {}).get("player")
            if not player:
                return
            players = list(room.get("players") or [])
            if not any(int(p.get("id") or 0) == int(player.get("id") or 0) for p in players):
                players.append(player)
            room = {**room, "players": players, "currentPlayers": len(players)}
            self._update_room(room)
            self._log(f"玩家加入: {player.get('username') or player.get('id')}")

        @sio.on("game_player_left")
        def on_player_left(data):
            room = self.current_room()
            if not room or str(room.get("id")) != str((data or {}).get("roomId")):
                return
            pid = (data or {}).get("playerId")
            players = [p for p in (room.get("players") or []) if int(p.get("id") or 0) != int(pid or 0)]
            room = {**room, "players": players, "currentPlayers": len(players)}
            self._update_room(room)

        @sio.on("game_player_ready_changed")
        def on_ready_changed(data):
            room = self.current_room()
            if not room or str(room.get("id")) != str((data or {}).get("roomId")):
                return
            players = []
            for p in room.get("players") or []:
                if int(p.get("id") or 0) == int((data or {}).get("playerId") or 0):
                    p = {**p, "isReady": bool((data or {}).get("isReady"))}
                players.append(p)
            self._update_room({**room, "players": players})

        @sio.on("game_started")
        def on_started(data):
            self._started.set()
            self._log(f"游戏开始 gameType={(data or {}).get('gameType')}")

        @sio.on("game_left_room")
        def on_left(_data):
            self._update_room(None)
            self._left.set()
            self._log("已离开房间")

        @sio.on("game_error")
        def on_game_error(data):
            # 进房/创房业务失败由调用方处理，不当作未捕获崩溃
            self._set_error((data or {}).get("message") or str(data), fatal=False)

        if self.enable_host_settle:

            @sio.on("team_dungeon_prepare")
            def on_prepare(data):
                # 绝不能在 WS 收包线程里跑战斗/sleep，否则心跳停、房间挂住，农场收菜也会受影响
                try:
                    self._start_settle_async(data or {})
                except Exception as e:
                    self._settle_error = str(e)
                    self._set_error(f"结算失败: {e}")
        else:
            prepare_event = f"{self.game_type}_prepare"
            if prepare_event != "team_dungeon_prepare":

                @sio.on(prepare_event)
                def on_mode_prepare(_data):
                    self._started.set()
                    self._log("收到 prepare，等待副本完成")

        @sio.on(self.complete_event)
        def on_complete(data):
            self._complete_payload = data
            self._complete.set()
            self._log("副本完成")

        @sio.on(self.error_event)
        def on_game_error_event(data):
            self._set_error((data or {}).get("message") or f"{self.game_type} 异常")
            self._complete.set()

    def _interruptible_sleep(self, seconds: float):
        """可被 stop/disconnect 打断的 sleep。"""
        deadline = time.time() + max(0.0, float(seconds))
        while time.time() < deadline:
            if self._stop_flag.is_set():
                raise InterruptedError("秘境已停止，中断结算等待")
            time.sleep(min(0.5, deadline - time.time()))

    def _start_settle_async(self, data: dict):
        room = self.current_room()
        room_id = data.get("roomId") or (room or {}).get("id")
        if not room_id:
            return
        if not self.is_host():
            print("[team_dungeon] 收到 prepare（客人，等待房主结算）")
            return
        if self._settle_done:
            return
        if self._settle_thread is not None and self._settle_thread.is_alive():
            return

        def worker():
            try:
                self._handle_prepare(data)
            except InterruptedError as e:
                self._settle_error = str(e)
                print(f"[team_dungeon] {e}")
            except Exception as e:
                self._settle_error = str(e)
                self._set_error(f"结算失败: {e}")

        t = threading.Thread(target=worker, name="team-dungeon-settle", daemon=True)
        self._settle_thread = t
        t.start()

    def _handle_prepare(self, data: dict):
        room = self.current_room()
        room_id = data.get("roomId") or (room or {}).get("id")
        if not room_id:
            return
        # 仅房主提交结算；客人等 complete
        if not self.is_host():
            print("[team_dungeon] 收到 prepare（客人，等待房主结算）")
            return
        if self._settle_done:
            return
        client_battle = stamp_received(dict(data.get("clientBattle") or {}))
        battle_token = data.get("battleToken")
        print("[team_dungeon] 房主运行战斗引擎...")
        result = run_team_dungeon(data)
        if self._stop_flag.is_set():
            raise InterruptedError("秘境已停止，中断结算")
        summary = build_settle_summary(result)
        delay = settle_delay_seconds(client_battle)
        if delay > 0:
            print(f"[team_dungeon] 等待结算延迟 {delay:.1f}s")
            self._interruptible_sleep(delay)
        secure = build_secure_payload(client_battle, battle_token, summary)
        self.sio.emit(
            "team_dungeon_settle",
            {"roomId": room_id, "battleToken": battle_token, "secure": secure},
        )
        self._settle_done = True
        print(
            f"[team_dungeon] 已提交结算 floors={summary.get('floorsCleared')} "
            f"died={summary.get('died')}"
        )

    # ---------- actions ----------
    def connect(self, timeout: float = 20):
        self._stop_flag.clear()
        self._log(f"连接 {self.ws_url}")
        self.sio.connect(
            self.ws_url,
            transports=["websocket", "polling"],
            wait_timeout=timeout,
            socketio_path="socket.io",
        )
        if not self._auth_ok.wait(timeout):
            raise TimeoutError(self._error or "认证超时")
        if self._error:
            raise RuntimeError(self._error)

    def disconnect(self):
        self._stop_flag.set()
        try:
            if self.sio.connected:
                try:
                    self.sio.emit("leave_game_lobby")
                except Exception:
                    pass
                try:
                    room = self.current_room()
                    if room:
                        self.sio.emit(
                            "game_leave_room",
                            {"userId": self.user_id, "roomId": room.get("id")},
                        )
                except Exception:
                    pass
                self.sio.disconnect()
        except Exception:
            pass
        t = self._settle_thread
        if t is not None and t.is_alive():
            t.join(timeout=2)
        self._settle_thread = None
        self._update_room(None)

    def refresh_rooms(self, timeout: float = 8) -> list[dict]:
        self._rooms_ready.clear()
        self.sio.emit("game_get_rooms", {"userId": self.user_id, "forceRefresh": True})
        self._rooms_ready.wait(timeout)
        with self._lock:
            return list(self._rooms.values())

    def _wait_joined(self, timeout: float, action: str) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._error:
                raise RuntimeError(self._error)
            if self._joined.wait(0.25):
                break
        else:
            raise TimeoutError(self._error or f"{action}超时")
        if self._error:
            raise RuntimeError(self._error)
        room = self.current_room()
        if not room:
            raise RuntimeError(f"{action}成功但房间为空")
        return room

    def join_room(self, room_id: str, timeout: float = 8) -> dict:
        self._joined.clear()
        self._error = None
        self.sio.emit(
            "game_join_room",
            {
                "userId": self.user_id,
                "username": self.username,
                "roomId": room_id,
                "experience": self.experience,
                "roomPassword": "",
            },
        )
        return self._wait_joined(timeout, "加入房间")

    def create_room(self, timeout: float = 8) -> dict:
        self._joined.clear()
        self._error = None
        payload = {
            "userId": self.user_id,
            "username": self.username,
            "roomName": self.room_name,
            "gameType": self.game_type,
            "betAmount": 0,
            "maxPlayers": self.max_players_default,
            "roomPassword": "",
        }
        if self.filter_dungeon_types:
            payload["dungeonType"] = self.dungeon_type
            payload["difficulty"] = self.difficulty
        self.sio.emit("game_create_room", payload)
        return self._wait_joined(timeout, "创建房间")
    def set_ready(self, ready: bool = True):
        room = self.current_room()
        if not room:
            return
        if self.is_host(room):
            return
        self.sio.emit(
            "game_player_ready",
            {"userId": self.user_id, "roomId": room.get("id"), "isReady": bool(ready)},
        )
        self._ready_flag = bool(ready)
        self._log(f"已发送准备 isReady={ready}")

    def start_game(self):
        room = self.current_room()
        if not room:
            return
        self.sio.emit("game_start", {"userId": self.user_id, "roomId": room.get("id")})
        self._log("已发送开始游戏")

    def leave_room(self, timeout: float = 5):
        room = self.current_room()
        if not room:
            return
        self._left.clear()
        self.sio.emit("game_leave_room", {"userId": self.user_id, "roomId": room.get("id")})
        self._left.wait(timeout)
        self._update_room(None)

    def try_join_any(self, tried: set[str] | None = None, *, refresh: bool = True) -> Optional[dict]:
        """尝试加入一个合适房间；失败返回 None。"""
        tried = tried if tried is not None else set()
        if refresh:
            self.refresh_rooms()
        while True:
            target = self.pick_join_target()
            if not target:
                return None
            rid = str(target.get("id"))
            if rid in tried:
                with self._lock:
                    rooms = list(self._rooms.values())
                others = []
                for r in rooms:
                    oid = str(r.get("id") or "")
                    if oid in tried:
                        continue
                    if self._match_room(r, require_difficulty=bool(self.filter_dungeon_types and self.difficulty)):
                        others.append(r)
                    elif self.filter_dungeon_types and self.difficulty and self._match_room(
                        r, require_difficulty=False
                    ):
                        others.append(r)
                if not others:
                    return None
                target = sorted(others, key=lambda r: -int(r.get("currentPlayers") or 0))[0]
                rid = str(target.get("id"))
            tried.add(rid)
            self._log(
                f"尝试加入房间 {rid} "
                f"players={target.get('currentPlayers')}/{target.get('maxPlayers')} "
                f"type={target.get('gameType')}"
                + (f" diff={target.get('difficulty')}" if target.get("difficulty") else "")
            )
            try:
                return self.join_room(rid)
            except Exception as e:
                self._log(f"加入失败: {e}")
                self._clear_error()
                self.refresh_rooms()

    def wait_and_join(
        self,
        stop_event: Optional[threading.Event] = None,
        heartbeat_sec: float = 30,
    ) -> dict:
        """常驻监听大厅，有符合房间立刻加入（不自建）。"""
        tried: set[str] = set()
        last_log = 0.0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError(f"{self.log_tag} 监听已停止")
            if not self.sio.connected:
                raise RuntimeError("WebSocket 已断开")

            room = self.try_join_any(tried, refresh=False)
            if room:
                return room
            self._match_event.clear()
            if self.pick_join_target():
                continue
            now = time.time()
            if now - last_log >= heartbeat_sec:
                self._log(f"监听中… {self._lobby_snapshot()}")
                last_log = now
                try:
                    self.refresh_rooms()
                except Exception as e:
                    self._log(f"刷新房间失败: {e}")
            self._match_event.wait(min(3.0, heartbeat_sec))

    def ensure_in_room(self, wait_sec: Optional[float] = None) -> dict:
        """加入合适公开房；wait_sec<=0 表示一直等到有房。"""
        if wait_sec is None:
            wait_sec = self.room_wait_sec
        if wait_sec is None or float(wait_sec) <= 0:
            return self.wait_and_join()

        deadline = time.time() + max(1.0, float(wait_sec))
        tried: set[str] = set()
        create_attempted = False
        last_create_err = None

        while time.time() < deadline:
            room = self.try_join_any(tried)
            if room:
                return room

            if self.create_if_empty and not create_attempted:
                create_attempted = True
                self._log("暂无合适房间，尝试创建公开房")
                try:
                    return self.create_room()
                except Exception as e:
                    last_create_err = str(e)
                    self._clear_error()
                    self._log(f"创建不可用: {e}")

            left = deadline - time.time()
            if left <= 0:
                break
            if int(left) % 15 < 3:
                self._log(f"等待公开房中… {self._lobby_snapshot()} 剩余{left:.0f}s")
            self._match_event.wait(min(3.0, left))

        if self.create_if_empty and last_create_err:
            raise TimeoutError(
                f"未找到可加入的无密码[{self.game_type}]房间；自建失败: {last_create_err}"
            )
        raise TimeoutError(
            f"未找到可加入的无密码[{self.game_type}]房间（{self._lobby_snapshot()}）"
        )

    def _session_timeout_result(
        self,
        *,
        room_id,
        host: bool,
        message: str,
        stopped: bool = False,
    ) -> dict:
        if room_id and "超时" in (message or ""):
            record_room_timeout_today(str(room_id), log_tag=self.log_tag)
        self.leave_room()
        result = {
            "ok": False,
            "roomId": room_id,
            "isHost": host,
            "message": message,
            "timedOut": True,
        }
        if stopped:
            result["stopped"] = True
        return result

    def run_session(
        self,
        *,
        wait_forever: bool = False,
        stop_event: Optional[threading.Event] = None,
    ) -> dict:
        """进房 → 准备/开局 → 等完成 → 退房。wait_forever 时一直监听直到有房。"""
        t0 = time.time()
        self.reset_run_state()

        def _stopped() -> bool:
            if stop_event is not None and stop_event.is_set():
                self._stop_flag.set()
                return True
            return self._stop_flag.is_set()

        try:
            if wait_forever:
                room = self.wait_and_join(stop_event=stop_event)
            else:
                room = self.ensure_in_room(self.room_wait_sec)
        except InterruptedError as e:
            return {"ok": False, "stopped": True, "message": str(e)}
        except Exception as e:
            return {"ok": False, "message": str(e)}

        room_id = room.get("id")
        host = self.is_host(room)
        self._log(f"当前房间 {room_id} host={host} players={self._player_count(room)}")

        if not host:
            self.set_ready(True)

        deadline = time.time() + self.room_wait_sec
        while time.time() < deadline:
            if _stopped():
                self.leave_room()
                return {"ok": False, "stopped": True, "roomId": room_id, "message": "已停止"}
            if self._error:
                self.leave_room()
                return {"ok": False, "roomId": room_id, "isHost": host, "message": self._error}
            if self._started.is_set() or self._complete.is_set():
                break
            room = self.current_room()
            if host and self._can_start(room):
                self.start_game()
                if self._started.wait(5) or self._complete.wait(0.1):
                    break
                time.sleep(1)
                if self._settle_done or self._complete.is_set():
                    break
            time.sleep(0.5)
        else:
            return self._session_timeout_result(
                room_id=room_id,
                host=host,
                message=f"等待开局超时 ({self.room_wait_sec}s)",
            )

        while True:
            if _stopped():
                self.leave_room()
                return {"ok": False, "stopped": True, "roomId": room_id, "message": "已停止"}
            left = self.run_timeout_sec - (time.time() - t0)
            if left <= 0:
                msg = self._settle_error or self._error or "副本超时"
                return self._session_timeout_result(room_id=room_id, host=host, message=msg)
            if self._complete.wait(min(1.0, left)):
                break

        if self._error and not self._complete_payload:
            self.leave_room()
            return {"ok": False, "roomId": room_id, "isHost": host, "message": self._error}

        complete = self._complete_payload or {}
        self.leave_room()
        return {
            "ok": True,
            "roomId": room_id,
            "isHost": host,
            "complete": complete,
            "message": "ok",
            "elapsedSec": round(time.time() - t0, 1),
        }
