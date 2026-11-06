"""
relay.py —— 司机只插自己的车，换插头全由夜班人员 on-call 来做

主循环每 15 分钟调用一次 apply(state)，放在 get_state 之后、health 之前。
它只决定两件事：
  1. 哪辆车插在哪个桩上
  2. 哪些车要提前充好，等夜班人员到场换下来（改它的 depart_slot）
电怎么分，还是 MPC / LLF 的事。

司机到达时只把自己的车插上空桩，没有空桩就停着等，不碰别的车。
夜班人员每晚按 ON_CALL_TIMES 到场，留到所有等着的车都插上才走。
某辆车再不插就来不及，或者困在故障柜上时，夜班人员再来一趟（兜底）。

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
                         [driver] [wait] [visit]（夜班人员来了一趟） [on-call]（换了一次车）

自己读三个 CSV（只读）：chargers.csv、vehicles.csv、fleet_schedule.csv
"""
import csv
from datetime import datetime, timedelta

# ---------- 可以改的参数 ----------
DATA_DIR = "data"
ON_CALL_TIMES = {"S1": "00:30", "S2": "23:00"}   # 每站每晚的到场时间，不做人员分配，只看换车的净次数和净时间
MINUTES_PER_MOVE = 8         # 每次换车的耗时，来自 operations_log.csv
MARGIN_KWH = 0.01            # 多充这么多 kWh，防止求解器差一点点就算欠电
SLOT = timedelta(minutes=15)

# ---------- 从 CSV 读来的数据（第一次调用时读） ----------
_chargers = {}   # 场站 -> [(桩 id, 柜子 id, 桩最大 kW), ...]
_schedule = {}   # 场站 -> [每次停站的 dict, ...]

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


def _visit_today(site, t):
    hour, minute = ON_CALL_TIMES[site].split(":")
    return t.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)


def _next_visit(site, t):
    visit = _visit_today(site, t)
    return visit if visit > t else visit + timedelta(days=1)


def _too_few_chargers(site, when):
    """到 when 时在场的车比桩多，就得有车提前充好让位。"""
    on_site = [x for x in _schedule.get(site, [])
               if x["arrive"] <= when and (x["depart"] is None or x["depart"] > when)]
    return len(on_site) > len(_chargers[site])


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

    if not mem["on_site"]:
        late = [vid for vid, _ in jobs() if slack(vid) <= 0.25]
        if late or (_visit_today(site, t) == t and (jobs() or _waiting_list(mem, present))):
            mem["on_site"] = True
            mem["moves_left"] = 0.0
            why = "scheduled" if _visit_today(site, t) == t else f"backup: {late[0]} is running out of time"
            events.append(f"[visit] {now} night staff arrive ({why})")
    if not mem["on_site"]:
        return

    mem["moves_left"] += SLOT.total_seconds() / 60 / MINUTES_PER_MOVE
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
    if not _waiting_list(mem, present) and not stuck():
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
    mem = _memory.setdefault(site, {"plug_of": {}, "seen": set(), "on_site": False, "moves_left": 0.0})
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

    # 3. 夜班人员：预约到场，或被叫来兜底
    _on_call(state, mem, present, t, now, events, avoid_cabinets)

    # 4. 车比桩多时，插着的车要在夜班人员到场前充好：把 depart_slot 改成到场时间
    visit = _next_visit(site, t)
    yield_at = {vid: visit for vid in mem["plug_of"]} if _too_few_chargers(site, visit) else {}

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
