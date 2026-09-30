"""Local registration and e-mail verification (CT-14, AS-1).

Covers AUTH-01, AUTH-02, AUTH-03, AUTH-91, AUTH-92, AUTH-94 and DATA-07.

Callers own the transaction (CT-2): these functions flush but never commit. Registering an
e-mail that already belongs to an account (local or Google, or pending deletion) creates
nothing and returns the same neutral result as a new registration whose e-mail was sent, so
the response never reveals whether an account exists. Registration never opens a session.
The one-time token and the link are only handed to ``send_email``; they are never logged.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password, password_policy_violations
from app.auth.tokens import consume_one_time_token, issue_one_time_token
from app.config import get_settings
from app.email.sender import send_email, verification_email
from app.errors import (
    LINK_INVALID,
    PASSWORD_POLICY,
    TERMS_NOT_ACCEPTED,
    VALIDATION_ERROR,
    AppError,
)
from app.legal.consent import record_consent
from app.models.account import TokenPurpose, User
from app.observability import log_event

EmailDispatch = Literal["sent", "delayed", "skipped"]

# Matches the `users.email_normalized` column size.
MAX_EMAIL_LENGTH = 320

# Result returned for an e-mail that already has an account: identical to a new
# registration whose verification e-mail was sent (AUTH-02).
_NEUTRAL_RESULT: EmailDispatch = "sent"


def normalize_email(raw: str) -> str:
    """Return the canonical form of an e-mail: surrounding whitespace removed, lower case."""
    return raw.strip().lower()


def _is_encodable(value: str) -> bool:
    # Lone surrogates cannot be encoded: they would crash hashing or the database driver.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_plausible_email(normalized: str) -> bool:
    if not normalized or len(normalized) > MAX_EMAIL_LENGTH or not _is_encodable(normalized):
        return False
    if any(ch.isspace() or not ch.isprintable() for ch in normalized):
        return False
    local, sep, domain = normalized.partition("@")
    return bool(sep) and bool(local) and bool(domain) and "@" not in domain


def _validation_error(field: str) -> AppError:
    return AppError.from_catalog(
        VALIDATION_ERROR,
        details={
            "fields": [{"loc": ["body", field], "msg": "Invalid value.", "type": "value_error"}]
        },
    )


def _verification_link(raw_token: str) -> str:
    base = get_settings().frontend_base_url.rstrip("/")
    return f"{base}/verify-email?token={raw_token}"


def _send_verification(db: Session, user: User) -> EmailDispatch:
    ttl = timedelta(minutes=get_settings().verification_ttl_minutes)
    raw_token = issue_one_time_token(db, user.id, TokenPurpose.VERIFY_EMAIL, ttl)
    delivered = send_email(user.email_normalized, verification_email(_verification_link(raw_token)))
    return "sent" if delivered else "delayed"


def _find_user(db: Session, normalized_email: str) -> User | None:
    return db.execute(
        select(User).where(User.email_normalized == normalized_email)
    ).scalar_one_or_none()


def register_local(db: Session, email: str, password: str, accepted_terms: bool) -> EmailDispatch:
    """Create an unverified local account and send its verification link.

    Raises ``AppError`` with ``VALIDATION_ERROR`` (malformed e-mail or password that cannot be
    encoded), ``TERMS_NOT_ACCEPTED`` or ``PASSWORD_POLICY`` (``details.violations``). Input is
    validated before looking the e-mail up, so errors never reveal whether an account exists.
    """
    normalized = normalize_email(email)
    if not _is_plausible_email(normalized):
        raise _validation_error("email")
    if accepted_terms is not True:
        raise AppError.from_catalog(TERMS_NOT_ACCEPTED)
    if not _is_encodable(password):
        raise _validation_error("password")
    violations = password_policy_violations(password)
    if violations:
        raise AppError.from_catalog(PASSWORD_POLICY, details={"violations": violations})

    # Hash before the lookup so new and existing e-mails cost roughly the same time.
    password_hash = hash_password(password)

    if _find_user(db, normalized) is not None:
        log_event("auth.registration_existing_email")
        return _NEUTRAL_RESULT

    user = User(email_normalized=normalized, password_hash=password_hash)
    try:
        with db.begin_nested():
            db.add(user)
            db.flush()
    except IntegrityError:
        # A concurrent registration won the UNIQUE(email_normalized) race (AUTH-91).
        log_event("auth.registration_existing_email", reason="concurrent")
        return _NEUTRAL_RESULT

    record_consent(db, user)
    dispatch = _send_verification(db, user)
    log_event("auth.registration_created", email_delivery=dispatch)
    return dispatch


def resend_verification(db: Session, email: str) -> EmailDispatch:
    """Send a new verification link, only to an existing unverified local account.

    Returns ``"skipped"`` for any other case (unknown e-mail, verified, Google-only or pending
    deletion); the API layer must answer all cases with the same neutral message.
    """
    normalized = normalize_email(email)
    if not _is_plausible_email(normalized):
        return "skipped"
    user = _find_user(db, normalized)
    if (
        user is None
        or user.password_hash is None
        or user.email_verified_at is not None
        or user.deletion_requested_at is not None
    ):
        return "skipped"
    dispatch = _send_verification(db, user)
    log_event("auth.verification_resent", email_delivery=dispatch)
    return dispatch


def verify_email(db: Session, raw_token: str) -> None:
    """Consume a verification link and mark its account as verified.

    Unknown, malformed, expired, already used or other-purpose tokens raise
    ``AppError(LINK_INVALID)`` (AUTH-94).
    """
    if not isinstance(raw_token, str):
        raise AppError.from_catalog(LINK_INVALID)
    token = consume_one_time_token(db, raw_token, TokenPurpose.VERIFY_EMAIL)
    user = db.get(User, token.user_id)
    if user is None:
        raise AppError.from_catalog(LINK_INVALID)
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)
        db.flush()
    log_event("auth.email_verified")
