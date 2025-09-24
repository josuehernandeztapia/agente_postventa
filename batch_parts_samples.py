import base64, csv, json, sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from app.vision_openai import classify_part_image, ocr_image_openai  # type: ignore


def load_stems(arg: str) -> List[str]:
    p = Path(arg)
    if p.exists():
        return [ln.strip() for ln in p.read_text(encoding='utf-8').splitlines() if ln.strip()]
    return [s.strip() for s in arg.split(',') if s.strip()]


def find_image(base: Path, stem: str) -> Path | None:
    for ext in ['.jpg', '.jpeg', '.png', '.webp']:
        p = base / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def main():
    if len(sys.argv) < 2:
        print('Uso: python3 batch_parts_samples.py <stems-file|comma-list>')
        sys.exit(1)
    stems = load_stems(sys.argv[1])
    base_dir = (Path(__file__).resolve().parent.parent / 'WhatsApp Chat - Higer Postventa AGS 2').resolve()
    out_csv = Path('rag-pinecone/logs/parts_samples.csv')
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for stem in stems:
        img = find_image(base_dir, stem)
        row = {'file': stem}
        if not img:
            row['status'] = 'missing'
            rows.append(row)
            continue
        b64 = base64.b64encode(img.read_bytes()).decode('utf-8')
        du = f'data:image/jpeg;base64,{b64}'
        res = classify_part_image(du) or {}
        ocr = ocr_image_openai(du, kind='evidencia') or {}
        row.update({
            'part_guess': res.get('part_guess'),
            'system': res.get('system'),
            'condition': res.get('condition'),
            'damage_signs': '|'.join(res.get('damage_signs') or []),
            'evidence_type': res.get('evidence_type') or ocr.get('evidence_type') or '',
            'recommended_checks': '|'.join(res.get('recommended_checks') or []),
            'risk_level': res.get('risk_level'),
            'confidence': res.get('confidence'),
            'ask_user': res.get('ask_user') or '',
            'notes': res.get('notes') or '',
            'status': 'ok'
        })
        rows.append(row)
    with out_csv.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['file','part_guess','system','condition','damage_signs','evidence_type','recommended_checks','risk_level','confidence','ask_user','notes','status'])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print('OK ->', out_csv)


if __name__ == '__main__':
    main()
