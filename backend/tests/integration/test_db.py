"""Integration tests for the database session helpers and the per-session test database."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.db import Base, get_db, get_engine, session_scope
from app.models import Base as ReexportedBase

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROBE_OUTPUT_ENV = "IR_TEST_DB_NAME_OUTPUT"
ROLLBACK_PROBE_TABLE = "rollback_probe"


def test_models_package_reexports_base() -> None:
    assert ReexportedBase is Base


def test_db_fixture_uses_isolated_test_database(db: Session) -> None:
    name = db.execute(text("SELECT current_database()")).scalar_one()
    assert name.startswith("ir_test_")
    output = os.environ.get(PROBE_OUTPUT_ENV)
    if output:
        Path(output).write_text(name)


def test_app_engine_points_to_test_database(db: Session) -> None:
    fixture_db = db.execute(text("SELECT current_database()")).scalar_one()
    with get_engine().connect() as connection:
        app_db = connection.execute(text("SELECT current_database()")).scalar_one()
    assert app_db == fixture_db


def test_test_database_is_migrated_to_head(db: Session, alembic_config: Config) -> None:
    head = ScriptDirectory.from_config(alembic_config).get_current_head()
    current = MigrationContext.configure(db.connection()).get_current_revision()
    assert current == head


def test_db_fixture_changes_are_visible_inside_the_test(db: Session) -> None:
    db.execute(text(f"CREATE TABLE {ROLLBACK_PROBE_TABLE} (id integer PRIMARY KEY)"))
    db.execute(text(f"INSERT INTO {ROLLBACK_PROBE_TABLE} (id) VALUES (1)"))  # noqa: S608 - test-generated table name
    db.commit()
    count = db.execute(text(f"SELECT count(*) FROM {ROLLBACK_PROBE_TABLE}")).scalar_one()  # noqa: S608 - test-generated table name
    assert count == 1


def test_db_fixture_rolls_back_after_each_test(db: Session) -> None:
    tables = inspect(db.connection()).get_table_names()
    assert ROLLBACK_PROBE_TABLE not in tables


def test_get_db_yields_session_and_closes_it(db: Session) -> None:
    generator = get_db()
    session = next(generator)
    assert isinstance(session, Session)
    assert session.execute(text("SELECT 1")).scalar_one() == 1
    with pytest.raises(StopIteration):
        next(generator)
    assert not session.in_transaction()


def test_session_scope_commits_on_success(db: Session) -> None:
    table = f"scope_probe_{uuid.uuid4().hex}"
    try:
        with session_scope() as session:
            session.execute(text(f"CREATE TABLE {table} (id integer PRIMARY KEY)"))
            session.execute(text(f"INSERT INTO {table} (id) VALUES (1)"))  # noqa: S608 - test-generated table name
        with session_scope() as session:
            count = session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608 - test-generated table name
        assert count == 1
    finally:
        with session_scope() as session:
            session.execute(text(f"DROP TABLE IF EXISTS {table}"))


def test_session_scope_rolls_back_on_error(db: Session) -> None:
    table = f"scope_probe_{uuid.uuid4().hex}"
    with pytest.raises(RuntimeError), session_scope() as session:
        session.execute(text(f"CREATE TABLE {table} (id integer PRIMARY KEY)"))
        raise RuntimeError("boom")
    with session_scope() as session:
        assert table not in inspect(session.connection()).get_table_names()


def test_test_database_is_dropped_after_pytest_session(
    tmp_path: Path, admin_database_url: str
) -> None:
    output = tmp_path / "db_name.txt"
    env = {**os.environ, PROBE_OUTPUT_ENV: str(output)}
    # The child session must create its own database from the server URL, not reuse ours.
    env["IR_DATABASE_URL"] = admin_database_url
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            f"{Path(__file__).resolve()}::test_db_fixture_uses_isolated_test_database",
        ],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    child_db = output.read_text()
    assert child_db.startswith("ir_test_")

    engine = create_engine(admin_database_url)
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": child_db}
            ).first()
    finally:
        engine.dispose()
    assert exists is None
