from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

import weather_analysis.queries.observations as observation_queries

from weather_analysis.queries.connection import validate_catalog
from weather_analysis.queries.coverage import expected_hours, fetch_coverage_grid, fetch_summary
from weather_analysis.queries.metadata import available_years, metric_metadata, stations_for_period
from weather_analysis.queries.models import DashboardFilters, QueryLimitError
from weather_analysis.queries.observations import fetch_timeseries


def make_catalog(tmp_path: Path) -> Path:
    root = tmp_path / "analytics"
    root.mkdir()
    connection = duckdb.connect(str(root / "weather.duckdb"))
    connection.execute("""
        CREATE TABLE hourly_core (
            department VARCHAR, year INTEGER, NUM_POSTE VARCHAR, NOM_USUEL VARCHAR,
            LAT DOUBLE, LON DOUBLE, ALTI INTEGER,
            observation_time_utc TIMESTAMPTZ, local_date DATE, local_hour UTINYINT,
            T REAL, QT UTINYINT, TD REAL, QTD UTINYINT, U REAL, QU UTINYINT,
            RR1 REAL, QRR1 UTINYINT, FF REAL, QFF UTINYINT,
            PSTAT REAL, QPSTAT UTINYINT
        )
    """)
    rows = [
        ("44", 2024, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2024-10-27 00:00:00+00", "2024-10-27", 2, 10.0, 1),
        ("44", 2024, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2024-10-27 01:00:00+00", "2024-10-27", 2, 12.0, 2),
        ("44", 2025, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2025-01-01 00:00:00+00", "2025-01-01", 1, 4.0, 1),
        ("44", 2025, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2025-01-01 01:00:00+00", "2025-01-01", 2, 6.0, 1),
        ("44", 2025, "44109012", "NANTES-VILLE", 47.22, -1.55, 17,
         "2025-01-01 00:00:00+00", "2025-01-01", 1, 5.0, 9),
        ("44", 2024, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2024-02-29 12:00:00+00", "2024-02-29", 13, 8.0, 1),
        ("44", 2024, "44000000", "TIME-EDGE", 47.00, -1.00, 10,
         "2024-12-31 23:00:00+00", "2025-01-01", 0, 3.0, 1),
    ]
    connection.executemany(
        "INSERT INTO hourly_core (department,year,NUM_POSTE,NOM_USUEL,LAT,LON,ALTI,"
        "observation_time_utc,local_date,local_hour,T,QT) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.execute("UPDATE hourly_core SET RR1 = T / 10, QRR1 = QT")
    connection.execute("CREATE TABLE stations AS SELECT DISTINCT department,NUM_POSTE,NOM_USUEL,LAT,LON,ALTI FROM hourly_core")
    connection.execute("""
        CREATE TABLE metrics (
            source_mnemonic VARCHAR, official_description_fr VARCHAR,
            documentation_source VARCHAR
        )
    """)
    connection.executemany(
        "INSERT INTO metrics VALUES (?,?,?)",
        [(name, f"Official {name}", "dictionary.csv") for name in ("T", "TD", "U", "RR1", "FF", "PSTAT")],
    )
    connection.execute("CREATE TABLE departments (department VARCHAR, name VARCHAR)")
    connection.execute("INSERT INTO departments VALUES ('44','Loire-Atlantique')")
    connection.close()
    return root


def filters(**changes) -> DashboardFilters:
    values = dict(
        department="44", metric="T", stations=("44020001",),
        start_date=date(2025, 1, 1), end_date=date(2025, 1, 1),
        time_basis="local", resolution="hourly", daily_statistics=("average",), quality_mode="all",
    )
    values.update(changes)
    return DashboardFilters(**values)


def test_filter_validation_and_whitelists():
    assert filters().metric_spec.unit == "°C"
    with pytest.raises(ValueError, match="at most 5"):
        filters(stations=tuple(str(i) for i in range(6)))
    with pytest.raises(ValueError, match="Unsupported metric"):
        filters(metric="DROP TABLE")
    with pytest.raises(ValueError, match="not valid"):
        filters(daily_statistics=("total",))


def test_catalog_metadata_and_missing_database(tmp_path: Path):
    root = make_catalog(tmp_path)
    assert validate_catalog(root).name == "weather.duckdb"
    assert available_years(str(root)) == (2025, 2024)
    assert metric_metadata(str(root))["T"]["description_fr"] == "Official T"
    assert stations_for_period(root, "44", date(2025, 1, 1), date(2025, 1, 1)).height == 3
    with pytest.raises(FileNotFoundError):
        validate_catalog(tmp_path / "missing")


def test_hourly_daily_and_quality_queries(tmp_path: Path):
    root = make_catalog(tmp_path)
    hourly = fetch_timeseries(root, filters())
    assert hourly["value"].to_list() == [4.0, 6.0]
    daily = fetch_timeseries(root, filters(resolution="daily"))
    assert daily["value"].to_list() == [5.0]
    assert fetch_timeseries(root, filters(resolution="daily", daily_statistics=("minimum",)))["value"].item() == 4.0
    rain = filters(metric="RR1", resolution="daily", daily_statistics=("total",))
    assert fetch_timeseries(root, rain)["value"].item() == pytest.approx(1.0)


def test_daily_query_returns_multiple_selected_statistics(tmp_path: Path):
    root = make_catalog(tmp_path)
    frame = fetch_timeseries(
        root,
        filters(resolution="daily", daily_statistics=("average", "minimum", "maximum")),
    )
    assert frame["statistic"].to_list() == ["average", "maximum", "minimum"]
    assert dict(zip(frame["statistic"], frame["value"])) == {
        "average": 5.0, "minimum": 4.0, "maximum": 6.0,
    }
    doubtful_period = filters(
        start_date=date(2024, 10, 27), end_date=date(2024, 10, 27),
        quality_mode="exclude_doubtful",
    )
    assert fetch_timeseries(root, doubtful_period)["value"].to_list() == [10.0]


def test_local_and_utc_date_boundaries(tmp_path: Path):
    root = make_catalog(tmp_path)
    local = filters(stations=("44000000",))
    utc = filters(stations=("44000000",), time_basis="utc")
    assert fetch_timeseries(root, local).height == 1
    assert fetch_timeseries(root, utc).is_empty()


def test_point_limit_is_actionable(tmp_path: Path, monkeypatch):
    root = make_catalog(tmp_path)
    monkeypatch.setattr(observation_queries, "MAX_CHART_POINTS", 1)
    with pytest.raises(QueryLimitError, match="fewer stations"):
        fetch_timeseries(root, filters())


def test_dst_coverage_grid_and_summary(tmp_path: Path):
    root = make_catalog(tmp_path)
    spring = filters(start_date=date(2024, 3, 31), end_date=date(2024, 3, 31))
    autumn = filters(start_date=date(2024, 10, 27), end_date=date(2024, 10, 27))
    assert expected_hours(spring) == 23
    assert expected_hours(autumn) == 25
    grid = fetch_coverage_grid(root, autumn)
    assert grid.filter(grid["observation_hour"] == 2)["observation_count"].item() == 2
    summary = fetch_summary(root, autumn)
    assert summary["observations"] == 2
    assert summary["expected"] == 25


def test_daily_resampling_is_consistent_across_multi_year_ranges(tmp_path: Path):
    root = make_catalog(tmp_path)
    daily = fetch_timeseries(root, filters(resolution="daily"))
    assert daily["value"].item() == 5.0
    multi_year = fetch_timeseries(
        root,
        filters(
            stations=("44020001", "44109012"), resolution="daily",
            start_date=date(2024, 1, 1), end_date=date(2025, 12, 31),
        ),
    )
    assert multi_year["NUM_POSTE"].n_unique() == 2
    assert date(2024, 2, 29) in multi_year["observation_date"].to_list()
