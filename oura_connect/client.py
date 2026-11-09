"""Zero-dependency Oura Ring API v2 REST client with cursor pagination and retry support."""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
import urllib.request
from typing import Any

from .oauth import HttpTransport, OAuthManager, default_http_transport

API_BASE_URL = "https://api.ouraring.com"

DAILY_COLLECTIONS: tuple[str, ...] = (
    "daily_sleep",
    "sleep",
    "daily_readiness",
    "daily_activity",
    "daily_stress",
    "daily_resilience",
    "daily_spo2",
    "daily_cardiovascular_age",
    "vO2_max",
    "workout",
    "session",
)


class OuraAPIError(RuntimeError):
    """Raised when an Oura API v2 request fails."""

    def __init__(self, status: int, message: str, endpoint: str = "") -> None:
        super().__init__(f"Oura API error (HTTP {status}) on {endpoint}: {message}")
        self.status = status
        self.endpoint = endpoint
        self.detail = message


class OuraClient:
    """Client for Oura API v2 `/v2/usercollection/*` endpoints."""

    def __init__(
        self,
        oauth: OAuthManager,
        transport: HttpTransport = default_http_transport,
        sleep_fn: Any = time.sleep,
    ) -> None:
        self.oauth = oauth
        self.transport = transport
        self._sleep = sleep_fn

    def get_personal_info(self) -> dict[str, Any]:
        return self._request_json("/v2/usercollection/personal_info")

    def fetch_collection(
        self,
        collection: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """Fetch all pages of a date-bounded `/v2/usercollection/<collection>` endpoint."""
        endpoint = f"/v2/usercollection/{collection}"
        items: list[dict[str, Any]] = []
        next_token: str | None = None

        while True:
            params: dict[str, str] = {
                "start_date": start_date,
                "end_date": end_date,
            }
            if next_token:
                params["next_token"] = next_token

            payload = self._request_json(endpoint, params=params)
            batch = payload.get("data") or []
            if isinstance(batch, list):
                items.extend(batch)
            next_token = payload.get("next_token")
            if not next_token:
                break

        return items

    def fetch_heartrate(
        self,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """Fetch 5-minute heart-rate time series in <=28-day windows (Oura API enforces 30-day max)."""
        start_dt = dt.date.fromisoformat(start_date)
        end_dt = dt.date.fromisoformat(end_date)
        all_points: list[dict[str, Any]] = []

        cursor = start_dt
        while cursor <= end_dt:
            chunk_end = min(cursor + dt.timedelta(days=27), end_dt)
            start_iso = f"{cursor.isoformat()}T00:00:00Z"
            end_iso = f"{chunk_end.isoformat()}T23:59:59Z"

            next_token: str | None = None
            while True:
                params: dict[str, str] = {
                    "start_datetime": start_iso,
                    "end_datetime": end_iso,
                }
                if next_token:
                    params["next_token"] = next_token
                payload = self._request_json("/v2/usercollection/heartrate", params=params)
                batch = payload.get("data") or []
                if isinstance(batch, list):
                    all_points.extend(batch)
                next_token = payload.get("next_token")
                if not next_token:
                    break

            cursor = chunk_end + dt.timedelta(days=1)

        return all_points

    def _request_json(
        self,
        path: str,
        params: dict[str, str] | None = None,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        url = f"{API_BASE_URL}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        refreshed_after_401 = False
        attempt = 0

        while True:
            token = self.oauth.get_valid_access_token()
            req = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "oura-connect/0.1.0",
                },
            )
            status, headers, body = self.transport(req, 30.0)

            if status == 401 and not refreshed_after_401:
                refreshed_after_401 = True
                cur_tokens = self.oauth.store.load_tokens()
                if cur_tokens is None or cur_tokens.is_expired(skew_seconds=3600) or (time.time() - cur_tokens.updated_at > 30):
                    self.oauth.refresh_tokens(force=True)
                    continue

            if status == 429 and attempt < max_retries:
                retry_after = float(headers.get("retry-after") or (2 ** attempt))
                self._sleep(min(retry_after, 10.0))
                attempt += 1
                continue

            text = body.decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text) if text else {}
            except json.JSONDecodeError:
                parsed = {"raw": text}

            if 200 <= status < 300:
                return parsed if isinstance(parsed, dict) else {"data": parsed}

            detail = (
                parsed.get("detail")
                if isinstance(parsed, dict)
                else text
            ) or text or f"HTTP {status}"
            raise OuraAPIError(status=status, message=str(detail), endpoint=path)
