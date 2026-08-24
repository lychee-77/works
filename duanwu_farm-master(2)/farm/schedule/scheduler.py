# farm/schedule/scheduler.py — 分通道独立调度 + 夜间作息 + 秘境监听线程
import random
import threading
import time
from datetime import date, datetime, timedelta
from config import (
    JITTER_SECONDS_MIN,
    JITTER_SECONDS_MAX,
    SCHEDULE_FALLBACK_MINUTES,
    POST_ACTION_DEFAULT_MINUTES,
    NIGHT_MODE_ENABLED,
    NIGHT_PLANT_HOUR,
    NIGHT_HARVEST_HOUR,
    NIGHT_CROP_ID,
    AUTO_TEAM_DUNGEON,
    AUTO_EXPEDITION,
)
from farm.schedule.round import (
    run_night_round,
    run_harvest_round,
    run_steal_round,
    run_explore_round,
    run_care_round,
)

# 收菜等待过久时，中途探测成熟情况的上限（秒）——仅 ETA 缺失时启用
HARVEST_PEEK_MAX_SLEEP_SEC = 3 * 60

# 收菜通道是否处于「ETA 缺失、短轮询复核」状态
_harvest_watch = {
    "eta_missing": False,
}



CHANNEL_LABEL = {
    "harvest": "收菜",
    "care": "浇水",
    "steal": "偷菜",
    "explore": "翻地",
}

REASON_LABEL = {
    "steal": "好友可偷",
    "harvest": "自己可收",
    "explore": "可翻地",
    "care": "可浇水",
    "post_action": "动作后默认",
    "none": "无目标/异常兜底",
    "daily_limit": "今日已达上限",
    "no_steal_target": "暂无可偷目标",
    "night_sleep": "夜间作息",
    "night_wait_harvest": "夜间等成熟收割",
}

# 农场通道（不含秘境）
CHANNEL_PRIORITY = ("harvest", "care", "explore", "steal")

# 秘境监听状态（按自然日）
_dungeon_state = {
    "day": None,
    "done": False,
    "thread": None,
    "stop": None,
}

# 远征监听状态（按自然日/周次；跨日重置后重新拉监听）
_expedition_state = {
    "day": None,
    "done": False,
    "thread": None,
    "stop": None,
}


def is_night_hours(now: datetime = None) -> bool:
    """是否处于夜间窗口 [NIGHT_PLANT_HOUR, NIGHT_HARVEST_HOUR)。"""
    now = now or datetime.now()
    start = int(NIGHT_PLANT_HOUR) % 24
    end = int(NIGHT_HARVEST_HOUR) % 24
    hour = now.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def seconds_until_hour(hour: int, minute: int = 0) -> float:
    """距离下一个 hour:minute 的秒数（若恰好等于该时刻则推到次日）。"""
    now = datetime.now()
    target = now.replace(hour=int(hour) % 24, minute=int(minute), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


def channel_sleep_seconds(schedule_info, channel: str) -> float:
    """单通道休眠秒数 = 基准分钟*60 + 独立抖动。达今日上限则睡到次日 0 点。"""
    ch_label = CHANNEL_LABEL.get(channel, channel)

    if isinstance(schedule_info, dict) and schedule_info.get("daily_done"):
        sleep_sec = seconds_until_hour(0)
        jitter_sec = random.uniform(JITTER_SECONDS_MIN, JITTER_SECONDS_MAX)
        total_sec = sleep_sec + jitter_sec
        next_at = datetime.fromtimestamp(time.time() + total_sec).strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[{ch_label}调度] 由【{REASON_LABEL['daily_limit']}】决定 "
            f"今日不再触发，睡到次日 0:00 "
            f"(约 {sleep_sec / 60:.1f}m + 抖动 {jitter_sec:.1f}s) => 下次约 {next_at}"
        )
        return total_sec

    if isinstance(schedule_info, dict):
        nearest = schedule_info.get("minutes")
        reason = schedule_info.get("reason") or "none"
        eta = schedule_info.get("eta")
        eta_schedule = schedule_info.get("eta_schedule")
        lead_used = schedule_info.get("lead_used")
        candidates = schedule_info.get("candidates")
    else:
        nearest = schedule_info
        reason = channel if nearest is not None else "none"
        eta = nearest
        eta_schedule = None
        lead_used = None
        candidates = None

    if nearest is None:
        base_min = float(SCHEDULE_FALLBACK_MINUTES)
        reason = "none"
    else:
        base_min = max(0.0, float(nearest))

    jitter_sec = random.uniform(JITTER_SECONDS_MIN, JITTER_SECONDS_MAX)
    label = REASON_LABEL.get(reason, reason)
    cand_txt = ""
    if candidates:
        cand_txt = " 候选=" + ",".join(f"{REASON_LABEL.get(r, r)}:{m:g}m" for r, m in candidates)

    total_sec = base_min * 60.0 + jitter_sec
    print(
        f"[{ch_label}调度] 由【{label}】决定 {base_min:g}m "
        f"(ETA={eta}m→{eta_schedule}m/提前{lead_used}s, "
        f"动作默认={POST_ACTION_DEFAULT_MINUTES}m)"
        f"{cand_txt} + 抖动 {jitter_sec:.1f}s => 休眠 {total_sec / 60:.2f} 分钟"
    )
    return total_sec


def _sleep_until_morning():
    """夜间种植成功后，一觉睡到早晨收获时刻（加少量抖动）。"""
    sleep_sec = seconds_until_hour(NIGHT_HARVEST_HOUR)
    jitter_sec = random.uniform(JITTER_SECONDS_MIN, JITTER_SECONDS_MAX)
    total = sleep_sec + jitter_sec
    next_at = datetime.fromtimestamp(time.time() + total).strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"调度: 由【{REASON_LABEL['night_sleep']}】决定 "
        f"睡到 {NIGHT_HARVEST_HOUR}:00 收 {NIGHT_CROP_ID} "
        f"(约 {sleep_sec / 60:.1f}m + 抖动 {jitter_sec:.1f}s) => 下次约 {next_at}"
    )
    time.sleep(total)


def _cap_sleep_before_night(sleep_sec: float) -> float:
    """白天休眠若会越过夜间种植点，则截断到午夜。"""
    if not NIGHT_MODE_ENABLED:
        return sleep_sec
    until_night = seconds_until_hour(NIGHT_PLANT_HOUR)
    if is_night_hours():
        return sleep_sec
    if until_night < sleep_sec:
        print(
            f"调度截断: 原计划休眠 {sleep_sec / 60:.2f}m，"
            f"提前到夜间种植点 {NIGHT_PLANT_HOUR}:00 "
            f"(约 {until_night / 60:.2f}m) 以便种植 {NIGHT_CROP_ID}"
        )
        return until_night
    return sleep_sec


def _fmt_due(due_map: dict) -> str:
    parts = []
    now = time.time()
    for ch in CHANNEL_PRIORITY:
        ts = due_map.get(ch, now)
        at = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        left = max(0.0, ts - now)
        parts.append(f"{CHANNEL_LABEL[ch]}@{at}({left/60:.1f}m)")
    return " | ".join(parts)


def _peek_harvest_eta_minutes():
    """轻量探测自己农场最近可收分钟数；失败返回 None。"""
    try:
        from farm.auth import SignClient
        from farm.own.farm import (
            get_my_farm,
            active_slots,
            minutes_until_harvestable,
        )

        client = SignClient(quiet=True)
        client.register_signing_key(force=False)
        data = (get_my_farm(client).get("data") or {})
        nearest = None
        for s in active_slots(data):
            nearest = _min_eta_local(nearest, minutes_until_harvestable(s))
        return nearest
    except Exception as e:
        print(f"[收菜探测] 失败: {e}")
        return None


def _min_eta_local(current, candidate):
    if candidate is None:
        return current
    if current is None or candidate < current:
        return candidate
    return current


def _refresh_harvest_due(due_map: dict) -> bool:
    """
    仅在收菜 ETA 缺失的短轮询模式下调用。
    探测到可收/有效 ETA 后更新到期时间，并清除缺失标记。
    """
    if not _harvest_watch.get("eta_missing"):
        return False

    now = time.time()
    eta = _peek_harvest_eta_minutes()
    if eta is None:
        # 仍缺失：维持短轮询，约 3 分钟后再看
        due_map["harvest"] = now + HARVEST_PEEK_MAX_SLEEP_SEC
        print(f"[收菜探测] ETA 仍缺失，{HARVEST_PEEK_MAX_SLEEP_SEC / 60:.0f}m 后再复核")
        return False

    _harvest_watch["eta_missing"] = False
    if float(eta) <= 0.5:
        due_map["harvest"] = now
        print(f"[收菜探测] 已拿到 ETA={eta}m（可收），立即触发收菜")
        return True

    due_map["harvest"] = now + max(0.0, float(eta) * 60.0 - 60.0)
    next_at = datetime.fromtimestamp(due_map["harvest"]).strftime("%H:%M:%S")
    print(f"[收菜探测] 已拿到 ETA={eta}m，按 ETA 预约到 {next_at}（此后不再短轮询）")
    return True


def _note_harvest_schedule(schedule_info) -> None:
    """根据本轮收菜调度结果，标记是否需要短轮询。"""
    if not isinstance(schedule_info, dict):
        _harvest_watch["eta_missing"] = True
        return
    eta = schedule_info.get("eta")
    reason = schedule_info.get("reason")
    # eta 明确有值（含 0=已成熟）→ 信任 ETA，一路睡到点
    if eta is not None:
        _harvest_watch["eta_missing"] = False
        return
    # 无 ETA：none / post_action 短复核
    _harvest_watch["eta_missing"] = reason in ("none", "post_action") or bool(
        schedule_info.get("force_fallback")
    )


def _run_channel(channel: str):
    """执行单通道并返回调度信息。"""
    if channel == "harvest":
        return run_harvest_round()
    if channel == "care":
        return run_care_round()
    if channel == "explore":
        return run_explore_round()
    if channel == "steal":
        return run_steal_round()
    raise ValueError(f"unknown channel: {channel}")


def _stop_dungeon_watcher():
    stop = _dungeon_state.get("stop")
    thread = _dungeon_state.get("thread")
    if stop is not None:
        stop.set()
    if thread is not None and thread.is_alive():
        # 结算/进房可能稍慢，多等一会；仍卡死则放弃引用，daemon 线程随进程回收
        thread.join(timeout=15)
    _dungeon_state["stop"] = None
    _dungeon_state["thread"] = None


def _seconds_until_dungeon_day_roll() -> float | None:
    """
    秘境本日已结束后，距离次日 0 点的秒数。
    未启用/未结束则返回 None（不必为此提前醒）。
    """
    if not AUTO_TEAM_DUNGEON:
        return None
    if not _dungeon_state.get("done"):
        return None
    today = date.today()
    if _dungeon_state.get("day") != today:
        return 0.0
    return seconds_until_hour(0)


def _ensure_dungeon_watcher():
    """
    秘境独立于农场 ETA 调度：后台常驻 WS 监听。
    次数用完标记本日 done；跨日后由主循环在 0 点附近再次调用以重新开监听。
    """
    if not AUTO_TEAM_DUNGEON:
        _stop_dungeon_watcher()
        return

    today = date.today()
    if _dungeon_state.get("day") != today:
        _stop_dungeon_watcher()
        _dungeon_state["day"] = today
        _dungeon_state["done"] = False

    if _dungeon_state.get("done"):
        return

    thread = _dungeon_state.get("thread")
    if thread is not None and thread.is_alive():
        return

    stop = threading.Event()

    def worker():
        try:
            from farm.team_dungeon import run_team_dungeon_watch

            result = run_team_dungeon_watch(stop_event=stop)
            if result.get("daily_done"):
                _dungeon_state["done"] = True
                print(f"[组队秘境] 本日任务结束: {result.get('message')}（本日不再触发）")
            elif result.get("stopped"):
                print("[组队秘境] 监听已停止")
            else:
                print(f"[组队秘境] 监听退出: {result.get('message')}，稍后重连")
        except Exception as e:
            if not stop.is_set():
                print(f"[组队秘境] 监听异常: {e}，稍后重连")
                time.sleep(5)

    t = threading.Thread(target=worker, name="team-dungeon-watch", daemon=True)
    _dungeon_state["stop"] = stop
    _dungeon_state["thread"] = t
    t.start()
    print("[组队秘境] 已启动常驻 WebSocket 监听（有公开房即加入，次数用完本日结束）")


def _stop_expedition_watcher():
    stop = _expedition_state.get("stop")
    thread = _expedition_state.get("thread")
    if stop is not None:
        stop.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=15)
    _expedition_state["stop"] = None
    _expedition_state["thread"] = None


def _seconds_until_expedition_day_roll() -> float | None:
    if not AUTO_EXPEDITION:
        return None
    if not _expedition_state.get("done"):
        return None
    today = date.today()
    if _expedition_state.get("day") != today:
        return 0.0
    return seconds_until_hour(0)


def _ensure_expedition_watcher():
    """远征（raid）独立于农场调度：后台常驻 WS，只进无密码公开房。"""
    if not AUTO_EXPEDITION:
        _stop_expedition_watcher()
        return

    today = date.today()
    if _expedition_state.get("day") != today:
        _stop_expedition_watcher()
        _expedition_state["day"] = today
        _expedition_state["done"] = False

    if _expedition_state.get("done"):
        return

    thread = _expedition_state.get("thread")
    if thread is not None and thread.is_alive():
        return

    stop = threading.Event()

    def worker():
        try:
            from farm.expedition import run_expedition_watch

            result = run_expedition_watch(stop_event=stop)
            if result.get("daily_done"):
                _expedition_state["done"] = True
                print(f"[远征] 本周期任务结束: {result.get('message')}（本周期不再触发）")
            elif result.get("stopped"):
                print("[远征] 监听已停止")
            else:
                print(f"[远征] 监听退出: {result.get('message')}，稍后重连")
        except Exception as e:
            if not stop.is_set():
                print(f"[远征] 监听异常: {e}，稍后重连")
                time.sleep(5)

    t = threading.Thread(target=worker, name="expedition-watch", daemon=True)
    _expedition_state["stop"] = stop
    _expedition_state["thread"] = t
    t.start()
    print("[远征] 已启动常驻 WebSocket 监听（raid 公开房即加入，次数用完本周期结束）")


def _stop_game_watchers():
    _stop_dungeon_watcher()
    _stop_expedition_watcher()


def run_forever():
    night_tip = ""
    if NIGHT_MODE_ENABLED:
        night_tip = (
            f"；夜间 {NIGHT_PLANT_HOUR}:00 起：地里有未成熟作物则等收完再种 {NIGHT_CROP_ID}，"
            f"{NIGHT_HARVEST_HOUR}:00 起床（期间暂停翻地/偷菜/收菜/浇水调度）"
        )
    dungeon_tip = (
        "；组队秘境=常驻WS监听/有房即进/次数用完本日结束"
        if AUTO_TEAM_DUNGEON
        else ""
    )
    expedition_tip = (
        "；远征=常驻WS监听/raid无密码房/次数用完本周期结束"
        if AUTO_EXPEDITION
        else ""
    )
    print(
        f"定时任务启动：翻地/偷菜/收菜/浇水分通道独立调度 "
        f"(各通道 = 自身ETA提前 或 动作后{POST_ACTION_DEFAULT_MINUTES}m 或 兜底{SCHEDULE_FALLBACK_MINUTES}m "
        f"+ 抖动[{JITTER_SECONDS_MIN},{JITTER_SECONDS_MAX}]s；"
        f"翻地/偷菜/浇水达今日上限则次日再触发)"
        f"{night_tip}{dungeon_tip}{expedition_tip}；Ctrl+C 退出"
    )

    _ensure_dungeon_watcher()
    _ensure_expedition_watcher()

    now = time.time()
    due_map = {ch: now for ch in CHANNEL_PRIORITY}
    round_no = 0

    while True:
        _ensure_dungeon_watcher()
        _ensure_expedition_watcher()

        # ----- 夜间作息 -----
        if NIGHT_MODE_ENABLED and is_night_hours():
            round_no += 1
            start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n######## 夜间轮 #{round_no} @ {start} ########")
            schedule_info = None
            try:
                schedule_info = run_night_round()
            except KeyboardInterrupt:
                _stop_game_watchers()
                raise
            except Exception as e:
                print(f"夜间轮执行异常: {e}")
                schedule_info = None

            night_ok = isinstance(schedule_info, dict) and schedule_info.get("night_plant_ok")
            if night_ok:
                _sleep_until_morning()
                wake = time.time()
                for ch in CHANNEL_PRIORITY:
                    due_map[ch] = wake
                continue

            wait_harvest = isinstance(schedule_info, dict) and schedule_info.get("night_wait_harvest")
            sleep_sec = channel_sleep_seconds(schedule_info, "harvest")
            next_at = datetime.fromtimestamp(time.time() + sleep_sec).strftime("%Y-%m-%d %H:%M:%S")
            if wait_harvest:
                print(
                    f"夜间地里作物未成熟，按收割 ETA 等待后再收再种 {NIGHT_CROP_ID}；"
                    f"下次触发约 {next_at}"
                )
            else:
                print(f"夜间种植未就绪，{SCHEDULE_FALLBACK_MINUTES}m 后再试；下次触发约 {next_at}")
            time.sleep(sleep_sec)
            continue

        now = time.time()
        due_channels = [ch for ch in CHANNEL_PRIORITY if due_map.get(ch, now) <= now + 0.05]

        if not due_channels:
            next_ts = min(due_map[ch] for ch in CHANNEL_PRIORITY)
            sleep_sec = max(0.0, next_ts - time.time())
            sleep_sec = _cap_sleep_before_night(sleep_sec)
            # 秘境本日已结束后，确保刚过 0 点主循环会醒一次并重新拉起 WS 监听
            dungeon_roll = _seconds_until_dungeon_day_roll()
            if dungeon_roll is not None:
                sleep_sec = min(sleep_sec, max(0.0, dungeon_roll))
            expedition_roll = _seconds_until_expedition_day_roll()
            if expedition_roll is not None:
                sleep_sec = min(sleep_sec, max(0.0, expedition_roll))

            # 仅 ETA 缺失时短睡复核；ETA 明确则直接睡到点
            if _harvest_watch.get("eta_missing"):
                harvest_left = max(0.0, due_map.get("harvest", now) - time.time())
                if harvest_left > HARVEST_PEEK_MAX_SLEEP_SEC:
                    sleep_sec = min(sleep_sec, float(HARVEST_PEEK_MAX_SLEEP_SEC))

            print(f"\n分通道等待: {_fmt_due(due_map)}")
            if _harvest_watch.get("eta_missing"):
                print("  （收菜 ETA 缺失，短间隔复核中）")
            next_at = datetime.fromtimestamp(time.time() + sleep_sec).strftime("%Y-%m-%d %H:%M:%S")
            print(f"下次唤醒约 {next_at}")
            if sleep_sec > 0:
                time.sleep(sleep_sec)

            if _harvest_watch.get("eta_missing"):
                _refresh_harvest_due(due_map)
            continue

        for channel in due_channels:
            if NIGHT_MODE_ENABLED and is_night_hours():
                break

            round_no += 1
            start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"\n######## 第 {round_no} 次触发 @ {start} "
                f"通道={CHANNEL_LABEL.get(channel, channel)} ########"
            )
            schedule_info = None
            try:
                schedule_info = _run_channel(channel)
            except KeyboardInterrupt:
                _stop_game_watchers()
                raise
            except Exception as e:
                print(f"通道【{CHANNEL_LABEL.get(channel, channel)}】异常: {e}")
                schedule_info = None

            sleep_sec = channel_sleep_seconds(schedule_info, channel)
            due_map[channel] = time.time() + sleep_sec
            if channel == "harvest":
                _note_harvest_schedule(schedule_info)
            next_at = datetime.fromtimestamp(due_map[channel]).strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{CHANNEL_LABEL.get(channel, channel)}] 下次约 {next_at}")

        print(f"当前各通道: {_fmt_due(due_map)}")
