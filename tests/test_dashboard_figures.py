from __future__ import annotations

from datetime import date, datetime

import polars as pl

from weather_analysis.queries.models import DashboardFilters
from weather_analysis.visualization.dash.figures import (
    coverage_heatmap_figure,
    empty_figure,
    historical_comparison_figure,
    station_map_figure,
    time_series_figure,
)


def filters(resolution: str = "hourly") -> DashboardFilters:
    return DashboardFilters(
        "44", "T", ("44020001",), date(2025, 1, 1), date(2025, 1, 1),
        resolution=resolution, daily_statistics=("average",),
    )


def test_time_series_and_empty_figures():
    frame = pl.DataFrame({
        "NUM_POSTE": ["44020001"], "NOM_USUEL": ["NANTES-BOUGUENAIS"],
        "observation_time": [datetime(2025, 1, 1)], "observation_date": [date(2025, 1, 1)],
        "value": [5.0], "quality_code": [1], "contributing_count": [1],
    })
    figure = time_series_figure(frame, filters())
    assert len(figure.data) == 1
    assert "°C" in figure.layout.yaxis.title.text
    assert len(empty_figure("Nothing").layout.annotations) == 1


def test_heatmap_and_map_figures():
    heatmap = pl.DataFrame({
        "observation_date": [date(2025, 1, 1)] * 2,
        "observation_hour": [0, 1], "observation_count": [1, 0],
    })
    assert coverage_heatmap_figure(heatmap, "Station", "T").data[0].z[0][0] == 1
    station_map = pl.DataFrame({
        "NUM_POSTE": ["44020001"], "NOM_USUEL": ["Station"], "LAT": [47.2],
        "LON": [-1.6], "ALTI": [20], "first_observation_utc": [datetime(2020, 1, 1)],
        "last_observation_utc": [datetime(2025, 1, 1)], "period_rows": [1],
        "metric_rows": [1], "coverage": [100.0], "active_in_period": [True],
    })
    assert station_map_figure(station_map, ("44020001",)).data[0].type == "scattermap"


def test_daily_average_minimum_and_maximum_have_distinct_visual_cues():
    daily_filters = DashboardFilters(
        "44", "T", ("44020001",), date(2025, 1, 1), date(2025, 1, 2),
        resolution="daily", daily_statistics=("average", "minimum", "maximum"),
    )
    frame = pl.DataFrame({
        "NUM_POSTE": ["44020001"] * 6, "NOM_USUEL": ["Station"] * 6,
        "observation_date": [date(2025, 1, 1)] * 3 + [date(2025, 1, 2)] * 3,
        "contributing_count": [24] * 6,
        "statistic": ["average", "minimum", "maximum"] * 2,
        "value": [5.0, 2.0, 8.0, 6.0, 3.0, 9.0],
    })
    figure = time_series_figure(frame, daily_filters)
    named = {trace.name: trace for trace in figure.data if trace.name}
    assert named["Station — Average"].line.dash == "solid"
    assert named["Station — Minimum"].line.dash == "dot"
    assert named["Station — Maximum"].line.dash == "dash"
    assert any(getattr(trace, "fill", None) == "tonexty" for trace in figure.data)


def comparison_frame(stations=("44020001",)) -> pl.DataFrame:
    rows = []
    for index, station in enumerate(stations):
        for day in (1, 2):
            rows.append({
                "NUM_POSTE": station, "NOM_USUEL": f"Station {index + 1}",
                "observation_date": date(2025, 1, day),
                "selected_total": None, "selected_average": 5.0 + day,
                "selected_minimum": 2.0 + day, "selected_maximum": 8.0 + day,
                "selected_contributing_hours": 24,
                "absolute_minimum": -5.0, "absolute_maximum": 15.0,
                "average_minimum": 1.0, "average_maximum": 10.0,
                "historical_average_total": None, "historical_days": 50,
                "historical_years": 50, "baseline_excludes_selected_period": True,
            })
    return pl.DataFrame(rows)


def test_historical_comparison_uses_nested_bands_and_distinct_selected_lines():
    comparison_filters = DashboardFilters(
        "44", "T", ("44020001", "44109012"), date(2025, 1, 1), date(2025, 1, 2)
    )
    figure = historical_comparison_figure(
        comparison_frame(("44020001", "44109012")), comparison_filters
    )
    named = {trace.name: trace for trace in figure.data if trace.name}
    assert named["Historical absolute range"].fill == "tonexty"
    assert named["Historical average min–max"].fill == "tonexty"
    assert named["Selected daily average"].line.dash == "solid"
    assert named["Selected daily minimum"].line.dash == "dot"
    assert named["Selected daily maximum"].line.dash == "dash"
    assert len(figure.layout.annotations) == 2
    assert figure.layout.yaxis2.matches == "y"
    assert sum(trace.showlegend is True for trace in figure.data) == 5
    hover_templates = [trace.hovertemplate or "" for trace in figure.data]
    assert all("%{x}" not in template for template in hover_templates)
    assert sum("Historical years" in template for template in hover_templates) == 2
    assert all("Historical days" not in template for template in hover_templates)
    metadata_traces = [
        trace for trace in figure.data if "Historical years" in (trace.hovertemplate or "")
    ]
    assert all(trace.showlegend is False for trace in metadata_traces)
    assert all("<i>" in trace.hovertemplate and "font-size:10px" in trace.hovertemplate
               for trace in metadata_traces)


def test_historical_rainfall_figure_and_empty_baseline():
    frame = comparison_frame().with_columns(
        pl.lit(1.5).alias("selected_total"),
        pl.lit(None, dtype=pl.Float64).alias("selected_average"),
        pl.lit(None, dtype=pl.Float64).alias("selected_minimum"),
        pl.lit(None, dtype=pl.Float64).alias("selected_maximum"),
        pl.lit(None, dtype=pl.Float64).alias("average_minimum"),
        pl.lit(None, dtype=pl.Float64).alias("average_maximum"),
        pl.lit(2.0).alias("historical_average_total"),
    )
    rain_filters = DashboardFilters(
        "44", "RR1", ("44020001",), date(2025, 1, 1), date(2025, 1, 2)
    )
    figure = historical_comparison_figure(frame, rain_filters)
    assert {trace.name for trace in figure.data if trace.name} == {
        "Historical absolute range", "Historical average daily total", "Selected daily total",
    }
    empty = frame.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("absolute_minimum"),
        pl.lit(None, dtype=pl.Float64).alias("absolute_maximum"),
    )
    assert len(historical_comparison_figure(empty, rain_filters).layout.annotations) == 1
