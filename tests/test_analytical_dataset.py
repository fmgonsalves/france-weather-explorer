from __future__ import annotations

import gzip
import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from weather_analysis import analytical_dataset as dataset


HEADER = ";".join([
    "NUM_POSTE", "NOM_USUEL", "LAT", "LON", "ALTI", "AAAAMMJJHH",
    "T", "QT", "RR1", "QRR1", "FF", "QFF", "DD", "QDD",
    "T10", "QT10", "TMER", "QTMER", "NEIGETOT", "QNEIGETOT",
])


def write_source(path: Path, rows: list[list[str]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as output:
        output.write(HEADER + "\n")
        for row in rows:
            output.write(";".join(row) + "\n")


def base_row(timestamp: str, temperature: str = "12.5") -> list[str]:
    return [
        "44020001", "NANTES-BOUGUENAIS", "47.150", "-1.600", "27", timestamp,
        temperature, "1", "0.2", "9", "3.5", "1", "270", "1",
        "", "", "", "", "", "",
    ]


def reference_dictionary(root: Path) -> Path:
    root.mkdir(parents=True)
    path = root / "H_descriptif_champs.csv"
    path.write_text("T : température sous abri (en °C)\nRR1 : pluie horaire (en mm)\n", encoding="utf-8")
    return path


def test_schema_uses_source_mnemonics_and_typed_quality_fields():
    schema = dataset.table_schema("hourly_core")
    assert schema.field("T").type == dataset.pa.float32()
    assert schema.field("QT").type == dataset.pa.uint8()
    assert schema.field("DD").type == dataset.pa.uint16()
    assert schema.metadata[b"dataset_version"] == b"1"


def test_sync_materializes_hourly_extensions_dst_and_catalog(tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    rows = [
        base_row("2024033100"),
        base_row("2024033101"),
        base_row("2024102700"),
        base_row("2024102701"),
    ]
    rows[0][14:16] = ["8.2", "1"]
    rows[1][16:18] = ["11.0", "1"]
    rows[2][18:20] = ["2", "1"]
    write_source(raw / "H_44_previous-2020-2024.csv.gz", rows)

    code, result = dataset.sync_dataset(raw, analytics, reference, "44")

    assert code == 0, result
    assert result["counts"]["core_rows"] == 4
    assert result["counts"]["extension_rows"] == {
        "hourly_surface": 1, "hourly_marine": 1, "hourly_snow": 1
    }
    core_path = next((analytics / "facts/hourly_core/department=44/year=2024").glob("*.parquet"))
    core = pq.read_table(core_path).to_pylist()
    assert [row["local_hour"] for row in core[:2]] == [1, 3]
    assert [row["local_hour"] for row in core[2:]] == [2, 2]
    assert [row["utc_offset_minutes"] for row in core[2:]] == [120, 60]
    assert core[0]["source_row_number"] == 2
    assert core[0]["QT"] == 1
    assert core[0]["T"] == 12.5
    assert (analytics / "dimensions/stations/department=44/part-00000.parquet").exists()
    assert (analytics / "dimensions/metrics.parquet").exists()
    manifest = json.loads((analytics / "manifests/department=44.json").read_text())
    assert manifest["materialized_years"] == [2024]
    connection = duckdb.connect(str(analytics / "weather.duckdb"))
    assert connection.execute("select count(*) from hourly_core").fetchone()[0] == 4
    assert connection.execute("select count(*) from hourly_surface").fetchone()[0] == 1
    connection.close()


def test_unchanged_sync_is_noop_and_reuses_digest(monkeypatch, tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    write_source(raw / "H_44_2020-2024.csv.gz", [base_row("2020010100")])
    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0

    def fail_hash(path):
        raise AssertionError("unchanged source should reuse its persisted SHA-256")

    monkeypatch.setattr(dataset, "sha256_file", fail_hash)
    plan = dataset.plan_sync(raw, analytics, "44")
    assert plan.no_op is True
    code, result = dataset.sync_dataset(raw, analytics, reference, "44")
    assert code == 0
    assert result["status"] == "up-to-date"
    connection = duckdb.connect(str(analytics / "weather.duckdb"))
    assert connection.execute("select count(*) from hourly_surface").fetchone()[0] == 0
    assert connection.execute("select count(*) from hourly_marine").fetchone()[0] == 0
    assert connection.execute("select count(*) from hourly_snow").fetchone()[0] == 0


def test_changed_source_rebuilds_affected_year(tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    source = raw / "H_44_2020-2024.csv.gz"
    write_source(source, [base_row("2020010100")])
    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0
    write_source(source, [base_row("2020010100"), base_row("2020010101")])
    code, result = dataset.sync_dataset(raw, analytics, reference, "44")
    assert code == 0, result
    assert result["counts"]["core_rows"] == 2
    connection = duckdb.connect(str(analytics / "weather.duckdb"))
    assert connection.execute("select count(*) from hourly_core").fetchone()[0] == 2


def test_duplicate_failure_does_not_publish_partition(tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    write_source(
        raw / "H_44_2020-2024.csv.gz",
        [base_row("2020010100"), base_row("2020010100")],
    )
    code, result = dataset.sync_dataset(raw, analytics, reference, "44")
    assert code == 1
    assert "Duplicate station-hour key" in result["error"]
    assert not (analytics / "facts/hourly_core").exists()
    assert list((analytics / "metadata/failures").glob("*.json"))


def test_invalid_timestamp_failure_preserves_previous_dataset(tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    source = raw / "H_44_2020-2024.csv.gz"
    write_source(source, [base_row("2020010100")])
    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0
    original = next((analytics / "facts/hourly_core").rglob("*.parquet")).read_bytes()
    write_source(source, [base_row("not-a-time")])
    code, result = dataset.sync_dataset(raw, analytics, reference, "44")
    assert code == 1
    assert "invalid AAAAMMJJHH" in result["error"]
    assert next((analytics / "facts/hourly_core").rglob("*.parquet")).read_bytes() == original


def test_overseas_department_is_rejected(tmp_path: Path):
    try:
        dataset.plan_sync(tmp_path, tmp_path / "analytics", "971")
    except ValueError as error:
        assert "metropolitan" in str(error)
    else:
        raise AssertionError("overseas time semantics must be rejected in v1")


def test_explicit_rebuild_and_metric_metadata(tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    write_source(raw / "H_44_2020-2024.csv.gz", [base_row("2020010100")])
    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0
    code, result = dataset.sync_dataset(raw, analytics, reference, "44", rebuild=True)
    assert code == 0
    assert result["status"] == "synchronized"
    metrics = pq.read_table(analytics / "dimensions/metrics.parquet").to_pylist()
    temperature = next(row for row in metrics if row["source_mnemonic"] == "T")
    assert temperature["official_description_fr"]
    assert temperature["official_unit_text"] == "°C"
    assert temperature["english_label"] == "Air temperature"
    assert temperature["english_text_curated"] is True


def test_transformation_version_change_requires_explicit_rebuild(tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    write_source(raw / "H_44_2020-2024.csv.gz", [base_row("2020010100")])
    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0
    manifest_path = analytics / "manifests/department=44.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["transformation_version"] = "0.9.0"
    manifest_path.write_text(json.dumps(manifest))
    plan = dataset.plan_sync(raw, analytics, "44")
    assert plan.schema_rebuild_required is True
    code, result = dataset.sync_dataset(raw, analytics, reference, "44")
    assert code == 1
    assert result["status"] == "schema-rebuild-required"
