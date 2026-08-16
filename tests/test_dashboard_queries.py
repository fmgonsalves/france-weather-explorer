from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

import weather_analysis.queries.observations as observation_queries

from weather_analysis.queries.connection import validate_catalog
from weather_analysis.queries.coverage import (
    expected_hours,
    fetch_coverage_grid,
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
from weather_analysis.queries.models import DashboardFilters, QueryLimitError
from weather_analysis.queries.observations import fetch_historical_comparison, fetch_timeseries


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
        ("44", 2023, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2023-01-01 00:00:00+00", "2023-01-01", 1, 0.0, 1),
        ("44", 2023, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2023-01-01 01:00:00+00", "2023-01-01", 2, 10.0, 1),
        ("44", 2024, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2024-01-01 00:00:00+00", "2024-01-01", 1, 2.0, 1),
        ("44", 2024, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2024-01-01 01:00:00+00", "2024-01-01", 2, 14.0, 1),
        ("44", 2024, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2024-01-02 00:00:00+00", "2024-01-02", 1, 3.0, 1),
        ("44", 2024, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2024-01-02 01:00:00+00", "2024-01-02", 2, 7.0, 1),
        ("44", 2020, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2020-02-29 12:00:00+00", "2020-02-29", 13, 1.0, 1),
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
        ("44", 2026, "44020001", "NANTES-BOUGUENAIS", 47.15, -1.61, 26,
         "2026-07-10 12:00:00+00", "2026-07-10", 14, 21.0, 1),
        ("35", 2024, "44020001", "RENNES-LONG-HISTORY", 48.11, -1.68, 36,
         "2024-01-01 00:00:00+00", "2024-01-01", 1, 8.0, 1),
        ("35", 2026, "44020001", "RENNES-LONG-HISTORY", 48.11, -1.68, 36,
         "2026-01-01 00:00:00+00", "2026-01-01", 1, 9.0, 1),
        ("35", 2026, "44020001", "RENNES-LONG-HISTORY", 48.11, -1.68, 36,
         "2026-06-15 00:00:00+00", "2026-06-15", 2, 20.0, 1),
        ("35", 2026, "35051001", "RENNES-SHORT-HISTORY", 48.12, -1.64, 42,
         "2026-01-01 00:00:00+00", "2026-01-01", 1, 9.0, 1),
        ("35", 2026, "35051001", "RENNES-SHORT-HISTORY", 48.12, -1.64, 42,
         "2026-06-15 00:00:00+00", "2026-06-15", 2, 20.0, 1),
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
    connection.execute("CREATE TABLE departments (department VARCHAR, department_name VARCHAR)")
    connection.executemany("INSERT INTO departments VALUES (?,?)", [
        ("44", "Loire-Atlantique"), ("35", "Ille-et-Vilaine"),
    ])
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
    assert filters(department="35").department == "35"
    with pytest.raises(ValueError, match="Invalid department"):
        filters(department="France")
    with pytest.raises(ValueError, match="at most 5"):
        filters(stations=tuple(str(i) for i in range(6)))
    with pytest.raises(ValueError, match="Unsupported metric"):
        filters(metric="DROP TABLE")
    with pytest.raises(ValueError, match="not valid"):
        filters(daily_statistics=("total",))


def test_catalog_metadata_and_missing_database(tmp_path: Path):
    root = make_catalog(tmp_path)
    assert validate_catalog(root).name == "weather.duckdb"
    assert available_departments(str(root)) == (
        ("35", "Ille-et-Vilaine"), ("44", "Loire-Atlantique"),
    )
    assert available_years(str(root), "44") == (2026, 2025, 2024, 2023, 2020)
    assert metric_metadata(str(root))["T"]["description_fr"] == "Official T"
    assert stations_for_period(root, "44", date(2025, 1, 1), date(2025, 1, 1)).height == 3
    with pytest.raises(FileNotFoundError):
        validate_catalog(tmp_path / "missing")


def test_department_period_defaults_and_best_station(tmp_path: Path):
    root = make_catalog(tmp_path)
    assert calendar_year_range(root, "44", 2026) == (date(2026, 1, 1), date(2026, 7, 10))
    assert calendar_year_range(root, "44", 2025) == (date(2025, 1, 1), date(2025, 12, 31))
    assert department_has_observations(root, "35", date(2026, 1, 1), date(2026, 1, 2))
    assert not department_has_observations(root, "35", date(2023, 1, 1), date(2023, 12, 31))
    assert best_station_for_period(
        root, "35", "T", date(2026, 1, 1), date(2026, 12, 31)
    ) == "44020001"
    stations = stations_for_period(root, "35", date(2026, 1, 1), date(2026, 12, 31))
    assert set(stations["NOM_USUEL"]) == {"RENNES-LONG-HISTORY", "RENNES-SHORT-HISTORY"}


def test_hourly_daily_and_quality_queries(tmp_path: Path):
    root = make_catalog(tmp_path)
    hourly = fetch_timeseries(root, filters())
    assert hourly["value"].to_list() == [4.0, 6.0]
    daily = fetch_timeseries(root, filters(resolution="daily"))
    assert daily["value"].to_list() == [5.0]
    assert fetch_timeseries(root, filters(resolution="daily", daily_statistics=("minimum",)))["value"].item() == 4.0
    rain = filters(metric="RR1", resolution="daily", daily_statistics=("total",))
    assert fetch_timeseries(root, rain)["value"].item() == pytest.approx(1.0)


def test_station_temperature_overview_uses_qualifying_station_days(tmp_path: Path):
    root = make_catalog(tmp_path)
    with duckdb.connect(str(root / "weather.duckdb")) as connection:
        connection.execute("""
            INSERT INTO hourly_core (
                department, year, NUM_POSTE, NOM_USUEL, LAT, LON, ALTI,
                observation_time_utc, local_date, local_hour, T, QT
            )
            SELECT '44', 2025, '44999001', 'QUALIFYING', 47.4, -1.4, 30,
                   TIMESTAMPTZ '2025-02-01 00:00:00+00' + number * INTERVAL 1 HOUR,
                   DATE '2025-02-01', number::UTINYINT, 10.0, 1
            FROM range(18) values(number)
        """)
        connection.execute("""
            INSERT INTO hourly_core (
                department, year, NUM_POSTE, NOM_USUEL, LAT, LON, ALTI,
                observation_time_utc, local_date, local_hour, T, QT
            )
            SELECT '44', 2025, '44999001', 'QUALIFYING-MOVED', 47.5, -1.3, 35,
                   TIMESTAMPTZ '2025-02-02 00:00:00+00' + number * INTERVAL 1 HOUR,
                   DATE '2025-02-02', number::UTINYINT, 20.0, 1
            FROM range(18) values(number)
        """)
        connection.execute("""
            INSERT INTO hourly_core (
                department, year, NUM_POSTE, NOM_USUEL, LAT, LON, ALTI,
                observation_time_utc, local_date, local_hour, T, QT
            )
            SELECT '35', 2025, '35999001', 'SPARSE', 48.1, -1.7, 40,
                   TIMESTAMPTZ '2025-02-01 00:00:00+00' + number * INTERVAL 1 HOUR,
                   DATE '2025-02-01', number::UTINYINT, 12.0, 1
            FROM range(17) values(number)
        """)
        connection.execute("""
            INSERT INTO hourly_core (
                department, year, NUM_POSTE, NOM_USUEL, LAT, LON, ALTI,
                observation_time_utc, local_date, local_hour, T, QT
            )
            SELECT '35', 2025, '35999002', 'DOUBTFUL', 48.2, -1.6, 45,
                   TIMESTAMPTZ '2025-02-01 00:00:00+00' + number * INTERVAL 1 HOUR,
                   DATE '2025-02-01', number::UTINYINT, 8.0, 2
            FROM range(18) values(number)
        """)

    all_quality = fetch_station_temperature_overview(
        root, date(2025, 2, 1), date(2025, 2, 2)
    )
    qualifying = all_quality.filter(all_quality["NUM_POSTE"] == "44999001").row(0, named=True)
    assert qualifying["period_mean_temperature"] == pytest.approx(15.0)
    assert qualifying["qualifying_days"] == 2
    assert qualifying["valid_hours"] == 36
    assert qualifying["NOM_USUEL"] == "QUALIFYING-MOVED"
    assert qualifying["LAT"] == pytest.approx(47.5)
    assert "35999001" not in all_quality["NUM_POSTE"].to_list()
    assert "35999002" in all_quality["NUM_POSTE"].to_list()

    filtered = fetch_station_temperature_overview(
        root, date(2025, 2, 1), date(2025, 2, 2), quality_mode="exclude_doubtful"
    )
    assert "35999002" not in filtered["NUM_POSTE"].to_list()


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


def test_historical_comparison_daily_envelopes_and_exact_date_exclusion(tmp_path: Path):
    root = make_catalog(tmp_path)
    row = fetch_historical_comparison(root, filters()).row(0, named=True)
    assert row["selected_average"] == pytest.approx(5.0)
    assert row["selected_minimum"] == pytest.approx(4.0)
    assert row["selected_maximum"] == pytest.approx(6.0)
    assert row["selected_contributing_hours"] == 2
    assert row["absolute_minimum"] == pytest.approx(0.0)
    assert row["absolute_maximum"] == pytest.approx(14.0)
    assert row["average_minimum"] == pytest.approx(1.0)
    assert row["average_maximum"] == pytest.approx(12.0)
    assert row["historical_days"] == 2
    assert row["historical_years"] == 2

    included = fetch_historical_comparison(
        root, filters(exclude_selected_period_from_baseline=False)
    ).row(0, named=True)
    assert included["average_minimum"] == pytest.approx(2.0)
    assert included["average_maximum"] == pytest.approx(10.0)
    assert included["historical_days"] == 3


def test_historical_comparison_keeps_missing_dates_and_leap_day(tmp_path: Path):
    root = make_catalog(tmp_path)
    comparison = fetch_historical_comparison(root, filters(end_date=date(2025, 1, 2)))
    assert comparison["observation_date"].to_list() == [date(2025, 1, 1), date(2025, 1, 2)]
    missing = comparison.filter(comparison["observation_date"] == date(2025, 1, 2)).row(0, named=True)
    assert missing["selected_average"] is None
    assert missing["absolute_minimum"] == pytest.approx(3.0)
    leap = fetch_historical_comparison(
        root, filters(start_date=date(2024, 2, 29), end_date=date(2024, 2, 29))
    ).row(0, named=True)
    assert leap["absolute_minimum"] == pytest.approx(1.0)
    assert leap["historical_years"] == 1


def test_historical_rainfall_uses_daily_totals(tmp_path: Path):
    root = make_catalog(tmp_path)
    rain = fetch_historical_comparison(root, filters(metric="RR1")).row(0, named=True)
    assert rain["selected_total"] == pytest.approx(1.0)
    assert rain["selected_average"] is None
    assert rain["absolute_minimum"] == pytest.approx(1.0)
    assert rain["absolute_maximum"] == pytest.approx(1.6)
    assert rain["historical_average_total"] == pytest.approx(1.3)


def test_historical_comparison_respects_quality_and_point_limit(tmp_path: Path, monkeypatch):
    root = make_catalog(tmp_path)
    period = filters(
        start_date=date(2024, 10, 27), end_date=date(2024, 10, 27),
        quality_mode="exclude_doubtful", exclude_selected_period_from_baseline=False,
    )
    selected = fetch_historical_comparison(root, period).row(0, named=True)
    assert selected["selected_average"] == pytest.approx(10.0)
    assert selected["selected_contributing_hours"] == 1
    monkeypatch.setattr(observation_queries, "MAX_CHART_POINTS", 0)
    with pytest.raises(QueryLimitError, match="fewer stations"):
        fetch_historical_comparison(root, filters())
