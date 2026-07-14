from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from weather_analysis import cli
from weather_analysis.meteo_france import FileStatus, Inspection, Resource, partial_path


def resource(size: int = 4) -> Resource:
    return Resource(
        resource_id="id",
        title="HOR_departement_44_periode_2010-2019",
        url="https://example.test/H_44_2010-2019.csv.gz",
        format="csv.gz",
        last_modified=None,
        expected_size=size,
        filename="H_44_2010-2019.csv.gz",
        department="44",
        period=(2010, 2019),
    )


def test_status_is_non_mutating_and_accepts_repeated_departments(monkeypatch, tmp_path: Path):
    calls = []
    monkeypatch.setattr(cli, "fetch_resources", lambda session, category: [resource()])
    monkeypatch.setattr(cli, "write_manifest", lambda *args: calls.append(args))

    result = cli.run([
        "meteo-france", "status", "--department", "44", "--department", "75",
        "--output-dir", str(tmp_path),
    ])

    assert result == 1
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_download_skips_complete_file(monkeypatch, tmp_path: Path):
    item = resource()
    (tmp_path / item.filename).write_bytes(b"good")
    downloads = []
    monkeypatch.setattr(cli, "fetch_resources", lambda session, category: [item])
    monkeypatch.setattr(cli, "download_resource", lambda *args: downloads.append(args))

    result = cli.run(["meteo-france", "download", "--output-dir", str(tmp_path)])

    assert result == 0
    assert downloads == []
    assert (tmp_path / "selected_resources.json").exists()


def test_download_failure_returns_nonzero(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cli, "fetch_resources", lambda session, category: [resource()])

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr(cli, "download_resource", fail)
    assert cli.run(["meteo-france", "download", "--output-dir", str(tmp_path)]) == 1


def test_category_and_default_output(monkeypatch):
    observed = {}

    def fetch(session, category):
        observed["category"] = category
        return []

    monkeypatch.setattr(cli, "fetch_resources", fetch)
    assert cli.run(["meteo-france", "status", "--category", "complementary"]) == 0
    assert observed["category"] == "complementary"


def test_download_removes_verified_redundant_partial(monkeypatch, tmp_path: Path):
    item = resource()
    final = tmp_path / item.filename
    final.write_bytes(b"good")
    partial_path(final).write_bytes(b"good")
    inspection = Inspection(
        status=FileStatus.COMPLETE,
        server_size=4,
        size_source="server",
        catalog_mismatch=True,
        redundant_partial=True,
    )
    monkeypatch.setattr(cli, "fetch_resources", lambda session, category: [item])
    monkeypatch.setattr(
        cli, "inspect_resources", lambda session, resources, output: {item.filename: inspection}
    )

    assert cli.run(["meteo-france", "download", "--output-dir", str(tmp_path)]) == 0
    assert final.read_bytes() == b"good"
    assert not partial_path(final).exists()


def test_dashboard_cli_defaults_and_options(monkeypatch, tmp_path: Path):
    observed = {}

    def run_dashboard(analytics_dir, host, port, debug):
        observed.update(analytics_dir=analytics_dir, host=host, port=port, debug=debug)

    monkeypatch.setattr(cli, "run_dashboard", run_dashboard)
    result = cli.run([
        "dashboard", "dash", "--analytics-dir", str(tmp_path),
        "--host", "0.0.0.0", "--port", "9000", "--debug",
    ])
    assert result == 0
    assert observed == {
        "analytics_dir": tmp_path, "host": "0.0.0.0", "port": 9000, "debug": True,
    }


def test_dataset_repair_metadata_cli(monkeypatch, tmp_path: Path, capsys):
    observed = {}

    def repair(analytics_dir):
        observed["analytics_dir"] = analytics_dir
        return {
            "status": "success", "department_count": 2, "source_file_count": 30,
            "report_path": str(tmp_path / "report.json"),
        }

    monkeypatch.setattr(cli, "repair_global_metadata", repair)
    result = cli.run([
        "meteo-france", "dataset", "repair-metadata",
        "--analytics-dir", str(tmp_path),
    ])
    assert result == 0
    assert observed["analytics_dir"] == tmp_path
    assert "Source archives: 30" in capsys.readouterr().out


def test_dataset_status_reports_global_metadata_drift(monkeypatch, tmp_path: Path, capsys):
    plan = SimpleNamespace(
        department="44", sources=(1,), changed_files=(), removed_files=(),
        affected_years=(), schema_rebuild_required=False, no_op=True,
    )
    audit = SimpleNamespace(
        consistent=False, expected_departments=("35", "44"),
        observed_departments=("35",), expected_source_files=30,
        observed_source_files=14, structural_issues=(), issues=("missing department 44",),
    )
    monkeypatch.setattr(cli, "plan_sync", lambda *args: plan)
    monkeypatch.setattr(cli, "audit_global_metadata", lambda *args: audit)
    result = cli.run([
        "meteo-france", "dataset", "status", "--department", "44",
        "--raw-dir", str(tmp_path), "--analytics-dir", str(tmp_path),
    ])
    assert result == 1
    output = capsys.readouterr().out
    assert "Shared catalog metadata: inconsistent" in output
    assert "Metadata departments: expected=2; observed=1" in output
    assert "Missing metadata departments: 44" in output
    assert "Expected metadata department list" not in output


def test_dataset_status_verbose_prints_complete_department_lists(
    monkeypatch, tmp_path: Path, capsys,
):
    plan = SimpleNamespace(
        department="35", sources=(1,), changed_files=(), removed_files=(),
        affected_years=(), schema_rebuild_required=False, no_op=True,
    )
    audit = SimpleNamespace(
        consistent=True, expected_departments=("35", "44"),
        observed_departments=("35", "44"), expected_source_files=30,
        observed_source_files=30, structural_issues=(), issues=(),
    )
    monkeypatch.setattr(cli, "plan_sync", lambda *args: plan)
    monkeypatch.setattr(cli, "audit_global_metadata", lambda *args: audit)
    result = cli.run([
        "meteo-france", "dataset", "status", "--department", "35",
        "--raw-dir", str(tmp_path), "--analytics-dir", str(tmp_path), "--verbose",
    ])
    assert result == 0
    output = capsys.readouterr().out
    assert "Metadata departments: expected=2; observed=2" in output
    assert "Expected metadata department list: 35,44" in output
    assert "Observed metadata department list: 35,44" in output


def test_dataset_sync_no_progress_uses_quiet_reporter(monkeypatch, tmp_path: Path):
    observed = {}

    def sync_dataset(**kwargs):
        observed.update(kwargs)
        return 0, {"status": "up-to-date"}

    monkeypatch.setattr(cli, "sync_dataset", sync_dataset)
    result = cli.run([
        "meteo-france", "dataset", "sync", "--department", "44",
        "--raw-dir", str(tmp_path), "--analytics-dir", str(tmp_path),
        "--reference-dir", str(tmp_path), "--no-progress",
    ])
    assert result == 0
    assert isinstance(observed["progress"], cli.NullProgressReporter)


def test_global_dataset_status_is_compact_and_can_list_raw_only(
    monkeypatch, tmp_path: Path, capsys,
):
    audit = SimpleNamespace(
        consistent=True, expected_departments=("35", "44"),
        observed_departments=("35", "44"), expected_source_files=30,
        observed_source_files=30, structural_issues=(), issues=(),
    )
    summary = {
        "departments": [
            {
                "department": "35", "status": "up-to-date", "source_archives": 14,
                "changed_archives": 0, "removed_archives": 0,
                "affected_years": (), "error": None,
            },
            {
                "department": "44", "status": "pending", "source_archives": 16,
                "changed_archives": 1, "removed_archives": 0,
                "affected_years": (2025, 2026), "error": None,
            },
        ],
        "audit": audit, "raw_only_departments": ("75", "85"),
        "fact_departments": 2, "station_departments": 2, "failed_runs": 0,
    }
    monkeypatch.setattr(cli, "summarize_dataset_status", lambda *args: summary)
    result = cli.run([
        "meteo-france", "dataset", "status",
        "--raw-dir", str(tmp_path), "--analytics-dir", str(tmp_path),
    ])
    assert result == 0
    output = capsys.readouterr().out
    assert "Materialized departments: 2" in output
    assert "Departments with pending changes: 1" in output
    assert "44  pending" in output
    assert "35  up-to-date" not in output
    assert "Raw-only departments: 2" in output
    assert "Raw-only department list" not in output

    result = cli.run([
        "meteo-france", "dataset", "status",
        "--raw-dir", str(tmp_path), "--analytics-dir", str(tmp_path),
        "--verbose", "--include-unmaterialized",
    ])
    assert result == 0
    output = capsys.readouterr().out
    assert "35  up-to-date" in output
    assert "Raw-only department list: 75,85" in output
