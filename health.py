"""health.py - faults, fan prediction and connectivity (owner: member 4).

Contract 3 (see interfaces.md), called by main.py once per site and 15-minute slot:

    h = check(site, t, vehicles=state["vehicles"])

    h["online"]          bool: can the site reach the cloud backend at t
    h["cabinet_factor"]  {cabinet_id: [48 floats]}, each value 1, 0.5 or 0, slot j = [t + 15j, t + 15j + 15)
    h["deadline"]        {vehicle_id: new depart_slot}, only vehicles that must be done earlier
    h["events"]          [str], human-readable log lines for log.csv and the pitch

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

    # 2.5.4 who can fix what, spare parts, travel times (optional file)
    techs = read("technicians.csv")
    parts = read("spare_parts.csv").set_index("part_id")
    travel_path = os.path.join(d, "travel_times.csv")
    travel = pd.read_csv(travel_path) if os.path.exists(travel_path) else None

    # 2.5.5 cabinets per site, so cabinet_factor lists every cabinet (default 1)
    cab_path = os.path.join(d, "power_cabinets.csv")
    cabinets = pd.read_csv(cab_path)[["cabinet_id", "site_id"]] if os.path.exists(cab_path) else None

    return dict(tel=tel, faults=faults, outages=out, techs=techs, parts=parts, travel=travel, cabinets=cabinets)


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


# ---------------------------------------------------------------- 4. dispatch
def _pick_technician(site: str, whole_cabinet: bool) -> str:
    """4.1 A cabinet fault needs the 'cabinet' skill (TE1 or TE3, not TE2); pick the closest one."""
    data = _load()
    techs = data["techs"]
    if whole_cabinet:
        techs = techs[techs.skills.str.contains("cabinet")]
    if data["travel"] is not None:
        tr = data["travel"][data["travel"].site_id == site].set_index("technician_id").travel_min
        techs = techs.assign(travel=techs.technician_id.map(tr).fillna(math.inf)).sort_values("travel")
    return str(techs.technician_id.iloc[0])


def _part_note(category: str) -> str:
    """4.2 Which spare part, and whether it is on the shelf or must be ordered."""
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
    """4.3 One work order line per fault, written in the round the fault first shows up."""
    whole = not isinstance(f.charger_id, str)
    tech = _pick_technician(f.site_id, whole)
    where = f.cabinet_id if whole else f"{f.cabinet_id}/{f.charger_id}"
    action = "stopped" if f.severity == "critical" else "derated"
    return (f"{f.start_ts:%d.%m %H:%M} {where} {action} ({f.fault_category}, {f.severity}): "
            f"dispatch {tech}, {_part_note(f.fault_category)}")


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
    active = fl[(fl.start_ts <= t) & ~(fl.cleared_ts <= t)]           # NaT (still open) counts as active
    for f in active.itertuples(index=False):
        if isinstance(f.charger_id, str):
            continue                                                   # 5.4.1 one dispenser down: not a cabinet limit
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
        p = predict_fan_failure(cab, t)
        if p is None:
            continue
        if predict_fan_failure(cab, t - SLOT) is None:                 # 5.6.2 log the warning once, when it first appears
            events.append(f"{t:%d.%m %H:%M} predict {cab} overtemperature at {p['predicted']:%d.%m %H:%M} "
                          f"(fan {p['rpm_now']:.0f} rpm, {p['slope_rpm_h']:.0f} rpm/h): "
                          f"vehicles on {cab} must finish by {p['predicted'] - DEADLINE_LEAD:%H:%M}")
        row = factor.setdefault(cab, [1.0] * H)
        for j in range(1, H):                                          # 5.6.3 slot 0 = measured state, not a forecast
            if slot_start[j] + SLOT > p["predicted"]:
                row[j] = 0.0

        # 5.6.4 vehicles on this cabinet must be done DEADLINE_LEAD before the predicted failure
        new_slot = max(1, int((p["predicted"] - DEADLINE_LEAD - t) // SLOT))
        for v in vehicles or []:
            if v.get("cabinet") == cab and new_slot < v.get("depart_slot", H):
                deadline[v["id"]] = new_slot

    return {"online": online, "cabinet_factor": factor, "deadline": deadline, "events": events}


# ---------------------------------------------------------------- 6. self-test
if __name__ == "__main__":
    # 6.1 S1 from 12.11 12:00 to 13.11 03:00, one line per slot:
    #     the prediction must appear before 01:50, the slot-0 factor of C1-3 must be 0.5 from
    #     01:50 and 0 from 02:25.  Run:  KEMPOWER_DATA=/path/to/data python health.py
    fake = [{"id": "B10", "cabinet": "C1-3", "depart_slot": 48}]
    for t in pd.date_range("2026-11-12T12:00:00+02:00", "2026-11-13T03:00:00+02:00", freq="15min"):
        h = check("S1", t, fake)
        c13 = h["cabinet_factor"].get("C1-3", [1.0] * H)
        first_zero = next((j for j, x in enumerate(c13) if x == 0.0), None)
        print(f"{t:%d.%m %H:%M} online={h['online']!s:5} C1-3 now={c13[0]:.1f} "
              f"first_zero_slot={first_zero!s:4} deadline={h['deadline']}")
        for e in h["events"]:
            print("    ", e)
