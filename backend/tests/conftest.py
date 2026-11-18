from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ledgerline.settings import settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _alembic(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["url"] = url
    return cfg


@pytest.fixture(scope="session")
def db_url() -> str:
    """Fresh test database, migrated to head; skips the DB tests if Postgres isn't running."""
    url = make_url(settings.test_database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except OperationalError as e:
        pytest.skip(f"Postgres not reachable ({e.orig}); run `docker compose up -d postgres`")
    finally:
        admin.dispose()
    rendered = url.render_as_string(hide_password=False)
    command.upgrade(_alembic(rendered), "head")
    return rendered


@pytest.fixture(scope="session")
def engine(db_url):
    eng = create_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """Each test runs in a transaction that is rolled back afterwards."""
    with engine.connect() as conn:
        trans = conn.begin()
        with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        trans.rollback()


@pytest.fixture
def alembic_cfg(db_url) -> Config:
    return _alembic(db_url)
