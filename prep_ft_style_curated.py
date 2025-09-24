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

TEMPLATES = {
    'fuga_llanta': [
        'No circules si baja rápido la presión.',
        'Repite prueba con agua jabonosa: talón (bead), base de válvula y soldadura del rin.',
        'Si burbujea en soldadura/poro: cambia el rin (no resoldar para servicio).',
        'Si es talón: desmonta, limpia asiento, aplica bead sealer y vuelve a asentar; balancea.',
        'Si es válvula: reemplázala y verifica nuevamente.'
    ],
    'grieta_rin': [
        'No circules; evalúa reemplazo del rin.',
        'Verifica deformaciones y torque de tuercas.',
        'Balancea y prueba en ruta; revisa vibraciones.'
    ],
    'tablero': [
        'Toma foto nítida del testigo y del odómetro.',
        'Valida si el testigo es fijo o intermitente y cuándo aparece.',
        'Si es crítico (aceite/temp/frenos), detén la vagoneta y solicita arrastre.'
    ],
    'odometro': [
        'Confirma lectura y si hay prueba reciente de mantenimiento.',
        'Considera servicio si supera el intervalo recomendado.'
    ],
    'fuga_liquido': [
        'Identifica color/olor del fluido (aceite/refrigerante/frenos).',
        'Limpia, aplica talco o traza y observa el origen.',
        'Aprieta conexiones; si persiste, lleva a inspección.'
    ],
}

def curate_examples(chat_csv: Path, vision_csv: Path, out_path: Path, want=40):
    rows = load_csv(chat_csv)
    vmap = {Path(r.get('file','')).name: r for r in load_csv(vision_csv)}
    ex = []
    # Priorizar evidencias específicas
    preferred = {'fuga_llanta','grieta_rin','fuga_liquido','tablero','odometro'}
    for r in rows:
        if len(ex) >= want:
            break
        att = (r.get('attachment') or '').strip()
        text = (r.get('text') or '').strip()
        if not att and not text:
            continue
        v = vmap.get(Path(att).name) if att else None
        ev = (v.get('evidence_type') if v else '') or ''
        if ev not in preferred:
            continue
        user = text or 'Evidencia adjunta'
        parts = []
        if v:
            if v.get('vin'): parts.append(f"VIN {v['vin']}")
            if v.get('plate'): parts.append(f"placa {v['plate']}")
            if v.get('odo_km'): parts.append(f"odómetro={v['odo_km']} km")
            parts.append(f"evidencia={ev}")
        if parts:
            user += "\nEvidencia detectada: " + ", ".join(parts)
        steps = TEMPLATES.get(ev, ['Confirma síntoma principal y cuándo empezó.', 'Adjunta una foto enfocando el detalle.'])
        assistant = (
            f"Resumen: caso {r.get('category') or 'general'} — severidad {r.get('severity') or 'normal'}.\n"
            + "Pasos:\n- " + "\n- ".join(steps) + "\n"
            + "Cierre: ¿Te late si seguimos así?"
        )
        ex.append({
            'messages': [
                {'role': 'system', 'content': 'Eres un técnico Higer. Responde breve, con pasos y tono cercano.'},
                {'role': 'user', 'content': user},
                {'role': 'assistant', 'content': assistant}
            ]
        })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('w', encoding='utf-8') as f:
        for rec in ex:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f'OK: curated {len(ex)} examples -> {out_path}')

def main():
    base = Path(__file__).parent
    curate_examples(base/'logs/chat_text.csv', base/'logs/vision_batch.csv', base/'datasets/ft_style_curated.jsonl')

if __name__ == '__main__':
    main()
