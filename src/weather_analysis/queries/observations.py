from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from .connection import read_connection
from .models import DashboardFilters, MAX_CHART_POINTS, QueryLimitError


AGGREGATIONS = {
    "average": "avg",
    "minimum": "min",
    "maximum": "max",
    "total": "sum",
}


def _expressions(filters: DashboardFilters) -> tuple[str, str, str]:
    if filters.time_basis == "local":
        return (
            "observation_time_utc AT TIME ZONE 'Europe/Paris'",
            "local_date",
            "local_hour",
        )
    return (
        "observation_time_utc AT TIME ZONE 'UTC'",
        "CAST(observation_time_utc AT TIME ZONE 'UTC' AS DATE)",
        "CAST(extract(hour FROM observation_time_utc AT TIME ZONE 'UTC') AS UTINYINT)",
    )


def _where(filters: DashboardFilters) -> tuple[str, list]:
    _, day_expression, _ = _expressions(filters)
    station_slots = ",".join("?" for _ in filters.stations)
    quality = filters.metric_spec.quality
    quality_clause = ""
    if filters.quality_mode == "exclude_doubtful":
        quality_clause = f' AND ("{quality}" IS NULL OR "{quality}" <> 2)'
    sql = (
        f"CAST(department AS VARCHAR)=? AND NUM_POSTE IN ({station_slots}) "
        f"AND {day_expression} BETWEEN ? AND ? AND \"{filters.metric}\" IS NOT NULL"
        f"{quality_clause}"
    )
    return sql, [filters.department, *filters.stations, filters.start_date, filters.end_date]


def _check_limit(frame: pl.DataFrame) -> pl.DataFrame:
    if frame.height > MAX_CHART_POINTS:
        raise QueryLimitError(
            f"The query returned {frame.height:,} points; limit is {MAX_CHART_POINTS:,}. "
            "Choose fewer stations, a shorter period, or daily resolution."
        )
    return frame


def fetch_timeseries(analytics_dir: Path, filters: DashboardFilters) -> pl.DataFrame:
    time_expression, day_expression, _ = _expressions(filters)
    where, parameters = _where(filters)
    metric = filters.metric
    quality = filters.metric_spec.quality
    if filters.resolution == "hourly":
        sql = f"""
            SELECT NUM_POSTE, NOM_USUEL, {time_expression} AS observation_time,
                   {day_expression} AS observation_date,
                   \"{metric}\" AS value, \"{quality}\" AS quality_code,
                   1::UBIGINT AS contributing_count
            FROM hourly_core WHERE {where}
            ORDER BY NUM_POSTE, observation_time
        """
    else:
        aggregate_columns = ",\n".join(
            f'{AGGREGATIONS[statistic]}(\"{metric}\") AS \"{statistic}\"'
            for statistic in filters.daily_statistics
        )
        sql = f"""
            SELECT NUM_POSTE, arg_max(NOM_USUEL, observation_time_utc) AS NOM_USUEL,
                   {day_expression} AS observation_date,
                   {aggregate_columns},
                   count(\"{metric}\")::UBIGINT AS contributing_count
            FROM hourly_core WHERE {where}
            GROUP BY NUM_POSTE, {day_expression}
            ORDER BY NUM_POSTE, observation_date
        """
    with read_connection(analytics_dir) as connection:
        frame = connection.execute(sql, parameters).pl()
    if filters.resolution == "hourly":
        frame = frame.with_columns(pl.lit("hourly").alias("statistic"))
    else:
        frame = frame.unpivot(
            on=list(filters.daily_statistics),
            index=["NUM_POSTE", "NOM_USUEL", "observation_date", "contributing_count"],
            variable_name="statistic", value_name="value",
        ).sort(["NUM_POSTE", "observation_date", "statistic"])
    return _check_limit(frame)


def filters_from_values(
    metric: str, stations: list[str] | tuple[str, ...], start: str, end: str,
    time_basis: str, resolution: str, daily_statistics: list[str] | tuple[str, ...],
    quality_mode: str,
) -> DashboardFilters:
    return DashboardFilters(
        department="44", metric=metric, stations=tuple(stations),
        start_date=date.fromisoformat(start), end_date=date.fromisoformat(end),
        time_basis=time_basis, resolution=resolution,
        daily_statistics=tuple(daily_statistics), quality_mode=quality_mode,
    )
