"""Configuration and atomic 0600-permission OAuth token management for oura-connect."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_REDIRECT_PORT = 8080
DEFAULT_REDIRECT_URI = f"http://localhost:{DEFAULT_REDIRECT_PORT}/oauth/callback"

DEFAULT_SCOPES: list[str] = [
    "email",
    "personal",
    "daily",
    "heartrate",
    "workout",
    "tag",
    "session",
    "spo2",
    "heart_health",
]


def default_config_dir() -> Path:
    override = os.environ.get("OURA_CONNECT_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "oura-connect"


def default_data_dir() -> Path:
    override = os.environ.get("OURA_CONNECT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "oura-connect"


def _atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    """Atomically write JSON with strict file permissions so token rotation never corrupts state."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


@dataclass
class AppConfig:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = DEFAULT_REDIRECT_URI
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_at: float = 0.0
    scope: str = ""
    updated_at: float = field(default_factory=time.time)

    def is_expired(self, skew_seconds: int = 120) -> bool:
        if not self.expires_at:
            return False
        return time.time() + skew_seconds >= self.expires_at

    @classmethod
    def from_token_response(cls, data: dict[str, Any], fallback_refresh: str = "") -> "OAuthTokens":
        now = time.time()
        expires_in = float(data.get("expires_in") or 86400)
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or fallback_refresh),
            token_type=str(data.get("token_type") or "Bearer"),
            expires_at=now + expires_in,
            scope=str(data.get("scope") or ""),
            updated_at=now,
        )


class ConfigStore:
    """Thread-safe persistence for Oura OAuth credentials and rotating refresh tokens."""

    def __init__(self, config_dir: Path | None = None, data_dir: Path | None = None) -> None:
        self.config_dir = config_dir or default_config_dir()
        self.data_dir = data_dir or default_data_dir()
        self.config_path = self.config_dir / "config.json"
        self.tokens_path = self.config_dir / "tokens.json"
        self.db_path = self.data_dir / "oura.db"
        self._lock = threading.RLock()

    def load_config(self) -> AppConfig:
        with self._lock:
            file_data: dict[str, Any] = {}
            if self.config_path.exists():
                file_data = json.loads(self.config_path.read_text(encoding="utf-8"))

            client_id = os.environ.get("OURA_CLIENT_ID") or file_data.get("client_id", "")
            client_secret = os.environ.get("OURA_CLIENT_SECRET") or file_data.get("client_secret", "")
            redirect_uri = os.environ.get("OURA_REDIRECT_URI") or file_data.get(
                "redirect_uri", DEFAULT_REDIRECT_URI
            )
            scopes = file_data.get("scopes") or list(DEFAULT_SCOPES)
            return AppConfig(
                client_id=str(client_id).strip(),
                client_secret=str(client_secret).strip(),
                redirect_uri=str(redirect_uri).strip(),
                scopes=[str(s) for s in scopes],
            )

    def save_config(self, config: AppConfig) -> None:
        with self._lock:
            _atomic_write_json(self.config_path, asdict(config), mode=0o600)

    def load_tokens(self) -> OAuthTokens | None:
        with self._lock:
            if not self.tokens_path.exists():
                return None
            raw = json.loads(self.tokens_path.read_text(encoding="utf-8"))
            if not raw.get("access_token"):
                return None
            return OAuthTokens(
                access_token=str(raw["access_token"]),
                refresh_token=str(raw.get("refresh_token") or ""),
                token_type=str(raw.get("token_type") or "Bearer"),
                expires_at=float(raw.get("expires_at") or 0.0),
                scope=str(raw.get("scope") or ""),
                updated_at=float(raw.get("updated_at") or 0.0),
            )

    def save_tokens(self, tokens: OAuthTokens) -> None:
        with self._lock:
            _atomic_write_json(self.tokens_path, asdict(tokens), mode=0o600)

    def clear_tokens(self) -> None:
        with self._lock:
            if self.tokens_path.exists():
                self.tokens_path.unlink(missing_ok=True)
