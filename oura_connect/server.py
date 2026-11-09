"""Local HTTP server handling Oura & Strava OAuth callbacks, Coach API routes, and web dashboard."""

from __future__ import annotations

import datetime as dt
import html
import json
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .client import OuraAPIError, OuraClient
from .coach import (
    auto_adapt_plan,
    ensure_plan_initialized,
    get_coach_overview,
    manual_swap_days,
)
from .config import AppConfig, ConfigStore
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
from .strava import StravaConfig, StravaError, StravaManager

STATIC_DIR = Path(__file__).resolve().parent / "static"


class OuraAppContext:
    """Shared state across HTTP request handlers."""

    def __init__(
        self,
        store: ConfigStore,
        oauth: OAuthManager,
        client: OuraClient,
        storage: OuraStorage,
        strava: StravaManager | None = None,
    ) -> None:
        self.store = store
        self.oauth = oauth
        self.client = client
        self.storage = storage
        self.strava = strava or StravaManager(store)
        self.last_oauth_event: dict[str, Any] | None = None
        ensure_plan_initialized(self.storage)
        self.orchestrator = MultiAgentCoachOrchestrator(store=self.store)


def create_http_server(
    host: str,
    port: int,
    ctx: OuraAppContext,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status: int, content: str) -> None:
            body = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                index_file = STATIC_DIR / "index.html"
                self._send_html(HTTPStatus.OK, index_file.read_text(encoding="utf-8"))
                return

            if path == "/api/status":
                cfg = ctx.store.load_config()
                tokens = ctx.store.load_tokens()
                strava_cfg = ctx.strava.load_config()
                strava_tok = ctx.strava.load_tokens()
                strava_runs = ctx.storage.get_strava_runs()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "configured": cfg.is_configured,
                        "client_id_preview": (
                            f"{cfg.client_id[:6]}...{cfg.client_id[-4:]}"
                            if len(cfg.client_id) > 10
                            else cfg.client_id
                        ),
                        "redirect_uri": cfg.redirect_uri,
                        "scopes": cfg.scopes,
                        "connected": tokens is not None,
                        "token_expires_at": tokens.expires_at if tokens else None,
                        "has_refresh_token": bool(tokens and tokens.refresh_token),
                        "personal_info": ctx.storage.get_personal_info(),
                        "last_sync": ctx.storage.get_meta("last_sync"),
                        "collection_counts": ctx.storage.get_collection_counts(),
                        "strava_configured": strava_cfg.is_configured,
                        "strava_client_id_preview": strava_cfg.client_id,
                        "strava_redirect_uri": strava_cfg.redirect_uri,
                        "strava_connected": strava_tok is not None,
                        "strava_athlete_name": strava_tok.athlete_name if strava_tok else "",
                        "strava_runs_count": len(strava_runs),
                    },
                )
                return

            if path == "/api/auth-url":
                try:
                    url, _ = ctx.oauth.build_authorize_url()
                    self._send_json(HTTPStatus.OK, {"authorize_url": url})
                except OuraOAuthError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if path == "/api/strava/auth-url":
                try:
                    url, _ = ctx.strava.build_authorize_url()
                    self._send_json(HTTPStatus.OK, {"authorize_url": url})
                except StravaError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if path == "/oauth/callback":
                error = (qs.get("error") or [""])[0]
                code = (qs.get("code") or [""])[0]
                state = (qs.get("state") or [""])[0]

                if error:
                    desc = (qs.get("error_description") or [error])[0]
                    ctx.last_oauth_event = {"ok": False, "error": desc}
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _oauth_result_page(False, f"Oura authorization declined: {desc}"),
                    )
                    return

                if not code:
                    ctx.last_oauth_event = {"ok": False, "error": "Missing authorization code."}
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _oauth_result_page(False, "Missing authorization code in callback URL."),
                    )
                    return

                if ctx.oauth._pending_state and not ctx.oauth.verify_state(state):
                    ctx.last_oauth_event = {"ok": False, "error": "Invalid CSRF state parameter."}
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _oauth_result_page(False, "State mismatch. Please retry connecting from the dashboard."),
                    )
                    return

                try:
                    ctx.oauth.exchange_code(code)
                    ctx.last_oauth_event = {"ok": True}
                    self._send_html(
                        HTTPStatus.OK,
                        _oauth_result_page(True, "Connected to your Oura Ring! Redirecting to dashboard..."),
                    )
                except OuraOAuthError as exc:
                    ctx.last_oauth_event = {"ok": False, "error": str(exc)}
                    self._send_html(HTTPStatus.BAD_REQUEST, _oauth_result_page(False, str(exc)))
                return

            if path == "/strava/callback":
                error = (qs.get("error") or [""])[0]
                code = (qs.get("code") or [""])[0]
                state = (qs.get("state") or [""])[0]

                if error:
                    ctx.last_oauth_event = {"ok": False, "error": error}
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _oauth_result_page(False, f"Strava authorization declined: {error}"),
                    )
                    return
                if not code:
                    ctx.last_oauth_event = {"ok": False, "error": "Missing Strava authorization code."}
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _oauth_result_page(False, "Missing Strava authorization code."),
                    )
                    return
                if ctx.strava._pending_state and not ctx.strava.verify_state(state):
                    ctx.last_oauth_event = {"ok": False, "error": "Invalid Strava CSRF state."}
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _oauth_result_page(False, "Strava state mismatch. Please retry connecting."),
                    )
                    return
                try:
                    ctx.strava.exchange_code(code)
                    ctx.last_oauth_event = {"ok": True, "provider": "strava"}
                    self._send_html(
                        HTTPStatus.OK,
                        _oauth_result_page(True, "Connected to Strava! Redirecting to Half-Marathon Coach..."),
                    )
                except StravaError as exc:
                    ctx.last_oauth_event = {"ok": False, "error": str(exc)}
                    self._send_html(HTTPStatus.BAD_REQUEST, _oauth_result_page(False, str(exc)))
                return

            if path == "/healthz":
                self._send_json(HTTPStatus.OK, {"status": "healthy", "service": "jackie-half-marathon-agent"})
                return

            if path == "/api/coach/approvals":
                status_filter = ((qs.get("status") or ["PENDING"])[0]).upper()
                self._send_json(
                    HTTPStatus.OK,
                    {"approvals": ctx.orchestrator.hitl.list_approvals(status=status_filter)},
                )
                return

            if path == "/api/telemetry/traces":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "spans": DEFAULT_TRACER.get_recent_spans(limit=30),
                        "intent_outcomes": DEFAULT_INTENT_TRACKER.get_recent_outcomes(limit=20),
                    },
                )
                return

            if path == "/api/coach/overview":
                run_adapt = (qs.get("adapt") or ["1"])[0] != "0"
                today_param = (qs.get("date") or [""])[0] or None
                adaptation = auto_adapt_plan(ctx.storage, target_day=today_param) if run_adapt else None
                overview = get_coach_overview(ctx.storage, today_str=today_param)
                overview["adaptation_event"] = adaptation
                overview["pending_approvals"] = ctx.orchestrator.hitl.list_approvals(status="PENDING", limit=10)
                self._send_json(HTTPStatus.OK, PIIRedactor.redact(overview))
                return

            if path == "/api/coach/week":
                week_num = max(1, min(17, int((qs.get("week") or ["1"])[0])))
                days = ctx.storage.get_plan_days(week_num=week_num)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "week_num": week_num,
                        "phase": days[0]["phase"] if days else "",
                        "planned_miles": round(sum(float(d["planned_miles"] or 0.0) for d in days), 1),
                        "completed_miles": round(sum(float(d["actual_miles"] or 0.0) for d in days), 2),
                        "completed_elevation_ft": round(sum(float(d["actual_elevation_ft"] or 0.0) for d in days), 0),
                        "days": days,
                    },
                )
                return

            if path == "/api/dashboard":
                days = max(1, min(3650, int((qs.get("days") or ["30"])[0])))
                end_dt = dt.date.today()
                start_dt = end_dt - dt.timedelta(days=days - 1)
                timeline = ctx.storage.get_daily_timeline(
                    start_date=start_dt.isoformat(),
                    end_date=end_dt.isoformat(),
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "days": days,
                        "start_date": start_dt.isoformat(),
                        "end_date": end_dt.isoformat(),
                        "timeline": timeline,
                        "summary": _compute_averages(timeline),
                        "personal_info": ctx.storage.get_personal_info(),
                        "last_sync": ctx.storage.get_meta("last_sync"),
                    },
                )
                return

            if path == "/api/export":
                fmt = ((qs.get("format") or ["csv"])[0]).lower()
                days = int((qs.get("days") or ["90"])[0])
                end_dt = dt.date.today()
                start_dt = end_dt - dt.timedelta(days=max(1, days) - 1)
                if fmt == "json":
                    timeline = ctx.storage.get_daily_timeline(
                        start_date=start_dt.isoformat(),
                        end_date=end_dt.isoformat(),
                    )
                    payload = json.dumps(timeline, indent=2).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="oura_daily_{start_dt}_{end_dt}.json"',
                    )
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    csv_text = ctx.storage.format_timeline_csv(
                        start_date=start_dt.isoformat(),
                        end_date=end_dt.isoformat(),
                    )
                    payload = csv_text.encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="oura_daily_{start_dt}_{end_dt}.csv"',
                    )
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown route: {path}"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON request body."})
                return

            if path == "/api/config":
                existing = ctx.store.load_config()
                client_id = str(body.get("client_id") or existing.client_id).strip()
                client_secret = str(body.get("client_secret") or existing.client_secret).strip()
                redirect_uri = str(body.get("redirect_uri") or existing.redirect_uri).strip()
                if not client_id or not client_secret:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "Both Client ID and Client Secret are required."},
                    )
                    return
                updated = AppConfig(
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=redirect_uri,
                    scopes=existing.scopes,
                )
                ctx.store.save_config(updated)
                self._send_json(HTTPStatus.OK, {"ok": True, "redirect_uri": updated.redirect_uri})
                return

            if path == "/api/strava/config":
                existing_s = ctx.strava.load_config()
                cid = str(body.get("client_id") or existing_s.client_id).strip()
                csec = str(body.get("client_secret") or existing_s.client_secret).strip()
                ruri = str(body.get("redirect_uri") or existing_s.redirect_uri).strip()
                if not cid or not csec:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "Both Strava Client ID and Client Secret are required."},
                    )
                    return
                upd_s = StravaConfig(
                    client_id=cid,
                    client_secret=csec,
                    redirect_uri=ruri,
                    scope=existing_s.scope,
                )
                ctx.strava.save_config(upd_s)
                self._send_json(HTTPStatus.OK, {"ok": True, "redirect_uri": upd_s.redirect_uri})
                return

            if path == "/api/disconnect":
                ctx.store.clear_tokens()
                self._send_json(HTTPStatus.OK, {"ok": True})
                return

            if path == "/api/strava/disconnect":
                ctx.strava.clear_tokens()
                self._send_json(HTTPStatus.OK, {"ok": True})
                return

            if path in ("/api/sync", "/api/coach/sync-all"):
                days = max(1, min(3650, int(body.get("days") or 30)))
                include_hr = bool(body.get("include_heartrate", True))
                end_dt = dt.date.today()
                start_dt = end_dt - dt.timedelta(days=days - 1)
                sync_report: dict[str, Any] = {}
                errors: list[str] = []

                if ctx.store.load_tokens() is not None:
                    try:
                        sync_report["oura"] = ctx.storage.sync_range(
                            ctx.client,
                            start_date=start_dt.isoformat(),
                            end_date=end_dt.isoformat(),
                            include_heartrate=include_hr,
                        )
                    except (OuraOAuthError, OuraAPIError) as exc:
                        errors.append(f"Oura: {exc}")

                if ctx.strava.load_tokens() is not None:
                    try:
                        runs = ctx.strava.fetch_runs(
                            start_date=start_dt.isoformat(),
                            end_date=end_dt.isoformat(),
                        )
                        sync_report["strava_runs_synced"] = ctx.storage.upsert_strava_runs(runs)
                    except StravaError as exc:
                        errors.append(f"Strava: {exc}")

                if not sync_report and errors:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "; ".join(errors)})
                    return

                adaptation = auto_adapt_plan(ctx.storage)
                oura_runs_total = len(ctx.storage.get_strava_runs(start_dt.isoformat(), end_dt.isoformat()))
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "sync": sync_report.get("oura") or {"counts": {}},
                        "oura_runs_synced": oura_runs_total,
                        "strava_runs_synced": sync_report.get("strava_runs_synced", 0),
                        "adaptation": adaptation,
                        "warnings": errors,
                    },
                )
                return

            if path == "/api/coach/checkin":
                day = str(body.get("day") or dt.date.today().isoformat())
                rpe = int(body["rpe"]) if body.get("rpe") not in (None, "") else None
                soreness = int(body["soreness"]) if body.get("soreness") not in (None, "") else None
                notes = str(body["notes"]) if body.get("notes") is not None else None
                miles = float(body["miles"]) if body.get("miles") not in (None, "") else None
                duration_min = float(body["duration_min"]) if body.get("duration_min") not in (None, "") else None
                elevation_ft = float(body["elevation_ft"]) if body.get("elevation_ft") not in (None, "") else None
                updated_day = ctx.storage.save_checkin(
                    day=day,
                    rpe=rpe,
                    soreness=soreness,
                    notes=notes,
                    miles=miles,
                    duration_min=duration_min,
                    elevation_ft=elevation_ft,
                )
                adaptation = auto_adapt_plan(ctx.storage, target_day=day)
                self._send_json(HTTPStatus.OK, {"ok": True, "day": updated_day, "adaptation": adaptation})
                return

            if path == "/api/coach/swap":
                day_a = str(body.get("day_a") or "")
                day_b = str(body.get("day_b") or "")
                reason = str(body.get("reason") or "Dashboard workout swap")
                try:
                    res = manual_swap_days(ctx.storage, day_a=day_a, day_b=day_b, reason=reason)
                    self._send_json(HTTPStatus.OK, {"ok": True, **res})
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if path == "/api/coach/ask":
                prompt = str(body.get("prompt") or "").strip()
                session_id = str(body.get("session_id") or "web-session")
                ref_date = str(body.get("date") or "") or None
                if not prompt:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Prompt cannot be empty."})
                    return
                result = ctx.orchestrator.handle_request(
                    user_prompt=prompt,
                    session_id=session_id,
                    reference_date=ref_date,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **result})
                return

            if path == "/api/coach/approvals/resolve":
                approval_id = int(body.get("approval_id") or 0)
                approved = bool(body.get("approved", True))
                note = str(body.get("note") or "")
                try:
                    res = ctx.orchestrator.hitl.resolve_approval(
                        approval_id=approval_id,
                        approved=approved,
                        note=note,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, **res})
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if path == "/api/coach/undo":
                adap_id = int(body.get("adaptation_id") or 0)
                try:
                    res = ctx.storage.undo_adaptation(adap_id)
                    self._send_json(HTTPStatus.OK, {"ok": True, **res})
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            log_event(DEFAULT_LOGGER, "http.404", f"Unknown POST route: {path}")
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown route: {path}"})

    return ThreadingHTTPServer((host, port), Handler)


def wait_for_oauth_callback(
    host: str,
    port: int,
    ctx: OuraAppContext,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Run a temporary local HTTP listener until the OAuth callback arrives."""
    done = threading.Event()
    ctx.last_oauth_event = None
    server = create_http_server(host, port, ctx)
    server.timeout = 0.5

    def loop() -> None:
        start = dt.datetime.now(dt.timezone.utc).timestamp()
        while not done.is_set():
            server.handle_request()
            if ctx.last_oauth_event is not None:
                done.set()
                break
            if dt.datetime.now(dt.timezone.utc).timestamp() - start >= timeout_seconds:
                done.set()
                break

    worker = threading.Thread(target=loop, daemon=True)
    worker.start()
    done.wait(timeout=timeout_seconds + 2.0)
    server.server_close()
    return ctx.last_oauth_event or {"ok": False, "error": "Timed out waiting for OAuth callback."}


def _compute_averages(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    def avg(key: str, digits: int = 1) -> float | None:
        vals = [float(r[key]) for r in timeline if r.get(key) is not None]
        return round(sum(vals) / len(vals), digits) if vals else None

    latest = timeline[-1] if timeline else None
    return {
        "days_with_data": len(timeline),
        "latest": latest,
        "avg_sleep_score": avg("sleep_score", 1),
        "avg_readiness_score": avg("readiness_score", 1),
        "avg_activity_score": avg("activity_score", 1),
        "avg_hrv": avg("average_hrv", 1),
        "avg_lowest_hr": avg("lowest_heart_rate", 1),
        "avg_sleep_hours": avg("total_sleep_hours", 2),
        "avg_steps": avg("steps", 0),
        "avg_spo2": avg("spo2_average", 1),
    }


def _oauth_result_page(success: bool, message: str) -> str:
    safe_msg = html.escape(message)
    badge_color = "#34d399" if success else "#f87171"
    title = "Connected!" if success else "Connection Error"
    redirect_meta = '<meta http-equiv="refresh" content="1.5;url=/" />' if success else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  {redirect_meta}
  <title>Oura Connect - {title}</title>
  <style>
    body {{
      margin: 0; font-family: system-ui, -apple-system, sans-serif;
      background: #0b0f19; color: #f1f5f9;
      display: flex; align-items: center; justify-content: center; min-height: 100vh;
    }}
    .card {{
      background: #131a2a; border: 1px solid #23304a; border-radius: 16px;
      padding: 32px 36px; max-width: 460px; text-align: center;
      box-shadow: 0 20px 40px rgba(0,0,0,0.45);
    }}
    h1 {{ margin: 0 0 12px; color: {badge_color}; font-size: 1.5rem; }}
    p {{ color: #94a3b8; line-height: 1.5; margin: 0 0 20px; }}
    a {{
      display: inline-block; padding: 10px 18px; border-radius: 8px;
      background: #38bdf8; color: #0b0f19; font-weight: 600; text-decoration: none;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{safe_msg}</p>
    <a href="/">Return to Dashboard</a>
  </div>
</body>
</html>"""
