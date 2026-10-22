# Check-in Relay

A voice-first family check-in and escalation service, built for the **Amazon
Developer Hackathon 2026 (Alexa+ track)**.

An older family member checks in by voice. If they miss a scheduled check-in,
the family is alerted. If they ask for help, the request is escalated
immediately. The family sees and resolves everything on a live dashboard.

> **Check-in Relay is a wellbeing reminder. It is not a medical service and it
> is not a replacement for emergency services.** Alerts appear on the family
> dashboard only - no SMS, phone call or push notification is sent.

All people, check-ins and notes in this project are fictional demo data.

---

## What it does

| Capability | How |
| --- | --- |
| Voice or text check-ins | Browser microphone (Web Speech API) or typed text; the agent turns either into MCP tool calls |
| Spoken replies | Optional text-to-speech read-back of the agent's answer (toggle in the UI) |
| Visible MCP trace | Every tool call and its arguments is shown under the reply |
| Check-in scheduling | Per-person interval plus a grace window, with 15s/30s/1m demo presets |
| Missed check-in detection | A monitor inside the MCP server scans the schedules continuously |
| Immediate help alerts | An explicit request for help always wins over upbeat wording |
| Family dashboard | Live status, open alerts, one-click alert resolution, and an activity feed |

## Architecture

```
   browser (voice simulator + family dashboard)
        |  HTTP / JSON
        v
   web_app.py            FastAPI, port 8001
        |                REST endpoints + static UI
        |  MCP client
        v
   agent.py              intent rules, speaker resolution, safety precedence
        |  MCP over Streamable HTTP
        v
   mcp_server.py         MCP server, port 8000  (/mcp)
        |                5 tools + missed-check-in monitor
        v
   db.py                 SQLite (WAL) - people, check-ins, schedules, alerts, events
```

The web app **never** touches the database directly. Every read and every
write goes through the MCP server, so the MCP integration is the real
integration rather than a claim in the documentation.

## The MCP server

Built with the official Python MCP SDK (`mcp` 2.3.0) and served over
**Streamable HTTP**.

**Negotiated protocol version: `2026-07-28`** (verified with
`python test_client.py`). The hackathon requires `2025-11-25` or later; this
build negotiates the newest revision the SDK supports. The server also
advertises `2025-11-25` through the classic `initialize` handshake, so it
interoperates with clients that do not speak the modern envelope.

### Tools

| Tool | Arguments | Purpose |
| --- | --- | --- |
| `log_checkin` | `person`, `status` (`ok`/`help`), `message`, `mood`, `source` | Records a check-in and clears any missed-check-in alert it recovers from |
| `get_status` | `person` (optional) | Reads the family dashboard snapshot: everyone, their schedule, open alerts, recent activity |
| `schedule_checkin` | `person`, `interval_minutes`, `grace_seconds` | Creates or updates a recurring check-in schedule. `interval_minutes` accepts fractions, so `0.25` is a 15-second demo interval |
| `escalate` | `person`, `reason` | Opens a critical help-request alert |
| `resolve_alert` | `alert_id`, `resolved_by` | Closes an alert from the dashboard |

`resolve_alert` is the one addition to the four tools in the project brief.
Without it the dashboard would have to write to the database directly, which
would weaken the "everything goes through MCP" design. The four required tools
are unchanged.

The server also exposes `GET /health` outside the MCP protocol, which reports
the negotiated protocol versions and the registered tools.

## AI mode (optional)

The agent is rule-based by default and needs no API key. Set
`ANTHROPIC_API_KEY` in `.env` and install `anthropic` to let a model choose the
MCP tools instead:

```powershell
python -m pip install anthropic
# .env
# ANTHROPIC_API_KEY=your-key
# ANTHROPIC_MODEL=claude-sonnet-5-5
```

Restart the web app; `/api/health` and the dashboard report `llm mode`, and
every `/api/checkin` response includes `"mode": "llm"`. To use Amazon Bedrock,
install `"anthropic[bedrock]"` and set `USE_BEDROCK=1`, `AWS_REGION` and
`BEDROCK_MODEL_ID` instead.

AI mode keeps the same guardrails as the rules:

- An explicit request for help is handled by the rules **before** the model is
  called, so upbeat wording can never downgrade it.
- Every model tool call is validated against the real family roster and the
  same interval/grace limits before anything executes.
- A missing package, bad key or model error falls back to the rules and logs
  the reason instead of breaking the demo.

AI mode is optional. The rules path is the one that is fully exercised by the
test suite and the demo video.

## Safety design

These are the rules the implementation enforces, each one covered by a test.

1. **An explicit request for help always wins.** The agent scans for help
   phrases *before* it looks at positive wording, so "I'm fine, everything is
   great, but I need help" escalates rather than logs a routine check-in.
   Symptoms (a fall, chest pain, not feeling well) are treated as help too.
2. **No duplicate alerts.** A partial unique index allows only one open alert
   per `(person, kind)`. Repeated and simultaneous requests reuse the existing
   alert instead of creating a second one - tested with five threads racing
   the same escalation.
3. **Grace windows.** A late check-in inside the grace window does not raise an
   alert. A late check-in after the alert was raised closes it again.
4. **A help alert is never downgraded.** A routine check-in afterwards does not
   clear an open help request.
5. **No false delivery claims.** The `escalate` tool returns an explicit
   `notification` field stating that the alert is on the dashboard and that no
   SMS, call or push was sent.
6. **The dashboard escapes user content.** Everything the server returns is
   inserted with `textContent`, never as HTML.
7. **Failures are handled.** If the MCP server is down, the web app returns a
   clear `503` and the dashboard shows the problem instead of failing silently.

## Requirements

- Python 3.11 or newer (3.10 also works)
- No API keys, cloud account or Node.js required for the default rules mode
- Optional: an Anthropic key or AWS Bedrock access for AI mode

## Setup

### Windows (PowerShell)

```powershell
mkdir checkin-relay
cd checkin-relay

git init
python --version

python -m venv venv
venv\Scripts\activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### macOS / Linux

```bash
mkdir checkin-relay && cd checkin-relay
git init
python3 --version

python3 -m venv venv
source venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Configure

```powershell
Copy-Item .env.example .env     # Windows
# cp .env.example .env           # macOS / Linux
```

`.env` is git-ignored. The defaults work as-is for a local demo: the MCP
server on `127.0.0.1:8000`, the web app on `127.0.0.1:8001`.

### Seed the demo data

```powershell
python seed.py --reset
```

This creates three fictional family members with schedules and a short check-in
history.

## Running

Three terminals, from the project root:

```powershell
# Terminal 1 - the MCP server (port 8000)
python mcp_server.py

# Terminal 2 - the web app (port 8001)
python web_app.py

# Terminal 3 - optional: the scripted demo
python demo.py
```

Then open **<http://127.0.0.1:8001>** for the family dashboard and the voice
simulator.

Voice input uses the browser's Web Speech API (Chrome or Edge). Typing, the
quick phrases and the demo controls work in any browser; when speech
recognition is unavailable the microphone button says so instead of failing
silently.

Both servers bind to `127.0.0.1` only. They have no authentication, so do not
expose them publicly without adding one.

## Testing

```powershell
python -m unittest discover -s tests -v
```

60 tests across the data layer, agent rules, AI-mode validation, MCP tools
and seed data.
Standard library only - no test dependency to install.

With the MCP server running, the client smoke test verifies the negotiated
protocol version and calls all four required tools:

```powershell
python test_client.py
```

## The demo script

`python demo.py` walks the whole story end to end against the real server:

1. A routine check-in from Elena.
2. "I'm fine, everything is great, but I need help" - the help request wins.
3. A repeat request does not create a duplicate alert.
4. The family resolves the alert from the dashboard.
5. Samuel misses a check-in and the alert is raised after the grace window.
6. A late check-in recovers the missed alert.
7. The final family status.

Use `--fast` to shorten step 5, and `--reset` to reseed first.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The family dashboard and voice simulator |
| `GET` | `/api/health` | Web app liveness plus the state of the MCP link |
| `GET` | `/api/status` | The full dashboard snapshot (via `get_status`) |
| `GET` | `/api/people` | The family roster for the speaker picker |
| `POST` | `/api/checkin` | `{"text", "speaker", "source"}` - speak, and the agent calls MCP |
| `POST` | `/api/schedule` | `{"person", "interval_seconds", "grace_seconds"}` - set a schedule for the demo controls (via `schedule_checkin`) |
| `POST` | `/api/alerts/{id}/resolve` | `{"resolved_by"}` - close an alert (via `resolve_alert`) |

## Project layout

```
checkin-relay/
├── db.py            SQLite data layer (WAL, transactions, alert de-duplication)
├── seed.py          Fictional demo data
├── mcp_server.py    MCP server, 5 tools, missed-check-in monitor, /health
├── agent.py         MCP client, intent rules, speaker resolution, optional AI mode
├── web_app.py       FastAPI app, REST endpoints, error handling
├── demo.py          Scripted end-to-end demo
├── test_client.py   MCP smoke test (protocol version + all four tools)
├── static/
│   └── index.html   Family dashboard + voice simulator (no external assets)
├── tests/
│   ├── test_db.py          22 data-layer tests
│   ├── test_agent.py       28 agent rule and AI-mode tests
│   ├── test_mcp_server.py   7 MCP tool tests
│   └── test_seed.py         3 seed-data tests
├── requirements.txt
├── .env.example
├── PRODUCT_FEEDBACK.md   required submission artifact
├── FRICTION_LOG.md       optional judging-bonus artifact
├── SUBMISSION.md         Devpost copy and checklist
├── VIDEO_SCRIPT.md       timed demo script
└── LICENSE               MIT
```

## Privacy

Every person, check-in and note is fictional. No real family data belongs in
this repository, `.env` is git-ignored, and the local SQLite files are
git-ignored too. The server stores nothing beyond what the demo needs and
exposes it only on localhost.

## Tool feedback

Feedback on every tool, API and SDK used is in
[PRODUCT_FEEDBACK.md](PRODUCT_FEEDBACK.md), as the hackathon requires. The
build-time issues and workarounds are in [FRICTION_LOG.md](FRICTION_LOG.md).
[SUBMISSION.md](SUBMISSION.md) has the Devpost copy and checklist, and
[VIDEO_SCRIPT.md](VIDEO_SCRIPT.md) has the timed demo script.
