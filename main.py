import argparse
import numpy as np
import pandas as pd

from config import (
    TZ,
    SIM_START,
    SIM_END,
    SLOT,
    SLOT_HOURS,
    HORIZON,
    SITES,
    OUTPUT_DIR,
    LOG_PATH,
)
import data

# Fall back automatically if modules from teammates are not ready yet
try:
    import mpc as mpc_mod
except Exception:
    mpc_mod = None

try:
    import llf as llf_mod
except Exception:
    llf_mod = None

try:
    import health as health_mod
except Exception:
    health_mod = None


class DummyHealth:
    def check(self, site, t, vehicles=None):
        return {
            "online": True,
            "cabinet_factor": {},
            "deadline": {},
            "events": [],
        }


class DummyPlanner:
    @staticmethod
    def plan(state):
        # 1) Start by giving every vehicle its max_kw
        plan = {v["id"]: float(v["max_kw"]) for v in state["vehicles"]}

        # 2) Scale down per cabinet if the cabinet limit is exceeded
        cab_sum = {}
        for v in state["vehicles"]:
            cab = v["cabinet"]
            cab_sum[cab] = cab_sum.get(cab, 0.0) + plan[v["id"]]

        for cab, s in cab_sum.items():
            if cab not in state["cabinets"]:
                continue
            limit = float(state["cabinets"][cab][0])
            if s > limit and s > 0:
                ratio = limit / s
                for v in state["vehicles"]:
                    if v["cabinet"] == cab:
                        plan[v["id"]] *= ratio

        # 3) Scale down at site level if the site limit is exceeded
        total = sum(plan.values())
        site_limit = float(state["site_limit"][0])
        if total > site_limit and total > 0:
            ratio = site_limit / total
            for vid in plan:
                plan[vid] *= ratio

        return plan


def _factor_list(fac):
    if isinstance(fac, (int, float, np.integer, np.floating)):
        return [float(fac)] * HORIZON
    return [float(x) for x in fac]


def apply_health(state, h):
    """
    Main loop step 3: apply the health dict produced by module 4 to the state.
    """
    # Cabinet factors
    for cab, fac in h.get("cabinet_factor", {}).items():
        cab = str(cab)
        if cab not in state["cabinets"]:
            continue
        facs = _factor_list(fac)
        new_limits = []
        for i, lim in enumerate(state["cabinets"][cab]):
            f = facs[i] if i < len(facs) else 1.0
            new_limits.append(float(lim) * f)
        state["cabinets"][cab] = new_limits

    # Deadlines
    for vid, new_dep in h.get("deadline", {}).items():
        vid = str(vid)
        for v in state["vehicles"]:
            if v["id"] != vid:
                continue

            if isinstance(new_dep, (int, np.integer, float, np.floating)):
                dep_slot = int(new_dep)
            else:
                dep_ts = pd.Timestamp(new_dep)
                if dep_ts.tzinfo is None:
                    dep_ts = dep_ts.tz_localize(TZ)
                else:
                    dep_ts = dep_ts.tz_convert(TZ)

                delta = dep_ts - state["t"]
                if delta.total_seconds() <= 0:
                    dep_slot = 0
                else:
                    dep_slot = int(np.ceil(delta / SLOT))

            v["depart_slot"] = min(HORIZON, max(0, dep_slot))

    return state


def call_planner(state, h):
    """
    Prefer MPC when online; fall back to LLF when offline or MPC fails;
    otherwise use DummyPlanner.
    """
    if h.get("online", True) and mpc_mod is not None:
        try:
            out = mpc_mod.plan(state)
            if out is not None:
                return out, "mpc"
        except Exception:
            pass

    if llf_mod is not None:
        try:
            llf_state = adapt_state_for_llf(state)
            out = llf_mod.run_llf(llf_state) 
            if out is not None:
                return out, "llf"
        except Exception as e:
            print(f"[llf] Conversion or invocation failed: {e}")

    return DummyPlanner.plan(state), "dummy"


def check_limits(state, plan):
    """
    Safety check: per vehicle, per cabinet, per site limits must all hold.
    """
    plan = plan or {}

    # Per vehicle
    for v in state["vehicles"]:
        kw = float(plan.get(v["id"], 0.0))
        if not np.isfinite(kw):
            raise ValueError(
                f"Non-finite power: {state['site']} {state['t']} {v['id']} {kw}"
            )
        if kw < -1e-6:
            raise ValueError(
                f"Negative power: {state['site']} {state['t']} {v['id']} {kw}"
            )
        if kw > v["max_kw"] + 1e-5:
            raise ValueError(
                f"Vehicle limit exceeded: {state['site']} {state['t']} {v['id']} "
                f"{kw:.3f} > {v['max_kw']:.3f}"
            )

    # Per cabinet
    cab_power = {}
    for v in state["vehicles"]:
        kw = float(plan.get(v["id"], 0.0))
        cab = v["cabinet"]
        cab_power[cab] = cab_power.get(cab, 0.0) + kw

    for cab, p in cab_power.items():
        if cab not in state["cabinets"]:
            continue
        limit = float(state["cabinets"][cab][0])
        if p > limit + 1e-5:
            raise ValueError(
                f"Cabinet limit exceeded: {state['site']} {state['t']} {cab} "
                f"{p:.3f} > {limit:.3f}"
            )

    # Per site
    total = sum(float(plan.get(v["id"], 0.0)) for v in state["vehicles"])
    site_limit = float(state["site_limit"][0])
    if total > site_limit + 1e-5:
        raise ValueError(
            f"Site limit exceeded: {state['site']} {state['t']} "
            f"{total:.3f} > {site_limit:.3f}"
        )

def safe_check_health(site, t, state, dummy_health):
    if health_mod is not None:
        try:
            return health_mod.check(site, t, vehicles=state["vehicles"])
        except TypeError:
            try:
                return health_mod.check(site, t)
            except Exception as e:
                print(f"[health] back to dummy: {e}")
        except Exception as e:
            print(f"[health] back to dummy: {e}")
    return dummy_health.check(site, t, vehicles=state["vehicles"])

def simulate(force_dummy_planner=False):
    OUTPUT_DIR.mkdir(exist_ok=True)
    data.reset_soc()

    rows = []
    dummy_health = DummyHealth()

    for site in SITES:
        t = SIM_START

        while t < SIM_END:
            state = data.get_state(site, t)

            h = safe_check_health(site, t, state, dummy_health)

            state = apply_health(state, h)

            if force_dummy_planner:
                plan = DummyPlanner.plan(state)
                algo = "dummy"
            else:
                plan, algo = call_planner(state, h)

            check_limits(state, plan)

            ev = h.get("events", [])
            events_text = ev if isinstance(ev, str) else " | ".join(str(x) for x in ev)

            # Write charge rows and update SoC
            for v in state["vehicles"]:
                kw = float(plan.get(v["id"], 0.0))
                soc_before = float(v["soc"])

                energy_kwh = kw * SLOT_HOURS
                delta_soc = energy_kwh / v["battery_kwh"] * 100.0
                soc_after = min(100.0, soc_before + delta_soc)

                data.update_soc(site, v["session_id"], soc_after)

                cab_limit = None
                if v["cabinet"] in state["cabinets"]:
                    cab_limit = float(state["cabinets"][v["cabinet"]][0])

                rows.append({
                    "row_type": "charge",
                    "t": t.isoformat(),
                    "site": site,
                    "vehicle_id": v["id"],
                    "session_id": v["session_id"],
                    "charger_id": v["charger"],
                    "cabinet_id": v["cabinet"],
                    "kw": kw,
                    "soc_before": soc_before,
                    "soc_after": soc_after,
                    "required_soc_pct": v["required_soc_pct"],
                    "below_required": "",
                    "price_eur_kwh": float(state["price"][0]),
                    "site_limit_kw": float(state["site_limit"][0]),
                    "cabinet_limit_kw": cab_limit,
                    "planner": algo,
                    "events": events_text,
                    "depart_ts": "" if pd.isna(v["depart_ts"]) else v["depart_ts"].isoformat(),
                })

            # Departure rows: add one extra row for vehicles departing in this slot
            for v in state["vehicles"]:
                dep_ts = v["depart_ts"]
                if pd.isna(dep_ts):
                    continue
                if t < dep_ts <= t + SLOT:
                    soc_after = data.get_soc(site, v["session_id"])
                    if soc_after is None:
                        soc_after = v["soc"]
                    below = int(soc_after + 1e-9 < v["required_soc_pct"])

                    rows.append({
                        "row_type": "departure",
                        "t": t.isoformat(),
                        "site": site,
                        "vehicle_id": v["id"],
                        "session_id": v["session_id"],
                        "charger_id": v["charger"],
                        "cabinet_id": v["cabinet"],
                        "kw": 0.0,
                        "soc_before": "",
                        "soc_after": soc_after,
                        "required_soc_pct": v["required_soc_pct"],
                        "below_required": below,
                        "price_eur_kwh": float(state["price"][0]),
                        "site_limit_kw": float(state["site_limit"][0]),
                        "cabinet_limit_kw": None,
                        "planner": algo,
                        "events": events_text,
                        "depart_ts": dep_ts.isoformat(),
                    })

            t += SLOT

    df = pd.DataFrame(rows)
    df.to_csv(LOG_PATH, index=False)

    print(f"written: {LOG_PATH}")
    print(f"rows: {len(df)}")
    if len(df):
        print(df["planner"].value_counts().to_string())

def adapt_state_for_llf(state):
    """把 1 号的 state 转成 3 号 llf.run_llf 需要的格式。"""
    vehicles_dict = {}
    for v in state["vehicles"]:
        depart_ts = v["depart_ts"]
        if pd.isna(depart_ts):
            deadline_str = "2099-12-31T23:59:59+02:00"
        else:
            deadline_str = depart_ts.isoformat()

        # 如果 4 号改过 depart_slot，就用新的
        slot_deadline_ts = state["t"] + v["depart_slot"] * SLOT
        effective = slot_deadline_ts.isoformat()

        vehicles_dict[v["id"]] = {
            "cabinet_id": v["cabinet"],
            "max_kw": v["max_kw"],
            "remaining_kwh": v["need_kwh"],
            "departure": deadline_str,
            "effective_deadline": effective,
        }

    cabinets_dict = {}
    for cab, limits in state["cabinets"].items():
        cabinets_dict[cab] = {"available_kw": float(limits[0])}

    return {
        "time": state["t"].isoformat(),
        "step_hours": SLOT_HOURS,
        "site_limit_kw": float(state["site_limit"][0]),
        "cabinets": cabinets_dict,
        "vehicles": vehicles_dict,
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dummy-planner",
        action="store_true",
        help="Force the dummy planner (every vehicle at max_kw) to smoke-test the main loop",
    )
    args = parser.parse_args()
    simulate(force_dummy_planner=args.dummy_planner)


if __name__ == "__main__":
    main()