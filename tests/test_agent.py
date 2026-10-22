"""Tests for the agent's language rules.

These are pure functions, so they run without the MCP server::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import (  # noqa: E402
    build_aliases,
    detect_intent,
    detect_mood,
    parse_interval_minutes,
    resolve_speaker,
)

ROSTER = [
    {"name": "Elena Whitfield", "relation": "mother"},
    {"name": "Samuel Whitfield", "relation": "father"},
    {"name": "Priya Raman", "relation": "sister"},
]


class IntentTests(unittest.TestCase):
    def assertIntent(self, utterance: str, expected: str) -> None:
        intent, _ = detect_intent(utterance)
        self.assertEqual(intent, expected, f"{utterance!r} -> {intent!r}")

    # -- routine check-ins ------------------------------------------------
    def test_routine_checkins(self) -> None:
        for utterance in (
            "I'm fine",
            "I am fine thanks",
            "Feeling good today",
            "All good here",
            "Everything is fine",
            "Yes",
            "No problems",
        ):
            self.assertIntent(utterance, "routine_checkin")

    # -- help requests ----------------------------------------------------
    def test_explicit_help_requests(self) -> None:
        for utterance in (
            "I need help",
            "help",
            "Help me please",
            "Please help",
            "I need some help",
        ):
            self.assertIntent(utterance, "help_request")

    def test_help_beats_positive_wording(self) -> None:
        """The safety rule: an explicit request wins over upbeat wording."""
        for utterance in (
            "I'm fine but I need help",
            "Everything is great, but I fell down",
            "No problem, I just need help",
            "I'm good thanks, please help",
        ):
            self.assertIntent(utterance, "help_request")

    def test_symptoms_are_treated_as_help(self) -> None:
        for utterance in (
            "I fell over",
            "I fell down the stairs",
            "I have chest pain",
            "I can't breathe",
            "I'm feeling dizzy",
            "I'm not feeling well",
            "I feel unwell",
            "Something is wrong",
        ):
            self.assertIntent(utterance, "help_request")

    def test_negated_positives_are_treated_as_help(self) -> None:
        for utterance in (
            "I'm not fine",
            "I'm not good",
            "I'm not okay",
            "I don't feel good",
        ):
            self.assertIntent(utterance, "help_request")

    def test_gratitude_is_not_a_help_request(self) -> None:
        self.assertIntent("Thanks for your help", "routine_checkin")
        self.assertIntent("Thank you for the help", "routine_checkin")

    # -- other intents ----------------------------------------------------
    def test_schedule_intent(self) -> None:
        for utterance in (
            "Set a schedule for me",
            "Remind me every hour",
            "I want a check-in every 30 minutes",
            "Make it daily",
        ):
            self.assertIntent(utterance, "schedule")

    def test_status_intent(self) -> None:
        for utterance in (
            "What's the status?",
            "How is everyone?",
            "Who hasn't checked in?",
            "Any alerts?",
        ):
            self.assertIntent(utterance, "status_query")

    def test_unknown_intent(self) -> None:
        self.assertIntent("The weather is nice today", "unknown")
        self.assertIntent("", "unknown")


class MoodTests(unittest.TestCase):
    def test_mood_detection(self) -> None:
        self.assertEqual(detect_mood("I'm great today"), "good")
        self.assertEqual(detect_mood("I'm fine"), "okay")
        self.assertEqual(detect_mood("Feeling a bit tired"), "low")
        self.assertEqual(detect_mood("Hello there"), "unspecified")


class IntervalTests(unittest.TestCase):
    def test_interval_parsing(self) -> None:
        self.assertEqual(parse_interval_minutes("every 30 minutes"), 30)
        self.assertEqual(parse_interval_minutes("every 2 hours"), 120)
        self.assertEqual(parse_interval_minutes("every hour"), 60)
        self.assertEqual(parse_interval_minutes("hourly"), 60)
        self.assertEqual(parse_interval_minutes("daily"), 1440)
        self.assertEqual(parse_interval_minutes("every day"), 1440)
        self.assertEqual(parse_interval_minutes("15 minutes"), 15)
        self.assertIsNone(parse_interval_minutes("set a schedule"))


class SpeakerTests(unittest.TestCase):
    def test_resolve_by_first_name(self) -> None:
        self.assertEqual(resolve_speaker("Hi, Elena here", ROSTER), "Elena Whitfield")

    def test_resolve_by_nickname(self) -> None:
        self.assertEqual(resolve_speaker("It's Sam", ROSTER), "Samuel Whitfield")

    def test_resolve_by_relation(self) -> None:
        self.assertEqual(resolve_speaker("Mum is fine", ROSTER), "Elena Whitfield")
        self.assertEqual(resolve_speaker("Dad checked in", ROSTER), "Samuel Whitfield")

    def test_explicit_speaker_wins(self) -> None:
        self.assertEqual(
            resolve_speaker("I'm fine", ROSTER, explicit="Priya Raman"), "Priya Raman"
        )

    def test_unknown_speaker_returns_none(self) -> None:
        self.assertIsNone(resolve_speaker("All good", ROSTER))

    def test_single_person_roster_assumes_that_person(self) -> None:
        roster = [{"name": "Elena Whitfield", "relation": "mother"}]
        self.assertEqual(resolve_speaker("I'm fine", roster), "Elena Whitfield")

    def test_aliases_are_derived_from_the_roster(self) -> None:
        aliases = build_aliases(ROSTER)
        self.assertEqual(aliases["mum"], "Elena Whitfield")
        self.assertEqual(aliases["dad"], "Samuel Whitfield")
        self.assertEqual(aliases["sam"], "Samuel Whitfield")
        self.assertNotIn("nana", aliases)


if __name__ == "__main__":
    unittest.main(verbosity=2)
