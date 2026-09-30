"""Integration tests for Google sign-in resolution and account linking (CT-18).

Covers AUTH-10, AUTH-11, AUTH-12 and AUTH-13 (LAC-15).
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth import google_accounts, throttle
from app.auth.google_accounts import GoogleLoginResult, complete_link, resolve_google_login
from app.auth.google_oidc import GoogleIdentity
from app.auth.login import login_local
from app.auth.password_reset import reset_password
from app.auth.passwords import hash_password
from app.auth.tokens import hash_secret, issue_one_time_token
from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import (
    GOOGLE_AUTH_FAILED,
    INVALID_CREDENTIALS,
    LINK_INVALID,
    TOO_MANY_ATTEMPTS,
    AppError,
)
from app.legal.consent import has_current_consent, record_consent
from app.models.account import LoginThrottle, OneTimeToken, TokenPurpose, User

GOOD_PASSWORD = "correct horse battery staple 42"  # noqa: S105 - test fixture value
WRONG_PASSWORD = "wrong horse battery staple 42"  # noqa: S105 - test fixture value


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.com"


def _unique_sub() -> str:
    return str(uuid.uuid4().int)[:21]


def _identity(email: str, sub: str | None = None) -> GoogleIdentity:
    return GoogleIdentity(sub=sub or _unique_sub(), email=email, email_verified=True)


def _local_user(db: Session, *, verified: bool = True, consent: bool = True) -> User:
    user = User(
        email_normalized=_unique_email(),
        password_hash=hash_password(GOOD_PASSWORD),
        email_verified_at=datetime.now(UTC) if verified else None,
    )
    db.add(user)
    db.flush()
    if consent:
        record_consent(db, user)
    return user


def _account_throttle(db: Session, user: User) -> LoginThrottle | None:
    return db.execute(
        select(LoginThrottle)
        .where(LoginThrottle.key == throttle.account_key(user.email_normalized))
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _error(call: Callable[[], Any]) -> AppError:
    with pytest.raises(AppError) as info:
        call()
    return info.value


def _needs_link(db: Session, user: User, sub: str | None = None) -> tuple[str, str]:
    sub = sub or _unique_sub()
    result = resolve_google_login(db, _identity(user.email_normalized, sub))
    assert result.kind == "needs_link"
    assert result.pending_link_token is not None
    return result.pending_link_token, sub


# --- AUTH-10: new identity ------------------------------------------------------------------


def test_auth_10_new_identity_creates_verified_google_account_needing_terms(db: Session) -> None:
    email = _unique_email()
    sub = _unique_sub()

    result = resolve_google_login(db, _identity(email, sub))

    assert isinstance(result, GoogleLoginResult)
    assert result.kind == "needs_terms"
    assert result.pending_link_token is None
    assert result.user is not None
    stored = db.get(User, result.user.id)
    assert stored is not None
    assert stored.email_normalized == email
    assert stored.google_sub == sub
    assert stored.email_verified_at is not None
    assert stored.password_hash is None
    assert not has_current_consent(stored)


def test_auth_10_google_email_is_normalized_before_creating_the_account(db: Session) -> None:
    email = _unique_email()

    result = resolve_google_login(db, _identity(f"  {email.upper()} "))

    assert result.user is not None
    assert result.user.email_normalized == email


# --- AUTH-13: linked identity ---------------------------------------------------------------


def test_auth_13_linked_identity_signs_in_to_the_same_account(db: Session) -> None:
    user = _local_user(db)
    user.google_sub = _unique_sub()
    db.flush()

    result = resolve_google_login(db, _identity(user.email_normalized, user.google_sub))

    assert result.kind == "signed_in"
    assert result.user is not None
    assert result.user.id == user.id
    assert result.pending_link_token is None


def test_auth_13_linked_identity_is_found_by_sub_even_if_google_email_changed(
    db: Session,
) -> None:
    user = _local_user(db)
    user.google_sub = _unique_sub()
    db.flush()

    result = resolve_google_login(db, _identity(_unique_email(), user.google_sub))

    assert result.kind == "signed_in"
    assert result.user is not None
    assert result.user.id == user.id


def test_auth_13_second_google_login_of_new_account_returns_same_user(db: Session) -> None:
    identity = _identity(_unique_email())
    first = resolve_google_login(db, identity)

    second = resolve_google_login(db, identity)

    assert first.user is not None and second.user is not None
    assert second.user.id == first.user.id
    assert (
        db.scalar(select(func.count()).select_from(User).where(User.google_sub == identity.sub))
        == 1
    )


# --- AUTH-11: local account with the same e-mail --------------------------------------------


def test_auth_11_local_account_email_returns_needs_link_without_linking(db: Session) -> None:
    user = _local_user(db)
    sub = _unique_sub()

    result = resolve_google_login(db, _identity(user.email_normalized.upper(), sub))

    assert result.kind == "needs_link"
    assert result.user is None
    assert result.pending_link_token is not None
    db.refresh(user)
    assert user.google_sub is None
    token = db.execute(
        select(OneTimeToken).where(
            OneTimeToken.token_hash == hash_secret(result.pending_link_token)
        )
    ).scalar_one()
    assert token.user_id == user.id
    assert token.purpose == TokenPurpose.GOOGLE_LINK
    assert token.payload == {"google_sub": sub}
    ttl = timedelta(minutes=get_settings().google_link_ttl_minutes)
    assert token.expires_at <= datetime.now(UTC) + ttl
    assert token.expires_at > datetime.now(UTC) + ttl - timedelta(minutes=1)


def test_auth_11_complete_link_with_correct_password_links_and_then_signs_in(
    db: Session,
) -> None:
    user = _local_user(db)
    raw, sub = _needs_link(db, user)

    linked = complete_link(db, raw, GOOD_PASSWORD)

    assert linked.id == user.id
    db.refresh(user)
    assert user.google_sub == sub
    again = resolve_google_login(db, _identity(user.email_normalized, sub))
    assert again.kind == "signed_in"
    assert again.user is not None
    assert again.user.id == user.id


def test_auth_11_link_token_cannot_be_reused(db: Session) -> None:
    user = _local_user(db)
    raw, _ = _needs_link(db, user)
    complete_link(db, raw, GOOD_PASSWORD)

    assert _error(lambda: complete_link(db, raw, GOOD_PASSWORD)).code == LINK_INVALID


def test_auth_11_successful_link_resets_account_throttle(db: Session) -> None:
    user = _local_user(db)
    raw, _ = _needs_link(db, user)
    _error(lambda: complete_link(db, raw, WRONG_PASSWORD))

    complete_link(db, raw, GOOD_PASSWORD)

    row = _account_throttle(db, user)
    assert row is not None
    assert row.failures == 0


@pytest.mark.parametrize(
    "raw", ["", "not a token", "a\x00b", "\ud800", "x" * 2000, None, 123, b"bytes"]
)
def test_auth_11_malformed_link_token_is_link_invalid(db: Session, raw: object) -> None:
    error = _error(lambda: complete_link(db, raw, GOOD_PASSWORD))  # type: ignore[arg-type]

    assert error.code == LINK_INVALID


def test_auth_11_other_purpose_token_is_link_invalid(db: Session) -> None:
    user = _local_user(db)
    raw = issue_one_time_token(
        db, user.id, TokenPurpose.RESET_PASSWORD, timedelta(minutes=5), {"google_sub": "1"}
    )

    assert _error(lambda: complete_link(db, raw, GOOD_PASSWORD)).code == LINK_INVALID
    db.refresh(user)
    assert user.google_sub is None


def test_auth_11_link_token_without_google_sub_payload_is_link_invalid(db: Session) -> None:
    user = _local_user(db)
    raw = issue_one_time_token(db, user.id, TokenPurpose.GOOGLE_LINK, timedelta(minutes=5))

    assert _error(lambda: complete_link(db, raw, GOOD_PASSWORD)).code == LINK_INVALID
    db.refresh(user)
    assert user.google_sub is None


def test_auth_11_sub_linked_elsewhere_meanwhile_is_link_invalid(db: Session) -> None:
    user = _local_user(db)
    raw, sub = _needs_link(db, user)
    other = _local_user(db)
    other.google_sub = sub
    db.flush()

    assert _error(lambda: complete_link(db, raw, GOOD_PASSWORD)).code == LINK_INVALID
    db.refresh(user)
    assert user.google_sub is None


def test_auth_11_account_linked_to_another_sub_meanwhile_is_link_invalid(db: Session) -> None:
    user = _local_user(db)
    raw, _ = _needs_link(db, user)
    user.google_sub = _unique_sub()
    db.flush()
    first_sub = user.google_sub

    assert _error(lambda: complete_link(db, raw, GOOD_PASSWORD)).code == LINK_INVALID
    db.refresh(user)
    assert user.google_sub == first_sub


def test_auth_11_account_pending_deletion_cannot_be_linked(db: Session) -> None:
    user = _local_user(db)
    raw, _ = _needs_link(db, user)
    user.deletion_requested_at = datetime.now(UTC)
    db.flush()

    assert _error(lambda: complete_link(db, raw, GOOD_PASSWORD)).code == LINK_INVALID
    db.refresh(user)
    assert user.google_sub is None


# --- AUTH-12: wrong password ----------------------------------------------------------------


def test_auth_12_wrong_password_raises_invalid_credentials_and_does_not_link(
    db: Session,
) -> None:
    user = _local_user(db)
    raw, _ = _needs_link(db, user)

    error = _error(lambda: complete_link(db, raw, WRONG_PASSWORD))

    assert error.code == INVALID_CREDENTIALS
    db.refresh(user)
    assert user.google_sub is None


def test_auth_12_wrong_password_counts_for_account_throttle_and_keeps_link(db: Session) -> None:
    user = _local_user(db)
    raw, sub = _needs_link(db, user)

    _error(lambda: complete_link(db, raw, WRONG_PASSWORD))

    row = _account_throttle(db, user)
    assert row is not None
    assert row.failures == 1
    # A typo does not burn the link: the right password still completes it.
    assert complete_link(db, raw, GOOD_PASSWORD).id == user.id
    db.refresh(user)
    assert user.google_sub == sub


def test_auth_12_wrong_password_failure_survives_request_rollback(db: Session) -> None:
    user = _local_user(db)
    raw, _ = _needs_link(db, user)
    db.commit()

    _error(lambda: complete_link(db, raw, WRONG_PASSWORD))
    db.rollback()

    row = _account_throttle(db, user)
    assert row is not None
    assert row.failures == 1


def test_auth_12_locked_account_rejects_even_correct_password(db: Session) -> None:
    user = _local_user(db)
    raw, _ = _needs_link(db, user)
    for _ in range(get_settings().login_max_failures):
        assert _error(lambda: complete_link(db, raw, WRONG_PASSWORD)).code == INVALID_CREDENTIALS

    error = _error(lambda: complete_link(db, raw, GOOD_PASSWORD))

    assert error.code == TOO_MANY_ATTEMPTS
    db.refresh(user)
    assert user.google_sub is None


def test_auth_12_wrong_link_password_shares_counter_with_local_login(db: Session) -> None:
    user = _local_user(db)
    raw, _ = _needs_link(db, user)
    _error(lambda: complete_link(db, raw, WRONG_PASSWORD))
    _error(lambda: login_local(db, user.email_normalized, WRONG_PASSWORD, "203.0.113.9"))

    row = _account_throttle(db, user)
    assert row is not None
    assert row.failures == 2


@pytest.mark.parametrize(
    "password", ["", "\ud800 horse battery", "nul\x00inside", "x" * 5000, None, 42]
)
def test_auth_12_unusable_password_is_invalid_credentials(db: Session, password: object) -> None:
    user = _local_user(db)
    raw, _ = _needs_link(db, user)

    error = _error(lambda: complete_link(db, raw, password))  # type: ignore[arg-type]

    assert error.code == INVALID_CREDENTIALS
    db.refresh(user)
    assert user.google_sub is None


def test_auth_12_long_password_never_reaches_argon2(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _local_user(db)
    raw, _ = _needs_link(db, user)
    calls: list[str] = []
    monkeypatch.setattr(google_accounts, "verify_password", lambda h, p: calls.append(p) or False)

    _error(lambda: complete_link(db, raw, "x" * 5000))

    assert calls == []


# --- Rejected identities --------------------------------------------------------------------


def test_google_unverified_email_is_rejected(db: Session) -> None:
    email = _unique_email()
    identity = GoogleIdentity(sub=_unique_sub(), email=email, email_verified=False)

    assert _error(lambda: resolve_google_login(db, identity)).code == GOOGLE_AUTH_FAILED
    assert db.execute(select(User).where(User.email_normalized == email)).first() is None


@pytest.mark.parametrize(
    ("sub", "email"),
    [
        ("", "ok@example.com"),
        ("a\x00b", "ok@example.com"),
        ("\ud800", "ok@example.com"),
        ("x" * 256, "ok@example.com"),
        ("sub with space", "ok@example.com"),
        (None, "ok@example.com"),
        ("123", ""),
        ("123", "not-an-email"),
        ("123", "a\x00b@example.com"),
        ("123", "\ud800@example.com"),
        ("123", "a" * 400 + "@example.com"),
        ("123", None),
    ],
)
def test_google_malformed_identity_is_rejected(db: Session, sub: object, email: object) -> None:
    identity = GoogleIdentity(sub=sub, email=email, email_verified=True)  # type: ignore[arg-type]

    assert _error(lambda: resolve_google_login(db, identity)).code == GOOGLE_AUTH_FAILED


def test_google_email_of_account_linked_to_other_sub_is_rejected(db: Session) -> None:
    user = _local_user(db)
    user.google_sub = _unique_sub()
    db.flush()
    original = user.google_sub

    error = _error(lambda: resolve_google_login(db, _identity(user.email_normalized)))

    assert error.code == GOOGLE_AUTH_FAILED
    db.refresh(user)
    assert user.google_sub == original


def test_google_login_of_account_pending_deletion_is_rejected(db: Session) -> None:
    linked = _local_user(db)
    linked.google_sub = _unique_sub()
    linked.deletion_requested_at = datetime.now(UTC)
    local = _local_user(db)
    local.deletion_requested_at = datetime.now(UTC)
    db.flush()

    by_sub = _error(lambda: resolve_google_login(db, _identity(_unique_email(), linked.google_sub)))
    by_email = _error(lambda: resolve_google_login(db, _identity(local.email_normalized)))

    assert by_sub.code == GOOGLE_AUTH_FAILED
    assert by_email.code == GOOGLE_AUTH_FAILED
    assert (
        db.scalar(
            select(func.count()).select_from(OneTimeToken).where(OneTimeToken.user_id == local.id)
        )
        == 0
    )


# --- Concurrency (real commits) -------------------------------------------------------------


@pytest.fixture
def committed_users(migrated_database: str) -> Iterator[Callable[[uuid.UUID], None]]:
    ids: list[uuid.UUID] = []
    emails: list[str] = []

    def track(user_id: uuid.UUID) -> None:
        ids.append(user_id)

    yield track
    with get_sessionmaker()() as session:
        emails = list(session.scalars(select(User.email_normalized).where(User.id.in_(ids))))
        session.execute(delete(User).where(User.id.in_(ids)))
        session.execute(
            delete(LoginThrottle).where(
                LoginThrottle.key.in_([throttle.account_key(e) for e in emails])
            )
        )
        session.commit()


def _run_concurrently(
    workers: list[Callable[[Session], Any]],
) -> tuple[list[Any], list[BaseException]]:
    barrier = threading.Barrier(len(workers))
    results: list[Any] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(work: Callable[[Session], Any]) -> None:
        try:
            with get_sessionmaker()() as session:
                barrier.wait(timeout=10)
                try:
                    value = work(session)
                    session.commit()
                except BaseException:
                    session.rollback()
                    raise
            with lock:
                results.append(value)
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertions
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=run, args=(work,)) for work in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    return results, errors


def _committed_local_user(track: Callable[[uuid.UUID], None]) -> tuple[uuid.UUID, str, str]:
    with get_sessionmaker()() as session:
        user = _local_user(session)
        track(user.id)
        raw, sub = _needs_link(session, user)
        session.commit()
        return user.id, raw, sub


@pytest.mark.parametrize("attempt", range(3))
def test_auth_11_concurrent_link_with_same_token_links_once(
    committed_users: Callable[[uuid.UUID], None], attempt: int
) -> None:
    user_id, raw, sub = _committed_local_user(committed_users)

    results, errors = _run_concurrently(
        [lambda s: complete_link(s, raw, GOOD_PASSWORD).id for _ in range(2)]
    )

    assert results == [user_id]
    assert len(errors) == 1
    assert isinstance(errors[0], AppError), repr(errors[0])
    assert errors[0].code == LINK_INVALID
    with get_sessionmaker()() as session:
        stored = session.get(User, user_id)
        assert stored is not None
        assert stored.google_sub == sub


@pytest.mark.parametrize("attempt", range(3))
def test_auth_12_concurrent_wrong_link_and_wrong_login_do_not_deadlock(
    committed_users: Callable[[uuid.UUID], None], attempt: int
) -> None:
    user_id, raw, _ = _committed_local_user(committed_users)
    with get_sessionmaker()() as session:
        email = session.get(User, user_id).email_normalized  # type: ignore[union-attr]

    results, errors = _run_concurrently(
        [
            lambda s: complete_link(s, raw, WRONG_PASSWORD),
            lambda s: login_local(s, email, WRONG_PASSWORD, f"198.51.100.{attempt}"),
        ]
    )

    assert results == []
    assert len(errors) == 2
    assert all(isinstance(e, AppError) and e.code == INVALID_CREDENTIALS for e in errors), errors
    with get_sessionmaker()() as session:
        row = session.get(LoginThrottle, throttle.account_key(email))
        assert row is not None
        assert row.failures == 2


@pytest.mark.parametrize("attempt", range(3))
def test_auth_11_concurrent_link_and_password_reset_do_not_deadlock(
    committed_users: Callable[[uuid.UUID], None], attempt: int
) -> None:
    user_id, raw, sub = _committed_local_user(committed_users)
    with get_sessionmaker()() as session:
        reset_raw = issue_one_time_token(
            session, user_id, TokenPurpose.RESET_PASSWORD, timedelta(minutes=5)
        )
        session.commit()

    results, errors = _run_concurrently(
        [
            lambda s: complete_link(s, raw, GOOD_PASSWORD).id,
            lambda s: reset_password(s, reset_raw, "brand new horse battery 9"),
        ]
    )

    # Either order is valid; neither may fail with a deadlock or a server error.
    assert all(isinstance(e, AppError) for e in errors), errors
    assert len(results) + len(errors) == 2
    with get_sessionmaker()() as session:
        stored = session.get(User, user_id)
        assert stored is not None
        if user_id in results:
            assert stored.google_sub == sub


@pytest.mark.parametrize("attempt", range(3))
def test_auth_10_concurrent_first_google_logins_create_one_account(
    committed_users: Callable[[uuid.UUID], None], attempt: int
) -> None:
    identity = _identity(_unique_email())

    def work(session: Session) -> tuple[str, uuid.UUID]:
        result = resolve_google_login(session, identity)
        assert result.user is not None
        return result.kind, result.user.id

    results, errors = _run_concurrently([work, work])
    for _, user_id in results:
        committed_users(user_id)

    assert errors == []
    assert len(results) == 2
    assert results[0][1] == results[1][1]
    assert {kind for kind, _ in results} <= {"needs_terms", "signed_in"}
    with get_sessionmaker()() as session:
        count = session.scalar(
            select(func.count()).select_from(User).where(User.google_sub == identity.sub)
        )
        assert count == 1
