"""Local e-mail and password login (CT-15, AUTH-04, AUTH-05, AUTH-90, AS-1).

Unknown e-mails, wrong passwords, Google-only accounts and accounts pending deletion all fail
with the same ``INVALID_CREDENTIALS`` error after the same work: one Argon2id verification
(against a dummy hash when there is no real one) and one failure recorded on the account and
origin throttle keys. The password, the e-mail and the client key are never logged.

Transactions: failed attempts are committed here before ``INVALID_CREDENTIALS`` is raised,
because the request session is rolled back on errors and the failure count must survive it
(AUTH-90). On success the caller commits (together with the new session, CT-11).
"""

import secrets
from functools import lru_cache

from sqlalchemy.orm import Session

from app.auth import throttle
from app.auth.passwords import hash_password, verify_password
from app.auth.registration import _find_user, _is_encodable, _is_plausible_email, normalize_email
from app.errors import EMAIL_NOT_VERIFIED, INVALID_CREDENTIALS, TOO_MANY_ATTEMPTS, AppError
from app.models.account import User
from app.observability import log_event


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    # Same Argon2id parameters as real hashes, so a missing account costs the same time.
    return hash_password(secrets.token_urlsafe(32))


def _password_matches(user: User | None, password: object) -> bool:
    usable = isinstance(password, str) and _is_encodable(password)
    candidate = password if usable and isinstance(password, str) else ""
    has_hash = user is not None and user.password_hash is not None
    stored = user.password_hash if user is not None and user.password_hash else _dummy_hash()
    matches = verify_password(stored, candidate)
    return usable and has_hash and matches


def login_local(db: Session, email: str, password: str, client_key: str) -> User:
    """Authenticate a local account by e-mail and password.

    Returns the ``User`` on success; the caller then opens the session. Raises ``AppError``
    with ``TOO_MANY_ATTEMPTS`` (account or origin locked, checked before the password),
    ``INVALID_CREDENTIALS`` or ``EMAIL_NOT_VERIFIED`` (correct password, unverified account).
    """
    normalized = normalize_email(email) if isinstance(email, str) else ""
    account = throttle.account_key(normalized)
    origin = throttle.origin_key(client_key if isinstance(client_key, str) else "")
    entries = throttle.acquire(db, [account, origin])
    if throttle.is_locked(entries):
        log_event("auth.login_rejected", reason="locked")
        raise AppError.from_catalog(TOO_MANY_ATTEMPTS)

    user = _find_user(db, normalized) if _is_plausible_email(normalized) else None
    if (
        not _password_matches(user, password)
        or user is None
        or (user.deletion_requested_at is not None)
    ):
        throttle.record_failure(db, entries)
        db.commit()
        log_event("auth.login_rejected", reason="invalid_credentials")
        raise AppError.from_catalog(INVALID_CREDENTIALS)

    if user.email_verified_at is None:
        log_event("auth.login_rejected", reason="email_not_verified")
        raise AppError.from_catalog(EMAIL_NOT_VERIFIED)

    # Only the account counter is reset: resetting the origin counter would let one valid
    # account be used to keep guessing other accounts' passwords from the same origin.
    throttle.clear(db, entries[account])
    log_event("auth.login_succeeded")
    return user
