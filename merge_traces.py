#!/usr/bin/env python3
"""Merge multi-day delivery_to_teich trace outputs.

Usage
-----
    python merge_traces.py <input_root> [--output-dir DIR] [options...]

See USER_GUIDE.md §12 for full documentation.
"""

from trajery.cli.merge_traces import main

if __name__ == "__main__":
    raise SystemExit(main())
