from __future__ import annotations

import gzip
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from weather_analysis import cli
from weather_analysis import department_exploration as exploration


HEADER = "NUM_POSTE;NOM_USUEL;LAT;LON;ALTI;AAAAMMJJHH;T;QT;RR1;QRR1\n"


def write_archive(path: Path, rows: list[str]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as output:
        output.write(HEADER)
        output.writelines(f"{row}\n" for row in rows)


def test_explore_requires_exactly_one_department():
    parser = cli.build_parser()
    try:
        cli.run(["meteo-france", "inspect", "--mode", "explore"])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("CLI should reject explore without a department")


def test_full_department_exploration_and_restart(monkeypatch, tmp_path: Path):
    root, reports = tmp_path / "raw", tmp_path / "reports"
    root.mkdir()
    first = root / "H_44_2010-2019.csv.gz"
    second = root / "H_44_previous-2020-2024.csv.gz"
    write_archive(first, [
        "44001001;STATION A;47.0;-1.0;10;2019123123;2.5;1;;",
        "44001001;STATION A;47.0;-1.0;10;invalid;3.0;9;0.0;1",
        "44002001;STATION B;47.1;-1.1;20;2019010100;;9;1.2;1",
        "malformed;row",
    ])
    write_archive(second, [
        "44001001;STATION A RENAMED;47.0;-1.0;11;2019123123;2.5;1;0.0;1",
        "44001001;STATION A RENAMED;47.0;-1.0;11;2020010100;4.0;9;0.0;1",
    ])
    before = {path.name: path.read_bytes() for path in (first, second)}

    code, summary = exploration.run_department_exploration(
        root, reports, "44", None, None, workers=2
    )

    assert code == 0
    assert summary["archives"] == 2
    assert summary["stations"] == 2
    assert summary["duplicates"] == 1
    assert summary["cross_archive_overlaps"] == 1
    assert summary["metadata_conflicts"] == 2
    assert not (reports / ".explore_tmp").exists()
    assert {path.name: path.read_bytes() for path in (first, second)} == before

    inventory = pq.read_table(reports / "archive_inventory.parquet").to_pylist()
    assert sum(row["malformed_rows"] for row in inventory) == 1
    assert sum(row["invalid_timestamps"] for row in inventory) == 1
    station_rows = pq.read_table(reports / "station_inventory.parquet").to_pylist()
    station_a = next(row for row in station_rows if row["station_id"] == "44001001")
    assert station_a["temperature_count"] == 3
    profiles = pq.read_table(reports / "column_profile.parquet").to_pylist()
    old_temperature = next(
        row for row in profiles
        if row["source_file"] == first.name and row["column"] == "T"
    )
    assert old_temperature["populated_count"] == 2
    quality = pq.read_table(reports / "quality_code_profile.parquet").to_pylist()
    assert {row["value"] for row in quality if row["column"] == "QT"} == {"1", "9"}
    duplicates = pl.read_parquet(reports / "duplicate_observations.parquet")
    assert duplicates.height == 1

    def fail(*args, **kwargs):
        raise AssertionError("unchanged completed exploration should be reused")

    monkeypatch.setattr(exploration, "explore_archive", fail)
    cached_code, cached_summary = exploration.run_department_exploration(
        root, reports, "44", None, None, workers=2
    )
    assert cached_code == 0
    assert cached_summary == summary


def test_year_filter_selects_overlapping_archives(tmp_path: Path):
    root, reports = tmp_path / "raw", tmp_path / "reports"
    root.mkdir()
    write_archive(root / "H_44_2010-2019.csv.gz", [
        "44001001;A;47;-1;10;2019010100;2;1;0;1"
    ])
    write_archive(root / "H_44_2020-2024.csv.gz", [
        "44001001;A;47;-1;10;2020010100;2;1;0;1"
    ])
    code, summary = exploration.run_department_exploration(
        root, reports, "44", 2020, 2020, workers=1
    )
    assert code == 0
    assert summary["archives"] == 1
