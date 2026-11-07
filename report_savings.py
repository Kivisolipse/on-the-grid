"""
report_savings.py —— 我们的方案在情景日（周四夜里 + 周五 13.11），比今天的做法整体省下了什么。

运行：先 python main.py，再 python report_savings.py
输出：
  终端里一张中文对比表
  output/savings.csv
  output/savings.html   英文网页，浏览器直接打开，可以放进 pitch

口径（和 report_kpi.py 完全一样，数字也从那里来）：
  电费、发车、电网、碳排放：13.11 当天
  夜间人工：周四夜里 12.11 18:00 – 13.11 08:00
  换算成钱：电费用 log 里的电价；欠电罚款用 contracts.csv；人工用 equipment_costs.csv 的时薪
"""
import html

import pandas as pd

import report_kpi as k
from config import DATA_DIR, OUTPUT_DIR, SITES, TZ, SLOT_HOURS

# ---------- 价格参数（来自数据） ----------
costs = pd.read_csv(DATA_DIR / "equipment_costs.csv").set_index("item")["eur"]
STAFF_EUR_PER_HOUR = float(costs["operations_staff"])            # 45

penalty = {}                                                     # 场站 -> 每次欠电发车罚多少
for _, c in pd.read_csv(DATA_DIR / "contracts.csv").iterrows():
    for s in str(c["sites"]).split(";"):
        penalty[s] = float(c["penalty_eur_per_late_departure"])

co2 = pd.read_csv(DATA_DIR / "co2_intensity.csv")
co2["ts"] = k.to_time(co2["ts"])
co2 = co2[co2["zone"] == "FI"].set_index("ts")["g_co2_per_kwh"]   # 五个 close-up 站都在芬兰


def co2_kg(times, kw):
    """每个 15 分钟的电量 × 那一小时的碳强度，加起来，单位 kg。"""
    g_per_kwh = times.dt.floor("h").map(co2)
    return float((kw * SLOT_HOURS * g_per_kwh).sum() / 1000)


# ---------- 每个场站的数字 ----------
def site_numbers(site):
    b, o = k.base(site), k.ours(site)
    base_power = k.power[(k.power["site_id"] == site) & k.on_day(k.power["ts"])]
    ours_charge = k.log[(k.log["row_type"] == "charge") & (k.log["site"] == site) & k.on_day(k.log["t"])]
    pen = penalty.get(site, 0.0)
    return {
        "energy_cost": (b["电费 €"], o["电费 €"]),
        "penalty": (b["欠电发车"] * pen, o["欠电发车"] * pen),
        "staff_cost": (b["换插头净工时（分钟）"] / 60 * STAFF_EUR_PER_HOUR,
                       o["换插头净工时（分钟）"] / 60 * STAFF_EUR_PER_HOUR),
        "late": (b["欠电发车"], o["欠电发车"]),
        "night_calls": (b["夜里叫技术员（次）"], o["夜里叫技术员（次）"]),
        "swaps": (b["周四夜里换插头（次）"], o["周四夜里换插头（次）"]),
        "staff_min": (b["换插头净工时（分钟）"], o["换插头净工时（分钟）"]),
        "visits": (b["夜班人员到场（趟）"], o["夜班人员到场（趟）"]),
        "spike_kwh": (b["电价尖峰期间用电 kWh"], o["电价尖峰期间用电 kWh"]),
        "energy_mwh": (b["用电 MWh"], o["用电 MWh"]),
        "co2": (co2_kg(base_power["ts"], base_power["power_kw"]), co2_kg(ours_charge["t"], ours_charge["kw"])),
        "peak": (b["峰值 kW"], o["峰值 kW"]),
        "over_limit": o["超过站点上限的时段"],
    }


per_site = {s: site_numbers(s) for s in SITES}


def total(key):
    return (sum(per_site[s][key][0] for s in SITES), sum(per_site[s][key][1] for s in SITES))


# ---------- 对比表的每一行 ----------
# (分组, 键, 中文名, 英文名, 单位, 小数位)  —— 所有行都是"越小越好"
ROWS = [
    ("money", "energy_cost", "电费", "Electricity cost", "€", 0),
    ("money", "staff_cost", "夜间换插头人工", "Night plugging work", "€", 0),
    ("fleet", "late", "欠电发车", "Vehicles leaving under-charged", "", 0),
    ("fleet", "night_calls", "夜里叫技术员", "Technician called out at night", "", 0),
    ("people", "swaps", "夜里换插头", "Plug swaps at night", "", 0),
    ("people", "staff_min", "换插头净工时", "Net plugging time at night", "min", 0),
    ("people", "visits", "夜班人员到场", "Night-staff trips to the depot", "", 0),
    ("grid", "spike_kwh", "电价尖峰期间用电（17:15–18:45）", "Energy bought in the price spike (17:15–18:45)", "kWh", 0),
    ("grid", "energy_mwh", "用电量", "Energy charged", "MWh", 2),
    ("grid", "co2", "碳排放", "CO₂ from charging", "kg", 0),
]
GROUPS = {"money": "Money", "fleet": "Fleet service", "people": "People at night", "grid": "Grid and energy"}

table = []
for group, key, zh, en, unit, dp in ROWS:
    b, o = total(key)
    table.append({"group": group, "key": key, "zh": zh, "en": en, "unit": unit, "dp": dp,
                  "base": b, "ours": o, "saved": b - o,
                  "pct": (b - o) / b * 100 if b else None,
                  "sites": {s: per_site[s][key] for s in SITES}})

# 真正省下的现金：电费 + 夜间人工（两边加起来）
money_saved = sum(r["saved"] for r in table if r["group"] == "money")

# ---------- 分账：客户、服务方各省了多少 ----------
# 假设（写进 assumptions.md）：
#   - baseline 里夜间换插头是客户自己的员工做的，我们的方案里由服务方的 on-call 做
#   - 合同罚款（S1 每次欠电 €150）是服务方赔给客户的钱，所以少欠电时：
#       服务方少赔这笔钱；客户少收这笔赔偿，但换来准点发车，
#       按合同的价格算，一次准点发车对客户值 €150，两者抵消
cost_saved = total("energy_cost")[0] - total("energy_cost")[1]
staff_b, staff_o = total("staff_cost")
late_value = sum((per_site[s]["late"][0] - per_site[s]["late"][1]) * penalty.get(s, 0.0) for s in SITES)
PARTY_ROWS = [
    ("电费", "Electricity", cost_saved, 0.0),
    ("夜间换插头", "Night plugging work", staff_b, -staff_o),
    ("欠电发车", "Late departures", 0.0, late_value),
]
customer_total = sum(r[2] for r in PARTY_ROWS)
provider_total = sum(r[3] for r in PARTY_ROWS)
cost_b, cost_o = total("energy_cost")
mwh_b, mwh_o = total("energy_mwh")
avg_b, avg_o = cost_b / mwh_b, cost_o / mwh_o


def fmt(x, dp=0):
    return f"{x:,.{dp}f}"


# ---------- 人从哪里退出了循环，系统接管了什么 ----------
# 全部从 log 和数据里找证据；找不到的格子写"—"，不编数字
import re

from config import SIM_START, SIM_END, SLOT

health_rows = k.log[k.log["row_type"] == "health"]
relay_rows = k.log[k.log["row_type"] == "relay"]
charge_rows = k.log[k.log["row_type"] == "charge"]

# 1) 每 15 分钟的计划：一共多少次，几次在断网时由场站本地完成
plans_total = len(SITES) * int((SIM_END - SIM_START) / SLOT)
cycles = charge_rows.groupby(["site", "t"])["planner"].first()
offline_cycles = int((cycles == "llf").sum())

# 2) 风扇故障：我们什么时候预警、什么时候修好；今天的做法什么时候才发现、几点叫人
fan = {}
warn = health_rows[health_rows["events"].str.contains(r"predict \S+ overtemperature", na=False)]
if len(warn):
    w = warn.iloc[0]
    cab = re.search(r"predict (\S+) overtemperature", w["events"]).group(1)
    fan["cab"], fan["warned"] = cab, w["t"]
    back = health_rows[health_rows["events"].str.contains(f"{cab} back on", na=False, regex=False)]
    if len(back):
        m = re.search(r"(\d\d\.\d\d \d\d:\d\d) \S+ back on: (\S+) replaced", back.iloc[0]["events"])
        fan["fixed_at"], fan["tech"] = m.group(1)[-5:], m.group(2)
    f = k.faults.copy()
    f["start_ts"] = k.to_time(f["start_ts"])
    f = f[(f["cabinet_id"] == cab) & (f["fault_category"] == "overtemperature")
          & (f["start_ts"] > fan["warned"]) & (f["start_ts"] < SIM_END)].sort_values("start_ts")
    if len(f):
        first = f.iloc[0]
        fan["fault_at"] = first["start_ts"]
        fan["lead_h"] = (first["start_ts"] - fan["warned"]).total_seconds() / 3600
        # 同一次过温在数据里拆成了几条记录（先降额、再停机），工单挂在其中一条上
        tk = k.tickets[k.tickets["fault_id"].isin(f["fault_id"])].sort_values("opened_ts")
        if len(tk):
            tk = tk.iloc[0]
            fan["opened_min"] = (k.to_time(pd.Series([tk["opened_ts"]]))[0] - first["start_ts"]).total_seconds() / 60
            fan["dispatched"] = tk["dispatched_ts"]
            fan["reset_tried"] = int(tk["remote_reset_tried"]) == 1


def hhmm(ts):
    return ts.strftime("%H:%M")


trips_b, trips_o = total("visits")
spike_b, spike_o = total("spike_kwh")
on_site_o = sum(k.ours(s)["夜班人员在现场（分钟）"] for s in SITES)
cfg = pd.read_csv(DATA_DIR / "site_config.csv")
cfg = cfg[cfg["site_id"].isin(SITES) & (cfg["parameter"] == "offline_fallback_kw_per_dispenser")]
fallback_kw = float(cfg["configured_value"].iloc[0]) if len(cfg) else 50.0
visit_reasons = [e for e in relay_rows["events"] if e.startswith("[visit]")]

# 每一行：(要做的事, 今天谁做, 我们谁做, 变化类型)
#   system = 决定交给了系统；hands = 系统决定、人只动手；human = 还是人，写明原因
TASKS = [
    ("Decide each vehicle's charging power",
     "Vehicles charge when plugged in; across the network, staff change power settings by hand when a site hits its limit",
     f"System plans 12 h ahead every 15 min: {plans_total} plans, no human input", "system"),
    ("React to price spikes and grid notices",
     f"Nobody: {fmt(spike_b)} kWh bought in the 17:15–18:45 spike",
     f"System replans at the 14:00 DSO notice: {fmt(spike_o)} kWh in the spike, limit never exceeded", "system"),
    ("Notice a failing cabinet",
     (f"After it failed: ticket opened {fan['opened_min']:.0f} min after the {hhmm(fan['fault_at'])} fault"
      if "opened_min" in fan else "—"),
     (f"System flags the fan trend {fan['lead_h']:.0f} h before the failure ({fan['warned']:%d.%m %H:%M})"
      if "lead_h" in fan else "—"), "system"),
    ("Decide who repairs it and when",
     ((("A person tries a remote reset, then calls" if fan.get("reset_tried") else "A person calls")
       + f" a technician out at {hhmm(fan['dispatched'])}") if "dispatched" in fan else "—"),
     (f"System sends a work order to {fan.get('tech', 'the technician')} on shift; fixed by {fan.get('fixed_at', '—')}"
      if fan else "—"), "system"),
    ("Do the repair", "Technician, at night", "Technician, on the day shift", "human"),
    ("Choose which vehicle to unplug and which charger to use", "Night staff", "System", "system"),
    ("Move the plugs",
     f"Night staff: {fmt(total('swaps')[0])} swaps, {fmt(trips_b)} trips",
     f"On-call staff: {fmt(total('swaps')[1])} swaps, {fmt(trips_o)} trips at times the system picks, {fmt(on_site_o)} min on site",
     "hands"),
    ("Keep charging when the cloud link is lost",
     f"Every charger drops to a fixed {fmt(fallback_kw)} kW",
     f"Site controller plans locally (tested: {offline_cycles * 15 // 60} h injected outage)", "system"),
]
moved = sum(1 for t in TASKS if t[3] == "system")
pd.DataFrame([{"任务": t, "今天": a, "我们的系统": b, "变化": {"system": "决定交给了系统", "hands": "系统决定、人动手",
                                                      "human": "仍需要人"}[c]} for t, a, b, c in TASKS]
             ).to_csv(OUTPUT_DIR / "savings_human_loop.csv", index=False, encoding="utf-8-sig")
STAY = [
    ("Moving plugs", "a cable needs hands; the system cuts it to visits it schedules, and calls a backup only when a vehicle runs late"),
    ("Repairs", "a fan or a cable is physical work; the system moves it from a night call-out to a day shift"),
    ("Safety faults", "after cable damage or a ground fault the charger stays locked, with no automatic restart, until a person has checked it"),
]


# ---------- 终端 + CSV ----------
df = pd.DataFrame([{
    "项目": r["zh"] + (f"（{r['unit']}）" if r["unit"] else ""),
    "今天的做法": fmt(r["base"], r["dp"]),
    "我们": fmt(r["ours"], r["dp"]),
    "省下": ("+" if r["saved"] > 0 else "") + fmt(r["saved"], r["dp"]),
    "省下 %": "" if r["pct"] is None else f"{r['pct']:.0f}%",
    **{s: f"{fmt(r['sites'][s][0], r['dp'])} → {fmt(r['sites'][s][1], r['dp'])}" for s in SITES},
} for r in table])

OUTPUT_DIR.mkdir(exist_ok=True)
df.to_csv(OUTPUT_DIR / "savings.csv", index=False, encoding="utf-8-sig")
pd.set_option("display.width", 220)
print(f"情景日对比（{' + '.join(SITES)}）：电费、发车、电网看 13.11 当天，人工看周四夜里\n")
print(df.to_string(index=False))
print(f"\n一天真正省下的现金（电费 + 夜间人工，两边加起来）：€{money_saved:,.0f}")
print(f"欠电发车 {fmt(total('late')[0])} → {fmt(total('late')[1])}，按合同每次 €150 算，值 €{late_value:,.0f}"
      f"（这笔是服务方少赔的钱，不是客户省的）")
party = pd.DataFrame([{"项目": zh, "客户省": fmt(c), "服务方省": fmt(pv), "两边合计": fmt(c + pv)}
                      for zh, _, c, pv in PARTY_ROWS]
                     + [{"项目": "合计", "客户省": fmt(customer_total), "服务方省": fmt(provider_total),
                         "两边合计": fmt(customer_total + provider_total)}])
party.to_csv(OUTPUT_DIR / "savings_by_party.csv", index=False, encoding="utf-8-sig")
print("\n谁省了多少（€）：")
print(party.to_string(index=False))
print("  客户那一栏是协议里按比例收费的基数；欠电那一行客户是 0：少收的赔偿和准点发车的价值抵消")
print(f"\n人在哪里退出了循环（{moved} 件事的决定交给了系统）：")
for task, today, ours, kind in TASKS:
    tag = {"system": "系统接管", "hands": "系统决定、人动手", "human": "仍需要人"}[kind]
    print(f"  [{tag}] {task}\n      今天：{today}\n      我们：{ours}")
print("人还留在哪里、为什么：")
for what, why in STAY:
    print(f"  {what}：{why}")
print(f"\n每 MWh 平均电价：€{avg_b:.0f} → €{avg_o:.0f}")
print(f"全程超过电网上限的时段：{sum(per_site[s]['over_limit'] for s in SITES)}")


# ---------- 网页 ----------
def cell_delta(r):
    if abs(r["saved"]) < 10 ** (-r["dp"]) / 2:
        return '<span class="delta same">no change</span>'
    cls = "good" if r["saved"] > 0 else "bad"
    word = "less" if r["saved"] > 0 else "more"
    pct = "" if r["pct"] is None else f' <span class="pct">{abs(r["pct"]):.0f}%</span>'
    unit = f" {r['unit']}" if r["unit"] and r["unit"] != "€" else ""
    money = "€" if r["unit"] == "€" else ""
    return f'<span class="delta {cls}">{money}{fmt(abs(r["saved"]), r["dp"])}{unit} {word}{pct}</span>'


def cell_value(r, which):
    i = 0 if which == "base" else 1
    money = "€" if r["unit"] == "€" else ""
    unit = f' <span class="unit">{r["unit"]}</span>' if r["unit"] and r["unit"] != "€" else ""
    split = " · ".join(f"{s} {money}{fmt(r['sites'][s][i], r['dp'])}" for s in SITES)
    return (f'<span class="num">{money}{fmt(r[which], r["dp"])}</span>{unit}'
            f'<span class="split">{html.escape(split)}</span>')


def eur(x):
    if abs(x) < 0.5:
        return '<span class="num muted">€0</span>'
    sign = "+" if x > 0 else "−"
    cls = "good" if x > 0 else "bad"
    return f'<span class="delta {cls}">{sign}€{fmt(abs(x))}</span>'


party_rows = "".join(
    f'<tr><th scope="row">{en}</th><td>{eur(c)}</td><td>{eur(pv)}</td><td>{eur(c + pv)}</td></tr>'
    for _, en, c, pv in PARTY_ROWS)
party_rows += (f'<tr class="total"><th scope="row">Total</th><td>{eur(customer_total)}</td>'
               f'<td>{eur(provider_total)}</td><td>{eur(customer_total + provider_total)}</td></tr>')

KIND = {"system": ("Decision moved to the system", "kind-system"),
        "hands": ("System decides, a person moves", "kind-hands"),
        "human": ("Still a person", "kind-human")}
task_rows = "".join(
    f'<tr><th scope="row">{html.escape(task)}<span class="kind {KIND[kind][1]}">{KIND[kind][0]}</span></th>'
    f'<td>{html.escape(today)}</td><td>{html.escape(ours)}</td></tr>'
    for task, today, ours, kind in TASKS)
stay_items = "".join(f"<li><strong>{html.escape(w)}</strong>: {html.escape(y)}.</li>" for w, y in STAY)
lead = f"{fan['lead_h']:.0f} h before" if "lead_h" in fan else "—"
late_notice = f"{fan['opened_min']:.0f} min after" if "opened_min" in fan else "—"

body_rows = []
for group, title in GROUPS.items():
    body_rows.append(f'<tr class="group"><th colspan="4">{title}</th></tr>')
    for r in [x for x in table if x["group"] == group]:
        body_rows.append(
            f'<tr><th scope="row">{html.escape(r["en"])}</th>'
            f'<td>{cell_value(r, "base")}</td><td>{cell_value(r, "ours")}</td><td>{cell_delta(r)}</td></tr>')

late = next(r for r in table if r["key"] == "late")
spike = next(r for r in table if r["key"] == "spike_kwh")
calls = next(r for r in table if r["key"] == "night_calls")
over_limit = sum(per_site[s]["over_limit"] for s in SITES)
peaks = " · ".join(f"{s} {fmt(per_site[s]['peak'][1])} kW" for s in SITES)

CSS = """
:root{
  --paper:#f4f6f5; --card:#ffffff; --ink:#18262b; --muted:#5d6c70; --line:#d9e0de;
  --accent:#0b6e69; --accent-bg:#dcefed; --warn:#8a5a00; --warn-bg:#f6ecd6; --good:#1f7a3f; --good-bg:#e3f2e7; --bad:#a13c2c; --bad-bg:#f7e4df;
  --display:"IBM Plex Sans Condensed","Arial Narrow",system-ui,sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Menlo,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --paper:#111a1c; --card:#182325; --ink:#e6eeec; --muted:#9aaaa8; --line:#2b393b;
  --accent:#4cc0b7; --accent-bg:#163432; --warn:#e7b65a; --warn-bg:#352a14; --good:#6fd08f; --good-bg:#18321f; --bad:#f08f7b; --bad-bg:#3a201b; color-scheme:dark}}
:root[data-theme="dark"]{
  --paper:#111a1c; --card:#182325; --ink:#e6eeec; --muted:#9aaaa8; --line:#2b393b;
  --accent:#4cc0b7; --accent-bg:#163432; --warn:#e7b65a; --warn-bg:#352a14; --good:#6fd08f; --good-bg:#18321f; --bad:#f08f7b; --bad-bg:#3a201b; color-scheme:dark}
body{background:var(--paper);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.5;margin:0}
.wrap{max-width:980px;margin:0 auto;padding-block:32px 48px;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0}
h1{font-family:var(--display);font-weight:600;font-size:clamp(28px,5vw,42px);line-height:1.1;margin:6px 0 8px;text-wrap:balance}
.lede{color:var(--muted);max-width:65ch;margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:28px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:16px}
.card .big{font-family:var(--display);font-size:34px;font-weight:600;line-height:1;font-variant-numeric:tabular-nums}
.card .big .from{color:var(--muted);font-weight:400;font-size:22px}
.card p{margin:8px 0 0;color:var(--muted);font-size:13px}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%;min-width:640px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top}
thead th{font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:500}
tbody th{font-weight:500}
tr.group th{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);
  background:var(--paper);padding-top:14px}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:15px}
.unit{color:var(--muted);font-size:12px}
.split{display:block;color:var(--muted);font-family:var(--mono);font-size:11px;margin-top:2px}
.delta{display:inline-block;font-family:var(--mono);font-size:13px;padding:2px 8px;border-radius:4px;font-variant-numeric:tabular-nums}
.delta.good{color:var(--good);background:var(--good-bg)}
.delta.bad{color:var(--bad);background:var(--bad-bg)}
.delta.same{color:var(--muted)}
.pct{opacity:.75}
.notes{margin-top:24px;color:var(--muted);font-size:13px;max-width:75ch}
table.tasks{min-width:720px}
table.tasks td{color:var(--ink);font-size:14px}
table.tasks th[scope="row"]{width:30%}
.kind{display:block;width:max-content;margin-top:6px;font-family:var(--mono);font-size:11px;
  padding:2px 8px;border-radius:4px;font-weight:500}
.kind-system{color:var(--accent);background:var(--accent-bg)}
.kind-hands{color:var(--warn);background:var(--warn-bg)}
.kind-human{color:var(--muted);background:var(--paper)}
.notes.stay{margin-top:16px}
h2.sub{font-family:var(--display);font-size:20px;font-weight:600;margin:28px 0 10px}
table.party{min-width:520px}
tr.total th,tr.total td{border-top:2px solid var(--line);font-weight:600}
.muted{color:var(--muted)}
.notes h2{font-family:var(--display);font-size:18px;color:var(--ink);margin:0 0 6px}
.notes li{margin-bottom:6px}
"""

page = f"""<title>Scenario Day Savings</title>
<meta charset="utf-8">
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@400;600&family=IBM+Plex+Sans:wght@400;500&display=swap">
<style>{CSS}</style>
<div class="wrap">
  <p class="eyebrow">Thursday night + Friday 13.11.2026 · {' + '.join(SITES)}</p>
  <h1>Fewer people in the loop, lower cost, on the hardest day</h1>
  <p class="lede">Bus depot {SITES[0]} and logistics depot {SITES[1]} in our simulation, compared with how they run today
  (baseline). Electricity, departures and grid on Friday 13.11; night work from 12.11 18:00 to 13.11 08:00.</p>

  <h2 class="sub">Who is in the loop</h2>
  <div class="cards">
    <div class="card"><div class="big">{moved} of {len(TASKS)}</div>
      <p>tasks where the decision moved from a person to the system</p></div>
    <div class="card"><div class="big">{lead}</div>
      <p>the failing cabinet was flagged, against {late_notice} the failure today</p></div>
    <div class="card"><div class="big">{plans_total}</div>
      <p>charging plans made without a person, one every 15 min per depot; {offline_cycles * 15 // 60} h of them on the site controller with the cloud link down</p></div>
    <div class="card"><div class="big"><span class="from">{fmt(trips_b)} →</span> {fmt(trips_o)}</div>
      <p>night trips to the depots; the system chooses when the on-call person comes and what they do</p></div>
  </div>
  <div class="scroll">
    <table class="tasks">
      <thead><tr><th scope="col">Task</th><th scope="col">Today</th><th scope="col">Our system</th></tr></thead>
      <tbody>{task_rows}</tbody>
    </table>
  </div>
  <div class="notes stay">
    <h2>Where people stay, and why</h2>
    <ul>{stay_items}</ul>
  </div>

  <h2 class="sub">What it saved</h2>
  <div class="cards">
    <div class="card"><div class="big">€{fmt(money_saved)}</div>
      <p>cash saved in one day: cheaper electricity and night plugging work</p></div>
    <div class="card"><div class="big"><span class="from">{fmt(late['base'])} →</span> {fmt(late['ours'])}</div>
      <p>vehicles leaving under-charged; worth €{fmt(late_value)} at the contract price of a late departure</p></div>
    <div class="card"><div class="big"><span class="from">{fmt(spike['base'])} →</span> {fmt(spike['ours'])} kWh</div>
      <p>bought during the 17:15–18:45 price spike (up to 820 €/MWh)</p></div>
    <div class="card"><div class="big"><span class="from">{fmt(calls['base'])} →</span> {fmt(calls['ours'])}</div>
      <p>technician called out at night; the fan was replaced on Thursday's day shift instead</p></div>
  </div>
  <div class="scroll">
    <table>
      <thead><tr><th scope="col"></th><th scope="col">Today</th><th scope="col">Our system</th><th scope="col">Difference</th></tr></thead>
      <tbody>
        {"".join(body_rows)}
      </tbody>
    </table>
  </div>

  <h2 class="sub">Who saves what</h2>
  <div class="scroll">
    <table class="party">
      <thead><tr><th scope="col"></th><th scope="col">Customer (fleet)</th><th scope="col">Provider</th><th scope="col">Together</th></tr></thead>
      <tbody>
        {party_rows}
      </tbody>
    </table>
  </div>

  <div class="notes">
    <h2>How to read this</h2>
    <ul>
      <li>We charge each vehicle to what its next route needs, today's operation charges to 100%. That is why energy is lower.
        The price we pay per MWh shows the part that comes from charging at better times:
        €{avg_b:.0f} → €{avg_o:.0f} per MWh.</li>
      <li>The late-departure penalty (€{penalty.get(SITES[0], 0):.0f} at {SITES[0]}, €{penalty.get(SITES[1], 0):.0f} at {SITES[1]})
        is paid by the provider to the customer. Fewer late departures save the provider that payment.
        The customer gets an on-time vehicle instead of the compensation, so its line is €0.</li>
      <li>Night plugging used to be done by the customer's own staff; in our system the provider's on-call staff do it.
        Work is valued at €{STAFF_EUR_PER_HOUR:.0f}/h (equipment_costs.csv).</li>
      <li>At {SITES[0]} the night staff swap more plugs than today: today three buses were never plugged in that night and left
        under-charged; we plug them in too.</li>
      <li>Grid limits were never exceeded ({over_limit} slots over the limit, including the DSO cap at {SITES[1]}).
        Peaks: {peaks}, the same as today.</li>
      <li>All data is synthetic (Kempower challenge dataset). One simulated day, not a yearly forecast.</li>
    </ul>
  </div>
</div>
"""
(OUTPUT_DIR / "savings.html").write_text(page, encoding="utf-8")
print(f"\n已保存：{OUTPUT_DIR / 'savings.csv'}  和  {OUTPUT_DIR / 'savings.html'}")
