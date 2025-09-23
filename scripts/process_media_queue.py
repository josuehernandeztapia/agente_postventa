#!/usr/bin/env python3
"""Worker para procesar medios pendientes cuando MEDIA_PROCESSING=queue."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from app import storage
from main import _process_media_items


class MediaQueueWorker:
    def __init__(self, verbose: bool = False, dry_run: bool = False):
        self.verbose = verbose
        self.dry_run = dry_run
        self.queue_path = Path(storage.MEDIA_QUEUE_FILE)

    def _log(self, message: str):
        if self.verbose:
            print(f"[media-worker] {message}")

    def _load_processing_file(self) -> List[Dict[str, Any]]:
        if not self.queue_path.exists():
            return []
        processing_path = self.queue_path.with_suffix(self.queue_path.suffix + ".processing")
        try:
            self.queue_path.replace(processing_path)
        except FileNotFoundError:
            return []
        entries: List[Dict[str, Any]] = []
        with processing_path.open('r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    self._log(f"Entrada inválida ignorada: {line}")
        return entries, processing_path

    def _read_append_log(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        if not self.queue_path.exists():
            return entries
        with self.queue_path.open('r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    self._log(f"Entrada nueva inválida ignorada: {line}")
        return entries

    def _save_queue(self, entries: List[Dict[str, Any]]):
        if self.dry_run:
            self._log(f"Dry-run: no se guarda cola ({len(entries)} pendientes)")
            return
        if not entries:
            try:
                if self.queue_path.exists():
                    self.queue_path.unlink()
            except FileNotFoundError:
                pass
            return
        os.makedirs(self.queue_path.parent, exist_ok=True)
        with self.queue_path.open('w', encoding='utf-8') as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _process_entry(self, entry: Dict[str, Any]) -> bool:
        contact = entry.get('contact')
        media = entry.get('media') or {}
        url = media.get('url') if isinstance(media, dict) else None
        if not contact or not url:
            self._log("Entrada sin contacto o URL, se descarta")
            return True
        try:
            case = storage.get_or_create_case(str(contact))
        except Exception as exc:
            self._log(f"No se pudo obtener caso para {contact}: {exc}")
            return False
        category = ((case or {}).get('categories') or ['general'])[-1]
        neon_case_id = (case or {}).get('db_case_id')

        try:
            res = _process_media_items(str(contact), [media], category, neon_case_id, case)
        except Exception as exc:
            self._log(f"Error procesando {url}: {exc}")
            return False

        provided = res.get('provided_items') or []
        if provided and not self.dry_run:
            storage.mark_provided(str(contact), list(dict.fromkeys(provided)))

        case_updated = res.get('case')
        if case_updated:
            case = case_updated

        if not self.dry_run:
            try:
                storage.log_event(
                    kind="media_processed",
                    payload={
                        "contact": contact,
                        "url": url,
                        "provided": provided,
                        "category": category,
                    },
                )
            except Exception:
                pass
        self._log(f"Procesado {url} para {contact} (provided={provided})")
        return True

    def process_once(self) -> bool:
        bundle = self._load_processing_file()
        if not bundle:
            return False
        entries, processing_path = bundle
        if not entries:
            processing_path.unlink(missing_ok=True)
            return False

        remaining: List[Dict[str, Any]] = []
        processed = 0
        for entry in entries:
            ok = self._process_entry(entry)
            if ok:
                processed += 1
            else:
                remaining.append(entry)

        # merge with new entries that arrived during processing
        new_entries = self._read_append_log()
        processing_path.unlink(missing_ok=True)
        remaining.extend(new_entries)

        self._save_queue(remaining)
        self._log(f"Procesados {processed} adjuntos, {len(remaining)} pendientes")
        return processed > 0 or bool(remaining)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Procesa la cola de medios pendientes")
    parser.add_argument("--loop", action="store_true", help="Ejecuta en ciclo infinito")
    parser.add_argument("--sleep", type=float, default=5.0, help="Segundos entre ciclos en modo loop")
    parser.add_argument("--dry-run", action="store_true", help="Simula procesamiento sin cambios")
    parser.add_argument("--verbose", action="store_true", help="Imprime detalles de progreso")
    return parser.parse_args(argv)


def main(argv: List[str] | None = None):
    args = parse_args(argv or sys.argv[1:])
    load_dotenv(override=False)

    worker = MediaQueueWorker(verbose=args.verbose, dry_run=args.dry_run)
    if not args.loop:
        worker.process_once()
        return

    worker._log("Iniciando worker en modo loop")
    try:
        while True:
            processed = worker.process_once()
            time.sleep(args.sleep if args.sleep > 0 else 1.0)
            if not processed and not worker.verbose:
                # evita saturar logs cuando no hay trabajo
                worker._log("Esperando nuevos adjuntos…")
    except KeyboardInterrupt:
        worker._log("Worker detenido por usuario")


if __name__ == "__main__":
    main()
