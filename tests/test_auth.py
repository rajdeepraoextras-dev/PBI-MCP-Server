"""core/auth: source precedence, msal-backed flows (msal faked in
sys.modules), in-memory caching/refresh, redaction, describe() hygiene."""

from __future__ import annotations

import base64
import json
import sys
import types

import pytest

from core.auth import (
    ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_PUBLIC_CLIENT_ID, ENV_TENANT,
    ENV_TOKEN, SCOPE, SOURCE_DEVICE_CODE, SOURCE_ENV, SOURCE_SERVICE_PRINCIPAL,
    SOURCE_SESSION, AuthError, TokenProvider, jwt_claims, redact,
)


def make_jwt(**claims) -> str:
    def part(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.fakesignature"


USER_JWT = make_jwt(upn="user@contoso.com", tid="tenant-1", exp=4_000_000_000)


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeConfidentialApp:
    instances: list["FakeConfidentialApp"] = []

    def __init__(self, client_id, authority=None, client_credential=None):
        self.client_id, self.authority = client_id, authority
        self.client_credential = client_credential
        self.calls = 0
        self.result: dict = {"expires_in": 3600}
        FakeConfidentialApp.instances.append(self)

    def acquire_token_for_client(self, scopes):
        assert scopes == [SCOPE]
        self.calls += 1
        if "error" in self.result:
            return dict(self.result)
        return {**self.result, "access_token": f"sp-token-{self.calls}"}


class FakePublicApp:
    instances: list["FakePublicApp"] = []

    def __init__(self, client_id, authority=None):
        self.client_id, self.authority = client_id, authority
        self.flow = {"user_code": "ABCD-1234", "device_code": "the-device-secret",
                     "verification_uri": "https://microsoft.com/devicelogin",
                     "message": "go sign in", "expires_in": 900,
                     "expires_at": 1_000_900.0, "interval": 5}
        self.poll_results: list[dict] = []
        self.polls = 0
        FakePublicApp.instances.append(self)

    def initiate_device_flow(self, scopes):
        assert scopes == [SCOPE]
        return dict(self.flow)

    def acquire_token_by_device_flow(self, flow, **kwargs):
        assert callable(kwargs.get("exit_condition"))
        assert flow["device_code"] == "the-device-secret"
        self.polls += 1
        return self.poll_results.pop(0)

    def get_accounts(self):
        return []

    def acquire_token_silent(self, scopes, account=None):
        return None


@pytest.fixture
def fake_msal(monkeypatch):
    FakeConfidentialApp.instances.clear()
    FakePublicApp.instances.clear()
    mod = types.ModuleType("msal")
    mod.ConfidentialClientApplication = FakeConfidentialApp
    mod.PublicClientApplication = FakePublicApp
    mod.__version__ = "0.fake"
    monkeypatch.setitem(sys.modules, "msal", mod)
    return mod


@pytest.fixture
def no_msal(monkeypatch):
    monkeypatch.setitem(sys.modules, "msal", None)


SP_ENV = {ENV_TENANT: "tenant-1", ENV_CLIENT_ID: "app-1",
          ENV_CLIENT_SECRET: "super-secret-value"}


# --- precedence -------------------------------------------------------------

def test_no_credentials_error_names_every_option():
    p = TokenProvider(env={})
    with pytest.raises(AuthError) as ei:
        p.get_token()
    msg = str(ei.value)
    for needle in ("pbi_service_login(token=", ENV_TOKEN, ENV_TENANT,
                   "device_code=True", ENV_PUBLIC_CLIENT_ID):
        assert needle in msg
    assert p.describe()["active_source"] is None


def test_env_token_is_used_and_described_without_leaking():
    p = TokenProvider(env={ENV_TOKEN: USER_JWT})
    assert p.get_token() == USER_JWT and p.last_source == SOURCE_ENV
    d = p.describe()
    assert d["active_source"] == SOURCE_ENV
    assert d["user"] == "user@contoso.com" and d["tenant_id"] == "tenant-1"
    assert d["expired"] is False
    assert USER_JWT not in json.dumps(d)


def test_session_token_beats_env_and_is_normalised():
    p = TokenProvider(env={ENV_TOKEN: "env-token"})
    info = p.set_token("  Bearer session-token  ")
    assert info["active_source"] == SOURCE_SESSION
    assert p.get_token() == "session-token" and p.last_source == SOURCE_SESSION
    p.clear_token()
    assert p.get_token() == "env-token"
    with pytest.raises(AuthError):
        p.set_token("")
    with pytest.raises(AuthError):
        p.set_token("has whitespace inside")


def test_env_token_beats_service_principal(fake_msal):
    p = TokenProvider(env={**SP_ENV, ENV_TOKEN: "env-token"})
    assert p.get_token() == "env-token"
    assert FakeConfidentialApp.instances == []  # msal never touched


# --- service principal ------------------------------------------------------

def test_service_principal_caches_then_refreshes_before_expiry(fake_msal):
    clock = FakeClock()
    p = TokenProvider(env=SP_ENV, clock=clock)
    assert p.describe()["active_source"] == SOURCE_SERVICE_PRINCIPAL

    assert p.get_token() == "sp-token-1" and p.last_source == SOURCE_SERVICE_PRINCIPAL
    assert p.get_token() == "sp-token-1"  # cached, no second acquisition
    app = FakeConfidentialApp.instances[0]
    assert app.calls == 1 and len(FakeConfidentialApp.instances) == 1
    assert app.client_id == "app-1" and app.client_credential == "super-secret-value"
    assert app.authority == "https://login.microsoftonline.com/tenant-1"

    clock.t += 3600 - 200  # inside the 300 s refresh margin -> refresh
    assert p.get_token() == "sp-token-2" and app.calls == 2
    d = p.describe()
    assert d["tenant_id"] == "tenant-1" and d["app_id"] == "app-1"
    assert d["cache_valid"] is True
    assert "super-secret-value" not in json.dumps(d)
    assert "sp-token" not in json.dumps(d)


def test_service_principal_failure_is_actionable(fake_msal):
    p = TokenProvider(env=SP_ENV)
    app = FakeConfidentialApp("app-1", client_credential="super-secret-value")
    app.result = {"error": "invalid_client",
                  "error_description": "AADSTS7000215 bad secret"}
    p._sp_app = app  # pre-seed the (lazily built) msal app with a failing one
    with pytest.raises(AuthError) as ei:
        p.get_token()
    msg = str(ei.value)
    assert "invalid_client" in msg and ENV_CLIENT_SECRET in msg
    assert "super-secret-value" not in msg


def test_partial_service_principal_config_warns():
    p = TokenProvider(env={ENV_TENANT: "t", ENV_CLIENT_ID: "c"})
    d = p.describe()
    assert d["active_source"] is None and d["sources"][SOURCE_SERVICE_PRINCIPAL] is False
    assert ENV_CLIENT_SECRET in d["warning"]


def test_missing_msal_gives_install_hint(no_msal):
    p = TokenProvider(env=SP_ENV)
    with pytest.raises(AuthError, match="msal is not installed"):
        p.get_token()
    assert p.describe()["msal_available"] is False


# --- device code ------------------------------------------------------------

def test_device_code_requires_public_client_id(fake_msal):
    p = TokenProvider(env={})
    with pytest.raises(AuthError) as ei:
        p.device_code_login()
    assert ENV_PUBLIC_CLIENT_ID in str(ei.value) and "public client" in str(ei.value)


def test_device_code_start_poll_complete(fake_msal):
    clock = FakeClock()
    p = TokenProvider(env={ENV_PUBLIC_CLIENT_ID: "pub-app"}, clock=clock)

    first = p.device_code_login()
    assert first["status"] == "pending" and first["user_code"] == "ABCD-1234"
    assert first["verification_uri"] == "https://microsoft.com/devicelogin"
    assert "device_code" not in first and "the-device-secret" not in json.dumps(first)
    app = FakePublicApp.instances[0]
    assert app.client_id == "pub-app"
    assert app.authority == "https://login.microsoftonline.com/organizations"
    assert p.describe()["sources"][SOURCE_DEVICE_CODE] == "pending"

    app.poll_results = [
        {"error": "authorization_pending", "error_description": "wait"},
        {"access_token": "device-token", "expires_in": 3600,
         "id_token_claims": {"preferred_username": "me@contoso.com",
                             "tid": "tenant-9"}},
    ]
    pending = p.device_code_login()
    assert pending["status"] == "pending" and pending["user_code"] == "ABCD-1234"
    done = p.device_code_login()
    assert done == {"status": "complete", "source": SOURCE_DEVICE_CODE,
                    "user": "me@contoso.com", "tenant_id": "tenant-9"}
    assert app.polls == 2

    assert p.get_token() == "device-token" and p.last_source == SOURCE_DEVICE_CODE
    d = p.describe()
    assert d["active_source"] == SOURCE_DEVICE_CODE
    assert d["user"] == "me@contoso.com"
    assert d["sources"][SOURCE_DEVICE_CODE] == "signed_in"
    assert "device-token" not in json.dumps(d)

    # a session token still wins over the device-code token
    p.set_token("session")
    assert p.get_token() == "session"


def test_device_code_expired_flow_restarts_and_failure_raises(fake_msal):
    clock = FakeClock()
    p = TokenProvider(env={ENV_PUBLIC_CLIENT_ID: "pub-app",
                           ENV_TENANT: "tenant-1"}, clock=clock)
    p.device_code_login()
    app = FakePublicApp.instances[0]
    assert app.authority.endswith("/tenant-1")

    clock.t = 2_000_000.0  # past flow["expires_at"]
    out = p.device_code_login()
    assert out["status"] == "pending" and "expired" in out["note"]
    assert app.polls == 0  # never polled an expired flow

    clock.t = 1_000_000.0
    app.poll_results = [{"error": "expired_token",
                         "error_description": "code expired"}]
    with pytest.raises(AuthError, match="expired_token"):
        p.device_code_login()
    # the failed flow is dropped: the next call starts fresh
    assert p.device_code_login()["note"] == "started"


def test_device_code_wait_seconds_drives_exit_condition(fake_msal):
    clock = FakeClock()
    p = TokenProvider(env={ENV_PUBLIC_CLIENT_ID: "pub-app"}, clock=clock)
    p.device_code_login()
    app = FakePublicApp.instances[0]
    captured = {}

    def poll(flow, **kw):
        captured["exit"] = kw["exit_condition"]
        return {"error": "authorization_pending"}
    app.acquire_token_by_device_flow = poll
    p.device_code_login(wait_seconds=30)
    exit_condition = captured["exit"]
    assert exit_condition({}) is False
    clock.t += 31
    assert exit_condition({}) is True


# --- redaction + claims -----------------------------------------------------

def test_redact_masks_secret_keys_jwts_and_bearer_strings():
    obj = {
        "access_token": "abc", "Authorization": "Bearer abc", "refreshToken": "r",
        "nested": {"client_secret": "s", "ok": "fine", "password": "p"},
        "list": [{"api_key": "k"}, "note eyJhbGciOi.eyJzdWIiOi.sig end"],
        "text": f"Bearer {USER_JWT} trailing",
        "url": "https://x/blob?sv=2020&sig=abc123&other=1",
        "user_code": "ABCD", "count": 3, "device_code": "not_started",
        "empty_token": None,
    }
    out = redact(obj)
    assert out["access_token"] == out["Authorization"] == out["refreshToken"] == "***"
    assert out["nested"] == {"client_secret": "***", "ok": "fine", "password": "***"}
    assert out["list"][0] == {"api_key": "***"}
    assert out["list"][1] == "note *** end"
    assert out["text"] == "Bearer *** trailing" and USER_JWT not in out["text"]
    assert out["url"] == "https://x/blob?sv=***&sig=***&other=1"
    assert out["user_code"] == "ABCD" and out["count"] == 3
    assert out["device_code"] == "not_started"  # a status word, not a secret
    assert out["empty_token"] is None
    assert obj["access_token"] == "abc"  # input untouched


def test_jwt_claims_whitelist_and_garbage():
    claims = jwt_claims(make_jwt(preferred_username="a@b", tid="t", appid="app",
                                 exp=1, secret="hidden"))
    assert claims == {"user": "a@b", "tenant_id": "t", "app_id": "app",
                      "expires_at": "1970-01-01T00:00:01Z", "expired": True}
    assert jwt_claims("not.a.jwt") == {}
    assert jwt_claims("plain-token") == {}
    assert jwt_claims("") == {}
    assert jwt_claims("a.b.c.d") == {}


def test_describe_precedence_order_is_documented():
    d = TokenProvider(env={}).describe()
    assert d["precedence"] == [SOURCE_SESSION, SOURCE_ENV,
                               SOURCE_SERVICE_PRINCIPAL, SOURCE_DEVICE_CODE]
    assert set(d["sources"]) == set(d["precedence"])
