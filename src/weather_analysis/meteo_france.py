from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

import requests


DATASETS = {
    "main": {
        "identifier": "6569b4473bedf2e7abad3b72",
        "output": Path("data/meteo_france/hourly_raw"),
    },
    "complementary": {
        "identifier": "donnees-climatologiques-de-base-horaires-stations-complementaires",
        "output": Path("data/meteo_france/hourly_complementary"),
    },
}
API_ROOT = "https://www.data.gouv.fr/api/1/datasets"
CHUNK_SIZE = 1024 * 1024
TIMEOUT_SECONDS = 120


class FileStatus(StrEnum):
    COMPLETE = "complete"
    MISSING = "missing"
    PARTIAL = "partial"
    INVALID = "invalid"
    UNKNOWN_SIZE = "unknown-size"


@dataclass(frozen=True)
class Resource:
    resource_id: str | None
    title: str | None
    url: str
    format: str | None
    last_modified: str | None
    expected_size: int | None
    filename: str
    department: str | None
    period: tuple[int, int] | None


@dataclass(frozen=True)
class Inspection:
    status: FileStatus
    server_size: int | None = None
    size_source: str = "unavailable"
    catalog_mismatch: bool = False
    remote_error: str | None = None
    redundant_partial: bool = False


@dataclass(frozen=True)
class DownloadResult:
    actual_size: int
    server_size: int | None
    size_source: str
    catalog_mismatch: bool


def dataset_api_url(category: str) -> str:
    try:
        identifier = DATASETS[category]["identifier"]
    except KeyError as error:
        raise ValueError(f"Unknown station category: {category!r}") from error
    return f"{API_ROOT}/{identifier}/"


def default_output_dir(category: str) -> Path:
    try:
        return DATASETS[category]["output"]
    except KeyError as error:
        raise ValueError(f"Unknown station category: {category!r}") from error


def filename_for(resource: dict) -> str:
    filename = Path(unquote(urlparse(resource.get("url") or "").path)).name
    return filename or f"{resource['id']}.csv.gz"


def resource_text(resource: dict) -> str:
    return f"{resource.get('title') or ''} {filename_for(resource)}"


def extract_department(resource: dict) -> str | None:
    text = resource_text(resource)
    patterns = (
        r"departement[_\s-]*(2A|2B|\d{2,3})",
        r"d[ée]partement[_\s-]*(2A|2B|\d{2,3})",
        r"horaires?[_\s-]+(2A|2B|\d{2,3})(?=[_\s-])",
        r"(?:^|\s)H[_\s-]+(2A|2B|\d{2,3})(?=[_\s-])",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def extract_period(resource: dict) -> tuple[int, int] | None:
    text = resource_text(resource)
    patterns = (
        r"p[ée]riode[_\s-]*(\d{4})[_\s-]+(\d{4})",
        r"d[ée]cennie[_\s-]*(\d{4})[_\s-]+(\d{4})",
        r"(?:^|[_\s-])(\d{4})[_\s-]+(\d{4})(?=\D|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def period_overlaps(
    period: tuple[int, int] | None,
    start_year: int | None,
    end_year: int | None,
) -> bool:
    if start_year is None and end_year is None:
        return True
    if period is None:
        return False
    start, end = period
    return not (
        (start_year is not None and end < start_year)
        or (end_year is not None and start > end_year)
    )


def is_hourly_csv_resource(resource: dict) -> bool:
    if (resource.get("type") or "").lower() == "documentation":
        return False
    url = (resource.get("url") or "").lower()
    file_format = (resource.get("format") or "").lower()
    return url.endswith(".csv.gz") or file_format in {"csv.gz", "gz"}


def parse_resource(raw: dict) -> Resource:
    return Resource(
        resource_id=raw.get("id"),
        title=raw.get("title"),
        url=raw["url"],
        format=raw.get("format"),
        last_modified=raw.get("last_modified"),
        expected_size=raw.get("filesize"),
        filename=filename_for(raw),
        department=extract_department(raw),
        period=extract_period(raw),
    )


def fetch_resources(session: requests.Session, category: str) -> list[Resource]:
    response = session.get(dataset_api_url(category), timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    raw_resources = response.json().get("resources", [])
    return [parse_resource(item) for item in raw_resources if is_hourly_csv_resource(item)]


def select_resources(
    resources: Iterable[Resource],
    departments: set[str] | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
) -> list[Resource]:
    normalized = {code.upper() for code in departments} if departments else None
    selected = [
        resource
        for resource in resources
        if (normalized is None or resource.department in normalized)
        and period_overlaps(resource.period, start_year, end_year)
    ]
    return sorted(
        selected,
        key=lambda item: (item.department or "", item.period or (0, 0), item.title or ""),
    )


def partial_path(destination: Path) -> Path:
    return destination.with_suffix(destination.suffix + ".part")


def classify_file(resource: Resource, output_dir: Path) -> FileStatus:
    destination = output_dir / resource.filename
    partial = partial_path(destination)
    if destination.exists():
        if resource.expected_size is None:
            return FileStatus.UNKNOWN_SIZE
        if destination.stat().st_size == resource.expected_size:
            return FileStatus.COMPLETE
        return FileStatus.INVALID
    if partial.exists():
        return FileStatus.PARTIAL
    return FileStatus.MISSING


def _content_length(headers: object) -> int | None:
    try:
        value = headers.get("Content-Length")  # type: ignore[union-attr]
        return int(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def inspect_resource(
    session: requests.Session, resource: Resource, output_dir: Path
) -> Inspection:
    destination = output_dir / resource.filename
    temporary = partial_path(destination)
    if not destination.exists():
        status = FileStatus.PARTIAL if temporary.exists() else FileStatus.MISSING
        return Inspection(status=status)

    local_size = destination.stat().st_size
    catalog_size = resource.expected_size
    if catalog_size is None:
        return Inspection(
            status=FileStatus.UNKNOWN_SIZE,
            redundant_partial=temporary.exists() and temporary.stat().st_size == local_size,
        )
    if local_size == catalog_size:
        return Inspection(
            status=FileStatus.COMPLETE,
            size_source="catalog",
            redundant_partial=temporary.exists() and temporary.stat().st_size == local_size,
        )

    try:
        response = session.head(resource.url, timeout=TIMEOUT_SECONDS, allow_redirects=True)
        response.raise_for_status()
        server_size = _content_length(response.headers)
    except requests.RequestException as error:
        return Inspection(
            status=FileStatus.INVALID,
            size_source="catalog",
            catalog_mismatch=True,
            remote_error=str(error),
        )

    if server_size is None:
        return Inspection(
            status=FileStatus.INVALID,
            size_source="catalog",
            catalog_mismatch=True,
            remote_error="HEAD response did not include a usable Content-Length",
        )
    status = FileStatus.COMPLETE if local_size == server_size else FileStatus.INVALID
    return Inspection(
        status=status,
        server_size=server_size,
        size_source="server",
        catalog_mismatch=server_size != catalog_size,
        redundant_partial=(
            status == FileStatus.COMPLETE
            and temporary.exists()
            and temporary.stat().st_size == local_size
        ),
    )


def inspect_resources(
    session: requests.Session,
    resources: Iterable[Resource],
    output_dir: Path,
    max_workers: int = 8,
) -> dict[str, Inspection]:
    items = list(resources)
    results: dict[str, Inspection] = {}
    local_results: list[Resource] = []
    remote_checks: list[Resource] = []
    for resource in items:
        destination = output_dir / resource.filename
        if (
            destination.exists()
            and resource.expected_size is not None
            and destination.stat().st_size != resource.expected_size
        ):
            remote_checks.append(resource)
        else:
            local_results.append(resource)
    for resource in local_results:
        results[resource.filename] = inspect_resource(session, resource, output_dir)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(inspect_resource, session, resource, output_dir): resource
            for resource in remote_checks
        }
        for future in as_completed(futures):
            resource = futures[future]
            results[resource.filename] = future.result()
    return results


def download_resource(
    session: requests.Session,
    resource: Resource,
    output_dir: Path,
) -> DownloadResult:
    destination = output_dir / resource.filename
    temporary = partial_path(destination)
    with session.get(resource.url, stream=True, timeout=TIMEOUT_SECONDS) as response:
        response.raise_for_status()
        server_size = _content_length(response.headers)
        with temporary.open("wb") as output:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    output.write(chunk)
    actual_size = temporary.stat().st_size
    validation_size = server_size if server_size is not None else resource.expected_size
    size_source = "server" if server_size is not None else (
        "catalog" if resource.expected_size is not None else "unavailable"
    )
    if validation_size is not None and actual_size != validation_size:
        raise ValueError(
            f"size mismatch for {resource.filename}: "
            f"expected {validation_size} from {size_source}, received {actual_size}"
        )
    temporary.replace(destination)
    return DownloadResult(
        actual_size=actual_size,
        server_size=server_size,
        size_source=size_source,
        catalog_mismatch=(
            server_size is not None
            and resource.expected_size is not None
            and server_size != resource.expected_size
        ),
    )


def write_manifest(
    resources: Iterable[Resource],
    output_dir: Path,
    inspections: dict[str, Inspection],
    downloads: dict[str, DownloadResult] | None = None,
) -> Path:
    manifest_path = output_dir / "selected_resources.json"
    manifest = [
        {
            "resource_id": resource.resource_id,
            "title": resource.title,
            "department": resource.department,
            "period": resource.period,
            "url": resource.url,
            "format": resource.format,
            "last_modified": resource.last_modified,
            "catalog_size": resource.expected_size,
            "server_size": (
                downloads[resource.filename].server_size
                if downloads and resource.filename in downloads
                else inspections[resource.filename].server_size
            ),
            "size_source": (
                downloads[resource.filename].size_source
                if downloads and resource.filename in downloads
                else inspections[resource.filename].size_source
            ),
            "catalog_server_mismatch": (
                downloads[resource.filename].catalog_mismatch
                if downloads and resource.filename in downloads
                else inspections[resource.filename].catalog_mismatch
            ),
            "local_path": str(output_dir / resource.filename),
            "pre_download_status": inspections[resource.filename].status,
            "actual_downloaded_size": (
                downloads[resource.filename].actual_size
                if downloads and resource.filename in downloads
                else None
            ),
        }
        for resource in resources
    ]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest_path


def human_size(number: int) -> str:
    size = float(number)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
