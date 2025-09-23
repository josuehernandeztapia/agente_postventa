#!/usr/bin/env python3
"""Generate a daily status report for operator review."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from app import storage


def build_report() -> dict:
    cases = storage.list_cases()
    total = len(cases)
    open_cases = [c for c in cases if (c.get('status') or '').lower() not in {'solved', 'rejected'}]
    severity_counter = Counter((c.get('severity') or 'normal') for c in cases)
    missing_counter = Counter(len(c.get('missing') or []) for c in cases)
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'total_cases': total,
        'open_cases': len(open_cases),
        'severity_breakdown': dict(severity_counter),
        'missing_counts': dict(missing_counter),
        'cases': cases,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate daily report from case state")
    parser.add_argument(
        '--out',
        default=None,
        help='Optional output path. Defaults to reports/daily_report_<date>.json',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report()
    out_path = args.out
    if not out_path:
        reports_dir = Path('reports')
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d')
        out_path = reports_dir / f'daily_report_{stamp}.json'
    else:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"Daily report saved to {out_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
