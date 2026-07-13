from __future__ import annotations

from pathlib import Path

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
