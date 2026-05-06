"""Backward-compatible entrypoint for macro valuation score builder."""

from __future__ import annotations

from pathlib import Path
import sys

THIS_DIR = Path(__file__).resolve().parent
ROOT_DIR = THIS_DIR.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from leon.calc.build_macro_valuation_score import _run


if __name__ == "__main__":
    _run()

