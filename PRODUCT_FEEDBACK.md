# Product feedback

The Amazon Developer Hackathon asks for feedback on every tool, API and SDK
used in the project. This is ours, written by the people who built it.

Everything listed here was actually used to build Check-in Relay, on Windows 11
with Python 3.11.9.

---

## 1. Model Context Protocol Python SDK (`mcp` 2.3.0)

**How we used it.** The whole domain of Check-in Relay is exposed as MCP tools
on a Streamable HTTP server (`mcp_server.py`), and every read and write the
web app performs goes through an MCP client (`agent.py`). We used
`MCPServer` and its `@server.tool` decorator to register five tools, the
`Client` class with `mode="auto"` on the client side, `custom_route` for a
`/health` endpoint outside the protocol, and the `lifespan` hook to run the
missed-check-in monitor inside the server process.

**Strengths.** The tool decorator derives the JSON schema for each tool from
the function signature and type hints, so the tool contracts are the code, not
a parallel document that can drift. Pydantic return models give every tool a
real `outputSchema` and a structured result alongside the text block. The
`Client` class made the client side about ten lines. The protocol version
registry (`mcp_types.version`) is a genuine asset: `is_version_at_least()`
took all the guesswork out of verifying the hackathon's
"2025-11-25 or later" rule, and `KNOWN_PROTOCOL_VERSIONS` let us confirm the
negotiated `2026-07-28` instead of trusting the installed SDK.

**Limitations.** Version 2 renamed `FastMCP` to `MCPServer` and moved it from
`mcp.server.fastmcp` to `mcp.server.mcpserver`. The old import path raises a
`ModuleNotFoundError` with a migration hint rather than a normal
`ImportError`, which is friendly but means a stale `requirements.txt` fails
loudly instead of degrading quietly. Structured output requires a Pydantic
model as the return type: annotating `-> dict` with `structured_output=True`
raises `InvalidSignature` ("return type <class 'dict'> is not serializable for
structured output"), so we wrote explicit models for all five tools. A tool
that raises inside the SDK returns `is_error=True` with the exception message,
and our first client silently ignored that flag and reported a successful
call - we had to add our own error check.

**Onboarding.** The quickstart is genuinely quick, but the 1.x-to-2.x
migration is the part that costs time. The error message points at the right
page, and the concepts (tools, sessions, transports, the transport choice
between stdio and Streamable HTTP) transferred directly. The thing we would
have liked more is a clearer statement that `mode="auto"` probes
`server/discover` and may land on the modern per-request envelope rather than
the `initialize` handshake - we only found that by reading `client/_probe.py`.

**Would we use it again?** Yes, without hesitating. It is the only reason this
project can honestly claim an MCP integration. The migration cost was about
an hour and paid for itself immediately.

---

## 2. Streamable HTTP transport

**How we used it.** The server runs with
`run_streamable_http_async(host, port, streamable_http_path="/mcp")`, and the
client connects over `http://127.0.0.1:8000/mcp`.

**Strengths.** One endpoint handles requests, responses and server-initiated
messages, and the same code serves the browser-facing web app and the CLI
client. The session model is handled for us, and `GET /health` on the same
port lets us check the server without a second port.

**Limitations.** We run the server bound to `127.0.0.1` with no
authentication, which is right for a demo and wrong for anything else. The
session id and resumption details are handled internally, so a client that
loses its session has to reconnect - which is why `agent.py` rebuilds the
session and retries once on failure.

**Onboarding.** The transport choice is well documented, and the health-route
pattern (`custom_route`) made adding an operational endpoint easy.

**Would we use it again?** Yes. It is the right transport for a service that
a browser and a CLI both need to reach.

---

## 3. FastAPI (`fastapi` 0.159.1) and Uvicorn (`uvicorn` 0.54.0)

**How we used it.** `web_app.py` is a FastAPI application serving the static
dashboard and six JSON endpoints. Uvicorn runs it on port 8001.

**Strengths.** Pydantic request models gave us validated bodies and automatic
`422` responses with no extra code. The `lifespan` context manager is the
cleanest place to open and close the MCP client session. `StaticFiles` mounted
the dashboard in one line. Custom exception handlers turned every failure into
a JSON body the dashboard could display, which is exactly what the "handle
server failures gracefully" requirement needed.

**Limitations.** The default `422` validation error body does not match the
shape of our own error responses, so the dashboard has to handle two error
formats. We would have preferred to disable the automatic MCP session
reconnect behaviour for the demo so that a restarted MCP server is visible to
the user rather than being papered over.

**Onboarding.** Very familiar. The `lifespan` API and the dependency
injection style both worked the way we expected, and the docs matched the
installed version.

**Would we use it again?** Yes.

---

## 4. Starlette (`starlette` 1.9.0) and SSE-Starlette (`sse-starlette` 3.5.0)

**How we used it.** Starlette underpins FastAPI and provided the `Request`
and `Response` types for the MCP server's `/health` route. SSE-Starlette is a
transitive dependency of the MCP SDK's Streamable HTTP implementation, where
it carries the server-to-client event stream.

**Strengths.** The `custom_route` decorator on `MCPServer` accepts a plain
Starlette handler, so adding `/health` took four lines without importing
anything.

**Limitations.** We never had to use SSE-Starlette directly, but we had to
understand it to reason about why the MCP POST response is a stream. The
transitive nature of the dependency means we had to check the installed
version to be sure what we were relying on.

**Onboarding.** Not really applicable - we used Starlette directly and
inherited SSE-Starlette.

**Would we use it again?** Starlette, yes, as a deliberate choice. We would
not choose SSE-Starlette directly for this project.

---

## 5. Pydantic (`pydantic` 2.14.1)

**How we used it.** Every MCP tool has a Pydantic model for its input (via
the type hints) and its output. The FastAPI request bodies are Pydantic
models. `ConfigDict(extra="allow")` lets the data layer pass through fields
the tool model does not declare.

**Strengths.** One model gives us the tool's `inputSchema`, its `outputSchema`
and the runtime validation of the result. The `extra="allow"` escape hatch
meant we did not have to mirror every column of the database into a dozen
near-identical models.

**Limitations.** `extra="allow"` means a typo in a field name is silently
accepted rather than reported, which we had to be careful about. Returning a
plain `dict` from an MCP tool does not produce structured output at all - the
result is text-only with a `null` output schema - so the models are doing real
work, not decoration.

**Onboarding.** Straightforward, and the error messages when a model is wrong
point straight at the field.

**Would we use it again?** Yes, it is already the backbone of the type
contracts.

---

## 6. SQLite via the standard library (`sqlite3`)

**How we used it.** The whole data layer: people, check-ins, schedules, alerts
and an event log. WAL mode, a foreign-key-aware schema, and a partial unique
index (`alerts(person_id, kind) WHERE status = 'open'`) that enforces one open
alert per person per kind.

**Strengths.** Zero dependencies and one file to copy. The partial unique
index turned out to be the cleanest possible answer to "do not create
duplicate alerts": it is enforced by the database rather than by application
logic, so it holds even when two requests arrive at the same moment. WAL mode
means the MCP server and the web app can both read and write the same file
without blocking each other.

**Limitations.** We opened a connection per operation rather than pooling,
which is fine at demo scale and wasteful at real scale. `isolation_level=None`
(the autocommit default) silently broke our explicit `COMMIT` until we
switched to the default deferred transaction handling. There is no
cross-process locking beyond SQLite's own, so a very busy write path would
need attention.

**Onboarding.** The standard library needs no onboarding. The two things that
cost us time were the isolation-level behaviour above and remembering that
`executescript` issues an implicit commit before it runs.

**Would we use it again?** Yes. For a single-node family service this is the
right size of database, and the partial index gave us a correctness guarantee
that would otherwise have taken application-level locking.

---

## 7. `python-dotenv` (`python-dotenv` 1.2.4)

**How we used it.** Every module calls `load_dotenv()` once and then reads its
configuration from the environment: ports, the MCP URL, the grace window, the
database path, the monitor interval.

**Strengths.** Twelve lines of dependency removed. It does not overwrite
variables that are already set, which is what let the test suite point the
data layer at a temporary database before importing it.

**Limitations.** It only reads a `.env` file from the working directory or its
parents, so the app has to be started from the project root. The `.env` file
still has to be git-ignored by hand, which we did.

**Onboarding.** Immediate. The README of the project is one command long.

**Would we use it again?** Yes. For a twelve-variable application, `os.environ`
plus a committed `.env.example` would have been almost as good.

---

## 8. AnyIO (`anyio` 4.15.1) and HTTPX2 (`httpx2` 2.16.0)

**How we used it.** Both are transitive dependencies of the MCP SDK. AnyIO
runs the SDK's sync tools on a worker thread - which is why our synchronous
SQLite calls do not block the MCP server's event loop - and HTTPX2 is the
HTTP client underneath the MCP client transport.

**Strengths.** The thread-offload behaviour meant we could write plain
synchronous database functions and still have a responsive async server. The
MCP client reconnection logic in `agent.py` is straightforward because the
transport underneath is well behaved.

**Limitations.** We only met them indirectly. Debugging a connection issue
means reading through two layers of the MCP SDK to reach them, and the
`httpx2` name (rather than `httpx`) briefly sent us looking for the wrong
package.

**Onboarding.** Indirect - we read the source of the MCP SDK to understand
them rather than learning them directly.

**Would we use it again?** AnyIO, yes, for the same thread-offload reason.
HTTPX2, we would not choose directly today.

---

## 9. The standard library: `unittest`, `argparse`, `json`, `logging`

**How we used it.** `unittest` runs all 40 tests with no extra dependency.
`argparse` drives `demo.py`, `seed.py` and `test_client.py`. `json` is used
for the CLI output and in the browser.

**Strengths.** The tests run on a clean clone with `pip install -r
requirements.txt` and nothing else, which matters when a judge has to verify
the project quickly. `unittest` discovery over `tests/` needed no configuration
at all.

**Limitations.** `unittest` has no built-in fixtures or parametrised tests, so
a few of our intent tests use a small `assertIntent` helper instead. There is
no built-in HTML report, though `-v` output was enough for us.

**Onboarding.** None needed.

**Would we use it again?** Yes. Zero test dependencies was a deliberate
choice for this project, and it is the reason the test suite can be verified
in a clean clone.

---

## 10. MCP Inspector - considered, not used

**How we used it.** Not used for the checked-in demo. `test_client.py` is the
repeatable client: it connects over Streamable HTTP, prints the negotiated
protocol version, lists every tool with its arguments, and calls all four
required tools. The `/health` route gives the same at-a-glance view.

**Why.** A scripted client is repeatable in CI and leaves no manual steps for a
judge. The Inspector is better for ad-hoc JSON-RPC experimentation, which was
not needed after the protocol tests passed.

**Limitations.** The Inspector can issue arbitrary raw JSON-RPC, which is
useful for testing protocol edge cases we did not need.

**Onboarding.** We read the tool listing our own client printed and knew what
was registered, which is the part we actually needed.

**Would we use it again?** Yes, for interactive protocol debugging.

---

## 11. Vercel Functions - deployment

**How we used it.** The public demo runs as one Vercel Python Function:
`api/index.py` exports the FastAPI `app`, and `vercel.json` routes every path
to it. The MCP server connects to the same process through the SDK's in-memory
transport instead of a second HTTP service.

**What worked well.** The Python build picked up `requirements.txt`, the ASGI
app deployed without a custom server, and the static dashboard was included
with `includeFiles`. Cold starts were fast enough for the demo.

**Limitations.**
- A Function is request-scoped, not a long-running host, so the two-process
  Streamable HTTP design used locally cannot be deployed as-is.
- Only `/tmp` is writable, so SQLite lives there and synthetic demo data resets
  on a cold start.
- Background tasks freeze between requests, so overdue schedules are reconciled
  on the dashboard read path (`CHECKIN_SCAN_ON_READ=1`).

**Would we use it again?** Yes for a serverless demo. For a real care product we
would use a managed database and a host that supports the always-on MCP server
and monitor.

---

## Summary

| Dependency | Role | Would use again |
| --- | --- | --- |
| MCP Python SDK 2.3.0 | Tool server, tool client, Streamable HTTP | Yes |
| Streamable HTTP transport | The wire protocol | Yes |
| FastAPI 0.159.1 | Web app and REST endpoints | Yes |
| Uvicorn 0.54.0 | ASGI server | Yes |
| Starlette 1.9.0 | ASGI foundation, `/health` route | Yes |
| SSE-Starlette 3.5.0 | Event stream under Streamable HTTP | Indirectly |
| Pydantic 2.14.1 | Tool schemas and validation | Yes |
| SQLite (`sqlite3`) | Data layer | Yes |
| python-dotenv 1.2.4 | Configuration | Yes |
| AnyIO 4.15.1 | Thread offload for sync tools | Yes |
| HTTPX2 2.16.0 | HTTP under the MCP client | Indirectly |
| Standard library | Tests, CLI, JSON | Yes |
| MCP Inspector | Protocol exploration | Yes, for ad-hoc debugging |
| Vercel Functions | Public serverless demo | Yes, with caveats |

The most useful thing in the whole stack was the MCP SDK. The most useful
thing in the rest was the fact that it was all small enough to read.
