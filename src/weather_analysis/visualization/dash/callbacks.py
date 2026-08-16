from __future__ import annotations

from datetime import date
from pathlib import Path

from dash import ALL, Input, Output, State, ctx, dcc, no_update

from weather_analysis.geography import (
    build_temperature_surface,
    load_administrative_geography,
)
from weather_analysis.queries.coverage import (
    fetch_coverage_grid,
    fetch_station_map,
    fetch_station_temperature_overview,
    fetch_summary,
)
from weather_analysis.queries.metadata import (
    available_departments,
    available_years,
    best_station_for_period,
    calendar_year_range,
    department_has_observations,
    metric_metadata,
    stations_for_period,
)
from weather_analysis.queries.models import DashboardFilters, METRICS
from weather_analysis.queries.observations import (
    fetch_historical_comparison,
    fetch_timeseries,
)

from .figures import (
    coverage_heatmap_figure,
    empty_figure,
    historical_comparison_figure,
    station_map_figure,
    station_temperature_overview_figure,
    time_series_figure,
)
from .state import (
    compose_dashboard_filters,
    pending_global_state,
    pending_historical_state,
    pending_time_series_state,
)


def _date_value(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value[:10])


def _station_options(frame) -> list[dict[str, str]]:
    return [
        {"label": f"{row['NOM_USUEL']} — {row['NUM_POSTE']}", "value": row["NUM_POSTE"]}
        for row in frame.iter_rows(named=True)
    ]


def global_filters_are_dirty(
    applied: dict,
    department: str,
    metric: str,
    stations: list[str] | None,
    start: str | date | None,
    end: str | date | None,
    time_basis: str,
    quality_mode: str,
) -> bool:
    if not applied or not start or not end:
        return True
    return applied != pending_global_state(
        department, metric, stations or [], start, end, time_basis, quality_mode,
    )


def time_series_options_are_dirty(
    applied: dict,
    metric: str,
    resolution: str,
    statistics: list[str] | None,
) -> bool:
    if not applied:
        return True
    return applied != pending_time_series_state(
        metric, resolution, statistics or [],
    )


def historical_options_are_dirty(applied: dict, baseline_mode: str) -> bool:
    if not applied:
        return True
    return applied != pending_historical_state(baseline_mode)


def contextual_store_updates(
    trigger: str,
    applied_global: dict,
    applied_time: dict,
    applied_historical: dict,
    next_global: dict,
    metric: str,
    resolution: str,
    statistics: list[str] | tuple[str, ...],
    baseline_mode: str,
):
    global_output = no_update if next_global == applied_global else next_global
    if trigger == "apply-global":
        return global_output, no_update, no_update
    if trigger == "apply-time-series":
        next_time = pending_time_series_state(metric, resolution, statistics)
        time_output = no_update if next_time == applied_time else next_time
        return global_output, time_output, no_update
    if trigger == "apply-historical":
        next_historical = pending_historical_state(baseline_mode)
        historical_output = (
            no_update if next_historical == applied_historical else next_historical
        )
        return global_output, no_update, historical_output
    return no_update, no_update, no_update


def resolve_department_period(
    analytics_dir: Path, department: str, time_basis: str,
    period_mode: str, selected_year: int | str | None,
    start: str | date | None, end: str | date | None,
) -> tuple[tuple[int, ...], int | None, str, date | str | None, date | str | None]:
    years = available_years(str(analytics_dir), department)
    if not years:
        return years, None, "year", None, None
    try:
        candidate = int(selected_year) if selected_year is not None else None
    except (TypeError, ValueError):
        candidate = None
    year = candidate if candidate in years else years[0]
    if period_mode == "custom" and start and end:
        if department_has_observations(
            analytics_dir, department, _date_value(start), _date_value(end), time_basis,
        ):
            return years, year, "custom", start, end
        year = years[0]
        period_mode = "year"
    first, last = calendar_year_range(analytics_dir, department, year, time_basis)
    return years, year, period_mode or "year", first, last


def _validated_global_state(
    analytics_dir: Path,
    department_names: dict[str, str],
    department: str,
    metric: str,
    stations: list[str] | None,
    start: str | date | None,
    end: str | date | None,
    time_basis: str,
    quality_mode: str,
) -> dict:
    if department not in department_names:
        raise ValueError(f"Department {department} is not materialized")
    if not start or not end:
        raise ValueError("Select both start and end dates")
    state = pending_global_state(
        department, metric, stations or [], start, end, time_basis, quality_mode,
    )
    valid_stations = {
        row["NUM_POSTE"]
        for row in stations_for_period(
            analytics_dir,
            department,
            _date_value(start),
            _date_value(end),
            time_basis,
        ).iter_rows(named=True)
    }
    if not stations or any(station not in valid_stations for station in stations):
        raise ValueError("Select stations available in the chosen department and period")
    return state


def register_callbacks(app, analytics_dir: Path, geography_dir: Path) -> None:
    department_names = dict(available_departments(str(analytics_dir)))

    @app.callback(
        Output("year-wrapper", "className"), Output("date-wrapper", "className"),
        Input("period-mode", "value"),
    )
    def toggle_period(mode):
        return ("", "is-hidden") if mode == "year" else ("is-hidden", "")

    @app.callback(
        Output("year", "options"), Output("year", "value"),
        Output("period-mode", "value"),
        Output("date-range", "start_date"), Output("date-range", "end_date"),
        Input("department", "value"), Input("time-basis", "value"),
        State("period-mode", "value"), State("year", "value"),
        State("date-range", "start_date"), State("date-range", "end_date"),
    )
    def synchronize_department_period(
        department, time_basis, period_mode, selected_year, start, end,
    ):
        try:
            years, year, mode, first, last = resolve_department_period(
                analytics_dir, department, time_basis, period_mode, selected_year, start, end,
            )
        except (OSError, ValueError):
            return [], None, "year", None, None
        options = [{"label": str(year), "value": year} for year in years]
        return options, year, mode, first, last

    @app.callback(
        Output("date-range", "start_date", allow_duplicate=True),
        Output("date-range", "end_date", allow_duplicate=True),
        Input("year", "value"), Input("period-mode", "value"),
        State("department", "value"), State("time-basis", "value"),
        prevent_initial_call=True,
    )
    def dates_for_year(year, period_mode, department, time_basis):
        if period_mode != "year" or year is None:
            return no_update, no_update
        try:
            return calendar_year_range(
                analytics_dir, department, int(year), time_basis,
            )
        except (OSError, ValueError):
            return no_update, no_update

    @app.callback(
        Output("daily-statistic", "options"), Output("daily-statistic", "value"),
        Output("metric-help", "children"), Input("metric", "value"),
    )
    def metric_details(metric):
        spec = METRICS.get(metric, METRICS["T"])
        metadata = metric_metadata(str(analytics_dir))[spec.mnemonic]
        options = [
            {"label": statistic.replace("_", " ").title(), "value": statistic}
            for statistic in spec.daily_statistics
        ]
        help_content = [
            dcc.Markdown(
                f"**{spec.label} · {spec.unit}**  \n"
                f"{metadata['description_fr'] or 'No official description available.'}  \n"
                "Quality: 0 protected · 1 validated · 2 doubtful · 9 first-level filtered"
            )
        ]
        return options, list(spec.default_daily_statistics), help_content

    @app.callback(
        Output("daily-statistic-wrapper", "className"),
        Input("resolution", "value"),
    )
    def toggle_daily_statistics(resolution):
        return "" if resolution == "daily" else "is-hidden"

    @app.callback(
        Output("stations", "options"), Output("stations", "value"),
        Input("department", "value"),
        Input("date-range", "start_date"), Input("date-range", "end_date"),
        Input("time-basis", "value"), Input("metric", "value"),
        Input("quality-mode", "value"),
        Input({"type": "station-map", "department": ALL}, "clickData"),
        State("stations", "value"), State("global-filter-store", "data"),
    )
    def update_stations(
        department, start, end, time_basis, metric, quality_mode, click_data,
        selected, applied_payload,
    ):
        if not department or not start or not end:
            return [], []
        try:
            start_date, end_date = _date_value(start), _date_value(end)
            frame = stations_for_period(
                analytics_dir, department, start_date, end_date, time_basis,
            )
            options = _station_options(frame)
            valid = {option["value"] for option in options}
            map_triggered = (
                isinstance(ctx.triggered_id, dict)
                and ctx.triggered_id.get("type") == "station-map"
            )
            if map_triggered:
                try:
                    current_click = next(item for item in click_data if item)
                    clicked = str(current_click["points"][0]["customdata"][0])
                    if clicked in valid:
                        return options, [clicked]
                except (KeyError, IndexError, StopIteration, TypeError):
                    pass
                return options, [item for item in (selected or []) if item in valid]

            applied_department = (applied_payload or {}).get("department")
            retained = [] if applied_department != department else [
                item for item in (selected or []) if item in valid
            ]
            if not retained:
                best = best_station_for_period(
                    analytics_dir, department, metric, start_date, end_date,
                    time_basis, quality_mode,
                )
                retained = [best] if best else []
            return options, retained
        except (OSError, ValueError):
            return [], []

    @app.callback(
        Output("global-filter-store", "data"),
        Output("time-series-options-store", "data"),
        Output("historical-options-store", "data"),
        Output("filter-error", "children"),
        Output("time-series-options-error", "children"),
        Output("historical-options-error", "children"),
        Input("apply-global", "n_clicks"),
        Input("apply-time-series", "n_clicks"),
        Input("apply-historical", "n_clicks"),
        State("global-filter-store", "data"),
        State("time-series-options-store", "data"),
        State("historical-options-store", "data"),
        State("department", "value"), State("metric", "value"),
        State("stations", "value"), State("date-range", "start_date"),
        State("date-range", "end_date"), State("time-basis", "value"),
        State("quality-mode", "value"), State("resolution", "value"),
        State("daily-statistic", "value"), State("baseline-mode", "value"),
        prevent_initial_call=True,
    )
    def apply_changes(
        _global_clicks, _time_clicks, _historical_clicks,
        applied_global, applied_time, applied_historical,
        department, metric, stations, start, end, time_basis, quality_mode,
        resolution, statistics, baseline_mode,
    ):
        trigger = ctx.triggered_id
        try:
            next_global = _validated_global_state(
                analytics_dir, department_names, department, metric, stations,
                start, end, time_basis, quality_mode,
            )
            global_output, time_output, historical_output = contextual_store_updates(
                trigger, applied_global, applied_time, applied_historical,
                next_global, metric, resolution, statistics or [], baseline_mode,
            )
            if trigger == "apply-global":
                return global_output, time_output, historical_output, "", no_update, no_update
            if trigger == "apply-time-series":
                return global_output, time_output, historical_output, "", "", no_update
            if trigger == "apply-historical":
                return global_output, time_output, historical_output, "", no_update, ""
            return no_update, no_update, no_update, no_update, no_update, no_update
        except (KeyError, OSError, TypeError, ValueError) as error:
            message = str(error)
            if trigger == "apply-time-series":
                return no_update, no_update, no_update, "", message, no_update
            if trigger == "apply-historical":
                return no_update, no_update, no_update, "", no_update, message
            return no_update, no_update, no_update, message, no_update, no_update

    global_pending_inputs = (
        Input("global-filter-store", "data"), Input("department", "value"),
        Input("metric", "value"), Input("stations", "value"),
        Input("date-range", "start_date"), Input("date-range", "end_date"),
        Input("time-basis", "value"), Input("quality-mode", "value"),
    )

    @app.callback(Output("unapplied-changes", "className"), *global_pending_inputs)
    def show_global_changes(
        applied, department, metric, stations, start, end, time_basis, quality_mode,
    ):
        try:
            dirty = global_filters_are_dirty(
                applied, department, metric, stations, start, end,
                time_basis, quality_mode,
            )
        except (KeyError, TypeError, ValueError):
            dirty = True
        return "pending-message" if dirty else "pending-message is-hidden"

    @app.callback(
        Output("time-series-unapplied-changes", "className"),
        *global_pending_inputs,
        Input("time-series-options-store", "data"),
        Input("resolution", "value"), Input("daily-statistic", "value"),
    )
    def show_time_series_changes(
        applied_global, department, metric, stations, start, end,
        time_basis, quality_mode, applied_time, resolution, statistics,
    ):
        try:
            dirty = global_filters_are_dirty(
                applied_global, department, metric, stations, start, end,
                time_basis, quality_mode,
            ) or time_series_options_are_dirty(
                applied_time, metric, resolution, statistics,
            )
        except (KeyError, TypeError, ValueError):
            dirty = True
        return "pending-message" if dirty else "pending-message is-hidden"

    @app.callback(
        Output("historical-unapplied-changes", "className"),
        *global_pending_inputs,
        Input("historical-options-store", "data"), Input("baseline-mode", "value"),
    )
    def show_historical_changes(
        applied_global, department, metric, stations, start, end,
        time_basis, quality_mode, applied_historical, baseline_mode,
    ):
        try:
            dirty = global_filters_are_dirty(
                applied_global, department, metric, stations, start, end,
                time_basis, quality_mode,
            ) or historical_options_are_dirty(
                applied_historical, baseline_mode,
            )
        except (KeyError, TypeError, ValueError):
            dirty = True
        return "pending-message" if dirty else "pending-message is-hidden"

    @app.callback(
        Output("applied-department-context", "children"),
        Input("global-filter-store", "data"),
    )
    def applied_department_context(payload):
        department = (payload or {}).get("department")
        name = department_names.get(department, "Unknown department")
        return f"Applied department: {department} — {name}"

    @app.callback(
        Output("card-period", "children"), Output("card-stations", "children"),
        Output("card-observations", "children"), Output("card-coverage", "children"),
        Output("station-map-container", "children"), Output("query-error", "children"),
        Input("global-filter-store", "data"),
    )
    def render_global_context(payload):
        try:
            filters = compose_dashboard_filters(payload)
            summary = fetch_summary(analytics_dir, filters)
            station_map = fetch_station_map(analytics_dir, filters)
            return (
                summary["period"], f"{summary['stations']:,}",
                f"{summary['observations']:,}", f"{summary['coverage']:.2f}%",
                dcc.Graph(
                    id={"type": "station-map", "department": filters.department},
                    figure=station_map_figure(
                        station_map, filters.stations, filters.department,
                    ),
                    config={"displaylogo": False},
                ),
                "",
            )
        except Exception as error:  # Dash must render a useful error instead of a traceback.
            message = str(error)
            map_error = dcc.Graph(
                id={"type": "station-map", "department": "error"},
                figure=empty_figure(message), config={"displaylogo": False},
            )
            return "—", "—", "—", "—", map_error, message

    @app.callback(
        Output("department-overview-map", "figure"),
        Output("overview-error", "children"),
        Input("global-filter-store", "data"),
        Input("overview-surface", "value"),
        Input("analysis-tabs", "value"),
    )
    def render_department_overview(global_payload, layers, analysis):
        if analysis != "department-overview":
            return no_update, ""
        try:
            filters = compose_dashboard_filters(global_payload)
            geography = load_administrative_geography(geography_dir)
            stations = fetch_station_temperature_overview(
                analytics_dir,
                filters.start_date,
                filters.end_date,
                filters.time_basis,
                filters.quality_mode,
            )
            codes = tuple(department_names)
            show_surface = "surface" in (layers or [])
            surface = (
                build_temperature_surface(stations, geography, codes)
                if show_surface else stations.head(0)
            )
            return station_temperature_overview_figure(
                stations, surface, geography, codes, show_surface
            ), ""
        except Exception as error:  # Keep geography failures isolated to this tab.
            message = str(error)
            return empty_figure(message, 610), message

    @app.callback(
        Output("time-series", "figure"), Output("coverage-heatmap", "figure"),
        Output("time-series-error", "children"),
        Input("global-filter-store", "data"),
        Input("time-series-options-store", "data"),
        Input("analysis-tabs", "value"),
    )
    def render_time_series(global_payload, time_payload, analysis):
        if analysis != "time-series":
            return no_update, no_update, ""
        try:
            filters = compose_dashboard_filters(global_payload, time_payload)
            observations = fetch_timeseries(analytics_dir, filters)
            heatmap = fetch_coverage_grid(analytics_dir, filters)
            first_station = filters.stations[0]
            names = observations.filter(observations["NUM_POSTE"] == first_station)
            first_label = names["NOM_USUEL"][0] if names.height else first_station
            return (
                time_series_figure(observations, filters),
                coverage_heatmap_figure(heatmap, first_label, filters.metric),
                "",
            )
        except Exception as error:
            message = str(error)
            return empty_figure(message), empty_figure(message, 330), message

    @app.callback(
        Output("historical-comparison", "figure"), Output("comparison-error", "children"),
        Input("global-filter-store", "data"), Input("historical-options-store", "data"),
        Input("analysis-tabs", "value"),
    )
    def render_historical_comparison(global_payload, historical_payload, analysis):
        if analysis != "historical-comparison":
            return no_update, ""
        try:
            filters = compose_dashboard_filters(
                global_payload, historical_state=historical_payload,
            )
            comparison = fetch_historical_comparison(analytics_dir, filters)
            return historical_comparison_figure(comparison, filters), ""
        except Exception as error:
            message = str(error)
            return empty_figure(message), message

    @app.callback(
        Output("download-data", "data"),
        Input("download-time-series", "n_clicks"),
        Input("download-historical", "n_clicks"),
        State("global-filter-store", "data"),
        State("time-series-options-store", "data"),
        State("historical-options-store", "data"),
        prevent_initial_call=True,
    )
    def download_displayed_data(
        _time_clicks, _historical_clicks,
        global_payload, time_payload, historical_payload,
    ):
        try:
            if ctx.triggered_id == "download-historical":
                filters = compose_dashboard_filters(
                    global_payload, historical_state=historical_payload,
                )
                frame = fetch_historical_comparison(analytics_dir, filters)
                filename = (
                    f"meteo_france_{filters.department}_{filters.metric}_"
                    f"{filters.start_date}_{filters.end_date}_historical_comparison.csv"
                )
            else:
                filters = compose_dashboard_filters(global_payload, time_payload)
                frame = fetch_timeseries(analytics_dir, filters)
                filename = (
                    f"meteo_france_{filters.department}_{filters.metric}_"
                    f"{filters.start_date}_{filters.end_date}_{filters.resolution}.csv"
                )
            return dcc.send_string(frame.write_csv(), filename)
        except Exception:
            return no_update

    app.clientside_callback(
        "function(n) { if (n) { window.location.reload(); } return window.location.pathname; }",
        Output("url", "pathname"), Input("reset", "n_clicks"), prevent_initial_call=True,
    )
