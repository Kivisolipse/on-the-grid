# Data dictionary

All data is generated and fictional; see "About this data" in README.md.

Files starting with `network_` and the energy, growth and investment files cover all 100 sites. The other files are close-up detail for sites S1–S5.

Units: kW = power, kWh = energy, MWh = 1 000 kWh, SoC = state of charge in % of battery capacity. `ts` = timestamp, Finnish time (UTC+2). A 15-minute row covers the 15 minutes starting at `ts`.

## A. Energy and control

### sites.csv — 1 row per site
| Column | Meaning |
|---|---|
| site_id | S1–S5 |
| name, type | `private_depot` or `public_hub` |
| grid_limit_kw | Maximum power the site may draw from the grid (connection agreement) |
| tariff_id | Grid tariff, see `tariffs.csv` |
| pv_kwp | Solar panel peak power on site (0 = none) |
| latitude, longitude | Fictional; empty for the close-up sites S1–S5 |
| connectivity_quality | `good` or `weak` mobile coverage |
| in_operation_from | First day the site is in use |

### power_cabinets.csv — 1 row per power cabinet
`cabinet_id`, `site_id`, `max_kw` (total power the cabinet can deliver, shared by its dispensers), `commissioned_date`.

### chargers.csv — 1 row per dispenser (the post a vehicle plugs into)
`charger_id`, `cabinet_id`, `site_id`, `max_kw` (rated maximum for one vehicle), `connector_type`, `commissioned_date`.

### vehicles.csv — 1 row per fleet vehicle
| Column | Meaning |
|---|---|
| vehicle_id | B01–B36 buses, T01–T14 trucks |
| fleet_id, customer_id | See `contracts.csv` |
| home_site_id | Depot where it charges |
| vehicle_type | `city_bus` or `truck` |
| battery_kwh | Usable battery capacity |
| max_charge_kw | Maximum power the vehicle accepts. Above 80 % SoC vehicles accept less (taper). |
| consumption_kwh_per_km | Typical consumption in normal weather |

### fleet_schedule.csv — 1 row per depot visit
| Column | Meaning |
|---|---|
| vehicle_id, site_id | |
| arrive_ts, arrive_soc_pct | When the vehicle arrives and how full it is |
| depart_ts | Planned departure. Empty = after the data period. |
| required_soc_pct | SoC needed at departure for the next route |
| next_route_km | Length of the next route |

### prices.csv — 15 min
`spot_eur_mwh`: day-ahead electricity price (synthetic, shaped like FI prices; can be negative). `grid_tariff_depot_eur_mwh`, `grid_tariff_public_eur_mwh`: energy part of the grid tariff for each tariff. Energy cost = (spot + grid tariff) × energy.

### tariffs.csv
Day and night energy price, day hours, and `peak_power_fee_eur_kw_month`: monthly fee per kW of the highest 15-minute average power.

### pv_production.csv — 15 min
`site_id`, `pv_kw`: solar production at S4. November in Finland: not much.

### grid_events.csv
Requests or outages from the grid company (DSO). `notified_ts` = when the operator was told. `type`: `limit_cap` (site must stay below `limit_kw`) or `outage` (no power).

### connectivity_outages.csv
`site_id`, `start_ts`, `end_ts`: the site had no connection to the cloud backend. Chargers keep working with a fallback limit (`offline_fallback_kw_per_dispenser` in `site_config.csv`), and the backend sees nothing until the connection returns.

### sessions_baseline.csv — 1 row per charging session, as operated today
| Column | Meaning |
|---|---|
| session_id, charger_id, site_id | |
| vehicle_id | Fleet vehicle (depots) |
| anon_user_id | Anonymous public driver (hubs) |
| start_ts, end_ts | Plug-in and unplug time |
| energy_kwh, max_kw | Energy delivered and highest power during the session |
| end_reason | `departed`, `unplugged_by_operator` (night staff moved the vehicle), `completed` (driver left when done), `charger_fault`, `period_end` |

### departures_baseline.csv — 1 row per fleet departure, as operated today
`depart_soc_pct` vs `required_soc_pct`; `below_required` = 1 if the vehicle left with too little energy.

### site_power_baseline.csv — 15 min
`power_kw`: total grid power per site, as operated today.

### baseline_kpis.csv
KPIs per site for `all_14_days` and `scenario_day` (Fri 13.11.). This is the score to beat.

## B. Operations and service

### telemetry.csv — 15 min per dispenser
| Column | Meaning |
|---|---|
| status | `available`, `charging`, `occupied` (vehicle plugged in, not charging), `faulted`, `offline` (never connected) |
| power_kw | Average power in the 15 minutes |
| cabinet_temp_c | Temperature inside the power cabinet |
| error_count | Number of error events logged in the 15 minutes |
| connectivity | 1 = connected to the backend, 0 = not (values logged locally, uploaded later) |

### telemetry_1min.csv
1-minute detail for a few dispensers for the 24 hours before selected events. `error_events` per minute; `fan_rpm` is the cabinet cooling fan speed where available.

### faults.csv — 1 row per fault
| Column | Meaning |
|---|---|
| cabinet_id, charger_id | Empty `charger_id` = the whole cabinet is affected |
| fault_category | `communication`, `payment`, `connector`, `overtemperature`, `power_module`, `ground_fault` |
| severity | `major`, `critical` |
| symptom | What the operator saw |
| cleared_ts, cleared_by | `self`, `remote_reset`, `technician`, `superseded` (replaced by a later fault), `open` (still open at the end of the data) |

### service_tickets.csv — how humans handled each fault today
Opened, remote reset tried, technician dispatched and arrived, part used, closed, `cost_eur` (labour + part).

### technicians.csv, travel_times.csv, spare_parts.csv
Who can fix what and when (`shift`), travel time from base to each site, spare parts with stock and lead time.

## C. Commissioning

### commissioning_steps.csv — S5, step by step
Planned vs actual dates, `waited_days` (time spent waiting for someone else), `status`, `blocker`, `note`.

### commissioning_history.csv — 12 earlier sites
Per step: `planned_days`, `actual_days`, `waited_days`, `rework` (1 = had to be redone), `blocker_category`.

### site_config.csv
Configuration of every site and dispenser as it is today (`configured_value`). Compare with `sites.csv` and `chargers.csv`.

## D. Traffic and public charging

### traffic_counts.csv — 15 min per counting point
`cars`, `vans`, `trucks` passing the point (synthetic, shaped like Fintraffic TMS counts). `traffic_points.csv` says which site each point is next to.

### public_arrivals.csv — 1 row per driver arriving at S3 or S4
| Column | Meaning |
|---|---|
| vehicle_class | `car`, `van`, `taxi`, `truck` |
| arrive_soc_pct, energy_wanted_kwh | |
| max_wait_min | How long the driver is willing to wait for a free dispenser |
| outcome | `charged`, `left_queue` (waited too long), `left_full` (queue too long, drove on), `left_after_interruption`, `period_end` |
| energy_delivered_kwh | |

## E. Commercial

### contracts.csv — 1 row per customer
`contract_type`, `uptime_sla_pct` (promised charger availability), `response_time_h` (promised time to react to a fault), `penalty_eur_per_late_departure`, `annual_fee_eur`. Customers are fictional. C1–C3 belong to the close-up sites, C04 onwards to depots in the network.

## Network level (all 100 sites)

### network_sites.csv — 1 row per site
| Column | Meaning |
|---|---|
| site_id | S1–S5 (close-up) or country code + number, e.g. `SE-07` |
| country, zone | Country and electricity bidding zone (see `zone_prices.csv`) |
| location | Nearest city (also the key to `weather.csv`) |
| type | `bus_depot`, `logistics_depot`, `highway_hub`, `city_hub`, `retail_destination`, `port_truck_depot` |
| dispensers, dispenser_kw | Number and rated power of normal dispensers |
| megawatt_dispensers | Dispensers rated 1 000 kW, for trucks |
| grid_limit_kw | Grid connection limit |
| connection_type | `firm`, or `flexible` (the grid company can lower the limit, see `network_grid_events.csv`) |
| bess_kwh, bess_kw | Battery storage capacity and power (0 = none) |
| pv_kwp | Solar panels |
| public_price_eur_kwh | Price drivers pay at public sites |
| customer_id | Depot owner, see `contracts.csv` |
| detail | `dispenser-level files` for S1–S5, otherwise `site level` |

### network_site_power.csv — 15 min per site
| Column | Meaning |
|---|---|
| demand_kw | Power the plugged-in vehicles asked for |
| delivered_kw | Power actually delivered to vehicles |
| grid_kw | Power taken from the grid |
| grid_limit_kw | Limit in force in that slot (lower during curtailment) |
| pv_kw | Solar production |
| bess_kw | Battery: positive = discharging to the site, negative = charging |
| bess_soc_pct | Battery state of charge (empty = no battery) |
| queue_len | Vehicles waiting for a dispenser at the end of the slot |
| arrivals, abandoned | Vehicles arriving; drivers who gave up (queue too long or waited too long) |
| dispensers_available | Dispensers not faulted |

### network_kpis.csv — 1 row per site (not S1–S5, see `baseline_kpis.csv`)
Two-week totals: energy, arrivals, abandoned, departures and departures with too little energy (depots), peak grid power vs limit, hours when demand was above the grid limit, dispenser availability.

### network_faults_daily.csv
Faults per site, day and category (`communication`, `hardware`, `other`) with total downtime in minutes.

### firmware.csv
Charger software version per site and when it was installed.

### operations_log.csv — people's work per site and day
| action | What it is |
|---|---|
| move_and_replug_vehicle | Night staff move a fully charged vehicle and plug in the next one |
| remote_reset | Someone resets a charger from the backend |
| technician_visit | A technician goes to the site |
| manual_load_adjustment | Someone changes power settings by hand when a site runs at its grid limit |
| customer_contact | A driver calls or writes |
| manual_price_change | Someone changes the public price |

`count` = how many times, `staff_minutes` = total time spent.

### price_changes.csv
Public price changes at some sites during the period (old and new price, from when).

### network_grid_events.csv
Curtailments at flexible connections: when the grid company told the site, and the lower limit.

## Energy system

### zone_prices.csv — 15 min
Day-ahead price in €/MWh per bidding zone (one column per zone). Synthetic, shaped like the real Nordic and German markets.

### reserve_market_prices.csv — hourly
Capacity prices in €/MW per hour for some frequency reserve markets (FCR-D, FCR-N, FCR, FFR). Synthetic.

### co2_intensity.csv — hourly
Carbon intensity of grid electricity per zone, g CO₂/kWh. Synthetic.

### weather.csv — hourly
Outdoor temperature per location.

## Growth and investment

### grid_capacity.csv — 1 row per network site
How much more grid capacity is available (`max_upgrade_kw`), at what cost per kW and lead time in months, and whether a flexible (curtailable) extra capacity is offered.

### candidate_locations.csv
Possible new sites: location, road type, daily traffic (AADT = average vehicles per day), grid capacity nearby, connection lead time, land cost, distance to the nearest existing site.

### equipment_costs.csv
Illustrative unit costs (public ballpark figures, not quotes).

### growth_forecast.csv
Expected number of electric vehicles per segment and country, index 2026 = 100. Illustrative.

### corridors.csv
Main freight routes: length, electric trucks per day now and forecast for 2029, and which of our sites are at either end.

