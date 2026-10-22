# Devpost submission copy

Paste the sections below into Devpost. Fill the two `[brackets]` before
submitting; everything else is ready.

- **Project:** Check-in Relay
- **Track:** Alexa+ (primary)
- **Repo:** https://github.com/tashawink1757-dot/checkin-relay
- **Live demo:** https://checkin-relay.vercel.app
- **License:** MIT
- **MCP protocol:** `2026-07-28` negotiated over Streamable HTTP (the server
  also advertises `2025-11-25` through the classic handshake)
- **Team:** [your names]
- **Demo video:** [public YouTube or Vimeo URL, under 3 minutes]

---

## Inspiration

[Your team]. Millions of families live far from their parents. A daily "are you
okay?" call is easy to forget and easy to miss. We wanted Alexa+ to do it
gently, and to tell the family only when it actually matters.

## What it does

Check-in Relay lets a person speak a check-in to an Alexa+ style assistant. Our
MCP server records it and exposes the four tools from the brief:
`log_checkin`, `get_status`, `schedule_checkin` and `escalate`. If a scheduled
check-in is missed, or the person asks for help, the server raises an alert on a
live family dashboard. The family can close the alert with one click.

Three details make the demo feel real:

- **Help always wins.** "I'm fine, everything is great, but I need help"
  escalates instead of logging a routine check-in.
- **The escalation is automatic.** A 15-second demo schedule plus a grace
  window shows the full `waiting -> missed -> recovered` loop in under a minute.
- **The MCP calls are visible.** Every tool name and its arguments appear under
  the assistant's reply, so the integration is not a claim in a slide.

## How we built it

A Python MCP server (FastMCP, Streamable HTTP, official `mcp` 2.3.0 SDK),
SQLite with WAL and a partial unique index for alert de-duplication, FastAPI,
and a browser simulator of Alexa+ that acts as a real MCP client. The web app
never touches the database directly: every read and write goes through the MCP
server. Voice input uses the browser Web Speech API, with text and quick
phrases as the fallback, and replies can be spoken with text-to-speech.

The agent is rule-based by default, so the demo needs no API key. It also has an
optional AI mode: when `ANTHROPIC_API_KEY` is set, a Claude model sees the tool
list and chooses the calls; an explicit help request is still handled by the
safety rules before the model runs, and any model failure falls back to the
rules. [If you demo AI mode, name the model/provider here. If you used Amazon
Bedrock, say which AWS services and how. Otherwise delete this sentence.]

## Challenges

- Getting Streamable HTTP and the modern protocol envelope working, and keeping
  the client alive across a server restart.
- Designing escalation that does not spam the family: one open alert per person
  and kind, a grace window, and a help alert that is never downgraded by a later
  routine check-in.
- Making a sub-minute demo honest: the MCP tool accepts fractional minutes
  (`0.25` = 15 seconds), and the UI speaks in seconds while the tool speaks in
  minutes.
- Time zones: store UTC, display local, and anchor every seeded schedule to the
  person's newest check-in so the demo never opens with a phantom alert.

## What we learned / what is next

A small, working end-to-end loop is more convincing than a large unfinished
feature. Next: real Alexa+ integration when available, multiple languages,
caregiver notification channels (SMS or push, with consent), and a proper
on-device privacy review.

## Not a medical service

Check-in Relay is a wellbeing and reminder tool. Alerts appear on the family
dashboard only; no SMS, phone call or push notification is sent. The assistant
tells users to call their local emergency services if something is urgent.

---

## Built with

Python 3.11, `mcp` 2.3.0 (FastMCP, Streamable HTTP), FastAPI, Uvicorn, Pydantic,
SQLite, HTML/CSS/JavaScript, Web Speech API, `unittest`, Vercel Functions.
Optional AI mode: Anthropic Claude or Amazon Bedrock.

## Try it

```powershell
python -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
python seed.py --reset

# Terminal 1
python mcp_server.py

# Terminal 2
python web_app.py
```

Open <http://127.0.0.1:8001>, then use the quick phrases or the 15-second demo
control. The README has the full run-through and the test command.

**Live demo:** <https://checkin-relay.vercel.app> (runs the same app and MCP
server in one Vercel Function; synthetic data resets on a cold start).

## Submission checklist

- [x] Public GitHub repo with an MIT `LICENSE`
- [x] README runs from a clean clone (three terminals, no API key)
- [x] MCP server over Streamable HTTP, protocol `2026-07-28` (>= `2025-11-25`)
- [x] Four required tools registered, plus `resolve_alert` for the dashboard
- [x] Product feedback for every tool/API/SDK used (`PRODUCT_FEEDBACK.md`)
- [x] Optional friction log (`FRICTION_LOG.md`)
- [x] 64 automated tests, all passing
- [x] Public live demo: https://checkin-relay.vercel.app
- [ ] Demo video recorded, public, English, under 3 minutes
- [ ] Video URL pasted above and played while logged out
- [ ] Team names filled in
- [ ] Devpost project page submitted and re-checked logged out
- [ ] Track and mini challenges selected honestly (Alexa+; add Open Source only
      if the rules' extra fields are met; add AWS Builder only if Bedrock is
      actually used and documented)

## Pre-existing work

Check-in Relay was built during the Amazon Developer Hackathon 2026. No
pre-existing project was reused.
