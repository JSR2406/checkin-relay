"""SQLite data layer for Check-in Relay.

Shared by the MCP server (port 8000) and the FastAPI web app (port 8001), so
the connection is opened per operation and the database runs in WAL mode to
allow the two processes to read and write concurrently.

All timestamps are stored as ISO-8601 UTC strings so they sort and compare
lexicographically.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(os.getenv("CHECKIN_DB", "checkin.db")).resolve()
DEFAULT_GRACE_SECONDS = int(os.getenv("GRACE_SECONDS", "20"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    relation    TEXT    NOT NULL DEFAULT 'family',
    timezone    TEXT    NOT NULL DEFAULT 'UTC',
    notes       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS checkins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    checked_at  TEXT    NOT NULL,
    status      TEXT    NOT NULL CHECK (status IN ('ok', 'help')),
    mood        TEXT    NOT NULL DEFAULT 'unspecified',
    message     TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL DEFAULT 'voice'
);
CREATE INDEX IF NOT EXISTS idx_checkins_person_ts ON checkins (person_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS schedules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id         INTEGER NOT NULL UNIQUE REFERENCES people(id) ON DELETE CASCADE,
    interval_seconds  INTEGER NOT NULL CHECK (interval_seconds > 0),
    grace_seconds     INTEGER NOT NULL DEFAULT 20,
    next_due          TEXT    NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL CHECK (kind IN ('missed_checkin', 'help_request')),
    severity    TEXT    NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'warning', 'critical')),
    status      TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    message     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT    NOT NULL DEFAULT ''
);
-- At most one open alert per (person, kind). This is the database-level guard
-- against duplicate alerts, including from simultaneous requests.
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_single_open_per_kind
    ON alerts (person_id, kind) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status, created_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    person_id   INTEGER REFERENCES people(id) ON DELETE SET NULL,
    detail      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at DESC);
"""


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    """Serialise a datetime to a sortable ISO-8601 UTC string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    """Parse a stored ISO-8601 timestamp back into an aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    """Open a configured connection. WAL keeps the two processes in step."""
    # The default isolation level defers BEGIN until the first write, so a
    # multi-statement operation either lands completely or not at all.
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Context manager yielding a connection with commit/rollback handling."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the schema if it does not exist yet."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def clear_all() -> None:
    """Delete every row but keep the schema. Used by the tests and by reseeding."""
    with get_conn() as conn:
        conn.executescript(
            """
            DELETE FROM events;
            DELETE FROM alerts;
            DELETE FROM schedules;
            DELETE FROM checkins;
            DELETE FROM people;
            """
        )


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------
def add_person(
    name: str,
    relation: str = "family",
    notes: str = "",
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    """Insert a person, or return the existing row when the name is known."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("A person needs a name")
    now = to_iso(utcnow())
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM people WHERE name = ?", (clean_name,)).fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT INTO people (name, relation, timezone, notes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (clean_name, relation, timezone_name, notes, now),
            )
            row = conn.execute(
                "SELECT * FROM people WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)


def list_people() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM people ORDER BY name").fetchall()
        return [dict(row) for row in rows]


def get_person(name: str) -> Optional[dict[str, Any]]:
    """Look a person up by name, case-insensitively."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM people WHERE name = ? COLLATE NOCASE", (name.strip(),)
        ).fetchone()
        return dict(row) if row else None


def _require_person(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM people WHERE name = ? COLLATE NOCASE", (name.strip(),)
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown person: {name.strip()}")
    return row


# --------------------------------------------------------------------------
# Check-ins
# --------------------------------------------------------------------------
def record_checkin(
    person_name: str,
    status: str = "ok",
    message: str = "",
    mood: str = "unspecified",
    source: str = "voice",
    checked_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Record a check-in and resolve any missed-check-in alert it recovers from.

    Returns a summary including whether a late check-in cleared an open alert.
    """
    status = status.strip().lower()
    if status not in ("ok", "help"):
        raise ValueError("status must be 'ok' or 'help'")

    moment = checked_at or utcnow()
    stamp = to_iso(moment)

    with get_conn() as conn:
        person = _require_person(conn, person_name)
        person_id = person["id"]

        conn.execute(
            "INSERT INTO checkins (person_id, checked_at, status, mood, message, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, stamp, status, mood, message, source),
        )

        recovered = _resolve_alerts(
            conn,
            person_id,
            kind="missed_checkin",
            resolved_by="late check-in",
        )

        # Keep the schedule anchored to the most recent check-in.
        schedule = conn.execute(
            "SELECT * FROM schedules WHERE person_id = ?", (person_id,)
        ).fetchone()
        next_due = None
        if schedule is not None and schedule["active"]:
            next_due = to_iso(moment + timedelta(seconds=schedule["interval_seconds"]))
            conn.execute(
                "UPDATE schedules SET next_due = ?, updated_at = ? WHERE id = ?",
                (next_due, stamp, schedule["id"]),
            )

        _log_event(
            conn,
            event_type="checkin",
            person_id=person_id,
            detail=f"status={status} source={source} message={message!r}",
        )
        if recovered:
            _log_event(
                conn,
                event_type="alert_resolved",
                person_id=person_id,
                detail=f"resolved {len(recovered)} missed-check-in alert(s) by check-in",
            )

        return {
            "person": person["name"],
            "checked_at": stamp,
            "status": status,
            "mood": mood,
            "message": message,
            "source": source,
            "next_due": next_due,
            "recovered_alerts": [dict(row) for row in recovered],
        }


def latest_checkin(person_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM checkins WHERE person_id = ? ORDER BY checked_at DESC, id DESC"
            " LIMIT 1",
            (person_id,),
        ).fetchone()
        return dict(row) if row else None


def recent_checkins(limit: int = 20) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, p.name AS person_name FROM checkins c"
            " JOIN people p ON p.id = c.person_id"
            " ORDER BY c.checked_at DESC, c.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------
def set_schedule(
    person_name: str,
    interval_seconds: int,
    grace_seconds: Optional[int] = None,
    start_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Create or update the check-in schedule for a person.

    ``start_at`` anchors the first due time; it exists so tests can place a
    schedule in the past without sleeping.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be greater than zero")
    grace = DEFAULT_GRACE_SECONDS if grace_seconds is None else int(grace_seconds)
    if grace < 0:
        raise ValueError("grace_seconds cannot be negative")

    now = start_at or utcnow()
    stamp = to_iso(now)
    next_due = to_iso(now + timedelta(seconds=interval_seconds))

    with get_conn() as conn:
        person = _require_person(conn, person_name)
        person_id = person["id"]
        conn.execute(
            "INSERT INTO schedules"
            " (person_id, interval_seconds, grace_seconds, next_due, active,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 1, ?, ?)"
            " ON CONFLICT(person_id) DO UPDATE SET"
            "  interval_seconds = excluded.interval_seconds,"
            "  grace_seconds = excluded.grace_seconds,"
            "  next_due = excluded.next_due,"
            "  active = 1,"
            "  updated_at = excluded.updated_at",
            (person_id, interval_seconds, grace, next_due, stamp, stamp),
        )
        row = conn.execute(
            "SELECT s.*, p.name AS person_name FROM schedules s"
            " JOIN people p ON p.id = s.person_id WHERE s.person_id = ?",
            (person_id,),
        ).fetchone()
        _log_event(
            conn,
            event_type="schedule_set",
            person_id=person_id,
            detail=f"every {interval_seconds}s grace {grace}s",
        )
        return dict(row)


def list_schedules(active_only: bool = True) -> list[dict[str, Any]]:
    query = (
        "SELECT s.*, p.name AS person_name FROM schedules s"
        " JOIN people p ON p.id = s.person_id"
    )
    if active_only:
        query += " WHERE s.active = 1"
    query += " ORDER BY p.name"
    with get_conn() as conn:
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]


def get_schedule(person_name: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT s.*, p.name AS person_name FROM schedules s"
            " JOIN people p ON p.id = s.person_id"
            " WHERE p.name = ? COLLATE NOCASE",
            (person_name.strip(),),
        ).fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
def _open_alert(
    conn: sqlite3.Connection,
    person_id: int,
    kind: str,
    severity: str,
    message: str,
) -> tuple[dict[str, Any], bool]:
    """Open an alert, or return the existing open one for the same kind.

    The partial unique index guarantees a single open alert per (person, kind)
    even if two requests race each other.
    """
    now = to_iso(utcnow())
    try:
        cursor = conn.execute(
            "INSERT INTO alerts (person_id, kind, severity, status, message, created_at)"
            " VALUES (?, ?, ?, 'open', ?, ?)",
            (person_id, kind, severity, message, now),
        )
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT a.*, p.name AS person_name FROM alerts a"
            " JOIN people p ON p.id = a.person_id"
            " WHERE a.person_id = ? AND a.kind = ? AND a.status = 'open'",
            (person_id, kind),
        ).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise
        return dict(row), False

    row = conn.execute(
        "SELECT a.*, p.name AS person_name FROM alerts a"
        " JOIN people p ON p.id = a.person_id WHERE a.id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return dict(row), True


def _resolve_alerts(
    conn: sqlite3.Connection,
    person_id: int,
    kind: Optional[str] = None,
    resolved_by: str = "system",
) -> list[dict[str, Any]]:
    """Close every open alert for a person (optionally filtered by kind)."""
    now = to_iso(utcnow())
    if kind is None:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE person_id = ? AND status = 'open'", (person_id,)
        ).fetchall()
        conn.execute(
            "UPDATE alerts SET status = 'resolved', resolved_at = ?, resolved_by = ?"
            " WHERE person_id = ? AND status = 'open'",
            (now, resolved_by, person_id),
        )
    else:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE person_id = ? AND kind = ? AND status = 'open'",
            (person_id, kind),
        ).fetchall()
        conn.execute(
            "UPDATE alerts SET status = 'resolved', resolved_at = ?, resolved_by = ?"
            " WHERE person_id = ? AND kind = ? AND status = 'open'",
            (now, resolved_by, person_id, kind),
        )
    return [dict(row) for row in rows]


def open_alert(
    person_name: str,
    kind: str,
    message: str = "",
    severity: str = "info",
) -> dict[str, Any]:
    """Open an alert for a person, de-duplicating against existing open alerts."""
    if kind not in ("missed_checkin", "help_request"):
        raise ValueError("kind must be 'missed_checkin' or 'help_request'")
    with get_conn() as conn:
        person = _require_person(conn, person_name)
        alert, created = _open_alert(conn, person["id"], kind, severity, message)
        if created:
            _log_event(
                conn,
                event_type="alert_opened",
                person_id=person["id"],
                detail=f"{kind}: {message}",
            )
        return {**alert, "created": created}


def escalate(person_name: str, reason: str = "Requested help") -> dict[str, Any]:
    """Raise an immediate help request alert.

    Always takes precedence: an open help alert is never downgraded by a later
    routine check-in, and repeated requests reuse the same open alert.
    """
    with get_conn() as conn:
        person = _require_person(conn, person_name)
        alert, created = _open_alert(
            conn, person["id"], "help_request", "critical", reason
        )
        _log_event(
            conn,
            event_type="escalation",
            person_id=person["id"],
            detail=f"{reason} (created={created})",
        )
        return {**alert, "created": created}


def resolve_alert(alert_id: int, resolved_by: str = "family") -> dict[str, Any]:
    """Resolve one alert by id."""
    now = to_iso(utcnow())
    with get_conn() as conn:
        row = conn.execute(
            "SELECT a.*, p.name AS person_name FROM alerts a"
            " JOIN people p ON p.id = a.person_id WHERE a.id = ?",
            (alert_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown alert id: {alert_id}")
        conn.execute(
            "UPDATE alerts SET status = 'resolved', resolved_at = ?, resolved_by = ?"
            " WHERE id = ?",
            (now, resolved_by, alert_id),
        )
        updated = conn.execute(
            "SELECT a.*, p.name AS person_name FROM alerts a"
            " JOIN people p ON p.id = a.person_id WHERE a.id = ?",
            (alert_id,),
        ).fetchone()
        _log_event(
            conn,
            event_type="alert_resolved",
            person_id=updated["person_id"],
            detail=f"alert #{alert_id} ({updated['kind']}) resolved by {resolved_by}",
        )
        return dict(updated)


def list_alerts(status: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    query = (
        "SELECT a.*, p.name AS person_name FROM alerts a"
        " JOIN people p ON p.id = a.person_id"
    )
    params: list[Any] = []
    if status is not None:
        query += " WHERE a.status = ?"
        params.append(status)
    query += " ORDER BY a.status = 'open' DESC, a.created_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def open_alert_count() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE status = 'open'"
        ).fetchone()
        return int(row["n"])


# --------------------------------------------------------------------------
# Missed check-in detection
# --------------------------------------------------------------------------
def detect_missed_checkins(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Scan active schedules and open an alert for each overdue check-in.

    A schedule is overdue when the grace window after ``next_due`` has passed
    and no check-in was recorded after the due time.
    """
    moment = now or utcnow()
    created_alerts: list[dict[str, Any]] = []

    with get_conn() as conn:
        schedules = conn.execute(
            "SELECT s.*, p.name AS person_name FROM schedules s"
            " JOIN people p ON p.id = s.person_id"
            " WHERE s.active = 1"
        ).fetchall()

        for schedule in schedules:
            due = from_iso(schedule["next_due"])
            deadline = due + timedelta(seconds=schedule["grace_seconds"])
            if moment <= deadline:
                continue

            last = conn.execute(
                "SELECT checked_at FROM checkins WHERE person_id = ?"
                " ORDER BY checked_at DESC, id DESC LIMIT 1",
                (schedule["person_id"],),
            ).fetchone()
            if last is not None and from_iso(last["checked_at"]) > due:
                continue  # checked in (late but within the window)

            overdue_by = int((moment - deadline).total_seconds())
            message = (
                f"{schedule['person_name']} missed the check-in scheduled for "
                f"{schedule['next_due']} (overdue by {overdue_by}s, "
                f"grace {schedule['grace_seconds']}s)."
            )
            alert, created = _open_alert(
                conn, schedule["person_id"], "missed_checkin", "warning", message
            )
            if created:
                _log_event(
                    conn,
                    event_type="missed_checkin",
                    person_id=schedule["person_id"],
                    detail=message,
                )
            created_alerts.append({**alert, "created": created})

    return created_alerts


# --------------------------------------------------------------------------
# Events / activity feed
# --------------------------------------------------------------------------
def _log_event(
    conn: sqlite3.Connection,
    event_type: str,
    person_id: Optional[int] = None,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO events (created_at, event_type, person_id, detail)"
        " VALUES (?, ?, ?, ?)",
        (to_iso(utcnow()), event_type, person_id, detail),
    )


def recent_events(limit: int = 30) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT e.*, p.name AS person_name FROM events e"
            " LEFT JOIN people p ON p.id = e.person_id"
            " ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Dashboard snapshot
# --------------------------------------------------------------------------
def dashboard_snapshot() -> dict[str, Any]:
    """Everything the family dashboard needs, in one payload."""
    now = utcnow()
    people = list_people()
    schedules = {s["person_id"]: s for s in list_schedules(active_only=False)}
    open_alerts = list_alerts(status="open")
    alerts_by_person: dict[int, list[dict[str, Any]]] = {}
    for alert in open_alerts:
        alerts_by_person.setdefault(alert["person_id"], []).append(alert)

    entries: list[dict[str, Any]] = []
    for person in people:
        person_id = person["id"]
        last = latest_checkin(person_id)
        schedule = schedules.get(person_id)
        schedule_view: Optional[dict[str, Any]] = None
        if schedule is not None:
            due = from_iso(schedule["next_due"])
            deadline = due + timedelta(seconds=schedule["grace_seconds"])
            schedule_view = {
                "next_due": schedule["next_due"],
                "interval_seconds": schedule["interval_seconds"],
                "grace_seconds": schedule["grace_seconds"],
                "active": bool(schedule["active"]),
                "seconds_until_due": int((due - now).total_seconds()),
                "seconds_until_grace_ends": int((deadline - now).total_seconds()),
                # past_due: the due time has passed but the grace window is open.
                # overdue: the grace window has closed and nobody has checked in.
                "past_due": now > due,
                "overdue": now > deadline,
                "grace_ends_at": to_iso(deadline),
            }
        person_alerts = alerts_by_person.get(person_id, [])
        if any(a["kind"] == "help_request" for a in person_alerts):
            state = "help_requested"
        elif person_alerts:
            state = "missed"
        elif schedule_view and schedule_view["overdue"]:
            state = "overdue"
        elif schedule_view and schedule_view["past_due"]:
            state = "grace"
        elif last is None:
            state = "never_checked_in"
        else:
            state = "ok"

        entries.append(
            {
                **person,
                "last_checkin": last,
                "schedule": schedule_view,
                "open_alerts": person_alerts,
                "state": state,
            }
        )

    return {
        "generated_at": to_iso(now),
        "people": entries,
        "open_alerts": open_alerts,
        "recent_checkins": recent_checkins(limit=10),
        "recent_events": recent_events(limit=20),
        "open_alert_count": len(open_alerts),
    }


if __name__ == "__main__":  # pragma: no cover - manual helper
    init_db()
    print(f"Initialised database at {DB_PATH}")
