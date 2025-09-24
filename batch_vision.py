import os
import sys
import argparse
from pathlib import Path
import csv

# Cargar .env y secrets.local.txt si existen (para OPENAI_API_KEY)
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None

def _fallback_openai_from_secrets(base: Path):
    try:
        p = base / 'secrets.local.txt'
        if not p.exists():
            return
        txt = p.read_text(errors='ignore')
        import re
        if not os.getenv('OPENAI_API_KEY'):
            m = re.search(r"\bsk-[\w-]{20,}\b", txt)
            if m:
                os.environ['OPENAI_API_KEY'] = m.group(0)
    except Exception:
        pass

# Permitir import relativo del módulo de visión
ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from app.vision_openai import ocr_image_openai  # type: ignore


def to_data_url(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    ctype = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png', '.webp': 'image/webp',
    }.get(ext, 'image/jpeg')
    import base64
    b64 = base64.b64encode(path.read_bytes()).decode('utf-8')
    return f"data:{ctype};base64,{b64}", ctype


def scan_images(root: Path):
    exts = {'.jpg', '.jpeg', '.png', '.webp'}
    for p in sorted(root.rglob('*')):
        if p.is_file() and p.suffix.lower() in exts:
            yield p


def main():
    ap = argparse.ArgumentParser(description='Batch OCR/Vision over a directory of images')
    ap.add_argument('--dir', required=True, help='Directory with images (recursive)')
    ap.add_argument('--limit', type=int, default=24, help='Max images to process')
    ap.add_argument('--out', default='logs/vision_batch.csv', help='CSV output path')
    args = ap.parse_args()

    root = Path(args.dir).expanduser()
    base = Path(__file__).parent
    # Cargar env
    if load_dotenv:
        try:
            load_dotenv(dotenv_path=base / '.env', override=False)
            load_dotenv(dotenv_path=base / 'secrets.local.txt', override=True)
        except Exception:
            pass
    _fallback_openai_from_secrets(base)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    count = 0
    for img in scan_images(root):
        if count >= max(1, args.limit):
            break
        try:
            data_url, ctype = to_data_url(img)
            res = ocr_image_openai(data_url, kind='evidencia') or {}
            rows.append({
                'file': str(img),
                'evidence_type': (res or {}).get('evidence_type'),
                'vin': (res or {}).get('vin'),
                'plate': (res or {}).get('plate'),
                'odo_km': (res or {}).get('odo_km'),
                'delivered_at': (res or {}).get('delivered_at'),
                'notes': (res or {}).get('notes'),
            })
            count += 1
        except Exception as e:
            rows.append({'file': str(img), 'evidence_type': None, 'vin': None, 'plate': None, 'odo_km': None, 'delivered_at': None, 'notes': f'error: {e}'})

    with out.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['file', 'evidence_type', 'vin', 'plate', 'odo_km', 'delivered_at', 'notes'])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f'OK: wrote {len(rows)} rows to {out}')


if __name__ == '__main__':
    main()
