import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def read_csv(p: Path):
    if not p.exists():
        return []
    with p.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def main():
    base = Path(__file__).parent
    forms = read_csv(base / 'logs' / 'forms_samples_master.csv')
    chat = read_csv(base / 'logs' / 'chat_text.csv')
    if not forms:
        print('No forms_samples_master.csv. Ejecuta consolidate_forms_master.py primero.')
        return
    if not chat:
        print('No chat_text.csv. Ejecuta batch_chat_text.py primero.')
        return
    # map attachment stem -> author
    stem_author = {}
    for r in chat:
        att = (r.get('attachment') or '').strip()
        if not att:
            continue
        stem = Path(att).stem
        author = (r.get('author') or '').strip() or 'contact-desconocido'
        stem_author.setdefault(stem, author)

    imported = 0
    try:
        from app import storage  # type: ignore
    except Exception:
        storage = None  # type: ignore
    for f in forms:
        stem = Path(f.get('file') or '').stem
        contact = stem_author.get(stem) or 'contact-desconocido'
        if not storage:
            continue
        case = storage.get_or_create_case(contact)
        patch = {}
        if f.get('vin'): patch['vin'] = f['vin']
        if f.get('placa'): patch['plate'] = f['placa']
        if f.get('kilometraje_km'): patch['odo_km'] = f['kilometraje_km']
        if f.get('fecha'): patch['delivered_at'] = f['fecha']
        if f.get('form_type'): patch['form_type'] = f['form_type']
        if f.get('reporte_falla'): patch['reporte_falla'] = f['reporte_falla']
        if f.get('pieza_danada'): patch['pieza_danada'] = f['pieza_danada']
        if f.get('pieza_solicitada_garantia'): patch['pieza_solicitada'] = f['pieza_solicitada_garantia']
        # marca formulario como proporcionado
        storage.update_case(contact, patch)
        storage.mark_provided(contact, ['formulario_postventa'])
        try:
            storage.log_event('forms_import', {'contact': contact, 'file': f.get('file'), 'patch': patch})
        except Exception:
            pass
        imported += 1
    print('OK imported:', imported)


if __name__ == '__main__':
    main()
