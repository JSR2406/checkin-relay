"""Check-in Relay web application.

A FastAPI app on port 8001 that serves the family dashboard and the voice
simulator. It never touches the database directly - every read and every write
goes through the MCP server via the agent, so the MCP integration is the real
integration and not a documentation claim.

Run it after the MCP server::

    python web_app.py

then open http://127.0.0.1:8001
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import CheckInAgent

APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8001"))
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")
USE_INPROCESS_MCP = os.getenv("USE_INPROCESS_MCP", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

STATIC_DIR = Path(__file__).parent / "static"

# A single agent per process. Vercel may not run ASGI lifespan events, so the
# endpoints also initialize lazily on the first request.
_agent: Optional[CheckInAgent] = None
_agent_lock: Optional[asyncio.Lock] = None
_agent_init_lock: Optional[asyncio.Lock] = None


def _agent_lock_for_request() -> asyncio.Lock:
    global _agent_lock
    if _agent_lock is None:
        _agent_lock = asyncio.Lock()
    return _agent_lock


def _agent_init_lock_for_request() -> asyncio.Lock:
    global _agent_init_lock
    if _agent_init_lock is None:
        _agent_init_lock = asyncio.Lock()
    return _agent_init_lock


def _new_agent() -> CheckInAgent:
    if USE_INPROCESS_MCP:
        from mcp_server import server as mcp_server_instance

        agent = CheckInAgent(mcp_server=mcp_server_instance)
        print("[web] using the in-process MCP server")
    else:
        agent = CheckInAgent(MCP_URL)
        print(f"[web] using the MCP server at {MCP_URL}")
    return agent


async def _ensure_agent() -> CheckInAgent:
    global _agent
    if _agent is not None:
        return _agent

    async with _agent_init_lock_for_request():
        if _agent is None:
            agent = _new_agent()
            print(f"[web] agent mode: {agent.mode}")
            try:
                await agent.connect()
            except Exception as exc:  # the server may not be up yet
                print(f"[web] could not connect to the MCP server at startup: {exc}")
            _agent = agent
    return _agent

DISCLAIMER = (
    "Check-in Relay is a wellbeing reminder, not a medical service, and it is "
    "not a replacement for emergency services. Alerts appear on this "
    "dashboard only - no SMS, phone call or push notification is sent."
)


class CheckinRequest(BaseModel):
    """A phrase spoken by a family member."""

    text: str = Field(min_length=1, max_length=2000)
    speaker: Optional[str] = Field(default=None, max_length=120)
    source: str = Field(default="voice", max_length=40)


class ResolveRequest(BaseModel):
    resolved_by: str = Field(default="family", max_length=120)


class ScheduleRequest(BaseModel):
    """A short schedule set from the dashboard demo controls."""

    person: str = Field(min_length=1, max_length=120)
    interval_seconds: int = Field(default=60, gt=0, le=7 * 24 * 3600)
    grace_seconds: Optional[int] = Field(default=None, ge=0, le=600)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Connect the MCP client on startup and release it on shutdown."""
    global _agent
    agent = await _ensure_agent()
    try:
        yield
    finally:
        if _agent is agent:
            try:
                await agent.close()
            except Exception:
                pass
            _agent = None


app = FastAPI(
    title="Check-in Relay",
    version="1.0.0",
    description="Voice-first family check-in and escalation demo (Alexa+ track).",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


async def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool, turning failures into a clean 503 for the browser."""
    agent = await _ensure_agent()
    try:
        async with _agent_lock_for_request():
            return await agent.call_tool(tool, arguments)
    except Exception as exc:
        target = (
            "the in-process MCP server"
            if USE_INPROCESS_MCP
            else f"the Check-in Relay MCP server at {MCP_URL}. "
            "Start it with 'python mcp_server.py' and try again."
        )
        raise HTTPException(
            status_code=503,
            detail=f"Could not reach {target} ({exc})",
        ) from exc


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """The family dashboard and voice simulator."""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="static/index.html is missing")
    return FileResponse(index_file)


@app.get("/api/health")
async def health() -> JSONResponse:
    """Liveness for the web app plus the state of the MCP link."""
    agent = await _ensure_agent()
    body: dict[str, Any] = {
        "status": "ok",
        "app": "check-in-relay-web",
        "mcp_url": "in-process" if USE_INPROCESS_MCP else MCP_URL,
        "disclaimer": DISCLAIMER,
        "agent_mode": agent.mode,
    }
    try:
        async with _agent_lock_for_request():
            body["protocol_version"] = agent.protocol_version
            body["tools"] = await agent.list_tools()
            body["mcp_status"] = "connected"
    except Exception as exc:
        body["mcp_status"] = "unavailable"
        body["error"] = str(exc)
    return JSONResponse(body)


@app.get("/api/status")
async def status() -> JSONResponse:
    """The full dashboard snapshot, read through the MCP get_status tool."""
    data = await _call("get_status", {})
    return JSONResponse(data)


@app.get("/api/people")
async def people() -> JSONResponse:
    """The family roster, for the voice simulator's speaker picker."""
    data = await _call("get_status", {})
    roster = [
        {
            "name": person.get("name"),
            "relation": person.get("relation"),
            "state": person.get("state"),
            "last_checkin": person.get("last_checkin"),
        }
        for person in data.get("people", [])
    ]
    return JSONResponse({"people": roster})


@app.post("/api/checkin")
async def checkin(request: CheckinRequest) -> JSONResponse:
    """Turn a spoken phrase into MCP tool calls and return the reply."""
    agent = await _ensure_agent()
    try:
        async with _agent_lock_for_request():
            result = await agent.handle(
                request.text, speaker=request.speaker, source=request.source
            )
    except HTTPException:
        raise
    except Exception as exc:
        target = (
            "the in-process MCP server"
            if USE_INPROCESS_MCP
            else f"the Check-in Relay MCP server at {MCP_URL}. "
            "Start it with 'python mcp_server.py' and try again."
        )
        raise HTTPException(
            status_code=503,
            detail=f"Could not reach {target} ({exc})",
        ) from exc

    return JSONResponse(result.to_dict())


@app.post("/api/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: int, request: ResolveRequest) -> JSONResponse:
    """Close an alert from the dashboard, through MCP."""
    data = await _call("resolve_alert", {"alert_id": alert_id, "resolved_by": request.resolved_by})
    return JSONResponse(data)


@app.post("/api/schedule")
async def schedule(request: ScheduleRequest) -> JSONResponse:
    """Set a short check-in interval for the demo, through MCP.

    The dashboard controls speak in seconds so the escalation story can be
    reproduced in under a minute; the MCP tool takes fractional minutes.
    """
    data = await _call(
        "schedule_checkin",
        {
            "person": request.person,
            "interval_minutes": request.interval_seconds / 60,
            "grace_seconds": request.grace_seconds,
        },
    )
    return JSONResponse(data)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Any, exc: HTTPException) -> JSONResponse:
    """Return errors as JSON the dashboard can display."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": True, "status": exc.status_code, "detail": exc.detail},
    )


def main() -> None:
    import uvicorn

    print(f"Check-in Relay web app starting on http://{APP_HOST}:{APP_PORT}")
    print(f"Talking to the MCP server at {MCP_URL}")
    uvicorn.run(app, host=APP_HOST, port=APP_PORT, log_level="info")


if __name__ == "__main__":
    main()
