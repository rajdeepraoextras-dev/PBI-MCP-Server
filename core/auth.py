"""Token providers for the Power BI / Fabric REST APIs (pbi-service server).

Credentials are resolved in this fixed order; the first source that is
configured wins:

  (a) a token handed to the session via ``pbi_service_login(token=...)``
  (b) env ``PBI_ACCESS_TOKEN`` (a bearer token you obtained elsewhere, e.g.
      ``az account get-access-token --resource https://analysis.windows.net/powerbi/api``)
  (c) a service principal from env ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID`` /
      ``AZURE_CLIENT_SECRET`` (msal ``ConfidentialClientApplication``, cached
      in memory and refreshed before expiry)
  (d) an interactive device-code sign-in started with
      ``pbi_service_login(device_code=True)`` (msal
      ``PublicClientApplication``). No client id is hard-coded: set env
      ``PBI_CLIENT_ID`` to the application (client) id of a public client app
      registered in your Entra tenant with "Allow public client flows" on and
      the Power BI Service delegated permissions you need.

msal is imported lazily (optional extra ``pbi-mcp[cloud]``) so the local-only
servers never depend on it. Tokens live in process memory only: nothing here
writes to disk or logs, and ``describe()`` / ``redact()`` exist so tool
results can report *which* source is active without ever echoing a secret.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any

SCOPE = "https://analysis.windows.net/powerbi/api/.default"
AUTHORITY = "https://login.microsoftonline.com/{tenant}"

ENV_TOKEN = "PBI_ACCESS_TOKEN"
ENV_TENANT = "AZURE_TENANT_ID"
ENV_CLIENT_ID = "AZURE_CLIENT_ID"
ENV_CLIENT_SECRET = "AZURE_CLIENT_SECRET"
ENV_PUBLIC_CLIENT_ID = "PBI_CLIENT_ID"

#: refresh a cached token this many seconds before it expires
REFRESH_MARGIN = 300

SOURCE_SESSION = "session_token"
SOURCE_ENV = "env_token"
SOURCE_SERVICE_PRINCIPAL = "service_principal"
SOURCE_DEVICE_CODE = "device_code"

REDACTED = "***"


class AuthError(ValueError):
    """No usable credential, or the identity provider rejected the request."""


# --- redaction --------------------------------------------------------------

_SECRET_KEY_WORDS = ("token", "secret", "password", "passwd", "authorization",
                     "credential", "apikey", "api_key", "api-key")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:sig|signature|token|access_token|sv|se|sp)=)[^&\s]+")


def _is_secret_key(key: Any) -> bool:
    k = str(key).lower()
    return any(w in k for w in _SECRET_KEY_WORDS)


def redact(obj: Any) -> Any:
    """Return a copy of `obj` with every secret masked.

    * dict keys that look like credentials (token, secret, password,
      authorization, credential, api key ...) have their value replaced by
      ``***`` whatever it is;
    * every string is scrubbed of JWT-shaped values, ``Bearer ...`` prefixes
      and SAS-style query parameters.
    Safe for nested dicts/lists; other values pass through unchanged.
    """
    if isinstance(obj, Mapping):
        out = {}
        for k, v in obj.items():
            if _is_secret_key(k) and v not in (None, "", False):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        s = _JWT.sub(REDACTED, obj)
        s = _BEARER.sub("Bearer " + REDACTED, s)
        s = _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, s)
        return s
    return obj


# --- JWT peek (unverified, for describe() only) -----------------------------

def jwt_claims(token: str) -> dict:
    """Best-effort, *unverified* decode of a JWT payload for display only.

    Returns a small whitelist of claims (user, tenant, app id, expiry) or an
    empty dict for anything that is not a JWT. The token itself is never
    included.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:  # noqa: BLE001 - any malformed input -> no claims
        return {}
    if not isinstance(claims, dict):
        return {}
    out: dict = {}
    user = (claims.get("upn") or claims.get("preferred_username")
            or claims.get("unique_name") or claims.get("email"))
    if user:
        out["user"] = user
    if claims.get("tid"):
        out["tenant_id"] = claims["tid"]
    if claims.get("appid") or claims.get("azp"):
        out["app_id"] = claims.get("appid") or claims.get("azp")
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        out["expires_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp))
        out["expired"] = exp < time.time()
    return out


def _import_msal():
    try:
        import msal  # optional dependency, imported lazily
    except ImportError as e:
        raise AuthError(
            "msal is not installed. Install the cloud extra "
            "(pip install \"pbi-mcp[cloud]\" or pip install msal) to use a "
            "service principal or device-code sign-in, or pass a ready token "
            "via pbi_service_login(token=...) / env PBI_ACCESS_TOKEN."
        ) from e
    return msal


# --- provider ---------------------------------------------------------------

class TokenProvider:
    """Resolve a bearer token from the configured sources (see module doc).

    `env` defaults to ``os.environ``; `clock` to ``time.time``. Both are
    injectable so tests can drive expiry and configuration without touching
    the real environment.
    """

    def __init__(self, env: Mapping[str, str] | None = None, clock=time.time):
        self._env = env if env is not None else os.environ
        self._clock = clock
        self._session_token: str | None = None
        # service principal
        self._sp_app = None
        self._sp_token: tuple[str, float] | None = None
        # device code
        self._device_app = None
        self._device_flow: dict | None = None
        self._device_token: tuple[str, float] | None = None
        self._device_account: dict = {}
        self.last_source: str | None = None

    # --- (a) session token ---------------------------------------------

    def set_token(self, token: str) -> dict:
        """Use `token` for the rest of the session (highest precedence)."""
        if not isinstance(token, str) or not token.strip():
            raise AuthError("token must be a non-empty bearer token string")
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if any(ch.isspace() for ch in token):
            raise AuthError("token must not contain whitespace")
        self._session_token = token
        return self.describe()

    def clear_token(self) -> None:
        self._session_token = None

    # --- (c) service principal ------------------------------------------

    def _sp_config(self) -> tuple[str, str, str] | None:
        tenant = self._env.get(ENV_TENANT)
        client = self._env.get(ENV_CLIENT_ID)
        secret = self._env.get(ENV_CLIENT_SECRET)
        if tenant and client and secret:
            return tenant, client, secret
        return None

    def _service_principal_token(self) -> str:
        cached = self._sp_token
        if cached and cached[1] - self._clock() > REFRESH_MARGIN:
            return cached[0]
        tenant, client, secret = self._sp_config()  # type: ignore[misc]
        msal = _import_msal()
        if self._sp_app is None:
            self._sp_app = msal.ConfidentialClientApplication(
                client, authority=AUTHORITY.format(tenant=tenant),
                client_credential=secret)
        result = self._sp_app.acquire_token_for_client(scopes=[SCOPE]) or {}
        if "access_token" not in result:
            raise AuthError(
                "Service principal sign-in failed: "
                f"{result.get('error', 'unknown_error')}: "
                f"{result.get('error_description', 'no description')}. Check "
                f"{ENV_TENANT}/{ENV_CLIENT_ID}/{ENV_CLIENT_SECRET} and that the "
                "app is allowed to use Fabric APIs (tenant setting 'Service "
                "principals can use Fabric APIs') and is a workspace member.")
        expires = self._clock() + float(result.get("expires_in", 3600))
        self._sp_token = (result["access_token"], expires)
        return result["access_token"]

    # --- (d) device code -------------------------------------------------

    def _public_client(self):
        client_id = self._env.get(ENV_PUBLIC_CLIENT_ID)
        if not client_id:
            raise AuthError(
                f"Device-code sign-in needs env {ENV_PUBLIC_CLIENT_ID}: the "
                "application (client) id of a public client app registered in "
                "your Entra tenant (Authentication -> 'Allow public client "
                "flows' = Yes; API permissions -> Power BI Service delegated "
                "scopes). No client id is built in. Alternatively pass a token "
                "via pbi_service_login(token=...) or env PBI_ACCESS_TOKEN.")
        msal = _import_msal()
        if self._device_app is None:
            tenant = self._env.get(ENV_TENANT) or "organizations"
            self._device_app = msal.PublicClientApplication(
                client_id, authority=AUTHORITY.format(tenant=tenant))
        return self._device_app

    def start_device_code(self) -> dict:
        """Begin a device-code flow; returns the code + URL to show the user."""
        app = self._public_client()
        flow = app.initiate_device_flow(scopes=[SCOPE]) or {}
        if "user_code" not in flow:
            raise AuthError(
                "Could not start the device-code flow: "
                f"{flow.get('error', 'unknown_error')}: "
                f"{flow.get('error_description', 'no description')}")
        self._device_flow = flow
        return self._pending(flow, note="started")

    def _pending(self, flow: dict, note: str) -> dict:
        return {
            "status": "pending",
            "note": note,
            "user_code": flow.get("user_code"),
            "verification_uri": flow.get("verification_uri"),
            "message": flow.get("message"),
            "expires_in": flow.get("expires_in"),
            "next": "Open verification_uri, enter user_code and sign in; then "
                    "call pbi_service_login(device_code=True) again to finish.",
        }

    def poll_device_code(self, wait_seconds: float = 0) -> dict:
        """Poll a pending flow (at most `wait_seconds`); complete it if signed in."""
        flow = self._device_flow
        if flow is None:
            return self.start_device_code()
        expires_at = flow.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at < self._clock():
            self._device_flow = None
            out = self.start_device_code()
            out["note"] = "previous code expired; a new one was issued"
            return out
        app = self._public_client()
        deadline = self._clock() + max(0.0, float(wait_seconds))
        result = app.acquire_token_by_device_flow(
            flow, exit_condition=lambda _f: self._clock() >= deadline) or {}
        if "access_token" in result:
            self._device_flow = None
            expires = self._clock() + float(result.get("expires_in", 3600))
            self._device_token = (result["access_token"], expires)
            claims = result.get("id_token_claims") or {}
            self._device_account = {
                k: v for k, v in {
                    "user": claims.get("preferred_username") or claims.get("upn"),
                    "tenant_id": claims.get("tid"),
                }.items() if v
            }
            return {"status": "complete", "source": SOURCE_DEVICE_CODE,
                    **self._device_account}
        err = result.get("error")
        if err in ("authorization_pending", "slow_down", None):
            return self._pending(flow, note="waiting for sign-in")
        self._device_flow = None
        raise AuthError(
            f"Device-code sign-in failed: {err}: "
            f"{result.get('error_description', 'no description')}")

    def device_code_login(self, wait_seconds: float = 0) -> dict:
        """Tool entry point: start a flow, or poll the pending one."""
        if self._device_flow is None:
            return self.start_device_code()
        return self.poll_device_code(wait_seconds)

    def _device_token_value(self) -> str | None:
        cached = self._device_token
        if cached is None:
            return None
        if cached[1] - self._clock() > REFRESH_MARGIN:
            return cached[0]
        app = self._device_app
        if app is not None:
            accounts = app.get_accounts() if hasattr(app, "get_accounts") else []
            result = app.acquire_token_silent(
                [SCOPE], account=accounts[0] if accounts else None) or {}
            if "access_token" in result:
                expires = self._clock() + float(result.get("expires_in", 3600))
                self._device_token = (result["access_token"], expires)
                return result["access_token"]
        return cached[0] if cached[1] > self._clock() else None

    # --- resolution ------------------------------------------------------

    def active_source(self) -> str | None:
        """Which source would serve get_token(), without any network call."""
        if self._session_token:
            return SOURCE_SESSION
        if self._env.get(ENV_TOKEN):
            return SOURCE_ENV
        if self._sp_config():
            return SOURCE_SERVICE_PRINCIPAL
        if self._device_token is not None:
            return SOURCE_DEVICE_CODE
        return None

    def get_token(self) -> str:
        """The bearer token to send, from the first configured source."""
        if self._session_token:
            self.last_source = SOURCE_SESSION
            return self._session_token
        env_token = self._env.get(ENV_TOKEN)
        if env_token:
            self.last_source = SOURCE_ENV
            return env_token.strip()
        if self._sp_config():
            self.last_source = SOURCE_SERVICE_PRINCIPAL
            return self._service_principal_token()
        device = self._device_token_value()
        if device:
            self.last_source = SOURCE_DEVICE_CODE
            return device
        raise AuthError(
            "No Power BI credentials configured. Either call "
            "pbi_service_login(token=...) with a bearer token, set env "
            f"{ENV_TOKEN}, set {ENV_TENANT}/{ENV_CLIENT_ID}/{ENV_CLIENT_SECRET} "
            "for a service principal, or run "
            "pbi_service_login(device_code=True) (needs env "
            f"{ENV_PUBLIC_CLIENT_ID}).")

    def describe(self) -> dict:
        """Which source is active plus cheap identity hints; never a secret."""
        active = self.active_source()
        partial_sp = [k for k in (ENV_TENANT, ENV_CLIENT_ID, ENV_CLIENT_SECRET)
                      if self._env.get(k)]
        out: dict = {
            "active_source": active,
            "sources": {
                SOURCE_SESSION: bool(self._session_token),
                SOURCE_ENV: bool(self._env.get(ENV_TOKEN)),
                SOURCE_SERVICE_PRINCIPAL: bool(self._sp_config()),
                SOURCE_DEVICE_CODE: (
                    "signed_in" if self._device_token is not None
                    else "pending" if self._device_flow is not None
                    else "not_started"),
            },
            "precedence": [SOURCE_SESSION, SOURCE_ENV,
                           SOURCE_SERVICE_PRINCIPAL, SOURCE_DEVICE_CODE],
        }
        try:
            import msal  # noqa: F401
            out["msal_available"] = True
        except ImportError:
            out["msal_available"] = False
        if active == SOURCE_SESSION:
            out.update(jwt_claims(self._session_token or ""))
        elif active == SOURCE_ENV:
            out.update(jwt_claims(self._env.get(ENV_TOKEN, "")))
        elif active == SOURCE_SERVICE_PRINCIPAL:
            out["tenant_id"] = self._env.get(ENV_TENANT)
            out["app_id"] = self._env.get(ENV_CLIENT_ID)
            if self._sp_token is not None:
                out["cache_valid"] = self._sp_token[1] > self._clock()
        elif active == SOURCE_DEVICE_CODE:
            out.update(self._device_account)
        if 0 < len(partial_sp) < 3:
            out["warning"] = ("service principal is partially configured; set "
                              f"all of {ENV_TENANT}, {ENV_CLIENT_ID}, "
                              f"{ENV_CLIENT_SECRET}")
        if self._env.get(ENV_PUBLIC_CLIENT_ID):
            out["device_code_client_id"] = self._env.get(ENV_PUBLIC_CLIENT_ID)
        return redact(out)


__all__ = ["AuthError", "TokenProvider", "SCOPE", "redact", "jwt_claims",
           "ENV_TOKEN", "ENV_TENANT", "ENV_CLIENT_ID", "ENV_CLIENT_SECRET",
           "ENV_PUBLIC_CLIENT_ID", "SOURCE_SESSION", "SOURCE_ENV",
           "SOURCE_SERVICE_PRINCIPAL", "SOURCE_DEVICE_CODE"]
