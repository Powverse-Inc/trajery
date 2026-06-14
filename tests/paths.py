"""Shared paths for tests."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DATA = ROOT / "fixtures" / "data"
