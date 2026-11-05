
import unittest
from llf import run_llf


class TestLLF(unittest.TestCase):

    def create_state(self):
        return {
            "time": "2026-11-13T00:00:00",
            "step_hours": 0.25,
            "site_limit_kw": 150.0,
            "cabinets": {
                "C1": {
                    "available_kw": 200.0
                },
                "C2": {
                    "available_kw": 100.0
                }
            },
            "vehicles": {
                "V001": {
                    "cabinet_id": "C1",
                    "max_kw": 100.0,
                    "remaining_kwh": 100.0,
                    "departure": "2026-11-13T02:00:00"
                },
                "V002": {
                    "cabinet_id": "C1",
                    "max_kw": 80.0,
                    "remaining_kwh": 80.0,
                    "departure": "2026-11-13T01:30:00"
                },
                "V003": {
                    "cabinet_id": "C2",
                    "max_kw": 100.0,
                    "remaining_kwh": 40.0,
                    "departure": "2026-11-13T03:00:00"
                }
            }
        }

    def test_priority(self):
        state = self.create_state()
        result = run_llf(state)

        self.assertEqual(result["V002"], 80.0)
        self.assertEqual(result["V001"], 70.0)
        self.assertEqual(result["V003"], 0.0)

    def test_site_limit(self):
        state = self.create_state()
        result = run_llf(state)

        self.assertLessEqual(
            sum(result.values()),
            state["site_limit_kw"]
        )

    def test_cabinet_limit(self):
        state = self.create_state()
        state["cabinets"]["C1"]["available_kw"] = 50.0

        result = run_llf(state)

        self.assertLessEqual(
            result["V001"] + result["V002"],
            50.0
        )

    def test_fault_shutdown(self):
        state = self.create_state()
        state["cabinets"]["C1"]["available_kw"] = 0.0

        result = run_llf(state)

        self.assertEqual(result["V001"], 0.0)
        self.assertEqual(result["V002"], 0.0)

    def test_completed_vehicle(self):
        state = self.create_state()
        state["vehicles"]["V002"]["remaining_kwh"] = 0.0

        result = run_llf(state)

        self.assertEqual(result["V002"], 0.0)

    def test_effective_deadline(self):
        state = self.create_state()

        state["vehicles"]["V003"]["effective_deadline"] = (
            "2026-11-13T00:30:00"
        )

        result = run_llf(state)

        self.assertEqual(result["V003"], 100.0)

    def test_energy_limit(self):
        state = self.create_state()
        state["vehicles"]["V002"]["remaining_kwh"] = 5.0

        result = run_llf(state)

        self.assertLessEqual(result["V002"], 20.0)

    def test_negative_site_limit(self):
        state = self.create_state()
        state["site_limit_kw"] = -10.0

        result = run_llf(state)

        self.assertEqual(sum(result.values()), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
