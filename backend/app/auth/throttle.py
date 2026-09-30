"""Login throttling per account and per origin (AUTH-90, DA-5, AS-1).

Two independent counters guard every local login attempt: one per account
(``account:<hmac(email_normalized)>``) and one per origin (``ip:<hmac(client_key)>``). Both
are keyed HMAC-SHA256 digests with ``IR_THROTTLE_SECRET``, so the table never holds an
e-mail or an IP address and the keys cannot be reversed by dictionary. Unknown e-mails get a
counter like any other, so locking never reveals whether an account exists.

``login_max_failures`` failures inside a window of ``login_lock_minutes`` lock the key for
``login_lock_minutes``. Rows are locked (``SELECT ... FOR UPDATE``, in key order) for the
whole attempt, so concurrent attempts on the same key are serialized and cannot overshoot
the limit. Nothing here commits.
"""

import hashlib
import hmac
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.account import LoginThrottle

ACCOUNT_PREFIX = "account:"
ORIGIN_PREFIX = "ip:"


def _now() -> datetime:
    """Current time; a seam so tests can simulate the clock."""
    return datetime.now(UTC)


def _digest(context: str, value: str) -> str:
    secret = get_settings().throttle_secret.get_secret_value().encode("utf-8")
    # surrogatepass: arbitrary client input must never raise while building a key.
    message = f"{context}\x00{value}".encode("utf-8", "surrogatepass")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def account_key(email_normalized: str) -> str:
    """Throttle key of an account, from its normalized e-mail (existing or not)."""
    return ACCOUNT_PREFIX + _digest("account", email_normalized)


def origin_key(client_key: str) -> str:
    """Throttle key of a request origin (e.g. the client IP address)."""
    return ORIGIN_PREFIX + _digest("ip", client_key)


def _window() -> timedelta:
    return timedelta(minutes=get_settings().login_lock_minutes)


def acquire(db: Session, keys: Sequence[str]) -> dict[str, LoginThrottle]:
    """Lock and return the throttle rows of ``keys``, creating missing ones.

    Expired locks and windows are restarted here, so callers only see live counters.
    """
    now = _now()
    ordered = sorted(set(keys))
    db.execute(
        insert(LoginThrottle)
        .values([{"key": key, "failures": 0, "window_started_at": now} for key in ordered])
        .on_conflict_do_nothing(index_elements=[LoginThrottle.key])
    )
    rows = (
        db.execute(
            select(LoginThrottle)
            .where(LoginThrottle.key.in_(ordered))
            .order_by(LoginThrottle.key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    window = _window()
    for row in rows:
        lock_over = row.locked_until is not None and row.locked_until <= now
        window_over = row.locked_until is None and row.window_started_at + window <= now
        if lock_over or window_over:
            _restart(row, now)
    db.flush()
    return {row.key: row for row in rows}


def is_locked(entries: dict[str, LoginThrottle]) -> bool:
    """True when any of the acquired keys is currently locked."""
    now = _now()
    return any(row.locked_until is not None and row.locked_until > now for row in entries.values())


def record_failure(db: Session, entries: dict[str, LoginThrottle]) -> None:
    """Count one failed attempt on every acquired key, locking those that reach the limit."""
    now = _now()
    settings = get_settings()
    for row in entries.values():
        row.failures += 1
        if row.failures >= settings.login_max_failures:
            row.locked_until = now + _window()
    db.flush()


def clear(db: Session, entry: LoginThrottle) -> None:
    """Reset the counter of one key (after a successful login)."""
    _restart(entry, _now())
    db.flush()


def _restart(row: LoginThrottle, now: datetime) -> None:
    row.failures = 0
    row.window_started_at = now
    row.locked_until = None
