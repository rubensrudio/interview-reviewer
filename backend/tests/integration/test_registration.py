"""Integration tests for local registration and e-mail verification (CT-14).

Covers AUTH-01, AUTH-02, AUTH-03, AUTH-91, AUTH-92, AUTH-94 and DATA-07.
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth import registration
from app.auth.passwords import verify_password
from app.auth.registration import (
    normalize_email,
    register_local,
    resend_verification,
    verify_email,
)
from app.auth.tokens import hash_secret, issue_one_time_token
from app.config import get_settings
from app.db import get_sessionmaker
from app.email.templates import EmailContent
from app.errors import LINK_INVALID, PASSWORD_POLICY, TERMS_NOT_ACCEPTED, VALIDATION_ERROR, AppError
from app.legal.consent import has_current_consent
from app.models.account import OneTimeToken, TokenPurpose, User

GOOD_PASSWORD = "correct horse battery staple 42"  # noqa: S105 - test fixture value


class FakeMailer:
    """Records calls to ``send_email`` and returns a configurable result."""

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.sent: list[tuple[str, EmailContent]] = []
        self._lock = threading.Lock()

    def __call__(self, to: str, message: EmailContent) -> bool:
        with self._lock:
            self.sent.append((to, message))
        return self.result


@pytest.fixture
def mailer(monkeypatch: pytest.MonkeyPatch) -> FakeMailer:
    fake = FakeMailer()
    monkeypatch.setattr(registration, "send_email", fake)
    return fake


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.com"


def _user_by_email(db: Session, email: str) -> User | None:
    return db.execute(select(User).where(User.email_normalized == email)).scalar_one_or_none()


def _count_users(db: Session, email: str) -> int:
    return db.execute(
        select(func.count()).select_from(User).where(User.email_normalized == email)
    ).scalar_one()


def _token_from_mail(message: EmailContent) -> str:
    marker = "/verify-email?token="
    start = message.body.index(marker) + len(marker)
    end = message.body.index("\n", start)
    return message.body[start:end]


# --- normalize_email ------------------------------------------------------------------------


def test_auth_02_normalize_email_strips_and_lowercases() -> None:
    assert normalize_email("  A@X.com ") == "a@x.com"
    assert normalize_email("\tMiXeD@Example.ORG\n") == "mixed@example.org"


# --- register_local -------------------------------------------------------------------------


def test_auth_01_register_creates_unverified_user_with_consent(
    db: Session, mailer: FakeMailer
) -> None:
    local = f"A{uuid.uuid4().hex}"
    raw_email = f"  {local}@X.com "
    expected = f"{local.lower()}@x.com"

    result = register_local(db, raw_email, GOOD_PASSWORD, accepted_terms=True)

    assert result == "sent"
    user = _user_by_email(db, expected)
    assert user is not None
    assert user.email_normalized == expected
    assert user.email_verified_at is None
    assert user.google_sub is None
    assert user.password_hash is not None
    assert user.password_hash != GOOD_PASSWORD
    assert verify_password(user.password_hash, GOOD_PASSWORD)
    assert has_current_consent(user)


def test_auth_01_register_sends_single_use_verification_link_with_configured_ttl(
    db: Session, mailer: FakeMailer
) -> None:
    email = _unique_email()
    settings = get_settings()

    register_local(db, email, GOOD_PASSWORD, accepted_terms=True)

    assert len(mailer.sent) == 1
    to, message = mailer.sent[0]
    assert to == email
    assert message.template == "verification"
    expected_prefix = f"{settings.frontend_base_url.rstrip('/')}/verify-email?token="
    assert expected_prefix in message.body

    raw = _token_from_mail(message)
    token = db.execute(
        select(OneTimeToken).where(OneTimeToken.token_hash == hash_secret(raw))
    ).scalar_one()
    user = _user_by_email(db, email)
    assert user is not None
    assert token.user_id == user.id
    assert token.purpose is TokenPurpose.VERIFY_EMAIL
    assert token.used_at is None
    ttl = token.expires_at - token.created_at
    expected_ttl = timedelta(minutes=settings.verification_ttl_minutes)
    assert expected_ttl - timedelta(minutes=1) < ttl <= expected_ttl + timedelta(minutes=1)


def test_auth_02_second_registration_same_email_is_neutral_and_creates_nothing(
    db: Session, mailer: FakeMailer
) -> None:
    email = _unique_email()
    first = register_local(db, email, GOOD_PASSWORD, accepted_terms=True)
    original = _user_by_email(db, email)
    assert original is not None
    original_hash = original.password_hash

    second = register_local(
        db, f"  {email.upper()} ", "another strong password 99", accepted_terms=True
    )

    assert first == "sent"
    assert second == first
    assert _count_users(db, email) == 1
    db.refresh(original)
    assert original.password_hash == original_hash
    # Only the first registration sent a verification link.
    assert len(mailer.sent) == 1


def test_auth_02_existing_google_account_is_neutral_and_untouched(
    db: Session, mailer: FakeMailer
) -> None:
    email = _unique_email()
    google_user = User(email_normalized=email, google_sub=f"sub-{uuid.uuid4().hex}")
    db.add(google_user)
    db.flush()

    result = register_local(db, email, GOOD_PASSWORD, accepted_terms=True)

    assert result == "sent"
    assert _count_users(db, email) == 1
    db.refresh(google_user)
    assert google_user.password_hash is None
    assert mailer.sent == []


def test_auth_92_smtp_failure_keeps_account_and_returns_delayed(
    db: Session, mailer: FakeMailer
) -> None:
    mailer.result = False
    email = _unique_email()

    result = register_local(db, email, GOOD_PASSWORD, accepted_terms=True)

    assert result == "delayed"
    user = _user_by_email(db, email)
    assert user is not None
    assert user.email_verified_at is None


def test_auth_92_registration_never_logs_token_or_link(
    db: Session, mailer: FakeMailer, caplog: pytest.LogCaptureFixture
) -> None:
    mailer.result = False
    email = _unique_email()
    caplog.set_level("DEBUG")

    register_local(db, email, GOOD_PASSWORD, accepted_terms=True)

    raw = _token_from_mail(mailer.sent[0][1])
    assert caplog.records
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert raw not in logged
    assert "verify-email" not in logged
    assert email not in logged
    assert GOOD_PASSWORD not in logged


def test_auth_01_short_password_raises_password_policy(db: Session, mailer: FakeMailer) -> None:
    email = _unique_email()

    with pytest.raises(AppError) as caught:
        register_local(db, email, "abc", accepted_terms=True)

    assert caught.value.code == PASSWORD_POLICY
    assert caught.value.details is not None
    assert caught.value.details["violations"] == ["too_short"]
    assert _count_users(db, email) == 0
    assert mailer.sent == []


def test_auth_01_common_password_raises_password_policy(db: Session, mailer: FakeMailer) -> None:
    with pytest.raises(AppError) as caught:
        register_local(db, _unique_email(), "Password123", accepted_terms=True)

    assert caught.value.code == PASSWORD_POLICY
    assert caught.value.details is not None
    assert "common" in caught.value.details["violations"]


def test_auth_02_password_policy_is_checked_before_existing_email(
    db: Session, mailer: FakeMailer
) -> None:
    email = _unique_email()
    register_local(db, email, GOOD_PASSWORD, accepted_terms=True)

    with pytest.raises(AppError) as caught:
        register_local(db, email, "abc", accepted_terms=True)

    assert caught.value.code == PASSWORD_POLICY


def test_auth_01_terms_not_accepted_raises_and_creates_nothing(
    db: Session, mailer: FakeMailer
) -> None:
    email = _unique_email()

    with pytest.raises(AppError) as caught:
        register_local(db, email, GOOD_PASSWORD, accepted_terms=False)

    assert caught.value.code == TERMS_NOT_ACCEPTED
    assert _count_users(db, email) == 0
    assert mailer.sent == []


@pytest.mark.parametrize(
    "bad_email",
    ["", "   ", "no-at-sign", "a@", "@x.com", "a@@x.com", "a b@x.com", "a@x\ud800.com"],
)
def test_auth_01_invalid_email_raises_validation_error(
    db: Session, mailer: FakeMailer, bad_email: str
) -> None:
    with pytest.raises(AppError) as caught:
        register_local(db, bad_email, GOOD_PASSWORD, accepted_terms=True)

    assert caught.value.code == VALIDATION_ERROR
    assert mailer.sent == []


@pytest.mark.parametrize(
    "undeliverable_email",
    [
        "a@b",
        "a,b@x.com",
        '"a"@x.com',
        "a<b@x.com",
        f"{'a' * 64}@{'b' * 187}.com",  # 256 characters: over the 254 SMTP limit
    ],
)
def test_auth_01_undeliverable_email_raises_validation_error_without_creating_user(
    db: Session, mailer: FakeMailer, undeliverable_email: str
) -> None:
    """Regression (QA TASK-016-1): e-mails the sender refuses must not create an account."""
    normalized = normalize_email(undeliverable_email)

    with pytest.raises(AppError) as caught:
        register_local(db, undeliverable_email, GOOD_PASSWORD, accepted_terms=True)

    assert caught.value.code == VALIDATION_ERROR
    assert _count_users(db, normalized) == 0
    assert mailer.sent == []
    assert resend_verification(db, undeliverable_email) == "skipped"


def test_auth_01_too_long_email_raises_validation_error(db: Session, mailer: FakeMailer) -> None:
    with pytest.raises(AppError) as caught:
        register_local(db, f"{'a' * 320}@x.com", GOOD_PASSWORD, accepted_terms=True)

    assert caught.value.code == VALIDATION_ERROR


def test_auth_01_password_with_lone_surrogate_raises_validation_error_not_crash(
    db: Session, mailer: FakeMailer
) -> None:
    email = _unique_email()

    with pytest.raises(AppError) as caught:
        register_local(db, email, "valid-length-\ud800-password", accepted_terms=True)

    assert caught.value.code == VALIDATION_ERROR
    assert _count_users(db, email) == 0


def test_data_07_email_of_deleted_account_creates_fresh_account(
    db: Session, mailer: FakeMailer
) -> None:
    email = _unique_email()
    register_local(db, email, GOOD_PASSWORD, accepted_terms=True)
    old = _user_by_email(db, email)
    assert old is not None
    old_id = old.id
    db.execute(delete(User).where(User.id == old_id))
    db.flush()
    db.expunge_all()

    result = register_local(db, email, "a brand new password 77", accepted_terms=True)

    assert result == "sent"
    fresh = _user_by_email(db, email)
    assert fresh is not None
    assert fresh.id != old_id
    assert fresh.email_verified_at is None
    assert verify_password(fresh.password_hash or "", "a brand new password 77")
    assert not verify_password(fresh.password_hash or "", GOOD_PASSWORD)


def test_data_07_account_pending_purge_is_not_duplicated(db: Session, mailer: FakeMailer) -> None:
    email = _unique_email()
    register_local(db, email, GOOD_PASSWORD, accepted_terms=True)
    pending = _user_by_email(db, email)
    assert pending is not None
    pending.deletion_requested_at = pending.created_at
    db.flush()
    mailer.sent.clear()

    result = register_local(db, email, GOOD_PASSWORD, accepted_terms=True)

    assert result == "sent"
    assert _count_users(db, email) == 1
    assert mailer.sent == []


# --- concurrency (AUTH-91) ------------------------------------------------------------------


@pytest.fixture
def committed_cleanup(migrated_database: str) -> Iterator[Callable[[str], None]]:
    emails: list[str] = []
    yield emails.append
    with get_sessionmaker()() as session:
        session.execute(delete(User).where(User.email_normalized.in_(emails)))
        session.commit()


def test_auth_91_concurrent_registrations_create_one_user(
    mailer: FakeMailer, committed_cleanup: Callable[[str], None]
) -> None:
    email = _unique_email()
    committed_cleanup(email)
    workers = 2
    barrier = threading.Barrier(workers)
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            with get_sessionmaker()() as session:
                barrier.wait(timeout=10)
                outcome = register_local(session, email, GOOD_PASSWORD, accepted_terms=True)
                session.commit()
            with lock:
                results.append(outcome)
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert results == ["sent", "sent"]
    with get_sessionmaker()() as session:
        assert _count_users(session, email) == 1


# --- verify_email ---------------------------------------------------------------------------


def test_auth_03_verify_email_marks_verified_and_second_use_is_link_invalid(
    db: Session, mailer: FakeMailer
) -> None:
    email = _unique_email()
    register_local(db, email, GOOD_PASSWORD, accepted_terms=True)
    raw = _token_from_mail(mailer.sent[0][1])

    verify_email(db, raw)

    user = _user_by_email(db, email)
    assert user is not None
    assert user.email_verified_at is not None

    with pytest.raises(AppError) as caught:
        verify_email(db, raw)
    assert caught.value.code == LINK_INVALID


@pytest.mark.parametrize("bad_token", ["", "not-a-real-token", "x" * 300, "bad\ud800token"])
def test_auth_94_verify_email_rejects_unknown_or_malformed_token(
    db: Session, bad_token: str
) -> None:
    with pytest.raises(AppError) as caught:
        verify_email(db, bad_token)

    assert caught.value.code == LINK_INVALID


def test_auth_94_verify_email_rejects_non_string_token(db: Session) -> None:
    with pytest.raises(AppError) as caught:
        verify_email(db, 12345)  # type: ignore[arg-type]

    assert caught.value.code == LINK_INVALID


def test_auth_94_verify_email_rejects_other_purpose_token(db: Session) -> None:
    user = User(email_normalized=_unique_email())
    db.add(user)
    db.flush()
    raw = issue_one_time_token(db, user.id, TokenPurpose.RESET_PASSWORD, timedelta(hours=1))

    with pytest.raises(AppError) as caught:
        verify_email(db, raw)

    assert caught.value.code == LINK_INVALID
    db.refresh(user)
    assert user.email_verified_at is None


# --- resend_verification --------------------------------------------------------------------


def test_auth_92_resend_sends_new_link_to_unverified_local_account(
    db: Session, mailer: FakeMailer
) -> None:
    email = _unique_email()
    register_local(db, email, GOOD_PASSWORD, accepted_terms=True)
    first_raw = _token_from_mail(mailer.sent[0][1])

    result = resend_verification(db, f" {email.upper()} ")

    assert result == "sent"
    assert len(mailer.sent) == 2
    assert mailer.sent[1][0] == email
    second_raw = _token_from_mail(mailer.sent[1][1])
    assert second_raw != first_raw
    verify_email(db, second_raw)
    user = _user_by_email(db, email)
    assert user is not None
    assert user.email_verified_at is not None


def test_auth_92_resend_returns_delayed_when_smtp_fails(db: Session, mailer: FakeMailer) -> None:
    email = _unique_email()
    register_local(db, email, GOOD_PASSWORD, accepted_terms=True)
    mailer.result = False

    assert resend_verification(db, email) == "delayed"


def test_auth_02_resend_skips_unknown_verified_and_google_accounts(
    db: Session, mailer: FakeMailer
) -> None:
    verified_email = _unique_email()
    register_local(db, verified_email, GOOD_PASSWORD, accepted_terms=True)
    verify_email(db, _token_from_mail(mailer.sent[0][1]))
    google_email = _unique_email()
    db.add(User(email_normalized=google_email, google_sub=f"sub-{uuid.uuid4().hex}"))
    db.flush()
    mailer.sent.clear()

    assert resend_verification(db, _unique_email()) == "skipped"
    assert resend_verification(db, verified_email) == "skipped"
    assert resend_verification(db, google_email) == "skipped"
    assert resend_verification(db, "not-an-email\ud800") == "skipped"
    assert mailer.sent == []
