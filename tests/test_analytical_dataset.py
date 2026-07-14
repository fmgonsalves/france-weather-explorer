from __future__ import annotations

import gzip
import hashlib
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


def test_two_department_sync_preserves_global_metadata(tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    write_source(raw / "H_44_2010-2019.csv.gz", [base_row("2010010100")])
    write_source(raw / "H_44_2020-2024.csv.gz", [base_row("2020010100")])
    write_source(raw / "H_35_2020-2024.csv.gz", [base_row("2020010101")])

    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0
    assert dataset.sync_dataset(raw, analytics, reference, "35")[0] == 0

    departments = pq.read_table(analytics / "dimensions/departments.parquet").to_pylist()
    sources = pq.read_table(analytics / "metadata/source_files.parquet").to_pylist()
    assert [row["department"] for row in departments] == ["35", "44"]
    assert [(row["department"], row["filename"]) for row in sources] == [
        ("35", "H_35_2020-2024.csv.gz"),
        ("44", "H_44_2010-2019.csv.gz"),
        ("44", "H_44_2020-2024.csv.gz"),
    ]
    connection = duckdb.connect(str(analytics / "weather.duckdb"))
    assert connection.execute("select distinct department from hourly_core order by 1").fetchall() == [(35,), (44,)]
    assert connection.execute("select department from departments order by 1").fetchall() == [("35",), ("44",)]
    assert connection.execute("select department,count(*) from source_files group by 1 order by 1").fetchall() == [
        ("35", 1), ("44", 2),
    ]
    connection.close()

    source_35_before = next(
        row for row in sources if row["department"] == "35"
    )
    write_source(
        raw / "H_44_2020-2024.csv.gz",
        [base_row("2020010100"), base_row("2020010102")],
    )
    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0
    sources_after = pq.read_table(analytics / "metadata/source_files.parquet").to_pylist()
    assert next(row for row in sources_after if row["department"] == "35") == source_35_before
    (raw / "H_44_2010-2019.csv.gz").unlink()
    code, result = dataset.sync_dataset(raw, analytics, reference, "44")
    assert code == 0, result
    sources_after_removal = pq.read_table(
        analytics / "metadata/source_files.parquet"
    ).to_pylist()
    assert next(
        row for row in sources_after_removal if row["department"] == "35"
    ) == source_35_before
    assert not any(
        row["filename"] == "H_44_2010-2019.csv.gz"
        for row in sources_after_removal
    )


def test_metadata_only_repair_restores_all_manifests_deterministically(tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    write_source(raw / "H_44_2020-2024.csv.gz", [base_row("2020010100")])
    write_source(raw / "H_35_2020-2024.csv.gz", [base_row("2020010101")])
    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0
    assert dataset.sync_dataset(raw, analytics, reference, "35")[0] == 0

    protected = [
        *raw.glob("*.csv.gz"),
        *analytics.glob("facts/**/*.parquet"),
        *analytics.glob("extensions/**/*.parquet"),
        *analytics.glob("dimensions/stations/**/*.parquet"),
        *analytics.glob("dimensions/station_metadata_history/**/*.parquet"),
        *analytics.glob("manifests/*.json"),
    ]
    before_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected
    }
    department_path = analytics / "dimensions/departments.parquet"
    source_path = analytics / "metadata/source_files.parquet"
    pq.write_table(
        pq.read_table(department_path).filter(dataset.pa.array([True, False])),
        department_path,
    )
    source_table = pq.read_table(source_path)
    source_mask = dataset.pa.array([
        value.as_py() == "35" for value in source_table["department"]
    ])
    pq.write_table(
        source_table.filter(source_mask),
        source_path,
    )
    assert dataset.audit_global_metadata(analytics).consistent is False

    result = dataset.repair_global_metadata(analytics)
    assert result["status"] == "success"
    assert result["department_count"] == 2
    assert result["source_file_count"] == 2
    assert dataset.audit_global_metadata(analytics).consistent is True
    assert [
        row["department"]
        for row in pq.read_table(department_path).to_pylist()
    ] == ["35", "44"]
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected
    } == before_hashes

    first_departments = department_path.read_bytes()
    first_sources = source_path.read_bytes()
    dataset.repair_global_metadata(analytics)
    assert department_path.read_bytes() == first_departments
    assert source_path.read_bytes() == first_sources


def test_repair_rejects_orphan_partitions_and_rolls_back_refresh_failure(
    tmp_path: Path, monkeypatch,
):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    write_source(raw / "H_44_2020-2024.csv.gz", [base_row("2020010100")])
    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0
    orphan = analytics / "facts/hourly_core/department=35"
    orphan.mkdir(parents=True)
    try:
        dataset.repair_global_metadata(analytics)
    except ValueError as error:
        assert "facts departments differ" in str(error)
    else:
        raise AssertionError("orphan facts must prevent metadata repair")
    orphan.rmdir()

    department_path = analytics / "dimensions/departments.parquet"
    source_path = analytics / "metadata/source_files.parquet"
    before = (department_path.read_bytes(), source_path.read_bytes())

    def fail_refresh(_root):
        raise OSError("catalog locked")

    monkeypatch.setattr(dataset, "refresh_duckdb", fail_refresh)
    try:
        dataset.repair_global_metadata(analytics)
    except OSError as error:
        assert "catalog locked" in str(error)
    else:
        raise AssertionError("refresh failure must fail repair")
    assert (department_path.read_bytes(), source_path.read_bytes()) == before


def test_metadata_audit_rejects_malformed_manifests_and_reports_duplicates(tmp_path: Path):
    malformed = tmp_path / "malformed"
    (malformed / "manifests").mkdir(parents=True)
    (malformed / "manifests/department=44.json").write_text("{bad json")
    try:
        dataset.audit_global_metadata(malformed)
    except ValueError as error:
        assert "Invalid manifest" in str(error)
    else:
        raise AssertionError("malformed manifests must fail metadata audit")

    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    write_source(raw / "H_44_2020-2024.csv.gz", [base_row("2020010100")])
    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0
    department_path = analytics / "dimensions/departments.parquet"
    source_path = analytics / "metadata/source_files.parquet"
    departments = pq.read_table(department_path)
    sources = pq.read_table(source_path)
    pq.write_table(dataset.pa.concat_tables([departments, departments]), department_path)
    pq.write_table(dataset.pa.concat_tables([sources, sources]), source_path)
    audit = dataset.audit_global_metadata(analytics)
    assert audit.consistent is False
    assert any("duplicate department" in issue for issue in audit.issues)
    assert any("duplicate department/filename" in issue for issue in audit.issues)


def test_sync_progress_reports_stages_archives_and_rows(tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    write_source(raw / "H_44_2020-2024.csv.gz", [base_row("2020010100")])

    class Recorder:
        def __init__(self):
            self.events = []

        def stage(self, name): self.events.append(("stage", name))
        def sources_started(self, total): self.events.append(("sources", total))
        def source_started(self, filename): self.events.append(("source", filename))
        def rows_processed(self, count): self.events.append(("rows", count))
        def source_finished(self): self.events.append(("finished",))
        def close(self): self.events.append(("close",))

    recorder = Recorder()
    assert dataset.sync_dataset(
        raw, analytics, reference, "44", progress=recorder
    )[0] == 0
    stages = [event[1] for event in recorder.events if event[0] == "stage"]
    assert stages == [
        "planning", "source processing", "compaction", "validation", "dimensions",
        "metadata", "promotion", "manifest", "DuckDB refresh",
    ]
    assert ("sources", 1) in recorder.events
    assert ("rows", 1) in recorder.events
    assert recorder.events[-1] == ("close",)


def test_global_dataset_status_summarizes_materialized_and_raw_only_departments(tmp_path: Path):
    raw, analytics, reference = tmp_path / "raw", tmp_path / "analytics", tmp_path / "reference"
    raw.mkdir()
    reference_dictionary(reference)
    write_source(raw / "H_44_2020-2024.csv.gz", [base_row("2020010100")])
    write_source(raw / "H_35_2020-2024.csv.gz", [base_row("2020010101")])
    write_source(raw / "H_75_2020-2024.csv.gz", [base_row("2020010102")])
    assert dataset.sync_dataset(raw, analytics, reference, "44")[0] == 0
    assert dataset.sync_dataset(raw, analytics, reference, "35")[0] == 0

    summary = dataset.summarize_dataset_status(raw, analytics)
    assert [row["department"] for row in summary["departments"]] == ["35", "44"]
    assert {row["status"] for row in summary["departments"]} == {"up-to-date"}
    assert summary["raw_only_departments"] == ("75",)
    assert summary["fact_departments"] == 2
    assert summary["station_departments"] == 2
