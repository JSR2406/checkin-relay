"""Scripted end-to-end demo of Check-in Relay.

This is the script to follow when recording the demo video. It drives the real
MCP server through the real agent - nothing is mocked.

    Terminal 1:  python mcp_server.py
    Terminal 2:  python web_app.py
    Terminal 3:  python demo.py

Use --fast for a 15-second schedule (about a 30-second wait), or --reset to
start from a freshly seeded database.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from datetime import datetime, timezone

from agent import CheckInAgent

MCP_URL = "http://127.0.0.1:8000/mcp"

GREEN = "\033[92m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
RESET = "\033[0m"


def step(number: str, title: str) -> None:
    print(f"\n{GREEN}[{number}] {title}{RESET}")


def say(who: str, text: str) -> None:
    print(f"{BLUE}  > {who}: \"{text}\"{RESET}")


def reply(text: str) -> None:
    for line in text.splitlines():
        print(f"{DIM}    {line}{RESET}")


async def run(fast: bool) -> int:
    interval_minutes = 0.25 if fast else 1
    grace_seconds = 5 if fast else 20
    wait_seconds = int(interval_minutes * 60) + grace_seconds + 8

    async with CheckInAgent(MCP_URL) as agent:
        print(f"Connected to the MCP server at {MCP_URL}")
        print(f"Negotiated MCP protocol version: {agent.protocol_version}")
        print(f"Tools exposed: {', '.join(await agent.list_tools())}")

        # ------------------------------------------------------------------
        step("1", "A routine check-in")
        say("Elena", "Hi, it's Elena. I'm fine, just checking in.")
        result = await agent.handle(
            "Hi, it's Elena. I'm fine, just checking in.", speaker="Elena Whitfield"
        )
        reply(result.reply)

        # ------------------------------------------------------------------
        step("2", "An explicit request for help wins over upbeat wording")
        say("Elena", "I'm fine, everything is great, but I need help.")
        result = await agent.handle(
            "I'm fine, everything is great, but I need help.", speaker="Elena Whitfield"
        )
        reply(result.reply)
        print(f"{YELLOW}  intent: {result.intent}{RESET}")
        print(f"{YELLOW}  tools:  {' -> '.join(c['tool'] for c in result.tool_calls)}{RESET}")

        status = await agent.call_tool("get_status", {})
        print(f"{YELLOW}  the dashboard now shows "
              f"{status['open_alert_count']} open alert(s){RESET}")

        # ------------------------------------------------------------------
        step("3", "A repeat request does not create a duplicate alert")
        result = await agent.handle(
            "I need help, please.", speaker="Elena Whitfield"
        )
        reply(result.reply)
        status = await agent.call_tool("get_status", {})
        print(f"{YELLOW}  the dashboard still shows "
              f"{status['open_alert_count']} open alert(s){RESET}")

        # ------------------------------------------------------------------
        step("4", "The family resolves the alert from the dashboard")
        alerts = status["open_alerts"]
        if alerts:
            alert = alerts[0]
            print(f"{DIM}  resolving alert #{alert['id']} "
                  f"({alert['kind']} for {alert['person_name']}){RESET}")
            await agent.call_tool(
                "resolve_alert", {"alert_id": alert["id"], "resolved_by": "Priya"}
            )
            status = await agent.call_tool("get_status", {})
            print(f"{YELLOW}  the dashboard now shows "
                  f"{status['open_alert_count']} open alert(s){RESET}")

        # ------------------------------------------------------------------
        step("5", "A missed check-in raises an alert after the grace window")
        interval_label = (
            f"{int(round(interval_minutes * 60))}-second"
            if interval_minutes < 1
            else f"{interval_minutes:g}-minute"
        )
        print(f"{DIM}  giving Samuel a {interval_label} schedule "
              f"with a {grace_seconds}s grace window{RESET}")
        await agent.call_tool(
            "schedule_checkin",
            {
                "person": "Samuel Whitfield",
                "interval_minutes": interval_minutes,
                "grace_seconds": grace_seconds,
            },
        )
        print(f"{DIM}  waiting {wait_seconds}s without Samuel checking in...{RESET}")
        await asyncio.sleep(wait_seconds)

        status = await agent.call_tool("get_status", {})
        missed = [
            a for a in status["open_alerts"] if a["kind"] == "missed_checkin"
        ]
        if missed:
            alert = missed[0]
            print(f"{YELLOW}  alert #{alert['id']}: {alert['message']}{RESET}")
        else:
            print(f"{RED}  no missed-check-in alert was raised{RESET}")
            return 1

        for person in status["people"]:
            print(f"{DIM}    {person['name']:<18} state={person['state']}{RESET}")

        # ------------------------------------------------------------------
        step("6", "A late check-in recovers the missed alert")
        say("Samuel", "Sorry, I was out walking the dog. I'm fine.")
        result = await agent.handle(
            "Sorry, I was out walking the dog. I'm fine.", speaker="Samuel Whitfield"
        )
        reply(result.reply)
        status = await agent.call_tool("get_status", {})
        print(f"{YELLOW}  the dashboard now shows "
              f"{status['open_alert_count']} open alert(s){RESET}")

        # ------------------------------------------------------------------
        step("7", "The family status at the end of the demo")
        for person in status["people"]:
            last = person.get("last_checkin")
            when = last["checked_at"] if last else "never"
            print(f"{DIM}    {person['name']:<18} {person['state']:<18} "
                  f"last check-in {when}{RESET}")

        print(f"\n{GREEN}Demo finished.{RESET}")
        print("Open http://127.0.0.1:8001 to show the family dashboard.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Check-in Relay demo")
    parser.add_argument(
        "--fast", action="store_true", help="shorten the missed check-in wait"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="reseed the database before the demo (run: python seed.py --reset)",
    )
    args = parser.parse_args()

    if args.reset:
        subprocess.run([sys.executable, "seed.py", "--reset"], check=True)

    print(f"{DIM}{datetime.now(timezone.utc).isoformat(timespec='seconds')}{RESET}")
    return asyncio.run(run(args.fast))


if __name__ == "__main__":
    sys.exit(main())
