"""Check-in Relay MCP server.

Exposes the family check-in domain as four MCP tools over Streamable HTTP on
port 8000:

    log_checkin     record a check-in (and recover an open missed-check-in alert)
    get_status      read the current state of the family
    schedule_checkin  set or update a person's check-in schedule
    escalate        raise an immediate help-request alert

Run it with::

    python mcp_server.py

The server binds to 127.0.0.1 by default. It has no authentication, so it must
not be exposed publicly without adding one.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse

import db

MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MONITOR_INTERVAL_SECONDS = float(os.getenv("MONITOR_INTERVAL_SECONDS", "5"))

INSTRUCTIONS = """
Check-in Relay is a voice-first wellbeing check-in service for a small family.

Typical flow:
  1. schedule_checkin(person, interval_minutes) sets how often someone must check in.
  2. log_checkin(person, status, message) records that they did check in.
  3. get_status(person) reads who is overdue and which alerts are open.
  4. escalate(person, reason) raises an immediate help-request alert.

An explicit request for help always wins over a routine check-in: escalate
before you treat a positive-sounding message as sufficient. This tool is a
wellbeing reminder, not a medical service and not a replacement for emergency
services.
""".strip()

async def _monitor_loop() -> None:
    """Watch the schedules and raise missed-check-in alerts.

    The monitor lives in the MCP server so every write to the domain goes
    through MCP, and so overdue detection keeps working even if the web app
    is not running.
    """
    while True:
        try:
            await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
            created = await asyncio.to_thread(db.detect_missed_checkins)
            for alert in created:
                if alert.get("created"):
                    print(
                        f"[monitor] missed check-in: {alert['person_name']} "
                        f"(alert #{alert['id']})"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - keep the monitor alive
            print(f"[monitor] scan failed: {exc}")


@asynccontextmanager
async def app_lifespan(app: MCPServer) -> AsyncIterator[None]:
    """Prepare the database and run the missed-check-in monitor."""
    db.init_db()
    task = asyncio.create_task(_monitor_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


server = MCPServer(
    name="checkin-relay",
    version="1.0.0",
    instructions=INSTRUCTIONS,
    lifespan=app_lifespan,
)


# --------------------------------------------------------------------------
# Tool result models
# --------------------------------------------------------------------------
class ToolResult(BaseModel):
    """Base for tool outputs. Extra fields are preserved so the data layer can
    pass through details without every tool declaring them."""

    model_config = ConfigDict(extra="allow")

    ok: bool = True


class LogCheckinResult(ToolResult):
    person: str
    checked_at: str
    status: str
    mood: str = "unspecified"
    message: str = ""
    source: str = "voice"
    next_due: str | None = None
    recovered_alerts: list[dict[str, Any]] = []


class StatusResult(ToolResult):
    generated_at: str
    people: list[dict[str, Any]]
    open_alerts: list[dict[str, Any]]
    open_alert_count: int = 0
    recent_checkins: list[dict[str, Any]] = []
    recent_events: list[dict[str, Any]] = []


class ScheduleResult(ToolResult):
    person: str
    interval_seconds: int
    grace_seconds: int
    next_due: str
    active: bool = True


class EscalateResult(ToolResult):
    person: str
    alert_id: int
    kind: str
    severity: str
    status: str
    message: str
    created_at: str
    created: bool = True
    notification: str = (
        "Recorded on the family dashboard only. No SMS, call or push "
        "notification was sent."
    )


class ResolveAlertResult(ToolResult):
    alert_id: int
    person: str
    kind: str
    status: str
    resolved_at: str
    resolved_by: str


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
@server.tool(
    name="log_checkin",
    title="Log a check-in",
    description=(
        "Record that a family member checked in. Use status='ok' for a routine "
        "check-in and status='help' when they explicitly asked for help - a "
        "request for help must take precedence over any upbeat wording. "
        "Logging a check-in clears an open missed-check-in alert for that person."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
def log_checkin(
    person: str,
    status: str = "ok",
    message: str = "",
    mood: str = "unspecified",
    source: str = "voice",
) -> LogCheckinResult:
    """Store a check-in for `person` and report the resulting schedule state."""
    result = db.record_checkin(
        person_name=person,
        status=status,
        message=message,
        mood=mood,
        source=source,
    )
    return LogCheckinResult(**result)


@server.tool(
    name="get_status",
    title="Get family status",
    description=(
        "Read the current state of the family: every person, their last "
        "check-in, their schedule and whether they are overdue, plus all open "
        "alerts. Optionally pass a person name to narrow the result."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
def get_status(person: str | None = None) -> StatusResult:
    """Return the family dashboard snapshot, optionally for one person."""
    snapshot = db.dashboard_snapshot()
    if person:
        wanted = person.strip().lower()
        snapshot["people"] = [
            entry
            for entry in snapshot["people"]
            if entry["name"].strip().lower() == wanted
        ]
        if not snapshot["people"]:
            raise ValueError(f"Unknown person: {person}")
        snapshot["open_alerts"] = [
            alert
            for alert in snapshot["open_alerts"]
            if alert["person_name"].strip().lower() == wanted
        ]
        snapshot["open_alert_count"] = len(snapshot["open_alerts"])
    return StatusResult(**snapshot)


@server.tool(
    name="schedule_checkin",
    title="Schedule recurring check-ins",
    description=(
        "Set or update how often a family member must check in. The schedule is "
        "re-anchored to each successful check-in. A grace period allows a late "
        "check-in without raising an alert."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
def schedule_checkin(
    person: str,
    interval_minutes: int = 60,
    grace_seconds: int | None = None,
) -> ScheduleResult:
    """Create or update the check-in schedule for `person`."""
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be greater than zero")
    row = db.set_schedule(
        person_name=person,
        interval_seconds=interval_minutes * 60,
        grace_seconds=grace_seconds,
    )
    return ScheduleResult(
        person=row["person_name"],
        interval_seconds=row["interval_seconds"],
        grace_seconds=row["grace_seconds"],
        next_due=row["next_due"],
        active=bool(row["active"]),
    )


@server.tool(
    name="escalate",
    title="Escalate a help request",
    description=(
        "Raise an immediate help-request alert for a family member. Call this "
        "whenever someone explicitly asks for help, regardless of how positive "
        "the rest of their message sounds. Repeated calls reuse the existing "
        "open alert instead of creating duplicates."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
def escalate(person: str, reason: str = "Requested help") -> EscalateResult:
    """Open a critical help-request alert for `person`."""
    alert = db.escalate(person_name=person, reason=reason)
    return EscalateResult(
        person=alert["person_name"],
        alert_id=alert["id"],
        kind=alert["kind"],
        severity=alert["severity"],
        status=alert["status"],
        message=alert["message"],
        created_at=alert["created_at"],
        created=bool(alert["created"]),
    )


@server.tool(
    name="resolve_alert",
    title="Resolve an alert",
    description=(
        "Close an open alert once the family has dealt with it. Used by the "
        "family dashboard. This is an addition to the four tools in the "
        "project brief, so the dashboard never has to write to the database "
        "directly."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
def resolve_alert(alert_id: int, resolved_by: str = "family") -> ResolveAlertResult:
    """Mark the alert with `alert_id` as resolved."""
    alert = db.resolve_alert(alert_id=alert_id, resolved_by=resolved_by)
    return ResolveAlertResult(
        alert_id=alert["id"],
        person=alert["person_name"],
        kind=alert["kind"],
        status=alert["status"],
        resolved_at=alert["resolved_at"],
        resolved_by=alert["resolved_by"],
    )


# --------------------------------------------------------------------------
# Operational routes (outside the MCP protocol)
# --------------------------------------------------------------------------
@server.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Liveness probe plus the protocol versions this build can negotiate."""
    from mcp_types.version import (
        HANDSHAKE_PROTOCOL_VERSIONS,
        LATEST_PROTOCOL_VERSION,
        MODERN_PROTOCOL_VERSIONS,
    )

    return JSONResponse(
        {
            "status": "ok",
            "server": "checkin-relay",
            "version": server.version,
            "protocol_versions": {
                "latest": LATEST_PROTOCOL_VERSION,
                "handshake": list(HANDSHAKE_PROTOCOL_VERSIONS),
                "modern": list(MODERN_PROTOCOL_VERSIONS),
            },
            "tools": [
                "log_checkin",
                "get_status",
                "schedule_checkin",
                "escalate",
                "resolve_alert",
            ],
            "database": str(db.DB_PATH),
        }
    )


def main() -> None:
    db.init_db()
    print(f"Check-in Relay MCP server starting on http://{MCP_HOST}:{MCP_PORT}/mcp")
    asyncio.run(
        server.run_streamable_http_async(
            host=MCP_HOST,
            port=MCP_PORT,
            streamable_http_path="/mcp",
        )
    )


if __name__ == "__main__":
    main()
