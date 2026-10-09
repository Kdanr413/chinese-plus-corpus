"""Small psycopg boundary kept separate from Registry business rules."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DatabaseSettings
from .errors import DatabaseDependencyError


def _psycopg() -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise DatabaseDependencyError(
            "install the project dependencies with: pip install -r requirements.txt"
        ) from exc
    return psycopg, dict_row


@contextmanager
def connect(settings: DatabaseSettings | None = None) -> Iterator[Any]:
    psycopg, dict_row = _psycopg()
    configured = settings or DatabaseSettings.from_environment()
    kwargs = configured.connection_kwargs()
    schema = os.environ.get("CORPUS_DB_SCHEMA", "public")
    if schema != "public":
        if not re.fullmatch(r"p_[0-9a-f]{32}", schema):
            raise ValueError("invalid project database schema")
        kwargs["options"] = "-c search_path=" + schema
    with psycopg.connect(**kwargs, row_factory=dict_row) as connection:
        yield connection
