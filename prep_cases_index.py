import os
import csv
from pathlib import Path
from typing import List, Dict

from dotenv import load_dotenv  # type: ignore


def _load_env():
    base = Path(__file__).parent
    try:
        load_dotenv(dotenv_path=base/'.env', override=False)
    except Exception:
        pass
    try:
        load_dotenv(dotenv_path=base/'secrets.local.txt', override=True)
    except Exception:
        pass
    # Fallback regex para OPENAI_API_KEY si secrets no tiene KEY=VAL
    try:
        if not os.getenv('OPENAI_API_KEY') and (base/'secrets.local.txt').exists():
            txt = (base/'secrets.local.txt').read_text(errors='ignore')
            import re
            m = re.search(r"\bsk-[\w-]{20,}\b", txt)
            if m:
                os.environ['OPENAI_API_KEY'] = m.group(0)
    except Exception:
        pass


def _read_csv(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as f:
        r = csv.DictReader(f)
        return list(r)


def build_rows() -> List[Dict]:
    base = Path(__file__).parent
    vp = base/'logs/vision_batch.csv'
    cp = base/'logs/chat_text.csv'
    vrows = _read_csv(vp)
    crows = _read_csv(cp)
    vmap = {Path(r.get('file','')).name: r for r in vrows}
    out: List[Dict] = []
    for r in crows:
        att = (r.get('attachment') or '').strip()
        if not att:
            continue
        key = Path(att).name
        v = vmap.get(key, {})
        text = (r.get('text') or '').strip()
        ev = (v.get('evidence_type') or '').strip()
        # Construir texto final del documento (máx ~2–3 párrafos breves)
        parts = []
        if text:
            parts.append(f"Mensaje: {text}")
        evs = []
        if v.get('vin'): evs.append(f"VIN {v['vin']}")
        if v.get('plate'): evs.append(f"placa {v['plate']}")
        if v.get('odo_km'): evs.append(f"odómetro={v['odo_km']} km")
        if v.get('delivered_at'): evs.append(f"entregado={v['delivered_at']}")
        if ev or evs:
            parts.append("Evidencia: " + ", ".join([e for e in [ev] if e] + evs))
        body = "\n".join(parts) or (ev or 'caso')
        out.append({
            'text': body,
            'category': r.get('category') or '',
            'severity': r.get('severity') or '',
            'evidence_type': ev,
            'vin': v.get('vin') or '',
            'plate': v.get('plate') or '',
            'odo_km': v.get('odo_km') or '',
            'delivered_at': v.get('delivered_at') or '',
            'ts': r.get('ts') or '',
            'author': r.get('author') or '',
            'attachment': key,
        })
    return out

def _category_from_ev(ev: str | None) -> str:
    ev = (ev or '').lower()
    if ev in {'fuga_llanta','grieta_rin'}:
        return 'tires'
    if ev in {'tablero','conector'}:
        return 'electrical'
    if ev in {'fuga_liquido'}:
        return 'oil'
    return 'general'


def upsert_cases(rows: List[Dict]):
    from langchain_openai import OpenAIEmbeddings  # type: ignore
    from pinecone import Pinecone as PineconeClient, ServerlessSpec  # type: ignore

    api_key = os.getenv('PINECONE_API_KEY')
    env = os.getenv('PINECONE_ENV', 'us-east-1-aws')
    index_name = os.getenv('PINECONE_INDEX_CASES', 'ssot-higer-cases')
    emb_model = os.getenv('EMBEDDINGS_MODEL', 'text-embedding-3-small')

    def emb_dim(m: str) -> int:
        m = (m or '').lower()
        if '3-small' in m: return 1536
        if '3-large' in m: return 3072
        return 1536

    pc = PineconeClient(api_key=api_key)
    # Crear índice si no existe
    names = set(pc.list_indexes().names())
    if index_name not in names:
        region, cloud = env.rsplit('-', 1)
        pc.create_index(name=index_name, dimension=emb_dim(emb_model), metric='cosine', spec=ServerlessSpec(cloud=cloud, region=region))
    idx = pc.Index(index_name)

    embeddings = OpenAIEmbeddings(model=emb_model)

    # Subir por lotes
    batch = 100
    vecs = []
    ids = []
    metas = []
    for i, r in enumerate(rows):
        ids.append(f"case-{i}")
        metas.append({
            'text': r['text'],
            'category': r['category'],
            'severity': r['severity'],
            'evidence_type': r['evidence_type'],
            'vin': r['vin'],
            'plate': r['plate'],
            'odo_km': r['odo_km'],
            'delivered_at': r['delivered_at'],
            'ts': r['ts'],
            'author': r['author'],
            'attachment': r['attachment'],
            'source': 'case',
        })
        vecs.append(r['text'])
        # flush in batches
        if len(vecs) >= batch:
            embs = embeddings.embed_documents(vecs)
            payload = [{'id': id_, 'values': v, 'metadata': m} for id_, v, m in zip(ids, embs, metas)]
            idx.upsert(vectors=payload)
            ids, metas, vecs = [], [], []
    if vecs:
        embs = embeddings.embed_documents(vecs)
        payload = [{'id': id_, 'values': v, 'metadata': m} for id_, v, m in zip(ids, embs, metas)]
        idx.upsert(vectors=payload)
    return idx


def main():
    _load_env()
    rows = build_rows()
    if not rows:
        print('No hay filas para casos. Asegura vision_batch.csv y chat_text.csv')
        return
    idx = upsert_cases(rows)
    # Agregar entradas solo-visión para evidencias no unidas en chat
    base = Path(__file__).parent
    vrows = _read_csv(base/'logs/vision_batch.csv')
    existing_keys = {r.get('attachment') for r in rows if r.get('attachment')}
    extra = []
    for v in vrows:
        fn = Path(v.get('file','')).name
        if fn in existing_keys:
            continue
        ev = v.get('evidence_type') or ''
        if not ev:
            continue
        parts = []
        if v.get('vin'): parts.append(f"VIN {v['vin']}")
        if v.get('plate'): parts.append(f"placa {v['plate']}")
        if v.get('odo_km'): parts.append(f"odómetro={v['odo_km']} km")
        parts.append(f"evidencia={ev}")
        text = "Evidencia: " + ", ".join(parts)
        extra.append({
            'text': text,
            'category': _category_from_ev(ev),
            'severity': 'normal',
            'evidence_type': ev,
            'vin': v.get('vin') or '',
            'plate': v.get('plate') or '',
            'odo_km': v.get('odo_km') or '',
            'delivered_at': v.get('delivered_at') or '',
            'ts': '',
            'author': '',
            'attachment': fn,
        })
    if extra:
        from langchain_openai import OpenAIEmbeddings  # type: ignore
        embeddings = OpenAIEmbeddings(model=os.getenv('EMBEDDINGS_MODEL', 'text-embedding-3-small'))
        ids = [f"vision-only-{i}" for i,_ in enumerate(extra)]
        metas = [{
            'text': r['text'], 'category': r['category'], 'severity': r['severity'],
            'evidence_type': r['evidence_type'], 'vin': r['vin'], 'plate': r['plate'],
            'odo_km': r['odo_km'], 'delivered_at': r['delivered_at'], 'ts': r['ts'],
            'author': r['author'], 'attachment': r['attachment'], 'source': 'case'
        } for r in extra]
        vecs = [r['text'] for r in extra]
        embs = embeddings.embed_documents(vecs)
        payload = [{'id': id_, 'values': v, 'metadata': m} for id_, v, m in zip(ids, embs, metas)]
        idx.upsert(vectors=payload)
        print(f"OK: casos vision-only subidos: {len(extra)}")
    print(f'OK: casos subidos: {len(rows)} al índice {os.getenv("PINECONE_INDEX_CASES", "ssot-higer-cases")}')


if __name__ == '__main__':
    main()
