# Este script se ejecuta una sola vez para crear y guardar el índice BM25.
import os
from langchain_community.document_loaders import PyMuPDFLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
import pickle
from dotenv import load_dotenv

# --- Requisito ---
# Antes de ejecutar, instala la librería necesaria:
# pip install rank_bm25

try:
    from rank_bm25 import BM25Okapi
except ImportError:
    print("Error: La librería 'rank_bm25' no está instalada.")
    print("Por favor, ejecuta: pip install rank_bm25")
    exit()

# --- Configuración ---
load_dotenv()
PDF_FILE = os.getenv("PDF_FILE", "SSOT-HIGER.pdf")
BM25_INDEX_FILE = os.getenv("BM25_INDEX_FILE", "bm25_index.pkl")

def create_and_save_bm25_index():
    """
    Carga el PDF, lo divide en chunks y crea un índice BM25 que se guarda en un archivo.
    """
    from pathlib import Path
    script_dir = Path(__file__).parent
    # Resolver ruta del PDF
    pdf_path = Path(PDF_FILE)
    if not pdf_path.exists():
        alt = script_dir / PDF_FILE
        if alt.exists():
            pdf_path = alt
        else:
            alt2 = script_dir / 'SSOT-HIGER.pdf'
            if alt2.exists():
                pdf_path = alt2
            else:
                raise FileNotFoundError(f"No se encontró el PDF en '{PDF_FILE}' ni en '{alt}'")

    print(f"Cargando documento: {pdf_path}")
    loader = PyMuPDFLoader(str(pdf_path))
    documents = loader.load()

    print("Creando chunks de texto...")
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        length_function=len,
    )
    chunks = text_splitter.split_documents(documents)
    
    # Extraer el contenido de texto de los chunks
    tokenized_corpus = [doc.page_content.split(" ") for doc in chunks]
    
    print("Creando el índice BM25...")
    bm25 = BM25Okapi(tokenized_corpus)

    # Guardar el índice y los documentos originales para referencia futura
    out_path = Path(BM25_INDEX_FILE)
    if not out_path.is_absolute():
        out_path = script_dir / out_path
    with open(out_path, 'wb') as f:
        pickle.dump({'bm25': bm25, 'chunks': chunks}, f)

    print(f"\n¡Éxito! Índice BM25 y {len(chunks)} chunks guardados en: {out_path}")
    print("Ahora puedes usar 'query_mejorado.py' para realizar búsquedas híbridas.")

if __name__ == "__main__":
    create_and_save_bm25_index()
