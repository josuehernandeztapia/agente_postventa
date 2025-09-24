import os
import sys
import csv
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from app.vision_openai import parse_postventa_form  # type: ignore


def to_data_url(path: Path) -> str:
    import base64
    ext = path.suffix.lower()
    ctype = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp'}.get(ext.strip('.'),'image/jpeg')
    return f"data:{ctype};base64,{base64.b64encode(path.read_bytes()).decode('utf-8')}"


def scan_images(root: Path):
    for p in sorted(root.rglob('*')):
        if p.is_file() and p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}:
            yield p


def load_env(base: Path):
    if load_dotenv:
        try:
            load_dotenv(dotenv_path=base/'.env', override=False)
            load_dotenv(dotenv_path=base/'secrets.local.txt', override=True)
        except Exception:
            pass
    # Fallback regex en secrets.local.txt
    try:
        txt = (base/'secrets.local.txt').read_text(errors='ignore')
        if 'OPENAI_API_KEY' not in os.environ:
            import re
            m = re.search(r"\bsk-[\w-]{20,}\b", txt)
            if m:
                os.environ['OPENAI_API_KEY'] = m.group(0)
    except Exception:
        pass


def main():
    base = Path(__file__).parent
    load_env(base)
    src = Path('../WhatsApp Chat - Higer Postventa AGS 2').resolve()
    # soportar ruta alternativa si se pasa como arg
    if len(sys.argv) > 1:
        src = Path(sys.argv[1]).expanduser().resolve()
    out_csv = base/'logs/forms_batch.csv'
    out_jsonl = base/'logs/forms_batch.jsonl'
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    found = 0
    for img in scan_images(src):
        try:
            du = to_data_url(img)
            res = parse_postventa_form(du)
            if isinstance(res, dict) and res.get('form_type') in {'postventa','garantia'}:
                found += 1
                rows.append({
                    'file': str(img),
                    'form_type': res.get('form_type'),
                    'completo': res.get('completo'),
                    'faltantes': '|'.join(res.get('faltantes') or []),
                    'cliente': res.get('cliente'),
                    'contacto': res.get('contacto'),
                    'telefono': res.get('telefono'),
                    'email': res.get('email'),
                    'unidad': res.get('unidad'),
                    'marca': res.get('marca'),
                    'modelo': res.get('modelo'),
                    'vin': res.get('vin'),
                    'placa': res.get('placa'),
                    'kilometraje_km': res.get('kilometraje_km'),
                    'fecha': res.get('fecha'),
                    'reporte_falla': res.get('reporte_falla') or res.get('sintomas'),
                    'acciones': res.get('acciones'),
                    'pieza_danada': res.get('pieza_danada'),
                    'pieza_solicitada_garantia': res.get('pieza_solicitada_garantia'),
                    'piezas': '|'.join(res.get('piezas') or []),
                })
                with out_jsonl.open('a', encoding='utf-8') as jf:
                    import json
                    jf.write(json.dumps({'file': str(img), **res}, ensure_ascii=False) + '\n')
        except Exception:
            continue

    with out_csv.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['file','form_type','completo','faltantes','cliente','contacto','telefono','email','unidad','marca','modelo','vin','placa','kilometraje_km','fecha','reporte_falla','acciones','pieza_danada','pieza_solicitada_garantia','piezas'])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f'OK: {found} formularios encontrados. CSV -> {out_csv}')


if __name__ == '__main__':
    main()
