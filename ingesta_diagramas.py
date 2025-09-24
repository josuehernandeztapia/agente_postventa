import os
import sys
from pathlib import Path
from dotenv import load_dotenv

try:
    load_dotenv(override=False)
except Exception:
    load_dotenv()

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
PINECONE_ENV = os.getenv("PINECONE_ENV", "us-east-1-aws")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PDF_FILE = os.getenv("PDF_FILE", "SSOT-HIGER.pdf")
INDEX_NAME = os.getenv("PINECONE_INDEX_DIAGRAMS", "ssot-higer-diagramas-elect")
EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-small")

# Asegurar ruta de datos de Tesseract: preferir local 'tessdata' junto al script
try:
    from pathlib import Path as _P
    _base_dir = _P(__file__).parent
    _local_tess = _base_dir / 'tessdata'
    if _local_tess.exists():
        os.environ.setdefault("TESSDATA_PREFIX", str(_local_tess))
    else:
        os.environ.setdefault("TESSDATA_PREFIX", "/opt/homebrew/share/tessdata")
except Exception:
    os.environ.setdefault("TESSDATA_PREFIX", "/opt/homebrew/share/tessdata")

def _embeddings_dim(model: str) -> int:
    m = (model or "").strip().lower()
    if m in {"text-embedding-3-small", "text-embedding-3-small@latest"}:
        return 1536
    if m in {"text-embedding-3-large", "text-embedding-3-large@latest"}:
        return 3072
    return 1536

def _fallback_parse_secrets():
    """Intento tolerante para extraer claves de un secrets.local.txt no-KEY=VALUE.
    Busca patrones comunes y las inyecta a os.environ si faltan.
    """
    try:
        base = Path(__file__).parent
        cand = base / "secrets.local.txt"
        if not cand.exists():
            return
        txt = cand.read_text(errors="ignore")
        # OpenAI
        global OPENAI_API_KEY
        if not OPENAI_API_KEY:
            import re as _re
            m = _re.search(r"\bsk-[\w-]{20,}\b", txt)
            if m:
                OPENAI_API_KEY = m.group(0)
                os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
        # Pinecone
        global PINECONE_API_KEY
        if not PINECONE_API_KEY:
            import re as _re
            m = _re.search(r"\bpcsk_[A-Za-z0-9_-]{20,}\b", txt)
            if m:
                PINECONE_API_KEY = m.group(0)
                os.environ["PINECONE_API_KEY"] = PINECONE_API_KEY
    except Exception:
        pass

if not (PINECONE_API_KEY and PINECONE_ENV and OPENAI_API_KEY):
    _fallback_parse_secrets()
    if not (PINECONE_API_KEY and PINECONE_ENV and OPENAI_API_KEY):
        print("Faltan variables de entorno: PINECONE_API_KEY, PINECONE_ENV, OPENAI_API_KEY")
        sys.exit(1)

def parse_env_region(env_str: str):
    try:
        region, cloud = env_str.rsplit('-', 1)
        return region, cloud
    except Exception:
        print(f"PINECONE_ENV inválido: {env_str}")
        sys.exit(1)

def is_diagram_like(text: str) -> bool:
    t = (text or '').lower()
    keys = [
        'diagrama', 'esquema', 'fusible', 'fuse', 'relay', 'relé', 'conector', 'connector',
        'pin', 'pinout', 'circuito', 'ecu', 'gnd', 'ign', 'masa', 'tierra', 'positivo'
    ]
    return any(k in t for k in keys)

def should_keep(text: str) -> bool:
    toks = (text or '').split()
    if len(toks) >= 40:
        return True
    return is_diagram_like(text)

def main():
    from langchain_community.document_loaders import UnstructuredPDFLoader
    from langchain_openai import OpenAIEmbeddings
    from pinecone import Pinecone as PineconeClient, ServerlessSpec
    # Evitar fallos por imágenes truncadas/dañadas en PDF
    try:
        from PIL import ImageFile  # type: ignore
        ImageFile.LOAD_TRUNCATED_IMAGES = True
    except Exception:
        pass

    pdf_path = Path(PDF_FILE)
    if not pdf_path.exists():
        here = Path(__file__).parent / PDF_FILE
        if here.exists():
            pdf_path = here
        else:
            print(f"No se encontró PDF en {PDF_FILE}")
            sys.exit(1)

    print(f"Cargando diagramas desde: {pdf_path}")
    # OCR bilingüe y alta resolución
    loader = UnstructuredPDFLoader(
        str(pdf_path),
        mode="elements",
        strategy="hi_res",
        languages=["spa","eng"],
        infer_table_structure=True,
    )
    try:
        docs = loader.load()
    except Exception as e:
        print("Fallo en carga hi_res (", e, ") → intento 'fast'")
        loader = UnstructuredPDFLoader(str(pdf_path), mode="elements", strategy="fast", languages=["spa","eng"], infer_table_structure=False)
        docs = loader.load()
    print(f"Elementos totales: {len(docs)}")

    # Filtrar por densidad e intención
    filtered = []
    for d in docs:
        txt = getattr(d, 'page_content', '') or ''
        if not should_keep(txt):
            continue
        md = dict(getattr(d, 'metadata', {}) or {})
        md['source'] = 'diagram'
        d.metadata = md
        filtered.append(d)
    print(f"Elementos tras filtro: {len(filtered)}")

    # Pinecone
    region, cloud = parse_env_region(PINECONE_ENV)
    pc = PineconeClient(api_key=PINECONE_API_KEY)
    if INDEX_NAME not in pc.list_indexes().names():
        print(f"Creando índice {INDEX_NAME}…")
        pc.create_index(name=INDEX_NAME, dimension=_embeddings_dim(EMBEDDINGS_MODEL), metric="cosine", spec=ServerlessSpec(cloud=cloud, region=region))
    else:
        try:
            desc = pc.describe_index(INDEX_NAME)
            dim = (desc.get('dimension') if isinstance(desc, dict) else getattr(desc, 'dimension', None))
            expected = _embeddings_dim(EMBEDDINGS_MODEL)
            if dim is not None and int(dim) != int(expected):
                print(f"Error: la dimensión del índice ({dim}) no coincide con EMBEDDINGS_MODEL ({expected}). Cambia EMBEDDINGS_MODEL o recrea el índice.")
                sys.exit(1)
        except Exception:
            pass

    index = pc.Index(INDEX_NAME)
    embeddings = OpenAIEmbeddings(model=EMBEDDINGS_MODEL, openai_api_key=OPENAI_API_KEY)

    # Subir en lotes
    print(f"Subiendo {len(filtered)} elementos a {INDEX_NAME}…")
    batch = 100
    for i in range(0, len(filtered), batch):
        chunk = filtered[i:i+batch]
        ids = [f"{pdf_path.stem}-diagram-{i+j}" for j,_ in enumerate(chunk)]
        texts = [c.page_content for c in chunk]
        metas = []
        for c in chunk:
            m = {}
            for k,v in (c.metadata or {}).items():
                if isinstance(v, (str,int,float,bool)):
                    m[k]=v
            m['text'] = c.page_content
            metas.append(m)
        vecs = embeddings.embed_documents(texts)
        payload = [
            {"id": idx, "values": vec, "metadata": meta}
            for idx, vec, meta in zip(ids, vecs, metas)
        ]
        if payload:
            index.upsert(vectors=payload)

    try:
        stats = index.describe_index_stats()
        print("Stats:", stats)
    except Exception:
        pass
    print("Listo: diagramas indexados.")

if __name__ == '__main__':
    main()
