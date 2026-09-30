import time
from collections.abc import Callable, Iterator
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from itsdangerous import URLSafeTimedSerializer
from joserfc import jwt
from joserfc.jwk import KeySet, OctKey, RSAKey
from starlette.requests import Request

from app.auth import google_oidc
from app.auth.google_oidc import (
    STATE_COOKIE_NAME,
    GoogleIdentity,
    build_authorization_redirect,
    clear_state_cookie,
    exchange_callback,
)
from app.config import get_settings
from app.errors import GOOGLE_AUTH_FAILED, AppError

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret-value"  # noqa: S105 - test fixture
REDIRECT_URI = "http://localhost:4200/api/auth/google/callback"

REQUIRED_ENV = {
    "IR_THROTTLE_SECRET": "test-throttle-secret",
    "IR_OIDC_STATE_SECRET": "test-oidc-state-secret",
    "IR_GOOGLE_CLIENT_ID": CLIENT_ID,
    "IR_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
    "IR_GOOGLE_REDIRECT_URI": REDIRECT_URI,
}

SIGNING_KEY = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "test-key"})


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _request(query: dict[str, str] | None = None, cookie: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookie is not None:
        headers.append((b"cookie", f"{STATE_COOKIE_NAME}={cookie}".encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/google/callback",
            "headers": headers,
            "query_string": urlencode(query or {}).encode(),
        }
    )


def _start() -> tuple[dict[str, list[str]], str]:
    """Run the start step and return the authorization URL query and the state cookie."""
    response = build_authorization_redirect(_request())
    query = parse_qs(urlparse(response.headers["location"]).query)
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return query, cookie[STATE_COOKIE_NAME].value


def _id_token(claims: dict[str, Any], key: RSAKey = SIGNING_KEY) -> str:
    return jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, key)


def _claims(expected_nonce: str, /, **overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-sub-123",
        "email": "candidate@example.com",
        "email_verified": True,
        "nonce": expected_nonce,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return claims


class _FakeGoogle:
    """Mock transport for the token endpoint and the JWKS endpoint."""

    def __init__(self, token_response: Callable[[], httpx.Response]) -> None:
        self.token_response = token_response
        self.token_requests: list[dict[str, list[str]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url == google_oidc.GOOGLE_TOKEN_ENDPOINT:
            self.token_requests.append(parse_qs(request.content.decode()))
            return self.token_response()
        if request.url == google_oidc.GOOGLE_JWKS_URI:
            return httpx.Response(200, json=KeySet([SIGNING_KEY]).as_dict(private=False))
        return httpx.Response(404)


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeGoogle) -> None:
    monkeypatch.setattr(
        google_oidc,
        "_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)),
    )


def _token_ok(id_token: str) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(
        200, json={"access_token": "at", "id_token": id_token, "token_type": "Bearer"}
    )


def _assert_google_failed(exc_info: pytest.ExceptionInfo[AppError]) -> None:
    assert exc_info.value.code == GOOGLE_AUTH_FAILED


def test_auth_10_authorization_redirect_uses_pkce_s256_state_and_nonce() -> None:
    response = build_authorization_redirect(_request())

    assert response.status_code in (302, 307)
    url = urlparse(response.headers["location"])
    assert url.scheme == "https"
    assert url.netloc == "accounts.google.com"
    query = parse_qs(url.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert query["state"][0]
    assert query["nonce"][0]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert set(query["scope"][0].split()) == {"openid", "email", "profile"}
    assert CLIENT_SECRET not in response.headers["location"]

    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{STATE_COOKIE_NAME}=")
    assert "HttpOnly" in set_cookie
    assert "Max-Age=600" in set_cookie
    assert "samesite=lax" in set_cookie.lower()


def test_auth_93_tampered_state_cookie_raises_google_auth_failed() -> None:
    query, cookie = _start()
    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"code": "c", "state": query["state"][0]}, cookie + "x"))
    _assert_google_failed(exc_info)


def test_auth_10_valid_id_token_returns_google_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    query, cookie = _start()
    state, nonce = query["state"][0], query["nonce"][0]
    fake = _FakeGoogle(_token_ok(_id_token(_claims(nonce))))
    _install(monkeypatch, fake)

    identity = exchange_callback(_request({"code": "auth-code", "state": state}, cookie))

    assert identity == GoogleIdentity(
        sub="google-sub-123", email="candidate@example.com", email_verified=True
    )
    sent = fake.token_requests[0]
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["auth-code"]
    assert sent["redirect_uri"] == [REDIRECT_URI]
    assert sent["client_id"] == [CLIENT_ID]
    assert sent["client_secret"] == [CLIENT_SECRET]
    assert sent["code_verifier"][0]


def test_auth_10_code_verifier_matches_the_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    from authlib.oauth2.rfc7636 import create_s256_code_challenge  # type: ignore[import-untyped]

    query, cookie = _start()
    fake = _FakeGoogle(_token_ok(_id_token(_claims(query["nonce"][0]))))
    _install(monkeypatch, fake)

    exchange_callback(_request({"code": "c", "state": query["state"][0]}, cookie))

    verifier = fake.token_requests[0]["code_verifier"][0]
    assert create_s256_code_challenge(verifier) == query["code_challenge"][0]


def test_auth_93_access_denied_raises_google_auth_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    query, cookie = _start()
    fake = _FakeGoogle(_token_ok("unused"))
    _install(monkeypatch, fake)

    with pytest.raises(AppError) as exc_info:
        exchange_callback(
            _request({"error": "access_denied", "state": query["state"][0]}, cookie)
        )

    _assert_google_failed(exc_info)
    assert fake.token_requests == []


def test_auth_93_divergent_state_raises_google_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, cookie = _start()
    fake = _FakeGoogle(_token_ok(_id_token(_claims(query["nonce"][0]))))
    _install(monkeypatch, fake)

    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"code": "c", "state": "attacker-state"}, cookie))

    _assert_google_failed(exc_info)
    assert fake.token_requests == []


def test_auth_93_missing_state_cookie_raises_google_auth_failed() -> None:
    query, _cookie = _start()
    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"code": "c", "state": query["state"][0]}))
    _assert_google_failed(exc_info)


def test_auth_93_expired_state_cookie_raises_google_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, cookie = _start()
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 601)
    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"code": "c", "state": query["state"][0]}, cookie))
    _assert_google_failed(exc_info)


def test_auth_93_cookie_signed_with_other_secret_is_rejected() -> None:
    query, _cookie = _start()
    forged = URLSafeTimedSerializer("other-secret", salt=google_oidc.STATE_COOKIE_SALT).dumps(
        {"state": query["state"][0], "nonce": "n", "code_verifier": "v" * 64}
    )
    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"code": "c", "state": query["state"][0]}, forged))
    _assert_google_failed(exc_info)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"email_verified": False}, id="email_not_verified"),
        pytest.param({"email_verified": "false"}, id="email_verified_string"),
        pytest.param({"nonce": "other-nonce"}, id="nonce_mismatch"),
        pytest.param({"aud": "someone-else"}, id="wrong_audience"),
        pytest.param({"iss": "https://evil.example.com"}, id="wrong_issuer"),
        pytest.param({"exp": int(time.time()) - 3600}, id="expired"),
        pytest.param({"email": ""}, id="empty_email"),
    ],
)
def test_auth_93_invalid_id_token_claims_raise_google_auth_failed(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    query, cookie = _start()
    claims = _claims(query["nonce"][0], **overrides)
    _install(monkeypatch, _FakeGoogle(_token_ok(_id_token(claims))))

    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"code": "c", "state": query["state"][0]}, cookie))

    _assert_google_failed(exc_info)


def test_auth_93_id_token_signed_by_unknown_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, cookie = _start()
    token = _id_token(_claims(query["nonce"][0]), key=OTHER_KEY)
    _install(monkeypatch, _FakeGoogle(_token_ok(token)))

    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"code": "c", "state": query["state"][0]}, cookie))

    _assert_google_failed(exc_info)


def _oct_key() -> OctKey:
    return OctKey.import_key("a-shared-secret-with-enough-length-0123456789")


def test_auth_93_hmac_signed_id_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    query, cookie = _start()
    token = jwt.encode({"alg": "HS256"}, _claims(query["nonce"][0]), _oct_key())
    _install(monkeypatch, _FakeGoogle(_token_ok(token)))

    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"code": "c", "state": query["state"][0]}, cookie))

    _assert_google_failed(exc_info)


@pytest.mark.parametrize(
    "token_response",
    [
        pytest.param(lambda: httpx.Response(400, json={"error": "invalid_grant"}), id="http_400"),
        pytest.param(lambda: httpx.Response(200, json={"access_token": "at"}), id="no_id_token"),
        pytest.param(lambda: httpx.Response(200, content=b"not json"), id="not_json"),
    ],
)
def test_auth_93_token_endpoint_failure_raises_google_auth_failed(
    monkeypatch: pytest.MonkeyPatch, token_response: Callable[[], httpx.Response]
) -> None:
    query, cookie = _start()
    _install(monkeypatch, _FakeGoogle(token_response))

    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"code": "c", "state": query["state"][0]}, cookie))

    _assert_google_failed(exc_info)


def test_auth_93_network_error_raises_google_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query, cookie = _start()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    monkeypatch.setattr(
        google_oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(boom))
    )
    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"code": "c", "state": query["state"][0]}, cookie))
    _assert_google_failed(exc_info)


def test_auth_93_missing_code_raises_google_auth_failed() -> None:
    query, cookie = _start()
    with pytest.raises(AppError) as exc_info:
        exchange_callback(_request({"state": query["state"][0]}, cookie))
    _assert_google_failed(exc_info)


def test_auth_93_google_not_configured_raises_google_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IR_GOOGLE_CLIENT_ID")
    get_settings.cache_clear()
    with pytest.raises(AppError) as exc_info:
        build_authorization_redirect(_request())
    _assert_google_failed(exc_info)


def test_auth_93_failures_never_log_the_client_secret_or_tokens(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    query, cookie = _start()
    token = _id_token(_claims(query["nonce"][0], email_verified=False))
    _install(monkeypatch, _FakeGoogle(_token_ok(token)))

    with caplog.at_level("DEBUG"), pytest.raises(AppError):
        exchange_callback(_request({"code": "secret-code", "state": query["state"][0]}, cookie))

    assert caplog.records
    for text in (CLIENT_SECRET, token, "secret-code", "candidate@example.com", cookie):
        assert text not in caplog.text


def test_clear_state_cookie_expires_the_cookie() -> None:
    from starlette.responses import RedirectResponse

    response = RedirectResponse("/login")
    clear_state_cookie(response)
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{STATE_COOKIE_NAME}=")
    assert "Max-Age=0" in set_cookie
