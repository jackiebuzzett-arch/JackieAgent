"""Unit tests for Oura Connect, Strava GPS ingestion, 17-Week Atlanta Coach, Tetris swaps, Undo, and MCP server."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from oura_connect.client import OuraClient
from oura_connect.coach import (
    RACE_DATE,
    auto_adapt_plan,
    ensure_plan_initialized,
    evaluate_readiness,
    generate_atlanta_sub2_plan,
)
from oura_connect.config import AppConfig, ConfigStore, OAuthTokens
from oura_connect.mcp_server import CoachMCPDispatcher, handle_jsonrpc_message
from oura_connect.oauth import OAuthManager
from oura_connect.server import OuraAppContext, create_http_server
from oura_connect.storage import OuraStorage
from oura_connect.strava import StravaConfig, StravaManager, normalize_strava_activity


class FakeTransport:
    """Deterministic in-memory HTTP transport for testing OAuth, Oura API v2, and Strava API v3 calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.handlers: list[object] = []

    def enqueue(self, status: int, payload: object, headers: dict[str, str] | None = None) -> None:
        self.handlers.append((status, headers or {}, json.dumps(payload).encode("utf-8")))

    def __call__(self, req: urllib.request.Request, timeout: float = 30.0) -> tuple[int, dict[str, str], bytes]:
        body_bytes = req.data or b""
        self.calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "headers": {k.lower(): v for k, v in req.header_items()},
                "body": body_bytes.decode("utf-8", errors="replace"),
            }
        )
        if not self.handlers:
            return 200, {}, json.dumps({"data": [], "next_token": None}).encode("utf-8")
        nxt = self.handlers.pop(0)
        if callable(nxt):
            return nxt(req)
        return nxt  # type: ignore[return-value]


class OuraConnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.store = ConfigStore(config_dir=base / "cfg", data_dir=base / "data")
        self.store.save_config(
            AppConfig(
                client_id="test-client-id",
                client_secret="test-client-secret",
                redirect_uri="http://localhost:8080/oauth/callback",
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_atomic_0600_permissions_and_single_use_refresh_rotation(self) -> None:
        mode = stat.S_IMODE(os.stat(self.store.config_path).st_mode)
        self.assertEqual(mode, 0o600)

        self.store.save_tokens(
            OAuthTokens(
                access_token="expired-access",
                refresh_token="refresh-v1",
                expires_at=time.time() - 30,
            )
        )
        token_mode = stat.S_IMODE(os.stat(self.store.tokens_path).st_mode)
        self.assertEqual(token_mode, 0o600)

        transport = FakeTransport()
        transport.enqueue(
            200,
            {
                "access_token": "fresh-access-v2",
                "refresh_token": "rotated-refresh-v2",
                "expires_in": 86400,
                "token_type": "Bearer",
            },
        )
        oauth = OAuthManager(self.store, transport=transport)
        access = oauth.get_valid_access_token()

        self.assertEqual(access, "fresh-access-v2")
        saved = self.store.load_tokens()
        assert saved is not None
        self.assertEqual(saved.refresh_token, "rotated-refresh-v2")

    def test_pagination_401_retry_and_heartrate_chunking(self) -> None:
        self.store.save_tokens(
            OAuthTokens(
                access_token="initial-token",
                refresh_token="refresh-initial",
                expires_at=time.time() + 3600,
                updated_at=time.time() - 120,
            )
        )
        transport = FakeTransport()
        transport.enqueue(401, {"detail": "Token revoked"})
        transport.enqueue(
            200,
            {
                "access_token": "recovered-token",
                "refresh_token": "refresh-next",
                "expires_in": 86400,
            },
        )
        transport.enqueue(
            200,
            {"data": [{"id": "s1", "day": "2026-11-01", "score": 84}], "next_token": "cursor-page-2"},
        )
        transport.enqueue(
            200,
            {"data": [{"id": "s2", "day": "2026-11-02", "score": 91}], "next_token": None},
        )

        oauth = OAuthManager(self.store, transport=transport)
        client = OuraClient(oauth, transport=transport, sleep_fn=lambda _: None)
        rows = client.fetch_collection("daily_sleep", "2026-11-01", "2026-11-02")
        self.assertEqual([r["score"] for r in rows], [84, 91])

    def test_17_week_atlanta_plan_generation_and_cutback_weeks(self) -> None:
        plan = generate_atlanta_sub2_plan()
        self.assertEqual(len(plan), 17 * 7)
        race_days = [d for d in plan if d["workout_type"] == "race"]
        self.assertEqual(len(race_days), 1)
        self.assertEqual(race_days[0]["day"], RACE_DATE)
        self.assertEqual(race_days[0]["planned_miles"], 13.1)

        # Verify Week 4 cutback has lower total planned mileage than Week 3
        w3 = sum(d["planned_miles"] for d in plan if d["week_num"] == 3)
        w4 = sum(d["planned_miles"] for d in plan if d["week_num"] == 4)
        self.assertLess(w4, w3)

    def test_readiness_short_sleep_high_hrv_stays_green_while_hrv_drop_tetris_swaps_and_undoes(self) -> None:
        storage = OuraStorage(self.store.db_path)
        ensure_plan_initialized(storage)

        # Seed 14 days of solid baseline (HRV ~78 ms, RHR ~55 bpm) up to 2026-12-09
        base_sleep = []
        for idx in range(1, 15):
            day_str = f"2026-11-{idx + 14:02d}"
            base_sleep.append(
                {
                    "id": f"sl-{idx}",
                    "day": day_str,
                    "type": "long_sleep",
                    "total_sleep_duration": 28800,
                    "average_hrv": 76 + (idx % 5),
                    "lowest_heart_rate": 55,
                }
            )
        storage.upsert_records("sleep", base_sleep)

        # Case 1: Short sleep (6.1h) WITH strong HRV (84 ms) and low RHR (52 bpm) -> stays GREEN!
        storage.upsert_records(
            "sleep",
            [
                {
                    "id": "sl-short-ok",
                    "day": "2026-12-08",
                    "type": "long_sleep",
                    "total_sleep_duration": 22000,
                    "average_hrv": 84,
                    "lowest_heart_rate": 52,
                }
            ],
        )
        r_green = evaluate_readiness(storage, target_day="2026-12-08")
        self.assertEqual(r_green["signal"], "GREEN")

        # Case 2: Thursday 2026-12-10 (Week 5 Quality Hill Repeats) wakes up with moderately dipped HRV (72 ms, z=-1.2) -> YELLOW!
        storage.upsert_records(
            "sleep",
            [
                {
                    "id": "sl-yellow",
                    "day": "2026-12-10",
                    "type": "long_sleep",
                    "total_sleep_duration": 27000,
                    "average_hrv": 72,
                    "lowest_heart_rate": 57,
                }
            ],
        )
        thu_before = storage.get_plan_day("2026-12-10")
        sat_before = storage.get_plan_day("2026-12-12")
        assert thu_before is not None and sat_before is not None
        self.assertTrue(thu_before["is_quality"])
        self.assertEqual(sat_before["workout_type"], "long_run")

        adap = auto_adapt_plan(storage, target_day="2026-12-10")
        self.assertTrue(adap["adapted"])
        self.assertEqual(adap["readiness"]["signal"], "YELLOW")

        # Verify Tetris double-swap: Thu -> Rest, Fri -> Hill Repeats, Sat -> Recovery, Sun -> Long Run (48h buffer!)
        thu_after = storage.get_plan_day("2026-12-10")
        fri_after = storage.get_plan_day("2026-12-11")
        sun_after = storage.get_plan_day("2026-12-13")
        assert thu_after and fri_after and sun_after
        self.assertEqual(thu_after["planned_miles"], 0.0)
        self.assertEqual(fri_after["workout_type"], "hills")
        self.assertEqual(sun_after["workout_type"], "long_run")

        # Verify 1-click Undo restores Thursday Hill Repeats & Saturday Long Run
        storage.undo_adaptation(adap["adaptation_id"])
        thu_restored = storage.get_plan_day("2026-12-10")
        sat_restored = storage.get_plan_day("2026-12-12")
        assert thu_restored and sat_restored
        self.assertEqual(thu_restored["workout_type"], "hills")
        self.assertEqual(sat_restored["workout_type"], "long_run")

        # Case 3: Deep HRV suppression (42 ms) + elevated RHR (59 bpm) -> RED circuit-breaker!
        storage.upsert_records(
            "sleep",
            [
                {
                    "id": "sl-red",
                    "day": "2026-12-15",
                    "type": "long_sleep",
                    "total_sleep_duration": 28000,
                    "average_hrv": 42,
                    "lowest_heart_rate": 59,
                }
            ],
        )
        red_adap = auto_adapt_plan(storage, target_day="2026-12-15")
        self.assertTrue(red_adap["adapted"])
        self.assertEqual(red_adap["readiness"]["signal"], "RED")

    def test_strava_run_normalization_and_plan_matching(self) -> None:
        storage = OuraStorage(self.store.db_path)
        ensure_plan_initialized(storage)

        norm = normalize_strava_activity(
            {
                "id": 998877,
                "name": "Piedmont Park Hills",
                "sport_type": "Run",
                "start_date_local": "2026-11-10T07:15:00Z",
                "distance": 3218.688,  # 2.00 miles
                "moving_time": 1240,   # 10:20/mi
                "total_elevation_gain": 45.0,  # ~147.6 ft
                "average_heartrate": 146.0,
            }
        )
        self.assertIsNotNone(norm)
        assert norm is not None
        self.assertEqual(norm["distance_miles"], 2.0)
        self.assertEqual(norm["pace_str"], "10:20/mi")
        storage.upsert_strava_runs([norm])

        tue = storage.get_plan_day("2026-11-10")
        assert tue is not None
        self.assertEqual(tue["status"], "completed")
        self.assertEqual(tue["actual_miles"], 2.0)
        self.assertEqual(tue["actual_pace"], "10:20/mi")

        # Verify native Oura `/v2/usercollection/workout` + `/v2/usercollection/heartrate` auto-matching!
        storage.upsert_records(
            "heartrate",
            [
                {"timestamp": "2026-11-12T07:10:00-05:00", "bpm": 148, "source": "workout"},
                {"timestamp": "2026-11-12T07:20:00-05:00", "bpm": 156, "source": "workout"},
            ],
        )
        storage.upsert_records(
            "workout",
            [
                {
                    "id": "oura-w1",
                    "activity": "running",
                    "day": "2026-11-12",
                    "distance": 4023.36,  # 2.50 miles
                    "start_datetime": "2026-11-12T07:00:00-05:00",
                    "end_datetime": "2026-11-12T07:26:15-05:00",  # 26m15s = 10:30/mi
                    "intensity": "moderate",
                    "calories": 265,
                }
            ],
        )
        thu = storage.get_plan_day("2026-11-12")
        assert thu is not None
        self.assertEqual(thu["status"], "completed")
        self.assertEqual(thu["actual_miles"], 2.5)
        self.assertEqual(thu["actual_pace"], "10:30/mi")
        self.assertEqual(thu["actual_avg_hr"], 152.0)

        # Verify manual treadmill / phone-free miles override via save_checkin
        storage.save_checkin(day="2026-11-14", miles=3.5, duration_min=36.75, rpe=5)
        sat = storage.get_plan_day("2026-11-14")
        assert sat is not None
        self.assertEqual(sat["status"], "completed")
        self.assertEqual(sat["actual_miles"], 3.5)
        self.assertEqual(sat["actual_pace"], "10:30/mi")

    def test_mcp_server_jsonrpc_handshake_and_tools(self) -> None:
        dispatcher = CoachMCPDispatcher(store=self.store)
        init_resp = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, dispatcher)
        assert init_resp is not None
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "oura-coach")

        list_resp = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, dispatcher)
        assert list_resp is not None
        tool_names = {t["name"] for t in list_resp["result"]["tools"]}
        self.assertIn("coach_morning_briefing", tool_names)
        self.assertIn("coach_undo_adaptation", tool_names)

        call_resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "coach_morning_briefing", "arguments": {"date": "2026-11-10"}},
            },
            dispatcher,
        )
        assert call_resp is not None
        self.assertFalse(call_resp["result"]["isError"])
        payload = json.loads(call_resp["result"]["content"][0]["text"])
        self.assertEqual(payload["race"]["date"], RACE_DATE)

    def test_http_server_strava_and_coach_routes(self) -> None:
        transport = FakeTransport()
        transport.enqueue(
            200,
            {
                "access_token": "strava-access",
                "refresh_token": "strava-refresh",
                "expires_at": int(time.time()) + 21600,
                "athlete": {"id": 42, "firstname": "Jackie", "lastname": "B"},
            },
        )
        oauth = OAuthManager(self.store, transport=transport)
        client = OuraClient(oauth, transport=transport)
        storage = OuraStorage(self.store.db_path)
        strava = StravaManager(self.store, transport=transport)
        strava.save_config(StravaConfig(client_id="999", client_secret="sec"))
        ctx = OuraAppContext(self.store, oauth, client, storage, strava=strava)

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        server = create_http_server("127.0.0.1", 0, ctx)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/strava/auth-url") as resp:
                s_auth = json.loads(resp.read().decode("utf-8"))
            state = urllib.parse.parse_qs(urllib.parse.urlparse(s_auth["authorize_url"]).query)["state"][0]

            with opener.open(f"http://127.0.0.1:{port}/strava/callback?code=sc-1&state={state}") as cb:
                self.assertEqual(cb.status, 200)

            with opener.open(f"http://127.0.0.1:{port}/api/coach/overview?date=2026-11-10") as ov_resp:
                ov = json.loads(ov_resp.read().decode("utf-8"))
            self.assertEqual(ov["race"]["name"], "Publix Atlanta Half Marathon")
            self.assertEqual(ov["active_week_num"], 1)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
