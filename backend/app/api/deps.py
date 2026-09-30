"""Shared FastAPI dependencies for API routes.

Every private route must depend on ``CurrentUser`` (or ``Depends(get_current_user)``), which
enforces the session cookie, CSRF on non-GET methods and a current terms acceptance.
``CurrentUserPendingTerms`` is reserved for the few routes a user must reach before accepting
the terms (e.g. ``POST /api/account/terms``).
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.auth.sessions import get_current_user, get_current_user_pending_terms
from app.db import get_db
from app.models.account import User

DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentUserPendingTerms = Annotated[User, Depends(get_current_user_pending_terms)]

__all__ = [
    "CurrentUser",
    "CurrentUserPendingTerms",
    "DbSession",
    "get_current_user",
    "get_current_user_pending_terms",
    "get_db",
]
