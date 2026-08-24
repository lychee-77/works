# config.py — 从 .env 加载运行配置
import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _env_int(key: str, default: int = 0) -> int:
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


def _env_float(key: str, default: float = 0.0) -> float:
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


def _env_list(key: str, default: list | None = None) -> list:
    raw = os.getenv(key)
    if raw is None:
        return list(default or [])
    text = raw.strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [x.strip().strip("'\"") for x in text.split(",") if x.strip().strip("'\"")]


ACCOUNT_USERNAME = _env("ACCOUNT_USERNAME")
ACCOUNT_PASSWORD = _env("ACCOUNT_PASSWORD")
API_HOST = _env("API_HOST", "https://api.duanwuqiufenmao.top").rstrip("/")
BASE_API = API_HOST + "/api"
FRIEND_ID = _env("FRIEND_ID", "53120")
USE_FRIENDS_API = _env_bool("USE_FRIENDS_API", True)
FRIEND_IDS = _env_list("FRIEND_IDS", ["53120", "52759", "52577", "41734"])

AUTO_STEAL = _env_bool("AUTO_STEAL", True)
AUTO_EXPLORE = _env_bool("AUTO_EXPLORE", True)
AUTO_HARVEST = _env_bool("AUTO_HARVEST", True)
AUTO_PLANT = _env_bool("AUTO_PLANT", True)
AUTO_CARE = _env_bool("AUTO_CARE", True)
AUTO_FRIEND_HELP = _env_bool("AUTO_FRIEND_HELP", True)
STEAL_MIN_EXP_REWARD = _env_int("STEAL_MIN_EXP_REWARD", 20)

# 白天作物：VIP 种藜麦，非 VIP 种胡萝卜（可在 .env 覆盖）
DAY_CROP_VIP = _env("DAY_CROP_VIP", "quinoa")
DAY_CROP_NORMAL = _env("DAY_CROP_NORMAL", "carrot")


def resolve_day_crop_id(is_premium: bool | None) -> str:
    """按账号 VIP（isPremium）决定白天种植作物。"""
    return DAY_CROP_VIP if is_premium else DAY_CROP_NORMAL


# 兼容旧引用：未带 VIP 信息时默认非 VIP 作物
PLANT_CROP_ID = DAY_CROP_NORMAL

# ---------- 组队秘境 ----------
# 秘境类型按账号已解锁动态判断，不再写死 TEAM_DUNGEON_TYPE
AUTO_TEAM_DUNGEON = _env_bool("AUTO_TEAM_DUNGEON", True)
TEAM_DUNGEON_DIFFICULTY = _env("TEAM_DUNGEON_DIFFICULTY", "")
TEAM_DUNGEON_NO_PASSWORD_ONLY = _env_bool("TEAM_DUNGEON_NO_PASSWORD_ONLY", True)
TEAM_DUNGEON_CREATE_IF_EMPTY = _env_bool("TEAM_DUNGEON_CREATE_IF_EMPTY", False)
TEAM_DUNGEON_ROOM_WAIT_SEC = _env_float("TEAM_DUNGEON_ROOM_WAIT_SEC", 50)
TEAM_DUNGEON_RUN_TIMEOUT_SEC = _env_float("TEAM_DUNGEON_RUN_TIMEOUT_SEC", 600)
TEAM_DUNGEON_ROOM_TIMEOUT_BLOCK = _env_int("TEAM_DUNGEON_ROOM_TIMEOUT_BLOCK", 3)
TEAM_DUNGEON_STAMINA_POTION = _env("TEAM_DUNGEON_STAMINA_POTION", "stamina_large")
TEAM_DUNGEON_BUY_POTION = _env_bool("TEAM_DUNGEON_BUY_POTION", True)
TEAM_DUNGEON_EXP_POTION = _env("TEAM_DUNGEON_EXP_POTION", "exp_medium")
TEAM_DUNGEON_BUY_EXP_POTION = _env_bool("TEAM_DUNGEON_BUY_EXP_POTION", True)
TEAM_DUNGEON_USE_EXP_POTION = _env_bool("TEAM_DUNGEON_USE_EXP_POTION", True)

# ---------- 远征团本（gameType=raid，同游戏大厅 WebSocket） ----------
AUTO_EXPEDITION = _env_bool("AUTO_EXPEDITION", False)
EXPEDITION_NO_PASSWORD_ONLY = _env_bool("EXPEDITION_NO_PASSWORD_ONLY", True)
EXPEDITION_CREATE_IF_EMPTY = _env_bool("EXPEDITION_CREATE_IF_EMPTY", False)
EXPEDITION_ROOM_WAIT_SEC = _env_float("EXPEDITION_ROOM_WAIT_SEC", 50)
EXPEDITION_RUN_TIMEOUT_SEC = _env_float("EXPEDITION_RUN_TIMEOUT_SEC", 3600)
EXPEDITION_ROOM_TIMEOUT_BLOCK = _env_int("EXPEDITION_ROOM_TIMEOUT_BLOCK", 3)
EXPEDITION_MIN_LEVEL = _env_int("EXPEDITION_MIN_LEVEL", 60)

# ---------- 夜间作息 ----------
NIGHT_MODE_ENABLED = _env_bool("NIGHT_MODE_ENABLED", True)
NIGHT_PLANT_HOUR = _env_int("NIGHT_PLANT_HOUR", 0)
NIGHT_HARVEST_HOUR = _env_int("NIGHT_HARVEST_HOUR", 7)
NIGHT_CROP_ID = _env("NIGHT_CROP_ID", "pineapple")

# ---------- 行为节奏 ----------
HARVEST_PARALLEL = _env_bool("HARVEST_PARALLEL", False)
STEAL_PARALLEL = _env_bool("STEAL_PARALLEL", False)
ACTION_GAP_SEC_MIN = _env_float("ACTION_GAP_SEC_MIN", 0.45)
ACTION_GAP_SEC_MAX = _env_float("ACTION_GAP_SEC_MAX", 1.8)
PLANT_GAP_SEC_MIN = _env_float("PLANT_GAP_SEC_MIN", 0.6)
PLANT_GAP_SEC_MAX = _env_float("PLANT_GAP_SEC_MAX", 2.2)
STEAL_FRIEND_GAP_SEC_MIN = _env_float("STEAL_FRIEND_GAP_SEC_MIN", 0.9)
STEAL_FRIEND_GAP_SEC_MAX = _env_float("STEAL_FRIEND_GAP_SEC_MAX", 2.8)
HARVEST_GAP_SEC = _env_float("HARVEST_GAP_SEC", 0.8)
STEAL_GAP_SEC = _env_float("STEAL_GAP_SEC", 0.8)
PLANT_GAP_SEC = _env_float("PLANT_GAP_SEC", 1.0)
STEAL_FRIEND_GAP_SEC = _env_float("STEAL_FRIEND_GAP_SEC", 1.5)

HARVEST_LEAD_SECONDS_MIN = _env_int("HARVEST_LEAD_SECONDS_MIN", 40)
HARVEST_LEAD_SECONDS_MAX = _env_int("HARVEST_LEAD_SECONDS_MAX", 140)
STEAL_LEAD_SECONDS_MIN = _env_int("STEAL_LEAD_SECONDS_MIN", 40)
STEAL_LEAD_SECONDS_MAX = _env_int("STEAL_LEAD_SECONDS_MAX", 140)
HARVEST_LEAD_SECONDS = _env_int("HARVEST_LEAD_SECONDS", 90)
STEAL_LEAD_SECONDS = _env_int("STEAL_LEAD_SECONDS", 90)

JITTER_SECONDS_MIN = _env_int("JITTER_SECONDS_MIN", 1)
JITTER_SECONDS_MAX = _env_int("JITTER_SECONDS_MAX", 20)
SCHEDULE_FALLBACK_MINUTES = _env_int("SCHEDULE_FALLBACK_MINUTES", 25)
POST_ACTION_DEFAULT_MINUTES = _env_int("POST_ACTION_DEFAULT_MINUTES", 25)

SIGN_DIR = _env("SIGN_DIR", "sign")
KEY_FILE = os.path.join(SIGN_DIR, "sign_key.json")
REGISTER_MARK_FILE = os.path.join(SIGN_DIR, ".reg_mark")

HEADERS_BASE = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
    ),
    "Origin": "https://www.duanwuqiufenmao.top",
    "Referer": "https://www.duanwuqiufenmao.top/",
}
