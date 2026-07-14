from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo

import duckdb
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from .archive_inspection import detect_encoding_and_delimiter


DATASET_VERSION = 1
TRANSFORMATION_VERSION = "1.0.1"
TARGET_FILE_SIZE = 256 * 1024 * 1024
BATCH_ROWS = 25_000
LOCAL_TIMEZONE = ZoneInfo("Europe/Paris")
SOURCE_PATTERN = re.compile(
    r"^H_(?P<department>2A|2B|\d{2,3})_"
    r"(?:(?:latest|previous)-)?(?P<start>\d{4})-(?P<end>\d{4})\.csv\.gz$"
)

IDENTITY_COLUMNS = ("NUM_POSTE", "NOM_USUEL", "LAT", "LON", "ALTI", "AAAAMMJJHH")
COMMON_COLUMNS = (
    "department", "network_category", "NUM_POSTE", "NOM_USUEL", "LAT", "LON", "ALTI",
    "AAAAMMJJHH", "observation_time_utc", "observation_time_local", "local_date",
    "local_hour", "utc_offset_minutes", "is_dst", "source_file", "source_row_number",
    "source_sha256", "processing_run_id",
)

CORE_BASE = (
    "RR1", "DRR1",
    "FF", "DD", "FXY", "DXY", "HXY", "FXI", "DXI", "HXI", "FXI3S", "DXI3S", "HFXI3S",
    "T", "TD", "TN", "HTN", "TX", "HTX", "DG",
    "U", "UN", "HUN", "UX", "HUX", "DHUMI40", "DHUMI80", "TSV",
    "PMER", "PSTAT", "PMERMIN", "GEOP",
    "N", "NBAS", "CL", "CM", "CH",
    "N1", "C1", "B1", "N2", "C2", "B2", "N3", "C3", "B3", "N4", "C4", "B4",
    "VV", "DVV200", "WW", "W1", "W2", "SOL", "SOLNG",
    "GLO", "GLO2", "DIR", "DIR2", "DIF", "DIF2", "UV", "UV2", "UV_INDICE",
    "INFRAR", "INFRAR2", "INS", "INS2",
)
CORE_STATUS = ("STATUS_FXI3S", "STATUS_DXI3S")
SURFACE_BASE = (
    "FF2", "DD2", "FXI2", "DXI2", "HXI2", "T10", "T20", "T50", "T100",
    "TNSOL", "TN50", "TCHAUSSEE", "DHUMEC",
)
MARINE_BASE = ("TMER", "VVMER", "ETATMER", "DIRHOULE", "HVAGUE", "PVAGUE")
SNOW_BASE = (
    "HNEIGEF", "NEIGETOT", "TSNEIGE", "TUBENEIGE", "HNEIGEFI3", "HNEIGEFI1",
    "ESNEIGE", "CHARGENEIGE",
)
TABLE_BASES = {
    "hourly_core": CORE_BASE,
    "hourly_surface": SURFACE_BASE,
    "hourly_marine": MARINE_BASE,
    "hourly_snow": SNOW_BASE,
}

INTEGER_METRICS = {
    "DD", "DXY", "HXY", "DXI", "HXI", "DXI3S", "HFXI3S", "HUN", "HUX",
    "HTN", "HTX", "DRR1", "DG", "DHUMI40", "DHUMI80", "DD2", "DXI2", "HXI2",
    "DHUMEC", "N", "NBAS", "CL", "CM", "CH", "N1", "C1", "B1", "N2", "C2",
    "B2", "N3", "C3", "B3", "N4", "C4", "B4", "DVV200", "WW", "W1", "W2",
    "SOL", "SOLNG", "VVMER", "ETATMER", "DIRHOULE", "ESNEIGE", "INS", "INS2",
}

ENGLISH_LABELS = {
    "RR1": "Hourly precipitation", "DRR1": "Precipitation duration",
    "FF": "Mean wind speed", "DD": "Mean wind direction", "FXY": "Maximum mean wind speed",
    "FXI": "Maximum instantaneous wind speed", "FXI3S": "Maximum 3-second wind speed",
    "T": "Air temperature", "TD": "Dew-point temperature", "TN": "Hourly minimum temperature",
    "TX": "Hourly maximum temperature", "DG": "Freezing duration", "U": "Relative humidity",
    "UN": "Hourly minimum relative humidity", "UX": "Hourly maximum relative humidity",
    "PMER": "Sea-level pressure", "PSTAT": "Station pressure", "N": "Total cloud cover",
    "VV": "Visibility", "WW": "Present weather", "GLO": "Global solar radiation",
    "INS": "Sunshine duration", "TMER": "Sea temperature", "ETATMER": "Sea state",
    "NEIGETOT": "Total snow depth", "TNSOL": "Near-ground minimum temperature",
}

DEPARTMENT_NAMES = {
    "01": "Ain", "02": "Aisne", "03": "Allier", "04": "Alpes-de-Haute-Provence",
    "05": "Hautes-Alpes", "06": "Alpes-Maritimes", "07": "Ardèche", "08": "Ardennes",
    "09": "Ariège", "10": "Aube", "11": "Aude", "12": "Aveyron",
    "13": "Bouches-du-Rhône", "14": "Calvados", "15": "Cantal", "16": "Charente",
    "17": "Charente-Maritime", "18": "Cher", "19": "Corrèze", "20": "Corse",
    "2A": "Corse-du-Sud", "2B": "Haute-Corse", "21": "Côte-d'Or",
    "22": "Côtes-d'Armor", "23": "Creuse", "24": "Dordogne", "25": "Doubs",
    "26": "Drôme", "27": "Eure", "28": "Eure-et-Loir", "29": "Finistère",
    "30": "Gard", "31": "Haute-Garonne", "32": "Gers", "33": "Gironde",
    "34": "Hérault", "35": "Ille-et-Vilaine", "36": "Indre", "37": "Indre-et-Loire",
    "38": "Isère", "39": "Jura", "40": "Landes", "41": "Loir-et-Cher",
    "42": "Loire", "43": "Haute-Loire", "44": "Loire-Atlantique", "45": "Loiret",
    "46": "Lot", "47": "Lot-et-Garonne", "48": "Lozère", "49": "Maine-et-Loire",
    "50": "Manche", "51": "Marne", "52": "Haute-Marne", "53": "Mayenne",
    "54": "Meurthe-et-Moselle", "55": "Meuse", "56": "Morbihan", "57": "Moselle",
    "58": "Nièvre", "59": "Nord", "60": "Oise", "61": "Orne",
    "62": "Pas-de-Calais", "63": "Puy-de-Dôme", "64": "Pyrénées-Atlantiques",
    "65": "Hautes-Pyrénées", "66": "Pyrénées-Orientales", "67": "Bas-Rhin",
    "68": "Haut-Rhin", "69": "Rhône", "70": "Haute-Saône", "71": "Saône-et-Loire",
    "72": "Sarthe", "73": "Savoie", "74": "Haute-Savoie", "75": "Paris",
    "76": "Seine-Maritime", "77": "Seine-et-Marne", "78": "Yvelines",
    "79": "Deux-Sèvres", "80": "Somme", "81": "Tarn", "82": "Tarn-et-Garonne",
    "83": "Var", "84": "Vaucluse", "85": "Vendée", "86": "Vienne",
    "87": "Haute-Vienne", "88": "Vosges", "89": "Yonne", "90": "Territoire de Belfort",
    "91": "Essonne", "92": "Hauts-de-Seine", "93": "Seine-Saint-Denis",
    "94": "Val-de-Marne", "95": "Val-d'Oise",
}


def _paired_columns(base_columns: Iterable[str], source_columns: set[str] | None = None) -> tuple[str, ...]:
    result: list[str] = []
    for name in base_columns:
        result.append(name)
        quality = f"Q{name}"
        if source_columns is None or quality in source_columns:
            result.append(quality)
    return tuple(result)


TABLE_METRICS = {
    "hourly_core": _paired_columns(CORE_BASE) + CORE_STATUS,
    "hourly_surface": _paired_columns(SURFACE_BASE),
    "hourly_marine": _paired_columns(MARINE_BASE),
    "hourly_snow": _paired_columns(SNOW_BASE),
}


def metric_type(name: str) -> pa.DataType:
    if name.startswith("Q") or name.startswith("STATUS_"):
        return pa.uint8()
    if name in INTEGER_METRICS:
        return pa.uint16()
    return pa.float32()


def table_schema(table_name: str) -> pa.Schema:
    common = [
        pa.field("department", pa.string()),
        pa.field("network_category", pa.string()),
        pa.field("NUM_POSTE", pa.string()),
        pa.field("NOM_USUEL", pa.string()),
        pa.field("LAT", pa.float64()),
        pa.field("LON", pa.float64()),
        pa.field("ALTI", pa.int32()),
        pa.field("AAAAMMJJHH", pa.string()),
        pa.field("observation_time_utc", pa.timestamp("us", tz="UTC")),
        pa.field("observation_time_local", pa.timestamp("us", tz="Europe/Paris")),
        pa.field("local_date", pa.date32()),
        pa.field("local_hour", pa.uint8()),
        pa.field("utc_offset_minutes", pa.int16()),
        pa.field("is_dst", pa.bool_()),
        pa.field("source_file", pa.string()),
        pa.field("source_row_number", pa.uint64()),
        pa.field("source_sha256", pa.string()),
        pa.field("processing_run_id", pa.string()),
    ]
    metrics = [pa.field(name, metric_type(name)) for name in TABLE_METRICS[table_name]]
    metadata = {
        b"dataset_version": str(DATASET_VERSION).encode(),
        b"transformation_version": TRANSFORMATION_VERSION.encode(),
        b"table": table_name.encode(),
    }
    return pa.schema(common + metrics, metadata=metadata)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    department: str
    period_start: int
    period_end: int
    size: int
    mtime_ns: int
    sha256: str

    def manifest_row(self) -> dict:
        return {
            "filename": self.path.name,
            "department": self.department,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class SyncPlan:
    department: str
    sources: tuple[SourceFile, ...]
    changed_files: tuple[str, ...]
    removed_files: tuple[str, ...]
    affected_years: tuple[int, ...]
    schema_rebuild_required: bool
    no_op: bool


class ProgressReporter(Protocol):
    def stage(self, name: str) -> None: ...
    def sources_started(self, total: int) -> None: ...
    def source_started(self, filename: str) -> None: ...
    def rows_processed(self, count: int) -> None: ...
    def source_finished(self) -> None: ...
    def close(self) -> None: ...


class NullProgressReporter:
    def stage(self, name: str) -> None:
        pass

    def sources_started(self, total: int) -> None:
        pass

    def source_started(self, filename: str) -> None:
        pass

    def rows_processed(self, count: int) -> None:
        pass

    def source_finished(self) -> None:
        pass

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class MetadataAudit:
    expected_departments: tuple[str, ...]
    observed_departments: tuple[str, ...]
    expected_source_files: int
    observed_source_files: int
    issues: tuple[str, ...]
    structural_issues: tuple[str, ...]

    @property
    def consistent(self) -> bool:
        return not self.issues and not self.structural_issues


def is_metropolitan(department: str) -> bool:
    if department in {"2A", "2B"}:
        return True
    return department.isdigit() and 1 <= int(department) <= 95


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _validated_manifests(analytics_root: Path) -> dict[str, dict]:
    manifests: dict[str, dict] = {}
    for path in sorted((analytics_root / "manifests").glob("department=*.json")):
        department = path.stem.split("=", 1)[1].upper()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid manifest {path.name}: {error}") from error
        if payload.get("department", "").upper() != department:
            raise ValueError(f"Manifest department mismatch in {path.name}")
        if not is_metropolitan(department):
            raise ValueError(f"Unsupported manifest department: {department}")
        source_files = payload.get("source_files")
        if not isinstance(source_files, dict):
            raise ValueError(f"Manifest {path.name} has no source_files mapping")
        for filename, row in source_files.items():
            if not isinstance(row, dict) or row.get("filename") != filename:
                raise ValueError(f"Invalid source record {filename!r} in {path.name}")
            required = {
                "filename", "department", "period_start", "period_end",
                "size", "mtime_ns", "sha256",
            }
            if not required.issubset(row):
                raise ValueError(f"Incomplete source record {filename!r} in {path.name}")
            if str(row["department"]).upper() != department:
                raise ValueError(f"Source department mismatch for {filename!r} in {path.name}")
        manifests[department] = payload
    return manifests


def _plan_manifest(plan: SyncPlan) -> dict:
    return {
        "department": plan.department,
        "network_category": "main",
        "source_files": {item.path.name: item.manifest_row() for item in plan.sources},
    }


def _metadata_rows(
    analytics_root: Path, current_plan: SyncPlan | None = None
) -> tuple[list[dict], list[dict]]:
    manifests = _validated_manifests(analytics_root)
    if current_plan is not None:
        manifests[current_plan.department] = _plan_manifest(current_plan)
    departments = []
    sources = []
    seen_sources: set[tuple[str, str]] = set()
    for department, manifest in sorted(manifests.items()):
        departments.append({
            "department": department,
            "department_name": DEPARTMENT_NAMES.get(department, f"Département {department}"),
            "source_time_convention": "UTC",
            "analytical_timezone": "Europe/Paris",
            "geography_class": "metropolitan",
        })
        network_category = manifest.get("network_category", "main")
        for filename, source in sorted(manifest["source_files"].items()):
            key = (department, filename)
            if key in seen_sources:
                raise ValueError(f"Duplicate source metadata key: {department}/{filename}")
            seen_sources.add(key)
            sources.append({
                "filename": filename,
                "department": department,
                "period_start": int(source["period_start"]),
                "period_end": int(source["period_end"]),
                "size": int(source["size"]),
                "mtime_ns": int(source["mtime_ns"]),
                "sha256": str(source["sha256"]),
                "network_category": network_category,
            })
    return departments, sources


def _partition_departments(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {
        path.name.split("=", 1)[1].upper()
        for path in root.glob("department=*") if path.is_dir()
    }


def audit_global_metadata(analytics_root: Path) -> MetadataAudit:
    manifests = _validated_manifests(analytics_root)
    expected = set(manifests)
    structural_issues = []
    partition_roots = {
        "facts": analytics_root / "facts/hourly_core",
        "stations": analytics_root / "dimensions/stations",
        "station history": analytics_root / "dimensions/station_metadata_history",
    }
    for label, root in partition_roots.items():
        observed = _partition_departments(root)
        if observed != expected:
            structural_issues.append(
                f"{label} departments differ from manifests: "
                f"missing={sorted(expected - observed)}, unexpected={sorted(observed - expected)}"
            )

    expected_source_count = sum(len(item["source_files"]) for item in manifests.values())
    department_path = analytics_root / "dimensions/departments.parquet"
    source_path = analytics_root / "metadata/source_files.parquet"
    observed_departments: list[str] = []
    observed_sources: list[dict] = []
    issues = []
    try:
        if department_path.exists():
            observed_departments = [
                str(row["department"]).upper()
                for row in pq.read_table(department_path).to_pylist()
            ]
        if source_path.exists():
            observed_sources = pq.read_table(source_path).to_pylist()
    except (OSError, pa.ArrowException) as error:
        issues.append(f"Global metadata cannot be read: {error}")

    observed_set = set(observed_departments)
    if observed_set != expected:
        issues.append(
            "departments metadata differs from manifests: "
            f"missing={sorted(expected - observed_set)}, unexpected={sorted(observed_set - expected)}"
        )
    if len(observed_departments) != len(observed_set):
        issues.append("departments metadata contains duplicate department rows")
    source_keys = [
        (str(row.get("department", "")).upper(), str(row.get("filename", "")))
        for row in observed_sources
    ]
    expected_keys = {
        (department, filename)
        for department, manifest in manifests.items()
        for filename in manifest["source_files"]
    }
    observed_keys = set(source_keys)
    if observed_keys != expected_keys:
        issues.append(
            "source_files metadata differs from manifests: "
            f"missing={len(expected_keys - observed_keys)}, unexpected={len(observed_keys - expected_keys)}"
        )
    if len(source_keys) != len(observed_keys):
        issues.append("source_files metadata contains duplicate department/filename rows")
    return MetadataAudit(
        expected_departments=tuple(sorted(expected)),
        observed_departments=tuple(sorted(observed_set)),
        expected_source_files=expected_source_count,
        observed_source_files=len(observed_sources),
        issues=tuple(issues), structural_issues=tuple(structural_issues),
    )


def summarize_dataset_status(raw_root: Path, analytics_root: Path) -> dict:
    manifests = _validated_manifests(analytics_root)
    department_rows = []
    for department in sorted(manifests):
        try:
            plan = plan_sync(raw_root, analytics_root, department)
            status = (
                "rebuild" if plan.schema_rebuild_required
                else "up-to-date" if plan.no_op
                else "pending"
            )
            department_rows.append({
                "department": department,
                "status": status,
                "source_archives": len(plan.sources),
                "changed_archives": len(plan.changed_files),
                "removed_archives": len(plan.removed_files),
                "affected_years": plan.affected_years,
                "error": None,
            })
        except (OSError, ValueError) as error:
            department_rows.append({
                "department": department,
                "status": "error",
                "source_archives": 0,
                "changed_archives": 0,
                "removed_archives": 0,
                "affected_years": (),
                "error": str(error),
            })

    raw_departments = set()
    for path in raw_root.glob("H_*.csv.gz"):
        match = SOURCE_PATTERN.fullmatch(path.name)
        if match:
            raw_departments.add(match.group("department").upper())
    audit = audit_global_metadata(analytics_root)
    return {
        "departments": department_rows,
        "audit": audit,
        "raw_only_departments": tuple(sorted(raw_departments - set(manifests))),
        "fact_departments": len(_partition_departments(analytics_root / "facts/hourly_core")),
        "station_departments": len(
            _partition_departments(analytics_root / "dimensions/stations")
        ),
        "failed_runs": len(list((analytics_root / "metadata/failures").glob("*.json"))),
    }


def discover_sources(raw_root: Path, department: str, previous: dict | None = None) -> tuple[SourceFile, ...]:
    previous_files = (previous or {}).get("source_files", {})
    sources = []
    for path in sorted(raw_root.glob(f"H_{department}_*.csv.gz")):
        match = SOURCE_PATTERN.fullmatch(path.name)
        if not match:
            continue
        stat = path.stat()
        old = previous_files.get(path.name)
        digest = (
            old["sha256"]
            if old and old.get("size") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns
            else sha256_file(path)
        )
        sources.append(SourceFile(
            path=path, department=department,
            period_start=int(match.group("start")), period_end=int(match.group("end")),
            size=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=digest,
        ))
    return tuple(sources)


def plan_sync(raw_root: Path, analytics_root: Path, department: str, rebuild: bool = False) -> SyncPlan:
    department = department.upper()
    if not is_metropolitan(department):
        raise ValueError(f"Dataset v1 supports metropolitan departments only: {department}")
    manifest_path = analytics_root / "manifests" / f"department={department}.json"
    previous = _load_manifest(manifest_path)
    sources = discover_sources(raw_root, department, previous)
    if not sources:
        raise ValueError(f"No verified local hourly archives found for department {department}")
    old_files = (previous or {}).get("source_files", {})
    current = {item.path.name: item.manifest_row() for item in sources}
    changed = sorted(
        name for name, value in current.items()
        if name not in old_files or value["sha256"] != old_files[name].get("sha256")
    )
    removed = sorted(set(old_files) - set(current))
    schema_mismatch = bool(previous and (
        previous.get("dataset_version") != DATASET_VERSION
        or previous.get("transformation_version") != TRANSFORMATION_VERSION
    ))
    if rebuild or previous is None:
        years = {year for item in sources for year in range(item.period_start, item.period_end + 1)}
    else:
        years = set()
        for name in changed:
            value = current[name]
            years.update(range(value["period_start"], value["period_end"] + 1))
        for name in removed:
            value = old_files[name]
            years.update(range(value["period_start"], value["period_end"] + 1))
    return SyncPlan(
        department=department, sources=sources, changed_files=tuple(changed),
        removed_files=tuple(removed), affected_years=tuple(sorted(years)),
        schema_rebuild_required=schema_mismatch,
        no_op=not rebuild and previous is not None and not changed and not removed and not schema_mismatch,
    )


class ChunkWriter:
    def __init__(self, root: Path):
        self.root = root
        self.counters: dict[tuple[str, int], int] = {}

    def write(self, table_name: str, year: int, rows: list[dict]) -> None:
        if not rows:
            return
        key = (table_name, year)
        index = self.counters.get(key, 0)
        directory = self.root / table_name / f"department={rows[0]['department']}" / f"year={year}" / "chunks"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"chunk-{index:06d}.parquet"
        table = pa.Table.from_pylist(rows, schema=table_schema(table_name))
        pq.write_table(table, path, compression="zstd", row_group_size=BATCH_ROWS)
        self.counters[key] = index + 1


def _convert_value(name: str, value: str):
    if value == "":
        return None
    data_type = metric_type(name)
    if pa.types.is_integer(data_type):
        return int(float(value))
    return float(value)


def _common_row(
    row: list[str], indexes: dict[str, int], department: str, source: SourceFile,
    source_row_number: int, run_id: str,
) -> tuple[dict, int]:
    for required in IDENTITY_COLUMNS:
        if required not in indexes or row[indexes[required]] == "":
            raise ValueError(f"{source.path.name}:{source_row_number}: missing required {required}")
    raw_time = row[indexes["AAAAMMJJHH"]]
    try:
        naive = datetime.strptime(raw_time, "%Y%m%d%H")
    except ValueError as error:
        raise ValueError(f"{source.path.name}:{source_row_number}: invalid AAAAMMJJHH={raw_time!r}") from error
    utc_time = naive.replace(tzinfo=timezone.utc)
    local_time = utc_time.astimezone(LOCAL_TIMEZONE)
    offset = local_time.utcoffset()
    offset_seconds = int(offset.total_seconds()) if offset else 0
    common = {
        "department": department,
        "network_category": "main",
        "NUM_POSTE": row[indexes["NUM_POSTE"]],
        "NOM_USUEL": row[indexes["NOM_USUEL"]],
        "LAT": float(row[indexes["LAT"]]),
        "LON": float(row[indexes["LON"]]),
        "ALTI": int(float(row[indexes["ALTI"]])),
        "AAAAMMJJHH": raw_time,
        "observation_time_utc": utc_time,
        "observation_time_local": local_time,
        "local_date": local_time.date(),
        "local_hour": local_time.hour,
        "utc_offset_minutes": int(offset_seconds / 60),
        "is_dst": bool(local_time.dst() and local_time.dst().total_seconds()),
        "source_file": source.path.name,
        "source_row_number": source_row_number,
        "source_sha256": source.sha256,
        "processing_run_id": run_id,
    }
    return common, utc_time.year


def _table_row(common: dict, row: list[str], indexes: dict[str, int], table_name: str) -> dict:
    output = dict(common)
    for name in TABLE_METRICS[table_name]:
        output[name] = _convert_value(name, row[indexes[name]]) if name in indexes else None
    return output


def _has_payload(row: list[str], indexes: dict[str, int], base_columns: Iterable[str]) -> bool:
    return any(name in indexes and row[indexes[name]] != "" for name in base_columns)


def process_sources(
    plan: SyncPlan, stage_root: Path, run_id: str, progress: ProgressReporter
) -> dict:
    affected = set(plan.affected_years)
    writer = ChunkWriter(stage_root / "data")
    buffers: dict[tuple[str, int], list[dict]] = {}
    source_rows = 0
    core_rows = 0
    extension_rows = {name: 0 for name in TABLE_BASES if name != "hourly_core"}
    processed_files = []
    selected_sources = [
        source for source in plan.sources
        if affected.intersection(range(source.period_start, source.period_end + 1))
    ]
    progress.sources_started(len(selected_sources))
    for source in selected_sources:
        progress.source_started(source.path.name)
        pending_progress = 0
        encoding, delimiter = detect_encoding_and_delimiter(source.path)
        with gzip.open(source.path, "rb") as binary:
            reader = csv.reader(io.TextIOWrapper(binary, encoding=encoding, newline=""), delimiter=delimiter)
            columns = tuple(next(reader))
            indexes = {name: index for index, name in enumerate(columns)}
            missing = [name for name in IDENTITY_COLUMNS if name not in indexes]
            if missing:
                raise ValueError(f"{source.path.name}: missing required columns {missing}")
            unknown_quality = sorted({name for name in columns if name.startswith("Q")} - {
                name for metrics in TABLE_METRICS.values() for name in metrics if name.startswith("Q")
            })
            processed_files.append({"filename": source.path.name, "unknown_quality_columns": unknown_quality})
            for source_row_number, row in enumerate(reader, start=2):
                if len(row) != len(columns):
                    raise ValueError(
                        f"{source.path.name}:{source_row_number}: expected {len(columns)} fields, got {len(row)}"
                    )
                common, year = _common_row(row, indexes, plan.department, source, source_row_number, run_id)
                if year not in affected:
                    continue
                source_rows += 1
                core = _table_row(common, row, indexes, "hourly_core")
                key = ("hourly_core", year)
                buffers.setdefault(key, []).append(core)
                core_rows += 1
                for table_name, base_columns in TABLE_BASES.items():
                    if table_name == "hourly_core" or not _has_payload(row, indexes, base_columns):
                        continue
                    buffers.setdefault((table_name, year), []).append(
                        _table_row(common, row, indexes, table_name)
                    )
                    extension_rows[table_name] += 1
                for buffer_key in list(buffers):
                    if len(buffers[buffer_key]) >= BATCH_ROWS:
                        writer.write(buffer_key[0], buffer_key[1], buffers[buffer_key])
                        buffers[buffer_key].clear()
                pending_progress += 1
                if pending_progress >= 1_000:
                    progress.rows_processed(pending_progress)
                    pending_progress = 0
        for buffer_key in list(buffers):
            if buffers[buffer_key]:
                writer.write(buffer_key[0], buffer_key[1], buffers[buffer_key])
                buffers[buffer_key].clear()
        if pending_progress:
            progress.rows_processed(pending_progress)
        progress.source_finished()
    return {
        "source_rows": source_rows, "core_rows": core_rows,
        "extension_rows": extension_rows, "processed_files": processed_files,
    }


def compact_stage(stage_root: Path, department: str) -> None:
    data_root = stage_root / "data"
    for chunks_dir in sorted(data_root.glob(f"*/department={department}/year=*/chunks")):
        partition = chunks_dir.parent
        schema = pq.read_schema(next(chunks_dir.glob("*.parquet")))
        dataset = pads.dataset(chunks_dir, format="parquet")
        part_index = 0
        writer: pq.ParquetWriter | None = None
        path: Path | None = None
        try:
            for batch in dataset.scanner(batch_size=BATCH_ROWS).to_batches():
                if writer is None:
                    path = partition / f"part-{part_index:05d}.parquet"
                    writer = pq.ParquetWriter(path, schema, compression="zstd")
                writer.write_batch(batch)
                if path and path.exists() and path.stat().st_size >= TARGET_FILE_SIZE:
                    writer.close()
                    writer = None
                    part_index += 1
        finally:
            if writer is not None:
                writer.close()
        shutil.rmtree(chunks_dir)


def _sql_paths(paths: list[Path]) -> str:
    return "[" + ",".join("'" + str(path).replace("'", "''") + "'" for path in paths) + "]"


def validate_stage(stage_root: Path, department: str, expected_rows: int) -> dict:
    paths = sorted((stage_root / "data" / "hourly_core" / f"department={department}").glob("year=*/*.parquet"))
    if not paths:
        if expected_rows:
            raise ValueError("No staged hourly_core Parquet files were written")
        return {"actual_rows": 0, "years": []}
    connection = duckdb.connect()
    path_sql = _sql_paths(paths)
    actual = connection.execute(f"SELECT count(*) FROM read_parquet({path_sql})").fetchone()[0]
    if actual != expected_rows:
        raise ValueError(f"Row reconciliation failed: source={expected_rows}, parquet={actual}")
    duplicate_count = connection.execute(f"""
        SELECT count(*) FROM (
            SELECT network_category, NUM_POSTE, observation_time_utc
            FROM read_parquet({path_sql})
            GROUP BY ALL HAVING count(*) > 1
        )
    """).fetchone()[0]
    if duplicate_count:
        raise ValueError(f"Duplicate station-hour keys: {duplicate_count}")
    years = [int(path.parent.name.split("=", 1)[1]) for path in paths]
    return {"actual_rows": actual, "years": sorted(set(years))}


def _all_core_paths(analytics_root: Path, stage_root: Path, department: str, affected: set[int]) -> list[Path]:
    final_root = analytics_root / "facts" / "hourly_core" / f"department={department}"
    paths = [
        path for path in final_root.glob("year=*/*.parquet")
        if int(path.parent.name.split("=", 1)[1]) not in affected
    ]
    paths.extend((stage_root / "data" / "hourly_core" / f"department={department}").glob("year=*/*.parquet"))
    return sorted(paths)


def build_station_dimensions(
    analytics_root: Path, stage_root: Path, department: str, affected: set[int]
) -> None:
    paths = _all_core_paths(analytics_root, stage_root, department, affected)
    if not paths:
        return
    connection = duckdb.connect()
    path_sql = _sql_paths(paths)
    stations = connection.execute(f"""
        SELECT network_category, NUM_POSTE,
               arg_max(NOM_USUEL, observation_time_utc) AS NOM_USUEL,
               arg_max(LAT, observation_time_utc) AS LAT,
               arg_max(LON, observation_time_utc) AS LON,
               arg_max(ALTI, observation_time_utc) AS ALTI,
               min(observation_time_utc) AS first_observation_utc,
               max(observation_time_utc) AS last_observation_utc,
               list_sort(list_distinct(list(department))) AS source_departments,
               count(DISTINCT NOM_USUEL) AS name_variant_count,
               count(DISTINCT struct_pack(lat := LAT, lon := LON, alti := ALTI)) AS location_variant_count
        FROM read_parquet({path_sql}) GROUP BY network_category, NUM_POSTE
        ORDER BY network_category, NUM_POSTE
    """).to_arrow_table()
    history = connection.execute(f"""
        SELECT network_category, NUM_POSTE, NOM_USUEL, LAT, LON, ALTI,
               min(observation_time_utc) AS first_observation_utc,
               max(observation_time_utc) AS last_observation_utc
        FROM read_parquet({path_sql})
        GROUP BY network_category, NUM_POSTE, NOM_USUEL, LAT, LON, ALTI
        ORDER BY network_category, NUM_POSTE, first_observation_utc
    """).to_arrow_table()
    metadata = {b"dataset_version": b"1", b"transformation_version": TRANSFORMATION_VERSION.encode()}
    dimension_root = stage_root / "dimensions"
    station_path = dimension_root / "stations" / f"department={department}" / "part-00000.parquet"
    history_path = dimension_root / "station_metadata_history" / f"department={department}" / "part-00000.parquet"
    station_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(stations.replace_schema_metadata(metadata), station_path, compression="zstd")
    pq.write_table(history.replace_schema_metadata(metadata), history_path, compression="zstd")


def parse_official_dictionary(path: Path) -> dict[str, str]:
    descriptions = {}
    if not path.exists():
        return descriptions
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if ":" not in line:
            continue
        name, description = line.split(":", 1)
        name = name.strip()
        if name and " " not in name:
            descriptions[name] = description.strip()
    return descriptions


def _official_unit(description: str | None) -> str | None:
    if not description:
        return None
    match = re.search(r"\(en ([^)]+)\)", description, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def build_static_dimensions(stage_root: Path, reference_root: Path) -> None:
    official = parse_official_dictionary(reference_root / "H_descriptif_champs.csv")
    metric_rows = []
    for table_name, metrics in TABLE_METRICS.items():
        for name in metrics:
            quality_for = name[1:] if name.startswith("Q") else None
            base = quality_for or name
            label = (
                f"Quality code for {ENGLISH_LABELS.get(base, base)}"
                if quality_for else ENGLISH_LABELS.get(name, name.replace("_", " ").title())
            )
            metric_rows.append({
                "source_mnemonic": name,
                "quality_mnemonic": f"Q{name}" if f"Q{name}" in metrics else None,
                "quality_for": quality_for,
                "category": table_name.removeprefix("hourly_"),
                "owning_table": table_name,
                "physical_type": str(metric_type(name)),
                "official_description_fr": official.get(name),
                "official_unit_text": _official_unit(official.get(name)),
                "english_label": label,
                "english_description": (
                    f"Météo-France quality code associated with {base}." if quality_for
                    else f"Météo-France hourly measurement: {label}."
                ),
                "english_text_curated": True,
                "categorical": name in INTEGER_METRICS or name.startswith("Q") or name.startswith("STATUS_"),
                "documentation_source": "data/meteo_france/reference/H_descriptif_champs.csv",
                "schema_version": DATASET_VERSION,
            })
    dimensions = stage_root / "dimensions"
    dimensions.mkdir(parents=True, exist_ok=True)
    metadata = {b"dataset_version": b"1", b"transformation_version": TRANSFORMATION_VERSION.encode()}
    metrics = pa.Table.from_pylist(metric_rows).replace_schema_metadata(metadata)
    pq.write_table(metrics, dimensions / "metrics.parquet", compression="zstd")


def build_global_metadata(
    analytics_root: Path, stage_root: Path, current_plan: SyncPlan | None = None
) -> tuple[int, int]:
    department_rows, source_rows = _metadata_rows(analytics_root, current_plan)
    metadata = {
        b"dataset_version": str(DATASET_VERSION).encode(),
        b"transformation_version": TRANSFORMATION_VERSION.encode(),
    }
    department_schema = pa.schema([
        pa.field("department", pa.string()),
        pa.field("department_name", pa.string()),
        pa.field("source_time_convention", pa.string()),
        pa.field("analytical_timezone", pa.string()),
        pa.field("geography_class", pa.string()),
    ], metadata=metadata)
    source_schema = pa.schema([
        pa.field("filename", pa.string()),
        pa.field("department", pa.string()),
        pa.field("period_start", pa.int64()),
        pa.field("period_end", pa.int64()),
        pa.field("size", pa.int64()),
        pa.field("mtime_ns", pa.int64()),
        pa.field("sha256", pa.string()),
        pa.field("network_category", pa.string()),
    ])
    dimensions = stage_root / "dimensions"
    metadata_root = stage_root / "metadata"
    dimensions.mkdir(parents=True, exist_ok=True)
    metadata_root.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(department_rows, schema=department_schema),
        dimensions / "departments.parquet", compression="zstd",
    )
    pq.write_table(
        pa.Table.from_pylist(source_rows, schema=source_schema),
        metadata_root / "source_files.parquet", compression="zstd",
    )
    return len(department_rows), len(source_rows)


def build_metadata_tables(
    analytics_root: Path, stage_root: Path, run_row: dict
) -> None:
    metadata_root = stage_root / "metadata"
    metadata_root.mkdir(parents=True, exist_ok=True)
    previous_path = analytics_root / "metadata" / "processing_runs.parquet"
    rows = pq.read_table(previous_path).to_pylist() if previous_path.exists() else []
    rows.append(run_row)
    pq.write_table(pa.Table.from_pylist(rows), metadata_root / "processing_runs.parquet", compression="zstd")


def _promote_directory(stage: Path, final: Path, backup_root: Path) -> None:
    backup = backup_root / final.name
    if final.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        final.replace(backup)
    if stage.exists():
        final.parent.mkdir(parents=True, exist_ok=True)
        stage.replace(final)


def promote(stage_root: Path, analytics_root: Path, department: str, affected: set[int]) -> None:
    backup_root = stage_root / "backup"
    promoted: list[tuple[Path, Path | None]] = []
    try:
        for table_name in TABLE_BASES:
            final_base = (
                analytics_root / ("facts" if table_name == "hourly_core" else "extensions") /
                table_name / f"department={department}"
            )
            stage_base = stage_root / "data" / table_name / f"department={department}"
            for year in sorted(affected):
                final = final_base / f"year={year}"
                staged = stage_base / f"year={year}"
                backup = backup_root / table_name / f"year={year}"
                if final.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    final.replace(backup)
                if staged.exists():
                    final.parent.mkdir(parents=True, exist_ok=True)
                    staged.replace(final)
                promoted.append((final, backup if backup.exists() else None))
        for name in ("stations", "station_metadata_history"):
            final = analytics_root / "dimensions" / name / f"department={department}"
            staged = stage_root / "dimensions" / name / f"department={department}"
            backup = backup_root / "dimensions" / name
            if final.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                final.replace(backup)
            promoted.append((final, backup if backup.exists() else None))
            final.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(final)
        for relative in (
            Path("dimensions/metrics.parquet"), Path("dimensions/departments.parquet"),
            Path("metadata/source_files.parquet"), Path("metadata/processing_runs.parquet"),
        ):
            final = analytics_root / relative
            staged = stage_root / relative
            backup = backup_root / relative
            if final.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                final.replace(backup)
            promoted.append((final, backup if backup.exists() else None))
            final.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(final)
    except Exception:
        for final, backup in reversed(promoted):
            if final.exists():
                shutil.rmtree(final) if final.is_dir() else final.unlink()
            if backup and backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.replace(final)
        raise


def write_manifest(analytics_root: Path, plan: SyncPlan, run_id: str, years: list[int], counts: dict) -> None:
    path = analytics_root / "manifests" / f"department={plan.department}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset_version": DATASET_VERSION,
        "transformation_version": TRANSFORMATION_VERSION,
        "department": plan.department,
        "network_category": "main",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "processing_run_id": run_id,
        "source_files": {item.path.name: item.manifest_row() for item in plan.sources},
        "materialized_years": years,
        "counts": counts,
    }
    temporary = path.with_suffix(".json.part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def refresh_duckdb(analytics_root: Path) -> None:
    database = analytics_root / "weather.duckdb"
    connection = duckdb.connect(str(database))
    view_paths = {
        "hourly_core": analytics_root / "facts/hourly_core/**/*.parquet",
        "hourly_surface": analytics_root / "extensions/hourly_surface/**/*.parquet",
        "hourly_marine": analytics_root / "extensions/hourly_marine/**/*.parquet",
        "hourly_snow": analytics_root / "extensions/hourly_snow/**/*.parquet",
        "stations": analytics_root / "dimensions/stations/**/*.parquet",
        "station_metadata_history": analytics_root / "dimensions/station_metadata_history/**/*.parquet",
        "metrics": analytics_root / "dimensions/metrics.parquet",
        "departments": analytics_root / "dimensions/departments.parquet",
        "source_files": analytics_root / "metadata/source_files.parquet",
        "processing_runs": analytics_root / "metadata/processing_runs.parquet",
    }
    for view, path in view_paths.items():
        files = list(path.parent.glob(path.name)) if "**" not in str(path) else list(analytics_root.glob(str(path.relative_to(analytics_root))))
        if not files:
            if view in TABLE_BASES:
                fields = table_schema(view)
                expressions = []
                for field in fields:
                    if pa.types.is_string(field.type):
                        sql_type = "VARCHAR"
                    elif pa.types.is_float32(field.type):
                        sql_type = "REAL"
                    elif pa.types.is_float64(field.type):
                        sql_type = "DOUBLE"
                    elif pa.types.is_uint8(field.type):
                        sql_type = "UTINYINT"
                    elif pa.types.is_uint16(field.type):
                        sql_type = "USMALLINT"
                    elif pa.types.is_uint64(field.type):
                        sql_type = "UBIGINT"
                    elif pa.types.is_int16(field.type):
                        sql_type = "SMALLINT"
                    elif pa.types.is_int32(field.type):
                        sql_type = "INTEGER"
                    elif pa.types.is_boolean(field.type):
                        sql_type = "BOOLEAN"
                    elif pa.types.is_date32(field.type):
                        sql_type = "DATE"
                    elif pa.types.is_timestamp(field.type):
                        sql_type = "TIMESTAMPTZ"
                    else:
                        raise TypeError(f"No DuckDB mapping for {field.type}")
                    expressions.append(f'CAST(NULL AS {sql_type}) AS "{field.name}"')
                connection.execute(
                    f"CREATE OR REPLACE VIEW {view} AS SELECT {','.join(expressions)} WHERE false"
                )
            continue
        quoted = str(path).replace("'", "''")
        hive = "true" if "department=" in str(files[0]) else "false"
        connection.execute(
            f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{quoted}', hive_partitioning={hive}, union_by_name=true)"
        )
    connection.close()


def _validate_staged_global_metadata(
    analytics_root: Path, stage_root: Path
) -> tuple[int, int]:
    expected_departments, expected_sources = _metadata_rows(analytics_root)
    department_rows = pq.read_table(stage_root / "dimensions/departments.parquet").to_pylist()
    source_rows = pq.read_table(stage_root / "metadata/source_files.parquet").to_pylist()
    expected_department_codes = [row["department"] for row in expected_departments]
    actual_department_codes = [row["department"] for row in department_rows]
    if actual_department_codes != expected_department_codes:
        raise ValueError("Staged department metadata does not match manifests")
    expected_keys = [(row["department"], row["filename"]) for row in expected_sources]
    actual_keys = [(row["department"], row["filename"]) for row in source_rows]
    if actual_keys != expected_keys or len(actual_keys) != len(set(actual_keys)):
        raise ValueError("Staged source metadata does not match manifests")
    return len(department_rows), len(source_rows)


def _promote_global_metadata(stage_root: Path, analytics_root: Path) -> None:
    relative_paths = (
        Path("dimensions/departments.parquet"),
        Path("metadata/source_files.parquet"),
    )
    backup_root = stage_root / "backup"
    promoted: list[tuple[Path, Path | None]] = []
    try:
        for relative in relative_paths:
            final = analytics_root / relative
            staged = stage_root / relative
            backup = backup_root / relative
            if final.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                final.replace(backup)
            promoted.append((final, backup if backup.exists() else None))
            final.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(final)
        refresh_duckdb(analytics_root)
    except Exception:
        for final, backup in reversed(promoted):
            if final.exists():
                final.unlink()
            if backup and backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.replace(final)
        try:
            refresh_duckdb(analytics_root)
        except Exception:
            pass
        raise


def repair_global_metadata(analytics_root: Path) -> dict:
    before = audit_global_metadata(analytics_root)
    if before.structural_issues:
        raise ValueError("; ".join(before.structural_issues))
    repair_id = str(uuid.uuid4())
    stage_root = analytics_root / "staging" / f"metadata-repair-{repair_id}"
    stage_root.mkdir(parents=True, exist_ok=True)
    try:
        build_global_metadata(analytics_root, stage_root)
        department_count, source_count = _validate_staged_global_metadata(
            analytics_root, stage_root
        )
        _promote_global_metadata(stage_root, analytics_root)
        after = audit_global_metadata(analytics_root)
        if not after.consistent:
            raise ValueError("Metadata remained inconsistent after repair")
        report = {
            "repair_id": repair_id,
            "repaired_at": datetime.now(timezone.utc).isoformat(),
            "departments": list(after.expected_departments),
            "department_count": department_count,
            "source_file_count": source_count,
            "issues_before": list(before.issues),
            "status": "success",
        }
        report_root = analytics_root / "metadata/repairs"
        report_root.mkdir(parents=True, exist_ok=True)
        report_path = report_root / f"{repair_id}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report | {"report_path": str(report_path)}
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def sync_dataset(
    raw_root: Path, analytics_root: Path, reference_root: Path, department: str,
    rebuild: bool = False, progress: ProgressReporter | None = None,
) -> tuple[int, dict]:
    reporter = progress or NullProgressReporter()
    reporter.stage("planning")
    try:
        plan = plan_sync(raw_root, analytics_root, department, rebuild=rebuild)
    except Exception:
        reporter.close()
        raise
    if plan.schema_rebuild_required and not rebuild:
        reporter.close()
        return 1, {"status": "schema-rebuild-required", "plan": plan}
    if plan.no_op:
        reporter.close()
        return 0, {"status": "up-to-date", "plan": plan}
    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    stage_root = analytics_root / "staging" / run_id
    stage_root.mkdir(parents=True, exist_ok=True)
    try:
        reporter.stage("source processing")
        counts = process_sources(plan, stage_root, run_id, reporter)
        reporter.stage("compaction")
        compact_stage(stage_root, plan.department)
        reporter.stage("validation")
        validation = validate_stage(stage_root, plan.department, counts["source_rows"])
        reporter.stage("dimensions")
        build_station_dimensions(
            analytics_root, stage_root, plan.department, set(plan.affected_years)
        )
        build_static_dimensions(stage_root, reference_root)
        reporter.stage("metadata")
        build_global_metadata(analytics_root, stage_root, plan)
        finished = datetime.now(timezone.utc)
        run_row = {
            "processing_run_id": run_id, "department": plan.department,
            "dataset_version": DATASET_VERSION, "transformation_version": TRANSFORMATION_VERSION,
            "started_at": started, "finished_at": finished, "status": "success",
            "changed_files": json.dumps(plan.changed_files),
            "affected_years": json.dumps(plan.affected_years),
            "source_rows": counts["source_rows"], "core_rows": counts["core_rows"],
        }
        build_metadata_tables(analytics_root, stage_root, run_row)
        reporter.stage("promotion")
        promote(stage_root, analytics_root, plan.department, set(plan.affected_years))
        all_years = sorted({
            int(path.name.split("=", 1)[1])
            for path in (analytics_root / "facts/hourly_core" / f"department={plan.department}").glob("year=*")
        })
        reporter.stage("manifest")
        write_manifest(analytics_root, plan, run_id, all_years, counts)
        reporter.stage("DuckDB refresh")
        refresh_duckdb(analytics_root)
        shutil.rmtree(stage_root, ignore_errors=True)
        return 0, {"status": "synchronized", "plan": plan, "counts": counts, "validation": validation, "run_id": run_id}
    except Exception as error:
        diagnostics = analytics_root / "metadata" / "failures"
        diagnostics.mkdir(parents=True, exist_ok=True)
        (diagnostics / f"{run_id}.json").write_text(json.dumps({
            "processing_run_id": run_id, "department": department,
            "started_at": started.isoformat(), "failed_at": datetime.now(timezone.utc).isoformat(),
            "error_type": type(error).__name__, "error": str(error),
            "affected_years": plan.affected_years,
        }, indent=2), encoding="utf-8")
        shutil.rmtree(stage_root, ignore_errors=True)
        return 1, {"status": "failed", "error": str(error), "plan": plan, "run_id": run_id}
    finally:
        reporter.close()
