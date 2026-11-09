"""Zero-dependency Strava API v3 OAuth 2.0 manager and GPS run ingestion client."""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import ConfigStore, _atomic_write_json
from .oauth import HttpTransport, default_http_transport

STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"
DEFAULT_STRAVA_REDIRECT_URI = "http://localhost:8080/strava/callback"
DEFAULT_STRAVA_SCOPE = "read,activity:read_all"

RUN_SPORT_TYPES = {"Run", "TrailRun", "VirtualRun"}


class StravaError(RuntimeError):
    """Raised when Strava OAuth or API requests fail."""


@dataclass
class StravaConfig:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = DEFAULT_STRAVA_REDIRECT_URI
    scope: str = DEFAULT_STRAVA_SCOPE

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())


@dataclass
class StravaTokens:
    access_token: str
    refresh_token: str
    expires_at: float = 0.0
    athlete_id: str = ""
    athlete_name: str = ""
    updated_at: float = field(default_factory=time.time)

    def is_expired(self, skew_seconds: int = 120) -> bool:
        if not self.expires_at:
            return False
        return time.time() + skew_seconds >= self.expires_at

    @classmethod
    def from_response(
        cls,
        data: dict[str, Any],
        fallback_athlete_id: str = "",
        fallback_athlete_name: str = "",
    ) -> "StravaTokens":
        now = time.time()
        expires_at = float(data.get("expires_at") or (now + float(data.get("expires_in") or 21600)))
        athlete = data.get("athlete") if isinstance(data.get("athlete"), dict) else {}
        athlete_id = str(athlete.get("id") or fallback_athlete_id or "")
        first = str(athlete.get("firstname") or "").strip()
        last = str(athlete.get("lastname") or "").strip()
        full_name = f"{first} {last}".strip() or fallback_athlete_name
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or ""),
            expires_at=expires_at,
            athlete_id=athlete_id,
            athlete_name=full_name,
            updated_at=now,
        )


def format_pace_per_mile(pace_seconds: float | int | None) -> str:
    if not pace_seconds or pace_seconds <= 0:
        return "-"
    total = int(round(float(pace_seconds)))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}/mi"


def normalize_strava_activity(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a raw Strava `/athlete/activities` JSON item into normalized imperial run metrics."""
    sport_type = str(raw.get("sport_type") or raw.get("type") or "")
    if sport_type not in RUN_SPORT_TYPES:
        return None

    distance_m = float(raw.get("distance") or 0.0)
    distance_miles = round(distance_m / 1609.344, 2) if distance_m > 0 else 0.0
    moving_time_sec = int(raw.get("moving_time") or raw.get("elapsed_time") or 0)
    pace_sec = int(round(moving_time_sec / distance_miles)) if distance_miles > 0.05 else 0
    elev_m = float(raw.get("total_elevation_gain") or 0.0)
    elev_ft = round(elev_m * 3.28084, 1)

    start_local = str(raw.get("start_date_local") or raw.get("start_date") or "")
    day = start_local[:10] if len(start_local) >= 10 else ""

    return {
        "activity_id": str(raw.get("id") or ""),
        "day": day,
        "name": str(raw.get("name") or "Run"),
        "sport_type": sport_type,
        "distance_miles": distance_miles,
        "moving_time_sec": moving_time_sec,
        "pace_sec": pace_sec,
        "pace_str": format_pace_per_mile(pace_sec),
        "elevation_gain_ft": elev_ft,
        "average_hr": float(raw["average_heartrate"]) if raw.get("average_heartrate") is not None else None,
        "max_hr": float(raw["max_heartrate"]) if raw.get("max_heartrate") is not None else None,
        "suffer_score": float(raw["suffer_score"]) if raw.get("suffer_score") is not None else None,
        "raw": raw,
    }


class StravaManager:
    """Manages Strava OAuth2 credentials, rotating tokens, and GPS activity sync."""

    def __init__(
        self,
        store: ConfigStore,
        transport: HttpTransport = default_http_transport,
    ) -> None:
        self.store = store
        self.transport = transport
        self.config_path = store.config_dir / "strava_config.json"
        self.tokens_path = store.config_dir / "strava_tokens.json"
        self._lock = threading.RLock()
        self._pending_state: str | None = None

    def load_config(self) -> StravaConfig:
        with self._lock:
            file_data: dict[str, Any] = {}
            if self.config_path.exists():
                file_data = json.loads(self.config_path.read_text(encoding="utf-8"))
            client_id = os.environ.get("STRAVA_CLIENT_ID") or file_data.get("client_id", "")
            client_secret = os.environ.get("STRAVA_CLIENT_SECRET") or file_data.get("client_secret", "")
            redirect_uri = os.environ.get("STRAVA_REDIRECT_URI") or file_data.get(
                "redirect_uri", DEFAULT_STRAVA_REDIRECT_URI
            )
            scope = file_data.get("scope") or DEFAULT_STRAVA_SCOPE
            return StravaConfig(
                client_id=str(client_id).strip(),
                client_secret=str(client_secret).strip(),
                redirect_uri=str(redirect_uri).strip(),
                scope=str(scope).strip(),
            )

    def save_config(self, cfg: StravaConfig) -> None:
        with self._lock:
            _atomic_write_json(self.config_path, asdict(cfg), mode=0o600)

    def load_tokens(self) -> StravaTokens | None:
        with self._lock:
            if not self.tokens_path.exists():
                return None
            raw = json.loads(self.tokens_path.read_text(encoding="utf-8"))
            if not raw.get("access_token"):
                return None
            return StravaTokens(
                access_token=str(raw["access_token"]),
                refresh_token=str(raw.get("refresh_token") or ""),
                expires_at=float(raw.get("expires_at") or 0.0),
                athlete_id=str(raw.get("athlete_id") or ""),
                athlete_name=str(raw.get("athlete_name") or ""),
                updated_at=float(raw.get("updated_at") or 0.0),
            )

    def save_tokens(self, tokens: StravaTokens) -> None:
        with self._lock:
            _atomic_write_json(self.tokens_path, asdict(tokens), mode=0o600)

    def clear_tokens(self) -> None:
        with self._lock:
            if self.tokens_path.exists():
                self.tokens_path.unlink(missing_ok=True)

    def build_authorize_url(self, config: StravaConfig | None = None) -> tuple[str, str]:
        cfg = config or self.load_config()
        if not cfg.is_configured:
            raise StravaError(
                "Strava Client ID and Client Secret are not configured yet. "
                "Create an app at https://www.strava.com/settings/api (Authorization Callback Domain: localhost)."
            )
        state = secrets.token_urlsafe(24)
        self._pending_state = state
        params = {
            "client_id": cfg.client_id,
            "redirect_uri": cfg.redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": cfg.scope,
            "state": state,
        }
        return f"{STRAVA_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", state

    def verify_state(self, received_state: str | None) -> bool:
        if not self._pending_state or not received_state:
            return False
        ok = secrets.compare_digest(self._pending_state, received_state)
        if ok:
            self._pending_state = None
        return ok

    def exchange_code(self, code: str) -> StravaTokens:
        cfg = self.load_config()
        if not cfg.is_configured:
            raise StravaError("Missing Strava Client ID / Client Secret.")
        payload = self._post_token(
            {
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            }
        )
        tokens = StravaTokens.from_response(payload)
        self.save_tokens(tokens)
        return tokens

    def refresh_tokens(self, force: bool = False) -> StravaTokens:
        with self._lock:
            current = self.load_tokens()
            if current is None:
                raise StravaError("Strava is not connected yet.")
            if not force and not current.is_expired():
                return current
            cfg = self.load_config()
            if not cfg.is_configured:
                raise StravaError("Missing Strava Client ID / Client Secret for refresh.")
            payload = self._post_token(
                {
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": current.refresh_token,
                }
            )
            rotated = StravaTokens.from_response(
                payload,
                fallback_athlete_id=current.athlete_id,
                fallback_athlete_name=current.athlete_name,
            )
            self.save_tokens(rotated)
            return rotated

    def get_valid_access_token(self) -> str:
        tok = self.load_tokens()
        if tok is None:
            raise StravaError("Strava is not connected yet.")
        if tok.is_expired():
            tok = self.refresh_tokens(force=True)
        return tok.access_token

    def fetch_runs(self, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
        """Fetch and normalize GPS runs from Strava `/api/v3/athlete/activities`."""
        start_dt = dt.datetime.fromisoformat(f"{start_date}T00:00:00+00:00")
        after_epoch = int(start_dt.timestamp()) - 86400  # 1-day timezone buffer
        before_epoch: int | None = None
        if end_date:
            end_dt = dt.datetime.fromisoformat(f"{end_date}T23:59:59+00:00")
            before_epoch = int(end_dt.timestamp()) + 86400

        runs: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, str] = {
                "after": str(after_epoch),
                "page": str(page),
                "per_page": "100",
            }
            if before_epoch is not None:
                params["before"] = str(before_epoch)

            batch = self._get_json("/athlete/activities", params=params)
            if not isinstance(batch, list) or not batch:
                break

            for raw_item in batch:
                if isinstance(raw_item, dict):
                    norm = normalize_strava_activity(raw_item)
                    if norm and norm["day"] >= start_date and (not end_date or norm["day"] <= end_date):
                        runs.append(norm)

            if len(batch) < 100:
                break
            page += 1

        return runs

    def _post_token(self, form: dict[str, str]) -> dict[str, Any]:
        encoded = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            STRAVA_TOKEN_URL,
            data=encoded,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "oura-connect/0.2.0",
            },
        )
        status, _, body = self.transport(req, 30.0)
        text = body.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"raw": text}
        if status < 200 or status >= 300:
            raise StravaError(f"Strava OAuth request failed (HTTP {status}): {parsed.get('message') or text}")
        if "access_token" not in parsed:
            raise StravaError("Strava token response missing `access_token`.")
        return parsed

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f"{STRAVA_API_BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        retried = False
        while True:
            token = self.get_valid_access_token()
            req = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "oura-connect/0.2.0",
                },
            )
            status, _, body = self.transport(req, 30.0)
            if status == 401 and not retried:
                retried = True
                self.refresh_tokens(force=True)
                continue
            text = body.decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text) if text else {}
            except json.JSONDecodeError:
                parsed = {"raw": text}
            if 200 <= status < 300:
                return parsed
            msg = parsed.get("message") if isinstance(parsed, dict) else text
            raise StravaError(f"Strava API error (HTTP {status}) on {path}: {msg or text}")
