from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from .connection import read_connection
from .models import DashboardFilters, MAX_CHART_POINTS, QueryLimitError
from .observations import _expressions, _where


PARIS = ZoneInfo("Europe/Paris")
MINIMUM_TEMPERATURE_HOURS_PER_DAY = 18


def expected_hours(filters: DashboardFilters) -> int:
    end_exclusive = filters.end_date + timedelta(days=1)
    if filters.time_basis == "utc":
        return (end_exclusive - filters.start_date).days * 24
    start = datetime.combine(filters.start_date, time.min, tzinfo=PARIS)
    end = datetime.combine(end_exclusive, time.min, tzinfo=PARIS)
    return int((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 3600)


def fetch_summary(analytics_dir: Path, filters: DashboardFilters) -> dict:
    where, parameters = _where(filters)
    with read_connection(analytics_dir) as connection:
        populated = connection.execute(
            f"SELECT count(*) FROM hourly_core WHERE {where}", parameters
        ).fetchone()[0]
    expected = expected_hours(filters) * len(filters.stations)
    return {
        "period": f"{filters.start_date.isoformat()} – {filters.end_date.isoformat()}",
        "stations": len(filters.stations),
        "observations": int(populated),
        "expected": expected,
        "coverage": (100.0 * populated / expected) if expected else 0.0,
    }


def fetch_coverage_grid(analytics_dir: Path, filters: DashboardFilters) -> pl.DataFrame:
    primary = DashboardFilters(
        department=filters.department, metric=filters.metric, stations=(filters.stations[0],),
        start_date=filters.start_date, end_date=filters.end_date,
        time_basis=filters.time_basis, resolution=filters.resolution,
        daily_statistics=filters.daily_statistics, quality_mode=filters.quality_mode,
        exclude_selected_period_from_baseline=filters.exclude_selected_period_from_baseline,
    )
    _, day_expression, hour_expression = _expressions(primary)
    where, parameters = _where(primary)
    days = (primary.end_date - primary.start_date).days + 1
    if days * 24 > MAX_CHART_POINTS:
        raise QueryLimitError("Coverage heatmap exceeds 100,000 cells; choose a shorter period.")
    with read_connection(analytics_dir) as connection:
        observed = connection.execute(
            f"""
            SELECT {day_expression} AS observation_date, {hour_expression} AS observation_hour,
                   count(*)::UTINYINT AS observation_count
            FROM hourly_core WHERE {where}
            GROUP BY observation_date, observation_hour
            """,
            parameters,
        ).fetchall()
    lookup = {(row[0], int(row[1])): int(row[2]) for row in observed}
    rows = []
    current = primary.start_date
    while current <= primary.end_date:
        rows.extend(
            {"observation_date": current, "observation_hour": hour,
             "observation_count": lookup.get((current, hour), 0)}
            for hour in range(24)
        )
        current += timedelta(days=1)
    return pl.DataFrame(rows)


def fetch_station_map(analytics_dir: Path, filters: DashboardFilters) -> pl.DataFrame:
    _, day_expression, _ = _expressions(filters)
    metric = filters.metric
    quality = filters.metric_spec.quality
    quality_clause = ""
    if filters.quality_mode == "exclude_doubtful":
        quality_clause = f' AND ("{quality}" IS NULL OR "{quality}" <> 2)'
    hours = expected_hours(filters)
    with read_connection(analytics_dir) as connection:
        return connection.execute(
            f"""
            WITH latest AS (
                SELECT NUM_POSTE, arg_max(NOM_USUEL, observation_time_utc) AS NOM_USUEL,
                       arg_max(LAT, observation_time_utc) AS LAT,
                       arg_max(LON, observation_time_utc) AS LON,
                       arg_max(ALTI, observation_time_utc) AS ALTI,
                       min(observation_time_utc) AS first_observation_utc,
                       max(observation_time_utc) AS last_observation_utc
                FROM hourly_core WHERE CAST(department AS VARCHAR)=?
                GROUP BY NUM_POSTE
            ), period AS (
                SELECT NUM_POSTE, count(*) AS period_rows,
                       count(\"{metric}\") FILTER (
                           WHERE \"{metric}\" IS NOT NULL{quality_clause}
                       ) AS metric_rows
                FROM hourly_core
                WHERE CAST(department AS VARCHAR)=? AND {day_expression} BETWEEN ? AND ?
                GROUP BY NUM_POSTE
            )
            SELECT latest.*, coalesce(period.period_rows, 0)::UBIGINT AS period_rows,
                   coalesce(period.metric_rows, 0)::UBIGINT AS metric_rows,
                   least(100.0, 100.0 * coalesce(period.metric_rows, 0) / ?) AS coverage,
                   coalesce(period.period_rows, 0) > 0 AS active_in_period
            FROM latest LEFT JOIN period USING (NUM_POSTE)
            ORDER BY NOM_USUEL, NUM_POSTE
            """,
            [filters.department, filters.department, filters.start_date, filters.end_date, hours],
        ).pl()


def fetch_station_temperature_overview(
    analytics_dir: Path,
    start_date: date,
    end_date: date,
    time_basis: str = "local",
    quality_mode: str = "all",
    minimum_daily_hours: int = MINIMUM_TEMPERATURE_HOURS_PER_DAY,
) -> pl.DataFrame:
    """Return station-balanced period temperatures across materialized departments."""
    if start_date > end_date:
        raise ValueError("Start date must not be after end date")
    if not 1 <= minimum_daily_hours <= 25:
        raise ValueError("Minimum daily hours must be between 1 and 25")
    if time_basis == "local":
        day_expression = "local_date"
    elif time_basis == "utc":
        day_expression = "CAST(observation_time_utc AT TIME ZONE 'UTC' AS DATE)"
    else:
        raise ValueError("Time basis must be local or utc")
    quality_clause = ""
    if quality_mode == "exclude_doubtful":
        quality_clause = " AND (QT IS NULL OR QT <> 2)"
    elif quality_mode != "all":
        raise ValueError("Unsupported quality mode")

    with read_connection(analytics_dir) as connection:
        return connection.execute(
            f"""
            WITH eligible_hourly AS (
                SELECT CAST(department AS VARCHAR) AS department,
                       NUM_POSTE, NOM_USUEL, LAT, LON, ALTI,
                       observation_time_utc,
                       {day_expression} AS observation_date,
                       T
                FROM hourly_core
                WHERE {day_expression} BETWEEN ? AND ?
                  AND T IS NOT NULL{quality_clause}
            ), station_days AS (
                SELECT department, NUM_POSTE, observation_date,
                       avg(T)::DOUBLE AS daily_mean_temperature,
                       count(T)::UBIGINT AS valid_hours,
                       arg_max(NOM_USUEL, observation_time_utc) AS NOM_USUEL,
                       arg_max(LAT, observation_time_utc) AS LAT,
                       arg_max(LON, observation_time_utc) AS LON,
                       arg_max(ALTI, observation_time_utc) AS ALTI,
                       max(observation_time_utc) AS latest_observation_utc
                FROM eligible_hourly
                GROUP BY department, NUM_POSTE, observation_date
                HAVING count(T) >= ?
            )
            SELECT department, NUM_POSTE,
                   arg_max(NOM_USUEL, latest_observation_utc) AS NOM_USUEL,
                   arg_max(LAT, latest_observation_utc) AS LAT,
                   arg_max(LON, latest_observation_utc) AS LON,
                   arg_max(ALTI, latest_observation_utc) AS ALTI,
                   avg(daily_mean_temperature)::DOUBLE AS period_mean_temperature,
                   count(*)::UBIGINT AS qualifying_days,
                   sum(valid_hours)::UBIGINT AS valid_hours,
                   max(latest_observation_utc) AS latest_observation_utc
            FROM station_days
            GROUP BY department, NUM_POSTE
            ORDER BY department, NOM_USUEL, NUM_POSTE
            """,
            [start_date, end_date, minimum_daily_hours],
        ).pl()
