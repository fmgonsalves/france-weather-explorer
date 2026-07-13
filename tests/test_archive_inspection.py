from __future__ import annotations

import gzip
from pathlib import Path

import pyarrow.parquet as pq

from weather_analysis import archive_inspection as inspection


def write_archive(path: Path, text: str, encoding: str = "utf-8") -> None:
    with gzip.open(path, "wb") as output:
        output.write(text.encode(encoding))


def test_discovery_filters_and_ignores_parts(tmp_path: Path):
    write_archive(tmp_path / "H_44_2010-2019.csv.gz", "A;B\n1;2\n")
    write_archive(tmp_path / "H_75_2020-2024.csv.gz", "A;B\n1;2\n")
    write_archive(tmp_path / "H_44_2010-2019.csv.gz.part", "A;B\n1;2\n")
    selected = inspection.discover_archives(tmp_path, {"44"}, 2015, 2020)
    assert [item.path.name for item in selected] == ["H_44_2010-2019.csv.gz"]


def test_schema_id_is_ordered_and_stable():
    assert inspection.schema_id(["A", "B"]) == inspection.schema_id(["A", "B"])
    assert inspection.schema_id(["A", "B"]) != inspection.schema_id(["B", "A"])


def test_encoding_fallback_and_delimiter(tmp_path: Path):
    path = tmp_path / "H_44_2010-2019.csv.gz"
    write_archive(path, "NOM;VALEUR\nCafé;1\n", "cp1252")
    encoding, delimiter = inspection.detect_encoding_and_delimiter(path)
    assert encoding == "cp1252"
    assert delimiter == ";"


def test_inventory_counts_rows_stations_dates_and_errors(tmp_path: Path):
    path = tmp_path / "H_44_2010-2019.csv.gz"
    write_archive(
        path,
        "NUM_POSTE;AAAAMMJJHH;T\n"
        "1;2010010100;2.0\n"
        "1;invalid;3.0\n"
        "2;2010010102\n",
    )
    archive = inspection.discover_archives(tmp_path)[0]
    result = inspection.inspect_archive(archive, inventory=True)
    assert result.row_count == 3
    assert result.station_count == 1
    assert result.invalid_timestamps == 1
    assert result.malformed_rows == 1
    assert result.earliest_timestamp == "2010-01-01T00:00:00"


def test_empty_archive_records_error(tmp_path: Path):
    path = tmp_path / "H_44_2010-2019.csv.gz"
    write_archive(path, "")
    result = inspection.inspect_archive(inspection.discover_archives(tmp_path)[0], False)
    assert "no header" in (result.inspection_error or "")


def test_profile_reports_duplicate_keys_and_numeric_ranges(tmp_path: Path):
    path = tmp_path / "H_44_2010-2019.csv.gz"
    write_archive(
        path,
        "NUM_POSTE;AAAAMMJJHH;T\n1;2010010100;2.5\n1;2010010100;4.5\n",
    )
    rows, duplicates = inspection.profile_archive(
        inspection.discover_archives(tmp_path)[0], ("NUM_POSTE", "AAAAMMJJHH", "T")
    )
    temperature = next(row for row in rows if row["column"] == "T")
    assert duplicates == 1
    assert temperature["inferred_type"] == "numeric"
    assert temperature["numeric_min"] == 2.5
    assert temperature["numeric_max"] == 4.5


class DocumentationSession:
    class Response:
        content = "T : température (en °C)\n".encode()

        def raise_for_status(self):
            return None

    def get(self, *args, **kwargs):
        return self.Response()


def test_run_writes_reports_without_changing_source(monkeypatch, tmp_path: Path):
    root, reports = tmp_path / "raw", tmp_path / "reports"
    root.mkdir()
    path = root / "H_44_2010-2019.csv.gz"
    write_archive(path, "NUM_POSTE;AAAAMMJJHH;T;QT\n1;2010010100;2.5;1\n")
    before = path.read_bytes()
    code, summary = inspection.run_inspection(
        root, reports, "profile", None, None, None, 1, DocumentationSession()
    )
    assert code == 0
    assert summary == {"selected": 1, "schemas": 1, "errors": 0}
    assert path.read_bytes() == before
    assert not list(root.glob("*.csv"))
    assert (reports / "schemas.json").exists()
    assert pq.read_table(reports / "archive_inventory.parquet").num_rows == 1
    assert pq.read_table(reports / "profile_summary.parquet").num_rows == 4
    dictionary = pq.read_table(reports / "column_dictionary.parquet").to_pylist()
    assert next(row for row in dictionary if row["column"] == "QT")["quality_for"] == "T"


def test_cached_inventory_is_reused(monkeypatch, tmp_path: Path):
    root, reports = tmp_path / "raw", tmp_path / "reports"
    root.mkdir()
    write_archive(root / "H_44_2010-2019.csv.gz", "NUM_POSTE;AAAAMMJJHH\n1;2010010100\n")
    session = DocumentationSession()
    inspection.run_inspection(root, reports, "inventory", None, None, None, 1, session)

    def fail(*args, **kwargs):
        raise AssertionError("unchanged archive should have been reused")

    monkeypatch.setattr(inspection, "inspect_archive", fail)
    code, summary = inspection.run_inspection(
        root, reports, "inventory", None, None, None, 1, session
    )
    assert code == 0
    assert summary["selected"] == 1
