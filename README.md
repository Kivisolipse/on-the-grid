# On the Grid

On the Grid is an autonomous charging loop for EV depots, built for the 2026 Kempower LahtiES Fuusio Energy Hackathon.
It runs bus depot S1 and logistics depot S2 through the scenario day of Friday 13.11.2026 with no person deciding how the vehicles charge.
In our simulation run, no vehicle leaves under-charged (8 in the baseline), the electricity bill falls 45 % from €1,357 to €744, and no grid limit is ever broken.

## How it works

Every 15 minutes, `main.py` runs one loop per depot: build state, assign plugs, check health, plan, pass the safety gate, log.
`data.py` uses only what is known at that moment, so spot prices appear after their 14:00 publication and grid caps after their notice.
`relay.py` picks each vehicle's charger and books night staff visits, and `health.py` turns faults and fan telemetry into capacity limits and preventive work orders.
`mpc.py` solves a linear program over a 12 h horizon with cvxpy and applies only the first slot.
When the site is offline or MPC finds no plan, `llf.py` plans locally with Least Laxity First and needs no solver.
Every plan from any planner must pass `check_limits`, which stops the run before a vehicle, cabinet or site limit can break.
The scenario day packs a cabinet fan failure, a €799/MWh price spike, an evening grid cap at S2 and a 2 h cloud outage at S1 into one run, and the fan forecast warns 12 h before the recorded failure so the repair moves to a day shift.

## Run it

```bash
pip install -r requirements.txt
python main.py            # output/log.csv
python report_kpi.py      # KPIs against data/baseline_kpis.csv
python report_savings.py  # savings.html for the pitch
```

These three commands rebuild every figure above from the synthetic dataset in `data/`.
`test_llf.py` covers the LLF scheduler in 8 cases, and `test_presence.py` pins the scenario day's outcomes such as zero under-charged departures and the preventive repair timing.
Module notes for the LLF scheduler and KPI checks are in [README_MEMBER3.md](README_MEMBER3.md).
