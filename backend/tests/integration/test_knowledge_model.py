"""Tests for the knowledge base table (migration 0004_knowledge), its model and `SourceRef`."""

from datetime import UTC, datetime
from typing import Any

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import KnowledgeItem as ExportedKnowledgeItem
from app.models import SourceRef as ExportedSourceRef
from app.models.knowledge import EXCERPT_MAX_LENGTH, KnowledgeItem, SourceRef

COLLECTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _item(**overrides: Any) -> KnowledgeItem:
    data: dict[str, Any] = {
        "source_id": "python-docs",
        "url": "https://docs.python.org/3/library/asyncio.html",
        "title": "asyncio - Asynchronous I/O",
        "collected_at": COLLECTED_AT,
        "excerpt": "Coroutines declared with async/await syntax are the preferred way.",
        "skill_terms": ["Python", "concurrency"],
    }
    data.update(overrides)
    return KnowledgeItem(**data)


# --- SourceRef (pure pydantic, no database) ---


def test_models_package_exports_knowledge_models() -> None:
    assert ExportedKnowledgeItem is KnowledgeItem
    assert ExportedSourceRef is SourceRef


def test_know_03_source_ref_holds_url_title_date_and_excerpt() -> None:
    ref = SourceRef(
        url="https://example.org/a", title="A", collected_at=COLLECTED_AT, excerpt="quote"
    )
    assert (ref.url, ref.title, ref.collected_at, ref.excerpt) == (
        "https://example.org/a",
        "A",
        COLLECTED_AT,
        "quote",
    )


def test_know_08_source_ref_json_roundtrip_keeps_all_fields() -> None:
    ref = SourceRef(
        url="https://example.org/a", title="A", collected_at=COLLECTED_AT, excerpt="quote"
    )
    dumped = ref.model_dump(mode="json")
    assert set(dumped) == {"url", "title", "collected_at", "excerpt"}
    assert SourceRef.model_validate(dumped) == ref


@pytest.mark.parametrize("missing", ["url", "title", "collected_at", "excerpt"])
def test_know_03_source_ref_requires_every_field(missing: str) -> None:
    data: dict[str, Any] = {
        "url": "https://example.org/a",
        "title": "A",
        "collected_at": COLLECTED_AT,
        "excerpt": "quote",
    }
    del data[missing]
    with pytest.raises(ValidationError):
        SourceRef.model_validate(data)


def test_source_ref_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SourceRef.model_validate(
            {
                "url": "https://example.org/a",
                "title": "A",
                "collected_at": COLLECTED_AT,
                "excerpt": "quote",
                "score": 1,
            }
        )


def test_source_ref_rejects_excerpt_over_limit() -> None:
    with pytest.raises(ValidationError):
        SourceRef(
            url="https://example.org/a",
            title="A",
            collected_at=COLLECTED_AT,
            excerpt="x" * (EXCERPT_MAX_LENGTH + 1),
        )


def test_knowledge_item_to_source_ref() -> None:
    item = _item()
    ref = item.to_source_ref()
    assert ref == SourceRef(
        url=item.url, title=item.title, collected_at=item.collected_at, excerpt=item.excerpt
    )


# --- knowledge_items table ---


def test_know_03_insert_fills_search_vector(db: Session) -> None:
    item = _item()
    db.add(item)
    db.commit()
    item_id = item.id
    db.expunge_all()

    loaded = db.get(KnowledgeItem, item_id)
    assert loaded is not None
    assert loaded.url == "https://docs.python.org/3/library/asyncio.html"
    assert loaded.collected_at == COLLECTED_AT
    assert loaded.skill_terms == ["Python", "concurrency"]
    assert loaded.search
    # Title, excerpt and skill terms all feed the generated vector.
    for query in ("asynchronous", "coroutines", "concurrency"):
        found = db.scalar(
            select(func.count())
            .select_from(KnowledgeItem)
            .where(
                KnowledgeItem.id == item_id,
                KnowledgeItem.search.bool_op("@@")(func.websearch_to_tsquery("english", query)),
            )
        )
        assert found == 1, query


def test_know_03_search_is_updated_when_item_changes(db: Session) -> None:
    item = _item()
    db.add(item)
    db.flush()
    item.excerpt = "Generators and iterators."
    db.flush()
    matches = db.scalar(
        select(func.count())
        .select_from(KnowledgeItem)
        .where(
            KnowledgeItem.id == item.id,
            KnowledgeItem.search.bool_op("@@")(func.websearch_to_tsquery("english", "iterator")),
        )
    )
    assert matches == 1


def test_know_03_unique_url_blocks_duplicates(db: Session) -> None:
    db.add(_item())
    db.flush()
    db.add(_item(title="Another title"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_skill_terms_default_to_empty_list(db: Session) -> None:
    item = KnowledgeItem(
        source_id="s",
        url="https://example.org/no-terms",
        title="No terms",
        collected_at=COLLECTED_AT,
        excerpt="Plain excerpt.",
    )
    db.add(item)
    db.flush()
    db.expire(item)
    assert item.skill_terms == []
    assert item.search


def test_excerpt_longer_than_limit_is_rejected(db: Session) -> None:
    db.add(_item(excerpt="x" * (EXCERPT_MAX_LENGTH + 1)))
    with pytest.raises(IntegrityError):
        db.flush()


def test_search_column_is_generated_tsvector_with_gin_index(db: Session) -> None:
    column = db.execute(
        text(
            "SELECT data_type, is_generated FROM information_schema.columns "
            "WHERE table_name = 'knowledge_items' AND column_name = 'search'"
        )
    ).one()
    assert tuple(column) == ("tsvector", "ALWAYS")
    definition = db.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_knowledge_items_search'")
    ).scalar_one()
    assert "USING gin (search)" in definition


def test_know_03_knowledge_items_has_no_user_data(db: Session) -> None:
    columns = {c["name"] for c in inspect(db.connection()).get_columns("knowledge_items")}
    assert columns == {
        "id",
        "source_id",
        "url",
        "title",
        "collected_at",
        "excerpt",
        "skill_terms",
        "search",
    }
    foreign_keys = inspect(db.connection()).get_foreign_keys("knowledge_items")
    assert foreign_keys == []


def test_no_pgvector_extension(db: Session) -> None:
    installed = db.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'"))
    assert installed.scalar_one() == 0


# --- migration chain ---


def test_migration_0004_downgrade_and_upgrade(
    migrated_database: str, alembic_config: Config
) -> None:
    head = ScriptDirectory.from_config(alembic_config).get_current_head()
    engine = create_engine(migrated_database)
    try:
        downgrade(alembic_config, "0003")
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == "0003"
            assert "knowledge_items" not in inspect(connection).get_table_names()
            functions = connection.execute(
                text("SELECT count(*) FROM pg_proc WHERE proname = 'knowledge_terms_text'")
            ).scalar_one()
            assert functions == 0
    finally:
        upgrade(alembic_config, "head")

    try:
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == head
            assert "knowledge_items" in inspect(connection).get_table_names()
    finally:
        engine.dispose()
