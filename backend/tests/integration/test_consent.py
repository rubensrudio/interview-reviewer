"""Integration tests for the terms and privacy consent record (AUTH-15, CT-13)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.legal import consent
from app.legal.consent import has_current_consent, record_consent
from app.models.account import User


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


def _override_settings(monkeypatch: pytest.MonkeyPatch, **changes: str) -> None:
    patched = get_settings().model_copy(update=changes)
    monkeypatch.setattr(consent, "get_settings", lambda: patched)


def test_auth_15_record_consent_stores_current_versions_and_utc_timestamp(db: Session) -> None:
    settings = get_settings()
    user = _user(db)
    before = datetime.now(UTC)

    record_consent(db, user)

    assert user.terms_version == settings.terms_version
    assert user.privacy_version == settings.privacy_version
    assert user.terms_accepted_at is not None
    assert user.terms_accepted_at.utcoffset() == UTC.utcoffset(None)
    assert before <= user.terms_accepted_at <= datetime.now(UTC)


def test_auth_15_record_consent_is_persisted_after_commit(db: Session) -> None:
    settings = get_settings()
    user = _user(db)
    user_id = user.id

    record_consent(db, user)
    db.commit()
    db.expunge_all()

    loaded = db.execute(select(User).where(User.id == user_id)).scalar_one()
    assert loaded.terms_version == settings.terms_version
    assert loaded.privacy_version == settings.privacy_version
    assert loaded.terms_accepted_at is not None
    assert has_current_consent(loaded) is True


def test_auth_15_record_consent_overwrites_previous_acceptance(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(db)
    record_consent(db, user)
    first_accepted_at = user.terms_accepted_at

    _override_settings(monkeypatch, terms_version="terms-new", privacy_version="privacy-new")
    record_consent(db, user)

    assert user.terms_version == "terms-new"
    assert user.privacy_version == "privacy-new"
    assert user.terms_accepted_at is not None
    assert first_accepted_at is not None
    assert user.terms_accepted_at >= first_accepted_at
    assert has_current_consent(user) is True


def test_auth_15_has_current_consent_true_after_record(db: Session) -> None:
    user = _user(db)
    record_consent(db, user)

    assert has_current_consent(user) is True


def test_auth_15_has_current_consent_false_when_never_accepted(db: Session) -> None:
    user = _user(db)

    assert has_current_consent(user) is False


def test_auth_15_changing_terms_version_invalidates_consent(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(db)
    record_consent(db, user)

    _override_settings(monkeypatch, terms_version="terms-2099-01")

    assert has_current_consent(user) is False


def test_auth_15_changing_privacy_version_invalidates_consent(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(db)
    record_consent(db, user)

    _override_settings(monkeypatch, privacy_version="privacy-2099-01")

    assert has_current_consent(user) is False


def test_auth_15_versions_without_timestamp_are_not_current_consent(db: Session) -> None:
    settings = get_settings()
    user = User(
        email_normalized="unused@example.com",
        terms_version=settings.terms_version,
        privacy_version=settings.privacy_version,
        terms_accepted_at=None,
    )

    assert has_current_consent(user) is False
