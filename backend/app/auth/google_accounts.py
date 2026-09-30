"""Google sign-in resolution and account linking (CT-18, AUTH-10..13, LAC-15, AS-1).

``resolve_google_login`` maps a validated Google identity to an account:

* ``google_sub`` already linked -> ``signed_in`` (AUTH-13);
* no account with the normalized e-mail -> a verified, password-less account linked to the
  identity is created and ``needs_terms`` is returned (AUTH-10);
* e-mail of a local account not yet linked -> nothing is linked; a ``google_link`` one-time
  token (payload ``google_sub``) is issued and ``needs_link`` is returned without a user, so no
  session can be opened before the local password is checked (AUTH-11, LAC-15).

Accounts pending deletion, accounts already linked to another Google identity and malformed
or unverified identities fail with ``GOOGLE_AUTH_FAILED``.

``complete_link`` links the identity only after the local password is verified (AUTH-12). A
wrong password counts on the same per-account throttle key as the local login; the failure is
committed before ``INVALID_CREDENTIALS`` is raised (the request session is rolled back on
errors) and the link token stays usable, so a typo does not force a new Google round trip.

Lock order is fixed: user row, then throttle row, then token row. Password reset locks user
then token and local login locks only throttle rows, so no path waits in the opposite order.

Callers own the transaction (CT-2) except for the committed failure above. The e-mail, the
Google subject, the password and the raw token are never logged.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import throttle
from app.auth.google_oidc import GoogleIdentity
from app.auth.passwords import verify_password
from app.auth.registration import normalize_email
from app.auth.tokens import consume_one_time_token, hash_secret, issue_one_time_token
from app.config import get_settings
from app.errors import (
    GOOGLE_AUTH_FAILED,
    INVALID_CREDENTIALS,
    LINK_INVALID,
    TOO_MANY_ATTEMPTS,
    AppError,
)
from app.models.account import OneTimeToken, TokenPurpose, User
from app.observability import log_event

GoogleLoginKind = Literal["signed_in", "needs_terms", "needs_link"]

# Google subjects are opaque ASCII identifiers (at most 255 chars, the column size).
_SUB_PATTERN = re.compile(r"[\x21-\x7e]{1,255}")
# Same shape the e-mail sender accepts; the column holds at most 320 chars.
_EMAIL_PATTERN = re.compile(r"[^\s@<>,;\"]+@[^\s@<>,;\"]+\.[^\s@<>,;\"]+")
_MAX_EMAIL_LENGTH = 254
# token_urlsafe(32) yields 43 base64url characters (CT-10).
_RAW_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,256}")
# LAC-32: string fields are capped at 1024 chars; longer passwords never reach Argon2.
_MAX_PASSWORD_LENGTH = 1024

_PAYLOAD_SUB_KEY = "google_sub"


@dataclass(frozen=True)
class GoogleLoginResult:
    kind: GoogleLoginKind
    user: User | None
    pending_link_token: str | None


def _google_failed(reason: str) -> AppError:
    log_event("auth.google_login_rejected", reason=reason)
    return AppError.from_catalog(GOOGLE_AUTH_FAILED)


def _is_encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_sub(value: object) -> str | None:
    if isinstance(value, str) and _SUB_PATTERN.fullmatch(value) is not None:
        return value
    return None


def _valid_email(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > _MAX_EMAIL_LENGTH * 2:
        return None
    normalized = normalize_email(value)
    if (
        not normalized
        or len(normalized) > _MAX_EMAIL_LENGTH
        or not _is_encodable(normalized)
        or any(not ch.isprintable() for ch in normalized)
        or _EMAIL_PATTERN.fullmatch(normalized) is None
    ):
        return None
    return normalized


def _user_by_sub(db: Session, sub: str) -> User | None:
    return db.execute(select(User).where(User.google_sub == sub)).scalar_one_or_none()


def _user_by_email(db: Session, email: str) -> User | None:
    return db.execute(select(User).where(User.email_normalized == email)).scalar_one_or_none()


def _signed_in(user: User) -> GoogleLoginResult:
    if user.deletion_requested_at is not None:
        raise _google_failed("pending_deletion")
    log_event("auth.google_login_resolved", kind="signed_in")
    return GoogleLoginResult(kind="signed_in", user=user, pending_link_token=None)


def _create_google_account(db: Session, sub: str, email: str) -> User | None:
    """Create the verified Google account; ``None`` if a concurrent request won the race."""
    user = User(
        email_normalized=email,
        password_hash=None,
        email_verified_at=datetime.now(UTC),
        google_sub=sub,
    )
    try:
        with db.begin_nested():
            db.add(user)
            db.flush()
    except IntegrityError:
        return None
    return user


def _resolve(db: Session, sub: str, email: str, *, retry: bool) -> GoogleLoginResult:
    linked = _user_by_sub(db, sub)
    if linked is not None:
        return _signed_in(linked)

    existing = _user_by_email(db, email)
    if existing is None:
        created = _create_google_account(db, sub, email)
        if created is None:
            if not retry:
                raise _google_failed("concurrent_conflict")
            # A concurrent sign-in or registration committed the same sub or e-mail.
            return _resolve(db, sub, email, retry=False)
        log_event("auth.google_login_resolved", kind="needs_terms")
        return GoogleLoginResult(kind="needs_terms", user=created, pending_link_token=None)

    if existing.deletion_requested_at is not None:
        raise _google_failed("pending_deletion")
    if existing.google_sub is not None:
        raise _google_failed("linked_to_other_identity")
    if existing.password_hash is None:
        raise _google_failed("account_not_linkable")

    # LAC-15: never link by e-mail alone; the local password must be confirmed first.
    ttl = timedelta(minutes=get_settings().google_link_ttl_minutes)
    raw = issue_one_time_token(
        db, existing.id, TokenPurpose.GOOGLE_LINK, ttl, {_PAYLOAD_SUB_KEY: sub}
    )
    log_event("auth.google_login_resolved", kind="needs_link")
    return GoogleLoginResult(kind="needs_link", user=None, pending_link_token=raw)


def resolve_google_login(db: Session, identity: GoogleIdentity) -> GoogleLoginResult:
    """Map a Google identity to an account (see module docstring).

    Raises ``AppError(GOOGLE_AUTH_FAILED)`` for malformed or unverified identities, accounts
    pending deletion and e-mails of accounts linked to another Google identity.
    """
    if not isinstance(identity, GoogleIdentity) or identity.email_verified is not True:
        raise _google_failed("unverified_identity")
    sub = _valid_sub(identity.sub)
    email = _valid_email(identity.email)
    if sub is None or email is None:
        raise _google_failed("malformed_identity")
    return _resolve(db, sub, email, retry=True)


def _link_invalid(reason: str) -> AppError:
    log_event("auth.google_link_rejected", reason=reason)
    return AppError.from_catalog(LINK_INVALID)


def _lock_user(db: Session, user_id: UUID) -> User | None:
    return db.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _lock_token_owner(db: Session, raw_token: str) -> User | None:
    owner_id = db.execute(
        select(OneTimeToken.user_id).where(OneTimeToken.token_hash == hash_secret(raw_token))
    ).scalar_one_or_none()
    return _lock_user(db, owner_id) if owner_id is not None else None


def _password_usable(password: object) -> bool:
    return (
        isinstance(password, str)
        and 0 < len(password) <= _MAX_PASSWORD_LENGTH
        and _is_encodable(password)
    )


def complete_link(db: Session, pending_link_token: str, password: str) -> User:
    """Link the Google identity of a ``google_link`` token after checking the local password.

    Raises ``AppError`` with ``LINK_INVALID`` (unknown, malformed, expired, used or
    other-purpose token, or an account that can no longer be linked), ``TOO_MANY_ATTEMPTS``
    (account throttle locked, checked before the password) or ``INVALID_CREDENTIALS``.
    """
    if (
        not isinstance(pending_link_token, str)
        or _RAW_TOKEN_PATTERN.fullmatch(pending_link_token) is None
    ):
        raise _link_invalid("malformed")

    user = _lock_token_owner(db, pending_link_token)
    if user is None:
        raise _link_invalid("not_found")

    entries = throttle.acquire(db, [throttle.account_key(user.email_normalized)])
    if throttle.is_locked(entries):
        log_event("auth.google_link_rejected", reason="locked")
        raise AppError.from_catalog(TOO_MANY_ATTEMPTS)

    # The token is consumed inside a savepoint so a wrong password can give it back.
    attempt = db.begin_nested()
    try:
        token = consume_one_time_token(db, pending_link_token, TokenPurpose.GOOGLE_LINK)
    except AppError:
        attempt.rollback()
        raise
    sub = _valid_sub((token.payload or {}).get(_PAYLOAD_SUB_KEY))
    if token.user_id != user.id or sub is None:
        attempt.rollback()
        raise _link_invalid("bad_payload")
    if (
        user.deletion_requested_at is not None
        or user.password_hash is None
        or user.google_sub is not None
    ):
        attempt.rollback()
        raise _link_invalid("account_not_linkable")

    stored_hash = user.password_hash
    if not _password_usable(password) or not verify_password(stored_hash, password):
        attempt.rollback()
        throttle.record_failure(db, entries)
        db.commit()
        log_event("auth.google_link_rejected", reason="invalid_credentials")
        raise AppError.from_catalog(INVALID_CREDENTIALS)

    owner = _user_by_sub(db, sub)
    if owner is not None:
        attempt.rollback()
        raise _link_invalid("identity_linked_elsewhere")

    user.google_sub = sub
    try:
        db.flush()
    except IntegrityError:
        # A concurrent request linked the same Google identity to another account.
        attempt.rollback()
        raise _link_invalid("identity_linked_elsewhere") from None
    attempt.commit()

    throttle.clear(db, entries[throttle.account_key(user.email_normalized)])
    log_event("auth.google_link_completed")
    return user
