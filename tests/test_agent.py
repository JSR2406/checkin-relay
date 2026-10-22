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
    LLM_TOOL_SCHEMAS,
    CheckInAgent,
    build_aliases,
    build_llm_client,
    detect_intent,
    detect_mood,
    format_interval,
    intent_for_tool_calls,
    normalise_llm_input,
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
            "Check on me in 15 seconds",
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

    def test_seconds_and_fractional_intervals(self) -> None:
        self.assertEqual(parse_interval_minutes("Check on me in 15 seconds"), 0.25)
        self.assertEqual(parse_interval_minutes("every 30 seconds"), 0.5)
        self.assertEqual(parse_interval_minutes("check in every 0.2 minutes"), 0.2)

    def test_interval_wording(self) -> None:
        self.assertEqual(format_interval(0.25), "15 seconds")
        self.assertEqual(format_interval(30), "30 minutes")
        self.assertEqual(format_interval(120), "2.0 hours")


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


class _FakeMessages:
    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.replies.pop(0)


class _FakeLLM:
    def __init__(self, replies: list[dict]) -> None:
        self.messages = _FakeMessages(replies)


class LLMToolCallTests(unittest.TestCase):
    def test_schema_offers_the_four_required_tools(self) -> None:
        self.assertEqual(
            {schema["name"] for schema in LLM_TOOL_SCHEMAS},
            {"log_checkin", "get_status", "schedule_checkin", "escalate"},
        )

    def test_model_input_is_validated_and_canonicalised(self) -> None:
        call = normalise_llm_input(
            "log_checkin",
            {"person": "elena whitfield", "status": "ok", "message": "I'm fine"},
            ROSTER,
            speaker=None,
            source="web",
        )
        self.assertEqual(call["person"], "Elena Whitfield")
        self.assertEqual(call["source"], "web")

        # A missing person falls back to the selected speaker, never a guess.
        call = normalise_llm_input(
            "schedule_checkin",
            {"interval_minutes": 0.25},
            ROSTER,
            speaker="Priya Raman",
        )
        self.assertEqual(call["person"], "Priya Raman")
        self.assertEqual(call["interval_minutes"], 0.25)

    def test_model_cannot_invent_a_person_or_bad_values(self) -> None:
        for name, arguments in (
            ("log_checkin", {"person": "Nobody", "status": "ok", "message": "hi"}),
            ("log_checkin", {"person": "Elena Whitfield", "status": "fine", "message": "hi"}),
            ("schedule_checkin", {"person": "Elena Whitfield", "interval_minutes": 0}),
            ("schedule_checkin", {"person": "Elena Whitfield", "interval_minutes": 10, "grace_seconds": 999}),
            ("resolve_alert", {"alert_id": 1}),
            ("escalate", {"person": "Elena Whitfield", "reason": " "}),
        ):
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaises(ValueError):
                    normalise_llm_input(name, arguments, ROSTER, speaker=None)

    def test_tool_calls_map_to_the_rules_intent_names(self) -> None:
        self.assertEqual(
            intent_for_tool_calls(
                [{"tool": "log_checkin", "arguments": {"status": "ok"}}]
            ),
            "routine_checkin",
        )
        self.assertEqual(
            intent_for_tool_calls(
                [{"tool": "escalate", "arguments": {}}]
            ),
            "help_request",
        )
        self.assertEqual(
            intent_for_tool_calls(
                [{"tool": "schedule_checkin", "arguments": {}}]
            ),
            "schedule",
        )


class LLMModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_turn_executes_tools_and_returns_mode(self) -> None:
        llm = _FakeLLM(
            [
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "log_checkin",
                            "input": {
                                "person": "Elena Whitfield",
                                "status": "ok",
                                "message": "I'm fine",
                            },
                        }
                    ]
                },
                {"content": [{"type": "text", "text": "Thanks Elena, logged."}]},
            ]
        )
        agent = CheckInAgent(llm_client=llm)
        executed: list[tuple[str, dict]] = []

        async def fake_call(name: str, arguments: dict) -> dict:
            executed.append((name, arguments))
            return {"next_due": "2026-10-23T10:00:00+00:00"}

        agent.call_tool = fake_call  # type: ignore[method-assign]
        result = await agent._handle_llm(
            "I'm fine", "Elena Whitfield", ROSTER, source="web"
        )

        self.assertEqual(result.mode, "llm")
        self.assertEqual(result.intent, "routine_checkin")
        self.assertEqual(result.reply, "Thanks Elena, logged.")
        self.assertEqual(executed, [("log_checkin", {
            "person": "Elena Whitfield",
            "status": "ok",
            "message": "I'm fine",
            "mood": "unspecified",
            "source": "web",
        })])
        # The second request carries the real tool result back to the model.
        second = llm.messages.requests[1]["messages"][-1]
        self.assertEqual(second["role"], "user")
        self.assertEqual(second["content"][0]["tool_use_id"], "toolu_1")

    async def test_model_error_falls_back_to_the_rules(self) -> None:
        agent = CheckInAgent(llm_client=_FakeLLM([]))
        executed: list[str] = []

        async def fake_roster(max_age: float = 15.0) -> list[dict]:
            return ROSTER

        async def fake_call(name: str, arguments: dict) -> dict:
            executed.append(name)
            return {"next_due": "2026-10-23T10:00:00+00:00"}

        agent.roster = fake_roster  # type: ignore[method-assign]
        agent.call_tool = fake_call  # type: ignore[method-assign]
        result = await agent.handle("I'm fine", speaker="Elena Whitfield")

        self.assertEqual(result.mode, "rules")
        self.assertEqual(result.intent, "routine_checkin")
        self.assertEqual(executed, ["log_checkin"])

    async def test_help_never_reaches_the_model(self) -> None:
        """Even a model that would downgrade help cannot see the request."""
        llm = _FakeLLM([])
        agent = CheckInAgent(llm_client=llm)
        executed: list[str] = []

        async def fake_roster(max_age: float = 15.0) -> list[dict]:
            return ROSTER

        async def fake_call(name: str, arguments: dict) -> dict:
            executed.append(name)
            return {"created": True, "alert_id": 7}

        agent.roster = fake_roster  # type: ignore[method-assign]
        agent.call_tool = fake_call  # type: ignore[method-assign]
        result = await agent.handle(
            "I'm fine, but I need help", speaker="Elena Whitfield"
        )

        self.assertEqual(result.mode, "rules")
        self.assertEqual(result.intent, "help_request")
        self.assertEqual(executed, ["escalate", "log_checkin"])
        self.assertEqual(llm.messages.requests, [])

    def test_ai_mode_is_off_without_credentials(self) -> None:
        from unittest.mock import patch

        with patch("agent.ANTHROPIC_API_KEY", ""), patch("agent.USE_BEDROCK", False):
            self.assertIsNone(build_llm_client())
            self.assertEqual(CheckInAgent(llm_client=None).mode, "rules")


if __name__ == "__main__":
    unittest.main(verbosity=2)
