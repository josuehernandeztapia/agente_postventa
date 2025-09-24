import os
from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_pinecone import Pinecone
from langchain.chains import RetrievalQA

# Load environment variables from .env file
load_dotenv()

# ====== CONFIGURACIÓN ======
# LangChain will automatically use the environment variables for API keys

INDEX_NAME = os.getenv("PINECONE_INDEX", "ssot-higer")

# ====== SCRIPT DE CONSULTA ======

# 1. Initialize Embeddings and LLM
import os as _os
embeddings = OpenAIEmbeddings(model=_os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-small"))
llm = ChatOpenAI(model=_os.getenv("LLM_MODEL", "gpt-4o"))

# 2. Connect to the existing Pinecone index
vectorstore = Pinecone.from_existing_index(index_name=INDEX_NAME, embedding=embeddings)

# 3. Create a retrieval chain
qa_chain = RetrievalQA.from_chain_type(
    llm=llm,
    chain_type="stuff",
    retriever=vectorstore.as_retriever()
)

# 4. Ask a question
# --- MODIFICA AQUÍ TU PREGUNTA ---
query = "¿Cuál es el tema principal del documento?"
# ---------------------------------

print(f"Pregunta: {query}")
print("Buscando respuesta...")

# 5. Get and print the answer
result = qa_chain.invoke({"query": query})
answer = result.get("result", "")
print(f"\nRespuesta: {answer}")
