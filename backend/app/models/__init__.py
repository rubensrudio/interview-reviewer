"""ORM models. Every model module must be imported here so Alembic sees its tables."""

from app.db import Base

__all__ = ["Base"]
