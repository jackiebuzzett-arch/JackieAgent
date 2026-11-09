"""Zero-dependency Model Context Protocol (MCP) stdio server with OTel tracing, HITL gates, and actionable error recovery."""

from __future__ import annotations

import copy
import datetime as dt
import json
import secrets
import sys
import time
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
from .errors import ActionableToolError, classify_exception_to_actionable_error
from .oauth import OAuthManager, OuraOAuthError
from .observability import (
    DEFAULT_INTENT_TRACKER,
    DEFAULT_LOGGER,
    DEFAULT_TRACER,
    PIIRedactor,
    log_event,
)
from .orchestrator import MultiAgentCoachOrchestrator
from .storage import OuraStorage
from .strava import StravaError, StravaManager

MCP_PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "coach_morning_briefing",
        "description": (
            "Compute today's Readiness-to-Train signal (GREEN/YELLOW/RED) from rolling 28-day Oura HRV/RHR/temp/stress "
            "and Oura workouts, auto-adapt the 17-week Publix Atlanta Sub-2:00 Half-Marathon plan if needed, "
            "and return today's workout + weekly schedule."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Optional YYYY-MM-DD date (defaults to today)."},
                "sync_live": {"type": "boolean", "description": "If true, sync recent Oura data before evaluating."},
            },
        },
    },
    {
        "name": "coach_ask_orchestrator",
        "description": (
            "Invoke the 4-Agent Supervisor Pipeline (TriageRouter -> PhysiologyAnalyst -> TrainingArchitect -> SafetyCritic) "
            "with Gemini 3 model routing, input/output guardrails, LLM history compaction, async memory consolidation, "
            "and Human-in-the-Loop (HITL) approval staging for high-impact schedule changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Natural-language coaching question, schedule request, or injury report."},
                "session_id": {"type": "string", "description": "Session identifier for multi-turn compacted memory (default: 'default')."},
                "date": {"type": "string", "description": "Optional YYYY-MM-DD reference date."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "coach_get_training_plan",
        "description": (
            "Fetch a specific week (1..17) or date range of the Publix Atlanta Sub-2:00 Half-Marathon plan, "
            "including matched Oura running workouts, check-ins, and recent adaptations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "week_num": {"type": "integer", "description": "Plan week number 1..17 (1 = Nov 9, 2026; 17 = Race Week Mar 6, 2027)."},
                "start_date": {"type": "string", "description": "Optional YYYY-MM-DD start filter."},
                "end_date": {"type": "string", "description": "Optional YYYY-MM-DD end filter."},
            },
        },
    },
    {
        "name": "coach_sync_telemetry",
        "description": "Pull latest Oura Ring v2 biometrics and running workouts into local SQLite and run auto-adaptation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Number of lookback days to sync (default 14)."}
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
            "High-impact mutations are gated by HITL unless `bypass_hitl=true` is explicitly authorized."
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
                "bypass_hitl": {"type": "boolean", "description": "Set true only after explicit human confirmation."},
            },
            "required": ["date"],
        },
    },
    {
        "name": "coach_list_pending_approvals",
        "description": "List pending Human-in-the-Loop (HITL) schedule approval requests awaiting athlete confirmation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by status ('PENDING', 'APPROVED', 'REJECTED'); defaults to 'PENDING'."}
            },
        },
    },
    {
        "name": "coach_resolve_approval",
        "description": "Approve or reject a staged Human-in-the-Loop (HITL) training plan mutation by `approval_id`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "integer", "description": "ID of the pending HITL approval request."},
                "approved": {"type": "boolean", "description": "True to commit the proposed plan change, False to reject it."},
                "note": {"type": "string", "description": "Optional athlete confirmation note."},
            },
            "required": ["approval_id", "approved"],
        },
    },
    {
        "name": "coach_undo_adaptation",
        "description": "Revert an automatic or approved plan adaptation by ID (or revert the latest active adaptation if omitted).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "adaptation_id": {"type": "integer", "description": "Optional adaptation ID from the audit log."}
            },
        },
    },
]


class CoachMCPDispatcher:
    """Executes MCP tool calls with OpenTelemetry spans, Intent-vs-Outcome tracking, and actionable recovery."""

    def __init__(self, store: ConfigStore | None = None) -> None:
        self.store = store or ConfigStore()
        self.oauth = OAuthManager(self.store)
        self.oura_client = OuraClient(self.oauth)
        self.strava = StravaManager(self.store)
        self.storage = OuraStorage(self.store.db_path)
        ensure_plan_initialized(self.storage)
        self.orchestrator = MultiAgentCoachOrchestrator(store=self.store)

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        corr_id = f"mcp-{secrets.token_hex(5)}"
        t0 = time.perf_counter()

        with DEFAULT_TRACER.start_as_current_span(
            f"mcp.tool.{name}",
            {"mcp.tool.name": name, "correlation.id": corr_id},
        ) as span:
            DEFAULT_INTENT_TRACKER.record_intent(
                correlation_id=corr_id,
                declared_intent=f"Execute MCP tool `{name}`",
                selected_agent="CoachMCPDispatcher",
                routed_model="mcp-tool-executor",
                planned_tools=[name],
            )
            try:
                result = self._dispatch_inner(name, args)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                span.set_attribute("mcp.tool.status", "ok")
                DEFAULT_INTENT_TRACKER.record_outcome(
                    correlation_id=corr_id,
                    declared_intent=f"Execute MCP tool `{name}`",
                    actual_outcome=f"Tool `{name}` succeeded",
                    status="OK",
                    executed_tools=[name],
                    latency_ms=elapsed_ms,
                )
                return PIIRedactor.redact(result)
            except BaseException as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                actionable = classify_exception_to_actionable_error(exc, tool_name=name)
                span.record_exception(actionable)
                DEFAULT_INTENT_TRACKER.record_outcome(
                    correlation_id=corr_id,
                    declared_intent=f"Execute MCP tool `{name}`",
                    actual_outcome=f"{actionable.error_code}: {actionable.user_message}",
                    status="ERROR",
                    executed_tools=[name],
                    intent_satisfied=False,
                    drift_reason=actionable.error_code,
                    latency_ms=elapsed_ms,
                )
                raise actionable from exc

    def _dispatch_inner(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        today_str = str(args.get("date") or dt.date.today().isoformat())
        dt.date.fromisoformat(today_str)  # Validate ISO date format early

        if name == "coach_morning_briefing":
            sync_notes: dict[str, Any] = {}
            if args.get("sync_live"):
                sync_notes = self._try_live_sync(days=7)
            adaptation = auto_adapt_plan(self.storage, target_day=today_str)
            overview = get_coach_overview(self.storage, today_str=today_str)
            overview["adaptation_result"] = adaptation
            overview["pending_hitl_approvals"] = self.orchestrator.hitl.list_approvals(status="PENDING", limit=5)
            if sync_notes:
                overview["sync_notes"] = sync_notes
            return overview

        if name == "coach_ask_orchestrator":
            prompt = str(args.get("prompt") or "").strip()
            if not prompt:
                raise ActionableToolError(
                    error_code="EMPTY_ORCHESTRATOR_PROMPT",
                    message="`prompt` cannot be empty when calling `coach_ask_orchestrator`.",
                    retryable=True,
                    recovery_instructions=[
                        "Provide a non-empty natural-language `prompt` describing the athlete's question, schedule request, or symptom."
                    ],
                )
            session_id = str(args.get("session_id") or "default")
            return self.orchestrator.handle_request(
                user_prompt=prompt,
                session_id=session_id,
                reference_date=today_str,
            )

        if name == "coach_get_training_plan":
            week_num = args.get("week_num")
            if week_num is not None and not (1 <= int(week_num) <= 17):
                raise ActionableToolError(
                    error_code="WEEK_NUM_OUT_OF_RANGE",
                    message=f"`week_num={week_num}` is outside the 17-week Publix Atlanta plan (valid range: 1..17).",
                    retryable=True,
                    suggested_tool="coach_get_training_plan",
                    valid_parameters={"min_week_num": 1, "max_week_num": 17},
                    recovery_instructions=[
                        "Retry `coach_get_training_plan` with `week_num` between 1 and 17, or omit `week_num` to fetch the active week."
                    ],
                )
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
            if updated_day is None:
                raise ValueError(f"Date {today_str} is outside the 17-week training plan.")
            adaptation = auto_adapt_plan(self.storage, target_day=today_str)
            return {
                "day": updated_day,
                "readiness": evaluate_readiness(self.storage, target_day=today_str),
                "adaptation": adaptation,
            }

        if name == "coach_swap_or_edit_workout":
            swap_with = args.get("swap_with_date")
            reason = str(args.get("reason") or "Coach schedule adjustment")
            bypass_hitl = bool(args.get("bypass_hitl", False))

            if swap_with:
                day_a_row = self.storage.get_plan_day(today_str)
                day_b_row = self.storage.get_plan_day(str(swap_with))
                if not day_a_row or not day_b_row:
                    raise ValueError(f"Both {today_str} and {swap_with} must be within the 17-week training plan.")
                if not bypass_hitl and (day_a_row.get("workout_type") == "long_run" or day_b_row.get("workout_type") == "long_run"):
                    upd_a, upd_b = copy.deepcopy(day_a_row), copy.deepcopy(day_b_row)
                    for k in ("workout_type", "title", "description", "planned_miles", "target_pace", "hill_focus", "strength_addon", "is_quality"):
                        upd_a[k], upd_b[k] = upd_b[k], upd_a[k]
                    pending = self.orchestrator.hitl.create_pending_approval(
                        session_id="mcp",
                        action_type="LONG_RUN_SWAP",
                        summary=f"Swap {today_str} ('{day_a_row['title']}') with {swap_with} ('{day_b_row['title']}')",
                        rationale="Modifies anchored weekend Long Run — requires explicit athlete HITL approval.",
                        previous_days=[day_a_row, day_b_row],
                        proposed_days=[upd_a, upd_b],
                    )
                    return {"status": "PENDING_APPROVAL", "pending_approval": pending}
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

            needs_hitl, hitl_reason = self.orchestrator.hitl.requires_approval("MCP_EDIT", [prev], [upd])
            if needs_hitl and not bypass_hitl:
                pending = self.orchestrator.hitl.create_pending_approval(
                    session_id="mcp",
                    action_type="WORKOUT_VOLUME_EDIT",
                    summary=f"Update {today_str} from '{prev['title']}' ({prev['planned_miles']} mi) to '{upd['title']}' ({upd['planned_miles']} mi)",
                    rationale=hitl_reason,
                    previous_days=[prev],
                    proposed_days=[upd],
                )
                return {"status": "PENDING_APPROVAL", "pending_approval": pending}

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

        if name == "coach_list_pending_approvals":
            status_filter = str(args.get("status") or "PENDING").upper()
            return {"approvals": self.orchestrator.hitl.list_approvals(status=status_filter)}

        if name == "coach_resolve_approval":
            approval_id = int(args["approval_id"])
            approved = bool(args["approved"])
            note = str(args.get("note") or "")
            return self.orchestrator.hitl.resolve_approval(approval_id=approval_id, approved=approved, note=note)

        if name == "coach_undo_adaptation":
            adap_id = args.get("adaptation_id")
            if adap_id is None:
                active = [a for a in self.storage.get_adaptations(limit=10) if a["reverted_at"] is None]
                if not active:
                    raise ValueError("Adaptation not found: no active adaptations available to undo.")
                adap_id = active[0]["id"]
            return self.storage.undo_adaptation(int(adap_id))

        raise ActionableToolError(
            error_code="UNKNOWN_TOOL_NAME",
            message=f"Unknown MCP tool `{name}`.",
            retryable=True,
            valid_parameters={"available_tools": [t["name"] for t in TOOLS]},
            recovery_instructions=[
                "Inspect `valid_parameters.available_tools` and retry using one of the registered MCP tool names."
            ],
        )

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
                report["oura_error"] = PIIRedactor.redact_text(str(exc))

        if self.strava.load_tokens() is not None:
            try:
                runs = self.strava.fetch_runs(start_date=start_dt.isoformat(), end_date=end_dt.isoformat())
                report["strava_runs_synced"] = self.storage.upsert_strava_runs(runs)
            except (StravaError, OSError) as exc:
                report["strava_error"] = PIIRedactor.redact_text(str(exc))

        return report


def handle_jsonrpc_message(msg: dict[str, Any], dispatcher: CoachMCPDispatcher) -> dict[str, Any] | None:
    """Process a single JSON-RPC 2.0 request and return structured MCP response."""
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if req_id is None:
        return None

    if method == "initialize":
        log_event(DEFAULT_LOGGER, "mcp.initialize", "Initialized oura-coach MCP stdio server")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oura-coach", "version": "0.3.0"},
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
        except ActionableToolError as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(exc.to_payload(), indent=2)}],
                    "isError": True,
                },
            }
        except Exception as exc:
            fallback_err = classify_exception_to_actionable_error(exc, tool_name=tool_name)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(fallback_err.to_payload(), indent=2)}],
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
