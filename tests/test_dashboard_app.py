from __future__ import annotations

from datetime import date

import duckdb
import pytest
from dash import no_update

from weather_analysis.queries.models import DashboardFilters
from weather_analysis.visualization.dash.callbacks import (
    contextual_store_updates,
    global_filters_are_dirty,
    historical_options_are_dirty,
    resolve_department_period,
    time_series_options_are_dirty,
)
from weather_analysis.visualization.dash.app import create_app
from weather_analysis.visualization.dash.layout import build_layout, default_filters
from weather_analysis.visualization.dash.state import (
    compose_dashboard_filters,
    global_state_from_filters,
    historical_state_from_filters,
    time_series_state_from_filters,
)

from test_dashboard_queries import make_catalog


def component_ids(component) -> set[str]:
    identifier = getattr(component, "id", None)
    identifiers = {identifier} if isinstance(identifier, str) else set()
    children = getattr(component, "children", None)
    if children is None:
        return identifiers
    for child in children if isinstance(children, (list, tuple)) else [children]:
        if hasattr(child, "children") or hasattr(child, "id"):
            identifiers.update(component_ids(child))
    return identifiers


def test_dash_layout_and_callback_smoke(tmp_path):
    app = create_app(make_catalog(tmp_path))
    response = app.server.test_client().get("/")
    assert response.status_code == 200
    assert app.title == "French Department Weather Explorer"
    assert len(app.callback_map) >= 10

    global_render = next(
        callback for output, callback in app.callback_map.items()
        if "station-map-container.children" in output
    )
    assert [item["id"] for item in global_render["inputs"]] == ["global-filter-store"]


def test_analysis_controls_are_not_in_global_sidebar(tmp_path):
    layout = build_layout(make_catalog(tmp_path))
    shell = layout.children[-1]
    sidebar = shell.children[0]
    sidebar_ids = component_ids(sidebar)
    all_ids = component_ids(layout)
    assert {"resolution", "daily-statistic", "baseline-mode"}.isdisjoint(sidebar_ids)
    assert {
        "resolution", "daily-statistic", "baseline-mode",
        "overview-surface", "department-overview-map",
    }.issubset(all_ids)
    assert {"apply-global", "reset"}.issubset(sidebar_ids)
    assert {
        "apply-time-series", "download-time-series",
        "apply-historical", "download-historical",
    }.issubset(all_ids)


def test_department_overview_is_lazy_and_informational(tmp_path):
    app = create_app(make_catalog(tmp_path))
    callback = next(
        callback for output, callback in app.callback_map.items()
        if "department-overview-map.figure" in output
    )
    assert [item["id"] for item in callback["inputs"]] == [
        "global-filter-store", "overview-surface", "analysis-tabs",
    ]


def test_dynamic_defaults_prefer_44_and_use_partial_latest_year(tmp_path):
    root = make_catalog(tmp_path)
    filters = default_filters(root)
    assert filters.department == "44"
    assert str(filters.end_date) == "2026-07-10"


def test_dynamic_defaults_fall_back_and_empty_catalog_fails(tmp_path):
    root = make_catalog(tmp_path)
    with duckdb.connect(str(root / "weather.duckdb")) as connection:
        connection.execute("DELETE FROM hourly_core WHERE department='44'")
        connection.execute("DELETE FROM stations WHERE department='44'")
    assert default_filters(root).department == "35"

    other = tmp_path / "other"
    other.mkdir()
    empty_root = make_catalog(other)
    with duckdb.connect(str(empty_root / "weather.duckdb")) as connection:
        connection.execute("DELETE FROM hourly_core")
        connection.execute("DELETE FROM stations")
    with pytest.raises(ValueError, match="no materialized departments"):
        default_filters(empty_root)


def test_unapplied_change_detection_is_split_by_scope():
    applied = DashboardFilters.from_dict({
        "department": "44", "metric": "T", "stations": ["44020001"],
        "start_date": "2026-01-01", "end_date": "2026-07-10",
        "time_basis": "local", "resolution": "hourly",
        "daily_statistics": ["average"], "quality_mode": "all",
        "exclude_selected_period_from_baseline": True,
    })
    global_values = (
        global_state_from_filters(applied), "44", "T", ["44020001"],
        "2026-01-01", "2026-07-10", "local", "all",
    )
    assert not global_filters_are_dirty(*global_values)
    changed = list(global_values)
    changed[1] = "35"
    assert global_filters_are_dirty(*changed)
    assert not time_series_options_are_dirty(
        time_series_state_from_filters(applied), "T", "hourly", ["average"],
    )
    assert time_series_options_are_dirty(
        time_series_state_from_filters(applied), "T", "daily", ["average"],
    )
    assert not historical_options_are_dirty(
        historical_state_from_filters(applied), "exclude",
    )
    assert historical_options_are_dirty(
        historical_state_from_filters(applied), "include",
    )


def test_split_state_composes_query_filters_and_normalizes_new_metric():
    applied = DashboardFilters(
        department="44", metric="RR1", stations=("44020001",),
        start_date=date(2025, 1, 1), end_date=date(2025, 1, 2),
        resolution="daily", daily_statistics=("total",),
    )
    global_state = global_state_from_filters(applied)
    time_state = time_series_state_from_filters(applied)
    assert compose_dashboard_filters(global_state, time_state) == applied
    global_state["metric"] = "T"
    normalized = compose_dashboard_filters(global_state, time_state)
    assert normalized.metric == "T"
    assert normalized.daily_statistics == ("average",)


def test_contextual_apply_updates_only_the_selected_scopes():
    global_applied = {"department": "44"}
    global_pending = {"department": "35"}
    time_applied = {"resolution": "hourly", "daily_statistics": ["average"]}
    historical_applied = {"exclude_selected_period_from_baseline": True}

    global_result = contextual_store_updates(
        "apply-global", global_applied, time_applied, historical_applied,
        global_pending, "T", "daily", ["minimum"], "include",
    )
    assert global_result[0] == global_pending
    assert global_result[1] is no_update
    assert global_result[2] is no_update

    time_result = contextual_store_updates(
        "apply-time-series", global_applied, time_applied, historical_applied,
        global_pending, "T", "daily", ["minimum"], "include",
    )
    assert time_result[0] == global_pending
    assert time_result[1] == {
        "resolution": "daily", "daily_statistics": ["minimum"],
    }
    assert time_result[2] is no_update

    time_only_result = contextual_store_updates(
        "apply-time-series", global_applied, time_applied, historical_applied,
        global_applied, "T", "daily", ["minimum"], "include",
    )
    assert time_only_result[0] is no_update
    assert time_only_result[1] == {
        "resolution": "daily", "daily_statistics": ["minimum"],
    }
    assert time_only_result[2] is no_update

    historical_result = contextual_store_updates(
        "apply-historical", global_applied, time_applied, historical_applied,
        global_pending, "T", "daily", ["minimum"], "include",
    )
    assert historical_result[0] == global_pending
    assert historical_result[1] is no_update
    assert historical_result[2] == {
        "exclude_selected_period_from_baseline": False,
    }


def test_department_switch_preserves_valid_period_and_falls_back(tmp_path):
    root = make_catalog(tmp_path)
    calendar = resolve_department_period(
        root, "35", "local", "year", 2024, "2024-01-01", "2024-12-31",
    )
    assert calendar[1:] == (2024, "year", date(2024, 1, 1), date(2024, 12, 31))
    valid = resolve_department_period(
        root, "35", "local", "custom", 2025, "2024-01-01", "2024-01-02",
    )
    assert valid[1:] == (2026, "custom", "2024-01-01", "2024-01-02")
    fallback = resolve_department_period(
        root, "35", "local", "custom", 2023, "2023-01-01", "2023-12-31",
    )
    assert fallback[1:] == (2026, "year", date(2026, 1, 1), date(2026, 6, 15))
