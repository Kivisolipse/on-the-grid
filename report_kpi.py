"""
report_kpi.py —— 读 output/log.csv，算 13.11 当天的 KPI，和 baseline 并排比较。

运行：先 python main.py，再 python report_kpi.py
输出：屏幕上一张表 + output/kpi.csv
（report_savings.py 会导入这里的 ours() 和 base()）

算法说明（pitch 被问到时用）：
  电费 = Σ 功率 kW × 0.25 h × 电价（spot + 场站电网电费，log 里的 price_eur_kwh）
  用同一个公式重算 baseline 的功率曲线，结果和 baseline_kpis.csv 一致，
  脚本最后会打印这个核对结果。
"""
import pandas as pd

from config import DATA_DIR, OUTPUT_DIR, LOG_PATH, SITES, TZ, SLOT_HOURS

DAY = "2026-11-13"
SPIKE_START = pd.Timestamp(f"{DAY} 17:15", tz=TZ)   # 电价尖峰
SPIKE_END = pd.Timestamp(f"{DAY} 18:45", tz=TZ)
# 人工只比较"周四夜里"这一晚：两边的夜间人工都完整落在仿真时间里
NIGHT_START = pd.Timestamp("2026-11-12 18:00", tz=TZ)
NIGHT_END = pd.Timestamp("2026-11-13 08:00", tz=TZ)


def to_time(col):
    return pd.to_datetime(col, utc=True).dt.tz_convert(TZ)


def on_day(times):
    return times.dt.strftime("%Y-%m-%d") == DAY


# ---------- 读数据 ----------
log = pd.read_csv(LOG_PATH)
log["t"] = to_time(log["t"])
log["depart_ts"] = to_time(log["depart_ts"])

baseline = pd.read_csv(DATA_DIR / "baseline_kpis.csv")
baseline = baseline[baseline["period"] == "scenario_day"].set_index("site_id")

power = pd.read_csv(DATA_DIR / "site_power_baseline.csv")
power["ts"] = to_time(power["ts"])

prices = pd.read_csv(DATA_DIR / "prices.csv")
prices["ts"] = to_time(prices["ts"])
prices["eur_kwh"] = (prices["spot_eur_mwh"] + prices["grid_tariff_depot_eur_mwh"]) / 1000
power = power.merge(prices[["ts", "eur_kwh"]], on="ts", how="left")

sessions = pd.read_csv(DATA_DIR / "sessions_baseline.csv")
sessions["end_ts"] = to_time(sessions["end_ts"])

tickets = pd.read_csv(DATA_DIR / "service_tickets.csv")
tickets["dispatched_ts"] = to_time(tickets["dispatched_ts"])
faults = pd.read_csv(DATA_DIR / "faults.csv")
tickets = tickets.merge(faults[["fault_id", "site_id"]], on="fault_id", how="left")


def in_night(times):
    return (times >= NIGHT_START) & (times < NIGHT_END)


def event_texts(rows):
    """一批 log 行里的所有事件文字（relay 行 + health 写在 events 列里的），去掉重复。"""
    texts = []
    for cell in rows["events"].dropna():
        texts.extend(x.strip() for x in str(cell).split(" | "))
    return sorted(set(x for x in texts if x))


def events_at_night(site):
    return event_texts(log[(log["site"] == site) & in_night(log["t"])])


def events_all(site):
    return event_texts(log[log["site"] == site])


# ---------- 我们的 KPI ----------
def ours(site):
    charge = log[(log["row_type"] == "charge") & (log["site"] == site) & on_day(log["t"])]
    deps = log[(log["row_type"] == "departure") & (log["site"] == site) & on_day(log["depart_ts"])]
    night = events_at_night(site)

    energy_mwh = (charge["kw"] * SLOT_HOURS).sum() / 1000
    cost = (charge["kw"] * SLOT_HOURS * charge["price_eur_kwh"]).sum()
    site_kw = charge.groupby("t")["kw"].sum()
    site_limit = charge.groupby("t")["site_limit_kw"].first()
    in_spike = (charge["t"] >= SPIKE_START) & (charge["t"] < SPIKE_END)
    planner = charge.groupby("t")["planner"].first().value_counts()

    swaps = [e for e in night if e.startswith("[on-call]")]
    visits = [e for e in night if e.startswith("[visit]")]
    on_site = 0
    for e in night:
        if e.startswith("[leave]"):
            on_site += int(e.split(" after ")[1].split(" min")[0])
    night_calls = [e for e in night if "dispatch" in e]
    preventive = [e for e in events_all(site) if "preventive work order" in e]

    return {
        "发车次数": len(deps),
        "欠电发车": int(deps["below_required"].astype(int).sum()),
        "用电 MWh": round(energy_mwh, 2),
        "电费 €": round(cost),
        "每 MWh 平均电价 €": round(cost / energy_mwh) if energy_mwh > 0 else 0,
        "峰值 kW": round(site_kw.max()),
        "超过站点上限的时段": int((site_kw > site_limit + 1e-5).sum()),
        "电价尖峰期间用电 kWh": round((charge.loc[in_spike, "kw"] * SLOT_HOURS).sum()),
        "周四夜里换插头（次）": len(swaps),
        "换插头净工时（分钟）": len(swaps) * 8,
        "夜班人员到场（趟）": len(visits),
        "夜班人员在现场（分钟）": on_site,
        "夜里叫技术员（次）": len(night_calls),
        "白班预防性工单（次）": len(preventive),
        "MPC 时段 / LLF 时段": f"{planner.get('mpc', 0)} / {planner.get('llf', 0)}",
    }


# ---------- baseline 的 KPI ----------
def base(site):
    b = baseline.loc[site]
    p = power[(power["site_id"] == site) & on_day(power["ts"])]
    in_spike = (p["ts"] >= SPIKE_START) & (p["ts"] < SPIKE_END)
    replugs = sessions[(sessions["site_id"] == site) & (sessions["end_reason"] == "unplugged_by_operator")
                       & in_night(sessions["end_ts"])]
    night_calls = tickets[(tickets["site_id"] == site) & in_night(tickets["dispatched_ts"])]
    return {
        "发车次数": int(b["departures"]),
        "欠电发车": int(b["departures_below_required_soc"]),
        "用电 MWh": float(b["energy_mwh"]),
        "电费 €": round(float(b["energy_cost_eur"])),
        "每 MWh 平均电价 €": round(float(b["energy_cost_eur"]) / float(b["energy_mwh"])),
        "峰值 kW": round(float(b["peak_kw"])),
        "超过站点上限的时段": "—",
        "电价尖峰期间用电 kWh": round((p.loc[in_spike, "power_kw"] * SLOT_HOURS).sum()),
        "周四夜里换插头（次）": len(replugs),
        "换插头净工时（分钟）": len(replugs) * 8,
        "夜班人员到场（趟）": replugs["end_ts"].dt.strftime("%H:%M").nunique(),
        "夜班人员在现场（分钟）": "—",
        "夜里叫技术员（次）": len(night_calls),
        "白班预防性工单（次）": 0,
        "MPC 时段 / LLF 时段": "—",
    }


# ---------- 拼表、输出 ----------
def main():
    table = {}
    for site in SITES:
        table[f"{site} baseline"] = base(site)
        table[f"{site} 我们"] = ours(site)
    table = pd.DataFrame(table)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / "kpi.csv"
    table.to_csv(out, encoding="utf-8-sig")

    pd.set_option("display.width", 200)
    print(f"13.11 KPI 对比（{', '.join(SITES)}）；人工那几行只看周四夜里 12.11 18:00–13.11 08:00\n")
    print(table.to_string())
    print(f"\n已保存：{out}")

    # 发车次数对不上，说明统计口径有问题，表里的数字不能比
    for site in SITES:
        if table.loc["发车次数", f"{site} baseline"] != table.loc["发车次数", f"{site} 我们"]:
            print(f"注意：{site} 的发车次数和 baseline 不一样，先查 data.py 的统计口径")

    # 核对：用我们的电费公式重算 baseline，应该等于官方的 energy_cost_eur
    print("\n电费公式核对（用我们的公式重算 baseline）：")
    for site in SITES:
        p = power[(power["site_id"] == site) & on_day(power["ts"])]
        recomputed = (p["power_kw"] * SLOT_HOURS * p["eur_kwh"]).sum()
        official = float(baseline.loc[site, "energy_cost_eur"])
        print(f"  {site}: 重算 €{recomputed:.0f}，官方 €{official:.0f}")


if __name__ == "__main__":
    main()
