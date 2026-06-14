#!/usr/bin/env python3
"""Merge multi-day delivery_to_teich report.json outputs.

Usage
-----
    python merge_reports.py <input_root> [--manifest PATH] [options...]

See USER_GUIDE.md §12 for full documentation.
"""

from trajery.cli.merge_reports import main

if __name__ == "__main__":
    raise SystemExit(main())
