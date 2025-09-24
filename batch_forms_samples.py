import base64, csv, json, sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from app.vision_openai import parse_postventa_form  # type: ignore


def load_stems(arg: str) -> List[str]:
    p = Path(arg)
    if p.exists():
        return [ln.strip() for ln in p.read_text(encoding='utf-8').splitlines() if ln.strip()]
    # comma-separated
    return [s.strip() for s in arg.split(',') if s.strip()]


def find_image(base: Path, stem: str) -> Path | None:
    for ext in ['.jpg', '.jpeg', '.png', '.webp']:
        p = base / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def main():
    if len(sys.argv) < 2:
        print('Uso: python3 batch_forms_samples.py <stems-file|comma-list>')
        sys.exit(1)
    stems = load_stems(sys.argv[1])
    base_dir = (Path(__file__).resolve().parent.parent / 'WhatsApp Chat - Higer Postventa AGS 2').resolve()
    out_csv = Path('rag-pinecone/logs/forms_samples_min.csv')
    out_jsonl = Path('rag-pinecone/logs/forms_samples_min.jsonl')
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    with out_jsonl.open('w', encoding='utf-8') as jf:
        for stem in stems:
            img = find_image(base_dir, stem)
            row = {'file': str(img) if img else stem}
            if not img:
                row['status'] = 'missing'
                rows.append(row)
                continue
            b64 = base64.b64encode(img.read_bytes()).decode('utf-8')
            du = f'data:image/jpeg;base64,{b64}'
            res = parse_postventa_form(du) or {}
            form_type = res.get('form_type') or ''
            row.update({
                'form_type': form_type,
                'vin': res.get('vin') or '',
                'placa': res.get('placa') or '',
                'kilometraje_km': res.get('kilometraje_km') or '',
                'fecha': res.get('fecha') or '',
                'reporte_falla': res.get('reporte_falla') or res.get('sintomas') or '',
                'pieza_danada': res.get('pieza_danada') or '',
                'pieza_solicitada_garantia': res.get('pieza_solicitada_garantia') or '',
                'status': 'ai_parsed' if form_type in {'postventa','garantia'} else 'ai_flexible'
            })
            rows.append(row)
            try:
                jf.write(json.dumps({'file': row['file'], **res}, ensure_ascii=False)+'\n')
            except Exception:
                pass

    with out_csv.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['file','form_type','vin','placa','kilometraje_km','fecha','reporte_falla','pieza_danada','pieza_solicitada_garantia','status'])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print('OK ->', out_csv)


if __name__ == '__main__':
    main()
