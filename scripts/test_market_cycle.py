"""Offline regression tests. All forecast and close fixtures are test-only."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

import market_cycle as cycle

DAY = "2026-11-26"


def taipei_at(time):
    return datetime.fromisoformat(f"{DAY}T{time}+08:00")


class ForecastLockTests(TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(cycle, "DATA", Path(directory)))
        self.enterContext(patch.object(cycle, "is_session", return_value=True))
        cycle.write("predictions.json", {"rows": []})
        cycle.write("forecast-snapshot.json", {
            "date": DAY,
            "captured_at": taipei_at("08:40:10").isoformat(),
            "cutoff_at": taipei_at("08:45:00").isoformat(),
            "direction": "偏多",
        })
        self.clock = self.enterContext(patch.object(cycle, "datetime", wraps=datetime))

    def test_late_run_and_elapsed_deadline_leave_book_empty(self):
        self.clock.now.return_value = taipei_at("08:46:00")
        for invoked_at in ("08:45:59", "08:46:00", "09:00:00"):
            with self.subTest(invoked_at=invoked_at), self.assertRaisesRegex(RuntimeError, "window missed"):
                cycle.lock_forecast(taipei_at(invoked_at))
        self.assertEqual(cycle.read("predictions.json")["rows"], [])

    def test_pre_cutoff_snapshot_required(self):
        draft = cycle.read("forecast-snapshot.json")
        draft["captured_at"] = taipei_at("08:45:10").isoformat()
        cycle.write("forecast-snapshot.json", draft)
        with self.assertRaisesRegex(RuntimeError, "pre-cutoff window"):
            cycle.lock_forecast(taipei_at("08:45:20"))
        self.assertEqual(cycle.read("predictions.json")["rows"], [])

    def test_actual_lock_time_is_recorded_and_row_is_immutable(self):
        self.clock.now.return_value = taipei_at("08:45:12")
        cycle.lock_forecast(taipei_at("08:45:11"))
        recorded = cycle.read("predictions.json")
        self.assertEqual(recorded["rows"][0]["locked_at"], taipei_at("08:45:12").isoformat())
        self.assertTrue(recorded["rows"][0]["on_time"])
        snapshot = cycle.read("forecast-snapshot.json")
        cycle.write("forecast-snapshot.json", {**snapshot, "direction": "偏空"})
        cycle.lock_forecast(taipei_at("10:00:00"))
        self.assertEqual(cycle.read("predictions.json"), recorded)


class CloseVerificationTests(TestCase):
    def test_errors_intervals_and_unlocked_exclusion(self):
        close = {"date": DAY, "close": 10100}
        previous = {"date": "2026-11-25", "close": 10000}
        prediction = {
            "date": DAY,
            "status": "locked",
            "direction": "偏多",
            "flat_pct": .30,
            "captured_at": taipei_at("08:40:10").isoformat(),
            "cutoff_at": taipei_at("08:45:00").isoformat(),
            "locked_at": taipei_at("08:45:12").isoformat(),
            "estimated_close": 10200,
            "estimated_change_pct": 2,
            "estimated_range": {"low": 10100, "high": 10300},
        }
        hit = cycle.verify(close, previous, prediction)
        self.assertEqual(hit["status"], "correct")
        self.assertEqual(hit["index_error_points"], -100)
        self.assertEqual(hit["change_error_pp"], -1)
        self.assertTrue(hit["range_hit"])
        miss = cycle.verify({**close, "close": 9900}, previous, prediction)
        self.assertEqual(miss["status"], "incorrect")
        self.assertFalse(miss["range_hit"])
        unlocked = cycle.verify(close, previous, None)
        self.assertEqual(unlocked["status"], "unlocked")
        self.assertEqual(unlocked["lock_status"], "missed")
        legacy = {key: value for key, value in prediction.items() if key != "estimated_range"}
        old_result = cycle.verify(close, previous, legacy)
        self.assertNotIn("range_hit", old_result)
        summary = cycle.stats([hit, miss, unlocked, old_result])
        self.assertEqual(summary["valid"], 3)
        self.assertEqual(summary["range_valid"], 2)
        self.assertEqual(summary["range_accuracy"], .5)


if __name__ == "__main__":
    main()
