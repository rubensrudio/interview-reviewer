"""Terms of use and privacy policy consent record (AUTH-15, LGPD).

Stores which document versions the user accepted and when (UTC). Callers own the
transaction (CT-2): `record_consent` flushes but never commits.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.account import User


def record_consent(db: Session, user: User) -> None:
    """Record acceptance of the current terms and privacy policy versions."""
    settings = get_settings()
    user.terms_version = settings.terms_version
    user.privacy_version = settings.privacy_version
    user.terms_accepted_at = datetime.now(UTC)
    db.add(user)
    db.flush()


def has_current_consent(user: User) -> bool:
    """True only when the accepted terms and privacy versions match the current ones."""
    settings = get_settings()
    return (
        user.terms_accepted_at is not None
        and user.terms_version == settings.terms_version
        and user.privacy_version == settings.privacy_version
    )
