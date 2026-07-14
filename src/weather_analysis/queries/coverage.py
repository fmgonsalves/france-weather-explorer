from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from .connection import read_connection
from .models import DashboardFilters, MAX_CHART_POINTS, QueryLimitError
from .observations import _expressions, _where


PARIS = ZoneInfo("Europe/Paris")


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
