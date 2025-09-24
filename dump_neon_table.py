#!/usr/bin/env python3
"""
Exporta una tabla de Neon/Postgres a CSV (streaming) con filtro por fecha.

Uso típico:

  export DATABASE_URL='postgresql://USER:PASSWORD@HOST/DB?sslmode=require'
  python3 dump_neon_table.py \
      --table log_records \
      --date-col created_at \
      --since 2025-08-01 \
      --out exports/log_records_20250801_to_today.csv

Notas:
- Usa un cursor de servidor (stream) para no cargar todo en memoria.
- Si no pasas --until, toma hoy (now()::date) como límite superior exclusivo.
- Si la tabla no tiene la columna de fecha, puedes omitir --date-col para descargar todo.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
from typing import Optional

import psycopg


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def valid_ident(name: str) -> bool:
    return bool(IDENT_RE.match(name))


def split_table(t: str) -> tuple[str, str]:
    """Divide en (schema, table) asumiendo public si no viene schema."""
    if "." in t:
        s, n = t.split(".", 1)
        return s, n
    return "public", t


def main():
    ap = argparse.ArgumentParser(description="Exporta una tabla de Postgres a CSV (streaming)")
    ap.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="DSN de Postgres (usa DATABASE_URL si no se pasa)")
    ap.add_argument("--table", required=True, help="Tabla (opcionalmente schema.table). Ej: public.log_records o log_records")
    ap.add_argument("--columns", default="*", help="Columnas separadas por coma. Por defecto: *")
    ap.add_argument("--date-col", default=None, help="Nombre de columna de fecha/tiempo para filtrar (ej: created_at)")
    ap.add_argument("--since", required=False, help="Fecha inicio (YYYY-MM-DD) inclusive")
    ap.add_argument("--until", required=False, help="Fecha fin (YYYY-MM-DD) exclusiva; si no, hoy")
    ap.add_argument("--out", default="datos.csv", help="Ruta CSV de salida")
    ap.add_argument("--fetchsize", type=int, default=10000, help="Tamaño de lote (default 10000)")
    args = ap.parse_args()

    if not args.dsn:
        raise SystemExit("Falta DSN. Exporta DATABASE_URL o usa --dsn.")

    schema, table = split_table(args.table.strip())
    if not (valid_ident(schema) and valid_ident(table)):
        raise SystemExit("Nombre de schema/tabla inválido.")

    cols = "*"
    if args.columns and args.columns.strip() != "*":
        raw = [c.strip() for c in args.columns.split(",") if c.strip()]
        if not raw:
            raise SystemExit("--columns vacío tras parseo.")
        bad = [c for c in raw if not valid_ident(c)]
        if bad:
            raise SystemExit(f"Columnas inválidas: {', '.join(bad)}")
        cols = ", ".join([f'"{c}"' for c in raw])

    where = ""
    params: list = []
    if args.date_col:
        if not valid_ident(args.date_col):
            raise SystemExit("--date-col inválido")
        # Rango HALF-OPEN: [since, until)
        since = None
        until = None
        if args.since:
            since = dt.date.fromisoformat(args.since)
        if args.until:
            until = dt.date.fromisoformat(args.until)
        else:
            until = dt.date.today()
        if since is None:
            raise SystemExit("Falta --since cuando usas --date-col")
        where = f"WHERE \"{args.date_col}\" >= %s AND \"{args.date_col}\" < %s"
        params.extend([since.isoformat(), until.isoformat()])

    sql = f"SELECT {cols} FROM \"{schema}\".\"{table}\" {where}"

    # Ejecutar y stream a CSV
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor(name="stream") as cur:  # server-side cursor
            cur.itersize = max(1000, int(args.fetchsize))
            cur.execute(sql, params)
            headers_written = False
            with open(args.out, "w", newline="", encoding="utf-8") as f:
                w = None
                while True:
                    rows = cur.fetchmany(cur.itersize)
                    if not rows:
                        break
                    if not headers_written:
                        headers = [d[0] for d in cur.description]
                        w = csv.writer(f)
                        w.writerow(headers)
                        headers_written = True
                    w.writerows(rows)
    print(f"OK: exportado a {args.out}")


if __name__ == "__main__":
    main()

