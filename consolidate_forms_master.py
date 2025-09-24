import csv
from pathlib import Path
from typing import Dict


def read_csv(p: Path):
    if not p.exists():
        return []
    with p.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_csv(p: Path, rows, fields):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in fields})


def main():
    base = Path(__file__).parent
    base_logs = base / 'logs'
    base_logs.mkdir(exist_ok=True)
    minimal = read_csv(base_logs / 'forms_samples.csv')
    enriched = read_csv(base_logs / 'forms_samples_enriched.csv')
    enr_map: Dict[str, dict] = {}
    for r in enriched:
        enr_map[Path(r.get('file','')).stem] = r
    out = []
    for r in minimal:
        key = Path(r.get('file','')).stem
        e = enr_map.get(key, {})
        out.append({
            'file': r.get('file') or e.get('file') or key,
            'form_type': r.get('form_type') or e.get('form_type') or '',
            'vin': r.get('vin') or '',
            'placa': r.get('placa') or '',
            'kilometraje_km': r.get('kilometraje_km') or r.get('odo_km') or '',
            'fecha': r.get('fecha') or r.get('delivered_at') or '',
            'reporte_falla': e.get('reporte_falla') or '',
            'pieza_danada': e.get('pieza_danada') or '',
            'pieza_solicitada_garantia': e.get('pieza_solicitada_garantia') or '',
            'status': e.get('status') or r.get('status') or '',
        })
    out_path = base_logs / 'forms_samples_master.csv'
    fields = ['file','form_type','vin','placa','kilometraje_km','fecha','reporte_falla','pieza_danada','pieza_solicitada_garantia','status']
    write_csv(out_path, out, fields)
    print('OK ->', out_path)


if __name__ == '__main__':
    main()

