"""Tests for the Check-in Relay data layer.

Standard library only - no extra test dependency needed for a clean clone::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

# Point the data layer at a throwaway database before it is imported.
_TMP_DIR = tempfile.mkdtemp(prefix="checkin-relay-tests-")
os.environ["CHECKIN_DB"] = str(Path(_TMP_DIR) / "test.db")
os.environ["GRACE_SECONDS"] = "20"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


class DataLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        db.clear_all()
        self.elena = db.add_person("Elena Whitfield", "mother")
        self.samuel = db.add_person("Samuel Whitfield", "father")
        # Second precision matches how timestamps are stored.
        self.now = db.utcnow().replace(microsecond=0)

    # -- people -----------------------------------------------------------
    def test_add_person_is_idempotent_by_name(self) -> None:
        db.add_person("Elena Whitfield", "duplicate")
        self.assertEqual(len(db.list_people()), 2)

    def test_unknown_person_raises(self) -> None:
        with self.assertRaises(KeyError):
            db.record_checkin("Nobody At All", status="ok")

    def test_invalid_status_raises(self) -> None:
        with self.assertRaises(ValueError):
            db.record_checkin("Elena Whitfield", status="maybe")

    # -- check-ins --------------------------------------------------------
    def test_checkin_persists(self) -> None:
        db.record_checkin(
            "Elena Whitfield",
            status="ok",
            message="Feeling fine",
            mood="good",
            source="voice",
        )
        latest = db.latest_checkin(self.elena["id"])
        self.assertIsNotNone(latest)
        self.assertEqual(latest["status"], "ok")
        self.assertEqual(latest["message"], "Feeling fine")
        self.assertEqual(latest["source"], "voice")

    def test_help_checkin_is_stored_as_help(self) -> None:
        db.record_checkin("Elena Whitfield", status="help", message="I need help")
        latest = db.latest_checkin(self.elena["id"])
        self.assertEqual(latest["status"], "help")

    # -- schedules --------------------------------------------------------
    def test_schedule_sets_next_due_from_interval(self) -> None:
        row = db.set_schedule(
            "Elena Whitfield", interval_seconds=3600, grace_seconds=20,
            start_at=self.now,
        )
        self.assertEqual(row["interval_seconds"], 3600)
        self.assertEqual(row["grace_seconds"], 20)
        self.assertEqual(
            db.from_iso(row["next_due"]),
            self.now + timedelta(seconds=3600),
        )

    def test_checkin_advances_the_schedule(self) -> None:
        db.set_schedule(
            "Elena Whitfield", interval_seconds=3600, grace_seconds=20,
            start_at=self.now,
        )
        db.record_checkin("Elena Whitfield", status="ok", checked_at=self.now)
        schedule = db.get_schedule("Elena Whitfield")
        self.assertEqual(
            db.from_iso(schedule["next_due"]),
            self.now + timedelta(seconds=3600),
        )

    def test_schedule_rejects_non_positive_interval(self) -> None:
        with self.assertRaises(ValueError):
            db.set_schedule("Elena Whitfield", interval_seconds=0)

    # -- missed check-in detection ---------------------------------------
    def test_no_alert_before_the_grace_window_ends(self) -> None:
        db.set_schedule(
            "Elena Whitfield", interval_seconds=60, grace_seconds=20,
            start_at=self.now,
        )
        # 10s past the due time, still inside the 20s grace window.
        created = db.detect_missed_checkins(now=self.now + timedelta(seconds=70))
        self.assertEqual(created, [])
        self.assertEqual(db.open_alert_count(), 0)

    def test_alert_opens_after_the_grace_window(self) -> None:
        db.set_schedule(
            "Elena Whitfield", interval_seconds=60, grace_seconds=20,
            start_at=self.now,
        )
        created = db.detect_missed_checkins(now=self.now + timedelta(seconds=90))
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["kind"], "missed_checkin")
        self.assertEqual(created[0]["severity"], "warning")
        self.assertEqual(db.open_alert_count(), 1)

    def test_checkin_inside_the_grace_window_prevents_an_alert(self) -> None:
        db.set_schedule(
            "Elena Whitfield", interval_seconds=60, grace_seconds=20,
            start_at=self.now,
        )
        db.record_checkin(
            "Elena Whitfield", status="ok", checked_at=self.now + timedelta(seconds=70)
        )
        created = db.detect_missed_checkins(now=self.now + timedelta(seconds=90))
        self.assertEqual(created, [])

    def test_repeated_scans_do_not_duplicate_the_alert(self) -> None:
        db.set_schedule(
            "Elena Whitfield", interval_seconds=60, grace_seconds=20,
            start_at=self.now,
        )
        first = db.detect_missed_checkins(now=self.now + timedelta(seconds=90))
        second = db.detect_missed_checkins(now=self.now + timedelta(seconds=95))
        third = db.detect_missed_checkins(now=self.now + timedelta(seconds=120))

        self.assertTrue(first[0]["created"])
        self.assertFalse(second[0]["created"])
        self.assertFalse(third[0]["created"])
        self.assertEqual(db.open_alert_count(), 1)

    def test_late_checkin_recovers_the_missed_alert(self) -> None:
        db.set_schedule(
            "Elena Whitfield", interval_seconds=60, grace_seconds=20,
            start_at=self.now,
        )
        db.detect_missed_checkins(now=self.now + timedelta(seconds=90))
        self.assertEqual(db.open_alert_count(), 1)

        result = db.record_checkin(
            "Elena Whitfield", status="ok", checked_at=self.now + timedelta(seconds=100)
        )
        self.assertEqual(len(result["recovered_alerts"]), 1)
        self.assertEqual(db.open_alert_count(), 0)

        # And the schedule is re-anchored to the late check-in.
        schedule = db.get_schedule("Elena Whitfield")
        self.assertEqual(
            db.from_iso(schedule["next_due"]),
            self.now + timedelta(seconds=100 + 60),
        )

    # -- escalation -------------------------------------------------------
    def test_escalate_opens_a_critical_alert(self) -> None:
        alert = db.escalate("Elena Whitfield", reason="Asked for help")
        self.assertEqual(alert["kind"], "help_request")
        self.assertEqual(alert["severity"], "critical")
        self.assertEqual(alert["status"], "open")
        self.assertTrue(alert["created"])

    def test_repeated_escalation_reuses_the_open_alert(self) -> None:
        first = db.escalate("Elena Whitfield", reason="Asked for help")
        second = db.escalate("Elena Whitfield", reason="Asked again")
        third = db.escalate("Elena Whitfield", reason="Asked a third time")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["id"], third["id"])
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertFalse(third["created"])
        self.assertEqual(db.open_alert_count(), 1)

    def test_help_alert_survives_a_later_routine_checkin(self) -> None:
        """An explicit help request must not be downgraded by a routine check-in."""
        db.escalate("Elena Whitfield", reason="Asked for help")
        db.record_checkin("Elena Whitfield", status="ok", message="all fine now")

        open_alerts = db.list_alerts(status="open")
        self.assertEqual(len(open_alerts), 1)
        self.assertEqual(open_alerts[0]["kind"], "help_request")

    def test_help_and_missed_alerts_can_coexist_for_one_person(self) -> None:
        db.set_schedule(
            "Elena Whitfield", interval_seconds=60, grace_seconds=20,
            start_at=self.now,
        )
        db.detect_missed_checkins(now=self.now + timedelta(seconds=90))
        db.escalate("Elena Whitfield", reason="Asked for help")

        kinds = {alert["kind"] for alert in db.list_alerts(status="open")}
        self.assertEqual(kinds, {"missed_checkin", "help_request"})

    def test_resolve_alert_closes_it(self) -> None:
        alert = db.escalate("Elena Whitfield", reason="Asked for help")
        resolved = db.resolve_alert(alert["id"], resolved_by="Priya")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolved_by"], "Priya")
        self.assertEqual(db.open_alert_count(), 0)

        # A new escalation after resolution opens a fresh alert.
        reopened = db.escalate("Elena Whitfield", reason="Asked for help")
        self.assertNotEqual(reopened["id"], alert["id"])
        self.assertTrue(reopened["created"])

    # -- dashboard --------------------------------------------------------
    def test_dashboard_reports_state_per_person(self) -> None:
        db.set_schedule(
            "Elena Whitfield", interval_seconds=60, grace_seconds=20,
            start_at=self.now,
        )
        db.detect_missed_checkins(now=self.now + timedelta(seconds=90))

        snapshot = db.dashboard_snapshot()
        by_name = {entry["name"]: entry for entry in snapshot["people"]}

        self.assertEqual(by_name["Elena Whitfield"]["state"], "missed")
        self.assertEqual(by_name["Samuel Whitfield"]["state"], "never_checked_in")
        self.assertEqual(snapshot["open_alert_count"], 1)
        self.assertIn("generated_at", snapshot)

    def test_dashboard_marks_a_late_but_not_yet_alerted_schedule(self) -> None:
        """Due time passed, grace window still open: wait, do not alert."""
        db.set_schedule(
            "Elena Whitfield", interval_seconds=60, grace_seconds=20,
            start_at=self.now - timedelta(seconds=70),
        )
        snapshot = db.dashboard_snapshot()
        entry = snapshot["people"][0]
        self.assertEqual(entry["state"], "grace")
        self.assertTrue(entry["schedule"]["past_due"])
        self.assertFalse(entry["schedule"]["overdue"])

    def test_dashboard_marks_an_expired_grace_window(self) -> None:
        """Grace window closed without a check-in: overdue, awaiting the scanner."""
        db.set_schedule(
            "Elena Whitfield", interval_seconds=60, grace_seconds=20,
            start_at=self.now - timedelta(seconds=100),
        )
        snapshot = db.dashboard_snapshot()
        entry = snapshot["people"][0]
        self.assertEqual(entry["state"], "overdue")
        self.assertTrue(entry["schedule"]["overdue"])

    # -- concurrency ------------------------------------------------------
    def test_simultaneous_escalations_open_one_alert(self) -> None:
        """Two requests racing each other must not create two alerts."""
        import threading

        results: list[dict] = []
        errors: list[Exception] = []

        def escalate_once() -> None:
            try:
                results.append(db.escalate("Elena Whitfield", reason="race"))
            except Exception as exc:  # pragma: no cover - surfaced by assert
                errors.append(exc)

        threads = [threading.Thread(target=escalate_once) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 5)
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertEqual(db.open_alert_count(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
