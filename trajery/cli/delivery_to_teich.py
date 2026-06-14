"""delivery → Teich trace 流水线 CLI 入口 / CLI for the delivery → Teich trace pipeline.

中文：
- 命令行薄包装，解析参数后调用 ``trajery.pipeline.process``。
- 日志输出到 stderr；报表写入 ``report.json``（默认 ``<output_dir>/report.json``）。
- 退出码见 USER_GUIDE §11：0 成功、1 strict-empty、2 input_dir 无效、3 teich 未安装。

English:
- Thin CLI wrapper around ``process()``; logs to stderr.
- Exit codes: 0 ok, 1 strict-empty, 2 bad input_dir, 3 teich missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trajery.export import check_teich_available
from trajery.pipeline import process
from trajery.report import to_markdown_report


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口 / Command-line entry point for delivery_to_teich.

    中文：解析参数 → 校验 teich → 调用 ``process()`` → 写 report → 返回退出码。

    English: Parses args, validates teich, runs pipeline, writes report.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 0 success, 1 strict-empty, 2 bad input_dir, 3 teich missing.
    """
    parser = argparse.ArgumentParser(
        description="Convert delivery logs to Teich Codex traces."
    )

    # --- 位置参数 / Positional arguments ---
    parser.add_argument(
        "input_dir", type=Path, help="Directory with *.jsonl, *.jsonl.gz, or *.tar.gz delivery logs"
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        default=None,
        help="Output directory (default: <input_dir>/output)",
    )

    # --- 限流 / Rate limits (debug) ---
    parser.add_argument("--limit-files", type=int, default=None)
    parser.add_argument("--limit-records", type=int, default=None)

    # --- 流水线开关 / Pipeline toggles ---
    parser.add_argument("--no-filter", action="store_true", help="Skip R1-R7 filter")
    parser.add_argument("--no-dedup", action="store_true", help="Skip session_id dedup")
    parser.add_argument(
        "--no-dropped",
        action="store_true",
        help="Skip writing the dropped/ tree; stats and drop_reasons in report are still collected",
    )
    parser.add_argument(
        "--keep-unwrapped",
        action="store_true",
        help="Write unwrapped openai_responses JSONL",
    )

    # --- 输出与报表 / Output and reporting ---
    parser.add_argument(
        "--emit-training-rows",
        action="store_true",
        help="Also emit training_rows.jsonl via teich",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write JSON stats report to this path (default: <output_dir>/report.json)",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Skip writing the stats report",
    )
    parser.add_argument(
        "--report-md",
        type=Path,
        default=None,
        help="Write Markdown report to this path (default: <output_dir>/report.md)",
    )
    parser.add_argument(
        "--no-report-md",
        action="store_true",
        help="Skip writing the Markdown report",
    )
    parser.add_argument(
        "--report-include-valid",
        action="store_true",
        help=(
            "Include valid trace filenames in report.json teich_trace_results "
            "(default: empty; incomplete/invalid are not listed)"
        ),
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Log scan progress every N records (0=off, default 100)",
    )
    parser.add_argument(
        "--clean-output",
        action="store_true",
        help="Clear traces/, incomplete/, invalid/, dropped/, unwrapped/ before running",
    )

    # --- Teich / Validation ---
    parser.add_argument(
        "--skip-teich-validate",
        action="store_true",
        help="Skip Teich validation (for environments without teich installed)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel worker count for scan phase only (1=serial, default)",
    )
    parser.add_argument(
        "--strict-empty", action="store_true", help="Exit 1 if teich_valid == 0"
    )
    args = parser.parse_args(argv)

    if not args.input_dir.is_dir():
        print(f"ERROR: input_dir is not a directory: {args.input_dir}", file=sys.stderr)
        return 2

    # Fail-fast: teich required unless --skip-teich-validate (exit code 3).
    if not args.skip_teich_validate:
        teich_ok, teich_err = check_teich_available()
        if not teich_ok:
            print(
                "ERROR: teich is required for trace validation. "
                f"Install teich or pass --skip-teich-validate. ({teich_err})",
                file=sys.stderr,
            )
            return 3

    output_dir = (
        args.output_dir if args.output_dir is not None else args.input_dir / "output"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    if args.output_dir is None and not args.quiet:
        log(f"output_dir (default): {output_dir}")

    if not args.quiet:
        log("=== delivery_to_teich: starting ===")

    stats = process(
        input_dir=args.input_dir,
        output_dir=output_dir,
        apply_filter=not args.no_filter,
        dedup=not args.no_dedup,
        keep_unwrapped=args.keep_unwrapped,
        write_dropped=not args.no_dropped,
        emit_training_rows=args.emit_training_rows,
        limit_files=args.limit_files,
        limit_records=args.limit_records,
        progress_every=args.progress_every,
        clean_output=args.clean_output,
        skip_teich_validate=args.skip_teich_validate,
        workers=args.workers,
        log=None if args.quiet else log,
    )

    # Write report.json via stats.to_report().
    if not args.no_report:
        report_path = (
            args.report if args.report is not None else output_dir / "report.json"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_data = stats.to_report(
            input_dir=args.input_dir,
            output_dir=output_dir,
            include_valid_files=args.report_include_valid,
        )
        report_path.write_text(
            json.dumps(report_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if not args.quiet:
            log(f"report: {report_path}")

        if not args.no_report_md:
            report_md_path = (
                args.report_md
                if args.report_md is not None
                else output_dir / "report.md"
            )
            report_md_path.parent.mkdir(parents=True, exist_ok=True)
            report_md_path.write_text(
                to_markdown_report(report_data),
                encoding="utf-8",
            )
            if not args.quiet:
                log(f"report: {report_md_path}")

    if args.strict_empty and stats.teich_valid == 0:
        return 1
    return 0
