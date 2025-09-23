import argparse
import os
import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from dotenv import load_dotenv
from tqdm import tqdm

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

try:
    from langchain_community.document_loaders import PyMuPDFLoader, UnstructuredPDFLoader
    from langchain.text_splitter import RecursiveCharacterTextSplitter
    from langchain_core.documents import Document
    from langchain_openai import OpenAIEmbeddings
    from pinecone import Pinecone as PineconeClient, ServerlessSpec
    from rank_bm25 import BM25Okapi
except ImportError as exc:  # pragma: no cover - dependency guard
    print(f"Error: falta una dependencia requerida para la ingesta ({exc}).")
    print("Instala: langchain-community, langchain-openai, pinecone-client, rank_bm25, unstructured")
    sys.exit(1)


def _load_env():
    try:
        load_dotenv(override=False)
        candidates = [
            ROOT_DIR / "secrets.local.txt",
            APP_DIR / "secrets.local.txt",
            Path("secrets.local.txt"),
            ROOT_DIR / "secrets.loca.txt",
            APP_DIR / "secrets.loca.txt",
            Path("secrets.loca.txt"),
        ]
        for cand in candidates:
            if os.path.exists(cand):
                load_dotenv(cand, override=True)
                break
    except Exception:
        load_dotenv()


def _embeddings_dim(model: str) -> int:
    m = (model or "").strip().lower()
    if m in {"text-embedding-3-small", "text-embedding-3-small@latest"}:
        return 1536
    if m in {"text-embedding-3-large", "text-embedding-3-large@latest"}:
        return 3072
    return 1536


def _parse_args(cli_args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingesta unificada para Pinecone + BM25")
    parser.add_argument("--pdf", default=os.environ.get("PDF_FILE", "SSOT-HIGER.pdf"), help="Ruta del PDF fuente")
    parser.add_argument("--index-name", default=os.environ.get("PINECONE_INDEX", "ssot-higer"), help="Nombre del índice en Pinecone")
    parser.add_argument("--bm25-index-file", default=os.environ.get("BM25_INDEX_FILE", "bm25_index_unstructured.pkl"), help="Archivo destino para el índice BM25")
    parser.add_argument("--chunk-size", type=int, default=int(os.getenv("INGEST_CHUNK_SIZE", "1000")), help="Tamaño de chunk de texto")
    parser.add_argument("--chunk-overlap", type=int, default=int(os.getenv("INGEST_CHUNK_OVERLAP", "200")), help="Superposición entre chunks")
    parser.add_argument("--ocr", action="store_true", help="Usar OCR (Unstructured hi_res) con fallback a métodos rápidos")
    parser.add_argument("--bm25-only", action="store_true", help="Solo generar índice BM25, sin subir a Pinecone")
    parser.add_argument("--incremental", action="store_true", help="No recrear el índice; upsert incremental")
    parser.add_argument("--recreate", action="store_true", help="Forzar recrear índice Pinecone (ignora incremental)")
    parser.add_argument("--pages", default=None, help="Páginas a procesar (ej. 12,15-18)")
    parser.add_argument("--ingest-version", default=os.getenv("INGEST_VERSION"), help="Etiqueta de versión a registrar en metadatos")
    parser.add_argument("--doc-id", default=None, help="Identificador lógico del documento (default: stem del PDF)")
    return parser.parse_args(cli_args)


def _ensure_tessdata():
    try:
        local_tess = ROOT_DIR / "tessdata"
        if local_tess.exists():
            os.environ.setdefault("TESSDATA_PREFIX", str(local_tess))
        else:
            os.environ.setdefault("TESSDATA_PREFIX", "/opt/homebrew/share/tessdata")
    except Exception:
        os.environ.setdefault("TESSDATA_PREFIX", "/opt/homebrew/share/tessdata")


def _load_with_unstructured(pdf_path: Path) -> List[Document]:
    _ensure_tessdata()
    try:
        loader = UnstructuredPDFLoader(
            str(pdf_path),
            mode="elements",
            strategy="hi_res",
            languages=["spa"],
            infer_table_structure=True,
        )
        docs = loader.load()
        if docs:
            return docs
        raise RuntimeError("Unstructured hi_res devolvió 0 elementos")
    except Exception as err_hi:
        print(f"[Aviso] Unstructured hi_res falló ({err_hi}). Probando strategy=fast...")
        try:
            loader = UnstructuredPDFLoader(
                str(pdf_path),
                mode="elements",
                strategy="fast",
                infer_table_structure=True,
            )
            docs = loader.load()
            if docs:
                return docs
            raise RuntimeError("Unstructured fast devolvió 0 elementos")
        except Exception as err_fast:
            print(f"[Aviso] Unstructured fast falló ({err_fast}). Fallback a PyMuPDF...")
            return _load_with_pymupdf(pdf_path)


def _load_with_pymupdf(pdf_path: Path) -> List[Document]:
    loader = PyMuPDFLoader(str(pdf_path))
    return loader.load()


def _fallback_pdfplumber(pdf_path: Path) -> List[Document]:
    try:
        import pdfplumber
    except Exception as e:
        raise RuntimeError(f"pdfplumber no disponible ({e})") from e

    documents: List[Document] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if not text.strip():
                continue
            meta = {"page_label": str(page.page_number), "doc_id": pdf_path.stem}
            documents.append(Document(page_content=text, metadata=meta))
    if not documents:
        raise RuntimeError("pdfplumber no logró extraer texto")
    return documents


def load_documents(pdf_path: Path, use_ocr: bool) -> List[Document]:
    try:
        if use_ocr:
            return _load_with_unstructured(pdf_path)
        return _load_with_pymupdf(pdf_path)
    except Exception as exc:
        print(f"[Aviso] Lectura primaria del PDF falló ({exc}). Fallback a pdfplumber...")
        return _fallback_pdfplumber(pdf_path)


def parse_page_spec(spec: str) -> Optional[set]:
    spec = (spec or "").strip()
    if not spec:
        return None
    pages: set = set()
    for token in spec.split(','):
        token = token.strip()
        if not token:
            continue
        if '-' in token:
            start, end = token.split('-', 1)
            try:
                a = int(start)
                b = int(end)
            except Exception:
                continue
            for p in range(min(a, b), max(a, b) + 1):
                pages.add(p)
        else:
            try:
                pages.add(int(token))
            except Exception:
                continue
    return pages or None


def extract_table_docs(pdf_path: Path, doc_id: str) -> List[Document]:
    docs: List[Document] = []
    try:
        import pdfplumber
    except Exception:
        return docs

    try:
        whitelist = parse_page_spec(os.getenv("PARTS_PAGES", ""))
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                if whitelist and page.page_number not in whitelist:
                    continue
                text = page.extract_text() or ""
                tables = page.extract_tables() or []
                if not tables:
                    continue
                for tbl in tables:
                    if not tbl:
                        continue
                    header = [((cell or '').strip().lower()) for cell in tbl[0]] if tbl else []
                    rows = tbl[1:] if header else tbl
                    for row in rows:
                        cells = [(cell or '').strip() for cell in row]
                        if not any(cells):
                            continue
                        text_repr = " | ".join(cells)
                        if not text_repr:
                            continue
                        meta = {
                            "doc_id": doc_id,
                            "source": "table",
                            "page_label": str(page.page_number),
                            "table_row": text_repr,
                        }
                        docs.append(Document(page_content=text_repr, metadata=meta))
    except Exception:
        return docs
    return docs


def chunk_documents(documents: List[Document], doc_id: str, chunk_size: int, chunk_overlap: int, brand: str, model: str) -> List[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap, length_function=len)
    chunks = splitter.split_documents(documents)
    for idx, chunk in enumerate(chunks):
        meta = dict(chunk.metadata or {})
        meta.setdefault("doc_id", doc_id)
        meta["chunk_index"] = idx
        meta.setdefault("brand", brand)
        meta.setdefault("model", model)
        chunk.metadata = meta
    for idx, chunk in enumerate(chunks):
        prev_text = chunks[idx - 1].page_content if idx > 0 else ""
        next_text = chunks[idx + 1].page_content if idx < len(chunks) - 1 else ""
        chunk.metadata["prev_window"] = prev_text[:200]
        chunk.metadata["next_window"] = next_text[:200]
    return chunks


def build_bm25(chunks: List[Document], output_file: Path, build_ts: str, ingest_version: str):
    print("Generando índice BM25 ...")
    tokenized = [doc.page_content.split() for doc in chunks]
    bm25 = BM25Okapi(tokenized)
    payload = {
        "bm25": bm25,
        "chunks": chunks,
        "build_ts": build_ts,
        "ingest_version": ingest_version,
    }
    with open(output_file, "wb") as fh:
        pickle.dump(payload, fh)
    print(f"Índice BM25 guardado en '{output_file}'.")


def ensure_pinecone_index(pc: PineconeClient, index_name: str, dimension: int, incremental: bool, recreate: bool, metric: str = "cosine"):
    existing = index_name in pc.list_indexes().names()

    def wait_deleted(name: str, timeout: int = 180) -> None:
        start = time.time()
        while time.time() - start < timeout:
            try:
                if name not in pc.list_indexes().names():
                    return
            except Exception:
                pass
            time.sleep(2)

    def wait_ready(name: str, timeout: int = 240) -> None:
        start = time.time()
        while time.time() - start < timeout:
            try:
                desc = pc.describe_index(name)
                status = desc.get("status", {}) if isinstance(desc, dict) else getattr(desc, "status", {})
                ready = False
                if isinstance(status, dict):
                    ready = bool(status.get("ready")) or (status.get("state") == "Ready")
                if ready:
                    return
            except Exception:
                pass
            time.sleep(2)

    if incremental and existing and not recreate:
        return pc.Index(index_name)

    if incremental and not existing and not recreate:
        print(f"El índice '{index_name}' no existe. Creándolo...")
    elif recreate or not incremental:
        if existing:
            print(f"Recreando índice '{index_name}' ...")
            pc.delete_index(index_name)
            wait_deleted(index_name)
        else:
            print(f"Creando índice '{index_name}' ...")

    try:
        region, cloud = os.getenv("PINECONE_ENV", "us-east-1-aws").rsplit('-', 1)
    except ValueError as err:
        raise RuntimeError(f"PINECONE_ENV inválido: {os.getenv('PINECONE_ENV')} ({err})")

    pc.create_index(
        name=index_name,
        dimension=dimension,
        metric=metric,
        spec=ServerlessSpec(cloud=cloud, region=region),
    )
    wait_ready(index_name)
    return pc.Index(index_name)


def prepare_pdf_path(pdf: str) -> Path:
    pdf_path = Path(pdf)
    if pdf_path.exists():
        return pdf_path
    alt = ROOT_DIR / pdf
    if alt.exists():
        return alt
    raise FileNotFoundError(f"No se encontró el PDF en '{pdf}'")


def _extract_page_number(meta: dict) -> Optional[int]:
    if not isinstance(meta, dict):
        return None
    for key in ("page", "page_number", "page_index", "page_label"):
        value = meta.get(key)
        if value is None:
            continue
        try:
            if key == "page_index":
                num = int(value) + 1
            else:
                num = int(str(value).strip())
            if num <= 0:
                continue
            return num
        except Exception:
            continue
    return None


def _filter_documents_by_pages(documents: List[Document], pages: set[int]) -> List[Document]:
    if not pages:
        return documents
    filtered: List[Document] = []
    for doc in documents:
        page = _extract_page_number(getattr(doc, "metadata", {}))
        if page and page in pages:
            filtered.append(doc)
    return filtered


def main(cli_args: Optional[List[str]] = None):
    _load_env()
    args = _parse_args(cli_args)

    pdf_path = prepare_pdf_path(args.pdf)
    doc_id = args.doc_id or pdf_path.stem

    brand_name = os.getenv("BRAND_NAME", "Higer")
    model_name = os.getenv("MODEL_NAME", "H6C")
    ingest_version = args.ingest_version or os.getenv("APP_VERSION") or "dev"
    build_ts = datetime.now(timezone.utc).isoformat()

    print(f"Iniciando ingesta para '{pdf_path}' (doc_id={doc_id})")
    docs = load_documents(pdf_path, use_ocr=args.ocr)
    table_docs = extract_table_docs(pdf_path, doc_id)
    pages_filter = parse_page_spec(args.pages) if args.pages else None
    if pages_filter:
        docs = _filter_documents_by_pages(docs, pages_filter)
        table_docs = _filter_documents_by_pages(table_docs, pages_filter)
    if table_docs:
        print(f"Se añadieron {len(table_docs)} filas de tablas detectadas")
        docs.extend(table_docs)

    chunks = chunk_documents(docs, doc_id, args.chunk_size, args.chunk_overlap, brand_name, model_name)
    if not chunks:
        raise RuntimeError("No se generaron chunks para la ingesta")
    print(f"Generados {len(chunks)} chunks de texto")

    bm25_path = Path(args.bm25_index_file)
    build_bm25(chunks, bm25_path, build_ts, ingest_version)

    if args.bm25_only:
        print("Modo --bm25-only activo: se omite escritura en Pinecone")
        return

    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY no configurado")
    PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
    if not PINECONE_API_KEY:
        raise RuntimeError("PINECONE_API_KEY no configurado")

    embeddings_model = os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-small")
    embeddings = OpenAIEmbeddings(model=embeddings_model, openai_api_key=OPENAI_API_KEY)
    pc = PineconeClient(api_key=PINECONE_API_KEY)
    dim = _embeddings_dim(embeddings_model)
    index = ensure_pinecone_index(pc, args.index_name, dim, incremental=args.incremental, recreate=args.recreate)

    if args.recreate and args.incremental:
        print("Opción --recreate tiene prioridad: se recreó el índice antes de la ingesta.")

    if (args.incremental and not args.recreate) and doc_id:
        try:
            delete_filter: dict = {"doc_id": {"$eq": doc_id}}
            if pages_filter:
                delete_filter["page_label"] = {"$in": [str(p) for p in sorted(pages_filter)]}
            index.delete(filter=delete_filter)
            if pages_filter:
                print(f"Eliminadas entradas previas de doc_id='{doc_id}' páginas={sorted(pages_filter)}")
            else:
                print(f"Eliminadas entradas previas de doc_id='{doc_id}' antes de la reingesta")
        except Exception as exc:
            print(f"[Aviso] No se pudo limpiar doc_id='{doc_id}' ({exc})")

    print(f"Subiendo {len(chunks)} chunks a Pinecone (index={args.index_name}) ...")
    batch_size = 100
    for start in tqdm(range(0, len(chunks), batch_size), desc="Pinecone upsert"):
        batch = chunks[start:start + batch_size]
        ids = [f"{doc_id}-{chunk.metadata.get('chunk_index', start + offset)}" for offset, chunk in enumerate(batch)]
        texts = [chunk.page_content for chunk in batch]
        vectors = embeddings.embed_documents(texts)
        metadata_batch = []
        for chunk in batch:
            meta = {k: v for k, v in chunk.metadata.items() if isinstance(v, (str, int, float, bool))}
            meta.setdefault("doc_id", doc_id)
            meta.setdefault("brand", brand_name)
            meta.setdefault("model", model_name)
            page_num = _extract_page_number(chunk.metadata)
            if page_num is not None:
                meta.setdefault("page_label", str(page_num))
            meta["build_ts"] = build_ts
            meta["ingest_version"] = ingest_version
            meta["text"] = chunk.page_content
            metadata_batch.append(meta)
        index.upsert(vectors=zip(ids, vectors, metadata_batch))

    try:
        stats = index.describe_index_stats()
        total = stats.get("total_vector_count") if isinstance(stats, dict) else None
        print(f"Ingesta completa. Vectores totales en '{args.index_name}': {total}")
    except Exception:
        print("Ingesta completada (estadísticas no disponibles).")


if __name__ == "__main__":
    main()
