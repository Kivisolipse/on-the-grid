import cvxpy as cp
import numpy as np

H = 48
DT = 0.25
SHORT_PENALTY = 10
EARLY_WEIGHT = 1e-5


def plan_full(state):
    vehicles = state["vehicles"]
    N = len(vehicles)
    if N == 0:
        return np.zeros((0, H))

    try:
        price = np.array(state["price"], dtype=float)
        site_limit = np.array(state["site_limit"], dtype=float)

        p = cp.Variable((N, H), nonneg=True)  
        short = cp.Variable(N, nonneg=True) 
        constraints = []

        for i in range(N):
            car = vehicles[i]

            constraints.append(p[i, :] <= car["max_kw"])

            if car["depart_slot"] < H:
                constraints.append(p[i, car["depart_slot"]:] == 0)


            constraints.append(cp.sum(p[i, :]) * DT + short[i] >= car["need_kwh"])

            room_kwh = (100 - car["soc"]) / 100 * car["battery_kwh"]
            constraints.append(cp.sum(p[i, :]) * DT <= room_kwh)

        constraints.append(cp.sum(p, axis=0) <= site_limit)

        for cabinet_id, limit in state["cabinets"].items():
            rows = [i for i in range(N) if vehicles[i]["cabinet"] == cabinet_id]
            if len(rows) == 0:    
                continue
            constraints.append(cp.sum(p[rows, :], axis=0) <= np.array(limit, dtype=float))

        energy_cost = price @ cp.sum(p, axis=0) * DT
        short_cost = SHORT_PENALTY * cp.sum(short)
        late_cost = EARLY_WEIGHT * (np.arange(H) @ cp.sum(p, axis=0)) * DT
        problem = cp.Problem(cp.Minimize(energy_cost + short_cost + late_cost), constraints)
        problem.solve()

        if problem.status not in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE):
            return None
        return p.value
    except Exception:
        return None


def plan(state):
    vehicles = state["vehicles"]
    if len(vehicles) == 0:
        return {}

    full = plan_full(state)
    if full is None:
        return None

    result = {}
    for i in range(len(vehicles)):
        result[vehicles[i]["id"]] = max(0.0, float(full[i, 0]))
    return result