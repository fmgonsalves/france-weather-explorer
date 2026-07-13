from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb


REQUIRED_VIEWS = {"hourly_core", "stations", "metrics", "departments"}


def database_path(analytics_dir: Path) -> Path:
    return Path(analytics_dir) / "weather.duckdb"


def validate_catalog(analytics_dir: Path) -> Path:
    path = database_path(analytics_dir)
    if not path.is_file():
        raise FileNotFoundError(f"DuckDB catalog not found: {path}")
    connection = duckdb.connect(str(path), read_only=True)
    try:
        present = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
    finally:
        connection.close()
    missing = sorted(REQUIRED_VIEWS - present)
    if missing:
        raise ValueError(f"DuckDB catalog is missing required views: {', '.join(missing)}")
    return path


@contextmanager
def read_connection(analytics_dir: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    path = validate_catalog(analytics_dir)
    connection = duckdb.connect(str(path), read_only=True)
    connection.execute("SET TimeZone='UTC'")
    try:
        yield connection
    finally:
        connection.close()
