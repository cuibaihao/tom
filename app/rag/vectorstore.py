import chromadb
from langchain_chroma import Chroma
from app.config import settings

def get_vectorstore(embeddings):
    client = chromadb.HttpClient(
        host=settings.chroma_host,
        port=settings.chroma_port
    )
    return Chroma(
        client=client,
        collection_name=settings.collection_name,
        embedding_function=embeddings,
    )