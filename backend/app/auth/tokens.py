"""Secret tokens and one-time links (CT-10).

Raw secrets are only ever returned to the caller (to be sent by e-mail or set in a cookie);
the database stores their SHA-256 hex digest. Callers own the transaction (CT-2): these
functions flush but never commit.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import LINK_INVALID, AppError
from app.models.account import OneTimeToken, TokenPurpose
from app.observability import log_event

_SECRET_BYTES = 32
# token_urlsafe(32) yields 43 characters; anything far longer is not one of ours.
_MAX_RAW_LENGTH = 256


def new_secret() -> str:
    """Return a new URL-safe random secret with 256 bits of entropy."""
    return secrets.token_urlsafe(_SECRET_BYTES)


def hash_secret(raw: str) -> str:
    """Return the SHA-256 hex digest (64 chars) persisted in place of the raw secret."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_one_time_token(
    db: Session,
    user_id: UUID,
    purpose: TokenPurpose,
    ttl: timedelta,
    payload: dict[str, str] | None = None,
) -> str:
    """Persist a new one-time token for `user_id` and return its raw value."""
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")
    raw = new_secret()
    db.add(
        OneTimeToken(
            user_id=user_id,
            purpose=purpose,
            token_hash=hash_secret(raw),
            payload=dict(payload) if payload is not None else None,
            expires_at=datetime.now(UTC) + ttl,
        )
    )
    db.flush()
    log_event("auth.one_time_token_issued", purpose=purpose.value)
    return raw


def _link_invalid(reason: str, purpose: TokenPurpose) -> AppError:
    log_event("auth.one_time_token_rejected", purpose=purpose.value, reason=reason)
    return AppError.from_catalog(LINK_INVALID)


def consume_one_time_token(db: Session, raw: str, purpose: TokenPurpose) -> OneTimeToken:
    """Mark the token as used and return it.

    The row is locked with ``SELECT ... FOR UPDATE`` so two concurrent requests cannot both
    consume it. Unknown, expired, already used or other-purpose tokens raise
    ``AppError(LINK_INVALID)`` without telling the caller which case applied.
    """
    if not raw or len(raw) > _MAX_RAW_LENGTH:
        raise _link_invalid("malformed", purpose)

    token = db.execute(
        select(OneTimeToken)
        .where(OneTimeToken.token_hash == hash_secret(raw))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    if token is None:
        raise _link_invalid("not_found", purpose)
    if token.purpose != purpose:
        raise _link_invalid("wrong_purpose", purpose)
    if token.used_at is not None:
        raise _link_invalid("already_used", purpose)
    now = datetime.now(UTC)
    if token.expires_at <= now:
        raise _link_invalid("expired", purpose)

    token.used_at = now
    db.flush()
    log_event("auth.one_time_token_consumed", purpose=purpose.value)
    return token
