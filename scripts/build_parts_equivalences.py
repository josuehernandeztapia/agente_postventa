#!/usr/bin/env python3
"""Consolidate spare parts equivalences from nationalization sources.

Outputs:
- data/parts_equivalences.json
- data/parts_equivalences_validation.csv
- migrations/20240924_part_equivalences.sql

Usage:
    python3 scripts/build_parts_equivalences.py
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / ".." / "refacciones_nacionalizacion"
SRC_CSV = SRC_DIR / "part_equivalences_complete.csv"
CATALOG_CSV = SRC_DIR / "Higer Spare Parts - Nacionalización.csv"
CODES_CSV = SRC_DIR / "codigos_partes_equivalences.csv"
COMPONENTS_CSV = SRC_DIR / "higer_nationalization_ssot.csv"
OUT_JSON = ROOT / "data" / "parts_equivalences.json"
OUT_VALIDATION = ROOT / "data" / "parts_equivalences_validation.csv"
MIGRATIONS_DIR = ROOT / "migrations"
DEFAULT_SQL_NAME = f"{datetime.utcnow():%Y%m%d}_part_equivalences.sql"


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def _split_component_refs(value: str) -> set[str]:
    refs: set[str] = set()
    if not value:
        return refs
    for token in value.replace("(", ";").replace(")", ";").split(";"):
        token_norm = token.strip()
        if token_norm:
            refs.add(token_norm.upper())
    return refs


def build_equivalences(args: argparse.Namespace) -> int:
    if not SRC_CSV.exists():
        print(f"Source CSV not found: {SRC_CSV}", file=sys.stderr)
        return 1

    raw_rows = _read_csv_rows(SRC_CSV)
    parts_catalog = _read_csv_rows(CATALOG_CSV) if CATALOG_CSV.exists() else []
    codes_catalog = _read_csv_rows(CODES_CSV) if CODES_CSV.exists() else []
    components_rows = _read_csv_rows(COMPONENTS_CSV) if COMPONENTS_CSV.exists() else []

    name_lookup: dict[str, str] = {}
    for row in parts_catalog:
        ref = (row.get("Internal Reference") or "").strip()
        if ref and ref not in name_lookup:
            name_lookup[ref.upper()] = (row.get("Name") or "").strip()

    for row in codes_catalog:
        ref = (row.get("Internal Reference") or "").strip()
        if ref and ref not in name_lookup:
            name_lookup[ref.upper()] = (row.get("Name") or "").strip()

    catalog_refs = {ref.upper() for ref in name_lookup.keys()}

    components_refs: set[str] = set()
    for row in components_rows:
        value = row.get("Parte original Higer/Toyota") or ""
        components_refs.update(_split_component_refs(value))

    equivalences: dict[str, dict] = {}
    validation_rows: list[dict[str, object]] = []

    provider_entries: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)

    for row in raw_rows:
        ref = (row.get("internal_ref") or "").strip()
        provider = (row.get("provider_name") or "").strip()
        part_number = (row.get("provider_part_number") or "").strip()
        description = (row.get("provider_description") or "").strip()
        if not ref or not provider:
            continue

        ref_upper = ref.upper()
        provider_type = "oem" if "toyota" in provider.lower() else "aftermarket"

        entry = equivalences.setdefault(
            ref_upper,
            {
                "name": name_lookup.get(ref_upper) or None,
                "equivalents": [],
                "sources": {
                    "catalog": ref_upper in catalog_refs,
                    "components": ref_upper in components_refs,
                },
            },
        )

        key = (provider, part_number, description, provider_type)
        if key not in provider_entries[ref_upper]:
            entry["equivalents"].append(
                {
                    "provider": provider,
                    "part_number": part_number or None,
                    "description": description or None,
                    "type": provider_type,
                }
            )
            provider_entries[ref_upper].add(key)

    # Sort equivalences and their entries
    sorted_refs = sorted(equivalences.keys())
    for ref in sorted_refs:
        entry = equivalences[ref]
        entry["equivalents"].sort(key=lambda x: (x.get("type", ""), x.get("provider", "")))

        has_oem = any(eq.get("type") == "oem" for eq in entry["equivalents"])
        has_aftermarket = any(eq.get("type") == "aftermarket" for eq in entry["equivalents"])

        validation_rows.append(
            {
                "internal_ref": ref,
                "name": entry.get("name"),
                "has_oem": has_oem,
                "has_aftermarket": has_aftermarket,
                "in_catalog": entry["sources"]["catalog"],
                "in_components": entry["sources"]["components"],
            }
        )

    # Write JSON
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump({ref: equivalences[ref] for ref in sorted_refs}, f, ensure_ascii=False, indent=2)

    # Write validation CSV
    with OUT_VALIDATION.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["internal_ref", "name", "has_oem", "has_aftermarket", "in_catalog", "in_components"])
        writer.writeheader()
        for row in validation_rows:
            writer.writerow(row)

    # Write migration SQL
    MIGRATIONS_DIR.mkdir(exist_ok=True)
    sql_path = MIGRATIONS_DIR / args.sql_filename
    with sql_path.open("w", encoding="utf-8") as f:
        f.write("-- Auto-generated by scripts/build_parts_equivalences.py\n")
        for ref in sorted_refs:
            for provider, part_number, description, provider_type in sorted(provider_entries[ref]):
                desc = description.replace("'", "''") if description else ""
                part_num = part_number.replace("'", "''") if part_number else ""
                provider_clean = provider.replace("'", "''")
                f.write(
                    "INSERT INTO part_equivalences (spare_part_id, provider_name, provider_part_number, provider_description)\n"
                )
                f.write(
                    (
                        "SELECT refaccion_id, '{prov}', '{part}', '{desc}' FROM spare_parts WHERE referencia_interna = '{ref}'\n"
                        "ON CONFLICT DO NOTHING;\n"
                    ).format(
                        prov=provider_clean,
                        part=part_num,
                        desc=desc,
                        ref=ref,
                    )
                )
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_VALIDATION}")
    print(f"Wrote {sql_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Consolidate spare part equivalences")
    parser.add_argument(
        "--sql-filename",
        default=DEFAULT_SQL_NAME,
        help="Output filename for migration SQL (default: %(default)s)",
    )
    args = parser.parse_args()
    return build_equivalences(args)


if __name__ == "__main__":
    sys.exit(main())
