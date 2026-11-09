"""Command-line interface for oura-connect and the Adaptive Half-Marathon Coach."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import sys
import urllib.parse
import webbrowser
from pathlib import Path

from .client import OuraAPIError, OuraClient
from .coach import auto_adapt_plan, get_coach_overview
from .config import AppConfig, ConfigStore, DEFAULT_REDIRECT_URI
from .oauth import OAuthManager, OuraOAuthError
from .server import OuraAppContext, create_http_server, wait_for_oauth_callback
from .storage import OuraStorage
from .strava import DEFAULT_STRAVA_REDIRECT_URI, StravaConfig, StravaError, StravaManager


def build_context(store: ConfigStore | None = None) -> OuraAppContext:
    cfg_store = store or ConfigStore()
    oauth = OAuthManager(cfg_store)
    client = OuraClient(oauth)
    storage = OuraStorage(cfg_store.db_path)
    strava = StravaManager(cfg_store)
    return OuraAppContext(store=cfg_store, oauth=oauth, client=client, storage=storage, strava=strava)


def cmd_setup(args: argparse.Namespace) -> int:
    ctx = build_context()
    existing = ctx.store.load_config()

    print("=== Oura Ring API v2 Setup ===")
    print("1. Open https://cloud.ouraring.com -> API Applications -> Create New Application")
    print(f"2. Set Redirect URI to: {args.redirect_uri or existing.redirect_uri or DEFAULT_REDIRECT_URI}")
    print("3. Copy your Client ID and Client Secret below.\n")

    client_id = (args.client_id or "").strip()
    if not client_id:
        prompt_id = f"Client ID [{existing.client_id[:8]}...]: " if existing.client_id else "Client ID: "
        client_id = input(prompt_id).strip() or existing.client_id

    client_secret = (args.client_secret or "").strip()
    if not client_secret:
        prompt_secret = (
            "Client Secret (hidden, leave blank to keep existing): "
            if existing.client_secret
            else "Client Secret (hidden): "
        )
        entered = getpass.getpass(prompt_secret).strip()
        client_secret = entered or existing.client_secret

    redirect_uri = (args.redirect_uri or existing.redirect_uri or DEFAULT_REDIRECT_URI).strip()

    if not client_id or not client_secret:
        print("Error: Both Client ID and Client Secret are required.", file=sys.stderr)
        return 1

    cfg = AppConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=existing.scopes,
    )
    ctx.store.save_config(cfg)
    print(f"Saved Oura credentials to {ctx.store.config_path} (permissions 0600).")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    ctx = build_context()
    cfg = ctx.store.load_config()
    if not cfg.is_configured:
        print("Credentials not configured yet. Starting guided setup first...\n")
        rc = cmd_setup(args)
        if rc != 0:
            return rc
        cfg = ctx.store.load_config()

    auth_url, _ = ctx.oauth.build_authorize_url(config=cfg)
    parsed_redirect = urllib.parse.urlparse(cfg.redirect_uri)
    host = parsed_redirect.hostname or "127.0.0.1"
    port = parsed_redirect.port or 8080

    print("\nAuthorize access to your Oura Ring in your browser:")
    print(f"\n  {auth_url}\n")
    if not args.no_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    print(f"Listening for OAuth callback on http://{host}:{port}/oauth/callback (timeout: {args.timeout}s)...")
    event = wait_for_oauth_callback(host=host, port=port, ctx=ctx, timeout_seconds=float(args.timeout))
    if not event.get("ok"):
        print(f"Authentication failed: {event.get('error')}", file=sys.stderr)
        return 1

    print(f"Authenticated! Tokens stored securely at {ctx.store.tokens_path}")
    return 0


def cmd_strava_auth(args: argparse.Namespace) -> int:
    ctx = build_context()
    existing = ctx.strava.load_config()

    client_id = (args.client_id or "").strip()
    client_secret = (args.client_secret or "").strip()

    if not existing.is_configured or client_id or client_secret:
        print("=== Strava API v3 OAuth Setup ===")
        print("1. Open https://www.strava.com/settings/api")
        print("2. Set Authorization Callback Domain to: localhost")
        print("3. Enter your Strava Client ID and Client Secret below.\n")
        if not client_id:
            client_id = input(f"Strava Client ID [{existing.client_id}]: ").strip() or existing.client_id
        if not client_secret:
            entered = getpass.getpass("Strava Client Secret (hidden): ").strip()
            client_secret = entered or existing.client_secret
        if not client_id or not client_secret:
            print("Error: Strava Client ID and Client Secret are required.", file=sys.stderr)
            return 1
        ctx.strava.save_config(
            StravaConfig(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=args.redirect_uri or existing.redirect_uri or DEFAULT_STRAVA_REDIRECT_URI,
            )
        )

    cfg = ctx.strava.load_config()
    auth_url, _ = ctx.strava.build_authorize_url(config=cfg)
    parsed = urllib.parse.urlparse(cfg.redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8080

    print(f"\nAuthorize Strava in your browser:\n\n  {auth_url}\n")
    if not args.no_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    print(f"Listening for Strava callback on http://{host}:{port}/strava/callback (timeout: {args.timeout}s)...")
    event = wait_for_oauth_callback(host=host, port=port, ctx=ctx, timeout_seconds=float(args.timeout))
    if not event.get("ok"):
        print(f"Strava authentication failed: {event.get('error')}", file=sys.stderr)
        return 1
    print(f"Connected to Strava! Tokens saved to {ctx.strava.tokens_path}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    ctx = build_context()
    end_date = args.end_date or dt.date.today().isoformat()
    if args.start_date:
        start_date = args.start_date
    else:
        start_dt = dt.date.fromisoformat(end_date) - dt.timedelta(days=max(1, args.days) - 1)
        start_date = start_dt.isoformat()

    if ctx.store.load_tokens() is not None:
        print(f"Syncing Oura Ring data from {start_date} to {end_date}...")
        try:
            result = ctx.storage.sync_range(
                ctx.client,
                start_date=start_date,
                end_date=end_date,
                include_heartrate=not args.skip_heartrate,
            )
            print("\nSynced Oura records:")
            for col, count in result["counts"].items():
                print(f"  - {col:26s}: {count}")
            if result["warnings"]:
                print("\nNotes:")
                for warning in result["warnings"]:
                    print(f"  ! {warning}")
        except (OuraOAuthError, OuraAPIError) as exc:
            print(f"Oura sync failed: {exc}", file=sys.stderr)
            return 1

    if ctx.strava.load_tokens() is not None:
        print(f"\nSyncing Strava GPS runs from {start_date} to {end_date}...")
        try:
            runs = ctx.strava.fetch_runs(start_date=start_date, end_date=end_date)
            count = ctx.storage.upsert_strava_runs(runs)
            print(f"  - strava_runs               : {count}")
        except StravaError as exc:
            print(f"Strava sync warning: {exc}", file=sys.stderr)

    adap = auto_adapt_plan(ctx.storage)
    if adap.get("adapted"):
        print(f"\n[Coach Auto-Adaptation] {adap.get('action_summary')}")
    return 0


def cmd_coach(args: argparse.Namespace) -> int:
    ctx = build_context()
    today_str = args.date or dt.date.today().isoformat()

    if args.sync:
        end_dt = dt.date.fromisoformat(today_str)
        start_dt = end_dt - dt.timedelta(days=13)
        if ctx.store.load_tokens() is not None:
            ctx.storage.sync_range(ctx.client, start_dt.isoformat(), end_dt.isoformat(), include_heartrate=False)
        if ctx.strava.load_tokens() is not None:
            runs = ctx.strava.fetch_runs(start_dt.isoformat(), end_dt.isoformat())
            ctx.storage.upsert_strava_runs(runs)

    adap = auto_adapt_plan(ctx.storage, target_day=today_str)
    overview = get_coach_overview(ctx.storage, today_str=today_str)
    race = overview["race"]
    readiness = overview["readiness"]
    m = readiness.get("metrics") or {}
    today_w = overview["today_workout"]

    print(f"=== {race['name']} ({race['date']}) — {race['goal']} ===")
    print(f"Countdown: {race['days_remaining']} days ({race['weeks_remaining']} weeks) | Date: {today_str}")
    print(f"\nReadiness Signal: [{readiness['signal']}] (Score {readiness['score']}/100) — {readiness['headline']}")
    if m:
        print(
            f"  HRV: {m.get('hrv_ms') or '-'} ms (28d avg {m.get('hrv_28d_mean') or '-'} ms, z={m.get('hrv_z') or 0:+.2f}) | "
            f"Min HR: {m.get('rhr_bpm') or '-'} bpm (avg {m.get('rhr_28d_mean') or '-'}) | "
            f"Sleep: {m.get('sleep_hours') or '-'}h | Temp: {m.get('temperature_deviation') or 0:+.2f}°C"
        )
    for reason in readiness.get("reasons", []):
        print(f"  * {reason}")

    if adap.get("adapted"):
        print(f"\n>> AUTO-ADAPTED (#{adap['adaptation_id']}): {adap['action_summary']}")

    if today_w:
        print(f"\nToday's Prescription ({today_w['day']} — Week {today_w['week_num']}, {today_w['phase']}):")
        print(f"  Workout : {today_w['title']} | {today_w['planned_miles']:g} mi @ {today_w['target_pace']}")
        if today_w.get("hill_focus"):
            print(f"  Hills   : {today_w['hill_focus']}")
        if today_w.get("strength_addon"):
            print(f"  Prehab  : {today_w['strength_addon']}")
        print(f"  Details : {today_w['description']}")

    week_num = int(args.week or overview["active_week_num"])
    week_days = ctx.storage.get_plan_days(week_num=week_num)
    planned_tot = sum(float(d["planned_miles"] or 0.0) for d in week_days)
    actual_tot = sum(float(d["actual_miles"] or 0.0) for d in week_days)

    print(f"\n--- Week {week_num} Schedule (Planned: {planned_tot:.1f} mi | Completed (Oura): {actual_tot:.2f} mi) ---")
    for d in week_days:
        dow = dt.date.fromisoformat(d["day"]).strftime("%a")
        hr_part = f", {d['actual_avg_hr']:.0f}bpm" if d.get("actual_avg_hr") else ""
        done_str = (
            f" [DONE: {d['actual_miles']:.2f}mi @ {d['actual_pace']}{hr_part}]"
            if d.get("actual_miles")
            else ""
        )
        marker = ">>" if d["day"] == today_str else "  "
        print(
            f"{marker} {dow} {d['day']} | {d['planned_miles']:4.1f} mi | {d['title']:<44s} | {d['target_pace']}{done_str}"
        )
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    ctx = build_context()
    end_dt = dt.date.today()
    start_dt = end_dt - dt.timedelta(days=max(1, args.days) - 1)
    timeline = ctx.storage.get_daily_timeline(
        start_date=start_dt.isoformat(),
        end_date=end_dt.isoformat(),
    )

    if not timeline:
        print("No synced Oura records found for this date range. Run `python3 -m oura_connect sync` first.")
        return 0

    header = (
        f"{'Date':10s}  {'Sleep':>5s}  {'Ready':>5s}  {'Activ':>5s}  "
        f"{'Sleep(h)':>8s}  {'HRV':>5s}  {'MinHR':>5s}  {'Steps':>7s}  {'Stress':>10s}"
    )
    print(header)
    print("-" * len(header))
    for row in timeline:
        def fmt(val: object, width: int, suffix: str = "") -> str:
            if val is None:
                return "-".rjust(width)
            return f"{val}{suffix}".rjust(width)

        print(
            f"{row['day']:10s}  "
            f"{fmt(row['sleep_score'], 5)}  "
            f"{fmt(row['readiness_score'], 5)}  "
            f"{fmt(row['activity_score'], 5)}  "
            f"{fmt(row['total_sleep_hours'], 8)}  "
            f"{fmt(row['average_hrv'], 5)}  "
            f"{fmt(row['lowest_heart_rate'], 5)}  "
            f"{fmt(row['steps'], 7)}  "
            f"{fmt(row['stress_summary'], 10)}"
        )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    ctx = build_context()
    end_dt = dt.date.today()
    start_dt = end_dt - dt.timedelta(days=max(1, args.days) - 1) if args.days else None
    out_dir = Path(args.output).expanduser().resolve()
    files = ctx.storage.export_data(
        output_dir=out_dir,
        fmt=args.format,
        start_date=start_dt.isoformat() if start_dt else None,
        end_date=end_dt.isoformat() if start_dt else None,
    )
    print(f"Exported {len(files)} file(s) to {out_dir}:")
    for path in files:
        print(f"  - {path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    ctx = build_context()
    server = create_http_server(args.host, args.port, ctx)
    url = f"http://{args.host}:{args.port}"
    print(f"Oura Connect + Half-Marathon Coach running at {url}")
    print(f"  Oura Callback  : http://localhost:{args.port}/oauth/callback")
    print(f"  Strava Callback: http://localhost:{args.port}/strava/callback")
    print("Press Ctrl+C to stop.")
    if args.open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oura_connect",
        description="Oura Ring v2 + Strava v3 Adaptive Sub-2:00 Half-Marathon Coach.",
    )
    sub = parser.add_subparsers(dest="command")

    p_setup = sub.add_parser("setup", help="Save Oura OAuth2 Client ID & Client Secret")
    p_setup.add_argument("--client-id", default="", help="Oura OAuth2 Client ID")
    p_setup.add_argument("--client-secret", default="", help="Oura OAuth2 Client Secret")
    p_setup.add_argument("--redirect-uri", default="", help=f"Redirect URI (default: {DEFAULT_REDIRECT_URI})")
    p_setup.set_defaults(func=cmd_setup)

    p_auth = sub.add_parser("auth", help="Authenticate with Oura Ring via browser OAuth 2.0")
    p_auth.add_argument("--client-id", default="")
    p_auth.add_argument("--client-secret", default="")
    p_auth.add_argument("--redirect-uri", default="")
    p_auth.add_argument("--no-browser", action="store_true")
    p_auth.add_argument("--timeout", type=int, default=180)
    p_auth.set_defaults(func=cmd_auth)

    p_sauth = sub.add_parser("strava-auth", help="Configure & authenticate Strava OAuth 2.0 for GPS runs")
    p_sauth.add_argument("--client-id", default="")
    p_sauth.add_argument("--client-secret", default="")
    p_sauth.add_argument("--redirect-uri", default="")
    p_sauth.add_argument("--no-browser", action="store_true")
    p_sauth.add_argument("--timeout", type=int, default=180)
    p_sauth.set_defaults(func=cmd_strava_auth)

    p_sync = sub.add_parser("sync", help="Fetch latest Oura + Strava data and run Coach auto-adaptation")
    p_sync.add_argument("--days", type=int, default=30)
    p_sync.add_argument("--start-date", default="")
    p_sync.add_argument("--end-date", default="")
    p_sync.add_argument("--skip-heartrate", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    p_coach = sub.add_parser("coach", help="View today's Half-Marathon Readiness-to-Train briefing & weekly plan")
    p_coach.add_argument("--date", default="", help="Optional YYYY-MM-DD date to evaluate")
    p_coach.add_argument("--week", type=int, default=0, help="Specific week 1..17 to display")
    p_coach.add_argument("--sync", action="store_true", help="Sync live Oura + Strava before briefing")
    p_coach.set_defaults(func=cmd_coach)

    p_sum = sub.add_parser("summary", help="Print daily Oura scores and sleep/HRV table in terminal")
    p_sum.add_argument("--days", type=int, default=14)
    p_sum.set_defaults(func=cmd_summary)

    p_exp = sub.add_parser("export", help="Export cached Oura data to CSV or JSON")
    p_exp.add_argument("--format", choices=("csv", "json"), default="csv")
    p_exp.add_argument("--days", type=int, default=90)
    p_exp.add_argument("--output", default="./exports")
    p_exp.set_defaults(func=cmd_export)

    p_srv = sub.add_parser("serve", help="Start the local web dashboard & OAuth callback server")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8080)
    p_srv.add_argument("--open-browser", action="store_true")
    p_srv.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args))
