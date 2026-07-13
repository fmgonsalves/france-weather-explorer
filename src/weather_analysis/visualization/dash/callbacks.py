from __future__ import annotations

from datetime import date
from pathlib import Path

from dash import Input, Output, State, callback, dcc, no_update

from weather_analysis.queries.coverage import (
    fetch_coverage_grid,
    fetch_station_map,
    fetch_summary,
)
from weather_analysis.queries.metadata import metric_metadata, stations_for_period
from weather_analysis.queries.models import DashboardFilters, METRICS
from weather_analysis.queries.observations import (
    fetch_timeseries,
    filters_from_values,
)

from .figures import (
    coverage_heatmap_figure,
    empty_figure,
    station_map_figure,
    time_series_figure,
)


def register_callbacks(app, analytics_dir: Path) -> None:
    @app.callback(
        Output("year-wrapper", "className"), Output("date-wrapper", "className"),
        Input("period-mode", "value"),
    )
    def toggle_period(mode):
        return ("", "is-hidden") if mode == "year" else ("is-hidden", "")

    @app.callback(
        Output("date-range", "start_date"), Output("date-range", "end_date"),
        Input("year", "value"), prevent_initial_call=True,
    )
    def dates_for_year(year):
        if year is None:
            return no_update, no_update
        return f"{int(year):04d}-01-01", f"{int(year):04d}-12-31"

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
        Output("daily-statistic-wrapper", "className"), Input("resolution", "value"),
    )
    def toggle_daily_statistic(resolution):
        return "" if resolution == "daily" else "is-hidden"

    @app.callback(
        Output("stations", "options"),
        Input("date-range", "start_date"), Input("date-range", "end_date"),
        Input("time-basis", "value"),
    )
    def station_options(start, end, time_basis):
        if not start or not end:
            return []
        try:
            frame = stations_for_period(
                analytics_dir, "44", date.fromisoformat(start[:10]), date.fromisoformat(end[:10]),
                time_basis,
            )
        except (OSError, ValueError):
            return []
        return [
            {"label": f"{row['NOM_USUEL']} — {row['NUM_POSTE']}", "value": row["NUM_POSTE"]}
            for row in frame.iter_rows(named=True)
        ]

    @app.callback(
        Output("filter-store", "data"), Output("filter-error", "children"),
        Input("apply", "n_clicks"),
        State("metric", "value"), State("stations", "value"),
        State("period-mode", "value"), State("year", "value"),
        State("date-range", "start_date"), State("date-range", "end_date"),
        State("time-basis", "value"), State("resolution", "value"),
        State("daily-statistic", "value"), State("quality-mode", "value"),
        prevent_initial_call=True,
    )
    def apply_filters(_clicks, metric, stations, period_mode, year, start, end,
                      time_basis, resolution, statistic, quality_mode):
        try:
            if period_mode == "year":
                if year is None:
                    raise ValueError("Select a calendar year")
                start, end = f"{int(year):04d}-01-01", f"{int(year):04d}-12-31"
            if not start or not end:
                raise ValueError("Select both start and end dates")
            filters = filters_from_values(
                metric, stations or [], start[:10], end[:10], time_basis, resolution,
                statistic or [], quality_mode,
            )
        except (KeyError, TypeError, ValueError) as error:
            return no_update, str(error)
        return filters.to_dict(), ""

    @app.callback(
        Output("card-period", "children"), Output("card-stations", "children"),
        Output("card-observations", "children"), Output("card-coverage", "children"),
        Output("station-map", "figure"), Output("time-series", "figure"),
        Output("coverage-heatmap", "figure"), Output("query-error", "children"),
        Input("filter-store", "data"),
    )
    def render_dashboard(payload):
        try:
            filters = DashboardFilters.from_dict(payload)
            summary = fetch_summary(analytics_dir, filters)
            station_map = fetch_station_map(analytics_dir, filters)
            observations = fetch_timeseries(analytics_dir, filters)
            heatmap = fetch_coverage_grid(analytics_dir, filters)
            first_station = filters.stations[0]
            names = observations.filter(observations["NUM_POSTE"] == first_station)
            first_label = names["NOM_USUEL"][0] if names.height else first_station
            return (
                summary["period"], f"{summary['stations']:,}", f"{summary['observations']:,}",
                f"{summary['coverage']:.2f}%",
                station_map_figure(station_map, filters.stations),
                time_series_figure(observations, filters),
                coverage_heatmap_figure(heatmap, first_label, filters.metric),
                "",
            )
        except Exception as error:  # Dash must render a useful error instead of a callback traceback.
            message = str(error)
            empty = empty_figure(message)
            return "—", "—", "—", "—", empty, empty, empty_figure(message, 330), message

    @app.callback(
        Output("stations", "value"), Input("station-map", "clickData"),
        prevent_initial_call=True,
    )
    def select_station_from_map(click_data):
        try:
            return [str(click_data["points"][0]["customdata"][0])]
        except (KeyError, IndexError, TypeError):
            return no_update

    @app.callback(
        Output("download-data", "data"), Input("download", "n_clicks"),
        State("filter-store", "data"), prevent_initial_call=True,
    )
    def download_displayed_data(_clicks, payload):
        try:
            filters = DashboardFilters.from_dict(payload)
            frame = fetch_timeseries(analytics_dir, filters)
            filename = (
                f"meteo_france_44_{filters.metric}_{filters.start_date}_"
                f"{filters.end_date}_{filters.resolution}.csv"
            )
            return dcc.send_string(frame.write_csv(), filename)
        except Exception:
            return no_update

    app.clientside_callback(
        "function(n) { if (n) { window.location.reload(); } return window.location.pathname; }",
        Output("url", "pathname"), Input("reset", "n_clicks"), prevent_initial_call=True,
    )
