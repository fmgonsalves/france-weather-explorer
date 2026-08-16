from __future__ import annotations

from datetime import date

from weather_analysis.queries.models import DashboardFilters, METRICS


def global_state_from_filters(filters: DashboardFilters) -> dict:
    return {
        "department": filters.department,
        "metric": filters.metric,
        "stations": list(filters.stations),
        "start_date": filters.start_date.isoformat(),
        "end_date": filters.end_date.isoformat(),
        "time_basis": filters.time_basis,
        "quality_mode": filters.quality_mode,
    }


def time_series_state_from_filters(filters: DashboardFilters) -> dict:
    return {
        "resolution": filters.resolution,
        "daily_statistics": list(filters.daily_statistics),
    }


def historical_state_from_filters(filters: DashboardFilters) -> dict:
    return {
        "exclude_selected_period_from_baseline": (
            filters.exclude_selected_period_from_baseline
        ),
    }


def pending_global_state(
    department: str,
    metric: str,
    stations: list[str] | tuple[str, ...],
    start: str | date,
    end: str | date,
    time_basis: str,
    quality_mode: str,
) -> dict:
    spec = METRICS.get(metric)
    statistics = spec.default_daily_statistics if spec else ("average",)
    filters = DashboardFilters(
        department=department,
        metric=metric,
        stations=tuple(stations),
        start_date=_date_value(start),
        end_date=_date_value(end),
        time_basis=time_basis,
        resolution="hourly",
        daily_statistics=statistics,
        quality_mode=quality_mode,
    )
    return global_state_from_filters(filters)


def pending_time_series_state(
    metric: str, resolution: str, statistics: list[str] | tuple[str, ...]
) -> dict:
    if metric not in METRICS:
        raise ValueError(f"Unsupported metric: {metric}")
    if resolution not in {"hourly", "daily"}:
        raise ValueError("Resolution must be hourly or daily")
    values = tuple(statistics)
    if not values:
        raise ValueError("Select at least one daily statistic")
    allowed = METRICS[metric].daily_statistics
    invalid = [value for value in values if value not in allowed]
    if invalid:
        raise ValueError(
            f"{', '.join(invalid)!r} is not valid for {metric}; "
            f"choose {', '.join(allowed)}"
        )
    if len(set(values)) != len(values):
        raise ValueError("Daily statistics must be unique")
    return {"resolution": resolution, "daily_statistics": list(values)}


def pending_historical_state(baseline_mode: str) -> dict:
    if baseline_mode not in {"exclude", "include"}:
        raise ValueError("Unsupported historical baseline mode")
    return {
        "exclude_selected_period_from_baseline": baseline_mode != "include",
    }


def compose_dashboard_filters(
    global_state: dict,
    time_series_state: dict | None = None,
    historical_state: dict | None = None,
    *,
    normalize_time_options: bool = True,
) -> DashboardFilters:
    metric = str(global_state["metric"])
    spec = METRICS[metric]
    time_series_state = time_series_state or {}
    resolution = str(time_series_state.get("resolution", "hourly"))
    statistics = tuple(time_series_state.get(
        "daily_statistics", spec.default_daily_statistics,
    ))
    if normalize_time_options:
        if resolution not in {"hourly", "daily"}:
            resolution = "hourly"
        if not statistics or any(item not in spec.daily_statistics for item in statistics):
            statistics = spec.default_daily_statistics
    historical_state = historical_state or {}
    return DashboardFilters(
        department=str(global_state["department"]),
        metric=metric,
        stations=tuple(str(item) for item in global_state["stations"]),
        start_date=_date_value(global_state["start_date"]),
        end_date=_date_value(global_state["end_date"]),
        time_basis=global_state.get("time_basis", "local"),
        resolution=resolution,
        daily_statistics=statistics,
        quality_mode=global_state.get("quality_mode", "all"),
        exclude_selected_period_from_baseline=bool(
            historical_state.get("exclude_selected_period_from_baseline", True)
        ),
    )


def _date_value(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
