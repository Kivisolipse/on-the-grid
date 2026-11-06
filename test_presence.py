"""Vehicle presence comes from fleet_schedule.csv; plug-ins come from relay.py or sessions_baseline.csv."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import data
import health
import main
from config import TZ


def run_dummy(**kwargs):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "log.csv"
        with mock.patch.object(main, "LOG_PATH", path):
            main.simulate(force_dummy_planner=True, **kwargs)
        return pd.read_csv(path)


class TestPresence(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.log = run_dummy(baseline_plugs=True)
        cls.dep = cls.log[cls.log["row_type"] == "departure"]
        cls.charge = cls.log[cls.log["row_type"] == "charge"]

    def departures(self, vehicle, day="2026-11-13"):
        return self.dep[
            (self.dep["vehicle_id"] == vehicle)
            & self.dep["depart_ts"].str.startswith(day)
        ]

    def test_scenario_day_departures_match_baseline(self):
        day = self.dep[self.dep["depart_ts"].str.startswith("2026-11-13")]
        self.assertEqual((day["site"] == "S1").sum(), 32)
        self.assertEqual((day["site"] == "S2").sum(), 18)

    def test_never_plugged_vehicle_departs_at_arrival_soc(self):
        # B18 arrives 23:27 at 8%, the baseline never gives it a charger
        row = self.departures("B18").iloc[0]
        self.assertEqual(row["soc_after"], 8.0)
        self.assertEqual(row["below_required"], 1)
        self.assertTrue(pd.isna(row["charger_id"]))

    def test_unplugged_vehicle_keeps_soc_until_departure(self):
        # T01 is unplugged at 23:00 on 12.11 and leaves at 06:00 on 13.11
        rows = self.charge[self.charge["vehicle_id"] == "T01"]
        rows = rows[rows["t"].str.startswith(("2026-11-12", "2026-11-13T00"))]
        rows = rows[rows["session_id"] == "SE03192"]
        self.assertEqual(rows["t"].iloc[-1], "2026-11-12T22:45:00+02:00")
        self.assertEqual(
            self.departures("T01").iloc[0]["soc_after"],
            rows["soc_after"].iloc[-1],
        )

    def test_no_charge_rows_outside_baseline_sessions(self):
        sess = pd.read_csv(data.DATA_DIR / "sessions_baseline.csv")
        known = set(sess["session_id"])
        self.assertTrue(self.charge["session_id"].isin(known).all())

    def test_state_splits_present_vehicles_by_plug(self):
        data.reset_soc()
        # B21 arrived 20:38, gets a charger at 00:30
        before = data.get_state("S1", pd.Timestamp("2026-11-13 00:15", tz=TZ))
        after = data.get_state("S1", pd.Timestamp("2026-11-13 00:30", tz=TZ))
        self.assertIn("B21", [v["id"] for v in before["parked"]])
        self.assertNotIn("B21", [v["id"] for v in before["vehicles"]])
        self.assertIn("B21", [v["id"] for v in after["vehicles"]])
        self.assertNotIn("B21", [v["id"] for v in after["parked"]])


class TestRelay(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.log = run_dummy()
        cls.dep = cls.log[cls.log["row_type"] == "departure"]
        cls.charge = cls.log[cls.log["row_type"] == "charge"]
        cls.relay = cls.log[cls.log["row_type"] == "relay"]

    def test_scenario_day_departures_match_baseline(self):
        day = self.dep[self.dep["depart_ts"].str.startswith("2026-11-13")]
        self.assertEqual((day["site"] == "S1").sum(), 32)
        self.assertEqual((day["site"] == "S2").sum(), 18)

    def test_one_vehicle_per_charger_each_slot(self):
        self.assertFalse(self.charge.duplicated(["t", "charger_id"]).any())

    def test_never_more_vehicles_than_chargers(self):
        per_slot = self.charge.groupby(["site", "t"]).size()
        self.assertLessEqual(per_slot.xs("S1").max(), 12)
        self.assertLessEqual(per_slot.xs("S2").max(), 8)

    def test_soc_carries_over_while_unplugged(self):
        # Every departure leaves at the SoC of the vehicle's last charge row of that visit
        for row in self.dep.itertuples():
            before = self.charge[
                (self.charge["vehicle_id"] == row.vehicle_id)
                & (self.charge["depart_ts"] == row.depart_ts)
            ]
            if len(before):
                self.assertAlmostEqual(row.soc_after, before["soc_after"].iloc[-1])

    def test_relay_events_are_one_tagged_row_each(self):
        self.assertTrue(len(self.relay))
        self.assertTrue(self.relay["events"].str.match(r"^\[(driver|wait|visit|on-call)\]").all())

    def test_drivers_only_plug_their_own_vehicle(self):
        driver = self.relay[self.relay["events"].str.startswith("[driver]")]
        self.assertTrue(len(driver))
        self.assertFalse(driver["events"].str.contains("unplugged").any())
        for event in driver["events"]:
            self.assertEqual(event.split("driver of ")[1].split(" plugged")[0], event.split("plugged ")[1].split(" into")[0])


class TestScenarioDayWithRealPlanner(unittest.TestCase):
    """The full MPC / LLF run with the preventive fan repair: nothing leaves under-charged on 13.11."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "log.csv"
            with mock.patch.object(main, "LOG_PATH", path):
                main.simulate()
            cls.log = pd.read_csv(path)
        dep = cls.log[cls.log["row_type"] == "departure"]
        cls.day = dep[dep["depart_ts"].str.startswith("2026-11-13")]
        cls.relay = cls.log[cls.log["row_type"] == "relay"]
        cls.charge = cls.log[cls.log["row_type"] == "charge"]

    def test_no_departure_below_required(self):
        below = self.day[self.day["soc_after"] + 1e-6 < self.day["required_soc_pct"]]
        self.assertEqual(below["vehicle_id"].tolist(), [])
        self.assertEqual(self.day["below_required"].sum(), 0)

    def test_every_departure_is_counted(self):
        self.assertEqual((self.day["site"] == "S1").sum(), 32)
        self.assertEqual((self.day["site"] == "S2").sum(), 18)

    def test_scheduled_visits(self):
        visits = self.relay[self.relay["events"].str.contains("night staff arrive (scheduled)", regex=False)]
        self.assertIn("2026-11-13T00:30:00+02:00", set(visits[visits["site"] == "S1"]["t"]))
        self.assertIn("2026-11-12T23:00:00+02:00", set(visits[visits["site"] == "S2"]["t"]))

    def test_net_moves_on_the_scenario_night(self):
        # One charger per vehicle: 12 extra buses at S1, 6 extra trucks at S2. The baseline needs 19.
        moves = self.relay[self.relay["events"].str.startswith("[on-call]")]
        night = moves[(moves["t"] > "2026-11-12T15:00") & (moves["t"] < "2026-11-13T12:00")]
        self.assertEqual((night["site"] == "S1").sum(), 12)
        self.assertEqual((night["site"] == "S2").sum(), 6)

    def test_c1_3_keeps_full_power_through_the_night(self):
        rows = self.charge[
            (self.charge["cabinet_id"] == "C1-3")
            & (self.charge["t"] >= "2026-11-13T01:45")
            & (self.charge["t"] < "2026-11-13T06:30")
        ]
        self.assertTrue(len(rows))
        self.assertTrue((rows["cabinet_limit_kw"] == 360).all())

    def test_night_staff_needs_8_minutes_per_move(self):
        moves = self.relay[self.relay["events"].str.startswith("[on-call]")]
        self.assertTrue(len(moves))
        # 15 min at 8 min per move: at most 2 moves in one slot
        self.assertLessEqual(moves.groupby(["site", "t"]).size().max(), 2)


class TestPreventiveRepair(unittest.TestCase):
    """health.py acts on its own work order: warned 12.11 13:30, TE1 on site 13:45 to 15:16."""

    @staticmethod
    def check(t):
        return health.check("S1", pd.Timestamp(t, tz=TZ))

    def test_nothing_is_known_before_the_warning(self):
        h = self.check("2026-11-12 13:15")
        self.assertEqual(set(h["cabinet_factor"]["C1-3"]), {1.0})
        self.assertEqual(h["events"], [])

    def test_work_order_goes_out_with_the_warning(self):
        h = self.check("2026-11-12 13:30")
        self.assertTrue(any("preventive work order C1-3" in e and "TE1" in e for e in h["events"]))
        self.assertEqual(h["cabinet_factor"]["C1-3"][:2], [1.0, 0.0])

    def test_cabinet_is_off_only_while_the_fan_is_replaced(self):
        self.assertEqual(self.check("2026-11-12 14:00")["cabinet_factor"]["C1-3"][0], 0.0)
        self.assertEqual(set(self.check("2026-11-12 15:30")["cabinet_factor"]["C1-3"]), {1.0})

    def test_recorded_failure_does_not_happen_after_the_repair(self):
        for t in ("2026-11-13 02:00", "2026-11-13 02:30"):
            h = self.check(t)
            self.assertEqual(set(h["cabinet_factor"]["C1-3"]), {1.0})
            self.assertEqual(h["deadline"], {})
            self.assertFalse(any("derated" in e or "stopped" in e for e in h["events"]))


if __name__ == "__main__":
    unittest.main()
