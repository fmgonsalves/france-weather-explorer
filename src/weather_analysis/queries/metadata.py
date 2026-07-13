from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import polars as pl

from .connection import read_connection
from .models import METRICS


@lru_cache(maxsize=8)
def available_years(analytics_dir: str, department: str = "44") -> tuple[int, ...]:
    with read_connection(Path(analytics_dir)) as connection:
        rows = connection.execute(
            "SELECT DISTINCT year FROM hourly_core "
            "WHERE CAST(department AS VARCHAR)=? ORDER BY year DESC",
            [department],
        ).fetchall()
    return tuple(int(row[0]) for row in rows)


@lru_cache(maxsize=8)
def metric_metadata(analytics_dir: str) -> dict[str, dict]:
    placeholders = ",".join("?" for _ in METRICS)
    with read_connection(Path(analytics_dir)) as connection:
        rows = connection.execute(
            f"SELECT source_mnemonic, official_description_fr, documentation_source "
            f"FROM metrics WHERE source_mnemonic IN ({placeholders})",
            list(METRICS),
        ).fetchall()
    official = {row[0]: row for row in rows}
    return {
        name: {
            "mnemonic": name,
            "label": spec.label,
            "unit": spec.unit,
            "quality": spec.quality,
            "description_fr": official.get(name, (None, None, None))[1],
            "documentation_source": official.get(name, (None, None, None))[2],
            "daily_statistics": spec.daily_statistics,
            "default_daily_statistics": spec.default_daily_statistics,
        }
        for name, spec in METRICS.items()
    }


def stations_for_period(
    analytics_dir: Path,
    department: str,
    start_date: date,
    end_date: date,
    time_basis: str = "local",
) -> pl.DataFrame:
    day_expression = (
        "local_date" if time_basis == "local"
        else "CAST(observation_time_utc AT TIME ZONE 'UTC' AS DATE)"
    )
    with read_connection(analytics_dir) as connection:
        return connection.execute(
            f"""
            SELECT NUM_POSTE, arg_max(NOM_USUEL, observation_time_utc) AS NOM_USUEL,
                   arg_max(LAT, observation_time_utc) AS LAT,
                   arg_max(LON, observation_time_utc) AS LON,
                   arg_max(ALTI, observation_time_utc) AS ALTI,
                   min(observation_time_utc) AS first_observation_utc,
                   max(observation_time_utc) AS last_observation_utc
            FROM hourly_core
            WHERE CAST(department AS VARCHAR)=? AND {day_expression} BETWEEN ? AND ?
            GROUP BY NUM_POSTE ORDER BY NOM_USUEL, NUM_POSTE
            """,
            [department, start_date, end_date],
        ).pl()
