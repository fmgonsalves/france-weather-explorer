from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import requests

from .archive_inspection import run_inspection
from .analytical_dataset import DATASET_VERSION, plan_sync, sync_dataset
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weather-analysis")
    commands = parser.add_subparsers(dest="service", required=True)
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
        command.add_argument("--department", required=True)
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


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action == "dataset":
        if args.dataset_action == "rebuild" and args.schema_version != DATASET_VERSION:
            parser.error(f"--schema-version must be {DATASET_VERSION}")
        if args.dataset_action == "status":
            try:
                plan = plan_sync(args.raw_dir, args.analytics_dir, args.department)
            except (OSError, ValueError) as error:
                print(f"Dataset status failed: {error}", file=sys.stderr)
                return 1
            print(f"Department: {plan.department}")
            print(f"Source archives: {len(plan.sources):,}")
            print(f"Changed archives: {len(plan.changed_files):,}")
            print(f"Removed archives: {len(plan.removed_files):,}")
            print(f"Affected years: {len(plan.affected_years):,}")
            print(f"Schema rebuild required: {plan.schema_rebuild_required}")
            print(f"Status: {'up-to-date' if plan.no_op else 'pending'}")
            return 1 if plan.schema_rebuild_required else 0
        code, result = sync_dataset(
            raw_root=args.raw_dir,
            analytics_root=args.analytics_dir,
            reference_root=args.reference_dir,
            department=args.department,
            rebuild=args.dataset_action == "rebuild",
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
