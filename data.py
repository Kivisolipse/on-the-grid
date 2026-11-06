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

# Current SoC, key = (site, visit_id). A visit is one fleet_schedule row, so SoC
# carries over between plug-ins and starts fresh when the vehicle comes back.
SOC = {}


def reset_soc():
    global SOC
    SOC = {}


def get_soc(site, visit_id):
    return SOC.get((site, str(visit_id)), None)


def update_soc(site, visit_id, new_soc):
    SOC[(site, str(visit_id))] = float(max(0.0, min(100.0, new_soc)))


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


def _visits_on_site(site, t):
    """
    fleet_schedule rows with the vehicle on site at time t:
    arrive_ts <= t and (depart_ts is empty or depart_ts > t).
    """
    fs = _load("fleet_schedule.csv", ["arrive_ts", "depart_ts"])
    return fs[
        (fs["site_id"] == site)
        & (fs["arrive_ts"] <= t)
        & (fs["depart_ts"].isna() | (fs["depart_ts"] > t))
    ]


def _depart_slot(depart_ts, t):
    if pd.isna(depart_ts):
        return HORIZON
    delta = depart_ts - t
    if delta.total_seconds() <= 0:
        return 0
    return min(HORIZON, int(np.ceil(delta / SLOT)))


def _tapered(max_kw, soc):
    """Taper above 80% SoC."""
    if soc > 80.0:
        return max_kw * max(0.0, (100.0 - soc) / 20.0)
    return max_kw


def get_state(site, t, use_sessions=True):
    """
    Build the state dict defined by Convention 1.

    Presence comes from fleet_schedule.csv; sessions_baseline.csv only says who
    is plugged in. "vehicles" holds the plugged-in vehicles the planners can
    charge. "parked" holds the others on site: same keys, no charger, max_kw 0.

    use_sessions=False is the input for relay.apply: "vehicles" holds everyone on
    site with no charger yet, and max_kw is the vehicle's own limit.
    """
    t = _as_tz(t)

    price = _price_for_site(site, t)
    site_limit = _site_limits(site, t)
    cabinets = _cabinet_limits(site)

    sess = _load("sessions_baseline.csv", ["start_ts", "end_ts"])
    plugged = sess[
        (sess["site_id"] == site)
        & (sess["start_ts"] <= t)
        & (sess["end_ts"] > t)
    ].set_index("vehicle_id")

    vehicles = []
    parked = []

    for _, visit in _visits_on_site(site, t).iterrows():
        vid = str(visit["vehicle_id"])
        visit_id = f"{vid}@{visit['arrive_ts'].isoformat()}"
        veh = _vehicle_row(vid)

        key = (site, visit_id)
        if key not in SOC:
            SOC[key] = float(visit["arrive_soc_pct"])

        soc = float(SOC[key])
        battery_kwh = float(veh["battery_kwh"])
        required_soc_pct = float(visit["required_soc_pct"])
        depart_ts = visit["depart_ts"]

        need_kwh = max(
            0.0,
            (required_soc_pct - soc) / 100.0 * battery_kwh,
        )

        v = {
            "id": vid,
            "visit_id": visit_id,
            "session_id": None,
            "charger": None,
            "cabinet": None,
            "soc": soc,
            "battery_kwh": battery_kwh,
            "max_kw": 0.0,
            "need_kwh": need_kwh,
            "depart_slot": _depart_slot(depart_ts, t),
            "depart_ts": depart_ts,
            "required_soc_pct": required_soc_pct,
        }

        if not use_sessions:
            v["max_kw"] = _tapered(float(veh["max_charge_kw"]), soc)
            vehicles.append(v)
            continue

        if vid not in plugged.index:
            parked.append(v)
            continue

        s = plugged.loc[vid]
        ch = _charger_row(str(s["charger_id"]))

        charger_max = float(ch["max_kw"])
        vehicle_max = float(veh["max_charge_kw"])

        v["session_id"] = str(s["session_id"])
        v["charger"] = str(s["charger_id"])
        v["cabinet"] = str(ch["cabinet_id"])
        v["max_kw"] = _tapered(min(charger_max, vehicle_max), soc)
        vehicles.append(v)

    return {
        "t": t,
        "site": site,
        "price": price,
        "site_limit": site_limit,
        "cabinets": cabinets,
        "vehicles": vehicles,
        "parked": parked,
    }
