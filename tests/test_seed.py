"""Tests for the demo seed data.

The seeded state is the first thing a reviewer sees after ``python seed.py``, so
it has to be coherent: everyone calm, every schedule anchored to that person's
newest check-in, and no alert waiting to appear out of nowhere.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Never touch the working database: fall back to a throwaway file when another
# test module has not already pointed the data layer somewhere safe.
os.environ.setdefault(
    "CHECKIN_DB",
    str(Path(tempfile.mkdtemp(prefix="checkin-relay-seed-")) / "seed.db"),
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import seed  # noqa: E402

_REAL_DB = (Path(__file__).resolve().parent.parent / "checkin.db").resolve()


class SeedTests(unittest.TestCase):
    def setUp(self) -> None:
        if Path(db.DB_PATH).resolve() == _REAL_DB:
            self.skipTest("refusing to reseed the working database")
        seed.reset()
        seed.seed()

    def test_every_person_starts_ok(self) -> None:
        states = sorted(
            (person["name"], person["state"])
            for person in db.dashboard_snapshot()["people"]
        )
        self.assertEqual(
            states,
            [
                ("Elena Whitfield", "ok"),
                ("Priya Raman", "ok"),
                ("Samuel Whitfield", "ok"),
            ],
        )

    def test_schedule_is_anchored_to_the_newest_checkin(self) -> None:
        for person in db.dashboard_snapshot()["people"]:
            with self.subTest(person=person["name"]):
                schedule = person["schedule"]
                self.assertIsNotNone(schedule)
                self.assertGreater(
                    schedule["next_due"], person["last_checkin"]["checked_at"]
                )
                self.assertFalse(schedule["past_due"])

    def test_seeding_opens_no_alerts(self) -> None:
        snapshot = db.dashboard_snapshot()
        self.assertEqual(snapshot["open_alerts"], [])
        self.assertEqual(db.detect_missed_checkins(), [])


if __name__ == "__main__":
    unittest.main()
