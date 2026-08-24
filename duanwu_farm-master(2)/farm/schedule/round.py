# farm/schedule/round.py — 各通道一轮：自己浇水/翻地/收菜/种植 + 好友浇水/翻地/偷菜
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import (
    FRIEND_IDS,
    AUTO_STEAL,
    AUTO_EXPLORE,
    AUTO_HARVEST,
    AUTO_PLANT,
    AUTO_CARE,
    AUTO_FRIEND_HELP,
    USE_FRIENDS_API,
    NIGHT_CROP_ID,
    POST_ACTION_DEFAULT_MINUTES,
    SCHEDULE_FALLBACK_MINUTES,
    HARVEST_PARALLEL,
    STEAL_PARALLEL,
    ACTION_GAP_SEC_MIN,
    ACTION_GAP_SEC_MAX,
    PLANT_GAP_SEC_MIN,
    PLANT_GAP_SEC_MAX,
    STEAL_FRIEND_GAP_SEC_MIN,
    STEAL_FRIEND_GAP_SEC_MAX,
    HARVEST_LEAD_SECONDS_MIN,
    HARVEST_LEAD_SECONDS_MAX,
    STEAL_LEAD_SECONDS_MIN,
    STEAL_LEAD_SECONDS_MAX,
    resolve_day_crop_id,
)
from farm.auth import SignClient
from farm.friend import (
    get_friend_farm,
    is_stealable,
    is_explorable,
    is_helpable,
    minutes_until_stealable,
    minutes_until_explorable,
    minutes_until_helpable,
    steal_slot,
    explore_slot,
    help_slot,
    get_friends,
    print_friends,
    summarize_slot_filters,
    land_blocks_steal,
)
from farm.own import (
    get_my_farm,
    active_slots,
    is_harvestable,
    is_plantable,
    is_removable,
    is_careable,
    minutes_until_harvestable,
    minutes_until_careable,
    harvest_slot,
    plant_slot,
    care_slot,
    remove_slot,
    explore_own_slot,
    response_indicates_care_limit,
)


def _min_eta(current, candidate):
    if candidate is None:
        return current
    if current is None or candidate < current:
        return candidate
    return current


def _sleep_rand(lo: float, hi: float):
    """随机停顿，避免固定节奏"""
    if hi <= 0:
        return
    lo = max(0.0, float(lo))
    hi = max(lo, float(hi))
    time.sleep(random.uniform(lo, hi))


def resolve_friends(client: SignClient):
    """返回 [(friend_id, nick_name), ...]，顺序随机打乱。

    优先用好友接口的完整列表，再并上 FRIEND_IDS，避免漏人。
    """
    seen = set()
    items = []
    if USE_FRIENDS_API:
        try:
            friends = get_friends(client)
            print_friends(friends)
            for f in friends:
                fid = f.get("id")
                if fid is None:
                    continue
                sid = str(fid)
                if sid in seen:
                    continue
                seen.add(sid)
                items.append((sid, f.get("nick_name") or "-"))
        except Exception as e:
            print(f"拉取好友列表失败，回退 FRIEND_IDS: {e}")
    for fid in FRIEND_IDS:
        sid = str(fid)
        if sid in seen:
            continue
        seen.add(sid)
        items.append((sid, "-"))
    random.shuffle(items)
    return items


def _harvest_one(client, slot, action_token):
    """单块收菜。返回 (ok, info_dict|None)"""
    crop_name = slot.get("cropName") or slot.get("cropId") or "-"
    crop_id = slot.get("cropId")
    slot_index = slot.get("slotIndex")
    resp = harvest_slot(client, slot_index, action_token)
    ok = resp.get("status") == 200 and (resp.get("data") or {}).get("success") is True
    if not ok:
        return False, None
    return True, {
        "slot": slot_index,
        "cropName": crop_name,
        "cropId": crop_id,
    }


def harvest_targets_fast(client, harvest_targets, action_token):
    """
    收完可收地块：默认串行 + 随机间隔；可选并行（不推荐）。
    地块顺序随机，降低固定模式。
    """
    harvested = []
    if not harvest_targets:
        return harvested

    targets = list(harvest_targets)
    random.shuffle(targets)

    t0 = time.time()
    if HARVEST_PARALLEL and len(targets) > 1:
        print(f"  => 并行收菜 {len(targets)} 块...")
        with ThreadPoolExecutor(max_workers=min(3, len(targets))) as pool:
            futures = [pool.submit(_harvest_one, client, s, action_token) for s in targets]
            for fut in as_completed(futures):
                try:
                    ok, info = fut.result()
                except Exception as e:
                    print(f"  收菜异常: {e}")
                    continue
                if ok and info:
                    harvested.append(info)
    else:
        print(f"  => 收菜 {len(targets)} 块（随机间隔）...")
        for s in targets:
            try:
                ok, info = _harvest_one(client, s, action_token)
            except Exception as e:
                print(f"  收菜异常: {e}")
                continue
            if ok and info:
                harvested.append(info)
            _sleep_rand(ACTION_GAP_SEC_MIN, ACTION_GAP_SEC_MAX)

    harvested.sort(key=lambda x: (x.get("slot") is None, x.get("slot")))
    print(f"  => 收菜完成 {len(harvested)}/{len(targets)} 耗时 {time.time() - t0:.2f}s")
    return harvested


def scan_my_farm(
    client: SignClient,
    crop_id: str = None,
    clear_other_crops: bool = False,
    do_care: bool = True,
    do_harvest: bool = True,
    do_plant: bool = True,
    do_explore: bool = True,
):
    """
    查自己地块：可浇先浇，可收则快收，再立刻种植。
    crop_id: 本轮种植作物；默认按 VIP 自动选择（VIP=quinoa，非 VIP=carrot）。
    clear_other_crops: True 时铲除非目标作物（夜间换种用），再种目标作物。
    do_care / do_harvest / do_plant / do_explore: 分通道时可单独关掉。
    返回 dict:
      nearest, nearest_care, harvested, planted, cared, removed,
      care_count, care_cap, did_action, api_error, action_error,
      target_crop_ready, plant_crop, is_premium
    """
    print(f"\n{'=' * 48}")
    mode_tip = f" clear_other={clear_other_crops}" if clear_other_crops else ""
    if do_harvest:
        title = "我的农场（优先收菜防偷）"
    elif do_care:
        title = "我的农场（浇水）"
    else:
        title = "我的农场"
    print(f"{title}{mode_tip}")
    print("=" * 48)

    result = {
        "nearest": None,
        "nearest_care": None,
        "harvested": [],
        "planted": [],
        "cared": [],
        "explored": [],
        "removed": [],
        "care_count": 0,
        "care_cap": None,
        "did_action": False,
        "api_error": False,
        "action_error": False,
        "target_crop_ready": False,
        "plant_crop": None,
        "is_premium": None,
        "explore_limit_reached": False,
        "care_limit_reached": False,
    }

    try:
        farm = get_my_farm(client)
    except Exception as e:
        print(f"  查询失败: {e}")
        result["api_error"] = True
        return result

    data = farm.get("data") or {}
    is_premium = bool(data.get("isPremium"))
    plant_crop = crop_id or resolve_day_crop_id(is_premium)
    result["is_premium"] = is_premium
    result["plant_crop"] = plant_crop
    print(f"  cropId={plant_crop} vip={is_premium}")

    slots = active_slots(data)
    action_token = data.get("actionToken") or ""
    care_count = int(data.get("todayCareCount") or 0)
    care_cap = (data.get("constants") or {}).get("DAILY_CARE_CAP")
    if care_cap is not None:
        care_cap = int(care_cap)
    result["care_count"] = care_count
    result["care_cap"] = care_cap

    unlocked = data.get("unlockedSlots")
    print(
        f"  level={data.get('level')} exp={data.get('experience')} "
        f"unlockedSlots={unlocked} 可用地块={len(slots)} "
        f"今日浇水={care_count}/{care_cap if care_cap is not None else '-'}"
    )

    harvest_targets = []
    care_targets = []
    nearest = None
    nearest_care = None
    for s in slots:
        ok = is_harvestable(s)
        care_ok = is_careable(s)
        eta = minutes_until_harvestable(s)
        care_eta = minutes_until_careable(s)
        nearest = _min_eta(nearest, eta)
        nearest_care = _min_eta(nearest_care, care_eta)
        crop = s.get("cropName") or "-"
        eta_txt = f"eta={eta}m" if eta is not None else "eta=-"
        care_txt = f"careEta={care_eta}m" if care_eta is not None else "careEta=-"
        mark = "可收" if ok else ("可浇" if care_ok else "跳过")
        print(
            f"  [{mark}] slot={s.get('slotIndex')} {crop} "
            f"state={s.get('state')} rem={s.get('remainingMinutes')} "
            f"canHarvest={s.get('canHarvest')} canCare={s.get('canCare')} "
            f"canPlant={s.get('canPlant')} {eta_txt} {care_txt}"
        )
        if ok:
            harvest_targets.append(s)
        if care_ok:
            # 夜间换种：即将铲除的非目标作物不必再浇
            if clear_other_crops and s.get("cropId") and s.get("cropId") != plant_crop:
                pass
            else:
                care_targets.append(s)

    if nearest is not None:
        print(f"  => 可收地块: {len(harvest_targets)}  最近可收约 {nearest} 分钟")
    else:
        print(f"  => 可收地块: {len(harvest_targets)}")
    if nearest_care is not None:
        print(f"  => 可浇地块: {len(care_targets)}  最近可浇约 {nearest_care} 分钟")
    else:
        print(f"  => 可浇地块: {len(care_targets)}")

    # 0) 先浇水（生长中缺水）；与帮好友浇水共用 todayCareCount / DAILY_CARE_CAP
    if do_care and care_cap is not None and care_count >= care_cap:
        result["care_limit_reached"] = True
        nearest_care = None
        care_targets = []
        print("  已达今日浇水上限（与帮浇共用），本日不再浇水")

    if do_care and AUTO_CARE and care_targets:
        if care_cap is not None:
            remain_care = max(0, care_cap - care_count)
            if remain_care <= 0:
                print("  已达今日浇水上限，跳过自己浇水")
                care_targets = []
                result["care_limit_reached"] = True
                nearest_care = None
            elif len(care_targets) > remain_care:
                print(f"  今日剩余可浇 {remain_care}，截取 {remain_care} 块")
                care_targets = care_targets[:remain_care]

        if care_targets:
            random.shuffle(care_targets)
            print(f"  => 浇水 {len(care_targets)} 块（随机间隔）...")
            for s in care_targets:
                slot_index = s.get("slotIndex")
                crop_name = s.get("cropName") or s.get("cropId") or "-"
                resp = care_slot(client, slot_index, action_token)
                if response_indicates_care_limit(resp):
                    result["care_limit_reached"] = True
                    nearest_care = None
                    print(
                        f"  接口返回今日浇水已达次数（slot={slot_index}），"
                        f"浇水通道今日不再触发"
                    )
                    break
                ok = resp.get("status") == 200 and (resp.get("data") or {}).get("success") is True
                if ok:
                    result["did_action"] = True
                    care_count += 1
                    result["cared"].append({
                        "slot": slot_index,
                        "cropName": crop_name,
                        "message": (resp.get("data") or {}).get("message"),
                    })
                _sleep_rand(ACTION_GAP_SEC_MIN, ACTION_GAP_SEC_MAX)

            result["care_count"] = care_count
            if result.get("care_limit_reached"):
                nearest_care = None
                print("  浇水次数已用完，本日不再浇水")
            elif care_targets and not result["cared"]:
                result["action_error"] = True
                print("  => 浇水接口无成功返回，稍后按半天间隔重试")
            elif care_cap is not None and care_count >= care_cap:
                result["care_limit_reached"] = True
                nearest_care = None
                print("  浇水次数已用完，本日不再浇水")
    elif do_care and result["care_limit_reached"]:
        pass
    elif do_care and AUTO_CARE and care_cap is not None and care_count >= care_cap:
        result["care_limit_reached"] = True
        nearest_care = None
    elif not do_care:
        nearest_care = None

    # 0.5) 成熟地先翻（与好友共用每日翻地上限），再收菜
    if do_explore and AUTO_EXPLORE:
        explore_st = data.get("explorationStatus") or {}
        explore_count = explore_st.get("todayCount")
        explore_limit = explore_st.get("dailyLimit")
        can_explore_global = explore_st.get("canExplore", True)
        print(
            f"  今日翻地={explore_count} 上限={explore_limit} canExplore={can_explore_global}"
        )
        explore_targets = [s for s in slots if is_explorable(s)]
        if can_explore_global is False:
            result["explore_limit_reached"] = True
            explore_targets = []
        elif explore_limit is not None and explore_count is not None and int(explore_count) >= int(explore_limit):
            result["explore_limit_reached"] = True
            explore_targets = []
        elif explore_targets:
            if explore_limit is not None and explore_count is not None:
                remain_ex = max(0, int(explore_limit) - int(explore_count))
                if remain_ex <= 0:
                    result["explore_limit_reached"] = True
                    explore_targets = []
                elif len(explore_targets) > remain_ex:
                    print(f"  今日剩余可翻 {remain_ex}，截取 {remain_ex} 块（自己）")
                    explore_targets = explore_targets[:remain_ex]

        if explore_targets:
            random.shuffle(explore_targets)
            print(f"  => 翻自己地 {len(explore_targets)} 块（随机间隔）...")
            for s in explore_targets:
                slot_index = s.get("slotIndex")
                crop_name = s.get("cropName") or s.get("cropId") or "-"
                resp = explore_own_slot(client, slot_index, action_token)
                ok = resp.get("status") == 200 and (resp.get("data") or {}).get("success") is True
                if ok:
                    result["did_action"] = True
                    if explore_count is not None:
                        explore_count = int(explore_count) + 1
                    result["explored"].append({
                        "friend_id": None,
                        "nick_name": "自己",
                        "slot": slot_index,
                        "cropName": crop_name,
                        "message": (resp.get("data") or {}).get("message"),
                    })
                _sleep_rand(ACTION_GAP_SEC_MIN, ACTION_GAP_SEC_MAX)
            if explore_targets and not result["explored"]:
                # 翻地失败不阻断收菜调度（避免进秘境时瞬时失败把收菜 ETA 打成 25m 兜底）
                print("  => 自己翻地接口无成功返回，继续尝试收菜")
            if (
                explore_limit is not None
                and explore_count is not None
                and int(explore_count) >= int(explore_limit)
            ):
                result["explore_limit_reached"] = True
        elif result["explore_limit_reached"]:
            print("  已达今日翻地上限，跳过自己翻地")

    # 1) 再全部快收（不穿插种植、不等待）
    if do_harvest and AUTO_HARVEST and harvest_targets:
        harvested = harvest_targets_fast(client, harvest_targets, action_token)
        result["harvested"] = harvested
        if harvested:
            result["did_action"] = True
        else:
            # 可收但接口全失败/无返回：避免 eta=0 立刻重打
            result["action_error"] = True
            print("  => 收菜接口无成功返回，稍后按半天间隔重试")

    # 2) 立刻种刚收完的地块（不先 GET，缩短空窗）
    plant_attempted = False
    if do_plant and AUTO_PLANT and result["harvested"]:
        plant_attempted = True
        plant_order = list(result["harvested"])
        random.shuffle(plant_order)
        for h in plant_order:
            slot_index = h.get("slot")
            plant_resp = plant_slot(client, slot_index, plant_crop, action_token)
            if plant_resp.get("status") == 200 and (plant_resp.get("data") or {}).get("success") is True:
                result["planted"].append({"slot": slot_index, "cropId": plant_crop})
            _sleep_rand(PLANT_GAP_SEC_MIN, PLANT_GAP_SEC_MAX)

    # 2.5) 夜间换种：铲除仍占着地的非目标作物，腾出空地
    if clear_other_crops and do_plant and AUTO_PLANT:
        try:
            farm_clr = get_my_farm(client)
            data_clr = farm_clr.get("data") or {}
            slots_clr = active_slots(data_clr)
            action_token = data_clr.get("actionToken") or action_token
        except Exception as e:
            print(f"  铲除前刷新失败: {e}")
            result["api_error"] = True
            slots_clr = slots

        planted_slots = {p["slot"] for p in result["planted"]}
        clear_targets = []
        for s in slots_clr:
            slot_index = s.get("slotIndex")
            if slot_index in planted_slots:
                continue
            crop_id_now = s.get("cropId")
            if not crop_id_now:
                continue
            if crop_id_now == plant_crop:
                continue
            if is_removable(s):
                clear_targets.append(s)
            else:
                print(
                    f"  无法铲除 slot={slot_index} {s.get('cropName') or crop_id_now} "
                    f"state={s.get('state')} canRemove={s.get('canRemove')} "
                    f"canManualRemove={s.get('canManualRemove')}"
                )

        if clear_targets:
            random.shuffle(clear_targets)
            print(f"  => 铲除非目标作物 {len(clear_targets)} 块（换种 {plant_crop}）...")
            for s in clear_targets:
                slot_index = s.get("slotIndex")
                crop_name = s.get("cropName") or s.get("cropId") or "-"
                resp = remove_slot(client, slot_index, action_token)
                ok = resp.get("status") == 200 and (resp.get("data") or {}).get("success") is True
                if ok:
                    result["did_action"] = True
                    result["removed"].append({
                        "slot": slot_index,
                        "cropName": crop_name,
                        "cropId": s.get("cropId"),
                        "message": (resp.get("data") or {}).get("message"),
                    })
                _sleep_rand(ACTION_GAP_SEC_MIN, ACTION_GAP_SEC_MAX)
            if clear_targets and not result["removed"]:
                result["action_error"] = True
                print("  => 铲除接口无成功返回，稍后按半天间隔重试")

    # 3) 再扫一次：补种其它空地 + 重算 ETA
    if do_plant and AUTO_PLANT:
        try:
            farm2 = get_my_farm(client)
            data2 = farm2.get("data") or {}
            slots2 = active_slots(data2)
            action_token = data2.get("actionToken") or action_token
            if data2.get("todayCareCount") is not None:
                care_count = int(data2.get("todayCareCount") or care_count)
                result["care_count"] = care_count
        except Exception as e:
            print(f"  补种前刷新失败: {e}")
            result["api_error"] = True
            slots2 = slots

        planted_slots = {p["slot"] for p in result["planted"]}
        empties = [s for s in slots2 if s.get("slotIndex") not in planted_slots and is_plantable(s)]
        if empties:
            plant_attempted = True
        random.shuffle(empties)
        for s in empties:
            slot_index = s.get("slotIndex")
            plant_resp = plant_slot(client, slot_index, plant_crop, action_token)
            if plant_resp.get("status") == 200 and (plant_resp.get("data") or {}).get("success") is True:
                result["planted"].append({"slot": slot_index, "cropId": plant_crop})
            _sleep_rand(PLANT_GAP_SEC_MIN, PLANT_GAP_SEC_MAX)

        if plant_attempted and not result["planted"]:
            already_ok = clear_other_crops and slots2 and all(
                (s.get("cropId") == plant_crop) for s in slots2
            )
            if not already_ok:
                result["action_error"] = True
                print("  => 种植接口无成功返回，稍后按半天间隔重试")

        try:
            farm3 = get_my_farm(client)
            slots3 = active_slots(farm3.get("data") or {})
            nearest = None
            nearest_care = None
            for s in slots3:
                nearest = _min_eta(nearest, minutes_until_harvestable(s))
                nearest_care = _min_eta(nearest_care, minutes_until_careable(s))
            if clear_other_crops:
                result["target_crop_ready"] = bool(slots3) and all(
                    (s.get("cropId") == plant_crop) for s in slots3
                )
        except Exception as e:
            print(f"  重算可收 ETA 失败: {e}")
            result["api_error"] = True
            nearest = None
            nearest_care = None
            for s in slots2:
                nearest = _min_eta(nearest, minutes_until_harvestable(s))
                nearest_care = _min_eta(nearest_care, minutes_until_careable(s))
            if clear_other_crops:
                result["target_crop_ready"] = bool(slots2) and all(
                    (s.get("cropId") == plant_crop) for s in slots2
                )
    elif result["harvested"]:
        harvested_slots = {h["slot"] for h in result["harvested"]}
        nearest = None
        nearest_care = None
        for s in slots:
            if s.get("slotIndex") in harvested_slots:
                continue
            nearest = _min_eta(nearest, minutes_until_harvestable(s))
            nearest_care = _min_eta(nearest_care, minutes_until_careable(s))
    elif result["cared"]:
        # 浇过后重算可浇 ETA（已浇块通常不再可浇）
        cared_slots = {c["slot"] for c in result["cared"]}
        nearest_care = None
        for s in slots:
            if s.get("slotIndex") in cared_slots:
                continue
            nearest_care = _min_eta(nearest_care, minutes_until_careable(s))

    # 收菜/浇水瞬时失败时：短暂重试，避免 eta=0 死循环，也不要直接睡满兜底 25m
    if result["action_error"] and nearest == 0:
        nearest = 2.0
    if result["action_error"] and nearest_care == 0:
        nearest_care = 2.0

    if result.get("care_limit_reached"):
        nearest_care = None

    result["nearest"] = nearest
    result["nearest_care"] = nearest_care
    result["care_count"] = care_count
    return result


def scan_friend(
    client: SignClient,
    friend_id: str,
    steal_count: int,
    steal_limit,
    nick_name: str = "-",
    care_count: int = 0,
    care_cap=None,
    do_help: bool = True,
    do_explore: bool = True,
    do_steal: bool = True,
):
    """
    查询好友地块：可按开关分别执行帮浇 / 翻地 / 偷菜。
    帮浇与自己浇水共用 todayCareCount / DAILY_CARE_CAP；用完本日不再帮浇。
    返回 dict（含 nearest_steal / nearest_explore / nearest_help）。
    """
    print(f"\n{'=' * 48}")
    print(f"好友 id={friend_id}  nick_name={nick_name}")
    print("=" * 48)

    out = {
        "steal_count": steal_count,
        "care_count": care_count,
        "care_cap": care_cap,
        "nearest_steal": None,
        "nearest_explore": None,
        "nearest_help": None,
        "stolen": [],
        "explored": [],
        "helped": [],
        "did_action": False,
        "api_error": False,
        "action_error": False,
        "steal_limit_reached": False,
        "explore_limit_reached": False,
        "care_limit_reached": False,
    }

    try:
        farm = get_friend_farm(client, friend_id)
    except Exception as e:
        print(f"  查询失败: {e}")
        out["api_error"] = True
        return out

    data = farm.get("data") or {}
    slots = active_slots(data)
    action_token = data.get("actionToken") or ""

    api_count = data.get("visitorTodayStealCount")
    api_limit = data.get("dailyStealLimit")
    if api_count is not None:
        steal_count = api_count
    if api_limit is not None:
        steal_limit = api_limit

    # 与自己浇水共用次数：好友农场也会带 todayCareCount
    api_care = data.get("todayCareCount")
    if api_care is not None:
        care_count = int(api_care)
    if care_cap is None:
        cap = (data.get("constants") or {}).get("DAILY_CARE_CAP")
        if cap is not None:
            care_cap = int(cap)
    out["care_count"] = care_count
    out["care_cap"] = care_cap

    explore_st = data.get("explorationStatus") or {}
    explore_count = explore_st.get("todayCount")
    explore_limit = explore_st.get("dailyLimit")
    can_explore_global = explore_st.get("canExplore", True)

    print(f"  今日已偷={steal_count} 上限={steal_limit}")
    print(f"  今日翻地={explore_count} 上限={explore_limit} canExplore={can_explore_global}")
    print(
        f"  今日浇水(共用)={care_count}/"
        f"{care_cap if care_cap is not None else '-'}"
    )
    print(f"  本轮动作: 帮浇={do_help} 翻地={do_explore} 偷菜={do_steal}")
    print(f"  unlockedSlots={data.get('unlockedSlots')} 可用地块={len(slots)}")

    # 今日翻地/偷菜/浇水上限：标记后由调度器推迟到次日，不再空转
    if do_explore and AUTO_EXPLORE:
        if can_explore_global is False:
            out["explore_limit_reached"] = True
        elif explore_limit is not None and explore_count is not None and int(explore_count) >= int(explore_limit):
            out["explore_limit_reached"] = True
    if do_steal and AUTO_STEAL and steal_limit is not None and int(steal_count) >= int(steal_limit):
        out["steal_limit_reached"] = True
    if care_cap is not None and int(care_count) >= int(care_cap):
        out["care_limit_reached"] = True

    steal_targets = []
    explore_targets = []
    help_targets = []
    nearest = None
    nearest_explore = None
    nearest_help = None
    for s in slots:
        can_steal = is_stealable(s)
        can_dig = is_explorable(s)
        can_help = is_helpable(s)
        eta = minutes_until_stealable(s)
        explore_eta = minutes_until_explorable(s)
        help_eta = minutes_until_helpable(s)
        nearest = _min_eta(nearest, eta)
        nearest_explore = _min_eta(nearest_explore, explore_eta)
        nearest_help = _min_eta(nearest_help, help_eta)
        crop = s.get("cropName") or "-"
        eta_txt = f"eta={eta}m" if eta is not None else "eta=-"
        ex_txt = f"exEta={explore_eta}m" if explore_eta is not None else "exEta=-"
        help_txt = f"helpEta={help_eta}m" if help_eta is not None else "helpEta=-"
        exp = s.get("expReward")
        exp_txt = f"exp={exp}" if exp is not None else "exp=-"
        print(
            f"  [偷:{'Y' if can_steal else 'N'} 翻:{'Y' if can_dig else 'N'} 浇:{'Y' if can_help else 'N'}] "
            f"slot={s.get('slotIndex')} {crop} state={s.get('state')} rem={s.get('remainingMinutes')} "
            f"{exp_txt} {eta_txt} {ex_txt} {help_txt} "
            f"stolenByMe={s.get('alreadyStolenByVisitor')} explored={s.get('alreadyExploredByVisitor')} "
            f"full={s.get('stolenFull')} protected={s.get('isProtected')}"
        )
        if can_steal:
            steal_targets.append(s)
        if can_dig:
            explore_targets.append(s)
        if can_help:
            help_targets.append(s)

    print(
        f"  => 可偷={len(steal_targets)} 可翻={len(explore_targets)} 可浇={len(help_targets)} "
        f"最近可偷约 {nearest} 分钟 最近可翻约 {nearest_explore} 分钟 最近可浇约 {nearest_help} 分钟"
    )
    if do_steal:
        if nearest is None:
            print("  偷菜排期：本页无合格目标（需成熟可偷 + 经验达阈值 + 非黑土地 + 未偷过/未偷满），走 25m 兜底")
        elif float(nearest) > float(SCHEDULE_FALLBACK_MINUTES):
            print(
                f"  偷菜排期：最近 {nearest} 分钟后可偷，超过{SCHEDULE_FALLBACK_MINUTES}m 兜底，"
                f"先复核以免浇水提前成熟漏偷"
            )
        else:
            print(f"  偷菜排期：仅合格地块，最近 {nearest} 分钟后可偷")
    filter_lines = summarize_slot_filters(slots)
    if filter_lines:
        print("  过滤原因:")
        for line in filter_lines:
            print(f"    {line}")

    # 0) 帮忙浇水（与自己浇水共用每日上限）
    if do_help and AUTO_FRIEND_HELP and out["care_limit_reached"]:
        print("  已达今日浇水上限（与自己浇水共用），跳过帮浇（今日不再浇水）")
        help_targets = []
        nearest_help = None
    elif do_help and AUTO_FRIEND_HELP and help_targets:
        if care_cap is not None:
            remain_care = max(0, int(care_cap) - int(care_count))
            if remain_care <= 0:
                print("  已达今日浇水上限，跳过帮浇")
                help_targets = []
                nearest_help = None
                out["care_limit_reached"] = True
            elif len(help_targets) > remain_care:
                print(f"  今日剩余可浇 {remain_care}，截取 {remain_care} 块帮浇")
                help_targets = help_targets[:remain_care]

        if help_targets:
            random.shuffle(help_targets)
            print(f"  => 帮浇 {len(help_targets)} 块（随机间隔）...")
            for s in help_targets:
                slot_index = s.get("slotIndex")
                crop_name = s.get("cropName") or s.get("cropId") or "-"
                resp = help_slot(client, friend_id, slot_index, action_token)
                if response_indicates_care_limit(resp):
                    out["care_limit_reached"] = True
                    nearest_help = None
                    print(
                        f"  接口返回今日浇水已达次数（slot={slot_index}），"
                        f"浇水通道今日不再触发"
                    )
                    break
                ok = resp.get("status") == 200 and (resp.get("data") or {}).get("success") is True
                if ok:
                    out["did_action"] = True
                    care_count = int(care_count) + 1
                    out["helped"].append({
                        "friend_id": friend_id,
                        "nick_name": nick_name,
                        "slot": slot_index,
                        "cropName": crop_name,
                        "message": (resp.get("data") or {}).get("message"),
                    })
                _sleep_rand(ACTION_GAP_SEC_MIN, ACTION_GAP_SEC_MAX)

            if out.get("care_limit_reached"):
                nearest_help = None
            elif help_targets and not out["helped"]:
                out["action_error"] = True
                print("  => 好友浇水接口无成功返回，稍后按半天间隔重试")

            helped_ok = {h["slot"] for h in out["helped"]}
            if not out.get("care_limit_reached"):
                nearest_help = None
                for s in slots:
                    if s.get("slotIndex") in helped_ok:
                        continue
                    nearest_help = _min_eta(nearest_help, minutes_until_helpable(s))
                if out["action_error"] and nearest_help == 0:
                    nearest_help = None

            if out.get("care_limit_reached") or (
                care_cap is not None and int(care_count) >= int(care_cap)
            ):
                out["care_limit_reached"] = True
                nearest_help = None
                print("  浇水次数已用完，本日不再帮浇")

    out["care_count"] = care_count
    out["care_cap"] = care_cap
    if out["care_limit_reached"]:
        nearest_help = None

    # 1) 翻地
    if do_explore and AUTO_EXPLORE and out["explore_limit_reached"]:
        print("  已达今日翻地上限，跳过翻地（今日不再调度）")
        explore_targets = []
        nearest_explore = None
    elif do_explore and AUTO_EXPLORE and explore_targets and can_explore_global:
        if explore_limit is not None and explore_count is not None:
            remain_ex = max(0, int(explore_limit) - int(explore_count))
            if remain_ex <= 0:
                print("  已达今日翻地上限，跳过翻地（今日不再调度）")
                explore_targets = []
                nearest_explore = None
                out["explore_limit_reached"] = True
            elif len(explore_targets) > remain_ex:
                print(f"  今日剩余可翻 {remain_ex}，截取 {remain_ex} 块")
                explore_targets = explore_targets[:remain_ex]

        if explore_targets:
            random.shuffle(explore_targets)
            print(f"  => 翻地 {len(explore_targets)} 块（随机间隔）...")
            for s in explore_targets:
                slot_index = s.get("slotIndex")
                crop_name = s.get("cropName") or s.get("cropId") or "-"
                resp = explore_slot(client, friend_id, slot_index, action_token)
                ok = resp.get("status") == 200 and (resp.get("data") or {}).get("success") is True
                if ok:
                    out["did_action"] = True
                    if explore_count is not None:
                        explore_count += 1
                    out["explored"].append({
                        "friend_id": friend_id,
                        "nick_name": nick_name,
                        "slot": slot_index,
                        "cropName": crop_name,
                        "message": (resp.get("data") or {}).get("message"),
                    })
                _sleep_rand(ACTION_GAP_SEC_MIN, ACTION_GAP_SEC_MAX)

            if explore_targets and not out["explored"]:
                out["action_error"] = True
                print("  => 翻地接口无成功返回，稍后按半天间隔重试")

            explored_ok = {e["slot"] for e in out["explored"]}
            nearest_explore = None
            for s in slots:
                if s.get("slotIndex") in explored_ok:
                    continue
                nearest_explore = _min_eta(nearest_explore, minutes_until_explorable(s))
            if out["action_error"] and nearest_explore == 0:
                nearest_explore = None

            # 本轮翻完后若已达上限
            if explore_limit is not None and explore_count is not None and int(explore_count) >= int(explore_limit):
                out["explore_limit_reached"] = True
                nearest_explore = None
    elif do_explore and AUTO_EXPLORE and explore_targets and not can_explore_global:
        print("  全局不可翻地，跳过（今日不再调度）")
        nearest_explore = None
        out["explore_limit_reached"] = True

    # 2) 偷菜
    if do_steal and AUTO_STEAL and out["steal_limit_reached"]:
        print("  已达今日偷取上限，跳过偷菜（今日不再调度）")
        nearest = None
        steal_targets = []
    elif do_steal and AUTO_STEAL and steal_targets:
        if steal_limit is not None:
            remain = max(0, int(steal_limit) - int(steal_count))
            if remain <= 0:
                print("  已达今日偷取上限，跳过偷菜（今日不再调度）")
                nearest = None
                out["steal_limit_reached"] = True
                out["steal_count"] = steal_count
                out["nearest_steal"] = None
                out["nearest_explore"] = nearest_explore if do_explore else None
                out["nearest_help"] = nearest_help
                return out
            if len(steal_targets) > remain:
                print(f"  今日剩余可偷 {remain}，截取 {remain} 块")
                steal_targets = steal_targets[:remain]

        random.shuffle(steal_targets)
        stolen_ok = set()
        skipped_slots = set()
        network_fails = 0
        business_fails = 0
        t0 = time.time()

        def _steal_one(slot):
            slot_index = slot.get("slotIndex")
            crop_name = slot.get("cropName") or slot.get("cropId") or "-"
            crop_id = slot.get("cropId")
            if land_blocks_steal(slot):
                return {
                    "ok": False,
                    "skip": True,
                    "error_kind": "business",
                    "message": "黑土地（本地过滤）",
                    "slot": slot_index,
                    "cropName": crop_name,
                    "cropId": crop_id,
                }
            result = steal_slot(client, friend_id, slot_index, action_token)
            if result.get("ok"):
                steal_exp = ((result.get("data") or {}).get("data") or {}).get("stealExp")
                return {
                    "ok": True,
                    "skip": False,
                    "error_kind": None,
                    "message": result.get("message"),
                    "friend_id": friend_id,
                    "nick_name": nick_name,
                    "slot": slot_index,
                    "cropName": crop_name,
                    "cropId": crop_id,
                    "stealExp": steal_exp,
                }
            return {
                "ok": False,
                "skip": bool(result.get("skip")),
                "error_kind": result.get("error_kind") or "unknown",
                "message": result.get("message"),
                "slot": slot_index,
                "cropName": crop_name,
                "cropId": crop_id,
            }

        def _handle_steal_result(info):
            nonlocal steal_count, network_fails, business_fails
            if not info:
                return
            slot_index = info.get("slot")
            crop_name = info.get("cropName") or "-"
            if info.get("ok"):
                out["stolen"].append(info)
                stolen_ok.add(slot_index)
                steal_count += 1
                out["did_action"] = True
                return
            if info.get("skip"):
                skipped_slots.add(slot_index)
                print(
                    f"  跳过不可偷 slot={slot_index} {crop_name} "
                    f"原因={info.get('message') or '业务限制'}，继续下一块"
                )
                return
            kind = info.get("error_kind") or "unknown"
            msg = info.get("message") or "-"
            if kind == "network":
                network_fails += 1
                print(f"  偷菜网络失败 slot={slot_index} {crop_name}: {msg}")
            else:
                business_fails += 1
                # 非跳过类业务失败：仍继续下一块，不中断本好友
                print(f"  偷菜业务失败 slot={slot_index} {crop_name}: {msg}，继续下一块")

        if STEAL_PARALLEL and len(steal_targets) > 1:
            print(f"  => 并行偷菜 {len(steal_targets)} 块...")
            with ThreadPoolExecutor(max_workers=min(3, len(steal_targets))) as pool:
                futures = [pool.submit(_steal_one, s) for s in steal_targets]
                for fut in as_completed(futures):
                    try:
                        info = fut.result()
                    except Exception as e:
                        network_fails += 1
                        print(f"  偷菜异常(按网络计): {e}")
                        continue
                    _handle_steal_result(info)
        else:
            print(f"  => 偷菜 {len(steal_targets)} 块（随机间隔）...")
            for s in steal_targets:
                try:
                    info = _steal_one(s)
                except Exception as e:
                    network_fails += 1
                    print(f"  偷菜异常(按网络计): {e}")
                    _sleep_rand(ACTION_GAP_SEC_MIN, ACTION_GAP_SEC_MAX)
                    continue
                _handle_steal_result(info)
                _sleep_rand(ACTION_GAP_SEC_MIN, ACTION_GAP_SEC_MAX)

        out["stolen"].sort(key=lambda x: (x.get("slot") is None, x.get("slot")))
        print(
            f"  => 偷菜完成 成功={len(out['stolen'])}/{len(steal_targets)} "
            f"跳过={len(skipped_slots)} 网络失败={network_fails} 业务失败={business_fails} "
            f"耗时 {time.time() - t0:.2f}s"
        )

        # 全失败时：仅网络问题才算 action_error（触发短重试）；全是跳过/业务则正常继续扫
        if steal_targets and not out["stolen"]:
            if network_fails > 0 and network_fails >= business_fails + len(skipped_slots):
                out["action_error"] = True
                print("  => 偷菜多为网络失败，稍后短间隔重试")
            elif skipped_slots and network_fails == 0 and business_fails == 0:
                print("  => 本好友可偷地块均已跳过（如黑土地），继续下一位")
            elif network_fails == 0:
                print("  => 本好友偷菜均业务失败/跳过，继续下一位")
            else:
                out["action_error"] = True
                print("  => 偷菜含网络失败且无成功，稍后短间隔重试")

        nearest = None
        ignore_slots = stolen_ok | skipped_slots
        for s in slots:
            if s.get("slotIndex") in ignore_slots:
                continue
            nearest = _min_eta(nearest, minutes_until_stealable(s))

        # 仍显示可偷=0 且本轮失败：短重试，避免立刻空转或睡满 25m
        if out["action_error"] and nearest == 0:
            nearest = 2.0
        elif (not out["stolen"]) and (not out["action_error"]) and nearest == 0:
            # 现在偷不了（跳过/业务失败），不要 eta=0 立刻再查好友列表
            nearest = None

        if steal_limit is not None and int(steal_count) >= int(steal_limit):
            out["steal_limit_reached"] = True
            nearest = None
            print("  偷菜后已达今日上限，本通道今日不再调度")

    out["steal_count"] = steal_count
    out["nearest_steal"] = nearest if do_steal else None
    out["nearest_explore"] = nearest_explore if do_explore else None
    if (not do_help) or out.get("care_limit_reached"):
        nearest_help = None
    out["nearest_help"] = nearest_help
    return out


def print_round_summary(
    stolen_list,
    explored_list,
    harvested_list,
    planted_list,
    cared_list=None,
    helped_list=None,
    removed_list=None,
):
    cared_list = cared_list or []
    helped_list = helped_list or []
    removed_list = removed_list or []
    print(f"\n{'#' * 48}")
    print("本轮成果汇总")
    print("#" * 48)
    if stolen_list:
        print(f"偷菜成功 {len(stolen_list)} 次：")
        for item in stolen_list:
            exp = item.get("stealExp")
            exp_txt = f" +{exp}经验" if exp is not None else ""
            print(
                f"  - 好友 {item.get('nick_name')}(id={item.get('friend_id')}) "
                f"slot={item.get('slot')} 菜={item.get('cropName')}{exp_txt}"
            )
    else:
        print("偷菜成功：无")

    if explored_list:
        print(f"翻地成功 {len(explored_list)} 次：")
        for item in explored_list:
            msg = item.get("message") or ""
            fid = item.get("friend_id")
            if fid is None:
                print(
                    f"  - 自己 slot={item.get('slot')} "
                    f"菜={item.get('cropName')} {msg}"
                )
            else:
                print(
                    f"  - 好友 {item.get('nick_name')}(id={fid}) "
                    f"slot={item.get('slot')} 菜={item.get('cropName')} {msg}"
                )
    else:
        print("翻地成功：无")

    if helped_list:
        print(f"帮好友浇水成功 {len(helped_list)} 次：")
        for item in helped_list:
            msg = item.get("message") or ""
            print(
                f"  - 好友 {item.get('nick_name')}(id={item.get('friend_id')}) "
                f"slot={item.get('slot')} 菜={item.get('cropName')} {msg}"
            )
    else:
        print("帮好友浇水成功：无")

    if cared_list:
        print(f"自己浇水成功 {len(cared_list)} 次：")
        for item in cared_list:
            msg = item.get("message") or ""
            print(f"  - 自己 slot={item.get('slot')} 菜={item.get('cropName')} {msg}")
    else:
        print("自己浇水成功：无")

    if removed_list:
        print(f"铲除作物成功 {len(removed_list)} 次：")
        for item in removed_list:
            msg = item.get("message") or ""
            print(f"  - 自己 slot={item.get('slot')} 菜={item.get('cropName')} {msg}")
    else:
        print("铲除作物成功：无")

    if harvested_list:
        print(f"收菜成功 {len(harvested_list)} 次：")
        for item in harvested_list:
            print(f"  - 自己 slot={item.get('slot')} 菜={item.get('cropName')}")
    else:
        print("收菜成功：无")

    if planted_list:
        print(f"种植成功 {len(planted_list)} 次：")
        for item in planted_list:
            print(f"  - 自己 slot={item.get('slot')} cropId={item.get('cropId')}")
    else:
        print("种植成功：无")


def build_channel_schedule(channel: str, eta_minutes, did_action=False, force_fallback=False):
    """
    单通道调度：不再与其它通道取 min。
    channel: harvest / steal / explore / care

    收菜：有可收 ETA 时必须按 ETA 等，不能被「动作后 25m」抢跑提前唤醒。
    收菜若 ETA 缺失：短间隔复核（避免成熟了还傻等 25m）。
    浇水：有可浇 ETA 时按 ETA；达今日上限则由外层标 daily_done 睡到次日。
    偷菜：有可偷 ETA 时按 ETA；但最长只睡兜底时间再扫一次
    （浇水/道具会提前成熟，一口气睡到公式成熟点会漏偷）。
    """
    lead_min, lead_max = HARVEST_LEAD_SECONDS_MIN, HARVEST_LEAD_SECONDS_MAX
    if channel == "steal":
        lead_min, lead_max = STEAL_LEAD_SECONDS_MIN, STEAL_LEAD_SECONDS_MAX
    elif channel == "explore":
        # 翻地提前量与偷菜同一量级
        lead_min, lead_max = STEAL_LEAD_SECONDS_MIN, STEAL_LEAD_SECONDS_MAX
    elif channel == "care":
        lead_min, lead_max = HARVEST_LEAD_SECONDS_MIN, HARVEST_LEAD_SECONDS_MAX

    lead_used = None
    eta_for_schedule = None
    candidates = []

    if eta_minutes is not None:
        lead_used = random.uniform(lead_min, lead_max)
        eta_for_schedule = max(0.0, float(eta_minutes) - lead_used / 60.0)
        candidates.append((channel, eta_for_schedule))

    # 收菜：有明确 ETA 时绝不用 post_action 抢跑；无 ETA 时短复核而不是 25m
    # 浇水：有明确 ETA 时按 ETA；无 ETA 时用动作后默认间隔再看
    # 偷菜：有可偷 ETA 时按 ETA，同时用 25m 兜底封顶再扫（防提前成熟漏偷）
    if channel == "steal" and eta_minutes is not None:
        candidates.append(("none", float(SCHEDULE_FALLBACK_MINUTES)))
    if did_action:
        if channel == "harvest":
            if eta_minutes is None:
                candidates.append(("post_action", 3.0))
        elif channel == "care":
            if eta_minutes is None:
                candidates.append(("post_action", float(POST_ACTION_DEFAULT_MINUTES)))
        elif channel == "steal":
            # 有可偷 ETA 时上面已用 25m 封顶；没有则走下面 25m 兜底
            pass
        else:
            candidates.append(("post_action", float(POST_ACTION_DEFAULT_MINUTES)))

    if force_fallback or not candidates:
        # 收菜通道兜底也缩短，防止「地里已成熟却睡 25m」
        fallback = 3.0 if channel == "harvest" else float(SCHEDULE_FALLBACK_MINUTES)
        candidates.append(("none", fallback))

    reason, minutes = min(candidates, key=lambda x: x[1])
    return {
        "channel": channel,
        "minutes": minutes,
        "reason": reason,
        "eta": eta_minutes,
        "eta_schedule": eta_for_schedule,
        "lead_used": lead_used,
        "did_action": did_action,
        "force_fallback": force_fallback,
        "candidates": candidates,
    }


# 兼容旧名：统一调度已废弃，保留包装以免外部引用报错
def build_schedule(nearest_steal, nearest_harvest, did_action, force_fallback=False, nearest_care=None):
    """兼容旧调用：返回 harvest 通道为主的结构（不再跨通道取最短）。"""
    harvest = build_channel_schedule(
        "harvest",
        nearest_harvest if nearest_harvest is not None else nearest_care,
        did_action=did_action,
        force_fallback=force_fallback,
    )
    steal = build_channel_schedule("steal", nearest_steal, did_action=False, force_fallback=force_fallback and nearest_steal is None)
    return {
        **harvest,
        "steal": nearest_steal,
        "harvest": nearest_harvest,
        "care": nearest_care,
        "channels": {
            "harvest": harvest,
            "steal": steal,
        },
    }


def _scan_own_explore(client):
    """翻自己地：成熟且本日未翻；与好友共用 explorationStatus 上限。"""
    out = {
        "nearest_explore": None,
        "explored": [],
        "did_action": False,
        "api_error": False,
        "action_error": False,
        "explore_limit_reached": False,
        "explore_count": None,
        "explore_limit": None,
    }
    if not AUTO_EXPLORE:
        return out

    print(f"\n{'=' * 48}")
    print("我的农场（翻地）")
    print("=" * 48)
    try:
        farm = get_my_farm(client)
    except Exception as e:
        print(f"  查询失败: {e}")
        out["api_error"] = True
        return out

    data = farm.get("data") or {}
    slots = active_slots(data)
    action_token = data.get("actionToken") or ""
    explore_st = data.get("explorationStatus") or {}
    explore_count = explore_st.get("todayCount")
    explore_limit = explore_st.get("dailyLimit")
    can_explore_global = explore_st.get("canExplore", True)
    out["explore_count"] = explore_count
    out["explore_limit"] = explore_limit
    print(f"  今日翻地={explore_count} 上限={explore_limit} canExplore={can_explore_global}")

    explore_targets = []
    nearest_explore = None
    for s in slots:
        can_dig = is_explorable(s)
        explore_eta = minutes_until_explorable(s)
        nearest_explore = _min_eta(nearest_explore, explore_eta)
        crop = s.get("cropName") or "-"
        ex_txt = f"exEta={explore_eta}m" if explore_eta is not None else "exEta=-"
        mark = "可翻" if can_dig else "跳过"
        print(
            f"  [{mark}] slot={s.get('slotIndex')} {crop} "
            f"state={s.get('state')} rem={s.get('remainingMinutes')} "
            f"explored={s.get('alreadyExploredByVisitor')} {ex_txt}"
        )
        if can_dig:
            explore_targets.append(s)

    if can_explore_global is False:
        out["explore_limit_reached"] = True
        explore_targets = []
        nearest_explore = None
    elif explore_limit is not None and explore_count is not None and int(explore_count) >= int(explore_limit):
        out["explore_limit_reached"] = True
        explore_targets = []
        nearest_explore = None
    elif explore_targets:
        if explore_limit is not None and explore_count is not None:
            remain_ex = max(0, int(explore_limit) - int(explore_count))
            if remain_ex <= 0:
                out["explore_limit_reached"] = True
                explore_targets = []
                nearest_explore = None
            elif len(explore_targets) > remain_ex:
                print(f"  今日剩余可翻 {remain_ex}，截取 {remain_ex} 块（自己）")
                explore_targets = explore_targets[:remain_ex]

    if explore_targets:
        random.shuffle(explore_targets)
        print(f"  => 翻自己地 {len(explore_targets)} 块（随机间隔）...")
        for s in explore_targets:
            slot_index = s.get("slotIndex")
            crop_name = s.get("cropName") or s.get("cropId") or "-"
            resp = explore_own_slot(client, slot_index, action_token)
            ok = resp.get("status") == 200 and (resp.get("data") or {}).get("success") is True
            if ok:
                out["did_action"] = True
                if explore_count is not None:
                    explore_count = int(explore_count) + 1
                out["explored"].append({
                    "friend_id": None,
                    "nick_name": "自己",
                    "slot": slot_index,
                    "cropName": crop_name,
                    "message": (resp.get("data") or {}).get("message"),
                })
            _sleep_rand(ACTION_GAP_SEC_MIN, ACTION_GAP_SEC_MAX)
        if explore_targets and not out["explored"]:
            out["action_error"] = True
            print("  => 自己翻地接口无成功返回，稍后按半天间隔重试")
        explored_ok = {e["slot"] for e in out["explored"]}
        nearest_explore = None
        for s in slots:
            if s.get("slotIndex") in explored_ok:
                continue
            nearest_explore = _min_eta(nearest_explore, minutes_until_explorable(s))
        if out["action_error"] and nearest_explore == 0:
            nearest_explore = None
        if (
            explore_limit is not None
            and explore_count is not None
            and int(explore_count) >= int(explore_limit)
        ):
            out["explore_limit_reached"] = True
            nearest_explore = None
    elif out["explore_limit_reached"]:
        print("  已达今日翻地上限，跳过自己翻地")
        nearest_explore = None

    out["nearest_explore"] = nearest_explore
    out["explore_count"] = explore_count
    return out


def _scan_friends_channel(client, *, do_help, do_explore, do_steal):
    """按通道扫描所有好友。

    偷菜：每次触发都拉完整好友列表，并实时 GET 每位好友当前农场再偷；
    不按上次成熟 ETA 跳过任何人。仅今日偷菜次数用尽才停止。
    """
    friend_list = resolve_friends(client)
    print(f"本轮好友: {[(fid, nick) for fid, nick in friend_list]}")
    if do_steal:
        print(f"偷菜：实时扫描全部 {len(friend_list)} 位好友当前农场")

    steal_count = 0
    steal_limit = None
    care_count = 0
    care_cap = None
    nearest_steal = None
    nearest_explore = None
    nearest_help = None
    stolen_list = []
    explored_list = []
    helped_list = []
    did_action = False
    api_error = False
    action_error = False
    friend_query_ok = False
    steal_limit_reached = False
    explore_limit_reached = False
    care_limit_reached = False

    # 帮浇与自己浇水共用次数：先读自己农场拿到当前计数
    if do_help and AUTO_FRIEND_HELP:
        try:
            my = get_my_farm(client)
            md = my.get("data") or {}
            care_count = int(md.get("todayCareCount") or 0)
            cap = (md.get("constants") or {}).get("DAILY_CARE_CAP")
            if cap is not None:
                care_cap = int(cap)
            print(f"浇水共用次数: {care_count}/{care_cap if care_cap is not None else '-'}")
            if care_cap is not None and care_count >= care_cap:
                care_limit_reached = True
                print("  浇水次数已满，本轮跳过帮浇")
        except Exception as e:
            print(f"  读取浇水次数失败: {e}")

    scanned_friends = 0
    for idx, (friend_id, nick_name) in enumerate(friend_list, 1):
        # 对应通道已达今日上限：不再继续扫后续好友
        if do_steal and steal_limit_reached and not do_help and not do_explore:
            left = len(friend_list) - idx + 1
            print(f"  偷菜今日已满，剩余 {left} 位好友不再查询")
            break
        if do_explore and explore_limit_reached:
            print("  翻地今日已满，停止继续扫好友")
            break
        if do_help and care_limit_reached and not do_steal and not do_explore:
            print("  浇水今日已满，停止继续扫好友")
            break

        if do_steal:
            print(f"  [{idx}/{len(friend_list)}] 查询好友当前农场 id={friend_id} {nick_name}")
        scanned_friends += 1
        fr = scan_friend(
            client,
            friend_id,
            steal_count,
            steal_limit,
            nick_name=nick_name,
            care_count=care_count,
            care_cap=care_cap,
            do_help=do_help and not care_limit_reached,
            do_explore=do_explore and not explore_limit_reached,
            do_steal=do_steal and not steal_limit_reached,
        )
        steal_count = fr.get("steal_count", steal_count)
        if fr.get("care_count") is not None:
            care_count = fr.get("care_count")
        if fr.get("care_cap") is not None:
            care_cap = fr.get("care_cap")
        if not fr.get("api_error"):
            friend_query_ok = True
        nearest_steal = _min_eta(nearest_steal, fr.get("nearest_steal"))
        nearest_explore = _min_eta(nearest_explore, fr.get("nearest_explore"))
        nearest_help = _min_eta(nearest_help, fr.get("nearest_help"))
        stolen_list.extend(fr.get("stolen") or [])
        explored_list.extend(fr.get("explored") or [])
        helped_list.extend(fr.get("helped") or [])
        if fr.get("did_action"):
            did_action = True
        if fr.get("api_error"):
            api_error = True
        if fr.get("action_error"):
            action_error = True
        if fr.get("steal_limit_reached"):
            steal_limit_reached = True
            nearest_steal = None
        if fr.get("explore_limit_reached"):
            explore_limit_reached = True
            nearest_explore = None
        if fr.get("care_limit_reached"):
            care_limit_reached = True
            nearest_help = None
            if do_help and not do_steal and not do_explore:
                print("  浇水今日已满，停止继续扫好友")
                break
        _sleep_rand(STEAL_FRIEND_GAP_SEC_MIN, STEAL_FRIEND_GAP_SEC_MAX)

    if friend_list and not friend_query_ok:
        api_error = True
        nearest_steal = None
        nearest_explore = None

    if care_limit_reached:
        nearest_help = None

    if do_steal:
        print(
            f"偷菜扫描完成: {scanned_friends}/{len(friend_list)} 位好友当前农场，"
            f"成功偷 {len(stolen_list)} 次"
        )

    return {
        "steal_count": steal_count,
        "care_count": care_count,
        "care_cap": care_cap,
        "nearest_steal": nearest_steal,
        "nearest_explore": nearest_explore,
        "nearest_help": nearest_help,
        "stolen": stolen_list,
        "explored": explored_list,
        "helped": helped_list,
        "did_action": did_action,
        "api_error": api_error,
        "action_error": action_error,
        "friend_list": friend_list,
        "steal_limit_reached": steal_limit_reached,
        "explore_limit_reached": explore_limit_reached,
        "care_limit_reached": care_limit_reached,
    }


def run_harvest_round():
    """收菜通道：自己收菜 + 种植（不含浇水）。"""
    client = SignClient(quiet=True)
    client.register_signing_key(force=False)
    print(
        f"\n>>> 通道【收菜】开始  AUTO_HARVEST={AUTO_HARVEST} "
        f"AUTO_PLANT={AUTO_PLANT} crop=按VIP自动"
    )

    my = scan_my_farm(client, do_care=False)
    print(
        f"  本轮作物={my.get('plant_crop')} vip={my.get('is_premium')}"
    )
    nearest_harvest = my.get("nearest")
    eta = nearest_harvest

    did_action = bool(my.get("did_action"))
    api_error = bool(my.get("api_error"))
    action_error = bool(my.get("action_error"))
    # action_error 时 scan_my_farm 已给出短重试 ETA；不要再强行 25m 兜底把收菜拖死
    force_fallback = api_error or eta is None

    print_round_summary(
        [],
        my.get("explored") or [],
        my.get("harvested") or [],
        my.get("planted") or [],
        cared_list=[],
    )

    schedule = build_channel_schedule("harvest", eta, did_action=did_action, force_fallback=force_fallback)
    schedule.update({
        "harvested": my.get("harvested") or [],
        "planted": my.get("planted") or [],
        "cared": [],
        "stolen": [],
        "explored": my.get("explored") or [],
        "helped": [],
        "api_error": api_error,
        "action_error": action_error,
        "nearest_harvest": nearest_harvest,
        "nearest_care": None,
    })
    print(
        f"【收菜】完成 最近可收={nearest_harvest}m "
        f"下次基准={schedule['reason']} {schedule['minutes']}m"
    )
    return schedule


def run_steal_round():
    """偷菜通道：每次触发都实时扫全部好友当前农场再偷（不帮浇、不翻地）。"""
    client = SignClient()
    client.register_signing_key(force=False)
    print(f"\n>>> 通道【偷菜】开始  AUTO_STEAL={AUTO_STEAL}")

    fr = _scan_friends_channel(
        client,
        do_help=False,
        do_explore=False,
        do_steal=True,
    )
    eta = fr.get("nearest_steal")
    did_action = bool(fr.get("did_action"))
    api_error = bool(fr.get("api_error"))
    action_error = bool(fr.get("action_error"))
    daily_done = bool(fr.get("steal_limit_reached"))
    stolen = fr.get("stolen") or []
    # 本轮确认现在偷不了：不要用 eta=0 立刻再拉好友列表
    if (not stolen) and (not action_error) and eta == 0:
        eta = None
    if action_error and eta == 0:
        eta = 2.0
    force_fallback = (not daily_done) and (api_error or eta is None)

    print_round_summary(
        fr.get("stolen") or [],
        [],
        [],
        [],
        helped_list=[],
    )

    schedule = build_channel_schedule("steal", eta, did_action=did_action, force_fallback=force_fallback)
    schedule.update({
        "stolen": fr.get("stolen") or [],
        "explored": [],
        "helped": [],
        "harvested": [],
        "planted": [],
        "cared": [],
        "api_error": api_error,
        "action_error": action_error,
        "nearest_steal": eta,
        "daily_done": daily_done,
    })
    if daily_done:
        schedule["reason"] = "daily_limit"
        schedule["minutes"] = None
        print("【偷菜】今日已达上限，本通道今日不再触发")
    else:
        extra = ""
        if eta is not None and schedule.get("minutes") is not None and float(eta) > float(schedule["minutes"]) + 0.5:
            extra = f"（公式ETA={eta}m，按{SCHEDULE_FALLBACK_MINUTES}m兜底先复核）"
        print(
            f"【偷菜】完成 最近可偷={eta}m 下次基准={schedule['reason']} {schedule['minutes']}m{extra}"
        )
    return schedule


def run_explore_round():
    """翻地通道：先翻自己地，再扫好友（共用每日上限）。"""
    client = SignClient()
    client.register_signing_key(force=False)
    print(f"\n>>> 通道【翻地】开始  AUTO_EXPLORE={AUTO_EXPLORE}")

    own = _scan_own_explore(client)
    fr = _scan_friends_channel(
        client,
        do_help=False,
        do_explore=True and not own.get("explore_limit_reached"),
        do_steal=False,
    )

    eta = _min_eta(own.get("nearest_explore"), fr.get("nearest_explore"))
    did_action = bool(own.get("did_action") or fr.get("did_action"))
    api_error = bool(own.get("api_error") or fr.get("api_error"))
    action_error = bool(own.get("action_error") or fr.get("action_error"))
    daily_done = bool(own.get("explore_limit_reached") or fr.get("explore_limit_reached"))
    force_fallback = (not daily_done) and (api_error or action_error or eta is None)

    explored = (own.get("explored") or []) + (fr.get("explored") or [])
    print_round_summary([], explored, [], [])

    schedule = build_channel_schedule("explore", eta, did_action=did_action, force_fallback=force_fallback)
    schedule.update({
        "stolen": [],
        "explored": explored,
        "helped": [],
        "harvested": [],
        "planted": [],
        "cared": [],
        "api_error": api_error,
        "action_error": action_error,
        "nearest_explore": eta,
        "daily_done": daily_done,
    })
    if daily_done:
        schedule["reason"] = "daily_limit"
        schedule["minutes"] = None
        print("【翻地】今日已达上限，本通道今日不再触发")
    else:
        print(f"【翻地】完成 最近可翻={eta}m 下次基准={schedule['reason']} {schedule['minutes']}m")
    return schedule


def run_care_round():
    """浇水通道：先浇自己地，再帮好友浇（共用每日上限）；达上限本日不再触发。"""
    client = SignClient()
    client.register_signing_key(force=False)
    print(
        f"\n>>> 通道【浇水】开始  AUTO_CARE={AUTO_CARE} "
        f"AUTO_FRIEND_HELP={AUTO_FRIEND_HELP}"
    )

    own = {
        "nearest_care": None,
        "cared": [],
        "did_action": False,
        "api_error": False,
        "action_error": False,
        "care_limit_reached": False,
        "care_count": None,
        "care_cap": None,
    }
    if AUTO_CARE:
        own = scan_my_farm(
            client,
            do_care=True,
            do_harvest=False,
            do_plant=False,
            do_explore=False,
        )

    daily_done = bool(own.get("care_limit_reached"))
    fr = {
        "nearest_help": None,
        "helped": [],
        "did_action": False,
        "api_error": False,
        "action_error": False,
        "care_limit_reached": False,
        "care_count": own.get("care_count"),
        "care_cap": own.get("care_cap"),
    }
    if AUTO_FRIEND_HELP and not daily_done:
        fr = _scan_friends_channel(
            client,
            do_help=True,
            do_explore=False,
            do_steal=False,
        )
        daily_done = daily_done or bool(fr.get("care_limit_reached"))

    eta = _min_eta(own.get("nearest_care"), fr.get("nearest_help"))
    if daily_done:
        eta = None
    did_action = bool(own.get("did_action") or fr.get("did_action"))
    api_error = bool(own.get("api_error") or fr.get("api_error"))
    action_error = bool(own.get("action_error") or fr.get("action_error"))
    if action_error and eta == 0:
        eta = 2.0
    force_fallback = (not daily_done) and (api_error or eta is None)

    print_round_summary(
        [],
        [],
        [],
        [],
        cared_list=own.get("cared") or [],
        helped_list=fr.get("helped") or [],
    )

    schedule = build_channel_schedule("care", eta, did_action=did_action, force_fallback=force_fallback)
    care_count = fr.get("care_count")
    if care_count is None:
        care_count = own.get("care_count")
    care_cap = fr.get("care_cap")
    if care_cap is None:
        care_cap = own.get("care_cap")
    schedule.update({
        "stolen": [],
        "explored": [],
        "helped": fr.get("helped") or [],
        "harvested": [],
        "planted": [],
        "cared": own.get("cared") or [],
        "api_error": api_error,
        "action_error": action_error,
        "nearest_care": eta,
        "care_count": care_count,
        "care_cap": care_cap,
        "care_limit_reached": daily_done,
        "daily_done": daily_done,
    })
    print(
        f"【浇水】完成 最近可浇={eta}m 次数={care_count}/"
        f"{care_cap if care_cap is not None else '-'}"
    )
    if daily_done:
        schedule["reason"] = "daily_limit"
        schedule["minutes"] = None
        print("【浇水】今日已达上限，本通道今日不再触发")
    else:
        print(f"【浇水】下次基准={schedule['reason']} {schedule['minutes']}m")
    return schedule


def run_night_round():
    """
    夜间作息：只处理自己农场，不扫好友。
    - 地里若还有未成熟的非夜间作物：先睡到成熟，收完再种夜间作物
    - 已成熟/空地：收割后种夜间作物（必要时铲除枯萎等可移除残留）
    成功种满夜间作物后由外层睡到早晨。
    """
    client = SignClient()
    client.register_signing_key(force=False)

    print(
        f"夜间作息：优先等非 {NIGHT_CROP_ID} 成熟收割，再种植 {NIGHT_CROP_ID}；"
        f"跳过好友翻地/偷菜/帮浇；种好后睡到早晨再恢复白天（VIP=quinoa / 非VIP=carrot）"
    )

    # 先看地里是否还有未成熟的白天作物 —— 有则只返回等待 ETA，不铲除
    try:
        farm = get_my_farm(client)
    except Exception as e:
        print(f"  夜间查询农场失败: {e}")
        result = build_channel_schedule("harvest", None, did_action=False, force_fallback=True)
        result.update({
            "stolen": [],
            "explored": [],
            "helped": [],
            "cared": [],
            "removed": [],
            "harvested": [],
            "planted": [],
            "night_mode": True,
            "night_wait_harvest": False,
            "night_plant_ok": False,
            "api_error": True,
            "action_error": False,
        })
        return result

    data = farm.get("data") or {}
    slots = active_slots(data)
    wait_eta = None
    waiting_slots = []
    for s in slots:
        crop_id = s.get("cropId")
        if not crop_id or crop_id == NIGHT_CROP_ID:
            continue
        if is_harvestable(s):
            continue
        eta = minutes_until_harvestable(s)
        waiting_slots.append({
            "slot": s.get("slotIndex"),
            "cropName": s.get("cropName") or crop_id,
            "eta": eta,
            "state": s.get("state"),
            "rem": s.get("remainingMinutes"),
        })
        wait_eta = _min_eta(wait_eta, eta)

    if waiting_slots:
        print(f"  地里还有 {len(waiting_slots)} 块未成熟作物，先等成熟再收再种 {NIGHT_CROP_ID}：")
        for item in waiting_slots:
            eta_txt = f"{item['eta']}m" if item["eta"] is not None else "-"
            print(
                f"    slot={item['slot']} {item['cropName']} "
                f"state={item['state']} rem={item['rem']} eta={eta_txt}"
            )
        force_fallback = wait_eta is None
        result = build_channel_schedule(
            "harvest",
            wait_eta,
            did_action=False,
            force_fallback=force_fallback,
        )
        if not force_fallback:
            result["reason"] = "night_wait_harvest"
        result.update({
            "stolen": [],
            "explored": [],
            "helped": [],
            "cared": [],
            "removed": [],
            "harvested": [],
            "planted": [],
            "night_mode": True,
            "night_wait_harvest": True,
            "night_plant_ok": False,
            "nearest_harvest": wait_eta,
            "api_error": False,
            "action_error": False,
        })
        print(
            f"  => 夜间等待收割：最近约 {wait_eta} 分钟 "
            f"(调度 {result.get('reason')} {result.get('minutes')}m)"
        )
        return result

    # 无可等待的未成熟作物：收成熟菜 → 种夜间作物（残留可铲则铲）
    my = scan_my_farm(client, crop_id=NIGHT_CROP_ID, clear_other_crops=True)
    harvested_list = my.get("harvested") or []
    planted_list = my.get("planted") or []
    cared_list = my.get("cared") or []
    removed_list = my.get("removed") or []
    explored_list = my.get("explored") or []
    did_action = bool(my.get("did_action"))
    api_error = bool(my.get("api_error"))
    action_error = bool(my.get("action_error"))
    target_ready = bool(my.get("target_crop_ready"))

    print_round_summary(
        [],
        explored_list,
        harvested_list,
        planted_list,
        cared_list=cared_list,
        removed_list=removed_list,
    )

    force_fallback = api_error or action_error or not target_ready
    result = build_channel_schedule("harvest", None, did_action=did_action, force_fallback=force_fallback)
    result["stolen"] = []
    result["explored"] = explored_list
    result["helped"] = []
    result["cared"] = cared_list
    result["removed"] = removed_list
    result["harvested"] = harvested_list
    result["planted"] = planted_list
    result["night_mode"] = True
    result["night_wait_harvest"] = False
    # 全部地块已是夜间作物才睡到早晨；否则按兜底间隔重试
    result["night_plant_ok"] = target_ready and (not api_error) and (not action_error)
    result["api_error"] = api_error
    result["action_error"] = action_error

    print(
        f"\n夜间轮完成。种植={len(planted_list)} 收菜={len(harvested_list)} "
        f"浇水={len(cared_list)} 铲除={len(removed_list)}  "
        f"target_ready={target_ready} api_error={api_error} action_error={action_error}  "
        f"night_plant_ok={result['night_plant_ok']}"
    )
    return result


def run_round():
    """一次性跑齐农场四通道（main.py 兼容）；定时循环请用分通道调度。"""
    print(
        f"自动偷取: {AUTO_STEAL}  自动翻地: {AUTO_EXPLORE}  自动收菜: {AUTO_HARVEST}  "
        f"自动种植: {AUTO_PLANT}  自动浇水: {AUTO_CARE}  帮好友浇水: {AUTO_FRIEND_HELP}  "
        f"白天作物=按VIP自动"
    )
    harvest = run_harvest_round()
    care = run_care_round()
    explore = run_explore_round()
    steal = run_steal_round()
    return {
        "channels": {
            "harvest": harvest,
            "care": care,
            "explore": explore,
            "steal": steal,
        },
        "minutes": harvest.get("minutes"),
        "reason": "harvest",
        "harvested": harvest.get("harvested") or [],
        "planted": harvest.get("planted") or [],
        "cared": care.get("cared") or [],
        "explored": explore.get("explored") or [],
        "stolen": steal.get("stolen") or [],
        "helped": care.get("helped") or [],
    }


# 兼容旧入口名
main = run_round
