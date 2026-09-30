"""Integration tests for local login and login throttling (CT-15).

Covers AUTH-04, AUTH-05 and AUTH-90.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import login, throttle
from app.auth.login import login_local
from app.auth.passwords import hash_password
from app.config import get_settings
from app.errors import (
    EMAIL_NOT_VERIFIED,
    INVALID_CREDENTIALS,
    TOO_MANY_ATTEMPTS,
    AppError,
)
from app.models.account import LoginThrottle, User

GOOD_PASSWORD = "correct horse battery staple 42"  # noqa: S105 - test fixture value
WRONG_PASSWORD = "wrong horse battery staple 42"  # noqa: S105 - test fixture value
CLIENT = "203.0.113.7"


class FakeClock:
    """Controllable replacement for ``throttle._now``."""

    def __init__(self) -> None:
        self.current = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(throttle, "_now", fake)
    return fake


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.com"


def _unique_client() -> str:
    return f"198.51.100.{uuid.uuid4().int % 250}-{uuid.uuid4().hex[:8]}"


def _make_user(db: Session, *, verified: bool = True, password: str | None = GOOD_PASSWORD) -> User:
    user = User(
        email_normalized=_unique_email(),
        password_hash=hash_password(password) if password is not None else None,
        email_verified_at=datetime.now(UTC) if verified else None,
    )
    db.add(user)
    db.flush()
    return user


def _login_error(db: Session, email: str, password: str, client: str) -> AppError:
    with pytest.raises(AppError) as info:
        login_local(db, email, password, client)
    return info.value


# --- AUTH-05: success and generic failure ---------------------------------------------------


def test_auth_05_verified_account_with_correct_password_returns_user(
    db: Session, clock: FakeClock
) -> None:
    user = _make_user(db)

    assert login_local(db, user.email_normalized, GOOD_PASSWORD, _unique_client()) is user


def test_auth_05_email_is_normalized_before_lookup(db: Session, clock: FakeClock) -> None:
    user = _make_user(db)

    assert login_local(db, f"  {user.email_normalized.upper()} ", GOOD_PASSWORD, CLIENT) is user


def test_auth_05_unknown_email_and_wrong_password_raise_same_error(
    db: Session, clock: FakeClock
) -> None:
    user = _make_user(db)

    unknown = _login_error(db, _unique_email(), GOOD_PASSWORD, _unique_client())
    wrong = _login_error(db, user.email_normalized, WRONG_PASSWORD, _unique_client())

    assert unknown.code == wrong.code == INVALID_CREDENTIALS
    assert unknown.message == wrong.message == "Invalid e-mail or password."
    assert unknown.status == wrong.status
    assert unknown.details == wrong.details


def test_auth_05_unknown_email_still_verifies_a_dummy_hash(
    db: Session, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_verify = login.verify_password

    def spy(stored_hash: str, raw: str) -> bool:
        calls.append(stored_hash)
        return real_verify(stored_hash, raw)

    monkeypatch.setattr(login, "verify_password", spy)

    _login_error(db, _unique_email(), GOOD_PASSWORD, _unique_client())

    assert len(calls) == 1
    assert calls[0].startswith("$argon2id$")


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("not-an-email", GOOD_PASSWORD),
        ("", GOOD_PASSWORD),
        ("a\ud800@example.com", GOOD_PASSWORD),
        (None, GOOD_PASSWORD),
    ],
)
def test_auth_05_malformed_email_raises_invalid_credentials(
    db: Session, clock: FakeClock, email: object, password: str
) -> None:
    error = _login_error(db, email, password, _unique_client())  # type: ignore[arg-type]

    assert error.code == INVALID_CREDENTIALS


@pytest.mark.parametrize("password", ["bad\ud800surrogate", "", None])
def test_auth_05_malformed_password_raises_invalid_credentials(
    db: Session, clock: FakeClock, password: object
) -> None:
    user = _make_user(db)

    error = _login_error(db, user.email_normalized, password, _unique_client())  # type: ignore[arg-type]

    assert error.code == INVALID_CREDENTIALS


def test_auth_05_google_only_account_raises_invalid_credentials(
    db: Session, clock: FakeClock
) -> None:
    user = _make_user(db, password=None)

    error = _login_error(db, user.email_normalized, GOOD_PASSWORD, _unique_client())

    assert error.code == INVALID_CREDENTIALS


def test_auth_05_account_pending_deletion_raises_invalid_credentials(
    db: Session, clock: FakeClock
) -> None:
    user = _make_user(db)
    user.deletion_requested_at = datetime.now(UTC)
    db.flush()

    error = _login_error(db, user.email_normalized, GOOD_PASSWORD, _unique_client())

    assert error.code == INVALID_CREDENTIALS


# --- AUTH-04: unverified account ------------------------------------------------------------


def test_auth_04_unverified_account_with_correct_password_raises_email_not_verified(
    db: Session, clock: FakeClock
) -> None:
    user = _make_user(db, verified=False)

    error = _login_error(db, user.email_normalized, GOOD_PASSWORD, _unique_client())

    assert error.code == EMAIL_NOT_VERIFIED


def test_auth_04_unverified_account_with_wrong_password_raises_invalid_credentials(
    db: Session, clock: FakeClock
) -> None:
    user = _make_user(db, verified=False)

    error = _login_error(db, user.email_normalized, WRONG_PASSWORD, _unique_client())

    assert error.code == INVALID_CREDENTIALS


# --- AUTH-90: throttling --------------------------------------------------------------------


def _fail_until_locked(db: Session, email: str, client: str) -> None:
    for _ in range(get_settings().login_max_failures):
        assert _login_error(db, email, WRONG_PASSWORD, client).code == INVALID_CREDENTIALS


def test_auth_90_account_locked_after_max_failures_even_with_correct_password(
    db: Session, clock: FakeClock
) -> None:
    user = _make_user(db)
    _fail_until_locked(db, user.email_normalized, _unique_client())

    # A different origin does not bypass the account lock.
    error = _login_error(db, user.email_normalized, GOOD_PASSWORD, _unique_client())

    assert error.code == TOO_MANY_ATTEMPTS


def test_auth_90_origin_locked_after_max_failures_across_accounts(
    db: Session, clock: FakeClock
) -> None:
    client = _unique_client()
    for _ in range(get_settings().login_max_failures):
        _login_error(db, _unique_email(), WRONG_PASSWORD, client)
    user = _make_user(db)

    error = _login_error(db, user.email_normalized, GOOD_PASSWORD, client)

    assert error.code == TOO_MANY_ATTEMPTS


def test_auth_90_unknown_email_is_locked_like_an_existing_one(
    db: Session, clock: FakeClock
) -> None:
    email = _unique_email()
    _fail_until_locked(db, email, _unique_client())

    assert _login_error(db, email, WRONG_PASSWORD, _unique_client()).code == TOO_MANY_ATTEMPTS


def test_auth_90_login_allowed_again_after_lock_minutes(db: Session, clock: FakeClock) -> None:
    user = _make_user(db)
    client = _unique_client()
    _fail_until_locked(db, user.email_normalized, client)

    clock.advance(timedelta(minutes=get_settings().login_lock_minutes) - timedelta(seconds=1))
    assert _login_error(db, user.email_normalized, GOOD_PASSWORD, client).code == (
        TOO_MANY_ATTEMPTS
    )

    clock.advance(timedelta(seconds=1))
    assert login_local(db, user.email_normalized, GOOD_PASSWORD, client) is user


def test_auth_90_failures_outside_the_window_do_not_accumulate(
    db: Session, clock: FakeClock
) -> None:
    user = _make_user(db)
    client = _unique_client()
    for _ in range(get_settings().login_max_failures - 1):
        _login_error(db, user.email_normalized, WRONG_PASSWORD, client)

    clock.advance(timedelta(minutes=get_settings().login_lock_minutes, seconds=1))
    _login_error(db, user.email_normalized, WRONG_PASSWORD, client)

    assert login_local(db, user.email_normalized, GOOD_PASSWORD, client) is user


def test_auth_90_success_resets_the_account_counter(db: Session, clock: FakeClock) -> None:
    user = _make_user(db)
    for _ in range(get_settings().login_max_failures - 1):
        _login_error(db, user.email_normalized, WRONG_PASSWORD, _unique_client())

    assert login_local(db, user.email_normalized, GOOD_PASSWORD, _unique_client()) is user

    # The counter restarted: max - 1 new failures still do not lock the account.
    for _ in range(get_settings().login_max_failures - 1):
        _login_error(db, user.email_normalized, WRONG_PASSWORD, _unique_client())
    assert login_local(db, user.email_normalized, GOOD_PASSWORD, _unique_client()) is user


def test_auth_90_success_does_not_reset_the_origin_counter(db: Session, clock: FakeClock) -> None:
    client = _unique_client()
    for _ in range(get_settings().login_max_failures - 1):
        _login_error(db, _unique_email(), WRONG_PASSWORD, client)
    user = _make_user(db)
    assert login_local(db, user.email_normalized, GOOD_PASSWORD, client) is user

    _login_error(db, _unique_email(), WRONG_PASSWORD, client)

    assert _login_error(db, user.email_normalized, GOOD_PASSWORD, client).code == (
        TOO_MANY_ATTEMPTS
    )


def test_auth_90_throttle_keys_never_store_raw_email_or_ip(db: Session, clock: FakeClock) -> None:
    email = _unique_email()
    client = _unique_client()
    _login_error(db, email, WRONG_PASSWORD, client)

    keys = db.execute(select(LoginThrottle.key)).scalars().all()

    assert keys
    assert all(len(key) <= 128 for key in keys)
    assert not any(email in key or client in key for key in keys)
    assert throttle.account_key(email) in keys
    assert throttle.origin_key(client) in keys


# --- Logging --------------------------------------------------------------------------------


def test_auth_05_logs_never_contain_password_or_email(
    db: Session, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    user = _make_user(db)
    unverified = _make_user(db, verified=False)
    client = _unique_client()

    login_local(db, user.email_normalized, GOOD_PASSWORD, client)
    _login_error(db, unverified.email_normalized, GOOD_PASSWORD, client)
    _fail_until_locked(db, user.email_normalized, _unique_client())
    _login_error(db, user.email_normalized, WRONG_PASSWORD, _unique_client())

    assert caplog.records
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert GOOD_PASSWORD not in logged
    assert WRONG_PASSWORD not in logged
    assert user.email_normalized not in logged
    assert client not in logged
