import numpy as np
import pandas as pd

from config import (
    DATA_DIR,
    TZ,
    HORIZON,
    SLOT,
    SLOT_HOURS,
)

# Cache for CSV files
_CACHE = {}

# Current SoC, key = (site, session_id)
SOC = {}


def reset_soc():
    global SOC
    SOC = {}


def get_soc(site, session_id):
    return SOC.get((site, str(session_id)), None)


def update_soc(site, session_id, new_soc):
    SOC[(site, str(session_id))] = float(max(0.0, min(100.0, new_soc)))


def _load(name, time_cols=()):
    key = (name, tuple(time_cols))
    if key not in _CACHE:
        path = DATA_DIR / name
        df = pd.read_csv(path)
        for c in time_cols:
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], utc=True, errors="coerce").dt.tz_convert(TZ)
        _CACHE[key] = df
    return _CACHE[key]


def _load_optional(name, time_cols=()):
    try:
        return _load(name, time_cols)
    except FileNotFoundError:
        return pd.DataFrame()


def _as_tz(t):
    t = pd.Timestamp(t)
    if t.tzinfo is None:
        return t.tz_localize(TZ)
    return t.tz_convert(TZ)


def _slot_list(t):
    return [t + i * SLOT for i in range(HORIZON)]


def _price_for_site(site, t):
    """
    Price = (spot + grid_tariff_depot_eur_mwh) / 1000, unit: EUR/kWh.
    Publication rule: the price for day D is published at 14:00 on D-1.
    Any slot that is not published yet is filled with the last known price.
    """
    df = _load("prices.csv", ["ts"]).copy()
    if df.empty:
        return [0.0] * HORIZON

    df = df.dropna(subset=["ts"]).sort_values("ts").drop_duplicates("ts", keep="last")

    spot = pd.to_numeric(df["spot_eur_mwh"], errors="coerce").fillna(0.0)
    if "grid_tariff_depot_eur_mwh" in df.columns:
        tariff = pd.to_numeric(df["grid_tariff_depot_eur_mwh"], errors="coerce").fillna(0.0)
    else:
        tariff = pd.Series(0.0, index=df.index)

    df["price_eur_kwh"] = (spot + tariff) / 1000.0
    df["pub_ts"] = (
        df["ts"].dt.normalize()
        - pd.Timedelta(days=1)
        + pd.Timedelta(hours=14)
    )

    known = df[df["pub_ts"] <= t]
    if known.empty:
        last_price = float(df["price_eur_kwh"].iloc[0])
    else:
        known_before = known[known["ts"] <= t]
        if not known_before.empty:
            last_price = float(known_before.sort_values("ts")["price_eur_kwh"].iloc[-1])
        else:
            last_price = float(known.sort_values("ts")["price_eur_kwh"].iloc[-1])

    actual = dict(zip(df["ts"], df["price_eur_kwh"]))
    pub = dict(zip(df["ts"], df["pub_ts"]))

    out = []
    for ts in _slot_list(t):
        if ts in actual and pub[ts] <= t:
            out.append(float(actual[ts]))
        else:
            out.append(last_price)
    return out


def _site_limits(site, t):
    """
    Default from sites.csv: grid_limit_kw.
    Events in grid_events.csv only take effect after notified_ts and only
    apply within the event window.
    """
    sites = _load("sites.csv")
    row = sites[sites["site_id"] == site]
    if row.empty:
        default_limit = 0.0
    else:
        default_limit = float(row.iloc[0]["grid_limit_kw"])

    limits = [default_limit] * HORIZON
    ge = _load_optional("grid_events.csv", ["notified_ts", "start_ts", "end_ts"])
    if ge.empty:
        return limits

    ge = ge[ge["site_id"] == site]
    slots = _slot_list(t)

    for _, e in ge.iterrows():
        if pd.isna(e.get("notified_ts")):
            continue
        if t < e["notified_ts"]:
            continue
        if pd.isna(e.get("limit_kw")):
            continue

        lim = float(e["limit_kw"])
        start_ts = e.get("start_ts")
        end_ts = e.get("end_ts")

        for i, ts in enumerate(slots):
            if pd.notna(start_ts) and pd.notna(end_ts):
                if start_ts <= ts < end_ts:
                    limits[i] = min(limits[i], lim)
            else:
                # If no explicit window is given, apply from start_ts onwards
                if pd.notna(start_ts) and ts >= start_ts:
                    limits[i] = min(limits[i], lim)

    return limits


def _cabinet_limits(site):
    """
    Cabinet limits, length 48. Derating is applied by module 4 health
    via cabinet_factor inside main.
    """
    out = {}

    pc = _load_optional("power_cabinets.csv", ["commissioned_date"])
    if not pc.empty and "site_id" in pc.columns:
        sub = pc[pc["site_id"] == site]
        for _, r in sub.iterrows():
            out[str(r["cabinet_id"])] = [float(r["max_kw"])] * HORIZON
        if out:
            return out

    # Fallback: if power_cabinets.csv is missing, sum the rated power of
    # all dispensers under that cabinet
    ch = _load("chargers.csv", ["commissioned_date"])
    sub = ch[ch["site_id"] == site]
    grp = sub.groupby("cabinet_id")["max_kw"].sum()
    for cab, val in grp.items():
        out[str(cab)] = [float(val)] * HORIZON
    return out


def _charger_row(charger_id):
    ch = _load("chargers.csv", ["commissioned_date"])
    row = ch[ch["charger_id"] == charger_id]
    if row.empty:
        return None
    return row.iloc[0]


def _vehicle_row(vehicle_id):
    veh = _load("vehicles.csv")
    row = veh[veh["vehicle_id"] == vehicle_id]
    if row.empty:
        return None
    return row.iloc[0]


def _find_schedule(vehicle_id, site, t):
    """
    Find the fleet_schedule row that is active at time t:
    arrive_ts <= t and (depart_ts is empty or depart_ts > t).
    If multiple rows match, take the one with the latest arrive_ts.
    """
    fs = _load("fleet_schedule.csv", ["arrive_ts", "depart_ts"])
    sub = fs[
        (fs["vehicle_id"] == vehicle_id)
        & (fs["site_id"] == site)
        & (fs["arrive_ts"] <= t)
    ]
    if sub.empty:
        return None

    active = sub[
        sub["depart_ts"].isna()
        | (sub["depart_ts"] > t)
    ]
    if active.empty:
        return None

    return active.sort_values("arrive_ts").iloc[-1]


def _depart_slot(depart_ts, t):
    if pd.isna(depart_ts):
        return HORIZON
    delta = depart_ts - t
    if delta.total_seconds() <= 0:
        return 0
    return min(HORIZON, int(np.ceil(delta / SLOT)))


def get_state(site, t):
    """
    Build the state dict defined by Convention 1.
    """
    t = _as_tz(t)

    price = _price_for_site(site, t)
    site_limit = _site_limits(site, t)
    cabinets = _cabinet_limits(site)

    sess = _load("sessions_baseline.csv", ["start_ts", "end_ts"])
    active = sess[
        (sess["site_id"] == site)
        & (sess["start_ts"] <= t)
        & (sess["end_ts"] > t)
    ]

    vehicles = []

    for _, s in active.iterrows():
        vid = s.get("vehicle_id")
        if pd.isna(vid) or str(vid) == "":
            continue

        vid = str(vid)
        sid = str(s["session_id"])
        charger_id = str(s["charger_id"])

        ch = _charger_row(charger_id)
        if ch is None:
            continue

        veh = _vehicle_row(vid)
        if veh is None:
            continue

        sched = _find_schedule(vid, site, t)

        key = (site, sid)
        if key not in SOC:
            if sched is not None and not pd.isna(sched.get("arrive_soc_pct")):
                SOC[key] = float(sched["arrive_soc_pct"])
            else:
                SOC[key] = 100.0

        soc = float(SOC[key])
        battery_kwh = float(veh["battery_kwh"])

        if sched is not None and not pd.isna(sched.get("required_soc_pct")):
            required_soc_pct = float(sched["required_soc_pct"])
        else:
            required_soc_pct = soc

        depart_ts = sched.get("depart_ts") if sched is not None else pd.NaT
        depart_slot = _depart_slot(depart_ts, t)

        charger_max = float(ch["max_kw"])
        vehicle_max = float(veh["max_charge_kw"])
        max_kw = min(charger_max, vehicle_max)

        # Taper above 80% SoC
        if soc > 80.0:
            max_kw = max_kw * max(0.0, (100.0 - soc) / 20.0)
        max_kw = max(0.0, max_kw)

        need_kwh = max(
            0.0,
            (required_soc_pct - soc) / 100.0 * battery_kwh,
        )

        vehicles.append({
            "id": vid,
            "session_id": sid,
            "charger": charger_id,
            "cabinet": str(ch["cabinet_id"]),
            "soc": soc,
            "battery_kwh": battery_kwh,
            "max_kw": max_kw,
            "need_kwh": need_kwh,
            "depart_slot": depart_slot,
            "depart_ts": depart_ts,
            "required_soc_pct": required_soc_pct,
        })

    return {
        "t": t,
        "site": site,
        "price": price,
        "site_limit": site_limit,
        "cabinets": cabinets,
        "vehicles": vehicles,
    }