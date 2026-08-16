from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import polars as pl

from .connection import read_connection
from .models import METRICS


@lru_cache(maxsize=8)
def available_departments(analytics_dir: str) -> tuple[tuple[str, str], ...]:
    with read_connection(Path(analytics_dir)) as connection:
        rows = connection.execute(
            "SELECT CAST(d.department AS VARCHAR), d.department_name "
            "FROM departments d "
            "JOIN (SELECT DISTINCT CAST(department AS VARCHAR) AS department "
            "      FROM hourly_core) f "
            "ON CAST(d.department AS VARCHAR)=f.department "
            "ORDER BY CAST(d.department AS VARCHAR)"
        ).fetchall()
    return tuple((str(code), str(name)) for code, name in rows)


def _day_expression(time_basis: str) -> str:
    if time_basis == "local":
        return "local_date"
    if time_basis == "utc":
        return "CAST(observation_time_utc AT TIME ZONE 'UTC' AS DATE)"
    raise ValueError("Time basis must be local or utc")


@lru_cache(maxsize=32)
def department_year_bounds(
    analytics_dir: str, department: str, time_basis: str = "local"
) -> tuple[tuple[int, date, date], ...]:
    day = _day_expression(time_basis)
    with read_connection(Path(analytics_dir)) as connection:
        rows = connection.execute(
            f"""
            SELECT CAST(extract(year FROM {day}) AS INTEGER) AS observation_year,
                   min({day}) AS first_date, max({day}) AS last_date
            FROM hourly_core WHERE CAST(department AS VARCHAR)=?
            GROUP BY observation_year ORDER BY observation_year DESC
            """,
            [department],
        ).fetchall()
    return tuple((int(year), first, last) for year, first, last in rows)


@lru_cache(maxsize=8)
def available_years(analytics_dir: str, department: str) -> tuple[int, ...]:
    return tuple(year for year, _, _ in department_year_bounds(analytics_dir, department))


def calendar_year_range(
    analytics_dir: Path, department: str, year: int, time_basis: str = "local"
) -> tuple[date, date]:
    bounds = department_year_bounds(str(analytics_dir), department, time_basis)
    if not bounds:
        raise ValueError(f"Department {department} has no observations")
    available = {item[0]: item for item in bounds}
    if year not in available:
        raise ValueError(f"Department {department} has no observations in {year}")
    latest_year = bounds[0][0]
    end = available[year][2] if year == latest_year else date(year, 12, 31)
    return date(year, 1, 1), end


def department_has_observations(
    analytics_dir: Path, department: str, start_date: date, end_date: date,
    time_basis: str = "local",
) -> bool:
    day = _day_expression(time_basis)
    with read_connection(analytics_dir) as connection:
        return bool(connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM hourly_core "
            f"WHERE CAST(department AS VARCHAR)=? AND {day} BETWEEN ? AND ?)",
            [department, start_date, end_date],
        ).fetchone()[0])


def best_station_for_period(
    analytics_dir: Path, department: str, metric: str,
    start_date: date, end_date: date, time_basis: str = "local",
    quality_mode: str = "all",
) -> str | None:
    if metric not in METRICS:
        raise ValueError(f"Unsupported metric: {metric}")
    day = _day_expression(time_basis)
    quality = METRICS[metric].quality
    quality_condition = ""
    if quality_mode == "exclude_doubtful":
        quality_condition = f' AND ("{quality}" IS NULL OR "{quality}" <> 2)'
    elif quality_mode != "all":
        raise ValueError("Unsupported quality mode")
    with read_connection(analytics_dir) as connection:
        row = connection.execute(
            f"""
            WITH history AS (
                SELECT NUM_POSTE, min(observation_time_utc) AS first_observation,
                       max(observation_time_utc) AS last_observation
                FROM hourly_core WHERE CAST(department AS VARCHAR)=?
                GROUP BY NUM_POSTE
            ), period AS (
                SELECT NUM_POSTE, count(*) AS period_rows,
                       count("{metric}") FILTER (
                           WHERE "{metric}" IS NOT NULL{quality_condition}
                       ) AS metric_rows
                FROM hourly_core
                WHERE CAST(department AS VARCHAR)=? AND {day} BETWEEN ? AND ?
                GROUP BY NUM_POSTE
            )
            SELECT period.NUM_POSTE
            FROM period JOIN history USING (NUM_POSTE)
            ORDER BY metric_rows DESC,
                     date_diff('second', first_observation, last_observation) DESC,
                     period.NUM_POSTE
            LIMIT 1
            """,
            [department, department, start_date, end_date],
        ).fetchone()
    return str(row[0]) if row else None


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
