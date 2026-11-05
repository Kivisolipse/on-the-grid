
from datetime import datetime


def run_llf(state):
    current_time = datetime.fromisoformat(state["time"])
    step_hours = state.get("step_hours", 0.25)

    site_remaining = max(0.0, state["site_limit_kw"])

    cabinet_remaining = {
        cabinet_id: max(0.0, cabinet["available_kw"])
        for cabinet_id, cabinet in state["cabinets"].items()
    }

    allocation = {}
    candidates = []

    for vehicle_id, vehicle in state["vehicles"].items():
        allocation[vehicle_id] = 0.0

        remaining = max(0.0, vehicle["remaining_kwh"])
        max_kw = max(0.0, vehicle["max_kw"])

        cabinet_id = vehicle["cabinet_id"]

        if remaining <= 0 or max_kw <= 0:
            continue

        if cabinet_id not in cabinet_remaining:
            continue

        deadline = datetime.fromisoformat(
            vehicle.get("effective_deadline")
            or vehicle["departure"]
        )

        hours_left = max(
            0.0,
            (deadline - current_time).total_seconds() / 3600
        )

        if hours_left <= 0:
            continue

        laxity = hours_left - remaining / max_kw

        candidates.append(
            (laxity, deadline, vehicle_id, max_kw,
             remaining, cabinet_id)
        )

    candidates.sort(key=lambda x: (x[0], x[1], x[2]))

    for laxity, deadline, vehicle_id, max_kw, remaining, cabinet_id in candidates:

        max_needed_kw = remaining / step_hours

        power = min(
            max_kw,
            max_needed_kw,
            cabinet_remaining[cabinet_id],
            site_remaining
        )

        power = max(0.0, power)

        allocation[vehicle_id] = power

        cabinet_remaining[cabinet_id] -= power
        site_remaining -= power

    return allocation
