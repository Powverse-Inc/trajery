"""CLI for merge_reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trajery.merge.reports import merge_reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge multi-day delivery_to_teich report.json files."
    )
    parser.add_argument(
        "input_root",
        type=Path,
        help="Root directory containing per-day output folders (each with report.json)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Merged report output directory (default: <input_root>/merged)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to merge_manifest.json from merge_traces (for quality_filter stats)",
    )
    parser.add_argument(
        "--include-days",
        type=str,
        default=None,
        help="Comma-separated day folder names to include (default: all with report.json)",
    )
    parser.add_argument(
        "--report-absolute-paths",
        action="store_true",
        help="Write absolute paths in merged report.json (default: relative to cwd)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not args.input_root.is_dir():
        print(f"ERROR: input_root is not a directory: {args.input_root}", file=sys.stderr)
        return 2

    output_dir = args.output_dir if args.output_dir is not None else args.input_root / "merged"
    manifest_path = args.manifest
    if manifest_path is None and (output_dir / "merge_manifest.json").is_file():
        manifest_path = output_dir / "merge_manifest.json"

    include_days = None
    if args.include_days:
        include_days = [d.strip() for d in args.include_days.split(",") if d.strip()]

    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        merge_reports(
            args.input_root,
            output_dir,
            manifest_path=manifest_path,
            include_days=include_days,
            report_absolute_paths=args.report_absolute_paths,
            log=None if args.quiet else log,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    return 0
