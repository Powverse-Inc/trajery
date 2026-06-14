#!/usr/bin/env python3
"""Prove filter_traj_multi_plat.py stays equivalent to the vendor filter.

Compares ``evaluate(extract(...))`` results on every JSON example shipped in
``traj_procurement_vendor_filter/examples/``.

Usage (from powverse/trajery/):

    python run_tests_against_vendor.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR_FILTER = HERE.parent / "traj_procurement_vendor_filter" / "multi_platform_traj_filter.py"
VENDOR_EXAMPLES = HERE.parent / "traj_procurement_vendor_filter" / "examples"
LOCAL_FILTER = HERE / "filter_traj_multi_plat.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    if not VENDOR_FILTER.is_file():
        print(f"ERROR: vendor filter not found: {VENDOR_FILTER}", file=sys.stderr)
        return 2
    if not VENDOR_EXAMPLES.is_dir():
        print(f"ERROR: vendor examples not found: {VENDOR_EXAMPLES}", file=sys.stderr)
        return 2

    vendor = _load_module(VENDOR_FILTER, "_vendor_mptf")
    local = _load_module(LOCAL_FILTER, "_local_mptf")

    mismatches: list[str] = []
    checked = 0

    for example_path in sorted(VENDOR_EXAMPLES.glob("*.json")):
        payload = json.loads(example_path.read_text(encoding="utf-8"))
        checked += 1

        vendor_traj = vendor.extract(payload)
        local_traj = local.extract(payload)
        vendor_pass, vendor_reasons = vendor.evaluate(vendor_traj)
        local_pass, local_reasons = local.evaluate(local_traj)

        if vendor_pass != local_pass or sorted(vendor_reasons) != sorted(local_reasons):
            mismatches.append(
                f"{example_path.name}: vendor=({vendor_pass}, {vendor_reasons}) "
                f"local=({local_pass}, {local_reasons})"
            )

        vendor_sid = vendor.compute_session_id(vendor_traj)
        local_sid = local.compute_session_id(local_traj)
        if vendor_sid != local_sid:
            mismatches.append(
                f"{example_path.name}: session_id vendor={vendor_sid!r} local={local_sid!r}"
            )

    print(f"Checked {checked} vendor examples against {LOCAL_FILTER.name}")

    if mismatches:
        print("MISMATCHES:", file=sys.stderr)
        for line in mismatches:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print("OK: local filter matches vendor filter on all examples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
