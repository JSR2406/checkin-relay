"""Seed the database with synthetic demo data.

All people, check-ins and notes are fictional. Nothing here describes a real
person, and no real family data should ever be added to this project.

Usage::

    python seed.py            # add the demo family if it is not there yet
    python seed.py --reset    # wipe the database and reseed from scratch
"""

from __future__ import annotations

import argparse
from datetime import timedelta

import db

# Fictional demo family. Names, relations and notes are invented.
FAMILY = [
    {
        "name": "Elena Whitfield",
        "relation": "mother",
        "notes": "Lives alone, prefers a phone call in the morning.",
    },
    {
        "name": "Samuel Whitfield",
        "relation": "father",
        "notes": "Walks the dog every morning around 8am.",
    },
    {
        "name": "Priya Raman",
        "relation": "sister",
        "notes": "Studying for exams this term.",
    },
]

# (person, minutes between check-ins, grace seconds)
SCHEDULES = [
    ("Elena Whitfield", 60, 20),
    ("Samuel Whitfield", 240, 20),
    ("Priya Raman", 180, 20),
]

# (person, minutes ago, status, mood, message)
HISTORY = [
    ("Elena Whitfield", 95, "ok", "good", "Slept well, had my tea."),
    ("Samuel Whitfield", 200, "ok", "okay", "Knee was stiff but fine."),
    ("Elena Whitfield", 155, "ok", "good", "Walked to the shops and back."),
    ("Priya Raman", 40, "ok", "great", "Finished the last chapter!"),
    ("Samuel Whitfield", 420, "ok", "good", "Dog is being slow on walks."),
    ("Elena Whitfield", 215, "ok", "okay", "A bit tired today."),
    ("Priya Raman", 120, "ok", "good", "Library was busy, try again later."),
    ("Samuel Whitfield", 600, "ok", "good", "All good here."),
]


def reset() -> None:
    """Drop every table so the demo starts from a clean, known state."""
    with db.get_conn() as conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS events;
            DROP TABLE IF EXISTS alerts;
            DROP TABLE IF EXISTS schedules;
            DROP TABLE IF EXISTS checkins;
            DROP TABLE IF EXISTS people;
            """
        )
    db.init_db()


def seed() -> None:
    db.init_db()

    for person in FAMILY:
        db.add_person(**person)

    for name, interval_minutes, grace in SCHEDULES:
        db.set_schedule(
            person_name=name,
            interval_seconds=interval_minutes * 60,
            grace_seconds=grace,
        )

    now = db.utcnow()
    for name, minutes_ago, status, mood, message in HISTORY:
        db.record_checkin(
            person_name=name,
            status=status,
            message=message,
            mood=mood,
            source="seed",
            checked_at=now - timedelta(minutes=minutes_ago),
        )

    print(f"Seeded {len(FAMILY)} people, {len(SCHEDULES)} schedules, {len(HISTORY)} check-ins")
    print(f"Database: {db.DB_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Check-in Relay database")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop all tables and reseed from scratch",
    )
    args = parser.parse_args()

    if args.reset:
        reset()
    seed()


if __name__ == "__main__":
    main()
