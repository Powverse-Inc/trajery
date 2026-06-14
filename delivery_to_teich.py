#!/usr/bin/env python3
"""Delivery log → Teich Codex trace pipeline.

Usage
-----
    python delivery_to_teich.py <input_dir> [<output_dir>] [options...]

See README.md and USER_GUIDE.md for full documentation.
"""

from trajery.cli.delivery_to_teich import main

if __name__ == "__main__":
    raise SystemExit(main())
