
from fastapi import Request
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from app.router_graph import router_graph
from app.deps import get_vs, get_embeddings
from app.ingestion.loader import load_single_file, split_with_visibility, load_docs, split_docs
from app.config import settings
import time
import uuid
from pathlib import Path
from typing import Optional
import chromadb


app = FastAPI(title="Enterprise KB Assistant")
DATA_DOCS_DIR = Path("./data/docs")
SESSIONS: dict[str, dict] = {}
DATA_DOCS_DIR.mkdir(parents=True, exist_ok=True)


class ChatReq(BaseModel):
    text: str
    user_role: str = "public"
    requester: str = "anonymous"
    mode: Optional[str] = None
    session_id: Optional[str] = None

class ChatResp(BaseModel):
    answer: str

@app.post("/chat", response_model=ChatResp)
def chat(req: ChatReq):
    payload = req.model_dump()
    sid = payload.get("session_id")

    if sid and sid in SESSIONS:
        prev = SESSIONS[sid]
        merged = {**prev, **payload}
        merged["text"] = payload.get("text")
        payload = merged

    out = router_graph.invoke(payload)

    if sid:
        SESSIONS[sid] = {**payload, **out}

    return {"answer": out["answer"]}


@app.post("/ingest")
async def ingest(file: UploadFile = File(...),
                 visibility: str = Form("public"),
                 doc_id: Optional[str] = Form(None)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Empty filename")

    visibility = (visibility or "public").strip().lower()

    suffix = Path(file.filename).suffix
    safe_name = f"{int(time.time())}_{uuid.uuid4().hex}{suffix}"
    save_path = DATA_DOCS_DIR / safe_name

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    save_path.write_bytes(content)

    docs = load_single_file(save_path)
    if not docs:
        raise HTTPException(status_code=400, detail=f"Unsupported or empty file type: {suffix}")

    chunks = split_with_visibility(docs, visibility=visibility, doc_id=doc_id)

    vs = get_vs()
    vs.add_documents(chunks)

    return {
        "saved_as": str(save_path),
        "visibility": visibility,
        "doc_id": doc_id,
        "chunks": len(chunks),
    }

@app.post("/reindex")
def reindex(visibility_default: str = Form("public")):
    visibility_default = (visibility_default or "public").strip().lower()

    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    try:
        client.delete_collection(settings.collection_name)
    except Exception:
        print('================删除chromadb报错了')
    client.get_or_create_collection(settings.collection_name)

    vs = get_vs()
    raw_docs = load_docs(str(DATA_DOCS_DIR))
    if not raw_docs:
        return {"chunks": 0, "docs": 0, "message": "No documents found in data/docs"}

    chunks = split_docs(raw_docs)
    for c in chunks:
        c.metadata = dict(c.metadata or {})
        c.metadata.setdefault("visibility", visibility_default)

    vs.add_documents(chunks)

    return {"docs": len(raw_docs), "chunks": len(chunks), "visibility_default": visibility_default}


@app.get("/")
def root():
    return {"status": "ok", "docs": "/docs"}