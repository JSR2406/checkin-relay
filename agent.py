"""Check-in Relay agent.

The agent is an MCP client. It connects to the Check-in Relay MCP server over
Streamable HTTP, works out what the speaker meant, and calls the matching MCP
tool.

It has two modes:

* **Rules** (default) - keyword rules with no external API dependency and no
  key to lose.
* **AI** (optional) - when ``ANTHROPIC_API_KEY`` (or AWS Bedrock credentials)
  are present, a language model sees the MCP tool list and chooses the tools.
  If the key is missing, the package is not installed, or the model call fails,
  the agent falls back to the rules.

The safety-critical rule is enforced in *both* modes before the model runs:

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
from datetime import datetime
from typing import Any, Optional

from dotenv import load_dotenv
from mcp.client import Client

# The web app imports this module directly, so load .env here as well. Without
# this, AI mode would never see ANTHROPIC_API_KEY in the web process.
load_dotenv()

MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")

# Optional AI mode (Part 10 of the build guide). Nothing here runs unless a
# key is configured; the rules stay the default and the fallback.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
USE_BEDROCK = os.getenv("USE_BEDROCK", "").strip().lower() in {"1", "true", "yes"}
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "").strip()

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
    r"\bcheck on me\b",
    r"\bin \d+(?:\.\d+)?\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
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

# The four tools from the brief, described for the model. Keep this list in
# sync with mcp_server.py; tests/test_mcp_server.py guards the registration.
LLM_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "log_checkin",
        "description": (
            "Record that a person has checked in. Use status 'ok' for a routine "
            "check-in and 'help' only when they asked for help."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Exact family member name."},
                "status": {"type": "string", "enum": ["ok", "help"]},
                "message": {"type": "string", "description": "What the person said."},
                "mood": {"type": "string", "enum": ["good", "okay", "low", "unspecified"]},
                "source": {"type": "string", "enum": ["voice", "web", "seed"]},
            },
            "required": ["person", "status", "message"],
        },
    },
    {
        "name": "get_status",
        "description": (
            "Read the family dashboard: every person's state, schedule, last "
            "check-in and the open alerts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Optional exact name."}
            },
        },
    },
    {
        "name": "schedule_checkin",
        "description": (
            "Create or replace a recurring check-in schedule. interval_minutes "
            "may be fractional: 0.25 means every 15 seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Exact family member name."},
                "interval_minutes": {
                    "type": "number",
                    "description": "Greater than zero; fractions are allowed.",
                },
                "grace_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 600,
                    "description": "Optional grace window before an alert.",
                },
            },
            "required": ["person", "interval_minutes"],
        },
    },
    {
        "name": "escalate",
        "description": (
            "Raise a critical help-request alert on the family dashboard. Use "
            "this whenever the person asks for help or describes an emergency."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "Exact family member name."},
                "reason": {"type": "string", "description": "Short reason for the family."},
            },
            "required": ["person", "reason"],
        },
    },
]

ALLOWED_LLM_TOOLS = {schema["name"] for schema in LLM_TOOL_SCHEMAS}


def build_llm_client() -> Any:
    """Create an Anthropic or Bedrock client, or return ``None``.

    AI mode is strictly opt-in: without a key this returns ``None`` and the
    agent keeps using the rules. A missing ``anthropic`` package is reported
    once and also falls back instead of breaking the demo.
    """
    if not (ANTHROPIC_API_KEY or USE_BEDROCK):
        return None
    try:
        if USE_BEDROCK:
            from anthropic import AnthropicBedrock

            return AnthropicBedrock(aws_region=AWS_REGION)
        from anthropic import Anthropic

        return Anthropic(api_key=ANTHROPIC_API_KEY)
    except Exception as exc:  # package missing or bad credentials setup
        print(
            f"[agent] AI mode is configured but unavailable ({exc}); using rules.",
            file=sys.stderr,
        )
        return None


def llm_model_name() -> str:
    """The model ID for the active provider."""
    if USE_BEDROCK and BEDROCK_MODEL_ID:
        return BEDROCK_MODEL_ID
    return ANTHROPIC_MODEL


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
    mode: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "reply": self.reply,
            "tool_calls": self.tool_calls,
            "data": self.data,
            "speaker": self.speaker,
            "needs_clarification": self.needs_clarification,
            "mode": self.mode,
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


def parse_interval_minutes(utterance: str) -> Optional[float]:
    """Pull a check-in interval out of something like 'every 30 minutes'.

    Fractions and seconds are supported so the demo can use a 15-second
    interval: 'check on me in 15 seconds' returns 0.25.
    """
    text = utterance.lower()

    match = re.search(r"\b(?:every|in)\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?)\b", text)
    if match:
        return round(float(match.group(1)) / 60, 4)

    if re.search(r"\bevery\s+(few\s+)?(hour|hours|hr|hrs)\b", text):
        return 60
    if re.search(r"\b(hourly|every\s+hour)\b", text):
        return 60
    if re.search(r"\b(daily|every\s+day|each\s+day|a\s+day)\b", text):
        return 1440
    if re.search(r"\b(weekly|every\s+week)\b", text):
        return 10080

    match = re.search(
        r"\bevery\s+(\d+(?:\.\d+)?)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b",
        text,
    )
    if match:
        amount = float(match.group(1))
        unit = match.group(2)
        if unit in MINUTE_UNITS:
            return amount
        if unit in HOUR_UNITS:
            return amount * 60
        if unit in DAY_UNITS:
            return amount * 1440

    match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(minutes|minute|mins|min|hours|hour|hrs|hr)\b", text
    )
    if match:
        amount = float(match.group(1))
        unit = match.group(2)
        if unit in MINUTE_UNITS:
            return amount
        if unit in HOUR_UNITS:
            return amount * 60
    return None


def format_interval(minutes: float) -> str:
    """Human wording for an interval: 0.25 -> '15 seconds'."""
    seconds = int(round(minutes * 60))
    if seconds < 120:
        return f"{seconds} seconds"
    if minutes < 120:
        return f"{int(round(minutes))} minutes"
    return f"{minutes / 60:.1f} hours"


def format_due(value: Optional[str]) -> str:
    """Show an ISO due time as a local clock time for the spoken reply."""
    if not value:
        return "soon"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return value


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
    matches = [
        alias
        for alias in aliases
        if re.search(rf"\b{re.escape(alias)}\b", text)
    ]
    if matches:
        # Prefer the longest alias so "elena whitfield" beats "elena".
        best = max(matches, key=len)
        return aliases[best]

    if len(roster) == 1:
        return roster[0]["name"]
    return None


# --------------------------------------------------------------------------
# AI mode: validate what the model asked for before calling anything
# --------------------------------------------------------------------------
def _canonical_person(
    value: Any, roster: list[dict[str, Any]], speaker: Optional[str]
) -> str:
    """Resolve a model-supplied name against the real roster, never a guess."""
    names = {
        person.get("name", "").lower(): person.get("name", "")
        for person in roster
        if person.get("name")
    }
    if isinstance(value, str) and value.strip():
        canonical = names.get(value.strip().lower())
        if canonical:
            return canonical
        raise ValueError(f"unknown family member: {value!r}")
    if speaker:
        return speaker
    raise ValueError("the model did not say who the tool call is for")


def normalise_llm_input(
    name: str,
    arguments: Any,
    roster: list[dict[str, Any]],
    speaker: Optional[str],
    source: str = "voice",
) -> dict[str, Any]:
    """Validate and normalise one model tool call.

    Raises ``ValueError`` for anything the rules would not accept. The caller
    validates every tool call in a turn before executing any of them, so a bad
    second call cannot leave a half-applied action behind.
    """
    if name not in ALLOWED_LLM_TOOLS:
        raise ValueError(f"tool is not available: {name!r}")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")

    if name == "get_status":
        person = arguments.get("person")
        return {} if person in (None, "") else {"person": _canonical_person(person, roster, None)}

    if name == "escalate":
        reason = str(arguments.get("reason") or "").strip()
        if not reason:
            raise ValueError("escalate needs a reason")
        return {
            "person": _canonical_person(arguments.get("person"), roster, speaker),
            "reason": reason[:500],
        }

    if name == "log_checkin":
        status = str(arguments.get("status") or "ok").lower()
        if status not in {"ok", "help"}:
            raise ValueError(f"invalid check-in status: {status!r}")
        message = str(arguments.get("message") or "").strip()
        if not message:
            raise ValueError("log_checkin needs a message")
        mood = str(arguments.get("mood") or "unspecified").lower()
        if mood not in MOOD_KEYWORDS and mood != "unspecified":
            mood = "unspecified"
        return {
            "person": _canonical_person(arguments.get("person"), roster, speaker),
            "status": status,
            "message": message[:2000],
            "mood": mood,
            "source": source if source in {"voice", "web"} else "voice",
        }

    # schedule_checkin
    try:
        minutes = float(arguments.get("interval_minutes"))
    except (TypeError, ValueError) as exc:
        raise ValueError("interval_minutes must be a number") from exc
    if not (0 < minutes <= 10080):
        raise ValueError("interval_minutes must be between 0 and 10080")
    call: dict[str, Any] = {
        "person": _canonical_person(arguments.get("person"), roster, speaker),
        "interval_minutes": minutes,
    }
    grace = arguments.get("grace_seconds")
    if grace is not None:
        try:
            grace_int = int(grace)
        except (TypeError, ValueError) as exc:
            raise ValueError("grace_seconds must be an integer") from exc
        if not (0 <= grace_int <= 600):
            raise ValueError("grace_seconds must be between 0 and 600")
        call["grace_seconds"] = grace_int
    return call


def intent_for_tool_calls(calls: list[dict[str, Any]]) -> str:
    """Label a model turn with the same intent names the rules mode uses."""
    names = {call.get("tool") for call in calls}
    if "escalate" in names or any(
        call.get("tool") == "log_checkin"
        and call.get("arguments", {}).get("status") == "help"
        for call in calls
    ):
        return "help_request"
    if "schedule_checkin" in names:
        return "schedule"
    if "get_status" in names:
        return "status_query"
    if "log_checkin" in names:
        return "routine_checkin"
    return "unknown"


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------
_LLM_UNSET = object()


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Turn an SDK content block into a plain dict for the next request."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return {
        key: value
        for key in ("type", "text", "id", "name", "input")
        if (value := getattr(block, key, None)) is not None
    }


class CheckInAgent:
    """An MCP client that turns speech into MCP tool calls."""

    def __init__(
        self,
        server_url: Optional[str] = None,
        llm_client: Any = _LLM_UNSET,
        mcp_server: Any = None,
    ) -> None:
        self.server_url = server_url or MCP_URL
        # An in-process MCPServer avoids a second HTTP service in serverless
        # deployments such as Vercel. Tests and local runs can keep using the
        # Streamable HTTP URL.
        self._mcp_server = mcp_server
        self._client: Optional[Client] = None
        self._roster: list[dict[str, Any]] = []
        self._roster_fetched_at: float = 0.0
        self._llm = build_llm_client() if llm_client is _LLM_UNSET else llm_client
        self.mode = "llm" if self._llm is not None else "rules"

    # -- connection ------------------------------------------------------
    async def connect(self) -> None:
        target = self._mcp_server if self._mcp_server is not None else self.server_url
        client = Client(target, mode="auto")
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

        # Safety override: help always goes through the rules path, even in AI
        # mode. A model can add nuance, but it cannot downgrade an explicit
        # request for help.
        if intent == "help_request":
            return await self._handle_help(
                utterance, resolved_speaker, roster, matched, source
            )

        if self._llm is not None:
            try:
                return await self._handle_llm(
                    utterance, resolved_speaker, roster, source
                )
            except Exception as exc:
                print(
                    f"[agent] AI mode failed ({exc}); falling back to rules.",
                    file=sys.stderr,
                )

        if intent == "schedule":
            return await self._handle_schedule(utterance, resolved_speaker, roster)
        if intent == "status_query":
            return await self._handle_status(utterance, resolved_speaker)
        if intent == "routine_checkin":
            return await self._handle_checkin(
                utterance, resolved_speaker, roster, source
            )
        return await self._handle_unknown(utterance, roster)

    # -- AI mode ---------------------------------------------------------
    async def _handle_llm(
        self,
        utterance: str,
        speaker: Optional[str],
        roster: list[dict[str, Any]],
        source: str = "voice",
    ) -> AgentResult:
        """Let the model choose MCP tools, then execute them for real.

        Every tool call is validated against the roster and the same limits the
        rules use before anything runs. A model failure raises, and ``handle``
        falls back to the rules.
        """
        people = [
            {
                "name": person.get("name"),
                "relation": person.get("relation"),
                "state": person.get("state"),
            }
            for person in roster
        ]
        system = (
            "You are the voice behind Check-in Relay, a gentle family wellbeing "
            "service. Use the MCP tools to act; never invent a person, a "
            "check-in or an alert. The family roster is "
            f"{json.dumps(people)}. The speaker selected in the app is "
            f"{speaker or 'not selected'}. Right now it is "
            f"{datetime.now().astimezone().isoformat(timespec='seconds')}. "
            "Call get_status for questions about the family. Call log_checkin "
            "for a routine check-in. Call schedule_checkin for reminders. Call "
            "escalate plus log_checkin with status 'help' if the person asks "
            "for help or describes an emergency. Alerts appear on the family "
            "dashboard only: never claim that an SMS, call or push was sent. "
            "After the tools return, answer in one or two short, warm spoken "
            "sentences with no markdown. This is a wellbeing reminder, not a "
            "medical service."
        )

        messages: list[dict[str, Any]] = [{"role": "user", "content": utterance}]
        calls: list[dict[str, Any]] = []
        results_by_tool: dict[str, Any] = {}

        for _ in range(3):
            response = await asyncio.to_thread(
                self._llm.messages.create,
                model=llm_model_name(),
                max_tokens=900,
                system=system,
                tools=LLM_TOOL_SCHEMAS,
                messages=messages,
            )
            raw_content = (
                response.get("content", [])
                if isinstance(response, dict)
                else response.content
            )
            blocks = [_block_to_dict(block) for block in raw_content]
            tool_uses = [block for block in blocks if block.get("type") == "tool_use"]
            text = "".join(
                block.get("text", "")
                for block in blocks
                if block.get("type") == "text"
            ).strip()

            if not tool_uses:
                return AgentResult(
                    intent=intent_for_tool_calls(calls),
                    reply=text or "I didn't catch that. Are you okay?",
                    tool_calls=calls,
                    data=results_by_tool or None,
                    speaker=speaker,
                    needs_clarification=not calls and not text,
                    mode="llm",
                )

            # Validate the whole turn before executing any of it.
            planned = [
                (
                    block,
                    normalise_llm_input(
                        block.get("name", ""),
                        block.get("input"),
                        roster,
                        speaker,
                        source,
                    ),
                )
                for block in tool_uses
            ]
            messages.append({"role": "assistant", "content": blocks})

            tool_results = []
            for block, arguments in planned:
                name = block.get("name", "")
                data = await self.call_tool(name, arguments)
                calls.append({"tool": name, "arguments": arguments})
                results_by_tool[name] = data
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.get("id"),
                        "content": json.dumps(data)[:8000],
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        return AgentResult(
            intent=intent_for_tool_calls(calls),
            reply=self._llm_summary(calls, results_by_tool, speaker),
            tool_calls=calls,
            data=results_by_tool or None,
            speaker=speaker,
            mode="llm",
        )

    def _llm_summary(
        self,
        calls: list[dict[str, Any]],
        results_by_tool: dict[str, Any],
        speaker: Optional[str],
    ) -> str:
        """A safe spoken reply if the model never produced final text."""
        intent = intent_for_tool_calls(calls)
        if intent == "help_request":
            return (
                f"I've raised a help alert for {speaker or 'your family member'}. "
                "It is on the family dashboard now. Please call your local "
                "emergency services if this is urgent."
            )
        if intent == "schedule":
            data = results_by_tool.get("schedule_checkin", {})
            return (
                f"Done. The next check-in is due at "
                f"{format_due(data.get('next_due'))}."
            )
        if intent == "status_query":
            data = results_by_tool.get("get_status", {})
            alerts = len(data.get("open_alerts", []))
            return (
                f"The family status is on the dashboard. There "
                f"{'is' if alerts == 1 else 'are'} {alerts} open alert(s)."
            )
        if intent == "routine_checkin":
            data = results_by_tool.get("log_checkin", {})
            reply = f"Thanks {speaker.split()[0] if speaker else ''}, your check-in is logged.".replace("  ", " ")
            if data.get("next_due"):
                reply += f" Your next check-in is due at {format_due(data['next_due'])}."
            return reply
        return "I didn't catch that. Are you okay, or do you need help?"

    # -- intent handlers -------------------------------------------------
    async def _handle_help(
        self,
        utterance: str,
        speaker: Optional[str],
        roster: list[dict[str, Any]],
        matched: Optional[str],
        source: str = "voice",
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
                "source": source if source in {"voice", "web"} else "voice",
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
                        "mood": detect_mood(utterance),
                        "source": source if source in {"voice", "web"} else "voice",
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
        source: str = "voice",
    ) -> AgentResult:
        if speaker is None:
            return AgentResult(
                intent="routine_checkin",
                reply="Thanks for checking in. Who is speaking?",
                needs_clarification=True,
            )

        mood = detect_mood(utterance)
        source = source if source in {"voice", "web"} else "voice"
        data = await self.call_tool(
            "log_checkin",
            {
                "person": speaker,
                "status": "ok",
                "message": utterance,
                "mood": mood,
                "source": source,
            },
        )
        next_due = data.get("next_due")
        recovered = data.get("recovered_alerts") or []
        reply = f"Thanks {speaker.split()[0]}, your check-in is logged."
        if next_due:
            reply += f" Your next check-in is due at {format_due(next_due)}."
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
                        "source": source,
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
        if minutes <= 0:
            return AgentResult(
                intent="schedule",
                reply="A check-in interval has to be longer than zero seconds. How often should I check in?",
                needs_clarification=True,
                speaker=speaker,
            )
        data = await self.call_tool(
            "schedule_checkin",
            {"person": speaker, "interval_minutes": minutes},
        )
        reply = (
            f"Done. {speaker} now has a check-in every {format_interval(minutes)}. "
            f"The next one is due at {format_due(data.get('next_due'))}."
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
                parts.append(f"{label} needs attention.")
            elif state == "never_checked_in":
                parts.append(f"{label} has not checked in yet.")
            else:
                last = person.get("last_checkin")
                when = format_due(last.get("checked_at")) if last else "never"
                parts.append(f"{label} last checked in at {when}.")

        reply = " ".join(parts) if parts else "I have no check-ins recorded yet."
        if open_alerts:
            count = len(open_alerts)
            reply += (
                f" There {'is' if count == 1 else 'are'} {count} "
                f"open alert{'s' if count != 1 else ''} on the dashboard."
            )
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
