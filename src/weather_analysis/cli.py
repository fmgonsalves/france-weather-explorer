from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import requests
from tqdm import tqdm

from .archive_inspection import run_inspection
from .analytical_dataset import (
    DATASET_VERSION,
    NullProgressReporter,
    audit_global_metadata,
    plan_sync,
    repair_global_metadata,
    summarize_dataset_status,
    sync_dataset,
)
from .department_exploration import run_department_exploration
from .meteo_france import (
    FileStatus,
    default_output_dir,
    download_resource,
    fetch_resources,
    human_size,
    inspect_resources,
    partial_path,
    select_resources,
    write_manifest,
)
from .visualization.dash import run_dashboard


class TqdmProgressReporter:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.interactive = enabled and sys.stderr.isatty()
        self.source_bar = None
        self.row_bar = None

    def stage(self, name: str) -> None:
        if not self.enabled:
            return
        if name != "source processing":
            self._close_bars()
        message = f"[dataset] {name}"
        tqdm.write(message, file=sys.stderr) if self.interactive else print(message, file=sys.stderr)

    def sources_started(self, total: int) -> None:
        if not self.interactive:
            return
        self.source_bar = tqdm(total=total, desc="Archives", unit="archive", position=0)
        self.row_bar = tqdm(desc="Rows", unit="row", unit_scale=True, position=1)

    def source_started(self, filename: str) -> None:
        if self.source_bar is not None:
            self.source_bar.set_postfix_str(filename)

    def rows_processed(self, count: int) -> None:
        if self.row_bar is not None:
            self.row_bar.update(count)

    def source_finished(self) -> None:
        if self.source_bar is not None:
            self.source_bar.update(1)

    def close(self) -> None:
        self._close_bars()

    def _close_bars(self) -> None:
        if self.row_bar is not None:
            self.row_bar.close()
            self.row_bar = None
        if self.source_bar is not None:
            self.source_bar.close()
            self.source_bar = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weather-analysis")
    commands = parser.add_subparsers(dest="service", required=True)
    dashboard = commands.add_parser("dashboard", help="Run weather visualization applications")
    dashboard_apps = dashboard.add_subparsers(dest="dashboard_app", required=True)
    dash = dashboard_apps.add_parser("dash", help="Run the Plotly Dash dashboard")
    dash.add_argument(
        "--analytics-dir", type=Path, default=Path("data/meteo_france/analytics/v1")
    )
    dash.add_argument(
        "--geography-dir", type=Path, default=Path("data/reference/geography")
    )
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8050)
    dash.add_argument("--debug", action="store_true")
    meteo = commands.add_parser("meteo-france", help="Manage Météo-France archives")
    actions = meteo.add_subparsers(dest="action", required=True)
    for action in ("status", "download", "inspect"):
        command = actions.add_parser(action)
        command.add_argument("--category", choices=("main", "complementary"), default="main")
        command.add_argument("--department", action="append", dest="departments")
        command.add_argument("--start-year", type=int)
        command.add_argument("--end-year", type=int)
        command.add_argument("--output-dir", type=Path)
        if action == "inspect":
            command.add_argument(
                "--mode", choices=("schema", "inventory", "profile", "explore"), default="schema"
            )
            command.add_argument(
                "--report-dir", type=Path, default=Path("data/meteo_france/metadata")
            )
            command.add_argument("--workers", type=int, default=4)
    dataset = actions.add_parser("dataset", help="Build the analytical Parquet dataset")
    dataset_actions = dataset.add_subparsers(dest="dataset_action", required=True)
    for dataset_action in ("status", "sync", "rebuild"):
        command = dataset_actions.add_parser(dataset_action)
        command.add_argument("--department", required=dataset_action != "status")
        command.add_argument(
            "--raw-dir", type=Path, default=Path("data/meteo_france/hourly_raw")
        )
        command.add_argument(
            "--analytics-dir", type=Path, default=Path("data/meteo_france/analytics/v1")
        )
        command.add_argument(
            "--reference-dir", type=Path, default=Path("data/meteo_france/reference")
        )
        if dataset_action == "rebuild":
            command.add_argument("--schema-version", type=int, required=True)
        if dataset_action == "status":
            command.add_argument(
                "--verbose", action="store_true",
                help="Show complete expected and observed metadata department lists",
            )
            command.add_argument(
                "--include-unmaterialized", action="store_true",
                help="List downloaded departments without an analytical dataset",
            )
        if dataset_action in {"sync", "rebuild"}:
            command.add_argument("--no-progress", action="store_true")
    repair = dataset_actions.add_parser(
        "repair-metadata", help="Rebuild shared metadata from department manifests"
    )
    repair.add_argument(
        "--analytics-dir", type=Path, default=Path("data/meteo_france/analytics/v1")
    )
    return parser


def _validate_years(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if (
        args.start_year is not None
        and args.end_year is not None
        and args.start_year > args.end_year
    ):
        parser.error("--start-year cannot be later than --end-year")


def _print_overview(category: str, resources, output_dir: Path, inspections) -> None:
    counts = Counter(item.status for item in inspections.values())
    expected = sum(resource.expected_size or 0 for resource in resources)
    redundant = sum(item.redundant_partial for item in inspections.values())
    discrepancies = sum(item.catalog_mismatch for item in inspections.values())
    print(f"Category: {category}")
    print(f"Output directory: {output_dir.resolve()}")
    print(f"Selected resources: {len(resources):,}")
    print(f"Known expected size: {human_size(expected)}")
    for status in FileStatus:
        print(f"{status.value}: {counts[status]:,}")
    print(f"catalog/server size discrepancies: {discrepancies:,}")
    print(f"redundant partial files: {redundant:,}")


def _print_metadata_audit(audit, verbose: bool = False) -> None:
    print(
        "Shared catalog metadata: "
        f"{'consistent' if audit.consistent else 'inconsistent'}"
    )
    print(
        "Metadata departments: "
        f"expected={len(audit.expected_departments):,}; "
        f"observed={len(audit.observed_departments):,}"
    )
    print(
        "Metadata source archives: "
        f"expected={audit.expected_source_files:,}; "
        f"observed={audit.observed_source_files:,}"
    )
    expected_departments = set(audit.expected_departments)
    observed_departments = set(audit.observed_departments)
    missing_departments = sorted(expected_departments - observed_departments)
    unexpected_departments = sorted(observed_departments - expected_departments)
    if missing_departments:
        print(f"Missing metadata departments: {','.join(missing_departments)}")
    if unexpected_departments:
        print(f"Unexpected metadata departments: {','.join(unexpected_departments)}")
    if verbose:
        print(
            "Expected metadata department list: "
            f"{','.join(audit.expected_departments) or 'none'}"
        )
        print(
            "Observed metadata department list: "
            f"{','.join(audit.observed_departments) or 'none'}"
        )
    for issue in (*audit.structural_issues, *audit.issues):
        print(f"Metadata issue: {issue}")


def _year_summary(years) -> str:
    if not years:
        return "none"
    return str(years[0]) if len(years) == 1 else f"{years[0]}–{years[-1]}"


def _print_global_dataset_status(summary: dict, verbose: bool, include_raw: bool) -> int:
    rows = summary["departments"]
    counts = Counter(row["status"] for row in rows)
    print(f"Analytical dataset v{DATASET_VERSION}")
    print(f"Materialized departments: {len(rows):,}")
    print(f"Departments up-to-date: {counts['up-to-date']:,}")
    print(f"Departments with pending changes: {counts['pending']:,}")
    print(f"Departments requiring rebuild: {counts['rebuild']:,}")
    print(f"Departments with errors: {counts['error']:,}")
    print()
    _print_metadata_audit(summary["audit"], verbose=verbose)
    print(f"Fact departments: {summary['fact_departments']:,}")
    print(f"Station-dimension departments: {summary['station_departments']:,}")
    print(f"Failed processing runs: {summary['failed_runs']:,}")
    raw_only = summary["raw_only_departments"]
    print(f"Raw-only departments: {len(raw_only):,}")

    displayed = rows if verbose else [row for row in rows if row["status"] != "up-to-date"]
    if displayed:
        print("\nMaterialized department details:")
        for row in displayed:
            details = (
                f"{row['department']}  {row['status']}  "
                f"archives={row['source_archives']:,}  "
                f"changed={row['changed_archives']:,}  removed={row['removed_archives']:,}  "
                f"years={_year_summary(row['affected_years'])}"
            )
            print(details)
            if row["error"]:
                print(f"  Error: {row['error']}")
    if include_raw and raw_only:
        print(f"\nRaw-only department list: {','.join(raw_only)}")
    unhealthy = (
        not summary["audit"].consistent
        or counts["rebuild"] > 0
        or counts["error"] > 0
    )
    return 1 if unhealthy else 0


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.service == "dashboard":
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        try:
            run_dashboard(
                args.analytics_dir,
                geography_dir=args.geography_dir,
                host=args.host,
                port=args.port,
                debug=args.debug,
            )
        except (OSError, ValueError) as error:
            print(f"Dashboard failed: {error}", file=sys.stderr)
            return 1
        return 0
    if args.action == "dataset":
        if args.dataset_action == "repair-metadata":
            try:
                result = repair_global_metadata(args.analytics_dir)
            except (OSError, ValueError) as error:
                print(f"Metadata repair failed: {error}", file=sys.stderr)
                return 1
            print(f"Metadata repair: {result['status']}")
            print(f"Departments: {result['department_count']:,}")
            print(f"Source archives: {result['source_file_count']:,}")
            print(f"Report: {Path(result['report_path']).resolve()}")
            return 0
        if args.dataset_action == "rebuild" and args.schema_version != DATASET_VERSION:
            parser.error(f"--schema-version must be {DATASET_VERSION}")
        if args.dataset_action == "status":
            if args.department is None:
                try:
                    summary = summarize_dataset_status(args.raw_dir, args.analytics_dir)
                except (OSError, ValueError) as error:
                    print(f"Dataset status failed: {error}", file=sys.stderr)
                    return 1
                return _print_global_dataset_status(
                    summary, verbose=args.verbose,
                    include_raw=args.include_unmaterialized,
                )
            try:
                plan = plan_sync(args.raw_dir, args.analytics_dir, args.department)
                audit = audit_global_metadata(args.analytics_dir)
            except (OSError, ValueError) as error:
                print(f"Dataset status failed: {error}", file=sys.stderr)
                return 1
            print(f"Department: {plan.department}")
            print(f"Source archives: {len(plan.sources):,}")
            print(f"Changed archives: {len(plan.changed_files):,}")
            print(f"Removed archives: {len(plan.removed_files):,}")
            print(f"Affected years: {len(plan.affected_years):,}")
            print(f"Schema rebuild required: {plan.schema_rebuild_required}")
            _print_metadata_audit(audit, verbose=args.verbose)
            print(f"Status: {'up-to-date' if plan.no_op else 'pending'}")
            return 1 if plan.schema_rebuild_required or not audit.consistent else 0
        reporter = (
            NullProgressReporter() if args.no_progress else TqdmProgressReporter()
        )
        code, result = sync_dataset(
            raw_root=args.raw_dir,
            analytics_root=args.analytics_dir,
            reference_root=args.reference_dir,
            department=args.department,
            rebuild=args.dataset_action == "rebuild",
            progress=reporter,
        )
        print(f"Dataset status: {result['status']}")
        if "counts" in result:
            print(f"Hourly core rows: {result['counts']['core_rows']:,}")
            for table, count in result["counts"]["extension_rows"].items():
                print(f"{table} rows: {count:,}")
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
        return code
    _validate_years(args, parser)
    if args.action == "inspect" and args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.action == "inspect" and args.mode == "explore" and (
        not args.departments or len(args.departments) != 1
    ):
        parser.error("--mode explore requires exactly one --department")
    output_dir = args.output_dir or default_output_dir(args.category)
    session = requests.Session()
    session.headers["User-Agent"] = "weather-analysis-meteofrance/1.0"
    if args.action == "inspect":
        if args.mode == "explore":
            exit_code, summary = run_department_exploration(
                root=output_dir,
                report_dir=args.report_dir,
                department=args.departments[0].upper(),
                start_year=args.start_year,
                end_year=args.end_year,
                workers=args.workers,
            )
            print(f"Explored archives: {summary['archives']:,}")
            print(f"Stations: {summary.get('stations', 0):,}")
            print(f"Inspection errors: {summary['errors']:,}")
            print(f"Reports: {args.report_dir.resolve()}")
            return exit_code
        exit_code, summary = run_inspection(
            root=output_dir,
            report_dir=args.report_dir,
            mode=args.mode,
            departments=set(args.departments) if args.departments else None,
            start_year=args.start_year,
            end_year=args.end_year,
            workers=args.workers,
            session=session,
        )
        print(f"Selected archives: {summary['selected']:,}")
        print(f"Distinct schemas: {summary['schemas']:,}")
        print(f"Inspection errors: {summary['errors']:,}")
        print(f"Reports: {args.report_dir.resolve()}")
        return exit_code
    try:
        resources = select_resources(
            fetch_resources(session, args.category),
            set(args.departments) if args.departments else None,
            args.start_year,
            args.end_year,
        )
    except requests.RequestException as error:
        print(f"Catalog request failed: {error}", file=sys.stderr)
        return 1

    inspections = inspect_resources(session, resources, output_dir)
    _print_overview(args.category, resources, output_dir, inspections)
    if args.action == "status":
        incomplete = {FileStatus.MISSING, FileStatus.PARTIAL, FileStatus.INVALID}
        unavailable = [
            (name, item.remote_error)
            for name, item in inspections.items()
            if item.remote_error
        ]
        for name, error in unavailable:
            print(f"Remote verification unavailable for {name}: {error}", file=sys.stderr)
        return 1 if any(item.status in incomplete for item in inspections.values()) else 0

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = write_manifest(resources, output_dir, inspections)
    print(f"Manifest: {manifest_path.resolve()}")
    downloaded = 0
    failed = 0
    cleaned = 0
    download_results = {}
    actionable = {FileStatus.MISSING, FileStatus.PARTIAL, FileStatus.INVALID}
    for index, resource in enumerate(resources, start=1):
        inspection = inspections[resource.filename]
        if inspection.status not in actionable:
            if inspection.redundant_partial:
                partial_path(output_dir / resource.filename).unlink()
                cleaned += 1
            continue
        print(f"[{index}/{len(resources)}] Downloading {resource.filename}")
        try:
            result = download_resource(session, resource, output_dir)
            download_results[resource.filename] = result
            downloaded += 1
            if result.catalog_mismatch:
                print(
                    f"  Warning: catalog size {resource.expected_size} differs "
                    f"from server size {result.server_size}; accepted server size."
                )
        except (requests.RequestException, OSError, ValueError) as error:
            failed += 1
            print(f"  Failed: {error}", file=sys.stderr)

    write_manifest(resources, output_dir, inspections, download_results)
    final_inspections = inspect_resources(session, resources, output_dir)
    final_counts = Counter(item.status for item in final_inspections.values())
    print("Download summary:")
    print(f"complete: {final_counts[FileStatus.COMPLETE]:,}")
    print(f"downloaded: {downloaded:,}")
    print(f"failed: {failed:,}")
    print(f"redundant partials removed: {cleaned:,}")
    print(f"partial: {final_counts[FileStatus.PARTIAL]:,}")
    print(f"invalid: {final_counts[FileStatus.INVALID]:,}")
    incomplete = sum(final_counts[status] for status in actionable)
    return 1 if failed or incomplete else 0


def main() -> None:
    raise SystemExit(run())
