import os
import json
import sys
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


FILES_CANDIDATES = [
    'logs/forms_samples_master.csv',
    'logs/forms_samples_enriched.csv',
    'logs/forms_samples_min.csv',
    'logs/vision_batch.csv',
    'logs/chat_text.csv',
    'logs/vision_chat_join.csv',
    'logs/events.csv',
    'logs/sources.csv',
]


def ensure_events_exports(base: Path):
    # Intenta exportar events/sources si no existen aún
    ev = base / 'logs' / 'events.csv'
    src = base / 'logs' / 'sources.csv'
    if ev.exists() and src.exists():
        return
    try:
        from app import storage  # type: ignore
        storage.export_csv()
    except Exception:
        pass


def write_readme(base: Path, out_dir: Path):
    text = (
        "Análisis — Bundle de CSV\n\n"
        "Incluye datos consolidados y listos para join.\n\n"
        "Archivos\n"
        "- logs/forms_samples_master.csv: Maestro consolidado (imagen + chat).\n"
        "  columnas: file, form_type, vin, placa, kilometraje_km, fecha, reporte_falla, pieza_danada, pieza_solicitada_garantia, status\n"
        "- logs/forms_samples_enriched.csv: Campos extraídos por ventana de chat + LLM/heurística.\n"
        "- logs/forms_samples_min.csv: Extracción mínima por imagen (Vision).\n"
        "- logs/vision_batch.csv: OCR/Visión por imagen.\n"
        "  columnas: file, evidence_type, vin, plate, odo_km, delivered_at, notes\n"
        "- logs/chat_text.csv: Chat WhatsApp parseado.\n"
        "  columnas: ts, author, text, attachment, category, severity, problem, model, oem, oil_code\n"
        "- logs/vision_chat_join.csv: Join imagen+texto por attachment (stem).\n"
        "- logs/events.csv, logs/sources.csv: Eventos API y fuentes citadas (si aplica).\n\n"
        "Claves de join\n"
        "- Por imagen: stem(file) ↔ stem(attachment).\n"
        "- Por vehículo: vin, placa, kilometraje_km, fecha.\n"
        "- Por conversación: author (contacto) + tiempos (ts).\n\n"
        "Notas\n"
        "- status=ai_flexible indica extracción con heurística/LLM por ventana; valida antes de decisiones críticas.\n"
        "- Si agregas más datos, vuelve a ejecutar forms-enrich y forms-master.\n"
    )
    out_path = out_dir / 'README_ANALYSIS.md'
    out_path.write_text(text, encoding='utf-8')
    return out_path


def main():
    base = Path(__file__).parent
    ensure_events_exports(base)
    out_dir = base / 'exports'
    out_dir.mkdir(parents=True, exist_ok=True)
    readme = write_readme(base, out_dir)
    bundle = out_dir / 'analysis_bundle.zip'
    with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        # README siempre
        z.write(readme, arcname='README_ANALYSIS.md')
        for rel in FILES_CANDIDATES:
            p = base / rel
            if p.exists():
                z.write(p, arcname=rel)
    files = []
    try:
        with zipfile.ZipFile(bundle, 'r') as z:
            files = z.namelist()
    except Exception:
        pass
    print(json.dumps({
        'bundle': str(bundle),
        'files': files,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
