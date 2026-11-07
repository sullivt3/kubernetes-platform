"""The ORM models and the Alembic migration history must describe the same schema."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.db.models import Base
from app.db.session import get_engine


def test_models_match_migrations() -> None:
    with get_engine().connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"Models and migrations differ; generate a migration. Diff: {diff}"
