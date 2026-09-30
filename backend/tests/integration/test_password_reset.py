"""Integration tests for the password reset flow (CT-16).

Covers AUTH-07, AUTH-08, AUTH-09 and AUTH-94.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import password_reset
from app.auth.password_reset import request_password_reset, reset_password
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import hash_secret, issue_one_time_token
from app.config import get_settings
from app.email.templates import EmailContent
from app.errors import LINK_INVALID, PASSWORD_POLICY, VALIDATION_ERROR, AppError
from app.models.account import AuthSession, OneTimeToken, TokenPurpose, User

OLD_PASSWORD = "correct horse battery staple 42"  # noqa: S105 - test fixture value
NEW_PASSWORD = "another horse battery staple 77"  # noqa: S105 - test fixture value
RESET_MARKER = "/reset-password?token="


class FakeMailer:
    """Records calls to ``send_email`` and returns a configurable result."""

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.sent: list[tuple[str, EmailContent]] = []

    def __call__(self, to: str, message: EmailContent) -> bool:
        self.sent.append((to, message))
        return self.result


@pytest.fixture
def mailer(monkeypatch: pytest.MonkeyPatch) -> FakeMailer:
    fake = FakeMailer()
    monkeypatch.setattr(password_reset, "send_email", fake)
    return fake


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.com"


def _local_user(db: Session, *, verified: bool = True) -> User:
    user = User(
        email_normalized=_unique_email(),
        password_hash=hash_password(OLD_PASSWORD),
        email_verified_at=datetime.now(UTC) if verified else None,
    )
    db.add(user)
    db.flush()
    return user


def _google_only_user(db: Session) -> User:
    user = User(
        email_normalized=_unique_email(),
        password_hash=None,
        email_verified_at=datetime.now(UTC),
        google_sub=f"google-{uuid.uuid4().hex}",
    )
    db.add(user)
    db.flush()
    return user


def _add_session(db: Session, user: User) -> AuthSession:
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=hash_secret(uuid.uuid4().hex),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(auth_session)
    db.flush()
    return auth_session


def _token_from_mail(message: EmailContent) -> str:
    start = message.body.index(RESET_MARKER) + len(RESET_MARKER)
    end = message.body.index("\n", start)
    return message.body[start:end]


def _issue_reset_token(db: Session, user: User) -> str:
    return issue_one_time_token(db, user.id, TokenPurpose.RESET_PASSWORD, timedelta(minutes=60))


def _token_row(db: Session, raw: str) -> OneTimeToken:
    return db.execute(
        select(OneTimeToken).where(OneTimeToken.token_hash == hash_secret(raw))
    ).scalar_one()


# --- request_password_reset -----------------------------------------------------------------


def test_auth_07_unknown_email_sends_nothing_and_returns_none(
    db: Session, mailer: FakeMailer
) -> None:
    result = request_password_reset(db, _unique_email())  # type: ignore[func-returns-value]

    assert result is None
    assert mailer.sent == []


@pytest.mark.parametrize("email", ["", "not-an-email", "a\ud800@example.com", "x" * 5000])
def test_auth_07_malformed_email_is_neutral_and_sends_nothing(
    db: Session, mailer: FakeMailer, email: str
) -> None:
    assert request_password_reset(db, email) is None  # type: ignore[func-returns-value]
    assert mailer.sent == []


def test_auth_07_non_string_email_is_neutral(db: Session, mailer: FakeMailer) -> None:
    assert request_password_reset(db, None) is None  # type: ignore[arg-type,func-returns-value]
    assert mailer.sent == []


def test_auth_07_local_account_gets_single_use_link_with_configured_ttl(
    db: Session, mailer: FakeMailer
) -> None:
    user = _local_user(db)
    settings = get_settings()

    result = request_password_reset(db, f"  {user.email_normalized.upper()} ")  # type: ignore[func-returns-value]

    assert result is None
    assert len(mailer.sent) == 1
    to, message = mailer.sent[0]
    assert to == user.email_normalized
    assert message.template == "password_reset"
    assert f"{settings.frontend_base_url.rstrip('/')}{RESET_MARKER}" in message.body

    token = _token_row(db, _token_from_mail(message))
    assert token.user_id == user.id
    assert token.purpose is TokenPurpose.RESET_PASSWORD
    assert token.used_at is None
    ttl = token.expires_at - token.created_at
    expected = timedelta(minutes=settings.reset_ttl_minutes)
    assert expected - timedelta(minutes=1) < ttl <= expected + timedelta(minutes=1)


def test_auth_07_smtp_failure_is_still_neutral(db: Session, mailer: FakeMailer) -> None:
    mailer.result = False
    user = _local_user(db)

    assert request_password_reset(db, user.email_normalized) is None  # type: ignore[func-returns-value]
    assert len(mailer.sent) == 1


def test_auth_09_google_only_account_gets_google_email_and_no_password(
    db: Session, mailer: FakeMailer
) -> None:
    user = _google_only_user(db)

    assert request_password_reset(db, user.email_normalized) is None  # type: ignore[func-returns-value]

    assert len(mailer.sent) == 1
    to, message = mailer.sent[0]
    assert to == user.email_normalized
    assert message.template == "google_only_account"
    assert RESET_MARKER not in message.body
    db.refresh(user)
    assert user.password_hash is None
    tokens = db.execute(select(OneTimeToken).where(OneTimeToken.user_id == user.id)).all()
    assert tokens == []


def test_auth_07_account_pending_deletion_gets_nothing(db: Session, mailer: FakeMailer) -> None:
    user = _local_user(db)
    user.deletion_requested_at = datetime.now(UTC)
    db.flush()

    assert request_password_reset(db, user.email_normalized) is None  # type: ignore[func-returns-value]
    assert mailer.sent == []


# --- reset_password -------------------------------------------------------------------------


def test_auth_08_valid_token_changes_password_and_reuse_is_link_invalid(
    db: Session, mailer: FakeMailer
) -> None:
    user = _local_user(db)
    request_password_reset(db, user.email_normalized)
    raw = _token_from_mail(mailer.sent[0][1])

    reset_password(db, raw, NEW_PASSWORD)

    db.refresh(user)
    assert user.password_hash is not None
    assert verify_password(user.password_hash, NEW_PASSWORD)
    assert not verify_password(user.password_hash, OLD_PASSWORD)
    assert _token_row(db, raw).used_at is not None

    with pytest.raises(AppError) as excinfo:
        reset_password(db, raw, "yet another horse battery 99")
    assert excinfo.value.code == LINK_INVALID
    db.refresh(user)
    assert user.password_hash is not None
    assert verify_password(user.password_hash, NEW_PASSWORD)


def test_auth_14_common_password_raises_policy_and_keeps_token(
    db: Session, mailer: FakeMailer
) -> None:
    user = _local_user(db)
    raw = _issue_reset_token(db, user)

    with pytest.raises(AppError) as excinfo:
        reset_password(db, raw, "password123")

    assert excinfo.value.code == PASSWORD_POLICY
    assert excinfo.value.details == {"violations": ["common"]}
    assert _token_row(db, raw).used_at is None
    db.refresh(user)
    assert user.password_hash is not None
    assert verify_password(user.password_hash, OLD_PASSWORD)

    reset_password(db, raw, NEW_PASSWORD)
    db.refresh(user)
    assert user.password_hash is not None
    assert verify_password(user.password_hash, NEW_PASSWORD)


def test_auth_08_previous_sessions_are_revoked(db: Session, mailer: FakeMailer) -> None:
    user = _local_user(db)
    other = _local_user(db)
    sessions = [_add_session(db, user), _add_session(db, user)]
    other_session = _add_session(db, other)
    raw = _issue_reset_token(db, user)

    reset_password(db, raw, NEW_PASSWORD)

    for auth_session in sessions:
        db.refresh(auth_session)
        assert auth_session.revoked_at is not None
    db.refresh(other_session)
    assert other_session.revoked_at is None


def test_auth_08_other_outstanding_reset_links_are_invalidated(
    db: Session, mailer: FakeMailer
) -> None:
    user = _local_user(db)
    first = _issue_reset_token(db, user)
    second = _issue_reset_token(db, user)

    reset_password(db, second, NEW_PASSWORD)

    with pytest.raises(AppError) as excinfo:
        reset_password(db, first, "yet another horse battery 99")
    assert excinfo.value.code == LINK_INVALID


@pytest.mark.parametrize("raw", ["", "unknown-token", "bad token!", "a\ud800b"])
def test_auth_94_invalid_token_raises_link_invalid(
    db: Session, mailer: FakeMailer, raw: str
) -> None:
    with pytest.raises(AppError) as excinfo:
        reset_password(db, raw, NEW_PASSWORD)
    assert excinfo.value.code == LINK_INVALID


def test_auth_94_non_string_token_raises_link_invalid(db: Session, mailer: FakeMailer) -> None:
    with pytest.raises(AppError) as excinfo:
        reset_password(db, None, NEW_PASSWORD)  # type: ignore[arg-type]
    assert excinfo.value.code == LINK_INVALID


def test_auth_94_expired_token_raises_link_invalid(db: Session, mailer: FakeMailer) -> None:
    user = _local_user(db)
    raw = _issue_reset_token(db, user)
    _token_row(db, raw).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()

    with pytest.raises(AppError) as excinfo:
        reset_password(db, raw, NEW_PASSWORD)
    assert excinfo.value.code == LINK_INVALID


def test_auth_94_verification_token_cannot_reset_password(db: Session, mailer: FakeMailer) -> None:
    user = _local_user(db, verified=False)
    raw = issue_one_time_token(db, user.id, TokenPurpose.VERIFY_EMAIL, timedelta(minutes=5))

    with pytest.raises(AppError) as excinfo:
        reset_password(db, raw, NEW_PASSWORD)
    assert excinfo.value.code == LINK_INVALID


def test_auth_09_reset_token_never_creates_password_for_google_only_account(
    db: Session, mailer: FakeMailer
) -> None:
    user = _google_only_user(db)
    raw = _issue_reset_token(db, user)

    with pytest.raises(AppError) as excinfo:
        reset_password(db, raw, NEW_PASSWORD)

    assert excinfo.value.code == LINK_INVALID
    db.refresh(user)
    assert user.password_hash is None


def test_auth_94_reset_for_account_pending_deletion_is_link_invalid(
    db: Session, mailer: FakeMailer
) -> None:
    user = _local_user(db)
    raw = _issue_reset_token(db, user)
    user.deletion_requested_at = datetime.now(UTC)
    db.flush()

    with pytest.raises(AppError) as excinfo:
        reset_password(db, raw, NEW_PASSWORD)
    assert excinfo.value.code == LINK_INVALID


@pytest.mark.parametrize("password", [None, "valid-looking-\ud800-password"])
def test_auth_08_unencodable_or_non_string_password_is_rejected_and_keeps_token(
    db: Session, mailer: FakeMailer, password: object
) -> None:
    user = _local_user(db)
    raw = _issue_reset_token(db, user)

    with pytest.raises(AppError) as excinfo:
        reset_password(db, raw, password)  # type: ignore[arg-type]

    assert excinfo.value.code == VALIDATION_ERROR
    assert _token_row(db, raw).used_at is None
