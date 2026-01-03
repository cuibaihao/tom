from pydantic import BaseModel
from dotenv import load_dotenv
import os


load_dotenv()

class Settings(BaseModel):
    base_url: str = "https://api.deepseek.com/v1"
    openai_api_key: str = os.getenv("deepseek_api_key", "")
    zhipuai_api_key: str = os.getenv("ZHIPUAI_API_KEY", "")
    model_name: str = os.getenv("MODEL_NAME", "deepseek-chat")
    chroma_dir: str = os.getenv("CHROMA_DIR", "./data/chroma")
    chroma_host: str = os.getenv("CHROMA_HOST", "localhost")
    chroma_port: int = int(os.getenv("CHROMA_PORT", "8000"))
    collection_name: str = os.getenv("COLLECTION_NAME", "knowledge_base")
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "800"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "120"))

settings = Settings()

audio_collection_name:str = os.getenv("AUDIO_COLLECTION_NAME", "audio_base")

