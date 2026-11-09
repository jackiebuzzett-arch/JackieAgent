"""OAuth 2.0 Authorization Code flow and single-use refresh token rotation for Oura API v2."""

from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .config import AppConfig, ConfigStore, OAuthTokens

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"

HttpTransport = Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]]


def default_http_transport(req: urllib.request.Request, timeout: float = 30.0) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, resp.read()
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        body = exc.read() if exc.fp else b""
        return exc.code, headers, body


class OuraOAuthError(RuntimeError):
    """Raised when OAuth configuration, authorization exchange, or refresh fails."""


class OAuthManager:
    """Handles Oura OAuth 2.0 URL generation, code exchange, and thread-safe token rotation."""

    def __init__(
        self,
        store: ConfigStore,
        transport: HttpTransport = default_http_transport,
    ) -> None:
        self.store = store
        self.transport = transport
        self._refresh_lock = threading.Lock()
        self._pending_state: str | None = None

    def generate_state(self) -> str:
        state = secrets.token_urlsafe(24)
        self._pending_state = state
        return state

    def verify_state(self, received_state: str | None) -> bool:
        if not self._pending_state or not received_state:
            return False
        matched = secrets.compare_digest(self._pending_state, received_state)
        if matched:
            self._pending_state = None
        return matched

    def build_authorize_url(self, state: str | None = None, config: AppConfig | None = None) -> tuple[str, str]:
        cfg = config or self.store.load_config()
        if not cfg.is_configured:
            raise OuraOAuthError(
                "Oura Client ID and Client Secret are not configured yet. "
                "Register an OAuth application at https://cloud.ouraring.com first."
            )
        csrf_state = state or self.generate_state()
        params = {
            "response_type": "code",
            "client_id": cfg.client_id,
            "redirect_uri": cfg.redirect_uri,
            "scope": " ".join(cfg.scopes),
            "state": csrf_state,
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", csrf_state

    def exchange_code(self, code: str, redirect_uri: str | None = None) -> OAuthTokens:
        cfg = self.store.load_config()
        if not cfg.is_configured:
            raise OuraOAuthError("Cannot exchange authorization code without Client ID and Client Secret.")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri or cfg.redirect_uri,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
        }
        data = self._post_token_form(form)
        tokens = OAuthTokens.from_token_response(data)
        self.store.save_tokens(tokens)
        return tokens

    def refresh_tokens(self, force: bool = False) -> OAuthTokens:
        """Refresh the access token and atomically save Oura's rotated single-use refresh token."""
        with self._refresh_lock:
            current = self.store.load_tokens()
            if current is None:
                raise OuraOAuthError("Not authenticated with Oura yet. Run `auth` or connect via the dashboard.")
            if not force and not current.is_expired():
                return current
            if not current.refresh_token:
                raise OuraOAuthError(
                    "Access token expired and no refresh_token is available. Please re-authenticate."
                )

            cfg = self.store.load_config()
            if not cfg.is_configured:
                raise OuraOAuthError("Missing Client ID / Client Secret needed to refresh Oura token.")

            form = {
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
            }
            data = self._post_token_form(form)
            rotated = OAuthTokens.from_token_response(data, fallback_refresh=current.refresh_token)
            self.store.save_tokens(rotated)
            return rotated

    def get_valid_access_token(self) -> str:
        tokens = self.store.load_tokens()
        if tokens is None:
            raise OuraOAuthError("No Oura OAuth token found. Run `python3 -m oura_connect auth` or use the web UI.")
        if tokens.is_expired():
            tokens = self.refresh_tokens(force=True)
        return tokens.access_token

    def _post_token_form(self, form: dict[str, str]) -> dict[str, Any]:
        encoded = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            TOKEN_URL,
            data=encoded,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "oura-connect/0.1.0",
            },
        )
        status, _, body = self.transport(req, 30.0)
        text = body.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"raw": text}

        if status < 200 or status >= 300:
            detail = parsed.get("error_description") or parsed.get("detail") or parsed.get("error") or text
            raise OuraOAuthError(f"Oura OAuth token request failed (HTTP {status}): {detail}")
        if "access_token" not in parsed:
            raise OuraOAuthError("Oura OAuth token response did not include `access_token`.")
        return parsed
