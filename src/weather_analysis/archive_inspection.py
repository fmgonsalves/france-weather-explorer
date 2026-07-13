from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from charset_normalizer import from_bytes
from tqdm import tqdm


DOCUMENTATION_URL = (
    "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/HOR/"
    "H_descriptif_champs.csv"
)
FILENAME_PATTERN = re.compile(
    r"^H_(?P<department>2A|2B|\d{2,3})_"
    r"(?:(?:latest|previous)-)?(?P<start>\d{4})-(?P<end>\d{4})\.csv\.gz$"
)
INVENTORY_COLUMNS = [
    "filename", "department", "period_start", "period_end", "compressed_size",
    "modified_ns", "encoding", "delimiter", "schema_id", "column_count",
    "row_count", "station_count", "earliest_timestamp", "latest_timestamp",
    "malformed_rows", "invalid_timestamps", "inspection_error",
]


@dataclass(frozen=True)
class Archive:
    path: Path
    department: str
    period_start: int
    period_end: int


@dataclass
class ArchiveResult:
    filename: str
    department: str
    period_start: int
    period_end: int
    compressed_size: int
    modified_ns: int
    encoding: str | None = None
    delimiter: str | None = None
    schema_id: str | None = None
    column_count: int | None = None
    row_count: int | None = None
    station_count: int | None = None
    earliest_timestamp: str | None = None
    latest_timestamp: str | None = None
    malformed_rows: int = 0
    invalid_timestamps: int = 0
    inspection_error: str | None = None
    columns: tuple[str, ...] = ()

    def report_row(self) -> dict:
        row = asdict(self)
        row.pop("columns")
        return row


def discover_archives(
    root: Path,
    departments: set[str] | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
) -> list[Archive]:
    normalized = {value.upper() for value in departments} if departments else None
    archives = []
    for path in root.glob("*.csv.gz"):
        match = FILENAME_PATTERN.fullmatch(path.name)
        if not match:
            continue
        department = match.group("department").upper()
        period_start, period_end = int(match.group("start")), int(match.group("end"))
        if normalized is not None and department not in normalized:
            continue
        if start_year is not None and period_end < start_year:
            continue
        if end_year is not None and period_start > end_year:
            continue
        archives.append(Archive(path, department, period_start, period_end))
    return sorted(archives, key=lambda item: (item.department, item.period_start, item.path.name))


def detect_encoding_and_delimiter(path: Path) -> tuple[str, str]:
    with gzip.open(path, "rb") as source:
        sample = source.read(65536)
    encoding = None
    for candidate in ("utf-8-sig", "cp1252"):
        try:
            sample.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            pass
    if encoding is None:
        match = from_bytes(sample).best()
        if match is None or not match.encoding:
            raise UnicodeError("could not identify archive encoding")
        encoding = match.encoding
    text = sample.decode(encoding)
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if not first_line.strip():
        raise ValueError("archive has no header")
    try:
        delimiter = csv.Sniffer().sniff(first_line, delimiters=";,\t|").delimiter
    except csv.Error as error:
        raise ValueError("could not detect CSV delimiter") from error
    return encoding, delimiter


def schema_id(columns: Iterable[str]) -> str:
    payload = "\x1f".join(columns).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _base_result(archive: Archive) -> ArchiveResult:
    stat = archive.path.stat()
    return ArchiveResult(
        filename=archive.path.name,
        department=archive.department,
        period_start=archive.period_start,
        period_end=archive.period_end,
        compressed_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
    )


def inspect_archive(archive: Archive, inventory: bool) -> ArchiveResult:
    result = _base_result(archive)
    try:
        encoding, delimiter = detect_encoding_and_delimiter(archive.path)
        result.encoding, result.delimiter = encoding, delimiter
        with gzip.open(archive.path, "rb") as binary:
            text = io.TextIOWrapper(binary, encoding=encoding, newline="")
            reader = csv.reader(text, delimiter=delimiter)
            columns = tuple(next(reader))
            if not columns or any(not column for column in columns):
                raise ValueError("header is empty or contains an unnamed column")
            result.columns = columns
            result.column_count = len(columns)
            result.schema_id = schema_id(columns)
            if not inventory:
                return result
            station_index = columns.index("NUM_POSTE") if "NUM_POSTE" in columns else None
            time_index = columns.index("AAAAMMJJHH") if "AAAAMMJJHH" in columns else None
            stations: set[str] = set()
            earliest_raw = latest_raw = None
            parsed_timestamps: dict[str, bool] = {}
            rows = 0
            for row in reader:
                rows += 1
                if len(row) != len(columns):
                    result.malformed_rows += 1
                    continue
                if station_index is not None and row[station_index]:
                    stations.add(row[station_index])
                if time_index is not None:
                    raw_time = row[time_index]
                    valid = parsed_timestamps.get(raw_time)
                    if valid is None:
                        try:
                            datetime.strptime(raw_time, "%Y%m%d%H")
                        except ValueError:
                            valid = False
                        else:
                            valid = True
                        parsed_timestamps[raw_time] = valid
                    if not valid:
                        result.invalid_timestamps += 1
                    else:
                        earliest_raw = (
                            raw_time if earliest_raw is None or raw_time < earliest_raw else earliest_raw
                        )
                        latest_raw = (
                            raw_time if latest_raw is None or raw_time > latest_raw else latest_raw
                        )
            result.row_count = rows
            result.station_count = len(stations)
            result.earliest_timestamp = (
                datetime.strptime(earliest_raw, "%Y%m%d%H").isoformat()
                if earliest_raw else None
            )
            result.latest_timestamp = (
                datetime.strptime(latest_raw, "%Y%m%d%H").isoformat()
                if latest_raw else None
            )
    except Exception as error:
        result.inspection_error = f"{type(error).__name__}: {error}"
    return result


def inspect_many(archives: list[Archive], inventory: bool, workers: int) -> list[ArchiveResult]:
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(inspect_archive, item, inventory): item for item in archives}
        for future in tqdm(
            as_completed(futures), total=len(futures), unit="archive", desc="Inspecting"
        ):
            results.append(future.result())
    return sorted(results, key=lambda item: (item.department, item.period_start, item.filename))


def load_cached_results(
    archives: list[Archive], report_dir: Path, inventory: bool
) -> tuple[list[ArchiveResult], list[Archive]]:
    inventory_path = report_dir / "archive_inventory.parquet"
    schemas_path = report_dir / "schemas.json"
    if not inventory_path.exists() or not schemas_path.exists():
        return [], archives
    try:
        rows = pq.read_table(inventory_path).to_pylist()
        schema_data = json.loads(schemas_path.read_text(encoding="utf-8"))["schemas"]
    except (OSError, ValueError, KeyError, pa.ArrowException):
        return [], archives
    cached_by_name = {row["filename"]: row for row in rows}
    cached, pending = [], []
    for archive in archives:
        row = cached_by_name.get(archive.path.name)
        stat = archive.path.stat()
        usable = (
            row is not None
            and row.get("compressed_size") == stat.st_size
            and row.get("modified_ns") == stat.st_mtime_ns
            and row.get("schema_id") in schema_data
            and (not inventory or row.get("row_count") is not None)
        )
        if not usable:
            pending.append(archive)
            continue
        result = ArchiveResult(**{key: row.get(key) for key in INVENTORY_COLUMNS})
        result.columns = tuple(schema_data[result.schema_id]["columns"])
        cached.append(result)
    return cached, pending


def _table(rows: list[dict], metadata: dict[str, str]) -> pa.Table:
    table = pa.Table.from_pylist(rows)
    encoded = {key.encode(): value.encode() for key, value in metadata.items()}
    return table.replace_schema_metadata(encoded)


def _report_metadata(root: Path, mode: str, selected: int, filters: dict) -> dict[str, str]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tool": "weather-analysis",
        "mode": mode,
        "source_archive_path": str(root.resolve()),
        "selected_archives": str(selected),
        "filters": json.dumps(filters, sort_keys=True),
    }


def write_schema_reports(
    results: list[ArchiveResult], report_dir: Path, metadata: dict[str, str]
) -> dict[str, dict]:
    report_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        _table([item.report_row() for item in results], metadata),
        report_dir / "archive_inventory.parquet",
    )
    groups: dict[str, list[ArchiveResult]] = {}
    for result in results:
        if result.schema_id:
            groups.setdefault(result.schema_id, []).append(result)
    schemas = {
        identifier: {
            "columns": list(items[0].columns),
            "file_count": len(items),
            "representative_file": min(items, key=lambda item: item.compressed_size).filename,
            "earliest_period": min(item.period_start for item in items),
            "latest_period": max(item.period_end for item in items),
        }
        for identifier, items in sorted(groups.items())
    }
    (report_dir / "schemas.json").write_text(
        json.dumps({"metadata": metadata, "schemas": schemas}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    coverage = Counter(column for item in results for column in set(item.columns))
    successful = sum(item.schema_id is not None for item in results)
    coverage_rows = [
        {
            "column": column,
            "archive_count": count,
            "archive_percentage": (100.0 * count / successful) if successful else 0.0,
        }
        for column, count in sorted(coverage.items())
    ]
    pq.write_table(_table(coverage_rows, metadata), report_dir / "column_coverage.parquet")
    return schemas


def profile_archive(archive: Archive, columns: tuple[str, ...]) -> tuple[list[dict], int]:
    encoding, delimiter = detect_encoding_and_delimiter(archive.path)
    stats = {
        column: {"populated": 0, "numeric": 0, "min": None, "max": None, "examples": [], "distinct": set(), "capped": False}
        for column in columns
    }
    keys: set[tuple[str, str]] = set()
    duplicates = 0
    station_index = columns.index("NUM_POSTE") if "NUM_POSTE" in columns else None
    time_index = columns.index("AAAAMMJJHH") if "AAAAMMJJHH" in columns else None
    with gzip.open(archive.path, "rb") as binary:
        reader = csv.reader(io.TextIOWrapper(binary, encoding=encoding, newline=""), delimiter=delimiter)
        next(reader)
        for row in reader:
            if len(row) != len(columns):
                continue
            if station_index is not None and time_index is not None:
                key = (row[station_index], row[time_index])
                if key in keys:
                    duplicates += 1
                else:
                    keys.add(key)
            for column, value in zip(columns, row):
                if value == "":
                    continue
                item = stats[column]
                item["populated"] += 1
                if len(item["examples"]) < 3 and value not in item["examples"]:
                    item["examples"].append(value)
                if not item["capped"]:
                    item["distinct"].add(value)
                    if len(item["distinct"]) > 10_000:
                        item["distinct"].clear()
                        item["capped"] = True
                try:
                    number = float(value)
                except ValueError:
                    pass
                else:
                    item["numeric"] += 1
                    item["min"] = number if item["min"] is None else min(item["min"], number)
                    item["max"] = number if item["max"] is None else max(item["max"], number)
    rows = []
    for column, item in stats.items():
        inferred = "numeric" if item["populated"] and item["numeric"] == item["populated"] else "string"
        rows.append({
            "filename": archive.path.name,
            "column": column,
            "inferred_type": inferred,
            "populated_count": item["populated"],
            "distinct_count": None if item["capped"] else len(item["distinct"]),
            "examples": json.dumps(item["examples"], ensure_ascii=False),
            "numeric_min": item["min"],
            "numeric_max": item["max"],
            "duplicate_station_timestamp_count": duplicates,
        })
    return rows, duplicates


def fetch_column_documentation(session: requests.Session) -> dict[str, str]:
    response = session.get(DOCUMENTATION_URL, timeout=120)
    response.raise_for_status()
    descriptions = {}
    for line in response.content.decode("utf-8-sig").splitlines():
        if ":" not in line:
            continue
        name, description = line.split(":", 1)
        name = name.strip()
        if name and " " not in name:
            descriptions[name] = description.strip()
    return descriptions


def write_column_dictionary(
    columns: list[str], coverage: dict[str, int], archive_count: int, report_dir: Path,
    metadata: dict[str, str], session: requests.Session,
) -> None:
    try:
        documented = fetch_column_documentation(session)
        documentation_error = None
    except requests.RequestException as error:
        documented, documentation_error = {}, str(error)
    rows = []
    for column in columns:
        quality_for = column[1:] if column.startswith("Q") and column[1:] in columns else None
        rows.append({
            "column": column,
            "description": documented.get(column),
            "documented": column in documented,
            "quality_for": quality_for,
            "archive_count": coverage.get(column, 0),
            "archive_percentage": 100.0 * coverage.get(column, 0) / archive_count if archive_count else 0.0,
        })
    dictionary_metadata = dict(metadata)
    dictionary_metadata["documentation_url"] = DOCUMENTATION_URL
    dictionary_metadata["documentation_error"] = documentation_error or ""
    pq.write_table(_table(rows, dictionary_metadata), report_dir / "column_dictionary.parquet")
    lines = ["# Météo-France Hourly Column Dictionary", "", f"Official source: {DOCUMENTATION_URL}", ""]
    if documentation_error:
        lines.extend([f"> Documentation could not be retrieved: {documentation_error}", ""])
    lines.extend(["| Column | Description | Quality for | Coverage |", "|---|---|---|---:|"])
    for row in rows:
        description = (row["description"] or "Undocumented or ambiguous").replace("|", "\\|")
        lines.append(f"| {row['column']} | {description} | {row['quality_for'] or ''} | {row['archive_percentage']:.1f}% |")
    (report_dir / "column_dictionary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_inspection(
    root: Path, report_dir: Path, mode: str, departments: set[str] | None,
    start_year: int | None, end_year: int | None, workers: int,
    session: requests.Session,
) -> tuple[int, dict[str, int]]:
    archives = discover_archives(root, departments, start_year, end_year)
    filters = {"departments": sorted(departments) if departments else None, "start_year": start_year, "end_year": end_year}
    metadata = _report_metadata(root, mode, len(archives), filters)
    needs_inventory = mode in {"inventory", "profile"}
    cached, pending = load_cached_results(archives, report_dir, needs_inventory)
    results = cached + inspect_many(pending, inventory=needs_inventory, workers=workers)
    results.sort(key=lambda item: (item.department, item.period_start, item.filename))
    schemas = write_schema_reports(results, report_dir, metadata)
    coverage = Counter(column for item in results for column in set(item.columns))
    columns = sorted(coverage)
    write_column_dictionary(columns, dict(coverage), len(results), report_dir, metadata, session)
    if mode == "profile":
        by_name = {archive.path.name: archive for archive in archives}
        result_by_name = {result.filename: result for result in results}
        profile_rows = []
        for schema in tqdm(schemas.values(), unit="schema", desc="Profiling"):
            filename = schema["representative_file"]
            rows, _ = profile_archive(by_name[filename], result_by_name[filename].columns)
            profile_rows.extend(rows)
        pq.write_table(_table(profile_rows, metadata), report_dir / "profile_summary.parquet")
    errors = sum(result.inspection_error is not None for result in results)
    summary = {"selected": len(archives), "schemas": len(schemas), "errors": errors}
    return (1 if errors else 0), summary
