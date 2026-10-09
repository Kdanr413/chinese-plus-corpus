"""Ordered, transactional SQL migration runner."""

from __future__ import annotations

from pathlib import Path

from .config import DatabaseSettings
from .db import connect


DEFAULT_MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


def initialize_database(
    settings: DatabaseSettings | None = None,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS,
) -> tuple[str, ...]:
    migrations = sorted(Path(migrations_dir).glob("*.sql"))
    applied: list[str] = []
    with connect(settings) as connection:
        with connection.transaction():
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        for migration in migrations:
            with connection.transaction():
                exists = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s",
                    (migration.name,),
                ).fetchone()
                if exists:
                    continue
                connection.execute(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (%s)",
                    (migration.name,),
                )
                applied.append(migration.name)
    return tuple(applied)
