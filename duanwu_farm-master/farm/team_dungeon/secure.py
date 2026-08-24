# farm/team_dungeon/secure.py — 对齐前端 team_dungeon_settle 签名
import base64
import hashlib
import json
import time


def _canonical_json(value) -> str:
    if value is None or isinstance(value, (str, int, float, bool)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(x) for x in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value.keys())
        parts = [f"{json.dumps(k, ensure_ascii=False)}:{_canonical_json(value[k])}" for k in keys]
        return "{" + ",".join(parts) + "}"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _b64url_utf8(text: str) -> str:
    raw = text.encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_secure_payload(client_battle: dict, battle_token: str, result_summary: dict) -> dict:
    """
    对齐 CjvPUARG.js:
      p = b64url(canonicalJson(summary))
      s = sha256hex(`${battleId}.${p}.${t}.${nonce}.${battleToken}`)
      return {bid, p, t, s}
    """
    battle_id = (client_battle or {}).get("battleId")
    nonce = (client_battle or {}).get("nonce")
    if not battle_id or not nonce or not battle_token:
        raise ValueError("missing battle session fields")
    payload = _b64url_utf8(_canonical_json(result_summary))
    ts = int(time.time() * 1000)
    digest = hashlib.sha256(f"{battle_id}.{payload}.{ts}.{nonce}.{battle_token}".encode("utf-8")).hexdigest()
    return {"bid": battle_id, "p": payload, "t": ts, "s": digest}


def settle_delay_seconds(client_battle: dict) -> float:
    if not isinstance(client_battle, dict):
        return 0.0
    min_delay = client_battle.get("minSettleDelayMs")
    if min_delay is not None:
        try:
            received = float(client_battle.get("_clientReceivedAt") or time.time() * 1000)
            elapsed = time.time() * 1000 - received
            return max(0.0, (float(min_delay) - elapsed) / 1000.0)
        except Exception:
            return 0.0
    min_at = client_battle.get("minSettleAt")
    if min_at is not None:
        try:
            return max(0.0, (float(min_at) - time.time() * 1000) / 1000.0)
        except Exception:
            return 0.0
    return 0.0


def stamp_received(client_battle: dict) -> dict:
    if isinstance(client_battle, dict) and client_battle.get("_clientReceivedAt") is None:
        client_battle["_clientReceivedAt"] = int(time.time() * 1000)
    return client_battle
