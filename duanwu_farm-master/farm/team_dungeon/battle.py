# farm/team_dungeon/battle.py — 用 Node 跑前端同款 runTeamDungeon
import json
import os
import subprocess
import urllib.request
from pathlib import Path

ENGINE_URL = "https://www.duanwuqiufenmao.top/_nuxt/BxRSV5WK.js"
CACHE_DIR = Path(__file__).resolve().parent / "_cache"
ENGINE_PATH = CACHE_DIR / "battle_engine.mjs"
RUNNER_PATH = CACHE_DIR / "run_team_dungeon.mjs"


def _ensure_engine(force: bool = False) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if force or not ENGINE_PATH.exists() or ENGINE_PATH.stat().st_size < 1000:
        print(f"[team_dungeon] 下载战斗引擎 {ENGINE_URL}")
        with urllib.request.urlopen(ENGINE_URL, timeout=60) as resp:
            data = resp.read()
        ENGINE_PATH.write_bytes(data)
    if not RUNNER_PATH.exists():
        RUNNER_PATH.write_text(
            """\
import { readFileSync } from 'fs';
import { pathToFileURL } from 'url';
import { createRequire } from 'module';

const input = JSON.parse(readFileSync(0, 'utf8'));
const engineUrl = pathToFileURL(process.argv[2]).href;
const mod = await import(engineUrl);
const api = typeof mod.u === 'function' ? mod.u() : (mod.default?.() || mod);
if (!api?.runTeamDungeon) {
  console.error(JSON.stringify({ ok: false, message: 'runTeamDungeon missing' }));
  process.exit(2);
}
const battleInput = input.clientBattle && input.clientBattle.seed != null
  ? input.clientBattle
  : (input.seed != null ? input : (input.clientBattle || input));
const result = api.runTeamDungeon(battleInput);
process.stdout.write(JSON.stringify({ ok: true, result }));
""",
            encoding="utf-8",
        )
    return ENGINE_PATH


def _nn(v) -> int:
    try:
        n = int(float(v or 0))
    except Exception:
        return 0
    if n <= 0:
        return 0
    return min(n, 999_999_999)


def floor_summaries(floor_results: list) -> list:
    out = []
    for fr in floor_results or []:
        out.append({
            "floor": fr.get("floor"),
            "isBoss": bool(fr.get("isBoss")),
            "won": bool(fr.get("won")),
            "monsterName": fr.get("monsterName"),
            "rounds": max(0, int(float(fr.get("rounds") or 0))),
            "expGained": max(0, int(float(fr.get("expGained") or 0))),
            "healed": bool(fr.get("healed")),
        })
    return out


def battle_stats(floor_results: list) -> list:
    agg = {}
    for fr in floor_results or []:
        for u in fr.get("teamResults") or []:
            uid = str(u.get("userId"))
            cur = agg.get(uid) or {
                "userId": u.get("userId"),
                "username": u.get("name") or f"玩家{u.get('userId')}",
                "damageDealt": 0,
                "damageTaken": 0,
                "healingDone": 0,
            }
            cur["damageDealt"] += _nn(u.get("damageDealt"))
            cur["damageTaken"] += _nn(u.get("damageTaken"))
            cur["healingDone"] += _nn(u.get("healingDone"))
            agg[uid] = cur
    return list(agg.values())


def run_team_dungeon(prepare_event: dict, node_bin: str = "node") -> dict:
    """
    输入 team_dungeon_prepare 事件，返回前端同结构 result:
      {floorResults, totalExp, floorsCleared, died}
    """
    engine = _ensure_engine()
    payload = json.dumps(prepare_event, ensure_ascii=False).encode("utf-8")
    try:
        proc = subprocess.run(
            [node_bin, str(RUNNER_PATH), str(engine)],
            input=payload,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError("未找到 node，无法运行组队秘境战斗引擎") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"战斗引擎失败 code={proc.returncode}: {err[:500]}")
    out = json.loads(proc.stdout.decode("utf-8"))
    if not out.get("ok"):
        raise RuntimeError(out.get("message") or "战斗引擎返回失败")
    return out["result"]


def build_settle_summary(result: dict) -> dict:
    floors = result.get("floorResults") or []
    return {
        "floorsCleared": int(result.get("floorsCleared") or 0),
        "died": bool(result.get("died")),
        "floorSummaries": floor_summaries(floors),
        "battleStats": battle_stats(floors),
    }
