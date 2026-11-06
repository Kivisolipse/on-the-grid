"""health.py - faults, fan prediction and connectivity (owner: member 4).

Contract 3 (see interfaces.md), called by main.py once per site and 15-minute slot:

    h = check(site, t, vehicles=state["vehicles"])

    h["online"]          bool: can the site reach the cloud backend at t
    h["cabinet_factor"]  {cabinet_id: [48 floats]}, each value 1, 0.5 or 0, slot j = [t + 15j, t + 15j + 15)
    h["charger_factor"]  {charger_id: float}, only dispensers with an active fault, 0 = cannot charge;
                         like a cabinet fault it is assumed to last the whole horizon
    h["deadline"]        {vehicle_id: new depart_slot}, only vehicles that must be done earlier
    h["events"]          [str], human-readable log lines for log.csv and the pitch

Work orders follow the shift plan in technicians.csv: a fault goes to someone on shift, then on call,
and only then to an off-shift call-out (named as such).  A predicted fan failure gets a preventive
work order as soon as the warning appears, so the repair can be done inside a regular shift.
Once that repair is finished the predicted failure does not happen: the cabinet is only off while
the fan is replaced, and later overtemperature records of that cabinet are ignored.

Rules:
  1. Only data from BEFORE t is used (no peeking into the future).  A fault that is active now
     is assumed to last for the whole horizon, because nobody knows yet when it will clear.
  2. Every CSV this module reads is read here and nowhere else (except power_cabinets.csv,
     which is only used to list the cabinets of a site, read-only).
  3. All timestamps are pandas Timestamps with the +02:00 offset, like everywhere else.
"""
from __future__ import annotations

import math
import os
import re
from functools import lru_cache

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- 1. constants
H = 48                                   # 1.1 horizon: 48 slots of 15 minutes = 12 hours
SLOT = pd.Timedelta(minutes=15)          # 1.2 slot length
TZ_OFFSET = "+02:00"                     # 1.3 all data is Finnish time

DANGER_RPM = 550.0                       # 1.4 C1-3 derated at about 550 rpm on 13.11 01:50
WINDOW = pd.Timedelta(hours=6)           # 1.5 fit the fan trend on the last 6 hours
MIN_SLOPE_RPM_H = -15.0                  # 1.6 flatter than this is noise, not wear
ALERT_HORIZON = pd.Timedelta(hours=12)   # 1.7 only warn when the crossing is within 12 hours
DEADLINE_LEAD = pd.Timedelta(minutes=30) # 1.8 vehicles on a cabinet at risk must be done 30 min before

# 1.9 fan sensors: cabinet -> (site, its dispensers); fan_rpm in telemetry_1min.csv is per cabinet
FAN_CABINETS = {"C1-3": ("S1", ["D1-9", "D1-10", "D1-11", "D1-12"])}

# 1.10 severity -> remaining capacity of the affected cabinet
SEVERITY_FACTOR = {"major": 0.5, "critical": 0.0}

# 1.10a single-dispenser fault category -> what that dispenser can still deliver.
#       Every dispenser fault in faults.csv stops or locks the dispenser ("charging stopped",
#       "locked out", "insulation fault"), except "communication": the dispenser is only offline
#       from the backend and keeps charging under local control.  Unknown categories -> 0 (safe side).
CHARGER_FACTOR = {"communication": 1.0}

# 1.11 fault category -> spare part that usually fixes it (None = inspection only)
CATEGORY_PART = {
    "overtemperature": "P_FAN",
    "connector": "P_CABLE_CCS2",
    "power_module": "P_POWER_MODULE",
    "communication": "P_MODEM",
    "payment": "P_PAYMENT",
    "ground_fault": None,
}

# 1.12 injected outage for the demo; config.INJECTED_OUTAGES overrides it if config.py defines it
INJECTED_OUTAGES = [("S1", "2026-11-13T01:00:00+02:00", "2026-11-13T03:00:00+02:00")]

# 1.13 on-site repair time when service_tickets.csv has no earlier repair with the same part
DEFAULT_REPAIR = pd.Timedelta(minutes=90)
WEEKDAY = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


# ---------------------------------------------------------------- 2. data loading
def _data_dir() -> str:
    """2.1 config.DATA_DIR if set, else $KEMPOWER_DATA, else ./data next to this file."""
    try:
        import config  # noqa: WPS433 (member 1 owns config.py)
        if getattr(config, "DATA_DIR", None):
            return config.DATA_DIR
    except ImportError:
        pass
    return os.environ.get("KEMPOWER_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))


def _injected_outages() -> list:
    """2.2 The demo outage list, taken from config.py when member 1 defines it there."""
    try:
        import config
        return list(getattr(config, "INJECTED_OUTAGES", INJECTED_OUTAGES))
    except ImportError:
        return INJECTED_OUTAGES


def _ts(x) -> pd.Series | pd.Timestamp:
    """2.3 Parse timestamps and keep the +02:00 offset."""
    return pd.to_datetime(x).dt.tz_convert(TZ_OFFSET) if isinstance(x, pd.Series) else _as_time(x)


def _as_time(t) -> pd.Timestamp:
    """2.4 Accept a Timestamp or an ISO string; a naive time is read as Finnish time."""
    t = pd.Timestamp(t)
    return t.tz_localize(TZ_OFFSET) if t.tzinfo is None else t.tz_convert(TZ_OFFSET)


@lru_cache(maxsize=1)
def _load() -> dict:
    """2.5 Read every CSV this module needs, once."""
    d = _data_dir()
    read = lambda name: pd.read_csv(os.path.join(d, name))

    # 2.5.1 1-minute fan readings, only the cabinets we watch
    tel = read("telemetry_1min.csv")
    tel = tel[tel.fan_rpm.notna()].copy()
    tel["ts"] = _ts(tel.ts)

    # 2.5.2 fault records; an empty cleared_ts means still open at the end of the data
    faults = read("faults.csv")
    faults["start_ts"] = _ts(faults.start_ts)
    faults["cleared_ts"] = _ts(faults.cleared_ts)

    # 2.5.3 recorded connectivity outages + the injected demo outage
    out = read("connectivity_outages.csv")
    inj = pd.DataFrame(_injected_outages(), columns=["site_id", "start_ts", "end_ts"])
    out = pd.concat([out, inj], ignore_index=True)
    out["start_ts"], out["end_ts"] = _ts(out.start_ts), _ts(out.end_ts)

    # 2.5.4 who can fix what and when, spare parts, travel times (optional file)
    techs = read("technicians.csv")
    techs["shift_rule"] = techs["shift"].map(_parse_shift)   # not techs.shift: that is DataFrame.shift()
    parts = read("spare_parts.csv").set_index("part_id")
    travel_path = os.path.join(d, "travel_times.csv")
    travel = pd.read_csv(travel_path) if os.path.exists(travel_path) else None

    # 2.5.5 past work orders, used to estimate how long a repair takes on site (optional file)
    tickets_path = os.path.join(d, "service_tickets.csv")
    tickets = pd.read_csv(tickets_path) if os.path.exists(tickets_path) else None
    if tickets is not None:
        tickets["arrived_ts"] = _ts(tickets.arrived_ts)
        tickets["closed_ts"] = _ts(tickets.closed_ts)

    # 2.5.6 cabinets per site, so cabinet_factor lists every cabinet (default 1)
    cab_path = os.path.join(d, "power_cabinets.csv")
    cabinets = pd.read_csv(cab_path)[["cabinet_id", "site_id"]] if os.path.exists(cab_path) else None

    return dict(tel=tel, faults=faults, outages=out, techs=techs, parts=parts, travel=travel,
                tickets=tickets, cabinets=cabinets)


# ---------------------------------------------------------------- 3. fan prediction
def predict_fan_failure(cabinet: str, t) -> dict | None:
    """3.1 Fit a straight line through the last 6 hours of fan speed and extrapolate to DANGER_RPM.

    Returns {"predicted": Timestamp, "slope_rpm_h": float, "rpm_now": float} or None when
    there is not enough data, the trend is flat, or the crossing is more than 12 hours away.
    """
    t = _as_time(t)
    tel = _load()["tel"]
    chargers = FAN_CABINETS.get(cabinet, ("", []))[1]

    # 3.2 only readings strictly before t, in the last 6 hours
    r = tel[tel.charger_id.isin(chargers) & (tel.ts >= t - WINDOW) & (tel.ts < t)]
    if r.empty or r.ts.min() > t - WINDOW + pd.Timedelta(minutes=5):
        return None                                        # 3.3 less than 6 hours of data: say nothing

    # 3.4 average the dispensers of the cabinet per minute, then fit rpm = a * hours + b
    per_min = r.groupby("ts").fan_rpm.mean()
    hours = (per_min.index - t).total_seconds().to_numpy() / 3600.0   # 0 = now, negative = past
    slope, rpm_now = np.polyfit(hours, per_min.to_numpy(float), 1)

    # 3.5 a flat or rising fan is healthy
    if slope >= MIN_SLOPE_RPM_H:
        return None

    # 3.6 time until the line crosses DANGER_RPM (already below -> now)
    h_left = max(0.0, (DANGER_RPM - rpm_now) / slope)
    predicted = t + pd.Timedelta(hours=h_left)
    if predicted - t > ALERT_HORIZON:
        return None
    return {"predicted": predicted, "slope_rpm_h": float(slope), "rpm_now": float(rpm_now)}


# ---------------------------------------------------------------- 4. technicians and dispatch
def _parse_shift(text) -> dict:
    """4.1 'Mon-Fri 08-16, on-call week 46' -> {days: {0..4}, start: 8, end: 16, on_call_weeks: {46}}.

    on call = reachable outside the regular shift during that ISO week.
    """
    text = str(text)
    rule = {"days": set(), "start": 0, "end": 0, "on_call_weeks": set()}
    day = "|".join(WEEKDAY)
    m = re.search(rf"({day})-({day})\s+(\d{{1,2}})-(\d{{1,2}})", text)
    if m:
        rule["days"] = set(range(WEEKDAY[m[1]], WEEKDAY[m[2]] + 1))
        rule["start"], rule["end"] = int(m[3]), int(m[4])
    rule["on_call_weeks"] = {int(w) for w in re.findall(r"on-call week (\d+)", text)}
    return rule


def _on_shift(rule: dict, t: pd.Timestamp) -> bool:
    """4.2 Inside the regular shift at t."""
    return t.weekday() in rule["days"] and rule["start"] <= t.hour + t.minute / 60 < rule["end"]


def _on_call(rule: dict, t: pd.Timestamp) -> bool:
    """4.3 On call at t: the on-call week, outside the regular shift."""
    return t.isocalendar().week in rule["on_call_weeks"] and not _on_shift(rule, t)


def _shift_end(rule: dict, t: pd.Timestamp) -> pd.Timestamp:
    """4.4 End of the regular shift on the day of t."""
    return t.normalize() + pd.Timedelta(hours=rule["end"])


def _next_shift_start(rule: dict, t: pd.Timestamp) -> pd.Timestamp | None:
    """4.5 First regular shift start after t (None if the technician has no regular shift)."""
    for k in range(8):
        day = t.normalize() + pd.Timedelta(days=k)
        start = day + pd.Timedelta(hours=rule["start"])
        if day.weekday() in rule["days"] and start > t:
            return start
    return None


def _technicians(site: str, whole_cabinet: bool) -> pd.DataFrame:
    """4.6 Technicians who can do the job, closest first.

    A cabinet fault needs the 'cabinet' skill (TE1 or TE3, not TE2).
    """
    data = _load()
    techs = data["techs"]
    if whole_cabinet:
        techs = techs[techs.skills.str.contains("cabinet")]
    if data["travel"] is not None:
        tr = data["travel"][data["travel"].site_id == site].set_index("technician_id").travel_min
        techs = techs.assign(travel=techs.technician_id.map(tr).fillna(math.inf))
    else:
        techs = techs.assign(travel=math.inf)
    return techs.sort_values("travel")


def _pick_technician(site: str, whole_cabinet: bool, t: pd.Timestamp) -> dict:
    """4.7 Respect the shift plan: on shift first, then on call, else call out the closest one."""
    techs = _technicians(site, whole_cabinet)
    if techs.empty:
        return {"id": None, "status": "none", "travel": math.inf, "next_shift": None}
    for status, available in (("on shift", _on_shift), ("on call", _on_call)):
        hit = techs[[available(rule, t) for rule in techs.shift_rule]]
        if not hit.empty:
            r = hit.iloc[0]
            return {"id": r.technician_id, "status": status, "travel": r.travel, "next_shift": None}
    r = techs.iloc[0]
    return {"id": r.technician_id, "status": "call-out", "travel": r.travel,
            "next_shift": _next_shift_start(r.shift_rule, t)}


def _away(travel: float) -> str:
    """4.8 '15 min away', or 'travel time unknown'."""
    return f"{travel:.0f} min away" if np.isfinite(travel) else "travel time unknown"


def _who(tech: dict, whole_cabinet: bool, t: pd.Timestamp) -> str:
    """4.9 'TE1 (on shift, 15 min away)', or the call-out with the reason why nobody closer in time can go."""
    if tech["id"] is None:
        return "no qualified technician"
    if tech["status"] != "call-out":
        return f"{tech['id']} ({tech['status']}, {_away(tech['travel'])})"
    note = f"{tech['id']} (off shift, call-out, {_away(tech['travel'])}"
    if tech["next_shift"] is not None:
        note += f"; next shift {tech['next_shift']:%d.%m %H:%M}"
    if whole_cabinet:
        techs = _load()["techs"]
        unskilled = [r.technician_id for r in techs.itertuples()
                     if _on_call(r.shift_rule, t) and "cabinet" not in r.skills]
        if unskilled:
            note += f"; on-call {', '.join(unskilled)} lacks cabinet skill"
    return note + ")"


def _repair_time(part: str | None, t: pd.Timestamp) -> pd.Timedelta:
    """4.10 Median time on site (arrived -> closed) of earlier repairs with the same part."""
    tickets = _load()["tickets"]
    if tickets is None or part is None:
        return DEFAULT_REPAIR
    done = tickets[(tickets.part_id == part) & (tickets.closed_ts < t) & tickets.arrived_ts.notna()]
    if done.empty:
        return DEFAULT_REPAIR
    return (done.closed_ts - done.arrived_ts).median()


def _part_note(category: str) -> str:
    """4.11 Which spare part, and whether it is on the shelf or must be ordered."""
    part = CATEGORY_PART.get(category)
    if part is None:
        return "no part (inspection)"
    parts = _load()["parts"]
    if part not in parts.index:
        return f"part {part}"
    p = parts.loc[part]
    if p.stock_location == "Local service base" and p.stock_qty > 0:
        return f"part {part} (in stock at local base: {int(p.stock_qty)})"
    return f"part {part} (order from {p.stock_location}, {int(p.lead_time_days)} days)"


def _dispatch_event(f) -> str:
    """4.12 One work order line per fault, written in the round the fault first shows up."""
    whole = not isinstance(f.charger_id, str)
    tech = _pick_technician(f.site_id, whole, f.start_ts)
    where = f.cabinet_id if whole else f"{f.cabinet_id}/{f.charger_id}"
    action = "stopped" if f.severity == "critical" else "derated"
    return (f"{f.start_ts:%d.%m %H:%M} {where} {action} ({f.fault_category}, {f.severity}): "
            f"dispatch {_who(tech, whole, f.start_ts)}, {_part_note(f.fault_category)}")


def _plan_preventive(site: str, t: pd.Timestamp, predicted: pd.Timestamp) -> dict | None:
    """4.13 Who replaces the fan and when, decided when the warning appears.

    Preference: (1) someone on shift now who can finish before the shift ends,
    (2) the earliest next regular shift that still finishes in time,
    (3) only if no shift fits, a call-out planned in advance (not an emergency one).
    "In time" = DEADLINE_LEAD before the predicted failure, like the vehicle deadlines.
    Returns None when there is no qualified technician with a known travel time.
    """
    repair = _repair_time(CATEGORY_PART["overtemperature"], t)
    must_finish = predicted - DEADLINE_LEAD
    techs = _technicians(site, whole_cabinet=True)
    techs = techs[np.isfinite(techs.travel)]
    if techs.empty:
        return None

    def plan(kind, r, start):
        arrive = start + pd.Timedelta(minutes=r.travel)
        return {"kind": kind, "tech": r.technician_id, "travel": r.travel, "start": start,
                "arrive": arrive, "done": arrive + repair, "repair": repair}

    # 4.13.1 on shift now
    for r in techs.itertuples():
        p = plan("on shift", r, t)
        if _on_shift(r.shift_rule, t) and p["done"] <= min(_shift_end(r.shift_rule, t), must_finish):
            return p

    # 4.13.2 next regular shift
    best = None
    for r in techs.itertuples():
        start = _next_shift_start(r.shift_rule, t)
        if start is None:
            continue
        p = plan("next shift", r, start)
        if p["done"] <= min(_shift_end(r.shift_rule, start), must_finish) and (best is None or p["done"] < best["done"]):
            best = p
    if best is not None:
        return best

    # 4.13.3 no shift fits: plan the call-out now, so it is not a surprise at night
    r = next(techs.itertuples())
    return plan("call-out", r, max(t, must_finish - pd.Timedelta(minutes=r.travel) - repair))


def _preventive_event(site: str, cabinet: str, t: pd.Timestamp, predicted: pd.Timestamp) -> str:
    """4.14 Work order for a predicted fan failure, sent to the technician when the warning appears."""
    category = "overtemperature"
    head = (f"{t:%d.%m %H:%M} preventive work order {cabinet} (fan replacement, about "
            f"{_repair_time(CATEGORY_PART[category], t).total_seconds() / 60:.0f} min on site, {_part_note(category)}): ")
    p = _plan_preventive(site, t, predicted)
    if p is None:
        return head + "no qualified technician with a known travel time"
    if p["kind"] == "call-out":
        return (head + f"no shift ends the job before the predicted failure at {predicted:%d.%m %H:%M}: "
                f"plan a call-out for {p['tech']}, leave by {p['start']:%d.%m %H:%M} ({_away(p['travel'])})")
    when = "now (on shift, " if p["kind"] == "on shift" else f"for the next shift {p['start']:%d.%m %H:%M} ("
    return (head + f"send to {p['tech']} {when}{_away(p['travel'])}), "
            f"done by about {p['done']:%d.%m %H:%M}, before the predicted failure at {predicted:%d.%m %H:%M}")


@lru_cache(maxsize=None)
def _fan_repair(cabinet: str) -> dict | None:
    """4.15 The preventive repair of a cabinet, from its first warning on the 15-minute grid.

    None when nothing is predicted or the repair cannot finish before the predicted failure.
    check() only uses the result from "warned" on, so nothing leaks from the future.
    """
    site, chargers = FAN_CABINETS[cabinet]
    tel = _load()["tel"]
    ts = tel.ts[tel.charger_id.isin(chargers)]
    if ts.empty:
        return None
    for t in pd.date_range(ts.min().ceil("15min"), ts.max(), freq=SLOT):
        p = predict_fan_failure(cabinet, t)
        if p is not None:
            plan = _plan_preventive(site, t, p["predicted"])
            if plan is None or plan["done"] > p["predicted"]:
                return None
            return {**plan, **p, "warned": t}
    return None


def _repair_events(site: str, cabinet: str, t: pd.Timestamp, r: dict) -> list[str]:
    """4.16 Log lines of a preventive repair, each in the slot it happens."""
    out = []
    if t == r["warned"]:
        out.append(f"{t:%d.%m %H:%M} predict {cabinet} overtemperature at {r['predicted']:%d.%m %H:%M} "
                   f"(fan {r['rpm_now']:.0f} rpm, {r['slope_rpm_h']:.0f} rpm/h)")
        out.append(_preventive_event(site, cabinet, t, r["predicted"]))
    if t - SLOT < r["arrive"] <= t:
        out.append(f"{r['arrive']:%d.%m %H:%M} {cabinet} off: {r['tech']} is replacing the fan")
    if t - SLOT < r["done"] <= t:
        out.append(f"{r['done']:%d.%m %H:%M} {cabinet} back on: {r['tech']} replaced the fan, predicted failure avoided")
    return out


# ---------------------------------------------------------------- 5. the contract function
def check(site: str, t, vehicles: list | None = None) -> dict:
    """5.1 Health of one site at time t, as agreed in contract 3.

    vehicles: optional, state["vehicles"] from member 1 (each with id, cabinet, depart_slot).
    Without it, "deadline" stays empty; everything else works.
    """
    t = _as_time(t)
    data = _load()
    slot_start = [t + j * SLOT for j in range(H)]
    events: list[str] = []

    # 5.2 cabinet_factor starts at 1 for every cabinet of the site
    cabs = []
    if data["cabinets"] is not None:
        cabs = list(data["cabinets"].cabinet_id[data["cabinets"].site_id == site])
    factor = {c: [1.0] * H for c in cabs}

    # 5.3 connectivity: offline if t falls inside a recorded or injected outage
    out = data["outages"]
    is_off = lambda m: bool(((out.site_id == site) & (out.start_ts <= m) & (m < out.end_ts)).any())
    online = not is_off(t)
    if not online and not is_off(t - SLOT):                            # 5.3.1 log once, when the outage starts
        events.append(f"{t:%d.%m %H:%M} {site} offline from the cloud backend, local fallback control")

    # 5.4 real faults active at t (started, not cleared by t); whole-cabinet faults change the factor
    fl = data["faults"]
    fl = fl[fl.site_id == site]
    for cab, (cab_site, _) in FAN_CABINETS.items():                    # 5.4.0 a finished repair stops the overheating
        r = _fan_repair(cab) if cab_site == site else None
        if r is not None and r["warned"] <= t:
            fl = fl[~((fl.cabinet_id == cab) & (fl.fault_category == "overtemperature") & (fl.start_ts >= r["done"]))]
    active = fl[(fl.start_ts <= t) & ~(fl.cleared_ts <= t)]           # NaT (still open) counts as active
    charger_factor: dict = {}
    for f in active.itertuples(index=False):
        if isinstance(f.charger_id, str):
            # 5.4.1 one dispenser down: not a cabinet limit, but that dispenser cannot deliver power
            k = CHARGER_FACTOR.get(f.fault_category, 0.0)
            charger_factor[f.charger_id] = min(charger_factor.get(f.charger_id, 1.0), k)
            continue
        k = SEVERITY_FACTOR.get(f.severity, 0.0)
        row = factor.setdefault(f.cabinet_id, [1.0] * H)
        factor[f.cabinet_id] = [min(x, k) for x in row]                # 5.4.2 assume it lasts the whole horizon

    # 5.5 work orders: faults that started during the last slot (t - 15 min, t]
    new = fl[(fl.start_ts > t - SLOT) & (fl.start_ts <= t)]
    for f in new.itertuples(index=False):
        events.append(_dispatch_event(f))

    # 5.6 fan prediction: future slots after the predicted crossing get factor 0
    deadline: dict = {}
    for cab, (cab_site, _) in FAN_CABINETS.items():
        if cab_site != site:
            continue                                                   # 5.6.1 cabinet belongs to another site
        r = _fan_repair(cab)
        if r is not None and r["warned"] <= t:                         # 5.6.1a repair planned: off only while it runs
            events.extend(_repair_events(site, cab, t, r))
            row = factor.setdefault(cab, [1.0] * H)
            factor[cab] = [0.0 if s < r["done"] and s + SLOT > r["arrive"] else x for s, x in zip(slot_start, row)]
            continue
        p = predict_fan_failure(cab, t)
        if p is None:
            continue
        if predict_fan_failure(cab, t - SLOT) is None:                 # 5.6.2 log the warning once, when it first appears
            events.append(f"{t:%d.%m %H:%M} predict {cab} overtemperature at {p['predicted']:%d.%m %H:%M} "
                          f"(fan {p['rpm_now']:.0f} rpm, {p['slope_rpm_h']:.0f} rpm/h): "
                          f"vehicles on {cab} must finish by {p['predicted'] - DEADLINE_LEAD:%H:%M}")
            events.append(_preventive_event(site, cab, t, p["predicted"]))   # 5.6.2a warn the technician now
        row = factor.setdefault(cab, [1.0] * H)
        for j in range(1, H):                                          # 5.6.3 slot 0 = measured state, not a forecast
            if slot_start[j] + SLOT > p["predicted"]:
                row[j] = 0.0

        # 5.6.4 vehicles on this cabinet must be done DEADLINE_LEAD before the predicted failure
        new_slot = max(1, int((p["predicted"] - DEADLINE_LEAD - t) // SLOT))
        for v in vehicles or []:
            if v.get("cabinet") == cab and new_slot < v.get("depart_slot", H):
                deadline[v["id"]] = new_slot

    return {"online": online, "cabinet_factor": factor, "charger_factor": charger_factor,
            "deadline": deadline, "events": events}


# ---------------------------------------------------------------- 6. self-test
if __name__ == "__main__":
    # 6.1 S1 from 12.11 12:00 to 13.11 03:00, one line per slot:
    #     the prediction must appear at 13:30, the slot-0 factor of C1-3 must be 0 only while
    #     the fan is replaced, then stay 1 through the night.  Run:  KEMPOWER_DATA=/path/to/data python health.py
    fake = [{"id": "B10", "cabinet": "C1-3", "depart_slot": 48}]
    for t in pd.date_range("2026-11-12T12:00:00+02:00", "2026-11-13T03:00:00+02:00", freq="15min"):
        h = check("S1", t, fake)
        c13 = h["cabinet_factor"].get("C1-3", [1.0] * H)
        first_zero = next((j for j, x in enumerate(c13) if x == 0.0), None)
        print(f"{t:%d.%m %H:%M} online={h['online']!s:5} C1-3 now={c13[0]:.1f} "
              f"first_zero_slot={first_zero!s:4} deadline={h['deadline']}")
        for e in h["events"]:
            print("    ", e)
