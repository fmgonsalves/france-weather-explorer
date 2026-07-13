from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import requests

from weather_analysis.meteo_france import (
    FileStatus,
    Resource,
    classify_file,
    download_resource,
    extract_department,
    extract_period,
    inspect_resource,
    inspect_resources,
    partial_path,
    period_overlaps,
    select_resources,
)


def resource(filename: str = "H_75_2010-2019.csv.gz", size: int | None = 4) -> Resource:
    return Resource(
        resource_id="resource-id",
        title="HOR_departement_75_periode_2010-2019",
        url=f"https://example.test/{filename}",
        format="csv.gz",
        last_modified="2026-01-01T00:00:00+00:00",
        expected_size=size,
        filename=filename,
        department="75",
        period=(2010, 2019),
    )


@pytest.mark.parametrize(
    ("title", "filename", "department", "period"),
    [
        ("HOR_departement_2A_periode_2010-2019", "file.csv.gz", "2A", (2010, 2019)),
        ("HOR_departement_971_periode_2020-2024", "file.csv.gz", "971", (2020, 2024)),
        ("", "H_988_latest-2025-2026.csv.gz", "988", (2025, 2026)),
        ("", "H_44_previous-2020-2024.csv.gz", "44", (2020, 2024)),
    ],
)
def test_extract_metadata(title, filename, department, period):
    raw = {"title": title, "url": f"https://example.test/{filename}", "id": "id"}
    assert extract_department(raw) == department
    assert extract_period(raw) == period


@pytest.mark.parametrize(
    ("period", "start", "end", "expected"),
    [
        ((2010, 2019), 2019, 2025, True),
        ((2020, 2024), 2010, 2020, True),
        ((2010, 2019), 2020, None, False),
        (None, 2020, None, False),
        (None, None, None, True),
    ],
)
def test_period_overlaps(period, start, end, expected):
    assert period_overlaps(period, start, end) is expected


def test_select_resources_filters_department_and_period():
    paris = resource()
    loire = replace(paris, filename="H_44_2020-2024.csv.gz", department="44", period=(2020, 2024))
    assert select_resources([paris, loire], {"44"}, 2024, 2025) == [loire]


def test_classify_file_states(tmp_path: Path):
    item = resource()
    assert classify_file(item, tmp_path) == FileStatus.MISSING

    partial_path(tmp_path / item.filename).write_bytes(b"x")
    assert classify_file(item, tmp_path) == FileStatus.PARTIAL

    (tmp_path / item.filename).write_bytes(b"bad")
    assert classify_file(item, tmp_path) == FileStatus.INVALID

    (tmp_path / item.filename).write_bytes(b"good")
    assert classify_file(item, tmp_path) == FileStatus.COMPLETE
    assert classify_file(replace(item, expected_size=None), tmp_path) == FileStatus.UNKNOWN_SIZE


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        error: Exception | None = None,
        content_length: int | None = None,
    ):
        self.chunks = chunks
        self.error = error
        self.headers = (
            {"Content-Length": str(content_length)} if content_length is not None else {}
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.error:
            raise self.error

    def iter_content(self, chunk_size: int):
        yield from self.chunks


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.response

    def head(self, *args, **kwargs):
        self.calls += 1
        return self.response


def test_download_overwrites_partial_and_promotes_atomically(tmp_path: Path):
    item = resource()
    partial = partial_path(tmp_path / item.filename)
    partial.write_bytes(b"old content")
    session = FakeSession(FakeResponse([b"go", b"od"]))

    download_resource(session, item, tmp_path)

    assert (tmp_path / item.filename).read_bytes() == b"good"
    assert not partial.exists()


def test_download_prefers_server_size_over_catalog(tmp_path: Path):
    item = resource(size=3)
    result = download_resource(
        FakeSession(FakeResponse([b"good"], content_length=4)), item, tmp_path
    )
    assert (tmp_path / item.filename).read_bytes() == b"good"
    assert result.size_source == "server"
    assert result.catalog_mismatch is True


def test_size_mismatch_retains_partial(tmp_path: Path):
    item = resource(size=10)
    with pytest.raises(ValueError, match="size mismatch"):
        download_resource(FakeSession(FakeResponse([b"short"])), item, tmp_path)
    assert partial_path(tmp_path / item.filename).read_bytes() == b"short"
    assert not (tmp_path / item.filename).exists()


def test_request_failure_retains_existing_partial(tmp_path: Path):
    item = resource()
    partial = partial_path(tmp_path / item.filename)
    partial.write_bytes(b"existing")
    response = FakeResponse([], requests.ConnectionError("offline"))
    with pytest.raises(requests.ConnectionError):
        download_resource(FakeSession(response), item, tmp_path)
    assert partial.read_bytes() == b"existing"


def test_catalog_match_does_not_make_head_request(tmp_path: Path):
    item = resource()
    (tmp_path / item.filename).write_bytes(b"good")
    session = FakeSession(FakeResponse([], content_length=99))
    inspection = inspect_resource(session, item, tmp_path)
    assert inspection.status == FileStatus.COMPLETE
    assert inspection.size_source == "catalog"
    assert session.calls == 0


def test_server_match_overrides_catalog_and_detects_redundant_partial(tmp_path: Path):
    item = resource(size=3)
    (tmp_path / item.filename).write_bytes(b"good")
    partial_path(tmp_path / item.filename).write_bytes(b"good")
    inspection = inspect_resource(
        FakeSession(FakeResponse([], content_length=4)), item, tmp_path
    )
    assert inspection.status == FileStatus.COMPLETE
    assert inspection.size_source == "server"
    assert inspection.catalog_mismatch is True
    assert inspection.redundant_partial is True


def test_server_and_catalog_mismatch_is_invalid(tmp_path: Path):
    item = resource(size=3)
    (tmp_path / item.filename).write_bytes(b"wrong")
    inspection = inspect_resource(
        FakeSession(FakeResponse([], content_length=4)), item, tmp_path
    )
    assert inspection.status == FileStatus.INVALID


def test_missing_content_length_falls_back_to_catalog_classification(tmp_path: Path):
    item = resource(size=3)
    (tmp_path / item.filename).write_bytes(b"wrong")
    inspection = inspect_resource(FakeSession(FakeResponse([])), item, tmp_path)
    assert inspection.status == FileStatus.INVALID
    assert inspection.size_source == "catalog"
    assert inspection.remote_error is not None


def test_head_failure_falls_back_to_catalog_classification(tmp_path: Path):
    item = resource(size=3)
    (tmp_path / item.filename).write_bytes(b"wrong")
    inspection = inspect_resource(
        FakeSession(FakeResponse([], requests.ConnectionError("offline"))), item, tmp_path
    )
    assert inspection.status == FileStatus.INVALID
    assert "offline" in (inspection.remote_error or "")


def test_inspection_pool_is_bounded_to_eight(monkeypatch, tmp_path: Path):
    item = resource(size=3)
    (tmp_path / item.filename).write_bytes(b"good")
    observed = {}

    from weather_analysis import meteo_france

    real_executor = meteo_france.ThreadPoolExecutor

    def executor(*args, **kwargs):
        observed["max_workers"] = kwargs.get("max_workers", args[0] if args else None)
        return real_executor(*args, **kwargs)

    monkeypatch.setattr(meteo_france, "ThreadPoolExecutor", executor)
    inspect_resources(
        FakeSession(FakeResponse([], content_length=4)), [item], tmp_path
    )
    assert observed["max_workers"] == 8
    inspect_resource,
    inspect_resources,
