import json
import sys
from pathlib import Path


def load_csv(path: Path):
    import csv
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as f:
        r = csv.DictReader(f)
        return list(r)


def build_examples(chat_csv: Path, vision_csv: Path, limit: int = 200):
    rows = load_csv(chat_csv)
    vmap = {Path(r.get('file','')).name: r for r in load_csv(vision_csv)}
    out = []
    for r in rows:
        if len(out) >= limit:
            break
        text = (r.get('text') or '').strip()
        att = (r.get('attachment') or '').strip()
        if not text and not att:
            continue
        cat = r.get('category') or 'general'
        sev = r.get('severity') or 'normal'
        ev = None
        vin = plate = odo = None
        if att:
            v = vmap.get(Path(att).name)
            if v:
                ev = v.get('evidence_type')
                vin = v.get('vin')
                plate = v.get('plate')
                odo = v.get('odo_km')
        # user prompt (condensado)
        ev_parts = []
        if vin: ev_parts.append(f"VIN {vin}")
        if plate: ev_parts.append(f"placa {plate}")
        if odo: ev_parts.append(f"odómetro={odo} km")
        if ev: ev_parts.append(f"evidencia={ev}")
        ev_txt = ("\nEvidencia detectada: " + ", ".join(ev_parts)) if ev_parts else ""
        user = (text or 'Evidencia adjunta') + ev_txt
        # assistant (plantilla estilo)
        steps_hint = {
            'fuga_llanta': [
                'No circules si baja rápido la presión.',
                'Repite prueba con agua jabonosa: talón, válvula, soldadura.',
                'Si burbujea en soldadura/poro: cambia el rin.',
                'Si es talón: limpia asiento, bead sealer y re-asienta.',
                'Si es válvula: reemplázala.'
            ],
            'grieta_rin': [
                'No circules; evalúa reemplazo del rin.',
                'Verifica deformación y torque de tuercas.',
                'Balancea y prueba en ruta.'
            ]
        }.get(ev or '', ['Confirma síntoma principal y cuándo empezó.', 'Te pido una foto adicional enfocando el detalle.'])
        assistant = (
            f"Resumen: Caso {cat} — severidad {sev}.\n"
            + "Pasos:\n- " + "\n- ".join(steps_hint) + "\n"
            + "Cierre: ¿Te late si seguimos así?"
        )
        out.append({
            'messages': [
                {'role': 'system', 'content': 'Eres un técnico Higer. Responde breve, con pasos y tono cercano.'},
                {'role': 'user', 'content': user},
                {'role': 'assistant', 'content': assistant}
            ]
        })
    return out


def main(out_path: str = 'datasets/ft_style.jsonl'):
    base = Path(__file__).parent
    chat_csv = base/'logs/chat_text.csv'
    vision_csv = base/'logs/vision_batch.csv'
    ex = build_examples(chat_csv, vision_csv)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w', encoding='utf-8') as f:
        for rec in ex:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f'OK: wrote {len(ex)} examples to {out}')


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv)>1 else 'datasets/ft_style.jsonl'
    main(path)

