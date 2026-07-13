from __future__ import annotations

import csv
import gzip
import io
import json
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

from .archive_inspection import (
    Archive,
    ArchiveResult,
    detect_encoding_and_delimiter,
    discover_archives,
    schema_id,
)


KEY_BATCH_SIZE = 250_000
CORE_COLUMNS = {
    "T", "TD", "TN", "TX", "U", "UN", "UX", "RR1", "FF", "DD", "PSTAT", "PMER"
}
STATION_METADATA = ("NOM_USUEL", "LAT", "LON", "ALTI")


@dataclass
class ValueStats:
    populated: int = 0
    numeric: int = 0
    minimum: float | None = None
    maximum: float | None = None
    examples: list[str] = field(default_factory=list)

    def add(self, value: str) -> None:
        if value == "":
            return
        self.populated += 1
        if len(self.examples) < 3 and value not in self.examples:
            self.examples.append(value)
        try:
            number = float(value)
        except ValueError:
            return
        self.numeric += 1
        self.minimum = number if self.minimum is None else min(self.minimum, number)
        self.maximum = number if self.maximum is None else max(self.maximum, number)


@dataclass
class StationStats:
    observations: int = 0
    temperature_observations: int = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    source_files: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    latitudes: set[str] = field(default_factory=set)
    longitudes: set[str] = field(default_factory=set)
    altitudes: set[str] = field(default_factory=set)


@dataclass
class ExplorationResult:
    archive: ArchiveResult
    column_stats: dict[str, ValueStats]
    quality_counts: dict[str, Counter[str]]
    stations: dict[str, StationStats]
    station_year_counts: dict[tuple[str, int], tuple[int, int]]
    key_files: list[str]


def _write_key_batch(rows: list[dict[str, str]], directory: Path, index: int) -> str:
    path = directory / f"keys-{index:05d}.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
    return str(path)


def _valid_timestamp(raw: str, cache: dict[str, bool]) -> bool:
    valid = cache.get(raw)
    if valid is None:
        try:
            datetime.strptime(raw, "%Y%m%d%H")
        except ValueError:
            valid = False
        else:
            valid = True
        cache[raw] = valid
    return valid


def explore_archive(archive: Archive, temporary_root: Path) -> ExplorationResult:
    work_dir = temporary_root / archive.path.name.removesuffix(".csv.gz")
    cache_path = work_dir / "summary.json"
    if cache_path.exists():
        return _load_cached_result(cache_path, archive)
    work_dir.mkdir(parents=True, exist_ok=True)
    stat = archive.path.stat()
    result = ArchiveResult(
        filename=archive.path.name,
        department=archive.department,
        period_start=archive.period_start,
        period_end=archive.period_end,
        compressed_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
    )
    column_stats: dict[str, ValueStats] = {}
    quality_counts: dict[str, Counter[str]] = {}
    stations: dict[str, StationStats] = {}
    station_year: dict[tuple[str, int], list[int]] = {}
    key_rows: list[dict[str, str]] = []
    key_files: list[str] = []
    batch_index = 0
    try:
        encoding, delimiter = detect_encoding_and_delimiter(archive.path)
        result.encoding, result.delimiter = encoding, delimiter
        with gzip.open(archive.path, "rb") as binary:
            reader = csv.reader(
                io.TextIOWrapper(binary, encoding=encoding, newline=""), delimiter=delimiter
            )
            columns = tuple(next(reader))
            result.columns = columns
            result.column_count = len(columns)
            result.schema_id = schema_id(columns)
            column_stats = {column: ValueStats() for column in columns}
            quality_counts = {
                column: Counter() for column in columns
                if column.startswith("Q") or column.startswith("STATUS_")
            }
            indexes = {column: columns.index(column) for column in columns}
            timestamp_cache: dict[str, bool] = {}
            earliest = latest = None
            for row in reader:
                result.row_count = (result.row_count or 0) + 1
                if len(row) != len(columns):
                    result.malformed_rows += 1
                    continue
                for column, value in zip(columns, row):
                    column_stats[column].add(value)
                    if value and column in quality_counts:
                        quality_counts[column][value] += 1
                station_id = row[indexes["NUM_POSTE"]]
                timestamp = row[indexes["AAAAMMJJHH"]]
                if not _valid_timestamp(timestamp, timestamp_cache):
                    result.invalid_timestamps += 1
                    continue
                earliest = timestamp if earliest is None or timestamp < earliest else earliest
                latest = timestamp if latest is None or timestamp > latest else latest
                station = stations.setdefault(station_id, StationStats())
                station.observations += 1
                station.source_files.add(archive.path.name)
                station.first_timestamp = (
                    timestamp if station.first_timestamp is None or timestamp < station.first_timestamp
                    else station.first_timestamp
                )
                station.last_timestamp = (
                    timestamp if station.last_timestamp is None or timestamp > station.last_timestamp
                    else station.last_timestamp
                )
                for field_name, attribute in zip(
                    STATION_METADATA, ("names", "latitudes", "longitudes", "altitudes")
                ):
                    value = row[indexes[field_name]]
                    if value:
                        getattr(station, attribute).add(value)
                has_temperature = bool(row[indexes["T"]])
                station.temperature_observations += int(has_temperature)
                year_key = (station_id, int(timestamp[:4]))
                counts = station_year.setdefault(year_key, [0, 0])
                counts[0] += 1
                counts[1] += int(has_temperature)
                key_rows.append({
                    "station_id": station_id,
                    "timestamp": timestamp,
                    "source_file": archive.path.name,
                })
                if len(key_rows) >= KEY_BATCH_SIZE:
                    key_files.append(_write_key_batch(key_rows, work_dir, batch_index))
                    batch_index += 1
                    key_rows.clear()
            if key_rows:
                key_files.append(_write_key_batch(key_rows, work_dir, batch_index))
            result.station_count = len(stations)
            result.earliest_timestamp = (
                datetime.strptime(earliest, "%Y%m%d%H").isoformat() if earliest else None
            )
            result.latest_timestamp = (
                datetime.strptime(latest, "%Y%m%d%H").isoformat() if latest else None
            )
    except Exception as error:
        result.inspection_error = f"{type(error).__name__}: {error}"
    exploration = ExplorationResult(
        result, column_stats, quality_counts, stations,
        {key: (value[0], value[1]) for key, value in station_year.items()}, key_files,
    )
    _write_cached_result(cache_path, exploration)
    return exploration


def _station_to_dict(value: StationStats) -> dict:
    return {
        "observations": value.observations,
        "temperature_observations": value.temperature_observations,
        "first_timestamp": value.first_timestamp,
        "last_timestamp": value.last_timestamp,
        "source_files": sorted(value.source_files),
        "names": sorted(value.names),
        "latitudes": sorted(value.latitudes),
        "longitudes": sorted(value.longitudes),
        "altitudes": sorted(value.altitudes),
    }


def _write_cached_result(path: Path, result: ExplorationResult) -> None:
    payload = {
        "fingerprint": [result.archive.compressed_size, result.archive.modified_ns],
        "archive": result.archive.report_row(),
        "columns": list(result.archive.columns),
        "column_stats": {name: vars(value) for name, value in result.column_stats.items()},
        "quality_counts": {name: dict(value) for name, value in result.quality_counts.items()},
        "stations": {name: _station_to_dict(value) for name, value in result.stations.items()},
        "station_year_counts": {
            f"{station}\x1f{year}": list(counts)
            for (station, year), counts in result.station_year_counts.items()
        },
        "key_files": result.key_files,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_cached_result(path: Path, archive: Archive) -> ExplorationResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    stat = archive.path.stat()
    if payload["fingerprint"] != [stat.st_size, stat.st_mtime_ns] or not all(
        Path(item).exists() for item in payload["key_files"]
    ):
        shutil.rmtree(path.parent)
        return explore_archive(archive, path.parent.parent)
    archive_result = ArchiveResult(**payload["archive"])
    archive_result.columns = tuple(payload["columns"])
    column_stats = {name: ValueStats(**value) for name, value in payload["column_stats"].items()}
    quality = {name: Counter(value) for name, value in payload["quality_counts"].items()}
    stations = {}
    for name, value in payload["stations"].items():
        stations[name] = StationStats(
            observations=value["observations"],
            temperature_observations=value["temperature_observations"],
            first_timestamp=value["first_timestamp"], last_timestamp=value["last_timestamp"],
            source_files=set(value["source_files"]), names=set(value["names"]),
            latitudes=set(value["latitudes"]), longitudes=set(value["longitudes"]),
            altitudes=set(value["altitudes"]),
        )
    station_year = {}
    for key, counts in payload["station_year_counts"].items():
        station, year = key.split("\x1f")
        station_year[(station, int(year))] = (counts[0], counts[1])
    return ExplorationResult(
        archive_result, column_stats, quality, stations, station_year, payload["key_files"]
    )


def _metadata(department: str, archives: list[Archive]) -> dict[bytes, bytes]:
    values = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "department": department,
        "archive_count": str(len(archives)),
        "period": f"{min(item.period_start for item in archives)}-{max(item.period_end for item in archives)}",
        "timestamp_convention": "UTC (metropolitan France)",
    }
    return {key.encode(): value.encode() for key, value in values.items()}


def _write_rows(rows: list[dict], path: Path, metadata: dict[bytes, bytes]) -> None:
    table = pa.Table.from_pylist(rows).replace_schema_metadata(metadata)
    pq.write_table(table, path, compression="zstd")


def _merge_station(target: StationStats, source: StationStats) -> None:
    target.observations += source.observations
    target.temperature_observations += source.temperature_observations
    target.first_timestamp = (
        source.first_timestamp if target.first_timestamp is None
        or (source.first_timestamp and source.first_timestamp < target.first_timestamp)
        else target.first_timestamp
    )
    target.last_timestamp = (
        source.last_timestamp if target.last_timestamp is None
        or (source.last_timestamp and source.last_timestamp > target.last_timestamp)
        else target.last_timestamp
    )
    for attribute in ("source_files", "names", "latitudes", "longitudes", "altitudes"):
        getattr(target, attribute).update(getattr(source, attribute))


def aggregate_exploration(
    results: list[ExplorationResult], report_dir: Path, department: str, archives: list[Archive]
) -> dict[str, int]:
    metadata = _metadata(department, archives)
    _write_rows([item.archive.report_row() for item in results], report_dir / "archive_inventory.parquet", metadata)
    stations: dict[str, StationStats] = {}
    station_year: dict[tuple[str, int], list[int]] = {}
    for result in results:
        for station_id, value in result.stations.items():
            _merge_station(stations.setdefault(station_id, StationStats()), value)
        for key, counts in result.station_year_counts.items():
            merged = station_year.setdefault(key, [0, 0])
            merged[0] += counts[0]
            merged[1] += counts[1]
    station_rows = [{
        "station_id": station_id,
        "names": json.dumps(sorted(value.names), ensure_ascii=False),
        "latitudes": json.dumps(sorted(value.latitudes)),
        "longitudes": json.dumps(sorted(value.longitudes)),
        "altitudes": json.dumps(sorted(value.altitudes)),
        "first_timestamp_utc": value.first_timestamp,
        "last_timestamp_utc": value.last_timestamp,
        "observation_count": value.observations,
        "temperature_count": value.temperature_observations,
        "source_files": json.dumps(sorted(value.source_files)),
    } for station_id, value in sorted(stations.items())]
    _write_rows(station_rows, report_dir / "station_inventory.parquet", metadata)
    conflicts = []
    for station_id, value in sorted(stations.items()):
        for field_name, values in (
            ("name", value.names), ("latitude", value.latitudes),
            ("longitude", value.longitudes), ("altitude", value.altitudes),
        ):
            if len(values) > 1:
                conflicts.append({
                    "station_id": station_id, "field": field_name,
                    "values": json.dumps(sorted(values), ensure_ascii=False),
                    "value_count": len(values),
                })
    _write_rows(conflicts, report_dir / "station_metadata_conflicts.parquet", metadata)
    column_rows = []
    for result in results:
        valid_rows = (result.archive.row_count or 0) - result.archive.malformed_rows
        for column, stats in result.column_stats.items():
            column_rows.append({
                "source_file": result.archive.filename,
                "period_start": result.archive.period_start,
                "period_end": result.archive.period_end,
                "column": column,
                "populated_count": stats.populated,
                "null_count": max(valid_rows - stats.populated, 0),
                "coverage_percentage": 100.0 * stats.populated / valid_rows if valid_rows else 0.0,
                "inferred_type": "numeric" if stats.populated and stats.numeric == stats.populated else "string",
                "numeric_min": stats.minimum, "numeric_max": stats.maximum,
                "examples": json.dumps(stats.examples, ensure_ascii=False),
                "is_core_dashboard_field": column in CORE_COLUMNS,
            })
    _write_rows(column_rows, report_dir / "column_profile.parquet", metadata)
    quality_rows = []
    for result in results:
        for column, counts in result.quality_counts.items():
            total = sum(counts.values())
            for value, count in sorted(counts.items()):
                quality_rows.append({
                    "source_file": result.archive.filename, "column": column,
                    "value": value, "count": count,
                    "percentage_of_populated": 100.0 * count / total if total else 0.0,
                })
    _write_rows(quality_rows, report_dir / "quality_code_profile.parquet", metadata)
    year_rows = []
    for (station_id, year), counts in sorted(station_year.items()):
        expected = 8784 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 8760
        year_rows.append({
            "station_id": station_id, "year": year, "observation_count": counts[0],
            "temperature_count": counts[1], "expected_hours": expected,
            "temperature_coverage_percentage": 100.0 * counts[1] / expected,
            "has_90_percent_temperature_coverage": counts[1] >= expected * 0.9,
        })
    _write_rows(year_rows, report_dir / "station_year_temperature_coverage.parquet", metadata)
    key_paths = [path for result in results for path in result.key_files]
    keys = pl.scan_parquet(key_paths)
    duplicate_frame = (
        keys.group_by(["station_id", "timestamp"])
        .agg([
            pl.len().alias("record_count"),
            pl.col("source_file").n_unique().alias("source_file_count"),
            pl.col("source_file").unique().sort().alias("source_files"),
        ])
        .filter(pl.col("record_count") > 1)
        .collect(engine="streaming")
    )
    duplicate_frame.write_parquet(report_dir / "duplicate_observations.parquet", compression="zstd")
    duplicate_frame.filter(pl.col("source_file_count") > 1).write_parquet(
        report_dir / "archive_overlaps.parquet", compression="zstd"
    )
    full_years = sum(row["has_90_percent_temperature_coverage"] for row in year_rows)
    errors = sum(result.archive.inspection_error is not None for result in results)
    summary = {
        "archives": len(results), "stations": len(stations), "errors": errors,
        "duplicates": duplicate_frame.height,
        "cross_archive_overlaps": duplicate_frame.filter(pl.col("source_file_count") > 1).height,
        "station_years_with_90_percent_temperature": full_years,
        "metadata_conflicts": len(conflicts),
    }
    _write_markdown_summary(report_dir, department, results, summary)
    return summary


def _write_markdown_summary(
    report_dir: Path, department: str, results: list[ExplorationResult], summary: dict[str, int]
) -> None:
    earliest = min(
        (item.archive.earliest_timestamp for item in results if item.archive.earliest_timestamp),
        default="unknown",
    )
    latest = max(
        (item.archive.latest_timestamp for item in results if item.archive.latest_timestamp),
        default="unknown",
    )
    lines = [
        f"# Department {department} Météo-France Exploration", "",
        "All source archives were streamed directly from gzip; no observations were modified.", "",
        "## Summary", "",
        f"- Archives: {summary['archives']}", f"- Stations: {summary['stations']}",
        f"- Observed UTC range: {earliest} to {latest}",
        f"- Inspection errors: {summary['errors']}",
        f"- Duplicate station/timestamp keys: {summary['duplicates']}",
        f"- Cross-archive overlaps: {summary['cross_archive_overlaps']}",
        f"- Station-years with at least 90% hourly temperature coverage: {summary['station_years_with_90_percent_temperature']}",
        f"- Station metadata conflicts: {summary['metadata_conflicts']}", "",
        "## Conversion recommendations", "",
        "- Preserve `NUM_POSTE` as text and retain source-file provenance.",
        "- Parse metropolitan `AAAAMMJJHH` as UTC, then derive Europe/Paris time separately.",
        "- Preserve quality fields and defer filtering until their codes are explicitly selected.",
        "- Resolve reported duplicate keys deterministically before departmental aggregation.",
        "- Build departmental averages from station-level aggregates to avoid weighting stations by row count.",
        "- Consider a stable-station subset for long-term comparisons because network composition changes over time.",
    ]
    (report_dir / "department_exploration.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_department_exploration(
    root: Path, report_dir: Path, department: str, start_year: int | None,
    end_year: int | None, workers: int,
) -> tuple[int, dict[str, int]]:
    archives = discover_archives(root, {department}, start_year, end_year)
    if not archives:
        return 1, {"archives": 0, "stations": 0, "errors": 1}
    report_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = [[item.path.name, item.path.stat().st_size, item.path.stat().st_mtime_ns] for item in archives]
    manifest_path = report_dir / "exploration_manifest.json"
    required = [
        "archive_inventory.parquet", "station_inventory.parquet", "column_profile.parquet",
        "quality_code_profile.parquet", "duplicate_observations.parquet",
        "station_metadata_conflicts.parquet", "archive_overlaps.parquet",
        "station_year_temperature_coverage.parquet", "department_exploration.md",
    ]
    if manifest_path.exists() and all((report_dir / name).exists() for name in required):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("fingerprint") == fingerprint:
            return (1 if manifest["summary"]["errors"] else 0), manifest["summary"]
    temporary_root = report_dir / ".explore_tmp"
    temporary_root.mkdir(exist_ok=True)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(explore_archive, archive, temporary_root): archive for archive in archives}
        for future in tqdm(as_completed(futures), total=len(futures), unit="archive", desc="Exploring"):
            results.append(future.result())
    results.sort(key=lambda item: item.archive.filename)
    summary = aggregate_exploration(results, report_dir, department, archives)
    manifest_path.write_text(
        json.dumps({"fingerprint": fingerprint, "summary": summary}, indent=2), encoding="utf-8"
    )
    shutil.rmtree(temporary_root)
    return (1 if summary["errors"] else 0), summary
