#!/usr/bin/env python3
"""
Descarga una tabla completa de la Neon Data API (PostgREST) a CSV.

Uso rápido:
  python3 descarga_geotab.py \
    --url https://<host>/<db>/rest/v1 \
    --table log_records \
    --token "$NEON_API_KEY" \
    --out datos_geotab.csv

Notas:
- Requiere un token de Neon Data API (JWT) con acceso a la rama/base de datos.
- No necesita dependencias externas; usa sólo la librería estándar.
- Hace paginación mediante cabeceras Range/Content-Range.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from typing import Dict, List
from urllib.parse import urljoin
import urllib.request as rq


def fetch_page(endpoint: str, token: str, start: int, end: int) -> tuple[List[Dict], int | None]:
    req = rq.Request(endpoint)
    req.add_header("Accept", "application/json")
    req.add_header("Range-Unit", "items")
    req.add_header("Range", f"{start}-{end}")
    req.add_header("Prefer", "count=exact")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with rq.urlopen(req) as resp:
        body = resp.read().decode("utf-8")
        try:
            rows = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"Respuesta no es JSON válida en rango {start}-{end}")
        content_range = resp.headers.get("Content-Range") or resp.headers.get("content-range")
        total = None
        if content_range:
            # Formato: items start-end/total
            try:
                _, span = content_range.split(" ", 1)
                span_main = span.split("/")
                total = int(span_main[1]) if len(span_main) == 2 and span_main[1].isdigit() else None
            except Exception:
                total = None
        return rows, total


def unify_headers(rows: List[Dict]) -> List[str]:
    keys = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys


def main():
    ap = argparse.ArgumentParser(description="Descargar tabla de Neon Data API a CSV")
    ap.add_argument("--url", required=True, help="Base URL de Data API (ej: https://host/neondb/rest/v1)")
    ap.add_argument("--table", required=True, help="Nombre de la tabla (ej: log_records)")
    ap.add_argument("--token", default=os.getenv("NEON_DATA_API_TOKEN") or os.getenv("NEON_API_KEY") or os.getenv("NEON_TOKEN"), help="Token JWT (Bearer) de Neon Data API")
    ap.add_argument("--out", default="datos_geotab.csv", help="Ruta de salida CSV")
    ap.add_argument("--pagesize", type=int, default=1000, help="Tamaño de página (por defecto 1000)")
    args = ap.parse_args()

    if not args.token:
        raise SystemExit("Falta token. Pasa --token o exporta NEON_DATA_API_TOKEN/NEON_API_KEY.")

    base = args.url.rstrip("/") + "/"
    endpoint = urljoin(base, args.table)
    # asegurar select=*
    if "?" in endpoint:
        endpoint += "&select=*"
    else:
        endpoint += "?select=*"

    all_rows: List[Dict] = []
    start = 0
    total = None
    page = 0
    print(f"Descargando desde: {endpoint}")
    while True:
        end = start + args.pagesize - 1
        rows, t = fetch_page(endpoint, args.token, start, end)
        if total is None and t is not None:
            total = t
        all_rows.extend(rows)
        got = len(rows)
        page += 1
        print(f"Página {page}: filas={got} (acum={len(all_rows)})")
        if got < args.pagesize:
            break
        start = end + 1
        if total is not None and start >= total:
            break

    if not all_rows:
        # aún así escribir CSV vacío con cabecera mínima
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            f.write("\n")
        print(f"Sin filas. CSV vacío creado: {args.out}")
        return

    headers = unify_headers(all_rows)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"¡Listo! Guardado CSV: {args.out} (filas={len(all_rows)})")


if __name__ == "__main__":
    main()

