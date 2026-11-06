#!/usr/bin/env python3
"""Build or refresh the offline TREC library.

Usage:
    python tools/build_library.py
    python tools/build_library.py --cache-dir .cache

The parser lives in ``trec_explorer.ingest`` so the desktop app's background
updater can reuse exactly the same fetch and parse logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trec_explorer.ingest import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
