# Demo video script (target 2:35)

Public on YouTube or Vimeo, English, under 3:00. Record at 1080p. Close other
tabs, turn on Do Not Disturb, and hide any personal bookmarks.

## Before recording

```powershell
# 1. Clean demo data:
python seed.py --reset

# 2. Terminal 1
python mcp_server.py

# 3. Terminal 2
python web_app.py

# 4. Terminal 3, once, to show the protocol and tools on camera:
python test_client.py
```

The **15s demo** control sets its own 5-second grace window, so you do not need
to change `GRACE_SECONDS` for the escalation shot.

In the browser:

- Open <http://127.0.0.1:8001> and hard-refresh.
- Turn **off** "Read the assistant's replies aloud" so the narration stays
  clean. (Turn it on for a separate 5-second clip if you want to show voice
  output.)
- Pick **Elena Whitfield** in "Who is speaking".
- Zoom the browser to 110% so the trace panel is readable.

## Shot list

### 0:00-0:15 - Title and dashboard

**Show:** A simple title card ("Check-in Relay - Alexa+ track"), then the live
dashboard with the three family cards and the MCP badges.

**Say:**
"Millions of families live apart from their parents. Check-in Relay turns
Alexa+ into a gentle daily check-in that alerts the family when something is
wrong."

### 0:15-0:55 - A routine check-in

**Show:** Click the **routine check-in** quick phrase. Point at the green reply,
the `log_checkin` trace line, and Elena's updated card.

**Say:**
"Elena talks to Alexa+. Alexa+ calls our MCP server tool `log_checkin`. The
family dashboard updates live, and the check-in is anchored to her own
schedule."

**On screen:** The reply reads "Thanks Elena, your check-in is logged." The trace
shows `log_checkin` with the person, status, message and mood.

### 0:55-1:30 - A missed check-in escalates automatically

**Show:** Keep Elena selected. Click **15s demo**. Show the confirmation and the
trace (`schedule_checkin` with `interval_minutes: 0.25`). Let the dashboard sit
for about 25 seconds; the card turns to **missed** and a red alert appears.
Speed up this wait with a jump cut if needed.

**Say:**
"For the demo, I set a fifteen-second schedule with a five-second grace window.
When the check-in is missed, the monitor inside the MCP server raises the alert
by itself. No one had to press anything."

**Show:** Type or click **routine check-in** as the late check-in. The alert
disappears and the card returns to **ok**.

**Say:**
"When Elena checks in late, the same tool closes the overdue alert."

### 1:30-2:05 - Help always wins

**Show:** Select **Samuel Whitfield**. Click the **help request** phrase
("I'm fine, everything is great, but I need help"). Show the red alert and the
two trace lines: `escalate`, then `log_checkin` with `status: "help"`.

**Say:**
"This is the safety rule. Even though the sentence starts with 'I'm fine', an
explicit request for help always wins. The server escalates immediately and
records the check-in as a help request. The family sees it on the dashboard and
can resolve it with one click."

**Show:** Click **Resolve** on Samuel's alert.

**Say:**
"The alert closes. The assistant never claims it sent an SMS or called anyone
- the dashboard is the only channel."

### 2:05-2:35 - The MCP proof

**Show:** Switch to Terminal 3 and run `python test_client.py`. Highlight the
negotiated protocol version and the four required tools. Then show a few lines
of `mcp_server.py` (the tool decorators and the monitor loop).

**Say:**
"The server follows MCP over Streamable HTTP. The negotiated protocol is
2026-07-28, newer than the 2025-11-25 requirement. The four required tools are
`log_checkin`, `get_status`, `schedule_checkin` and `escalate`, plus
`resolve_alert` so the dashboard never writes to the database directly."

### 2:35-2:50 - Close

**Show:** The dashboard with no open alerts, then the title card again.

**Say:**
"Check-in Relay is a wellbeing helper, not a medical service. Next: real Alexa+
integration, multiple languages, and caregiver notifications with consent.
Thank you."

## Editing notes

- Cut the 25-second wait in the middle; keep the clock visible before and after
  the cut so the escalation is still believable.
- Add captions for every spoken sentence. Judges may watch muted.
- Keep the total under 3:00. Aim for 2:35-2:45.
- If the 15-second schedule does not fire (wrong person selected, MCP server
  restarted), stop, run `python seed.py --reset`, reload, and start the take
  again. Do not debug on camera.
- Do not use copyrighted music. A quiet room tone is enough.

## Backup lines if a tool is slow

If the dashboard has not refreshed yet, say: "The dashboard polls every couple
of seconds" and keep going; the alert will appear. If the MCP server is down,
show the "MCP unavailable" badge and use that as the honest failure-mode beat,
then restart it.
