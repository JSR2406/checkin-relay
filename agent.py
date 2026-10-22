"""Check-in Relay agent.

The agent is an MCP client. It connects to the Check-in Relay MCP server over
Streamable HTTP, works out what the speaker meant, and calls the matching MCP
tool.

It is deliberately rule-based so the demo has no external API dependency and
no key to lose. The rule set below is the safety-critical part:

    An explicit request for help always wins.

If any help phrase appears anywhere in the utterance, the agent escalates, even
if the rest of the sentence sounds upbeat ("I'm fine, but I need help").

Run it from the command line::

    python agent.py "I'm doing fine, just checking in"
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from mcp.client import Client

MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")

# --------------------------------------------------------------------------
# Rule sets
# --------------------------------------------------------------------------
HELP_PATTERNS: tuple[str, ...] = (
    # explicit requests
    r"\bhelp me\b",
    r"\bneed(?:s)? help\b",
    r"\bplease help\b",
    r"\bhelp\b",
    r"\bi need someone\b",
    r"\bget me someone\b",
    # emergencies
    r"\bemergency\b",
    r"\bambulance\b",
    r"\bcall (?:999|911|112)\b",
    # acute symptoms
    r"\bchest pain\b",
    r"\b(?:can'?t|cannot|cant) breathe\b",
    r"\bbreathless\b",
    r"\bdizzy\b",
    r"\bfainted\b",
    r"\bunconscious\b",
    r"\bbleeding\b",
    r"\bseizure\b",
    r"\b(?:a lot of|serious|severe) pain\b",
    r"\b(?:can'?t|cannot|cant) get up\b",
    r"\bfell (?:over|down)\b",
    r"\bi fell\b",
    r"\bfallen down\b",
    # not well
    r"\bnot feeling (?:well|good|great)\b",
    r"\bfeel(?:ing)? (?:unwell|sick|ill)\b",
    r"\bnot well\b",
    r"\bsomething(?:'s| is) (?:wrong|off)\b",
    # asking for a person
    r"\bplease come\b",
    r"\bcome over\b",
    r"\bcome quickly\b",
    r"\bsend someone\b",
    r"\bget someone\b",
    # distress
    r"\b(?:i am|i'm|im) (?:scared|afraid|frightened)\b",
)

# "Thanks for your help" is gratitude, not a request for help. The "for" is
# required so "thanks, please help" is still read as a request.
GRATITUDE_PATTERN = r"\b(?:thanks|thank you)\b\s+for\b[^.!?]*\bhelp\b"

# "not fine", "not good", ... read as a problem rather than a positive check-in.
NEGATED_POSITIVE_PATTERNS: tuple[str, ...] = (
    r"\bnot\s+fine\b",
    r"\bnot\s+good\b",
    r"\bnot\s+okay\b",
    r"\bnot\s+ok\b",
    r"\bnot\s+well\b",
    r"\bnot\s+great\b",
    r"\bnot\s+very\s+well\b",
    r"\bdon'?t\s+feel\s+(fine|good|okay|ok|well|great)\b",
    r"\bunwell\b",
)

POSITIVE_PATTERNS: tuple[str, ...] = (
    r"\bi(?:'m| am) fine\b",
    r"\bfeeling fine\b",
    r"\ball good\b",
    r"\ball is well\b",
    r"\bnothing wrong\b",
    r"\bno problems?\b",
    r"\bnothing to report\b",
    r"\bno issues\b",
    r"\bi(?:'m| am) good\b",
    r"\bfeeling good\b",
    r"\bi(?:'m| am) ok(?:ay)?\b",
    r"\bfine\b",
    r"\bgood\b",
    r"\bgreat\b",
    r"\bok(?:ay)?\b",
    r"\balright\b",
    r"\ball right\b",
    r"\bhappy\b",
    r"\bwell\b",
    r"\bbetter\b",
    r"\byes\b",
    r"\byeah\b",
    r"\bthanks\b",
    r"\bthank you\b",
)

SCHEDULE_PATTERNS: tuple[str, ...] = (
    r"\bevery\b",
    r"\bschedule\b",
    r"\bremind me\b",
    r"\breminder\b",
    r"\bhourly\b",
    r"\bdaily\b",
    r"\beach day\b",
    r"\beach morning\b",
    r"\beach evening\b",
    r"\bcheck in at\b",
    r"\bset a timer\b",
    r"\bhow often\b",
)

STATUS_PATTERNS: tuple[str, ...] = (
    r"\bstatus\b",
    r"\bhow (?:is|are) everyone\b",
    r"\bhow (?:is|are) the others\b",
    r"\bhow(?:'s|s) everyone\b",
    r"\bwho hasn(?:'t| has not)\b",
    r"\banyone missing\b",
    r"\bany alerts\b",
    r"\bany problems\b",
    r"\bdashboard\b",
    r"\bwhat(?:'s| is) the status\b",
    r"\bfamily status\b",
)

MOOD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "good": ("great", "excellent", "wonderful", "happy", "lovely", "good"),
    "okay": ("fine", "okay", "ok", "alright", "all right", "average", "normal"),
    "low": ("tired", "low", "down", "sad", "lonely", "flat", "drained"),
}

MINUTE_UNITS = {"minute": 1, "minutes": 1, "min": 1, "mins": 1}
HOUR_UNITS = {"hour": 60, "hours": 60, "hr": 60, "hrs": 60}
DAY_UNITS = {"day": 1440, "days": 1440}


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------
@dataclass
class AgentResult:
    """What the agent understood and what it did about it."""

    intent: str
    reply: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    data: Optional[dict[str, Any]] = None
    speaker: Optional[str] = None
    needs_clarification: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "reply": self.reply,
            "tool_calls": self.tool_calls,
            "data": self.data,
            "speaker": self.speaker,
            "needs_clarification": self.needs_clarification,
        }


# --------------------------------------------------------------------------
# Intent detection
# --------------------------------------------------------------------------
def _matches_any(text: str, patterns: tuple[str, ...]) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def detect_intent(utterance: str) -> tuple[str, Optional[str]]:
    """Classify an utterance.

    Returns ``(intent, matched_phrase)``. The order matters: help beats
    everything, then scheduling, then status, then a routine check-in.
    """
    text = utterance.lower().strip()
    if not text:
        return "unknown", None

    # 1. Safety first: an explicit request for help wins over any other signal.
    #    Gratitude ("thanks for your help") is removed before scanning so it
    #    cannot be mistaken for a request.
    help_text = re.sub(GRATITUDE_PATTERN, " ", text)
    if (hit := _matches_any(help_text, HELP_PATTERNS)) is not None:
        return "help_request", hit
    if (hit := _matches_any(text, NEGATED_POSITIVE_PATTERNS)) is not None:
        return "help_request", hit

    # 2. Scheduling.
    if (hit := _matches_any(text, SCHEDULE_PATTERNS)) is not None:
        return "schedule", hit

    # 3. Status queries.
    if (hit := _matches_any(text, STATUS_PATTERNS)) is not None:
        return "status_query", hit

    # 4. A routine check-in.
    if (hit := _matches_any(text, POSITIVE_PATTERNS)) is not None:
        return "routine_checkin", hit

    return "unknown", None


def detect_mood(utterance: str) -> str:
    """Map the wording to a mood label."""
    text = utterance.lower()
    for mood, keywords in MOOD_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return mood
    return "unspecified"


def parse_interval_minutes(utterance: str) -> Optional[int]:
    """Pull a check-in interval out of something like 'every 30 minutes'."""
    text = utterance.lower()

    if re.search(r"\bevery\s+(few\s+)?(hour|hours|hr|hrs)\b", text):
        return 60
    if re.search(r"\b(hourly|every\s+hour)\b", text):
        return 60
    if re.search(r"\b(daily|every\s+day|each\s+day|a\s+day)\b", text):
        return 1440
    if re.search(r"\b(weekly|every\s+week)\b", text):
        return 10080

    match = re.search(r"\bevery\s+(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit in MINUTE_UNITS:
            return amount
        if unit in HOUR_UNITS:
            return amount * 60
        if unit in DAY_UNITS:
            return amount * 1440

    match = re.search(r"\b(\d+)\s*(minutes|minute|mins|min|hours|hour|hrs|hr)\b", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit in MINUTE_UNITS:
            return amount
        if unit in HOUR_UNITS:
            return amount * 60
    return None


# --------------------------------------------------------------------------
# Speaker resolution
# --------------------------------------------------------------------------
# Spoken forms for each relation, so "mum" finds the person whose relation
# is "mother" without hardcoding a particular family's names.
RELATION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "mother": ("mum", "mom", "mummy", "mumma"),
    "father": ("dad", "daddy", "pa", "papa"),
    "sister": ("sis",),
    "brother": ("bro",),
    "grandmother": ("nana", "nan", "granny"),
    "grandfather": ("grandpa", "pops"),
    "aunt": ("aunty",),
    "uncle": ("uncley",),
}


def build_aliases(roster: list[dict[str, Any]]) -> dict[str, str]:
    """Map the words a family member might be called by to their full name."""
    aliases: dict[str, str] = {}
    for person in roster:
        name = person.get("name", "")
        if not name:
            continue
        aliases[name.lower()] = name

        parts = name.split()
        first = parts[0].lower()
        aliases.setdefault(first, name)
        if len(first) >= 3:
            # "Sam" should find "Samuel".
            aliases.setdefault(first[:3], name)

        relation = (person.get("relation") or "").strip().lower()
        if relation:
            aliases.setdefault(relation, name)
            for synonym in RELATION_SYNONYMS.get(relation, ()):
                aliases.setdefault(synonym, name)
    return aliases


def resolve_speaker(
    utterance: str,
    roster: list[dict[str, Any]],
    explicit: Optional[str] = None,
) -> Optional[str]:
    """Work out who is speaking: explicit selection first, then the words used."""
    aliases = build_aliases(roster)

    if explicit:
        wanted = explicit.strip().lower()
        if wanted in aliases:
            return aliases[wanted]
        for person in roster:
            if person.get("name", "").lower() == wanted:
                return person["name"]

    text = utterance.lower()
    matches = [alias for alias in aliases if alias in text]
    if matches:
        # Prefer the longest alias so "elena whitfield" beats "elena".
        best = max(matches, key=len)
        return aliases[best]

    if len(roster) == 1:
        return roster[0]["name"]
    return None


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------
class CheckInAgent:
    """An MCP client that turns speech into MCP tool calls."""

    def __init__(self, server_url: Optional[str] = None) -> None:
        self.server_url = server_url or MCP_URL
        self._client: Optional[Client] = None
        self._roster: list[dict[str, Any]] = []
        self._roster_fetched_at: float = 0.0

    # -- connection ------------------------------------------------------
    async def connect(self) -> None:
        client = Client(self.server_url, mode="auto")
        await client.__aenter__()
        self._client = client
        self._roster = []
        self._roster_fetched_at = 0.0

    async def close(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def __aenter__(self) -> "CheckInAgent":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    @property
    def protocol_version(self) -> Optional[str]:
        return self._client.protocol_version if self._client else None

    # -- tool calls ------------------------------------------------------
    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call an MCP tool, reconnecting once if the session has gone stale."""
        if self._client is None:
            await self.connect()

        try:
            return await self._invoke(name, arguments)
        except Exception as first_error:
            # The server may have restarted; rebuild the session and retry once.
            try:
                await self.close()
            except Exception:
                pass
            await self.connect()
            try:
                return await self._invoke(name, arguments)
            except Exception:
                raise first_error

    async def _invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None
        result = await self._client.call_tool(name, arguments)
        if result.is_error:
            detail = "".join(
                block.text for block in result.content if hasattr(block, "text")
            )
            raise RuntimeError(f"MCP tool {name!r} failed: {detail}")
        return result.structured_content or {}

    async def list_tools(self) -> list[str]:
        if self._client is None:
            await self.connect()
        assert self._client is not None
        result = await self._client.list_tools()
        return [tool.name for tool in result.tools]

    # -- roster ----------------------------------------------------------
    async def roster(self, max_age: float = 15.0) -> list[dict[str, Any]]:
        """The family roster, fetched over MCP and cached briefly."""
        now = time.monotonic()
        if self._roster and (now - self._roster_fetched_at) < max_age:
            return self._roster
        data = await self.call_tool("get_status", {})
        self._roster = data.get("people", [])
        self._roster_fetched_at = now
        return self._roster

    # -- the main entry point -------------------------------------------
    async def handle(
        self,
        utterance: str,
        speaker: Optional[str] = None,
        source: str = "voice",
    ) -> AgentResult:
        """Understand an utterance and act on it through MCP."""
        utterance = (utterance or "").strip()
        if not utterance:
            return AgentResult(
                intent="unknown",
                reply="I didn't catch that. Are you okay, or do you need help?",
                needs_clarification=True,
            )

        intent, matched = detect_intent(utterance)
        roster = await self.roster()
        resolved_speaker = resolve_speaker(utterance, roster, speaker)

        if intent == "help_request":
            return await self._handle_help(utterance, resolved_speaker, roster, matched)
        if intent == "schedule":
            return await self._handle_schedule(utterance, resolved_speaker, roster)
        if intent == "status_query":
            return await self._handle_status(utterance, resolved_speaker)
        if intent == "routine_checkin":
            return await self._handle_checkin(utterance, resolved_speaker, roster)
        return await self._handle_unknown(utterance, roster)

    # -- intent handlers -------------------------------------------------
    async def _handle_help(
        self,
        utterance: str,
        speaker: Optional[str],
        roster: list[dict[str, Any]],
        matched: Optional[str],
    ) -> AgentResult:
        if speaker is None:
            return AgentResult(
                intent="help_request",
                reply=(
                    "I'm sorry - I need to know who that is so I can raise the "
                    "right alert. Who is speaking?"
                ),
                needs_clarification=True,
            )

        reason = f"Spoke on the check-in line and asked for help ({matched or 'explicit request'})"
        escalate_data = await self.call_tool(
            "escalate", {"person": speaker, "reason": reason}
        )
        # Also record the check-in itself, flagged as a help request.
        checkin_data = await self.call_tool(
            "log_checkin",
            {
                "person": speaker,
                "status": "help",
                "message": utterance,
                "mood": detect_mood(utterance),
                "source": "voice",
            },
        )

        alert_id = escalate_data.get("alert_id")
        created = escalate_data.get("created")
        headline = (
            f"I've raised a help alert for {speaker}. It is on the family "
            "dashboard now."
            if created
            else f"{speaker} already has an open help alert, so I have not "
            "raised a second one."
        )
        reply = (
            f"{headline} "
            "Please call your local emergency services if this is urgent - "
            "this app is a wellbeing reminder, not a medical service."
        )
        return AgentResult(
            intent="help_request",
            reply=reply,
            tool_calls=[
                {"tool": "escalate", "arguments": {"person": speaker, "reason": reason}},
                {
                    "tool": "log_checkin",
                    "arguments": {
                        "person": speaker,
                        "status": "help",
                        "message": utterance,
                    },
                },
            ],
            data={"escalate": escalate_data, "log_checkin": checkin_data},
            speaker=speaker,
        )

    async def _handle_checkin(
        self,
        utterance: str,
        speaker: Optional[str],
        roster: list[dict[str, Any]],
    ) -> AgentResult:
        if speaker is None:
            return AgentResult(
                intent="routine_checkin",
                reply="Thanks for checking in. Who is speaking?",
                needs_clarification=True,
            )

        mood = detect_mood(utterance)
        data = await self.call_tool(
            "log_checkin",
            {
                "person": speaker,
                "status": "ok",
                "message": utterance,
                "mood": mood,
                "source": "voice",
            },
        )
        next_due = data.get("next_due")
        recovered = data.get("recovered_alerts") or []
        reply = f"Thanks {speaker.split()[0]}, your check-in is logged."
        if next_due:
            reply += f" Your next check-in is due at {next_due}."
        if recovered:
            reply += " I've closed the overdue alert you just cleared."
        return AgentResult(
            intent="routine_checkin",
            reply=reply,
            tool_calls=[
                {
                    "tool": "log_checkin",
                    "arguments": {
                        "person": speaker,
                        "status": "ok",
                        "message": utterance,
                        "mood": mood,
                    },
                }
            ],
            data={"log_checkin": data},
            speaker=speaker,
        )

    async def _handle_schedule(
        self,
        utterance: str,
        speaker: Optional[str],
        roster: list[dict[str, Any]],
    ) -> AgentResult:
        if speaker is None:
            return AgentResult(
                intent="schedule",
                reply="Who would you like me to set that schedule for?",
                needs_clarification=True,
            )

        minutes = parse_interval_minutes(utterance)
        if minutes is None:
            minutes = 60
        data = await self.call_tool(
            "schedule_checkin",
            {"person": speaker, "interval_minutes": minutes},
        )
        reply = (
            f"Done. {speaker} now has a check-in every {minutes} minutes. "
            f"The next one is due at {data.get('next_due')}."
        )
        return AgentResult(
            intent="schedule",
            reply=reply,
            tool_calls=[
                {
                    "tool": "schedule_checkin",
                    "arguments": {"person": speaker, "interval_minutes": minutes},
                }
            ],
            data={"schedule_checkin": data},
            speaker=speaker,
        )

    async def _handle_status(
        self, utterance: str, speaker: Optional[str]
    ) -> AgentResult:
        data = await self.call_tool("get_status", {})
        people = data.get("people", [])
        open_alerts = data.get("open_alerts", [])

        parts = []
        for person in people:
            state = person.get("state", "unknown")
            label = person.get("name", "someone")
            if state in ("help_requested", "missed", "overdue"):
                parts.append(f"{label} needs attention")
            elif state == "never_checked_in":
                parts.append(f"{label} has not checked in yet")
            else:
                last = person.get("last_checkin")
                when = last.get("checked_at") if last else "never"
                parts.append(f"{label} last checked in at {when}")

        reply = " ".join(parts) if parts else "I have no check-ins recorded yet."
        if open_alerts:
            reply += f" There {len(open_alerts)} open alert(s) on the dashboard."
        else:
            reply += " There are no open alerts."

        return AgentResult(
            intent="status_query",
            reply=reply,
            tool_calls=[{"tool": "get_status", "arguments": {}}],
            data=data,
            speaker=speaker,
        )

    async def _handle_unknown(
        self, utterance: str, roster: list[dict[str, Any]]
    ) -> AgentResult:
        return AgentResult(
            intent="unknown",
            reply=(
                "I didn't catch that. Are you okay, or do you need help? "
                "You can say something like 'I'm fine' or 'I need help'."
            ),
            needs_clarification=True,
        )


async def _run_cli(utterance: str, speaker: Optional[str]) -> int:
    async with CheckInAgent() as agent:
        print(f"Connected to {agent.server_url}")
        print(f"Protocol version: {agent.protocol_version}")
        print(f"Tools: {await agent.list_tools()}")
        print()
        result = await agent.handle(utterance, speaker=speaker)
        print(f"Utterance : {utterance}")
        print(f"Intent    : {result.intent}")
        print(f"Speaker   : {result.speaker}")
        print(f"Tool calls: {json.dumps(result.tool_calls, indent=2)}")
        print(f"Reply     : {result.reply}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        print("usage: python agent.py \"<utterance>\" [speaker]")
        return 2
    utterance = sys.argv[1]
    speaker = sys.argv[2] if len(sys.argv) > 2 else None
    return asyncio.run(_run_cli(utterance, speaker))


if __name__ == "__main__":
    sys.exit(main())
