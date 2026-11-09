"""Zero-dependency Model Context Protocol (MCP) stdio server for the Oura + Strava Half-Marathon Coach."""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
from typing import Any

from .client import OuraAPIError, OuraClient
from .coach import (
    auto_adapt_plan,
    ensure_plan_initialized,
    evaluate_readiness,
    get_coach_overview,
    manual_swap_days,
)
from .config import ConfigStore
from .oauth import OAuthManager, OuraOAuthError
from .storage import OuraStorage
from .strava import StravaError, StravaManager

MCP_PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "coach_morning_briefing",
        "description": (
            "Compute today's Readiness-to-Train signal (GREEN/YELLOW/RED) from rolling 28-day Oura HRV/RHR/temp/stress "
            "and Strava GPS load, auto-adapt the 17-week Publix Atlanta Sub-2:00 Half-Marathon plan if needed, "
            "and return today's workout + weekly schedule."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Optional YYYY-MM-DD date (defaults to today).",
                },
                "sync_live": {
                    "type": "boolean",
                    "description": "If true, attempt to sync recent Oura and Strava data before evaluating.",
                },
            },
        },
    },
    {
        "name": "coach_get_training_plan",
        "description": (
            "Fetch a specific week (1..17) or date range of the Publix Atlanta Sub-2:00 Half-Marathon plan, "
            "including matched Strava GPS runs, elevation gain, check-ins, and recent adaptations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "week_num": {
                    "type": "integer",
                    "description": "Plan week number 1..17 (1 = Nov 9, 2026; 17 = Race Week Mar 6, 2027).",
                },
                "start_date": {"type": "string", "description": "Optional YYYY-MM-DD start filter."},
                "end_date": {"type": "string", "description": "Optional YYYY-MM-DD end filter."},
            },
        },
    },
    {
        "name": "coach_sync_telemetry",
        "description": "Pull latest Oura Ring v2 biometrics and Strava v3 GPS runs into local SQLite and run auto-adaptation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of lookback days to sync (default 14).",
                }
            },
        },
    },
    {
        "name": "coach_log_checkin",
        "description": (
            "Record completed run distance/duration override and/or subjective runner feedback "
            "(RPE 1-10, muscle/tendon soreness 0-10, notes like calf tightness or travel) "
            "for a given date and automatically adapt the training week if needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD date (defaults to today)."},
                "miles": {"type": "number", "description": "Optional run distance in miles (for treadmill/phone-free runs)."},
                "duration_min": {"type": "number", "description": "Optional run duration in minutes."},
                "elevation_ft": {"type": "number", "description": "Optional hill elevation gain in feet."},
                "rpe": {"type": "integer", "description": "Perceived exertion 1-10."},
                "soreness": {"type": "integer", "description": "Muscle/tendon soreness or tightness 0-10 (>=7 triggers RED rest)."},
                "notes": {"type": "string", "description": "Free-text notes (soreness location, travel, life stress)."},
            },
        },
    },
    {
        "name": "coach_swap_or_edit_workout",
        "description": (
            "Swap workouts between two calendar dates OR customize a single date's workout (miles, title, target_pace, workout_type). "
            "All edits are recorded in the adaptation audit log so they can be undone in 1 click."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Primary YYYY-MM-DD date to swap or edit."},
                "swap_with_date": {"type": "string", "description": "Optional second YYYY-MM-DD date to swap workouts with."},
                "title": {"type": "string", "description": "Optional updated workout title (when editing single date)."},
                "planned_miles": {"type": "number", "description": "Optional updated planned mileage."},
                "target_pace": {"type": "string", "description": "Optional updated target pace string."},
                "workout_type": {"type": "string", "description": "Optional workout_type (rest, easy, recovery, strides, tempo, hills, hmp, long_run)."},
                "reason": {"type": "string", "description": "Coaching rationale recorded in the audit log."},
            },
            "required": ["date"],
        },
    },
    {
        "name": "coach_undo_adaptation",
        "description": "Revert an automatic or manual plan adaptation by ID (or revert the latest active adaptation if omitted).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "adaptation_id": {"type": "integer", "description": "Optional adaptation ID from the audit log."}
            },
        },
    },
]


class CoachMCPDispatcher:
    """Executes MCP tool calls against the local Oura SQLite database."""

    def __init__(self, store: ConfigStore | None = None) -> None:
        self.store = store or ConfigStore()
        self.oauth = OAuthManager(self.store)
        self.oura_client = OuraClient(self.oauth)
        self.strava = StravaManager(self.store)
        self.storage = OuraStorage(self.store.db_path)
        ensure_plan_initialized(self.storage)

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        today_str = str(args.get("date") or dt.date.today().isoformat())

        if name == "coach_morning_briefing":
            sync_notes: dict[str, Any] = {}
            if args.get("sync_live"):
                sync_notes = self._try_live_sync(days=7)
            adaptation = auto_adapt_plan(self.storage, target_day=today_str)
            overview = get_coach_overview(self.storage, today_str=today_str)
            overview["adaptation_result"] = adaptation
            if sync_notes:
                overview["sync_notes"] = sync_notes
            return overview

        if name == "coach_get_training_plan":
            week_num = args.get("week_num")
            start_date = args.get("start_date")
            end_date = args.get("end_date")
            if week_num is None and not start_date and not end_date:
                cur = self.storage.get_plan_day(dt.date.today().isoformat())
                week_num = int(cur["week_num"]) if cur else 1
            days = self.storage.get_plan_days(
                start_date=start_date,
                end_date=end_date,
                week_num=int(week_num) if week_num is not None else None,
            )
            return {
                "week_num": week_num,
                "planned_miles_total": round(sum(float(d["planned_miles"] or 0.0) for d in days), 1),
                "completed_miles_total": round(sum(float(d["actual_miles"] or 0.0) for d in days), 2),
                "completed_elevation_ft": round(sum(float(d["actual_elevation_ft"] or 0.0) for d in days), 0),
                "days": days,
                "recent_adaptations": self.storage.get_adaptations(limit=5),
            }

        if name == "coach_sync_telemetry":
            days = max(1, min(365, int(args.get("days") or 14)))
            sync_info = self._try_live_sync(days=days)
            adaptation = auto_adapt_plan(self.storage)
            return {"sync": sync_info, "adaptation": adaptation}

        if name == "coach_log_checkin":
            updated_day = self.storage.save_checkin(
                day=today_str,
                rpe=int(args["rpe"]) if args.get("rpe") is not None else None,
                soreness=int(args["soreness"]) if args.get("soreness") is not None else None,
                notes=str(args["notes"]) if args.get("notes") is not None else None,
                miles=float(args["miles"]) if args.get("miles") is not None else None,
                duration_min=float(args["duration_min"]) if args.get("duration_min") is not None else None,
                elevation_ft=float(args["elevation_ft"]) if args.get("elevation_ft") is not None else None,
            )
            adaptation = auto_adapt_plan(self.storage, target_day=today_str)
            return {
                "day": updated_day,
                "readiness": evaluate_readiness(self.storage, target_day=today_str),
                "adaptation": adaptation,
            }

        if name == "coach_swap_or_edit_workout":
            swap_with = args.get("swap_with_date")
            reason = str(args.get("reason") or "Coach schedule adjustment")
            if swap_with:
                return manual_swap_days(self.storage, day_a=today_str, day_b=str(swap_with), reason=reason)

            existing = self.storage.get_plan_day(today_str)
            if not existing:
                raise ValueError(f"Date {today_str} is outside the 17-week training plan.")
            prev = copy.deepcopy(existing)
            upd = copy.deepcopy(existing)
            if args.get("title") is not None:
                upd["title"] = str(args["title"])
            if args.get("planned_miles") is not None:
                upd["planned_miles"] = float(args["planned_miles"])
            if args.get("target_pace") is not None:
                upd["target_pace"] = str(args["target_pace"])
            if args.get("workout_type") is not None:
                upd["workout_type"] = str(args["workout_type"])
                upd["is_quality"] = upd["workout_type"] in ("tempo", "hills", "hmp", "long_run", "race")
            self.storage.upsert_plan_days([upd], overwrite_existing=True)
            adap_id = self.storage.record_adaptation(
                target_day=today_str,
                signal_level="COACH_EDIT",
                reason=f"{reason}: updated {today_str} from '{prev['title']}' to '{upd['title']}'.",
                metrics={},
                previous_days=[prev],
                updated_days=[upd],
            )
            return {"adaptation_id": adap_id, "updated_day": upd}

        if name == "coach_undo_adaptation":
            adap_id = args.get("adaptation_id")
            if adap_id is None:
                active = [a for a in self.storage.get_adaptations(limit=10) if a["reverted_at"] is None]
                if not active:
                    raise ValueError("No active adaptations to undo.")
                adap_id = active[0]["id"]
            return self.storage.undo_adaptation(int(adap_id))

        raise ValueError(f"Unknown tool: {name}")

    def _try_live_sync(self, days: int = 14) -> dict[str, Any]:
        end_dt = dt.date.today()
        start_dt = end_dt - dt.timedelta(days=days - 1)
        report: dict[str, Any] = {}

        if self.store.load_tokens() is not None:
            try:
                report["oura"] = self.storage.sync_range(
                    self.oura_client,
                    start_date=start_dt.isoformat(),
                    end_date=end_dt.isoformat(),
                    include_heartrate=False,
                )
            except (OuraOAuthError, OuraAPIError, OSError) as exc:
                report["oura_error"] = str(exc)

        if self.strava.load_tokens() is not None:
            try:
                runs = self.strava.fetch_runs(start_date=start_dt.isoformat(), end_date=end_dt.isoformat())
                report["strava_runs_synced"] = self.storage.upsert_strava_runs(runs)
            except (StravaError, OSError) as exc:
                report["strava_error"] = str(exc)

        return report


def handle_jsonrpc_message(msg: dict[str, Any], dispatcher: CoachMCPDispatcher) -> dict[str, Any] | None:
    """Process a single JSON-RPC 2.0 request and return response dict (or None for notifications)."""
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if req_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oura-coach", "version": "0.2.0"},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        tool_args = params.get("arguments") or {}
        try:
            result = dispatcher.call_tool(tool_name, tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "isError": False,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve_stdio() -> int:
    """Run MCP JSON-RPC 2.0 stdio loop auto-supporting both newline-JSON and Content-Length framing."""
    dispatcher = CoachMCPDispatcher()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        first_line = stdin.readline()
        if not first_line:
            break
        stripped = first_line.strip()
        if not stripped:
            continue

        use_headers = False
        if stripped.lower().startswith(b"content-length:"):
            use_headers = True
            length = int(stripped.split(b":", 1)[1].strip())
            while True:
                hdr = stdin.readline()
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
            raw_body = stdin.read(length)
        else:
            raw_body = stripped

        try:
            msg = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            continue

        resp = handle_jsonrpc_message(msg, dispatcher)
        if resp is not None:
            encoded = json.dumps(resp).encode("utf-8")
            if use_headers:
                stdout.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
            else:
                stdout.write(encoded + b"\n")
            stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(serve_stdio())
