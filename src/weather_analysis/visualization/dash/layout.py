from __future__ import annotations

from pathlib import Path

from dash import dcc, html

from weather_analysis.queries.metadata import (
    available_departments,
    available_years,
    best_station_for_period,
    calendar_year_range,
    metric_metadata,
    stations_for_period,
)
from weather_analysis.queries.models import DashboardFilters, METRICS

from .components import analysis_options_panel, field, summary_card
from .figures import empty_figure
from .state import (
    global_state_from_filters,
    historical_state_from_filters,
    time_series_state_from_filters,
)


def default_filters(analytics_dir: Path) -> DashboardFilters:
    departments = available_departments(str(analytics_dir))
    if not departments:
        raise ValueError("The analytical catalog has no materialized departments")
    department_codes = {code for code, _ in departments}
    department = "44" if "44" in department_codes else departments[0][0]
    years = available_years(str(analytics_dir), department)
    if not years:
        raise ValueError(f"Department {department} has no observations")
    year = years[0]
    start_date, end_date = calendar_year_range(analytics_dir, department, year)
    station = best_station_for_period(
        analytics_dir, department, "T", start_date, end_date,
    )
    if station is None:
        raise ValueError(f"Department {department} has no stations in its latest year")
    return DashboardFilters(
        department=department, metric="T", stations=(station,),
        start_date=start_date, end_date=end_date,
        time_basis="local", resolution="hourly", daily_statistics=("average",), quality_mode="all",
        exclude_selected_period_from_baseline=True,
    )


def build_layout(analytics_dir: Path):
    filters = default_filters(analytics_dir)
    departments = available_departments(str(analytics_dir))
    department_names = dict(departments)
    years = available_years(str(analytics_dir), filters.department)
    stations = stations_for_period(
        analytics_dir, filters.department, filters.start_date, filters.end_date
    )
    station_options = [
        {"label": f"{row['NOM_USUEL']} — {row['NUM_POSTE']}", "value": row["NUM_POSTE"]}
        for row in stations.iter_rows(named=True)
    ]
    metric_options = [
        {"label": f"{spec.label} ({name})", "value": name} for name, spec in METRICS.items()
    ]
    metadata = metric_metadata(str(analytics_dir))["T"]
    return html.Div([
        dcc.Location(id="url", refresh=True),
        dcc.Store(id="global-filter-store", data=global_state_from_filters(filters)),
        dcc.Store(
            id="time-series-options-store",
            data=time_series_state_from_filters(filters),
        ),
        dcc.Store(
            id="historical-options-store",
            data=historical_state_from_filters(filters),
        ),
        dcc.Download(id="download-data"),
        html.Header([
            html.Div("Météo-France", className="eyebrow"),
            html.H1("French Department Weather Explorer"),
            html.P(
                f"Applied department: {filters.department} — "
                f"{department_names[filters.department]}",
                id="applied-department-context",
            ),
        ], className="app-header"),
        html.Div([
            html.Aside([
                html.H2("Filters"),
                field("Department", dcc.Dropdown(
                    id="department",
                    options=[
                        {"label": f"{code} — {name}", "value": code}
                        for code, name in departments
                    ],
                    value=filters.department, clearable=False,
                )),
                field("Period", dcc.RadioItems(
                    id="period-mode", options=[{"label": "Calendar year", "value": "year"},
                                                {"label": "Custom range", "value": "custom"}],
                    value="year", inline=True,
                )),
                html.Div(field("Year", dcc.Dropdown(
                    id="year", options=[{"label": str(year), "value": year} for year in years],
                    value=filters.start_date.year, clearable=False,
                )), id="year-wrapper"),
                html.Div(field("Dates", dcc.DatePickerRange(
                    id="date-range", start_date=filters.start_date, end_date=filters.end_date,
                    display_format="YYYY-MM-DD",
                )), id="date-wrapper", className="is-hidden"),
                field("Metric", dcc.Dropdown(id="metric", options=metric_options, value="T", clearable=False)),
                html.Div([
                    html.Strong(f"{metadata['label']} · {metadata['unit']}"),
                    html.Div(metadata["description_fr"] or "No official description available."),
                    html.Div("Quality: 0 protected · 1 validated · 2 doubtful · 9 first-level filtered"),
                ], id="metric-help", className="metric-help"),
                field("Stations (maximum 5)", dcc.Dropdown(
                    id="stations", options=station_options, value=list(filters.stations), multi=True,
                    placeholder="Select stations…",
                )),
                field("Time basis", dcc.RadioItems(
                    id="time-basis", options=[{"label": "Europe/Paris", "value": "local"},
                                               {"label": "UTC", "value": "utc"}],
                    value="local", inline=True,
                )),
                field("Quality", dcc.RadioItems(
                    id="quality-mode", options=[{"label": "All observations", "value": "all"},
                                                 {"label": "Exclude doubtful (code 2)", "value": "exclude_doubtful"}],
                    value="all",
                )),
                html.Div(id="filter-error", className="error-message"),
                html.Div(
                    "Unapplied filter changes — click Apply to update charts",
                    id="unapplied-changes", className="pending-message is-hidden",
                ),
                html.Div([
                    html.Button(
                        "Apply filters", id="apply-global",
                        className="button button-primary",
                    ),
                    html.Button("Reset", id="reset", className="button"),
                ], className="button-row"),
            ], className="sidebar"),
            html.Main([
                html.Div(id="query-error", className="error-message"),
                html.Div([
                    summary_card("Selected period", "card-period"),
                    summary_card("Stations", "card-stations"),
                    summary_card("Observations", "card-observations"),
                    summary_card("Coverage", "card-coverage"),
                ], className="summary-grid"),
                dcc.Loading(html.Div(
                    dcc.Graph(
                        id={"type": "station-map", "department": filters.department},
                        figure=empty_figure("Loading station network", 390),
                        config={"displaylogo": False},
                    ),
                    id="station-map-container", className="chart-card",
                )),
                dcc.Tabs(id="analysis-tabs", value="time-series", children=[
                    dcc.Tab(label="Department overview", value="department-overview", children=[
                        html.Div([
                            html.P(
                                "Air temperature (T) across all materialized departments. "
                                "Each station is the mean of days with at least 18 valid hourly readings; "
                                "the shared period, time basis, and quality filter apply.",
                                className="analysis-note",
                            ),
                            field("Map layers", dcc.Checklist(
                                id="overview-surface",
                                options=[{
                                    "label": "Estimated temperature surface",
                                    "value": "surface",
                                }],
                                value=["surface"],
                            )),
                        ], className="analysis-options-panel overview-options"),
                        html.Div(id="overview-error", className="error-message"),
                        html.Div(dcc.Loading(dcc.Graph(
                            id="department-overview-map",
                            figure=empty_figure("Open this tab to load the department overview", 610),
                            config={"displaylogo": False},
                        )), className="chart-card"),
                    ]),
                    dcc.Tab(label="Time series", value="time-series", children=[
                        analysis_options_panel(
                            "Inspect station observations at hourly or daily resolution.",
                            [
                                html.Div(field("Resampling rate", dcc.RadioItems(
                                    id="resolution",
                                    options=[
                                        {"label": "Hourly", "value": "hourly"},
                                        {"label": "Daily", "value": "daily"},
                                    ],
                                    value="hourly", inline=True,
                                )), id="resampling-wrapper"),
                                html.Div(field("Daily statistics", dcc.Dropdown(
                                    id="daily-statistic",
                                    options=[
                                        {"label": "Average", "value": "average"},
                                        {"label": "Minimum", "value": "minimum"},
                                        {"label": "Maximum", "value": "maximum"},
                                    ],
                                    value=["average"], multi=True, clearable=False,
                                )), id="daily-statistic-wrapper", className="is-hidden"),
                            ],
                            "time-series-unapplied-changes",
                            "apply-time-series",
                            "download-time-series",
                        ),
                        html.Div(id="time-series-options-error", className="error-message"),
                        html.Div(id="time-series-error", className="error-message"),
                        html.Div(dcc.Loading(dcc.Graph(
                            id="time-series", figure=empty_figure("Loading observations"),
                            config={"displaylogo": False},
                        )), className="chart-card"),
                        html.Div(dcc.Loading(dcc.Graph(
                            id="coverage-heatmap", figure=empty_figure("Loading coverage", 330),
                            config={"displaylogo": False},
                        )), className="chart-card"),
                    ]),
                    dcc.Tab(label="Historical comparison", value="historical-comparison", children=[
                        analysis_options_panel(
                            "Compare daily selected values with the same calendar day "
                            "across each station's history.",
                            [field("Historical baseline", dcc.RadioItems(
                                id="baseline-mode",
                                options=[
                                    {"label": "Exclude selected period", "value": "exclude"},
                                    {"label": "Include selected period", "value": "include"},
                                ],
                                value="exclude",
                            ))],
                            "historical-unapplied-changes",
                            "apply-historical",
                            "download-historical",
                        ),
                        html.Div(id="historical-options-error", className="error-message"),
                        html.Div(id="comparison-error", className="error-message"),
                        html.Div(dcc.Loading(dcc.Graph(
                            id="historical-comparison",
                            figure=empty_figure("Open this tab to load the historical comparison"),
                            config={"displaylogo": False},
                        )), className="chart-card"),
                    ]),
                ]),
            ], className="main-content"),
        ], className="app-shell"),
    ])
