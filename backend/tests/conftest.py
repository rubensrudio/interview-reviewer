"""Shared pytest fixtures.

Integration tests get a PostgreSQL database of their own per pytest session
(`ir_test_<uuid>`), migrated with `alembic upgrade head` and dropped at the end, so parallel
runs (e.g. tasks in separate worktrees) never share state (DA-10). The database is only
created when a test requests `db` (or another database fixture), so unit tests stay offline.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.command import upgrade
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine, reset_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB_PREFIX = "ir_test_"

# Placeholder secrets so `get_settings()` loads in tests; real values come from the environment.
TEST_REQUIRED_ENV = {
    "IR_THROTTLE_SECRET": "test-throttle-secret",
    "IR_OIDC_STATE_SECRET": "test-oidc-state-secret",
}


@pytest.fixture(scope="session")
def admin_database_url() -> str:
    """URL of the configured server database, used only to create and drop test databases."""
    missing = {k: v for k, v in TEST_REQUIRED_ENV.items() if k not in os.environ}
    with pytest.MonkeyPatch.context() as patch:
        for name, value in missing.items():
            patch.setenv(name, value)
        get_settings.cache_clear()
        url = get_settings().database_url
    get_settings.cache_clear()
    return url


@pytest.fixture(scope="session")
def test_database_url(admin_database_url: str) -> Iterator[str]:
    name = f"{TEST_DB_PREFIX}{uuid.uuid4().hex}"
    url = make_url(admin_database_url).set(database=name).render_as_string(hide_password=False)

    admin_engine = create_engine(admin_database_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))

        with pytest.MonkeyPatch.context() as patch:
            for key, value in TEST_REQUIRED_ENV.items():
                if key not in os.environ:
                    patch.setenv(key, value)
            patch.setenv("IR_DATABASE_URL", url)
            get_settings.cache_clear()
            reset_engine()
            try:
                yield url
            finally:
                reset_engine()
                get_settings.cache_clear()

        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session")
def alembic_config(test_database_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # ConfigParser interpolation: a literal "%" (e.g. URL-encoded password) must be doubled.
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
def migrated_database(alembic_config: Config, test_database_url: str) -> str:
    upgrade(alembic_config, "head")
    return test_database_url


@pytest.fixture
def db(migrated_database: str) -> Iterator[Session]:
    """Session inside an outer transaction that is rolled back after the test.

    `session.commit()` in the code under test only releases a savepoint, so nothing persists.
    """
    connection = get_engine().connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
