import os
import csv
import json
from pathlib import Path
from typing import List, Dict, Any


def load_env():
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        load_dotenv = None  # type: ignore
    base = Path(__file__).parent
    if 'OPENAI_API_KEY' not in os.environ and (base / 'secrets.local.txt').exists():
        try:
            txt = (base / 'secrets.local.txt').read_text(errors='ignore')
            import re
            m = re.search(r"\bsk-[\w-]{20,}\b", txt)
            if m:
                os.environ['OPENAI_API_KEY'] = m.group(0)
        except Exception:
            pass
    if load_env:
        try:
            load_dotenv(dotenv_path=base / '.env', override=False)  # type: ignore
        except Exception:
            pass


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in fields})


def window_text(chat_rows: List[Dict[str, str]], idx: int, window: int = 8) -> str:
    lo = max(0, idx - window)
    hi = min(len(chat_rows), idx + window + 1)
    texts = []
    for i in range(lo, hi):
        t = (chat_rows[i].get('text') or '').strip()
        if t:
            texts.append(t)
    return '\n'.join(texts)


def extract_fields_with_ai(context_text: str) -> Dict[str, str]:
    out = {"reporte_falla": "", "pieza_danada": "", "pieza_solicitada_garantia": ""}
    if not context_text or not os.getenv('OPENAI_API_KEY'):
        return out
    try:
        from openai import OpenAI  # type: ignore
        cli = OpenAI()
        model = os.getenv('LLM_MODEL', 'gpt-4o')
        prompt = (
            "Extrae campos del siguiente fragmento de conversación de taller/garantía. "
            "Devuelve SOLO JSON con: reporte_falla (texto breve), pieza_danada (texto breve), pieza_solicitada_garantia (texto breve). "
            "Si no hay información, usa \"\" en el campo.\n\n"
            f"Texto:\n{context_text}"
        )
        try:
            rsp = cli.responses.create(
                model=model,
                input=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            try:
                raw = rsp.output_text
            except Exception:
                raw = None
        except Exception:
            raw = None
        if not raw:
            msg = cli.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}], temperature=0.0)
            raw = msg.choices[0].message.content
        try:
            data = json.loads(raw)
            for k in out.keys():
                if isinstance(data.get(k), str):
                    out[k] = data.get(k).strip()
        except Exception:
            pass
    except Exception:
        return out
    return out


def heuristic_fields(context_text: str) -> Dict[str, str]:
    out = {"reporte_falla": "", "pieza_danada": "", "pieza_solicitada_garantia": ""}
    t = (context_text or '').lower()
    if not t:
        return out
    import re
    # Reporte de falla
    patterns = [
        r"no (prende|enciende|carga|funciona|frena)",
        r"se (calienta|apaga)",
        r"fuga[ s]? de? [a-záéíóúñ]+",
        r"(vibra|vibración|vibracion)",
        r"ruido (fuerte|extraño|metálico)",
        r"goteo",
        r"falla (intermitente)?",
        r"testigo [a-záéíóúñ ]+",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            out['reporte_falla'] = m.group(0)
            break
    # Catálogo de piezas frecuentes
    parts = [
        'tacómetro','medidor de temperatura','sensor','válvula','valvula','catalizador','convertidor','rin','llanta',
        'alternador','bomba','amortiguador','balata','tablero','bobina','filtro','banda','odómetro','odometro',
    ]
    # pieza dañada (palabras cerca de dañado/roto/fisura/grieta)
    m = re.search(r"(dañad[oa]|rota|quebrad[oa]|fisura|grieta)[^\n]{0,40}", t)
    if m:
        span = t[max(0, m.start()-40):m.end()+40]
        for w in parts:
            if w in span:
                out['pieza_danada'] = w
                break
    if not out['pieza_danada']:
        for w in parts:
            if w in t:
                out['pieza_danada'] = w
                break
    # pieza solicitada garantía (palabras cerca de solicitud/garantía/cambiar)
    m2 = re.search(r"(solicitud|garant[ií]a|cambiar|reemplazar)[^\n]{0,60}", t)
    if m2:
        span = t[max(0, m2.start()-40):m2.end()+60]
        for w in parts:
            if w in span:
                out['pieza_solicitada_garantia'] = w
                break
    return out


def main():
    load_env()
    base = Path(__file__).parent
    # Inputs
    forms_csv = base / 'logs' / 'forms_samples_min.csv'
    if not forms_csv.exists():
        print(f"No se encontró {forms_csv}. Ejecuta batch_forms_samples.py primero.")
        return
    chat_csv = base / 'logs' / 'chat_text.csv'
    if not chat_csv.exists():
        print(f"No se encontró {chat_csv}. Ejecuta batch_chat_text.py para generar el CSV del chat.")
        return
    forms = read_csv(forms_csv)
    chat = read_csv(chat_csv)
    # Mapa de attachment stem -> índices en chat
    attach_map: Dict[str, List[int]] = {}
    for i, r in enumerate(chat):
        att = (r.get('attachment') or '').strip()
        if not att:
            continue
        stem = Path(att).stem
        attach_map.setdefault(stem, []).append(i)

    enriched: List[Dict[str, Any]] = []
    for fr in forms:
        file_path = fr.get('file') or ''
        stem = Path(file_path).stem
        idxs = attach_map.get(stem) or []
        # contexto: si hay varios matches, toma el primero; si no hay, contexto vacío
        ctx = ''
        if idxs:
            win = int(os.getenv('FORMS_WINDOW', '20') or '20')
            ctx = window_text(chat, idxs[0], window=win)
        fields = extract_fields_with_ai(ctx)
        # Heurística si AI no llena
        h = heuristic_fields(ctx)
        for k, v in h.items():
            if not fields.get(k):
                fields[k] = v
        out = dict(fr)
        for k, v in fields.items():
            if not out.get(k):
                out[k] = v
        enriched.append(out)

    out_path = base / 'logs' / 'forms_samples_enriched.csv'
    fields = ['file','form_type','vin','placa','kilometraje_km','fecha','reporte_falla','pieza_danada','pieza_solicitada_garantia','status']
    write_csv(out_path, enriched, fields)
    print(f"OK -> {out_path}")


if __name__ == '__main__':
    main()
