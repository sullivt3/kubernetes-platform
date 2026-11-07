"""Helpers to compare the live database schema revision with the code's head revision."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return config


@lru_cache
def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(session: Session) -> str | None:
    try:
        return session.scalar(text("SELECT version_num FROM alembic_version"))
    except ProgrammingError:
        session.rollback()
        return None


def migration_state(session: Session) -> dict[str, str | bool | None]:
    current = current_revision(session)
    head = head_revision()
    return {"current": current, "head": head, "up_to_date": current == head}
