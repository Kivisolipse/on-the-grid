import csv
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
DAY = '2026-11-13'
SITE = 'S2'
LOG = ROOT / 'member1_integration/results_member1/log.csv'
DEPARTURES = ROOT / 'member1_integration/results_member1/departures.csv'
ZIP = ROOT / 'kempower-challenge(1).zip'

def read_csv(path):
    with path.open(newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))

log = [r for r in read_csv(LOG) if r['site_id'] == SITE and r['time'].startswith(DAY)]
deps = [r for r in read_csv(DEPARTURES) if r['site_id'] == SITE and r['departure'].startswith(DAY)]
with ZipFile(ZIP) as z:
    with z.open('kempower-challenge-data/baseline_kpis.csv') as f:
        baseline = next(r for r in csv.DictReader(line.decode('utf-8-sig') for line in f)
                        if r['site_id'] == SITE and r['period'] == 'scenario_day')

if not log:
    raise ValueError(f'No log entries for {SITE} on {DAY}')

cost = sum(float(r['cost_eur']) for r in log)
# Each task appears in log.csv, so deduplicate time before evaluating station peak.
site_by_time = {}
for r in log:
    t = r['time']
    p = float(r['site_total_kw'])
    if t in site_by_time and abs(site_by_time[t] - p) > 1e-6:
        raise ValueError(f'Inconsistent site_total_kw at {t}')
    site_by_time[t] = p
peak = max(site_by_time.values())
unmet = sum(float(r['unmet_energy_kwh']) > 1e-6 for r in deps)
baseline_count = int(baseline['departures'])
comparable = len(deps) == baseline_count
status = 'COUNT MATCHES (other scope checks still required)' if comparable else 'NOT COMPARABLE: different departure counts'

out = ROOT / f'kpi_checked_{SITE}_{DAY}.csv'
with out.open('w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(['KPI', 'Baseline', 'LLF', 'Comparison status'])
    w.writerow(['Departures in scope', baseline_count, len(deps), status])
    w.writerow(['Unmet departures', baseline['departures_below_required_soc'], unmet, status])
    w.writerow(['Energy cost (EUR)', f"{float(baseline['energy_cost_eur']):.2f}", f'{cost:.2f}', status])
    w.writerow(['Peak power (kW)', f"{float(baseline['peak_kw']):.1f}", f'{peak:.1f}', status])

print(f'Site: {SITE}, day: {DAY}')
print(f'Departures: baseline={baseline_count}, LLF={len(deps)}')
print(f'Unmet departures: baseline={baseline["departures_below_required_soc"]}, LLF={unmet}')
print(f'Energy cost: baseline={float(baseline["energy_cost_eur"]):.2f}, LLF={cost:.2f} EUR')
print(f'Peak: baseline={float(baseline["peak_kw"]):.1f}, LLF={peak:.1f} kW')
print(f'Comparison: {status}')
print(f'Saved: {out}')
