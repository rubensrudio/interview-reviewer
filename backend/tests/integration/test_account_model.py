"""Integration tests for the account tables (migration 0002_accounts) and their models."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuthSession as ExportedAuthSession
from app.models import LoginThrottle as ExportedLoginThrottle
from app.models import OneTimeToken as ExportedOneTimeToken
from app.models import TokenPurpose as ExportedTokenPurpose
from app.models import User as ExportedUser
from app.models.account import AuthSession, LoginThrottle, OneTimeToken, TokenPurpose, User


def _hash() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _user(email: str | None = None) -> User:
    return User(email_normalized=email or f"{uuid.uuid4().hex}@example.com")


def _in_one_hour() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def test_token_purpose_values() -> None:
    assert [purpose.value for purpose in TokenPurpose] == [
        "verify_email",
        "reset_password",
        "google_link",
    ]


def test_models_package_exports_account_models() -> None:
    assert ExportedUser is User
    assert ExportedAuthSession is AuthSession
    assert ExportedOneTimeToken is OneTimeToken
    assert ExportedTokenPurpose is TokenPurpose
    assert ExportedLoginThrottle is LoginThrottle


def test_insert_and_read_user_with_defaults(db: Session) -> None:
    user = _user("person@example.com")
    db.add(user)
    db.commit()
    user_id = user.id
    db.expunge_all()

    loaded = db.execute(select(User).where(User.id == user_id)).scalar_one()
    assert isinstance(loaded.id, uuid.UUID)
    assert loaded.email_normalized == "person@example.com"
    assert loaded.password_hash is None
    assert loaded.email_verified_at is None
    assert loaded.google_sub is None
    assert loaded.terms_version is None
    assert loaded.privacy_version is None
    assert loaded.terms_accepted_at is None
    assert loaded.deletion_requested_at is None
    assert loaded.created_at.tzinfo is not None
    assert abs(datetime.now(UTC) - loaded.created_at) < timedelta(minutes=5)


def test_auth_91_duplicate_email_normalized_raises_integrity_error(db: Session) -> None:
    db.add(_user("same@example.com"))
    db.flush()
    db.add(_user("same@example.com"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_duplicate_google_sub_raises_integrity_error(db: Session) -> None:
    first = _user()
    first.google_sub = "google-sub-1"
    db.add(first)
    db.flush()
    second = _user()
    second.google_sub = "google-sub-1"
    db.add(second)
    with pytest.raises(IntegrityError):
        db.flush()


def test_many_users_without_google_sub_are_allowed(db: Session) -> None:
    db.add_all([_user(), _user()])
    db.flush()


def test_auth_15_terms_acceptance_is_stored(db: Session) -> None:
    accepted_at = datetime.now(UTC)
    user = _user()
    user.terms_version = "2026-09"
    user.privacy_version = "2026-09"
    user.terms_accepted_at = accepted_at
    db.add(user)
    db.commit()
    user_id = user.id
    db.expunge_all()

    loaded = db.get(User, user_id)
    assert loaded is not None
    assert loaded.terms_version == "2026-09"
    assert loaded.privacy_version == "2026-09"
    assert loaded.terms_accepted_at == accepted_at


def test_deleting_user_cascades_sessions_and_tokens(db: Session) -> None:
    user = _user()
    db.add(user)
    db.flush()
    db.add(AuthSession(user_id=user.id, token_hash=_hash(), expires_at=_in_one_hour()))
    db.add(
        OneTimeToken(
            user_id=user.id,
            purpose=TokenPurpose.GOOGLE_LINK,
            token_hash=_hash(),
            payload={"google_sub": "google-sub-2"},
            expires_at=_in_one_hour(),
        )
    )
    db.flush()
    user_id = user.id

    db.delete(user)
    db.flush()
    db.expunge_all()

    sessions = db.scalar(
        select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user_id)
    )
    tokens = db.scalar(
        select(func.count()).select_from(OneTimeToken).where(OneTimeToken.user_id == user_id)
    )
    assert sessions == 0
    assert tokens == 0


def test_data_07_new_account_with_email_of_deleted_account_starts_empty(db: Session) -> None:
    old = _user("reused@example.com")
    db.add(old)
    db.flush()
    db.add(AuthSession(user_id=old.id, token_hash=_hash(), expires_at=_in_one_hour()))
    db.flush()
    db.delete(old)
    db.flush()

    new = _user("reused@example.com")
    db.add(new)
    db.flush()
    assert new.id != old.id
    count = db.scalar(select(func.count()).select_from(AuthSession))
    assert count == 0


def test_auth_session_token_hash_is_unique(db: Session) -> None:
    user = _user()
    db.add(user)
    db.flush()
    token_hash = _hash()
    db.add(AuthSession(user_id=user.id, token_hash=token_hash, expires_at=_in_one_hour()))
    db.flush()
    db.add(AuthSession(user_id=user.id, token_hash=token_hash, expires_at=_in_one_hour()))
    with pytest.raises(IntegrityError):
        db.flush()


def test_one_time_token_hash_is_unique(db: Session) -> None:
    user = _user()
    db.add(user)
    db.flush()
    token_hash = _hash()
    for _ in range(2):
        db.add(
            OneTimeToken(
                user_id=user.id,
                purpose=TokenPurpose.VERIFY_EMAIL,
                token_hash=token_hash,
                expires_at=_in_one_hour(),
            )
        )
    with pytest.raises(IntegrityError):
        db.flush()


def test_token_purpose_is_stored_as_postgres_enum(db: Session) -> None:
    labels = db.execute(
        text(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'token_purpose' ORDER BY e.enumsortorder"
        )
    ).scalars()
    assert list(labels) == ["verify_email", "reset_password", "google_link"]


def test_token_tables_store_only_hashes(db: Session) -> None:
    for table in ("auth_sessions", "one_time_tokens"):
        columns = {column["name"] for column in inspect(db.connection()).get_columns(table)}
        assert "token_hash" in columns
        assert "token" not in columns


def test_login_throttle_roundtrip(db: Session) -> None:
    key = f"account:{_hash()}"
    started = datetime.now(UTC)
    db.add(LoginThrottle(key=key, window_started_at=started))
    db.commit()
    db.expunge_all()

    loaded = db.get(LoginThrottle, key)
    assert loaded is not None
    assert loaded.failures == 0
    assert loaded.window_started_at == started
    assert loaded.locked_until is None
