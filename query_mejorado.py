import os
import sys
import re
import pickle
from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from pinecone import Pinecone as PineconeClient
from langchain_core.messages import SystemMessage, HumanMessage
import re as _re2
import json

# --- Carga de variables de entorno ---
# Carga .env y, si existe, sobrescribe con secrets.local.txt (compat con nombre anterior)
try:
    load_dotenv(override=False)
    _base = os.path.dirname(__file__)
    _cands = [
        "secrets.local.txt",
        os.path.join(_base, "secrets.local.txt"),
        "secrets.loca.txt",  # compatibilidad legado
        os.path.join(_base, "secrets.loca.txt"),
    ]
    for _p in _cands:
        if os.path.exists(_p):
            load_dotenv(_p, override=True)
            break
except Exception:
    load_dotenv()

# --- Configuración y Validación ---
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
INDEX_NAME = os.environ.get("PINECONE_INDEX", "ssot-higer")
BM25_INDEX_FILE = os.environ.get("BM25_INDEX_FILE", "bm25_index.pkl")
HYBRID_ALPHA = float(os.environ.get("HYBRID_ALPHA", "0.6"))
HYBRID_TOP_K = int(os.environ.get("HYBRID_TOP_K", "8"))
HYBRID_DEDUPE_THRESHOLD = float(os.environ.get("HYBRID_DEDUPE_THRESHOLD", "0.75"))
PARTS_INDEX_FILE = os.environ.get("PARTS_INDEX_FILE", "parts_index.json")
SYNONYMS_FILE = os.environ.get("SYNONYMS_FILE", "")
BRAND_NAME = os.environ.get("BRAND_NAME", "Higer")
MODEL_NAME = os.environ.get("MODEL_NAME", "H6C")

if not all([PINECONE_API_KEY, OPENAI_API_KEY]):
    print("Error: Asegúrate de que PINECONE_API_KEY y OPENAI_API_KEY están en tu .env")
    sys.exit(1)

# --- APIs Falsas (Mocks) para Herramientas ---

def search_spare_parts_api(part_name: str) -> dict:
    """
    Simula una llamada a una API interna de refacciones.
    Busca un número de parte basado en un nombre.
    """
    print(f"[Herramienta: Buscando refacción para '{part_name}']")
    # Base de datos falsa de refacciones
    spare_parts_db = {
        "filtro de aceite": {"oem": "103TZ-12500", "stock": 150, "precio": "$250 MXN"},
        "bujía": {"oem": "11FCA-04505-200", "stock": 300, "precio": "$180 MXN"},
        "balata": {"oem": "35RB1-02508", "stock": 80, "precio": "$800 MXN"},
    }
    # Normaliza la búsqueda a minúsculas
    part_name_lower = part_name.lower()
    for key, value in spare_parts_db.items():
        if key in part_name_lower:
            return value
    return {"error": "Refacción no encontrada"}

# --- Componentes del Pipeline RAG ---

def detect_and_run_tools(query: str) -> dict:
    """
    Detecta si la consulta requiere una herramienta y la ejecuta.
    En una implementación real, esto podría ser un router basado en LLM.
    """
    tool_results = {}
    # Detección simple por palabras clave
    if re.search(r'\b(refacción|parte|pieza|número de parte|oem)\b', query, re.IGNORECASE):
        # Extracción simple de la entidad (el nombre de la pieza)
        match = re.search(r'(refacción|parte|pieza|número de parte|oem)\s+(?:de|para)\s+(.+)', query, re.IGNORECASE)
        search_terms = []
        if match:
            part_name = match.group(2).strip()
            tool_results['spare_part'] = search_spare_parts_api(part_name)
            search_terms.append(part_name)
            # sumar sinónimos desde diccionario
            for w in load_synonyms().get('correa_distribucion', []):
                if w not in search_terms:
                    search_terms.append(w)
        # Consultar catálogo de partes local (usa part_name si existe; si no, toda la query)
        if not search_terms:
            search_terms = [query]
        seen = {}
        cat_agg = []
        for term in search_terms:
            hits = search_parts_catalog(term, top_k=3)
            for h in hits:
                key = h.get('oem') or h.get('part_name')
                if key and key not in seen:
                    seen[key] = True
                    cat_agg.append(h)
        # Ordena agregados por score desc
        cat_hits = sorted(cat_agg, key=lambda x: x.get('score',0), reverse=True)[:3]
        if cat_hits:
            tool_results['parts_catalog'] = cat_hits
    return tool_results

def load_parts_catalog():
    candidates = []
    # 1) ruta por env o CWD
    if PARTS_INDEX_FILE:
        candidates.append(PARTS_INDEX_FILE)
    # 2) junto al script
    try:
        from pathlib import Path
        script_dir = Path(__file__).parent
        if PARTS_INDEX_FILE:
            candidates.append(str((script_dir / PARTS_INDEX_FILE).resolve()))
    except Exception:
        pass
    for path in candidates:
        try:
            if path and os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            continue
    return None

def load_synonyms():
    base = {
        "correa_distribucion": [
            "correa de distribución",
            "banda de distribución",
            "cadena de distribución",
            "timing belt",
            "timing chain",
            "timing",
        ],
        "filtro_aceite": ["filtro de aceite", "oil filter"],
        "bujia": ["bujía", "bujia", "spark plug"],
    }
    path = SYNONYMS_FILE.strip()
    if not path:
        return base
    candidates = [path]
    try:
        from pathlib import Path
        candidates.append(str((Path(__file__).parent / path).resolve()))
    except Exception:
        pass
    for p in candidates:
        try:
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        # merge
                        for k,v in data.items():
                            if isinstance(v, list):
                                base[k] = list(dict.fromkeys((base.get(k, []) + v)))
                    break
        except Exception:
            continue
    return base

def token_set(s: str):
    return set(_re2.findall(r"\w+", (s or '').lower()))

def fuzzy_score(a: str, b: str) -> float:
    sa, sb = token_set(a), token_set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union

def search_parts_catalog(name_query: str, top_k: int = 3):
    cat = load_parts_catalog()
    if not cat:
        return []
    items = cat.get('items', [])
    # Si viene un OEM exacto en el query, priorizar coincidencia exacta
    m = _re2.search(r"\b[A-Z0-9]{2,}(?:[\-–][A-Z0-9]{2,})+\b", name_query)
    if m:
        oem_q = m.group(0)
        exact = [it for it in items if (it.get('oem') or '').upper() == oem_q.upper()]
        if exact:
            return [{**it, 'score': 1.0} for it in exact[:top_k]]
    scored = []
    for it in items:
        name = it.get('part_name', '')
        score = fuzzy_score(name_query, name)
        if score > 0:
            scored.append({**it, 'score': score})
    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored[:top_k]

def rewrite_query(query: str) -> str:
    # Expandir sinónimos clave para mejorar recall
    ql = query.lower()
    expansions = []
    syns = load_synonyms()
    # Si alguna variante aparece, añade todas
    for group, words in syns.items():
        for w in words:
            if w.lower() in ql:
                expansions.extend(words)
                break
    if "oem" in ql or _re2.search(r"\b[A-Z0-9]{2,}(?:[-–][A-Z0-9]{2,})+\b", query):
        expansions.append("número de parte OEM")
        expansions.append("No. de parte")
    # Añadir marca/modelo si no aparecen
    if "higer" not in ql:
        expansions.append("Higer")
    if any(m in ql for m in ["h6c", " klq6540", "klq6540", "h5c"]):
        expansions.extend(["H6C","KLQ6540","H5C"])  # añade variantes cercanas
    # Construir consulta enriquecida
    extra = " ".join(sorted(set(expansions)))
    return (f"Manual técnico Higer {query} {extra}").strip()

def build_pinecone_filter(query_text: str) -> dict | None:
    ql = (query_text or "").lower()
    model = MODEL_NAME
    # detección simple de modelo en la consulta
    for m in ["h6c", "h5c", "klq6540"]:
        if m in ql:
            model = m.upper()
            break
    flt = {"brand": BRAND_NAME}
    if model:
        flt["model"] = model
    return flt

def _embeddings_dim(model: str) -> int:
    m = (model or "").strip().lower()
    if m in {"text-embedding-3-small", "text-embedding-3-small@latest"}:
        return 1536
    if m in {"text-embedding-3-large", "text-embedding-3-large@latest"}:
        return 3072
    return 1536

def retrieve_from_pinecone(query_embedding, top_k=16, query_text: str = ""):
    try:
        pc = PineconeClient(api_key=PINECONE_API_KEY)
        # Verificar dimensión del índice vs embeddings
        try:
            desc = pc.describe_index(INDEX_NAME)
            idx_dim = (desc.get('dimension') if isinstance(desc, dict) else getattr(desc, 'dimension', None))
            expected = _embeddings_dim(os.environ.get('EMBEDDINGS_MODEL', 'text-embedding-3-small'))
            if idx_dim is not None and int(idx_dim) != int(expected):
                print(f"[Aviso] Índice '{INDEX_NAME}' tiene dimensión {idx_dim}, embeddings esperan {expected}. Ajusta EMBEDDINGS_MODEL o reingesta.")
        except Exception:
            pass
        index = pc.Index(INDEX_NAME)
        flt = build_pinecone_filter(query_text)
        results = index.query(vector=query_embedding, top_k=top_k, include_metadata=True, filter=flt)
        return results.get('matches', [])
    except Exception as e:
        print(f"[Aviso] Recuperación vectorial deshabilitada (Pinecone): {e}")
        return []

def alpha_for_query(query_text: str) -> float:
    # Si hay OEM explícito o "número de parte", pondera más BM25
    if _re2.search(r"\b[A-Z0-9]{2,}(?:[\-–][A-Z0-9]{2,})+\b", query_text) or ("número de parte" in query_text.lower() or "numero de parte" in query_text.lower()):
        return max(0.0, min(1.0, 0.3))
    return HYBRID_ALPHA

def lexical_rerank(query_text: str, documents: list) -> list:
    try:
        from rank_bm25 import BM25Okapi
    except Exception:
        return documents
    texts = []
    for d in documents:
        m = d.get('metadata', {})
        txt = m.get('text') or ''
        if not txt:
            txt = (m.get('prev_window','') or '') + ' ' + (m.get('next_window','') or '')
        texts.append(txt)
    if not texts:
        return documents
    tokenized = [t.split() for t in texts]
    bm = BM25Okapi(tokenized)
    scores = bm.get_scores(query_text.split())
    # Empatar con items y reordenar
    paired = list(zip(documents, scores))
    paired.sort(key=lambda x: x[1], reverse=True)
    return [p[0] for p in paired]

def load_bm25():
    candidates = []
    # 1) Ruta pasada por env (relativa al CWD)
    if BM25_INDEX_FILE:
        candidates.append(BM25_INDEX_FILE)
    # 2) Ruta en el directorio del script
    try:
        from pathlib import Path
        script_dir = Path(__file__).parent
        if BM25_INDEX_FILE:
            candidates.append(str((script_dir / BM25_INDEX_FILE).resolve()))
        candidates.append(str((script_dir / 'bm25_index_unstructured.pkl').resolve()))
    except Exception:
        pass
    # 3) Fallback en CWD
    candidates.append('bm25_index_unstructured.pkl')

    for path in candidates:
        if path and os.path.exists(path):
            try:
                with open(path, 'rb') as f:
                    return pickle.load(f)
            except Exception:
                continue
    return None

def bm25_top(query: str, bm25_pack, top_k=16):
    if not bm25_pack:
        return []
    bm25 = bm25_pack['bm25']
    chunks = bm25_pack['chunks']
    # Tokenización simple por espacios
    tokens = query.split()
    scores = bm25.get_scores(tokens)
    # Top-k índices por puntaje
    top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
    return [{
        'id': f'bm25-{i}',
        'score': float(scores[i]),
        'metadata': {
            'text': chunks[i].page_content,
            'prev_window': chunks[i].metadata.get('prev_window', ''),
            'next_window': chunks[i].metadata.get('next_window', ''),
            'page_label': chunks[i].metadata.get('page_label', chunks[i].metadata.get('page', 'N/A')),
        }
    } for i in top_idx]

def _normalize(scores):
    if not scores:
        return []
    mn, mx = min(scores), max(scores)
    if mx - mn < 1e-9:
        return [1.0 for _ in scores]
    return [(s - mn) / (mx - mn) for s in scores]

def hybrid_merge(vector_hits, bm25_hits, alpha=0.6, top_k=8, dedupe_threshold=0.75):
    # Normalizar cada lista
    v_scores = _normalize([h.get('score', 0.0) for h in vector_hits])
    b_scores = _normalize([h.get('score', 0.0) for h in bm25_hits])

    v_items = []
    for h, ns in zip(vector_hits, v_scores):
        v_items.append({
            'id': h.get('id', 'vec'),
            'score': ns,
            'metadata': h.get('metadata', {})
        })
    b_items = []
    for h, ns in zip(bm25_hits, b_scores):
        b_items.append({
            'id': h.get('id', 'bm25'),
            'score': ns,
            'metadata': h.get('metadata', {})
        })

    # Unión simple y reordenar por score combinado
    combined = []
    for it in v_items:
        combined.append({**it, 'hybrid': alpha * it['score']})
    for it in b_items:
        combined.append({**it, 'hybrid': (1 - alpha) * it['score']})

    # Boosts basados en metadatos
    def boost(item):
        meta = item.get('metadata', {}) or {}
        b = 0.0
        # Preferir filas de tabla (más precisas para OEM)
        if meta.get('source') == 'table':
            b += 0.25
        # OEM exacto en metadata si la consulta lo contenía
        # (se ajusta luego cuando sepamos el patrón del query)
        return b

    for it in combined:
        it['hybrid'] += boost(it)

    # Orden descendente por puntaje híbrido
    combined.sort(key=lambda x: x['hybrid'], reverse=True)

    # Dedupe por similitud de contenido entre BM25 y vector
    def tokenize(s: str):
        return set(re.findall(r"\w+", (s or "").lower()))

    def jaccard(a: str, b: str) -> float:
        sa, sb = tokenize(a), tokenize(b)
        if not sa and not sb:
            return 0.0
        inter = len(sa & sb)
        union = len(sa | sb)
        return inter / union if union else 0.0

    def signature(meta: dict, fallback_id: str) -> str:
        txt = (meta or {}).get('text') or ''
        if txt:
            return txt
        prev_next = ((meta or {}).get('prev_window', '') or '') + ' ' + ((meta or {}).get('next_window', '') or '')
        return prev_next or fallback_id

    deduped = []
    sigs = []
    for it in combined:
        sig = signature(it.get('metadata', {}), it.get('id', ''))
        if any(jaccard(sig, s) >= dedupe_threshold for s in sigs):
            continue
        deduped.append(it)
        sigs.append(sig)

    return deduped[:top_k]

def build_context(documents: list) -> str:
    """
    Construye el contexto para el LLM a partir de los documentos recuperados.
    Incluye el texto del chunk, la ventana y metadatos clave.
    """
    context_parts = []
    for i, doc in enumerate(documents):
        meta = doc.get('metadata', {})
        text = meta.get('text', '')
        prev_window = meta.get('prev_window', '')
        next_window = meta.get('next_window', '')
        page = meta.get('page_label', 'N/A')
        
        # Usando .format() para máxima compatibilidad
        template = (
            '--- Fuente {i_plus_1} (Pagina: {page}) ---\n'
            'Contexto Previo: ...{prev_window}\n'
            'Contenido Principal: {text}\n'
            'Contexto Posterior: {next_window}...\n'
            '------------------------------------'
        )
        context_parts.append(template.format(
            i_plus_1=i + 1,
            page=page,
            prev_window=prev_window,
            text=text,
            next_window=next_window
        ))
    return "\n".join(context_parts)

def get_system_prompt() -> str:
    return ("Eres un asistente técnico de postventa para vehículos Higer. "
            "Responde en español de forma clara y accionable, basándote únicamente en el contexto de manuales y resultados de herramientas proporcionados. "
            "Actúa como un experto mecánico. Si encuentras información de una herramienta, incorpórala naturalmente en tu respuesta.\n" 
            "### Estructura de la Respuesta:\n" 
            "1. **Diagnóstico/Resumen Breve:** (Un párrafo corto)\n" 
            "2. **Pasos a Seguir:** (Lista numerada y clara)\n" 
            "3. **Refacciones Sugeridas:** (Si aplica, incluye OEM, stock y precio de la herramienta)\n" 
            "4. **Advertencias de Seguridad:** (Si aplica)\n" 
            "5. **Fuente:** (Menciona la página del documento de donde sacaste la información)\n\n" 
            "Si el contexto no es suficiente, di: 'No he encontrado información suficiente en el manual para responder a tu pregunta.'")

def main(query: str):
    print(f"\nPregunta original: {query}")

    # 1. Detectar y ejecutar herramientas
    tool_results = detect_and_run_tools(query)

    # 2. Reescribir y obtener embedding
    rewritten_query = rewrite_query(query)
    query_embedding = None
    try:
        import os as _os
        emb_model = _os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-small")
        embeddings = OpenAIEmbeddings(model=emb_model)
        query_embedding = embeddings.embed_query(rewritten_query)
    except Exception as e:
        print(f"[Aviso] Embeddings deshabilitados (OpenAI): {e}")

    # 3. Recuperar (vector + BM25) y fusionar
    vector_hits = retrieve_from_pinecone(query_embedding, query_text=rewritten_query) if query_embedding is not None else []
    bm25_pack = load_bm25()
    bm25_hits = bm25_top(rewritten_query, bm25_pack) if bm25_pack else []
    dynamic_alpha = alpha_for_query(query)
    top_docs = hybrid_merge(
        vector_hits, bm25_hits,
        alpha=dynamic_alpha,
        top_k=HYBRID_TOP_K,
        dedupe_threshold=HYBRID_DEDUPE_THRESHOLD,
    )
    # Reranking léxico adicional
    top_docs = lexical_rerank(rewritten_query, top_docs)

    # 4. Construir contexto y prompt final
    context = build_context(top_docs)
    system_prompt = get_system_prompt()
    
    tool_context = ""
    if tool_results:
        lines = ["### Resultados de Herramientas Internas:"]
        spare = tool_results.get('spare_part') if isinstance(tool_results, dict) else None
        if spare:
            try:
                import json as _json
                lines.append("- Refacción sugerida (mock): " + _json.dumps(spare, ensure_ascii=False))
            except Exception:
                lines.append("- Refacción sugerida disponible.")
        cat = tool_results.get('parts_catalog') if isinstance(tool_results, dict) else None
        if cat:
            lines.append("Catálogo de Partes (top):")
            for it in cat:
                lines.append(f"- {it.get('part_name','?')} · OEM {it.get('oem','?')} · pág {it.get('page_label','?')} · score {it.get('score',0):.2f}")
        tool_context = "\n" + "\n".join(lines)

    human_prompt = f"### Contexto de Manuales Técnicos:\n{context}{tool_context}\n\n### Pregunta del Usuario:\n{query}"
    
    # 5. Generar respuesta
    print("Generando respuesta...")
    answer_text = None
    try:
        import os as _os
        llm = ChatOpenAI(model=_os.getenv("LLM_MODEL", "gpt-4o"), temperature=0.1)
        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prompt)
        ])
        answer_text = response.content
    except Exception as e:
        print(f"[Aviso] LLM deshabilitado (OpenAI): {e}")
        # Fallback sin LLM: construir respuesta simple y directa
        lines = [
            "No se pudo contactar al modelo. Resumen local:",
        ]
        # Si hay catálogo de partes
        cat = tool_results.get('parts_catalog') if tool_results else None
        if cat:
            best = cat[0]
            lines.append("- Coincidencia de catálogo:")
            lines.append(f"  Pieza: {best.get('part_name','?')}")
            lines.append(f"  OEM: {best.get('oem','?')}")
            lines.append(f"  Página: {best.get('page_label','?')}")
        if not cat and top_docs:
            lines.append("- Se encontraron fragmentos relacionados en el manual.")
        answer_text = "\n".join(lines)

    print("\n========== Respuesta Final ==========")
    print(answer_text)
    def trim_tokens(text: str, max_tokens: int = 60) -> str:
        toks = (text or '').split()
        if len(toks) <= max_tokens:
            return ' '.join(toks)
        return ' '.join(toks[:max_tokens])

    # Ordenar por página con fallback (arábigos -> romanos -> alfanuméricos)
    def roman_to_int(s: str):
        if not s:
            return None
        s = str(s).strip().lower()
        roman_map = {'i': 1, 'v': 5, 'x': 10, 'l': 50, 'c': 100, 'd': 500, 'm': 1000}
        if any(ch not in roman_map for ch in s):
            return None
        total, prev = 0, 0
        for ch in reversed(s):
            val = roman_map.get(ch, 0)
            if val < prev:
                total -= val
            else:
                total += val
            prev = val
        return total if total > 0 else None

    import re as _re
    def natural_key(text: str):
        parts = _re.split(r'(\d+)', str(text))
        key = []
        for p in parts:
            if p.isdigit():
                key.append((1, int(p)))
            else:
                key.append((0, p.lower()))
        return key

    def page_order_key_doc(d):
        m = d.get('metadata', {})
        p = m.get('page_label', 'N/A')
        try:
            return (0, int(str(p)))
        except Exception:
            pass
        r = roman_to_int(p)
        if r is not None:
            return (1, r)
        return (2, natural_key(p))

    ordered = sorted(top_docs[:5], key=page_order_key_doc)

    print("\n--- Fuentes seleccionadas (máx 5) ---")
    for i, d in enumerate(ordered, start=1):
        m = d.get('metadata', {})
        page = m.get('page_label', 'N/A')
        doc_id = m.get('doc_id')
        chunk_idx = m.get('chunk_index')
        source_kind = m.get('source')
        oem = m.get('oem')
        part_name = m.get('part_name')
        raw = (m.get('text', '') or '').replace('\n', ' ')
        txt = trim_tokens(raw, 60)
        extra = []
        if doc_id is not None:
            extra.append(f"doc {doc_id}")
        if chunk_idx is not None:
            extra.append(f"chunk {chunk_idx}")
        if source_kind:
            extra.append(f"src {source_kind}")
        if oem:
            extra.append(f"oem {oem}")
        if part_name:
            extra.append(f"pieza {part_name}")
        extra_str = f" · {' · '.join(extra)}" if extra else ""
        print(f"{i}. pág {page}{extra_str} · {txt} ...")
    print("===================================")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        user_query = ' '.join(sys.argv[1:])
        main(user_query)
    else:
        print("Por favor, proporciona una pregunta. Ejemplo: python query_mejorado.py ¿cuál es el número de parte del filtro de aceite?")
