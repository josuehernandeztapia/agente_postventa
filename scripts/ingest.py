#!/usr/bin/env python3
"""CLI wrapper para la ingesta unificada."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from app.cli import run_ingesta


if __name__ == "__main__":
    sys.exit(run_ingesta(sys.argv[1:]))
