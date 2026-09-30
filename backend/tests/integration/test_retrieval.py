"""Tests for knowledge retrieval by skill (CT-32): read-only FTS over `knowledge_items`."""

import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.knowledge.retrieval import SKILL_MAX_LENGTH, search_for_skill
from app.models.knowledge import KnowledgeItem, SourceRef

COLLECTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _add(db: Session, **overrides: Any) -> KnowledgeItem:
    data: dict[str, Any] = {
        "source_id": "python-docs",
        "url": "https://docs.python.org/3/library/asyncio.html",
        "title": "asyncio - Asynchronous I/O",
        "collected_at": COLLECTED_AT,
        "excerpt": "Coroutines declared with async/await syntax are the preferred way.",
        "skill_terms": ["Python", "concurrency"],
    }
    data.update(overrides)
    item = KnowledgeItem(**data)
    db.add(item)
    db.flush()
    return item


@pytest.fixture
def min_rank() -> Iterator[None]:
    """Reload settings so tests can override `knowledge_min_rank` via the environment."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_know_05_matching_skill_returns_source_ref_with_all_fields(db: Session) -> None:
    _add(db)

    refs = search_for_skill(db, "Python")

    assert refs == [
        SourceRef(
            url="https://docs.python.org/3/library/asyncio.html",
            title="asyncio - Asynchronous I/O",
            collected_at=COLLECTED_AT,
            excerpt="Coroutines declared with async/await syntax are the preferred way.",
        )
    ]


def test_know_90_empty_base_returns_empty_list(db: Session) -> None:
    assert search_for_skill(db, "Python") == []


def test_know_06_skill_without_matching_item_returns_empty_list(db: Session) -> None:
    _add(db)

    assert search_for_skill(db, "Kubernetes") == []


def test_know_05_search_works_with_network_blocked(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(db)
    db.commit()  # the fixture's connection is already open; only new connects are blocked

    def _blocked(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("network access attempted during retrieval")

    monkeypatch.setattr(socket.socket, "connect", _blocked)

    refs = search_for_skill(db, "Python")

    assert [ref.url for ref in refs] == ["https://docs.python.org/3/library/asyncio.html"]


def test_know_05_results_are_ordered_by_rank_and_limited(db: Session) -> None:
    _add(
        db,
        url="https://example.org/weak",
        title="Generic guide",
        excerpt="A long text that mentions docker only once among many other words here.",
        skill_terms=[],
    )
    _add(
        db,
        url="https://example.org/strong",
        title="Docker Docker basics",
        excerpt="Docker images, Docker containers and Docker volumes.",
        skill_terms=["Docker"],
    )
    _add(
        db,
        url="https://example.org/middle",
        title="Containers with Docker",
        excerpt="Docker containers explained.",
        skill_terms=["Docker"],
    )

    all_refs = search_for_skill(db, "Docker", limit=10)
    top = search_for_skill(db, "Docker")
    one = search_for_skill(db, "Docker", limit=1)

    assert [ref.url for ref in all_refs][0] == "https://example.org/strong"
    assert [ref.url for ref in all_refs][-1] == "https://example.org/weak"
    assert len(top) == 3
    assert [ref.url for ref in one] == ["https://example.org/strong"]


def test_know_06_items_below_min_rank_are_excluded(
    db: Session, monkeypatch: pytest.MonkeyPatch, min_rank: None
) -> None:
    _add(db)
    monkeypatch.setenv("IR_KNOWLEDGE_MIN_RANK", "1000")
    get_settings.cache_clear()

    assert search_for_skill(db, "Python") == []


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_returns_empty_list(db: Session, limit: int) -> None:
    _add(db)

    assert search_for_skill(db, "Python", limit=limit) == []


@pytest.mark.parametrize("skill", ["", "   ", "\x00\x01\x1f", "\x7f\x9f"])
def test_blank_or_control_only_skill_returns_empty_list(db: Session, skill: str) -> None:
    _add(db)

    assert search_for_skill(db, skill) == []


def test_skill_with_nul_and_controls_is_sanitized(db: Session) -> None:
    _add(db)

    assert [ref.url for ref in search_for_skill(db, "Py\x00thon")] == [
        "https://docs.python.org/3/library/asyncio.html"
    ]
    assert len(search_for_skill(db, "\x00Python\x07")) == 1


def test_skill_with_query_syntax_is_bound_as_parameter(db: Session) -> None:
    _add(db)

    refs = search_for_skill(db, "Python'); DROP TABLE knowledge_items; --")
    unbalanced = search_for_skill(db, '"unbalanced (python & | ! :*')

    assert isinstance(refs, list)
    assert isinstance(unbalanced, list)
    assert len(search_for_skill(db, "Python")) == 1


def test_oversized_skill_is_truncated(db: Session) -> None:
    _add(db)

    assert len(search_for_skill(db, "x or Python")) == 1
    # The OR clause lies past the length limit, so it is cut off before querying.
    assert search_for_skill(db, "x" * SKILL_MAX_LENGTH + " or Python") == []
