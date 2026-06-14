"""CLI for merge_traces."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trajery.merge.traces import merge_traces


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge multi-day delivery_to_teich traces with quality filter and cross-day dedup."
    )
    parser.add_argument(
        "input_root",
        type=Path,
        help="Root directory containing per-day output folders (each with traces/)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Merged output directory (default: <input_root>/merged)",
    )
    parser.add_argument(
        "--mode",
        choices=("copy", "hardlink"),
        default="copy",
        help="How to materialize merged traces (default: copy)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build manifest only; do not copy traces",
    )
    parser.add_argument(
        "--include-days",
        type=str,
        default=None,
        help="Comma-separated day folder names to include (default: all with report.json)",
    )
    parser.add_argument(
        "--verbose-manifest",
        action="store_true",
        help="Include full discarded/dedup_losers paths in merge_manifest.json",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=500,
        help="Log progress every N traces (0=off, default 500)",
    )
    parser.add_argument(
        "--report-absolute-paths",
        action="store_true",
        help="Write absolute paths in merge_manifest.json (default: relative to cwd)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not args.input_root.is_dir():
        print(f"ERROR: input_root is not a directory: {args.input_root}", file=sys.stderr)
        return 2

    output_dir = args.output_dir if args.output_dir is not None else args.input_root / "merged"
    include_days = None
    if args.include_days:
        include_days = [d.strip() for d in args.include_days.split(",") if d.strip()]

    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        merge_traces(
            args.input_root,
            output_dir,
            mode=args.mode,
            dry_run=args.dry_run,
            include_days=include_days,
            verbose_manifest=args.verbose_manifest,
            progress_every=args.progress_every,
            report_absolute_paths=args.report_absolute_paths,
            log=None if args.quiet else log,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0
