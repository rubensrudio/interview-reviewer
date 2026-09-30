"""Knowledge retrieval by skill (CT-32, KNOW-05, KNOW-06).

Full-text search over the already collected ``knowledge_items`` (DA-7): the skill is turned into
a ``websearch_to_tsquery('english', ...)`` and matches are ordered by ``ts_rank``, keeping only
those at or above ``knowledge_min_rank``. This module only reads the database; it never makes a
network request and never calls the LLM.

An empty result means "no verified source" for the skill; callers mark the question accordingly.
The returned excerpts are untrusted public text: consumers that put them in a prompt must wrap
them with ``wrap_untrusted`` (CT-20).
"""

import re

from sqlalchemy import cast, func, literal, select
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.knowledge import KnowledgeItem, SourceRef

SEARCH_CONFIG = "english"
SKILL_MAX_LENGTH = 200
MAX_LIMIT = 50
# NUL is dropped (PostgreSQL text cannot hold it); other C0/C1 control characters and lone
# surrogates (category Cs, which cannot be encoded as UTF-8) become spaces.
_NUL_RE = re.compile("\x00")
_CONTROL_RE = re.compile("[\x01-\x1f\x7f-\x9f\ud800-\udfff]")


def search_for_skill(db: Session, skill: str, limit: int = 3) -> list[SourceRef]:
    """Return up to ``limit`` knowledge items relevant to ``skill``, best match first."""
    term = _sanitize(skill)
    if not term or limit <= 0:
        return []

    query = func.websearch_to_tsquery(cast(literal(SEARCH_CONFIG), REGCONFIG), term)
    rank = func.ts_rank(KnowledgeItem.search, query)
    statement = (
        select(KnowledgeItem)
        .where(KnowledgeItem.search.bool_op("@@")(query))
        .where(rank >= get_settings().knowledge_min_rank)
        .order_by(rank.desc(), KnowledgeItem.collected_at.desc(), KnowledgeItem.url)
        .limit(min(limit, MAX_LIMIT))
    )
    return [item.to_source_ref() for item in db.scalars(statement)]


def _sanitize(skill: str) -> str:
    cleaned = _CONTROL_RE.sub(" ", _NUL_RE.sub("", skill))
    return cleaned[:SKILL_MAX_LENGTH].strip()
