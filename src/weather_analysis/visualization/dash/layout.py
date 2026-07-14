from __future__ import annotations

from datetime import date
from pathlib import Path

from dash import dcc, html

from weather_analysis.queries.metadata import available_years, metric_metadata, stations_for_period
from weather_analysis.queries.models import DashboardFilters, METRICS

from .components import field, summary_card
from .figures import empty_figure


DEFAULT_YEAR = 2025
DEFAULT_STATION = "44020001"


def default_filters() -> DashboardFilters:
    return DashboardFilters(
        department="44", metric="T", stations=(DEFAULT_STATION,),
        start_date=date(DEFAULT_YEAR, 1, 1), end_date=date(DEFAULT_YEAR, 12, 31),
        time_basis="local", resolution="hourly", daily_statistics=("average",), quality_mode="all",
        exclude_selected_period_from_baseline=True,
    )


def build_layout(analytics_dir: Path):
    filters = default_filters()
    years = available_years(str(analytics_dir))
    stations = stations_for_period(analytics_dir, "44", filters.start_date, filters.end_date)
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
        dcc.Store(id="filter-store", data=filters.to_dict()),
        dcc.Download(id="download-data"),
        html.Header([
            html.Div("Météo-France", className="eyebrow"),
            html.H1("Loire-Atlantique Weather Explorer"),
            html.P("Station observations from the department 44 analytical dataset."),
        ], className="app-header"),
        html.Div([
            html.Aside([
                html.H2("Filters"),
                field("Department", dcc.Dropdown(
                    id="department", options=[{"label": "44 — Loire-Atlantique", "value": "44"}],
                    value="44", disabled=True, clearable=False,
                )),
                field("Period", dcc.RadioItems(
                    id="period-mode", options=[{"label": "Calendar year", "value": "year"},
                                                {"label": "Custom range", "value": "custom"}],
                    value="year", inline=True,
                )),
                html.Div(field("Year", dcc.Dropdown(
                    id="year", options=[{"label": str(year), "value": year} for year in years],
                    value=DEFAULT_YEAR, clearable=False,
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
                    id="stations", options=station_options, value=[DEFAULT_STATION], multi=True,
                    placeholder="Select stations…",
                )),
                field("Time basis", dcc.RadioItems(
                    id="time-basis", options=[{"label": "Europe/Paris", "value": "local"},
                                               {"label": "UTC", "value": "utc"}],
                    value="local", inline=True,
                )),
                html.Div(field("Resampling rate", dcc.RadioItems(
                    id="resolution", options=[{"label": "Hourly", "value": "hourly"},
                                               {"label": "Daily", "value": "daily"}],
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
                html.Div(field("Historical baseline", dcc.RadioItems(
                    id="baseline-mode",
                    options=[
                        {"label": "Exclude selected period", "value": "exclude"},
                        {"label": "Include selected period", "value": "include"},
                    ],
                    value="exclude",
                )), id="baseline-mode-wrapper", className="is-hidden"),
                field("Quality", dcc.RadioItems(
                    id="quality-mode", options=[{"label": "All observations", "value": "all"},
                                                 {"label": "Exclude doubtful (code 2)", "value": "exclude_doubtful"}],
                    value="all",
                )),
                html.Div(id="filter-error", className="error-message"),
                html.Div([
                    html.Button("Apply", id="apply", className="button button-primary"),
                    html.Button("Reset", id="reset", className="button"),
                    html.Button("Download displayed data", id="download", className="button button-wide"),
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
                dcc.Loading(html.Div([
                    dcc.Graph(id="station-map", figure=empty_figure("Loading station network", 390),
                              config={"displaylogo": False}),
                ], className="chart-card")),
                dcc.Tabs(id="analysis-tabs", value="time-series", children=[
                    dcc.Tab(label="Time series", value="time-series", children=[
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
                        html.P(
                            "Daily selected values compared with the same calendar day across each station's history.",
                            className="analysis-note",
                        ),
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
