from __future__ import annotations

from datetime import date, datetime

import polars as pl

from weather_analysis.queries.models import DashboardFilters
from weather_analysis.visualization.dash.figures import (
    coverage_heatmap_figure,
    empty_figure,
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
