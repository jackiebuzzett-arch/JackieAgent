"""Cloud Secret Manager integration (Google Cloud Secret Manager SDK + REST + env + 0600 fallback)."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .observability import DEFAULT_LOGGER, log_event

try:
    from google.cloud import secretmanager as gcp_secretmanager  # type: ignore[import-untyped]

    HAS_GCP_SECRET_SDK = True
except Exception:  # pragma: no cover
    gcp_secretmanager = None  # type: ignore[assignment]
    HAS_GCP_SECRET_SDK = False


@dataclass(frozen=True)
class SecretValue:
    """Redaction-safe wrapper around secret plaintext values."""

    _value: str
    source: str

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"SecretValue(source={self.source!r}, value='[REDACTED]')"

    def __str__(self) -> str:
        return "[REDACTED]"


class CloudSecretManagerResolver:
    """Tiered secret resolver: Google Cloud Secret Manager -> Environment -> Local 0600 store."""

    def __init__(
        self,
        project_id: str | None = None,
        cache_ttl_seconds: int = 300,
        transport: Any = None,
    ) -> None:
        self.project_id = (
            project_id
            or os.environ.get("GCP_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or ""
        ).strip()
        self.cache_ttl_seconds = cache_ttl_seconds
        self._transport = transport
        self._cache: dict[str, tuple[float, SecretValue]] = {}

    def resolve(
        self,
        secret_id: str,
        env_var: str | None = None,
        fallback_value: str = "",
    ) -> SecretValue:
        now = time.time()
        cached = self._cache.get(secret_id)
        if cached and cached[0] > now:
            return cached[1]

        # Tier 1: Google Cloud Secret Manager (when GCP project is configured)
        if self.project_id:
            gcp_val = self._fetch_from_gcp_secret_manager(secret_id)
            if gcp_val:
                wrapped = SecretValue(gcp_val, source="gcp_secret_manager")
                self._cache[secret_id] = (now + self.cache_ttl_seconds, wrapped)
                log_event(
                    DEFAULT_LOGGER,
                    "secrets.resolved",
                    "Resolved secret from Google Cloud Secret Manager",
                    secret_id=secret_id,
                    source="gcp_secret_manager",
                )
                return wrapped

        # Tier 2: Environment variable
        if env_var:
            env_val = os.environ.get(env_var, "").strip()
            if env_val:
                wrapped = SecretValue(env_val, source=f"env:{env_var}")
                self._cache[secret_id] = (now + self.cache_ttl_seconds, wrapped)
                return wrapped

        # Tier 3: Local 0600 file store fallback
        if fallback_value:
            return SecretValue(fallback_value, source="local_0600_store")

        return SecretValue("", source="unset")

    def _fetch_from_gcp_secret_manager(self, secret_id: str) -> str:
        name = f"projects/{self.project_id}/secrets/{secret_id}/versions/latest"

        # 1a. Official google-cloud-secret-manager client if installed
        if HAS_GCP_SECRET_SDK and gcp_secretmanager is not None and self._transport is None:
            try:
                client = gcp_secretmanager.SecretManagerServiceClient()
                response = client.access_secret_version(request={"name": name})
                return response.payload.data.decode("utf-8").strip()
            except Exception as exc:
                log_event(
                    DEFAULT_LOGGER,
                    "secrets.gcp_sdk_fallback",
                    f"GCP Secret Manager SDK lookup fell back: {type(exc).__name__}",
                    secret_id=secret_id,
                )

        # 1b. Zero-dependency GCP Metadata Server + Secret Manager v1 REST API (Cloud Run / GCE)
        try:
            access_token = os.environ.get("GCP_ACCESS_TOKEN") or self._fetch_metadata_access_token()
            if not access_token:
                return ""
            url = f"https://secretmanager.googleapis.com/v1/{name}:access"
            req = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
            payload = self._http_json(req)
            b64_data = ((payload.get("payload") or {}).get("data")) or ""
            if b64_data:
                return base64.b64decode(b64_data).decode("utf-8").strip()
        except Exception as exc:
            log_event(
                DEFAULT_LOGGER,
                "secrets.gcp_rest_error",
                f"GCP Secret Manager REST lookup unavailable: {type(exc).__name__}",
                secret_id=secret_id,
            )
        return ""

    def _fetch_metadata_access_token(self) -> str:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        payload = self._http_json(req, timeout=1.5)
        return str(payload.get("access_token") or "")

    def _http_json(self, req: urllib.request.Request, timeout: float = 5.0) -> dict[str, Any]:
        if self._transport is not None:
            status, _, body = self._transport(req, timeout)
            if status < 200 or status >= 300:
                raise RuntimeError(f"HTTP {status}")
            return json.loads(body.decode("utf-8"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
