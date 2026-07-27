"""JD Edwards AIS server client.

AIS authentication is stateful, which is the single most common reason
generic REST connectors work in a demo and fail intermittently in production.
Three things matter:

* One long-lived token, reused across calls. Requesting a token per call will
  exhaust your licensed session pool.
* A stable ``deviceName``. JDE keys sessions to it; randomizing it per process
  leaks sessions that linger until they time out server-side.
* Reactive re-auth. Tokens can die before the TTL you predicted (server
  restart, admin logout, idle timeout). Catch the auth failure, re-auth once,
  and retry — do not let it surface as a confusing 500 to the model.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class AISError(RuntimeError):
    """An AIS call failed in a way the caller should surface to the model."""


class AISAuthError(AISError):
    """Credentials or session are not usable."""


class AISSession:
    """Thread-safe AIS session holder with lazy auth and one-shot retry."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.s = settings
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.RLock()
        self._explicit_client = client
        self._client_instance: httpx.Client | None = client

    @property
    def _client(self) -> httpx.Client:
        """Build the HTTP client on first use, not at import.

        Constructing it eagerly means anything environmental — a malformed
        proxy variable, a missing TLS extra — turns into an import error, and
        the MCP client shows the user "server failed to start" with no usable
        detail. Deferring it lets the server come up and report the problem
        through check_connection instead.
        """
        with self._lock:
            if self._client_instance is None:
                self._client_instance = httpx.Client(
                    timeout=httpx.Timeout(90.0, connect=15.0),
                    verify=self.s.verify_tls,
                )
            return self._client_instance

    # -- auth ---------------------------------------------------------------

    def _login(self) -> str:
        payload: dict[str, Any] = {
            "username": self.s.username,
            "password": self.s.password,
            "deviceName": self.s.device_name,
        }
        if self.s.environment:
            payload["environment"] = self.s.environment
        if self.s.role:
            payload["role"] = self.s.role

        url = f"{self.s.ais_url}/jderest/v2/tokenrequest"
        try:
            resp = self._client.post(url, json=payload)
        except httpx.RequestError as exc:
            raise AISError(f"cannot reach AIS server at {self.s.ais_url}: {exc}") from exc

        if resp.status_code in (401, 403):
            raise AISAuthError(
                "AIS rejected the service account credentials. Check "
                "JDE_USERNAME/JDE_PASSWORD and that the account is not locked."
            )
        if resp.status_code >= 400:
            raise AISError(f"token request failed ({resp.status_code}): {resp.text[:400]}")

        data = resp.json()
        token = (data.get("userInfo") or {}).get("token")
        if not token:
            raise AISAuthError(f"AIS response contained no token: {str(data)[:400]}")

        self._token = token
        self._expires_at = time.time() + self.s.token_ttl_seconds
        log.info("AIS session established (device=%s)", self.s.device_name)
        return token

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            return self._login()

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def logout(self) -> None:
        with self._lock:
            if not self._token:
                return
            try:
                self._client.post(
                    f"{self.s.ais_url}/jderest/v2/tokenrequest/logout",
                    json={"token": self._token},
                )
            except httpx.RequestError:
                pass  # best effort; the session will idle out server-side
            finally:
                self._token = None
                self._expires_at = 0.0

    # -- requests -----------------------------------------------------------

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to an AIS endpoint, injecting the token and retrying auth once."""
        url = f"{self.s.ais_url}{path}"

        for attempt in (1, 2):
            body = {**payload, "token": self.token()}
            try:
                resp = self._client.post(url, json=body)
            except httpx.RequestError as exc:
                raise AISError(f"AIS request to {path} failed: {exc}") from exc

            if resp.status_code in (401, 403) and attempt == 1:
                log.info("AIS token rejected, re-authenticating and retrying once")
                self.invalidate()
                continue

            if resp.status_code >= 400:
                raise AISError(
                    f"AIS {path} returned {resp.status_code}: {resp.text[:600]}"
                )

            try:
                return resp.json()
            except ValueError as exc:
                raise AISError(f"AIS {path} returned non-JSON body") from exc

        raise AISAuthError(f"AIS {path} failed authentication after retry")

    # -- convenience wrappers ----------------------------------------------

    def data_service(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Query a table or business view through the AIS data service.

        This path honours the service account's row and column security, which
        is why it is preferred over direct JDBC for anything agent-facing.
        """
        return self.post("/jderest/v2/dataservice", payload)

    def run_orchestration(self, name: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """Invoke a published orchestration by name.

        Orchestrations are the sanctioned route for writes: they call JDE
        business functions, so posting rules, validations and audit triggers
        all still apply.
        """
        return self.post(f"/jderest/v3/orchestrator/{name}", {"inputs": inputs})

    def health(self) -> dict[str, Any]:
        """Cheap round trip used by the health tool to prove connectivity."""
        token = self.token()
        return {"authenticated": bool(token), "ais_url": self.s.ais_url,
                "device_name": self.s.device_name}

    def close(self) -> None:
        if self._client_instance is not None:
            self.logout()
            self._client_instance.close()
            self._client_instance = self._explicit_client
