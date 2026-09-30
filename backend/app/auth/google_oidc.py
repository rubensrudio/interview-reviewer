"""Google sign-in client: OpenID Connect code flow with PKCE, state and nonce (DA-6, CT-17).

The start step keeps ``state``, ``nonce`` and the PKCE ``code_verifier`` in a short-lived,
signed, HttpOnly cookie. The callback step checks that cookie against the query, exchanges
the code at Google's token endpoint and validates the ``id_token`` (signature against
Google's JWKS, ``iss``, ``aud``, ``exp`` and ``nonce``).

Both functions are synchronous: routes run in FastAPI's worker threads (DA-3).
Every failure raises ``GOOGLE_AUTH_FAILED``. Logs carry only a failure reason: never the
client secret, codes, tokens, cookies or the user's e-mail.
"""

import hmac
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.common.security import generate_token  # type: ignore[import-untyped]
from authlib.oauth2.rfc7636 import create_s256_code_challenge  # type: ignore[import-untyped]
from itsdangerous import BadSignature, URLSafeTimedSerializer
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.config import Settings, get_settings
from app.errors import GOOGLE_AUTH_FAILED, AppError

logger = logging.getLogger(__name__)

GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL, not a secret
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
GOOGLE_SCOPES = "openid email profile"
ID_TOKEN_ALGORITHMS = ["RS256"]

STATE_COOKIE_NAME = "ir_google_oidc"
STATE_COOKIE_SALT = "google-oidc-state"
STATE_MAX_AGE_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 10.0
CLOCK_LEEWAY_SECONDS = 60


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    email_verified: bool


def _http_client() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)


def _fail(reason: str) -> AppError:
    logger.warning("Google sign-in failed: %s", reason)
    return AppError.from_catalog(GOOGLE_AUTH_FAILED)


def _client_credentials(settings: Settings) -> tuple[str, str]:
    client_id = settings.google_client_id
    secret = settings.google_client_secret
    if not client_id or secret is None or not secret.get_secret_value():
        raise _fail("client not configured")
    return client_id, secret.get_secret_value()


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        settings.oidc_state_secret.get_secret_value(), salt=STATE_COOKIE_SALT
    )


def build_authorization_redirect(request: Request) -> RedirectResponse:
    """Redirect to Google's consent screen and set the signed state cookie."""
    settings = get_settings()
    client_id, _secret = _client_credentials(settings)

    state = generate_token(32)
    nonce = generate_token(32)
    code_verifier = generate_token(64)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": settings.google_redirect_uri,
        "scope": GOOGLE_SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": create_s256_code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(
        f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(params)}", status_code=302
    )
    cookie_value = _serializer(settings).dumps(
        {"state": state, "nonce": nonce, "code_verifier": code_verifier}
    )
    response.set_cookie(
        STATE_COOKIE_NAME,
        cookie_value,
        max_age=STATE_MAX_AGE_SECONDS,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


def clear_state_cookie(response: Response) -> None:
    """Expire the state cookie; call on the response that ends the callback."""
    settings = get_settings()
    response.delete_cookie(
        STATE_COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax"
    )


def _load_state(request: Request, settings: Settings) -> dict[str, str]:
    raw = request.cookies.get(STATE_COOKIE_NAME)
    if not raw:
        raise _fail("state cookie missing")
    try:
        data = _serializer(settings).loads(raw, max_age=STATE_MAX_AGE_SECONDS)
    except BadSignature:
        # SignatureExpired is a subclass of BadSignature.
        raise _fail("state cookie invalid or expired") from None
    keys = ("state", "nonce", "code_verifier")
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) and data[key] for key in keys
    ):
        raise _fail("state cookie malformed")
    return {key: data[key] for key in keys}


def _fetch_id_token(
    client: httpx.Client, code: str, code_verifier: str, settings: Settings
) -> str:
    client_id, client_secret = _client_credentials(settings)
    try:
        response = client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.google_redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError:
        raise _fail("token endpoint unreachable") from None
    if response.status_code != 200:
        raise _fail(f"token endpoint returned HTTP {response.status_code}")
    try:
        payload: Any = response.json()
    except ValueError:
        raise _fail("token endpoint returned invalid JSON") from None
    id_token = payload.get("id_token") if isinstance(payload, dict) else None
    if not isinstance(id_token, str) or not id_token:
        raise _fail("token response without id_token")
    return id_token


def _fetch_jwks(client: httpx.Client) -> KeySet:
    try:
        response = client.get(GOOGLE_JWKS_URI, headers={"Accept": "application/json"})
        response.raise_for_status()
        return KeySet.import_key_set(response.json())
    except (httpx.HTTPError, ValueError, JoseError):
        raise _fail("JWKS unavailable") from None


def _validate_id_token(
    id_token: str, jwks: KeySet, client_id: str, nonce: str
) -> dict[str, Any]:
    try:
        token = jwt.decode(id_token, jwks, algorithms=ID_TOKEN_ALGORITHMS)
        registry = jwt.JWTClaimsRegistry(
            leeway=CLOCK_LEEWAY_SECONDS,
            iss={"essential": True, "values": GOOGLE_ISSUERS},
            aud={"essential": True, "value": client_id},
            exp={"essential": True},
            sub={"essential": True},
            nonce={"essential": True, "value": nonce},
        )
        registry.validate(token.claims)
    except (JoseError, ValueError):
        raise _fail("id_token rejected") from None
    return dict(token.claims)


def _identity_from_claims(claims: dict[str, Any]) -> GoogleIdentity:
    sub = claims.get("sub")
    email = claims.get("email")
    if not isinstance(sub, str) or not sub:
        raise _fail("id_token without subject")
    if not isinstance(email, str) or not email:
        raise _fail("id_token without e-mail")
    if claims.get("email_verified") is not True:
        raise _fail("e-mail not verified by Google")
    return GoogleIdentity(sub=sub, email=email, email_verified=True)


def exchange_callback(request: Request) -> GoogleIdentity:
    """Validate the callback, exchange the code and return the verified Google identity."""
    settings = get_settings()
    client_id, _secret = _client_credentials(settings)
    query = request.query_params

    if "error" in query:
        raise _fail("authorization denied or failed at Google")
    stored = _load_state(request, settings)
    received_state = query.get("state", "")
    if not hmac.compare_digest(received_state.encode(), stored["state"].encode()):
        raise _fail("state mismatch")
    code = query.get("code", "")
    if not code:
        raise _fail("authorization code missing")

    with _http_client() as client:
        id_token = _fetch_id_token(client, code, stored["code_verifier"], settings)
        jwks = _fetch_jwks(client)
    claims = _validate_id_token(id_token, jwks, client_id, stored["nonce"])
    return _identity_from_claims(claims)
