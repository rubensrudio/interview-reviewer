"""Password reset flow (CT-16, AUTH-07, AUTH-08, AUTH-09, AUTH-94, AS-1).

``request_password_reset`` always returns ``None`` whatever the e-mail, so the API can answer
every case with the same neutral message. A local account receives a single-use reset link
(TTL ``reset_ttl_minutes``); a Google-only account receives an e-mail telling it to sign in
with Google, and no password is ever created for it; unknown e-mails, malformed input and
accounts pending deletion get nothing.

``reset_password`` checks the new password against the policy before touching the link, so a
rejected password never burns it. On success it replaces the hash, consumes the link,
invalidates any other outstanding reset link of the account and revokes all its sessions.

Callers own the transaction (CT-2): these functions flush but never commit. The e-mail, the
password, the raw token and the link are never logged.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password, password_policy_violations
from app.auth.registration import normalize_email
from app.auth.sessions import revoke_all_sessions
from app.auth.tokens import consume_one_time_token, hash_secret, issue_one_time_token
from app.config import get_settings
from app.email.sender import google_only_account_email, password_reset_email, send_email
from app.errors import LINK_INVALID, PASSWORD_POLICY, VALIDATION_ERROR, AppError
from app.models.account import OneTimeToken, TokenPurpose, User
from app.observability import log_event

# Matches the `users.email_normalized` column size.
_MAX_EMAIL_LENGTH = 320


def _is_encodable(value: str) -> bool:
    # Lone surrogates cannot be encoded: they would crash hashing or the database driver.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _lookup_email(email: object) -> str | None:
    """Return the normalized e-mail if it can be safely looked up, else ``None``."""
    if not isinstance(email, str):
        return None
    normalized = normalize_email(email)
    if not normalized or len(normalized) > _MAX_EMAIL_LENGTH or not _is_encodable(normalized):
        return None
    # NUL is rejected by PostgreSQL text columns and control characters are never part of a
    # stored address: both would only turn a neutral answer into a server error.
    if any(not ch.isprintable() for ch in normalized):
        return None
    return normalized


def _find_user(db: Session, normalized_email: str) -> User | None:
    return db.execute(
        select(User).where(User.email_normalized == normalized_email)
    ).scalar_one_or_none()


def _reset_link(raw_token: str) -> str:
    base = get_settings().frontend_base_url.rstrip("/")
    return f"{base}/reset-password?token={raw_token}"


def request_password_reset(db: Session, email: str) -> None:
    """Send a reset link (local account) or Google guidance (Google-only account).

    Returns ``None`` in every case, including unknown e-mails and malformed input, so the
    caller cannot reveal whether an account exists (AUTH-07).
    """
    normalized = _lookup_email(email)
    user = _find_user(db, normalized) if normalized is not None else None

    if user is None or user.deletion_requested_at is not None:
        log_event("auth.password_reset_requested", outcome="skipped")
        return

    if user.password_hash is not None:
        ttl = timedelta(minutes=get_settings().reset_ttl_minutes)
        raw_token = issue_one_time_token(db, user.id, TokenPurpose.RESET_PASSWORD, ttl)
        delivered = send_email(user.email_normalized, password_reset_email(_reset_link(raw_token)))
        log_event("auth.password_reset_requested", outcome="link", delivered=delivered)
        return

    if user.google_sub is not None:
        # AUTH-09: never create a local password; point the user to Google instead.
        delivered = send_email(user.email_normalized, google_only_account_email())
        log_event("auth.password_reset_requested", outcome="google_only", delivered=delivered)
        return

    log_event("auth.password_reset_requested", outcome="skipped")


def _validate_new_password(new_password: object) -> str:
    if not isinstance(new_password, str) or not _is_encodable(new_password):
        raise AppError.from_catalog(
            VALIDATION_ERROR,
            details={
                "fields": [
                    {
                        "loc": ["body", "new_password"],
                        "msg": "Invalid value.",
                        "type": "value_error",
                    }
                ]
            },
        )
    violations = password_policy_violations(new_password)
    if violations:
        raise AppError.from_catalog(PASSWORD_POLICY, details={"violations": violations})
    return new_password


def _invalidate_other_reset_links(db: Session, user: User) -> None:
    db.execute(
        update(OneTimeToken)
        .where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.purpose == TokenPurpose.RESET_PASSWORD,
            OneTimeToken.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
    )


def _lock_user(db: Session, user_id: UUID) -> User | None:
    return db.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _lock_token_owner(db: Session, raw_token: str) -> None:
    """Lock the account that owns ``raw_token`` (if any) before the token itself is locked."""
    if not _is_encodable(raw_token):
        return  # consume_one_time_token rejects it as malformed
    owner_id = db.execute(
        select(OneTimeToken.user_id).where(OneTimeToken.token_hash == hash_secret(raw_token))
    ).scalar_one_or_none()
    if owner_id is not None:
        _lock_user(db, owner_id)


def reset_password(db: Session, raw_token: str, new_password: str) -> None:
    """Replace the password of the account that owns a valid reset link.

    Raises ``AppError`` with ``VALIDATION_ERROR`` (password not a valid string),
    ``PASSWORD_POLICY`` (``details.violations``; the link stays usable) or ``LINK_INVALID``
    (unknown, malformed, expired, already used or other-purpose link, or an account that has
    no local password or is pending deletion; AUTH-94).
    """
    password = _validate_new_password(new_password)
    if not isinstance(raw_token, str):
        raise AppError.from_catalog(LINK_INVALID)

    # Lock order is always user, then tokens: concurrent resets of the same account (with
    # different links) serialize on the user row instead of deadlocking on each other's token.
    _lock_token_owner(db, raw_token)
    token = consume_one_time_token(db, raw_token, TokenPurpose.RESET_PASSWORD)
    user = _lock_user(db, token.user_id)
    if user is None or user.password_hash is None or user.deletion_requested_at is not None:
        # AUTH-09: a Google-only account never gets a local password through this flow.
        log_event("auth.password_reset_rejected", reason="account_not_eligible")
        raise AppError.from_catalog(LINK_INVALID)

    user.password_hash = hash_password(password)
    _invalidate_other_reset_links(db, user)
    db.flush()
    revoke_all_sessions(db, user.id)
    log_event("auth.password_reset_completed")
