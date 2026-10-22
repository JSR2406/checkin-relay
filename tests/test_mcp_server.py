"""Tests for the MCP tool surface.

These call the registered Python tool functions directly - no network - so a
clean clone can verify the tools without starting the server::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault(
    "CHECKIN_DB",
    str(Path(tempfile.mkdtemp(prefix="checkin-relay-mcp-")) / "mcp.db"),
)
os.environ.setdefault("GRACE_SECONDS", "20")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import mcp_server  # noqa: E402

REQUIRED_TOOLS = {"log_checkin", "get_status", "schedule_checkin", "escalate"}


class ToolRegistrationTests(unittest.TestCase):
    def test_required_tools_are_registered(self) -> None:
        tools = asyncio.run(mcp_server.server.list_tools())
        names = {tool.name for tool in tools}
        self.assertTrue(REQUIRED_TOOLS.issubset(names), sorted(names))
        self.assertIn("resolve_alert", names)


class ScheduleToolTests(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        db.clear_all()
        db.add_person("Elena Whitfield", "mother")

    def test_fractional_minutes_become_seconds(self) -> None:
        """The dashboard demo control asks for 15 seconds = 0.25 minutes."""
        result = mcp_server.schedule_checkin("Elena Whitfield", 0.25, 5)
        self.assertEqual(result.interval_seconds, 15)
        self.assertEqual(result.grace_seconds, 5)
        self.assertTrue(result.active)

    def test_whole_minutes_still_work(self) -> None:
        result = mcp_server.schedule_checkin("Elena Whitfield", 30, 20)
        self.assertEqual(result.interval_seconds, 1800)

    def test_zero_interval_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            mcp_server.schedule_checkin("Elena Whitfield", 0)


class CheckinToolTests(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        db.clear_all()
        db.add_person("Elena Whitfield", "mother")

    def test_log_checkin_returns_the_next_due_time(self) -> None:
        mcp_server.schedule_checkin("Elena Whitfield", 1, 5)
        result = mcp_server.log_checkin(
            "Elena Whitfield", status="ok", message="Fine", source="test"
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.person, "Elena Whitfield")
        self.assertIsNotNone(result.next_due)

    def test_get_status_filters_to_one_person(self) -> None:
        db.add_person("Samuel Whitfield", "father")
        result = mcp_server.get_status("Elena Whitfield")
        self.assertEqual([person["name"] for person in result.people], ["Elena Whitfield"])

    def test_escalate_is_idempotent_while_open(self) -> None:
        first = mcp_server.escalate("Elena Whitfield", "Asked for help")
        second = mcp_server.escalate("Elena Whitfield", "Asked again")
        self.assertEqual(first.alert_id, second.alert_id)
        self.assertFalse(second.created)


if __name__ == "__main__":
    unittest.main(verbosity=2)
