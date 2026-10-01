"""Integration tests for secret tokens and one-time links (CT-10, AUTH-03, AUTH-08, AUTH-94)."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.tokens import consume_one_time_token, hash_secret, issue_one_time_token, new_secret
from app.errors import LINK_INVALID, AppError
from app.models.account import OneTimeToken, TokenPurpose, User


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


def _token_row(db: Session, raw: str) -> OneTimeToken:
    return db.execute(
        select(OneTimeToken).where(OneTimeToken.token_hash == hash_secret(raw))
    ).scalar_one()


def test_new_secret_is_urlsafe_random_and_long() -> None:
    first, second = new_secret(), new_secret()
    assert first != second
    # token_urlsafe(32) -> 43 base64url characters (256 bits of entropy).
    assert len(first) >= 43
    assert all(ch.isalnum() or ch in "-_" for ch in first)


def test_hash_secret_is_sha256_hex_digest() -> None:
    raw = new_secret()
    digest = hash_secret(raw)
    assert digest == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert len(digest) == 64
    assert hash_secret(raw) == digest


def test_auth_03_issue_then_consume_marks_token_used(db: Session) -> None:
    user = _user(db)
    raw = issue_one_time_token(db, user.id, TokenPurpose.VERIFY_EMAIL, timedelta(hours=24))

    token = consume_one_time_token(db, raw, TokenPurpose.VERIFY_EMAIL)

    assert token.user_id == user.id
    assert token.purpose is TokenPurpose.VERIFY_EMAIL
    assert token.used_at is not None
    db.expire_all()
    assert _token_row(db, raw).used_at is not None


def test_issue_stores_payload_and_expiry(db: Session) -> None:
    user = _user(db)
    before = datetime.now(UTC)
    raw = issue_one_time_token(
        db, user.id, TokenPurpose.GOOGLE_LINK, timedelta(minutes=30), {"google_sub": "sub-1"}
    )

    row = _token_row(db, raw)
    assert row.payload == {"google_sub": "sub-1"}
    assert row.used_at is None
    assert before + timedelta(minutes=29) < row.expires_at <= datetime.now(UTC) + timedelta(
        minutes=30
    )


def test_issue_rejects_non_positive_ttl(db: Session) -> None:
    user = _user(db)
    with pytest.raises(ValueError):
        issue_one_time_token(db, user.id, TokenPurpose.VERIFY_EMAIL, timedelta(0))


def test_auth_94_consume_twice_second_raises_link_invalid(db: Session) -> None:
    user = _user(db)
    raw = issue_one_time_token(db, user.id, TokenPurpose.RESET_PASSWORD, timedelta(hours=1))
    consume_one_time_token(db, raw, TokenPurpose.RESET_PASSWORD)

    with pytest.raises(AppError) as exc_info:
        consume_one_time_token(db, raw, TokenPurpose.RESET_PASSWORD)

    assert exc_info.value.code == LINK_INVALID
    assert exc_info.value.status == 400


def test_auth_94_expired_token_raises_link_invalid(db: Session) -> None:
    user = _user(db)
    raw = issue_one_time_token(db, user.id, TokenPurpose.VERIFY_EMAIL, timedelta(hours=1))
    row = _token_row(db, raw)
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()

    with pytest.raises(AppError) as exc_info:
        consume_one_time_token(db, raw, TokenPurpose.VERIFY_EMAIL)

    assert exc_info.value.code == LINK_INVALID
    db.expire_all()
    assert _token_row(db, raw).used_at is None


def test_auth_94_wrong_purpose_raises_link_invalid(db: Session) -> None:
    user = _user(db)
    raw = issue_one_time_token(db, user.id, TokenPurpose.VERIFY_EMAIL, timedelta(hours=1))

    with pytest.raises(AppError) as exc_info:
        consume_one_time_token(db, raw, TokenPurpose.RESET_PASSWORD)

    assert exc_info.value.code == LINK_INVALID
    db.expire_all()
    assert _token_row(db, raw).used_at is None
    # The token stays valid for its own purpose.
    assert consume_one_time_token(db, raw, TokenPurpose.VERIFY_EMAIL).used_at is not None


@pytest.mark.parametrize("raw", ["", "does-not-exist", "x" * 5000, "\ud800", "bad token!"])
def test_auth_94_unknown_token_raises_link_invalid(db: Session, raw: str) -> None:
    with pytest.raises(AppError) as exc_info:
        consume_one_time_token(db, raw, TokenPurpose.VERIFY_EMAIL)
    assert exc_info.value.code == LINK_INVALID


def test_table_never_contains_raw_token_value(db: Session) -> None:
    user = _user(db)
    raw = issue_one_time_token(
        db, user.id, TokenPurpose.GOOGLE_LINK, timedelta(hours=1), {"google_sub": "sub-2"}
    )
    consume_one_time_token(db, raw, TokenPurpose.GOOGLE_LINK)

    rows = db.execute(text("SELECT * FROM one_time_tokens")).mappings().all()
    assert rows
    for row in rows:
        for column, value in row.items():
            assert raw not in str(value), f"raw token found in column {column}"
    assert _token_row(db, raw).token_hash == hash_secret(raw)
