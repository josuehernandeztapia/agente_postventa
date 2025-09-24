import json
from pathlib import Path

from prep_ft_style_curated import TEMPLATES, load_csv  # reuse templates


def gold_examples(chat_csv: Path, vision_csv: Path, out_path: Path, want=30):
    rows = load_csv(chat_csv)
    vmap = {Path(r.get('file','')).name: r for r in load_csv(vision_csv)}
    # prioridad por evidencias 
    order = ['fuga_llanta','grieta_rin','fuga_liquido','tablero','odometro','conector','vin_plate','placa_unidad']
    picked = []
    used = set()
    # 1) seleccionar por evidencias prioritarias
    for ev in order:
        for r in rows:
            if len(picked) >= want:
                break
            att = (r.get('attachment') or '').strip()
            if not att:
                continue
            v = vmap.get(Path(att).name)
            if not v:
                continue
            if (v.get('evidence_type') or '') != ev:
                continue
            key = (att, r.get('text',''))
            if key in used:
                continue
            used.add(key)
            picked.append((r,v))
        if len(picked) >= want:
            break
    # 2) completar con otros ejemplos
    if len(picked) < want:
        for r in rows:
            if len(picked) >= want:
                break
            att = (r.get('attachment') or '').strip()
            v = vmap.get(Path(att).name) if att else None
            key = (att, r.get('text',''))
            if key in used:
                continue
            used.add(key)
            picked.append((r,v or {}))

    out = []
    for r, v in picked:
        ev = (v.get('evidence_type') or '').lower()
        text = (r.get('text') or '').strip()
        user = text or 'Evidencia adjunta'
        parts = []
        if v:
            if v.get('vin'): parts.append(f"VIN {v['vin']}")
            if v.get('plate'): parts.append(f"placa {v['plate']}")
            if v.get('odo_km'): parts.append(f"odómetro={v['odo_km']} km")
            if ev: parts.append(f"evidencia={ev}")
        if parts:
            user += "\nEvidencia detectada: " + ", ".join(parts)
        steps = TEMPLATES.get(ev, ['Confirma síntoma principal y cuándo empezó.', 'Adjunta una foto enfocando el detalle.'])
        assistant = (
            f"Resumen: caso {r.get('category') or 'general'} — severidad {r.get('severity') or 'normal'}.\n"
            + "Pasos:\n- " + "\n- ".join(steps) + "\n"
            + "Cierre: ¿Te late si seguimos así?"
        )
        out.append({'messages':[{'role':'system','content':'Eres un técnico Higer. Responde breve, con pasos y tono cercano.'},{'role':'user','content':user},{'role':'assistant','content':assistant}]})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('w', encoding='utf-8') as f:
        for rec in out:
            f.write(json.dumps(rec, ensure_ascii=False)+'\n')
    print(f'OK: gold {len(out)} examples -> {out_path}')


if __name__ == '__main__':
    base = Path(__file__).parent
    gold_examples(base/'logs/chat_text.csv', base/'logs/vision_batch.csv', base/'datasets/ft_style_gold.jsonl')

