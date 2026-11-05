# Member 3 – LLF Fallback Scheduler & KPI Reporting

## 1. Overview

This module is responsible for two tasks in the Kempower EV Charging Network project:

1. **LLF Fallback Scheduling:** Provide a charging power allocation algorithm when the network is offline or the MPC solver fails.
2. **KPI Reporting:** Evaluate charging performance using simulation logs and compare the results with the baseline.

## 2. LLF Algorithm

The scheduler uses the **Least-Laxity-First (LLF)** algorithm.

Laxity is calculated as:

`Laxity = Time until deadline − Remaining energy / Maximum charging power`

Vehicles with lower laxity values receive higher charging priority.

Charging power is then allocated using a greedy approach while respecting:

- Vehicle maximum charging power
- Cabinet available power
- Site power limit
- Remaining charging demand

The algorithm is implemented in Python without external optimization libraries.

## 3. Function Interface

**File:** `llf.py`

**Function:**

```python
from llf import run_llf

allocation = run_llf(state)
```

**Required input:**

The `state` dictionary contains:

| Field | Description |
|---|---|
| `time` | Current simulation timestamp |
| `step_hours` | Simulation interval in hours (0.25) |
| `site_limit_kw` | Available site power |
| `cabinets` | Cabinet IDs and available power |
| `vehicles` | Active vehicles and charging requirements |

Each vehicle must provide:

- `cabinet_id`
- `remaining_kwh`
- `max_kw`
- `departure`
- `effective_deadline` (optional)

**Output:**

```python
{
    "V001": 100.0,
    "V002": 50.0,
    "V003": 0.0
}
```

The output represents allocated charging power in kW for each vehicle during the next 15-minute interval.

## 4. Integration with Other Members

**Member 1 – Main Simulation**

Calls `run_llf(state)` when the network is offline or MPC fails. Member 1 is responsible for validating allocations, updating vehicle SoC, and recording simulation logs.

**Member 2 – MPC Optimization**

MPC and LLF use the same input and output format. MPC returns `None` when optimization fails.

**Member 4 – Fault Management**

Provides updated cabinet power availability and adjusted charging deadlines through the shared state. LLF uses these values during scheduling.

## 5. KPI Reporting

**File:** `report_kpi_checked.py`

The reporting module calculates three KPIs for November 13, 2026:

| KPI | Description |
|---|---|
| Unmet Departures | Number of vehicles departing below required SoC |
| Electricity Cost (€) | Total charging electricity cost |
| Peak Power (kW) | Maximum site charging power |

The report uses simulation logs, departure results, and baseline data.

A valid performance comparison requires the same site, date, and comparable charging workloads.

## 6. Testing

The LLF scheduler passed eight unit tests covering:

- Charging priority
- Site power limits
- Cabinet power limits
- Cabinet shutdown
- Completed charging demand
- Adjusted deadlines
- Energy allocation limits
- Negative site power protection

Run the tests:

```bash
python -m unittest test_llf.py -v
```

## 7. Current Status

**Completed:**
- LLF algorithm implementation
- Eight unit tests
- Initial integration with a CSV-based simulation
- Initial KPI reporting script

**Pending:**
- Integration with the final MPC and fault management modules
- Complete simulation using comparable workloads
- Final validated KPI comparison

## 8. Dependencies

- Python 3
- Python standard library for LLF scheduling and testing

No additional packages are required for the LLF algorithm.
