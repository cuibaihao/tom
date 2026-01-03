import uvicorn

from app.api.auth_api import get_current_user
from app.db import kb_db
from app.model.auth_model import UserInDB
from app.secutiry.rbac.perm import check_permission
from app.api.kb_api import router as kb_router, normalize_visibility
from app.api.auth_api import router as auth_router


from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from pydantic import BaseModel
from app.router_graph import router_graph
from app.deps import get_vs, get_embeddings
from app.ingestion.loader import load_single_file, split_with_visibility, load_docs, split_docs
from app.db.redis_session import load_session, save_session
from app.config import settings
import time
import uuid
from pathlib import Path
from typing import Optional
import chromadb
from app.api.audio_api import router as audio_router


app = FastAPI(title="Enterprise KB Assistant")



app.include_router(kb_router)
app.include_router(auth_router)

app.include_router(audio_router)

DATA_DOCS_DIR = Path("./data/docs")
SESSIONS: dict[str, dict] = {}
DATA_DOCS_DIR.mkdir(parents=True, exist_ok=True)

@app.post("/ingest")
async def ingest(
    file: UploadFile = File(...),
    visibility: str = Form("public"),
    doc_id: Optional[str] = Form(None),
    overwrite: bool = Form(False),
    delete_old_file: bool = Form(False),
    current_user: UserInDB = Depends(get_current_user),
):
    check_permission(current_user, "kb.manage_docs")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Empty filename")

    visibility = normalize_visibility(visibility or "public")

    doc_id = (doc_id or f"doc-{uuid.uuid4().hex[:12]}").strip()

    existed = kb_db.get_kb_document(doc_id)
    if existed and not overwrite:
        raise HTTPException(status_code=409, detail=f"doc_id already exists: {doc_id}")

    old_path = existed["stored_path"] if existed else None

    # 1) 先把新文件保存下来
    suffix = Path(file.filename).suffix
    safe_name = f"{int(time.time())}_{uuid.uuid4().hex}{suffix}"
    save_path = DATA_DOCS_DIR / safe_name

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    save_path.write_bytes(content)

    # 2) 先解析新文件、切分出 chunks（确保新文件 OK）
    docs = load_single_file(save_path)
    if not docs:
        raise HTTPException(status_code=400, detail=f"Unsupported or empty file type: {suffix}")

    extra_meta = {
        "original_filename": file.filename,
        "stored_path": str(save_path),
        "uploader_user_id": current_user.id,
        "uploader_username": current_user.username,
        "uploaded_at": int(time.time()),
    }
    chunks = split_with_visibility(docs, visibility=visibility, doc_id=doc_id, extra_meta=extra_meta)

    # 3) 如果 overwrite：现在再删旧的 chroma chunks（此时新 chunks 已经准备好）
    if existed and overwrite:
        from app.rag.chroma_admin import delete_by_doc_id
        delete_by_doc_id(doc_id)

    # 4) 写入向量库
    vs = get_vs()
    vs.add_documents(chunks)
    try:
        vs.persist()
    except Exception:
        pass

    # 5) 更新注册表
    from app.rag.chroma_admin import count_by_doc_id
    chroma_cnt = count_by_doc_id(doc_id)

    kb_db.upsert_kb_document(
        doc_id=doc_id,
        original_filename=file.filename,
        stored_path=str(save_path),
        visibility=visibility,
        uploader_user_id=current_user.id,
        uploader_username=current_user.username,
        chunk_count=chroma_cnt,
    )

    # 6) overwrite 时可选删除旧文件（最后一步做）
    deleted_old_file = False
    if delete_old_file and old_path and old_path != str(save_path):
        try:
            p = Path(old_path)
            if p.exists() and p.is_file():
                p.unlink()
                deleted_old_file = True
        except Exception:
            deleted_old_file = False

    return {
        "saved_as": str(save_path),
        "visibility": visibility,
        "doc_id": doc_id,
        "chunks": chroma_cnt,
        "overwrote": bool(existed and overwrite),
        "deleted_old_file": deleted_old_file,
    }



class ChatReq(BaseModel):
    text: str
    user_role: str = "public"
    requester: str = "anonymous"
    mode: Optional[str] = None
    session_id: Optional[str] = None

class ChatResp(BaseModel):
    answer: str
    session_id: Optional[str] = None    # ⚠️添加
    active_route: Optional[str] = None  # ⚠️添加

@app.post("/chat", response_model=ChatResp)
def chat(req: ChatReq):
    payload = req.model_dump()
    text = payload.get("text") or payload.get("question") or ""

    # 1) get or create session id
    sid = payload.get("session_id") or f"sid-{uuid.uuid4().hex[:10]}"
    payload["session_id"] = sid

    # 2) load previous state from redis and merge
    prev_state = load_session(sid)
    if prev_state:
        merged = {**prev_state, **payload}
        merged["text"] = text
        payload = merged

    # 3) run router graph
    out = router_graph.invoke(payload)

    # 4) save new state to redis
    new_state = {**payload, **out}
    save_session(sid, new_state)

    return {
        "answer": out.get("answer"),
        "session_id": sid,
        "active_route": new_state.get("active_route"),
    }


@app.post("/ingest")
async def ingest(
    file: UploadFile = File(...),
    visibility: str = Form("public"),
    doc_id: Optional[str] = Form(None),
    overwrite: bool = Form(False),
    delete_old_file: bool = Form(False),
    current_user: UserInDB = Depends(get_current_user),
):
    check_permission(current_user, "kb.manage_docs")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Empty filename")

    visibility = (visibility or "public").strip().lower()
    doc_id = (doc_id or f"doc-{uuid.uuid4().hex[:12]}").strip()

    existed = kb_db.get_kb_document(doc_id)
    if existed and not overwrite:
        raise HTTPException(status_code=409, detail=f"doc_id already exists: {doc_id}")

    old_path = existed["stored_path"] if existed else None

    # 1) 先把新文件保存下来
    suffix = Path(file.filename).suffix
    safe_name = f"{int(time.time())}_{uuid.uuid4().hex}{suffix}"
    save_path = DATA_DOCS_DIR / safe_name

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    save_path.write_bytes(content)

    # 2) 先解析新文件、切分出 chunks（确保新文件 OK）
    docs = load_single_file(save_path)
    if not docs:
        raise HTTPException(status_code=400, detail=f"Unsupported or empty file type: {suffix}")

    extra_meta = {
        "original_filename": file.filename,
        "stored_path": str(save_path),
        "uploader_user_id": current_user.id,
        "uploader_username": current_user.username,
        "uploaded_at": int(time.time()),
    }
    chunks = split_with_visibility(docs, visibility=visibility, doc_id=doc_id, extra_meta=extra_meta)

    # 3) 如果 overwrite：现在再删旧的 chroma chunks（此时新 chunks 已经准备好）
    if existed and overwrite:
        from app.rag.chroma_admin import delete_by_doc_id
        delete_by_doc_id(doc_id)

    # 4) 写入向量库
    vs = get_vs()
    vs.add_documents(chunks)
    try:
        vs.persist()
    except Exception:
        pass

    # 5) 更新注册表
    from app.rag.chroma_admin import count_by_doc_id
    chroma_cnt = count_by_doc_id(doc_id)

    kb_db.upsert_kb_document(
        doc_id=doc_id,
        original_filename=file.filename,
        stored_path=str(save_path),
        visibility=visibility,
        uploader_user_id=current_user.id,
        uploader_username=current_user.username,
        chunk_count=chroma_cnt,
    )

    # 6) overwrite 时可选删除旧文件（最后一步做）
    deleted_old_file = False
    if delete_old_file and old_path and old_path != str(save_path):
        try:
            p = Path(old_path)
            if p.exists() and p.is_file():
                p.unlink()
                deleted_old_file = True
        except Exception:
            deleted_old_file = False

    return {
        "saved_as": str(save_path),
        "visibility": visibility,
        "doc_id": doc_id,
        "chunks": chroma_cnt,
        "overwrote": bool(existed and overwrite),
        "deleted_old_file": deleted_old_file,
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


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8002, reload=True)