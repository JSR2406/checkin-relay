# Friction log

Optional bonus artifact for the Amazon Developer Hackathon 2026 (Alexa+ track).
One entry per thing that slowed the build down. Every entry below comes from
this project; the workarounds are in the committed code.

---

## 1. MCP Python SDK - a restarted server leaves the client on a dead session

**Tool/API/SDK:** `mcp` 2.3.0, Streamable HTTP client and server

**Task attempted:** Keep the FastAPI process connected to the MCP server across
a server restart during development.

**Steps taken:**
1. Start `mcp_server.py`, then `web_app.py`.
2. Send one check-in through `/api/checkin` - it works.
3. Stop and restart `mcp_server.py`.
4. Send another check-in.

**Expected:** The next tool call either reconnects or reports a clear
"server restarted" error.

**Actual:** The cached client session pointed at the old transport and the call
failed; the browser received a generic 503.

**Severity:** Medium

**Workaround:** `CheckInAgent.call_tool` catches the first failure, closes the
session, reconnects, and retries exactly once (`agent.py`). Any second failure
is surfaced as a clean 503.

**Suggestion:** Expose a documented "session expired / reconnect" error class
from the client, so applications can distinguish it from a real tool error
without retrying every exception.

---

## 2. MCP protocol negotiation - newest is not the version in the brief

**Tool/API/SDK:** MCP Python SDK 2.3.0 and Streamable HTTP transport

**Task attempted:** Prove the server satisfies the hackathon requirement of MCP
spec version `2025-11-25` or later.

**Steps taken:**
1. Read the requirement and expected the client to report `2025-11-25`.
2. Ran `python test_client.py` against the live server.
3. Saw the negotiated version was `2026-07-28`.

**Expected:** A single obvious version string in the docs and the response.

**Actual:** The SDK supports several revisions. The modern envelope negotiated
`2026-07-28`, while the classic `initialize` handshake advertises
`2025-11-25` as well. It was not obvious which value a judge would see.

**Severity:** Low

**Workaround:** `GET /health` lists `latest`, `handshake` and `modern`
versions; the dashboard and README state the negotiated version explicitly;
`test_client.py` asserts it is at least `2025-11-25`.

**Suggestion:** Add a one-line "protocol version in use" field to the client's
public connection object, and document the difference between the newest
revision and the compatibility handshake in the transport guide.

---

## 3. SQLite - deduplicating alerts under concurrent requests

**Tool/API/SDK:** Python standard library `sqlite3`

**Task attempted:** Make repeated or simultaneous help requests open exactly one
alert per person, without a race between "check then insert".

**Steps taken:**
1. Implemented `SELECT` open alert, then `INSERT` if none.
2. Ran five escalations concurrently in a test.
3. Saw more than one open alert.

**Expected:** A database-level guarantee, not an application-level check.

**Actual:** The check-then-insert window allowed duplicates.

**Severity:** High (the family would see the same emergency twice)

**Workaround:** A partial unique index on `(person_id, kind) WHERE resolved_at
IS NULL`, plus `INSERT ... ON CONFLICT DO NOTHING` and a follow-up select. The
test now races five threads and asserts one alert (`tests/test_db.py`).

**Suggestion:** SQLite's partial indexes are excellent but under-documented for
this "one open row" pattern. A short recipe in the Python `sqlite3` docs would
save a lot of trial and error.

---

## 4. Web Speech API - the microphone is not available in every browser

**Tool/API/SDK:** Browser Web Speech API (`SpeechRecognition` /
`webkitSpeechRecognition`)

**Task attempted:** Let a family member dictate a check-in with the microphone
button, with typing as a fallback.

**Steps taken:**
1. Added a mic button using `webkitSpeechRecognition`.
2. Tested in Chrome - recognition starts.
3. Tested in an automated/embedded Chromium session - permission is blocked.
4. Clicked the button with no microphone permission.

**Expected:** A clear, actionable message when the browser cannot grant access.

**Actual:** `onerror` fires with `not-allowed`, and some browsers expose no
`SpeechRecognition` object at all. Without handling both, the button appeared
broken.

**Severity:** Medium

**Workaround:** The UI maps `not-allowed`, `no-speech`, `audio-capture`,
`network` and unsupported-browser cases to plain-language messages, keeps the
text input usable, and marks the check-in `source: "voice"` only after a final
transcript. See `MIC_ERRORS` and `startListening()` in `static/index.html`.

**Suggestion:** Publish a single support matrix and a standard permission query
for the Web Speech API; feature detection currently differs between Chrome,
Edge and embedded browsers.

---

## 5. Time zones - UTC storage versus a local clock in the UI

**Tool/API/SDK:** Python `datetime` / SQLite timestamps / browser display

**Task attempted:** Show "next due at 11:39:14 PM" in the family's local time
while storing unambiguous timestamps.

**Steps taken:**
1. Stored ISO timestamps in UTC.
2. Rendered them directly and saw times that looked hours off.
3. Added a formatter that converts to the local zone for the reply and
   dashboard, while leaving the database values in UTC.

**Expected:** One obvious convention for "time" across db, MCP results, API and
UI.

**Actual:** Mixing naive and aware values produced off-by-one-day comparisons
in the seed when schedules were anchored to the newest check-in.

**Severity:** Medium

**Workaround:** `db.utcnow()` is the only clock; all stored values are
timezone-aware UTC ISO strings; `agent.format_due()` and the dashboard convert
for display. `tests/test_seed.py` checks that every schedule is anchored to the
person's newest check-in.

**Suggestion:** Make timezone-aware UTC the documented default in SQLite
examples, and show the conversion boundary in the MCP tool result schema.

---

## 6. `python-dotenv` - `.env` loaded in one process but not another

**Tool/API/SDK:** `python-dotenv` 1.2.4 with `mcp_server.py`, `web_app.py` and
`agent.py`

**Task attempted:** Make AI mode switch on automatically when
`ANTHROPIC_API_KEY` is added to `.env`.

**Steps taken:**
1. Added `load_dotenv()` where the data layer is imported, so the MCP server
   saw `.env`.
2. Added the key to `.env` and restarted the web app.
3. `/api/health` still reported `agent_mode: "rules"`.

**Expected:** Every process started from the project root sees the same `.env`.

**Actual:** Only modules that imported `db.py` called `load_dotenv()`. The web
app imports `agent.py` directly, so its `os.getenv()` calls ran before anything
loaded `.env`. The key existed, but AI mode stayed off.

**Severity:** Medium (a documented feature silently did not activate)

**Workaround:** `agent.py` now calls `load_dotenv()` before reading its
configuration, so the web app, the CLI and `demo.py` all get the same values.
`test_client.py` loads it too for a custom `MCP_URL`.

**Suggestion:** Make the "load once at the process entry point" rule explicit
in the dotenv docs, or offer a typed settings helper that raises when a
configuration key is present in `.env` but missing from the process
environment.

---

## 7. Vercel Functions - serverless is a different architecture

**Tool/API/SDK:** Vercel CLI 52.0.0 / Vercel Python runtime, `@vercel/python`,
`mcp` 2.3.0 in-memory transport

**Task attempted:** Publish the working two-process app (FastAPI on 8001, MCP
Streamable HTTP on 8000) as a public demo link.

**Steps taken:**
1. Added `api/index.py`, `vercel.json` and `.vercelignore`.
2. Deployed. The build installed `requirements.txt` and served the dashboard,
   but the web app could not reach the MCP server because a Vercel Function
   cannot host a second long-lived HTTP service.
3. Switched the web app to the MCP SDK's in-process client/server transport,
   seeded SQLite in `/tmp`, and enabled overdue detection on the read path.

**Expected:** A deployment should preserve the local runtime topology.

**Actual:** Vercel Functions are request-scoped. Background monitor tasks are
frozen between requests, only `/tmp` is writable, and cold starts can reset the
database. The CLI also stopped on an interactive Git-remote picker even with
`--yes`; deploying without `.git` bypassed it.

**Severity:** High for architecture, low for the demo

**Workaround:** `USE_INPROCESS_MCP=1`, `CHECKIN_DB=/tmp/checkin.db` and
`CHECKIN_SCAN_ON_READ=1` produce one self-contained Function. The canonical
Streamable HTTP server remains the local/CI path and is still verified by
`test_client.py`.

**Suggestion:** Document the serverless constraints next to the Python
runtime docs, especially "no long-lived processes", `/tmp` persistence, and a
non-interactive way to skip Git linking when a repo has multiple remotes.
