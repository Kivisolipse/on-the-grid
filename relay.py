"""
relay.py —— 司机只插自己的车，换插头全由夜班人员 on-call 来做

主循环每 15 分钟调用一次 apply(state)，放在 get_state 之后、health 之前。
它只决定两件事：
  1. 哪辆车插在哪个桩上
  2. 哪些车要提前充好，等夜班人员到场换下来（改它的 depart_slot）
电怎么分，还是 MPC / LLF 的事。

司机到达时只把自己的车插上空桩，没有空桩就停着等，不碰别的车。
夜班人员什么时候来，由系统按时刻表算一次（_plan_visit），这一趟做完之前不再改：
  车都到齐、而且有足够的车已经充好可以换下来的最早时刻，但不晚于等着的车最晚必须插上的时刻。
  定下来以后不改，是因为插着的车会按这个时间被要求充好（让位截止）；时间一变，规划器就会跟着推迟。
系统提前叫人，所以按计划到场时不算路上的时间。
没有车要处理了，或者空等超过 MAX_IDLE_MIN 分钟，就先走（还有车要换的话，下次能换时再来）。
兜底：等着的车按余量排队，第 i 辆的余量只够"路上时间 + i 次换车 + 一个时段"时，立刻叫人；
  人从 travel_times.csv 里最近的服务基地出发，路上时间之后才到。

输入 state 里用到的字段：
  state["site"]      场站，例如 "S1"
  state["t"]         当前时间（pandas Timestamp 或 datetime，带时区）
  state["cabinets"]  {柜子 id: [每个时段的上限 kW]}
  state["vehicles"]  此刻在场站里的所有车（不管插没插枪），每辆车是 dict：
                     id, soc, battery_kwh, max_kw（车自己的上限，含 80% 以上减速）,
                     need_kwh, depart_slot

输出（直接改 state 再返回）：
  state["vehicles"]      只剩插着枪的车，填好 charger / cabinet / max_kw
  state["all_vehicles"]  原来的完整列表（主循环用它记录发车）
  state["waiting"]       在场但没插枪的车 id
  state["relay_events"]  这一轮的记录，每条开头有标签，3 号按标签数次数：
                         [plan]（系统按时刻表约好夜班人员的到场时间）
                         [driver] [wait] [visit]（夜班人员来了一趟） [on-call]（换了一次车）
                         [leave]（夜班人员离开，写明这趟在现场待了几分钟）

自己读四个 CSV（只读）：chargers.csv、vehicles.csv、fleet_schedule.csv、travel_times.csv
"""
import csv
import heapq
from datetime import datetime, timedelta

# ---------- 可以改的参数 ----------
DATA_DIR = "data"
MINUTES_PER_MOVE = 8         # 每次换车的耗时，来自 operations_log.csv
MAX_IDLE_MIN = 30            # 夜班人员到场后没车可换，最多等多久就先走
PLAN_HOURS = 12              # 安排到场时间时往后看多久的时刻表
QUIET_HOURS = 1              # 最后一辆车到达后，时刻表里至少这么久没有车来，才算这一波车到齐了
MARGIN_KWH = 0.01            # 多充这么多 kWh，防止求解器差一点点就算欠电
SLOT = timedelta(minutes=15)

# ---------- 从 CSV 读来的数据（第一次调用时读） ----------
_chargers = {}   # 场站 -> [(桩 id, 柜子 id, 桩最大 kW), ...]
_schedule = {}   # 场站 -> [每次停站的 dict, ...]
_travel_min = {} # 场站 -> 从服务基地赶过来要几分钟（travel_times.csv 里最快的人）

# ---------- relay 自己的记忆（一轮一轮保留） ----------
_memory = {}     # 场站 -> {"plug_of": {车 id: 桩 id}, "seen": set(车 id), "on_site": 夜班人员在不在, "moves_left": 还能换几次}


def reset():
    """换一次运行前调用。"""
    _memory.clear()


def _to_dt(x):
    if isinstance(x, str):
        return datetime.fromisoformat(x)
    if hasattr(x, "to_pydatetime"):
        return x.to_pydatetime()
    return x


def _load():
    if _chargers:
        return
    with open(DATA_DIR + "/chargers.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            _chargers.setdefault(r["site_id"], []).append(
                (r["charger_id"], r["cabinet_id"], float(r["max_kw"])))
    vehicles = {}
    with open(DATA_DIR + "/vehicles.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            vehicles[r["vehicle_id"]] = r
    with open(DATA_DIR + "/fleet_schedule.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            v = vehicles[r["vehicle_id"]]
            need = (float(r["required_soc_pct"]) - float(r["arrive_soc_pct"])) / 100 * float(v["battery_kwh"])
            _schedule.setdefault(r["site_id"], []).append({
                "vehicle": r["vehicle_id"],
                "arrive": datetime.fromisoformat(r["arrive_ts"]),
                "depart": datetime.fromisoformat(r["depart_ts"]) if r["depart_ts"] else None,
                "need_kwh": max(0.0, need),
                "max_kw": float(v["max_charge_kw"]),
            })
    with open(DATA_DIR + "/travel_times.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            m = float(r["travel_min"])
            _travel_min[r["site_id"]] = min(m, _travel_min.get(r["site_id"], m))


def _is_done(v):
    return v["need_kwh"] <= 0


def _free_chargers(site, plug_of, avoid_cabinets):
    """空着的桩，按"先避开要坏的柜子，再选插车最少的柜子"排好序。"""
    used = set(plug_of.values())
    cars_on_cabinet = {}
    for ch_id, cab, _ in _chargers[site]:
        if ch_id in used:
            cars_on_cabinet[cab] = cars_on_cabinet.get(cab, 0) + 1
    free = [(ch_id, cab) for ch_id, cab, _ in _chargers[site] if ch_id not in used]
    free.sort(key=lambda c: (c[1] in avoid_cabinets, cars_on_cabinet.get(c[1], 0)))
    return [ch_id for ch_id, _ in free]


def _plug(site, mem, vid, who, events, avoid_cabinets, now):
    """司机把自己的车插上空桩，没有空桩返回 False。"""
    free = _free_chargers(site, mem["plug_of"], avoid_cabinets)
    if not free:
        return False
    mem["plug_of"][vid] = free[0]
    events.append(f"[driver] {now} {who} plugged {vid} into {free[0]}")
    return True


def _waiting_list(mem, present):
    """在场、没插枪、还没充够的车，最急的排前面。"""
    w = [vid for vid in present if vid not in mem["plug_of"] and not _is_done(present[vid])]
    w.sort(key=lambda vid: (present[vid]["depart_slot"], -present[vid]["need_kwh"]))
    return w


def _ceil_slot(when):
    """往后取整到 15 分钟的整点。"""
    extra = (when - when.replace(minute=0, second=0, microsecond=0)) % SLOT
    return when if extra == timedelta(0) else when + (SLOT - extra)


def _plan_visit(site, t, mem, present, share_kw):
    """
    系统按时刻表安排夜班人员什么时候来。不需要来就返回 None。
      1. 往后看 PLAN_HOURS 小时，按到达顺序模拟：司机到了有空桩就插上，没有就停着等
         （最后一辆车到达后要有 QUIET_HOURS 没有车来，才算到齐；否则先不定）
      2. 最后一辆车到达时还在等的车，就是要换上去的车，一共 k 辆
      3. 插着的车按 share_kw 一直充，估计各自什么时候充好；第 k 辆充好的时间就是最早能换完的时间
      4. 到场时间 = max(最后一辆车到达, 第 k 辆充好)
      5. 但不能晚于等着的车里最急的那辆必须插上的时间（还要算上排在它前面的换车时间）
    """
    end = t + timedelta(hours=PLAN_HOURS)

    def hours_to_charge(need, max_kw):
        return timedelta(hours=need / max(min(max_kw, share_kw), 1e-6))

    # 每辆车一条记录：[车 id, 开始充电的时间, 还差 kWh, 最大 kW, 发车时间]
    plugged = {}
    waiting = []
    for vid, v in present.items():
        depart = t + v["depart_slot"] * SLOT
        row = [vid, t, v["need_kwh"], v["max_kw"], depart]
        if vid in mem["plug_of"]:
            plugged[vid] = row
        elif not _is_done(v):
            waiting.append(row)
    free = len(_chargers[site]) - len(plugged)

    # 按时间顺序走一遍时刻表：(时间, 0 = 发车 / 1 = 到达, 序号, 内容)
    timeline = []
    seq = 0
    for row in plugged.values():
        if row[4] <= end:
            timeline.append((row[4], 0, seq, row))
            seq += 1
    for x in _schedule.get(site, []):
        if t < x["arrive"] <= end:
            timeline.append((x["arrive"], 1, seq, x))
            seq += 1
    heapq.heapify(timeline)

    last_arrival = t
    while timeline:
        when, kind, _, x = heapq.heappop(timeline)
        if kind == 0:                     # 插着的车开走了，桩空出来
            if plugged.get(x[0]) is x:
                del plugged[x[0]]
                free += 1
            continue
        last_arrival = when
        depart = x["depart"] or end + timedelta(days=1)
        row = [x["vehicle"], when, x["need_kwh"], x["max_kw"], depart]
        if x["need_kwh"] <= 0:
            continue
        if free > 0:                      # 司机插上自己的车；它发车时桩会再空出来
            free -= 1
            plugged[row[0]] = row
            if depart <= end:
                heapq.heappush(timeline, (depart, 0, seq, row))
                seq += 1
        else:
            waiting.append(row)

    if not waiting:
        return None
    # 看得到的时刻表快到头了还有车在来：这一波车还没到齐，先不定，下个时段再算
    if end - last_arrival < timedelta(hours=QUIET_HOURS):
        return None
    k = len(waiting)
    done_times = sorted(row[1] + hours_to_charge(row[2], row[3]) for row in plugged.values())
    if not done_times:
        earliest = last_arrival
    else:
        earliest = done_times[min(k, len(done_times)) - 1]
    visit = max(last_arrival, earliest, t)

    move = timedelta(minutes=MINUTES_PER_MOVE)
    waiting.sort(key=lambda row: row[4] - hours_to_charge(row[2], row[3]))
    latest = min(row[4] - hours_to_charge(row[2], row[3]) - (i + 1) * move for i, row in enumerate(waiting))
    if visit > latest:
        visit = max(latest, t)            # 宁可早点来，有几辆可能还没充好
    return _ceil_slot(visit)


def _share_kw(state):
    """桩全插满时每辆车分到的功率：柜子上限 / 柜子上的桩数，取最小。"""
    count = {}
    for _, cab, _ in _chargers[state["site"]]:
        count[cab] = count.get(cab, 0) + 1
    return min(state["cabinets"][cab][0] / n for cab, n in count.items())


def _target(site, mem, present, avoid_cabinets, healthy_only):
    """一辆车现在能去哪：返回 (桩, 要拔下的车或 None)，去不了返回 None。"""
    cabinet_of = {ch_id: cab for ch_id, cab, _ in _chargers[site]}
    used = set(mem["plug_of"].values())
    free = [ch_id for ch_id, _, _ in _chargers[site] if ch_id not in used]
    done = [x for x in mem["plug_of"] if _is_done(present[x])]
    done.sort(key=lambda x: -present[x]["soc"])
    for ch_id in free:
        if cabinet_of[ch_id] not in avoid_cabinets:
            return ch_id, None
    for x in done:
        if cabinet_of[mem["plug_of"][x]] not in avoid_cabinets:
            return mem["plug_of"][x], x
    if healthy_only:
        return None
    if free:
        return free[0], None
    if done:
        return mem["plug_of"][done[0]], done[0]
    return None


def _on_call(state, mem, present, t, now, events, avoid_cabinets):
    """夜班人员：按时到场或被叫来兜底，每次换一辆，等着的车都插上才走。"""
    site = state["site"]
    cabinet_of = {ch_id: cab for ch_id, cab, _ in _chargers[site]}
    share_kw = _share_kw(state)

    def slack(vid):
        v = present[vid]
        return v["depart_slot"] * 0.25 - v["need_kwh"] / min(v["max_kw"], share_kw)

    def stuck():
        """还没充够就困在故障柜上的车。"""
        return sorted((x for x in mem["plug_of"]
                       if cabinet_of[mem["plug_of"][x]] in avoid_cabinets and not _is_done(present[x])),
                      key=slack)

    def jobs():
        """现在能处理的车：先是等着的，再是困在故障柜上的（只往好柜子挪）。"""
        out = [(vid, False) for vid in _waiting_list(mem, present)] + [(vid, True) for vid in stuck()]
        return [(vid, is_stuck) for vid, is_stuck in out
                if _target(site, mem, present, avoid_cabinets, is_stuck) is not None]

    # 系统算出的到场时间到了，而且有车在等（或困在故障柜上）：今晚需要来一趟
    planned = mem["planned"]
    if planned is not None and t >= planned and (_waiting_list(mem, present) or stuck()):
        mem["due"] = True
    # 等着的车都插上了：今晚不用再来；预约时间过了，就清掉，下次按新的时刻表重新算
    if not _waiting_list(mem, present) and not stuck():
        mem["due"] = False
        if planned is not None and t >= planned and not mem["on_site"]:
            mem["planned"] = None

    if not mem["on_site"] and mem["coming_at"] is None:
        todo = jobs()
        travel = _travel_min.get(site, 15)
        # 兜底：按余量排队，第 i 辆车的余量只够"路上时间 + 前面 i 辆加它自己的换车时间 + 一个时段"时，立刻叫人
        queue = sorted(_waiting_list(mem, present) + stuck(), key=slack)
        late = [vid for i, vid in enumerate(queue)
                if slack(vid) <= (travel + (i + 1) * MINUTES_PER_MOVE) / 60 + 0.25]
        if late and todo:
            mem["coming_at"] = _ceil_slot(t + timedelta(minutes=travel))
            events.append(f"[visit] {now} night staff called (backup: {late[0]} is running out of time), "
                          f"{travel:.0f} min away, on site at {mem['coming_at']:%H:%M}")
        elif mem["due"] and todo:
            # 系统提前叫了人，所以按计划准时到；只有现在至少能换一辆车时才来
            mem["coming_at"] = t
            events.append(f"[visit] {now} night staff arrive (time planned by the system from the timetable)")
    if not mem["on_site"] and mem["coming_at"] is not None and t >= mem["coming_at"]:
        mem["on_site"] = True
        mem["moves_left"] = 0.0
        mem["idle_min"] = 0.0
        mem["arrived_at"] = mem["coming_at"]
        mem["coming_at"] = None
    if not mem["on_site"]:
        return

    mem["moves_left"] += SLOT.total_seconds() / 60 / MINUTES_PER_MOVE
    moved = False
    while mem["moves_left"] >= 1:
        todo = jobs()
        if not todo:
            mem["moves_left"] = 0.0   # 空等的时间不能攒起来换车
            break
        vid, is_stuck = todo[0]
        ch_id, out = _target(site, mem, present, avoid_cabinets, is_stuck)
        if is_stuck:
            del mem["plug_of"][vid]
        if out:
            del mem["plug_of"][out]
        mem["plug_of"][vid] = ch_id
        note = f", unplugged fully charged {out}" if out else ""
        note += " (moved off a faulty cabinet)" if is_stuck else ""
        events.append(f"[on-call] {now} night staff plugged {vid} into {ch_id}{note}")
        mem["moves_left"] -= 1
        moved = True
    mem["idle_min"] = 0.0 if moved else mem["idle_min"] + SLOT.total_seconds() / 60

    # 走人：没有车要处理了，或者空等太久（还有车要换的话，下次能换时再来）
    nothing_left = not _waiting_list(mem, present) and not stuck()
    if nothing_left or mem["idle_min"] >= MAX_IDLE_MIN:
        minutes = (t + SLOT - mem["arrived_at"]).total_seconds() / 60
        note = "" if nothing_left else ", will come back at the next possible swap"
        events.append(f"[leave] {now} night staff leave after {minutes:.0f} min on site{note}")
        mem["on_site"] = False


def apply(state, avoid_cabinets=()):
    """
    avoid_cabinets：可选，比如 ("C1-3",)。有多个空桩时尽量不选这些柜子上的桩。
    不传也能用。
    """
    _load()
    site = state["site"]
    t = _to_dt(state["t"])
    now = t.strftime("%d.%m %H:%M")
    mem = _memory.setdefault(site, {"plug_of": {}, "seen": set(), "on_site": False, "moves_left": 0.0,
                                    "due": False, "idle_min": 0.0, "arrived_at": None,
                                    "planned": None, "coming_at": None})
    present = {v["id"]: v for v in state["vehicles"]}
    events = []

    # 1. 走了的车：桩空出来
    for vid in sorted(mem["seen"]):
        if vid not in present:
            mem["seen"].discard(vid)
            mem["plug_of"].pop(vid, None)

    # 2. 新到的车：司机只插自己的车，没有空桩就停着等夜班人员
    arrivals = sorted(vid for vid in present if vid not in mem["seen"])
    for vid in arrivals:
        mem["seen"].add(vid)
    for vid in arrivals:
        if _is_done(present[vid]):
            continue
        if not _plug(site, mem, vid, f"driver of {vid}", events, avoid_cabinets, now):
            events.append(f"[wait] {now} {vid} arrived, no free charger, parked until night staff arrive")

    # 3. 系统按时刻表安排夜班人员什么时候来（定下来就不改）；到时间了就来，或者被叫来兜底
    if mem["planned"] is None and not mem["on_site"]:
        mem["planned"] = _plan_visit(site, t, mem, present, _share_kw(state))
        if mem["planned"] is not None:
            events.append(f"[plan] {now} system books night staff for {mem['planned']:%d.%m %H:%M}")
    _on_call(state, mem, present, t, now, events, avoid_cabinets)

    # 4. 有车要换时，插着的车要在夜班人员到场前充好：把 depart_slot 改成到场时间
    visit = mem["planned"]
    yield_at = {vid: visit for vid in mem["plug_of"]} if visit is not None and visit > t else {}

    # 5. 组装输出
    charger_info = {ch_id: (cab, kw) for ch_id, cab, kw in _chargers[site]}
    plugged = []
    for vid, ch_id in mem["plug_of"].items():
        v = dict(present[vid])
        cabinet, ch_kw = charger_info[ch_id]
        v["charger"] = ch_id
        v["cabinet"] = cabinet
        v["max_kw"] = min(v["max_kw"], ch_kw)
        if not _is_done(v):
            v["need_kwh"] += MARGIN_KWH
        if vid in yield_at:
            slots_left = (yield_at[vid] - t) // SLOT
            v["depart_slot"] = max(1, min(v["depart_slot"], slots_left))
        plugged.append(v)

    # 保险：每个桩最多一辆车
    assert len(set(mem["plug_of"].values())) == len(mem["plug_of"]), "relay: two vehicles on one charger"

    state["all_vehicles"] = state["vehicles"]
    state["vehicles"] = plugged
    state["waiting"] = sorted(vid for vid in present if vid not in mem["plug_of"])
    state["relay_events"] = events
    return state
