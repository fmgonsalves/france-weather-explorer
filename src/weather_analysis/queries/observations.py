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


def fetch_historical_comparison(
    analytics_dir: Path, filters: DashboardFilters
) -> pl.DataFrame:
    """Return selected daily values with a month/day historical station baseline."""
    _, day_expression, _ = _expressions(filters)
    metric = filters.metric
    quality = filters.metric_spec.quality
    station_slots = ",".join("?" for _ in filters.stations)
    quality_clause = ""
    if filters.quality_mode == "exclude_doubtful":
        quality_clause = f' AND ("{quality}" IS NULL OR "{quality}" <> 2)'
    common = (
        f"CAST(department AS VARCHAR)=? AND NUM_POSTE IN ({station_slots}) "
        f'AND "{metric}" IS NOT NULL{quality_clause}'
    )
    exclusion = ""
    if filters.exclude_selected_period_from_baseline:
        exclusion = f" AND NOT ({day_expression} BETWEEN ? AND ?)"

    station_parameters = [filters.department, *filters.stations]
    selected_parameters = [
        filters.department, *filters.stations, filters.start_date, filters.end_date,
    ]
    historical_parameters = [filters.department, *filters.stations]
    if filters.exclude_selected_period_from_baseline:
        historical_parameters.extend([filters.start_date, filters.end_date])

    if metric == "RR1":
        selected_columns = """
            sum("RR1") AS selected_total,
            NULL::DOUBLE AS selected_average,
            NULL::DOUBLE AS selected_minimum,
            NULL::DOUBLE AS selected_maximum
        """
        historical_columns = "sum(\"RR1\") AS daily_total"
        envelope_columns = """
            min(daily_total) AS absolute_minimum,
            max(daily_total) AS absolute_maximum,
            NULL::DOUBLE AS average_minimum,
            NULL::DOUBLE AS average_maximum,
            avg(daily_total) AS historical_average_total
        """
    else:
        selected_columns = f"""
            NULL::DOUBLE AS selected_total,
            avg(\"{metric}\") AS selected_average,
            min(\"{metric}\") AS selected_minimum,
            max(\"{metric}\") AS selected_maximum
        """
        historical_columns = (
            f'min(\"{metric}\") AS daily_minimum, '
            f'max(\"{metric}\") AS daily_maximum'
        )
        envelope_columns = """
            min(daily_minimum) AS absolute_minimum,
            max(daily_maximum) AS absolute_maximum,
            avg(daily_minimum) AS average_minimum,
            avg(daily_maximum) AS average_maximum,
            NULL::DOUBLE AS historical_average_total
        """

    sql = f"""
        WITH selected_stations AS (
            SELECT NUM_POSTE, arg_max(NOM_USUEL, observation_time_utc) AS NOM_USUEL
            FROM hourly_core WHERE {common}
            GROUP BY NUM_POSTE
        ), selected_daily AS (
            SELECT NUM_POSTE, {day_expression} AS observation_date,
                   {selected_columns},
                   count(\"{metric}\")::UBIGINT AS selected_contributing_hours
            FROM hourly_core
            WHERE {common} AND {day_expression} BETWEEN ? AND ?
            GROUP BY NUM_POSTE, {day_expression}
        ), historical_daily AS (
            SELECT NUM_POSTE, {day_expression} AS historical_date,
                   {historical_columns}
            FROM hourly_core
            WHERE {common}{exclusion}
            GROUP BY NUM_POSTE, {day_expression}
        ), historical_envelope AS (
            SELECT NUM_POSTE, strftime(historical_date, '%m-%d') AS month_day,
                   {envelope_columns},
                   count(*)::UBIGINT AS historical_days,
                   count(DISTINCT year(historical_date))::UBIGINT AS historical_years
            FROM historical_daily GROUP BY NUM_POSTE, month_day
        ), selected_dates AS (
            SELECT CAST(value AS DATE) AS observation_date
            FROM generate_series(?::DATE, ?::DATE, INTERVAL 1 DAY) dates(value)
        )
        SELECT stations.NUM_POSTE, stations.NOM_USUEL, dates.observation_date,
               selected.selected_total, selected.selected_average,
               selected.selected_minimum, selected.selected_maximum,
               selected.selected_contributing_hours,
               envelope.absolute_minimum, envelope.absolute_maximum,
               envelope.average_minimum, envelope.average_maximum,
               envelope.historical_average_total,
               envelope.historical_days, envelope.historical_years,
               ?::BOOLEAN AS baseline_excludes_selected_period
        FROM selected_stations stations CROSS JOIN selected_dates dates
        LEFT JOIN selected_daily selected USING (NUM_POSTE, observation_date)
        LEFT JOIN historical_envelope envelope
          ON envelope.NUM_POSTE = stations.NUM_POSTE
         AND envelope.month_day = strftime(dates.observation_date, '%m-%d')
        ORDER BY stations.NUM_POSTE, dates.observation_date
    """
    parameters = [
        *station_parameters,
        *selected_parameters,
        *historical_parameters,
        filters.start_date,
        filters.end_date,
        filters.exclude_selected_period_from_baseline,
    ]
    with read_connection(analytics_dir) as connection:
        frame = connection.execute(sql, parameters).pl()
    return _check_limit(frame)


def filters_from_values(
    department: str, metric: str, stations: list[str] | tuple[str, ...], start: str, end: str,
    time_basis: str, resolution: str, daily_statistics: list[str] | tuple[str, ...],
    quality_mode: str, exclude_selected_period_from_baseline: bool = True,
) -> DashboardFilters:
    return DashboardFilters(
        department=department, metric=metric, stations=tuple(stations),
        start_date=date.fromisoformat(start), end_date=date.fromisoformat(end),
        time_basis=time_basis, resolution=resolution,
        daily_statistics=tuple(daily_statistics), quality_mode=quality_mode,
        exclude_selected_period_from_baseline=exclude_selected_period_from_baseline,
    )
