from __future__ import annotations

from app.secutiry.kb_visibility import compute_allowed_kb_visibilities
from app.secutiry.rbac.perm import check_permission
from app.audio.audio_loader import ffprobe_duration_ms, transcode_to_wav_16k_mono
from app.audio.asr import ASR
from app.audio.segmenter import merge_by_max_duration

from app.rag.retrieve_audio import audio_similarity_search_for_user


from langchain_core.documents import Document



import time, uuid
from pathlib import Path
from typing import List, Optional

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile,)
from fastapi.responses import FileResponse

from app.api.auth_api import UserInDB, get_current_user
from app.audio.clip import clip_audio_to_mp3
from app.config import settings
from app.db import audio_db, audio_job_db
from app.deps import get_audio_vs
from app.model.audio_model import AudioIngestAsyncResp, AudioJobResp, AudioDocDetail, AudioSearchResp, AudioSearchHit, \
    AudioIngestResp
from app.secutiry.rbac.perm import check_permission, allowed_kb_visibilities
from app.tasks.audio_tasks import audio_ingest_task





AUDIO_WAV_DIR = Path("data/audio_wav")


router = APIRouter(prefix="/audio", tags=["audio"])

AUDIO_DIR = Path(getattr(settings, "audio_dir", "data/audio"))
CLIP_DIR = Path(getattr(settings, "audio_clip_dir", "data/audio_clips"))


@router.post("/ingest", response_model=AudioIngestResp)
async def ingest_audio(
    file: UploadFile = File(...),
    visibility: str = Form("public"),
    audio_id: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
    current_user: UserInDB = Depends(get_current_user),
):
    check_permission(current_user, "kb.manage_docs")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Empty filename")

    visibility = ["public"]
    audio_id = (audio_id or f"aud-{uuid.uuid4().hex[:12]}").strip()

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename).suffix or ".bin"
    raw_path = AUDIO_DIR / f"{int(time.time())}_{uuid.uuid4().hex}{suffix}"
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    raw_path.write_bytes(raw_bytes)

    AUDIO_WAV_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = AUDIO_WAV_DIR / f"{audio_id}.wav"
    transcode_to_wav_16k_mono(raw_path, wav_path)

    duration_ms = ffprobe_duration_ms(wav_path)

    asr = ASR(model_name="base", device="cpu", compute_type="int8")
    asr_segs, detected_lang = asr.transcribe(str(wav_path), language=language)
    lang = language or detected_lang

    chunks = merge_by_max_duration(asr_segs, max_ms=25_000, min_ms=6_000)

    vs = get_audio_vs()
    docs: list[Document] = []
    segment_rows: list[dict] = []
    for idx, c in enumerate(chunks):
        seg_id = f"{audio_id}:{idx}"
        text = c.text.strip()
        if not text:
            continue

        meta = {
            "doc_type": "audio",
            "audio_id": audio_id,
            "segment_id": seg_id,
            "segment_idx": idx,
            "start_ms": c.start_ms,
            "end_ms": c.end_ms,
            "visibility": visibility,
            "original_filename": file.filename,
            "stored_path": str(raw_path),
            "wav_path": str(wav_path),
            "language": lang,
        }
        docs.append(Document(page_content=text, metadata=meta))
        segment_rows.append(
            {"segment_idx": idx, "start_ms": c.start_ms, "end_ms": c.end_ms, "text": text}
        )

    if not docs:
        raise HTTPException(status_code=400, detail="No transcript produced")
    vs.add_documents(docs)

    # 7) 落库
    audio_db.upsert_audio_document(
        audio_id=audio_id,
        original_filename=file.filename,
        stored_path=str(raw_path),
        duration_ms=duration_ms,
        language=lang,
        visibility=visibility,
        status="indexed",
        uploader_user_id=int(current_user.id),
        uploader_username=current_user.username,
        segment_count=len(segment_rows),
    )
    audio_db.replace_audio_segments(audio_id, segment_rows)

    return AudioIngestResp(
        audio_id=audio_id,
        stored_as=str(raw_path),
        duration_ms=duration_ms,
        language=lang,
        visibility=visibility,
        segments=len(segment_rows),
    )

@router.get("/detail/{audio_id}", response_model=AudioDocDetail)
def get_audio(audio_id: str, current_user: UserInDB = Depends(get_current_user)):
    check_permission(current_user, "kb.manage_docs")
    row = audio_db.get_audio_document(audio_id)
    if not row:
        raise HTTPException(status_code=404, detail="audio not found")
    return row


@router.get("/search", response_model=AudioSearchResp)
def search_audio(
    request: Request,
    q: str = Query(..., min_length=1),
    k: int = Query(default=6, ge=1, le=20),
    current_user: UserInDB = Depends(get_current_user),
):
    docs, allowed = audio_similarity_search_for_user(q, current_user, k=k)

    base = str(request.base_url).rstrip("/")  # e.g. http://127.0.0.1:8002

    hits: list[AudioSearchHit] = []
    for d in docs:
        m = d.metadata or {}
        audio_id = str(m.get("audio_id", "") or "")
        s = int(m.get("start_ms", 0) or 0)
        e = int(m.get("end_ms", 0) or 0)

        clip_url = None
        if audio_id and e > s:
            clip_url = f"{base}/audio/{audio_id}/clip?start_ms={s}&end_ms={e}"

        hits.append(
            AudioSearchHit(
                audio_id=audio_id,
                segment_id=str(m.get("segment_id", "") or ""),
                start_ms=s,
                end_ms=e,
                text=d.page_content,
                score=None,
                clip_url=clip_url,
            )
        )

    return AudioSearchResp(q=q, k=k, allowed_visibilities=allowed, hits=hits)



@router.get("/{audio_id}/clip")
def get_audio_clip(
    audio_id: str,
    background_tasks: BackgroundTasks,
    start_ms: Optional[int] = Query(default=None, ge=0),
    end_ms: Optional[int] = Query(default=None, ge=0),
    segment_id: Optional[str] = Query(default=None),  # e.g. aud-xxxx:3
    current_user: UserInDB = Depends(get_current_user),
):
    """
    返回裁剪后的mp3片段，优先segment_id（从DB取 start/end），否则必须提供 start_ms + end_ms
    """
    # 1) 读取 audio doc
    row = audio_db.get_audio_document(audio_id)
    if not row:
        raise HTTPException(status_code=404, detail="audio not found")

    # 2) 可见性校验：audio_documents.visibility 必须在allowed中
    allowed = compute_allowed_kb_visibilities(current_user)
    doc_vis = (row.get("visibility") or "").strip().lower()
    if doc_vis not in set(allowed):
        raise HTTPException(status_code=403, detail="no permission to access this audio")

    # 3) 确定裁剪区间
    if segment_id:
        if ":" not in segment_id:
            raise HTTPException(status_code=400, detail="invalid segment_id format")
        seg_audio_id, seg_idx_str = segment_id.split(":", 1)
        if seg_audio_id != audio_id:
            raise HTTPException(status_code=400, detail="segment_id does not match audio_id")
        try:
            seg_idx = int(seg_idx_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid segment_idx in segment_id")

        seg = audio_db.get_audio_segment(audio_id, seg_idx)
        if not seg:
            raise HTTPException(status_code=404, detail="segment not found")

        start_ms = int(seg["start_ms"])
        end_ms = int(seg["end_ms"])
    else:  # 如果没有提供segment_id就必须提供开始和结束时间，可以剪辑任意范围内的音频，如果都没提供出异常
        if start_ms is None or end_ms is None:
            raise HTTPException(status_code=400, detail="start_ms and end_ms are required when segment_id is not provided")

    # 4) 合法性限制（防止一次裁太长）
    if end_ms <= start_ms:
        raise HTTPException(status_code=400, detail="end_ms must be greater than start_ms")
    max_clip_ms = 5 * 60 * 1000  # 5 分钟上限（你可以改）
    if (end_ms - start_ms) > max_clip_ms:
        raise HTTPException(status_code=400, detail="clip too long")

    # 5) 源文件路径（v0 我们从 raw stored_path 裁剪，兼容各种格式）
    src_path = Path(row["stored_path"])
    if not src_path.exists():
        raise HTTPException(status_code=404, detail="stored audio file missing")

    # 6) 生成 clip 文件
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    clip_name = f"{audio_id}_{start_ms}_{end_ms}_{uuid.uuid4().hex[:8]}.mp3"
    # 以后担心文件重名一般就是年月日时分秒毫秒+uuid
    clip_path = CLIP_DIR / clip_name

    try:
        clip_audio_to_mp3(
            src_path=src_path,
            dst_path=clip_path,
            start_ms=int(start_ms),
            end_ms=int(end_ms),
        )
    except Exception:
        raise HTTPException(status_code=500, detail="failed to generate clip")

    # 7) 返回并后台删除临时文件
    background_tasks.add_task(lambda p=str(clip_path): Path(p).unlink(missing_ok=True))

    return FileResponse(
        path=str(clip_path),
        media_type="audio/mpeg",
        filename=clip_name,
    )


def _require_manage_docs(user: UserInDB) -> None:
    check_permission(user, "kb.manage_docs")


def _normalize_visibility(v: str) -> str:
    v = (v or "").strip().lower()
    if v in ("public", "internal"):
        return v
    return "public"


def _compute_allowed_visibilities(user: UserInDB) -> List[str]:
    perms = getattr(user, "permissions", None)
    allowed = allowed_kb_visibilities(perms)
    if "public" not in allowed:
        allowed = ["public"] + [x for x in allowed if x != "public"]
    return allowed


def _ensure_can_access_visibility(user: UserInDB, doc_visibility: str) -> List[str]:
    allowed = _compute_allowed_visibilities(user)
    vis = (doc_visibility or "").strip().lower()
    if vis not in set(allowed):
        raise HTTPException(status_code=403, detail="no permission to access this audio")
    return allowed


def _absolute_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")



@router.post("/ingest", response_model=AudioIngestAsyncResp)
async def ingest_audio(
    file: UploadFile = File(...),
    visibility: str = Form("public"),
    audio_id: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
    overwrite: bool = Form(False),
    delete_old_file: bool = Form(False),
    current_user: UserInDB = Depends(get_current_user),):
    _require_manage_docs(current_user)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Empty filename")

    visibility = _normalize_visibility(visibility or "public")
    audio_id = (audio_id or f"aud-{uuid.uuid4().hex[:12]}").strip()
    job_id = f"job-{uuid.uuid4().hex[:12]}"

    if audio_db.is_audio_running(audio_id):
        raise HTTPException(status_code=409, detail="audio is running, try later")

    existed = audio_db.get_audio_document(audio_id)
    if existed and not overwrite:
        raise HTTPException(status_code=409, detail="audio_id already exists; set overwrite=true")

    old_stored_path = existed["stored_path"] if existed else None

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename).suffix or ".bin"
    raw_path = AUDIO_DIR / f"{int(time.time())}_{uuid.uuid4().hex}{suffix}"
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    raw_path.write_bytes(raw_bytes)

    audio_db.upsert_audio_document(
        audio_id=audio_id,
        original_filename=file.filename,
        stored_path=str(raw_path),
        duration_ms=0,
        language=language,
        visibility=visibility,
        status="queued",
        uploader_user_id=int(getattr(current_user, "id", 0) or 0) or None,
        uploader_username=getattr(current_user, "username", None),
        segment_count=0,
    )

    audio_job_db.create_job(
        job_id,
        audio_id,
        overwrite=bool(overwrite),
        delete_old_file=bool(delete_old_file),
        old_stored_path=old_stored_path if overwrite else None,
    )

    async_result = audio_ingest_task.apply_async(
        # audio_ingest_task是我们celery的的任务
        # 把一个task放进broker队列里，让worker之后去执行。调用这个方不会阻塞等待执行结果
        args=[job_id, audio_id],
        queue=getattr(settings, "celery_audio_queue", "audio"),
    )
    audio_job_db.bind_task(job_id, async_result.id)

    return AudioIngestAsyncResp(
        job_id=job_id,
        audio_id=audio_id,
        stored_as=str(raw_path),
        visibility=visibility,
        celery_task_id=async_result.id,
        status_url=f"/audio/jobs/{job_id}",
    )


@router.get("/jobs/{job_id}", response_model=AudioJobResp)
def get_audio_job(
    job_id: str,
    current_user: UserInDB = Depends(get_current_user),
):
    _require_manage_docs(current_user)
    row = audio_job_db.get_job(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="job not found")
    return row


@router.post("/jobs/{job_id}/cancel")
def cancel_audio_job(
    job_id: str,
    current_user: UserInDB = Depends(get_current_user),
):
    _require_manage_docs(current_user)
    ok = audio_job_db.request_cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": job_id, "cancel_requested": True}


@router.get("/docs/{audio_id}", response_model=AudioDocDetail)
def get_audio_doc(
    audio_id: str,
    current_user: UserInDB = Depends(get_current_user),
):
    _require_manage_docs(current_user)
    row = audio_db.get_audio_document(audio_id)
    if not row:
        raise HTTPException(status_code=404, detail="audio not found")
    return row


@router.get("/query", response_model=AudioSearchResp)
def query_audio(
    request: Request,
    q: str = Query(..., min_length=1),
    k: int = Query(6, ge=1, le=20),
    current_user: UserInDB = Depends(get_current_user),) -> AudioSearchResp:
    allowed_vis = _compute_allowed_visibilities(current_user)

    vs = get_audio_vs()
    where = {"visibility": {"$in": allowed_vis}}

    try: # 版本问题，新版本我也忘了是哪个了，留一个就行其实，好像是where
        docs_scores = vs.similarity_search_with_score(q, k=k, filter=where)
    except TypeError:
        docs_scores = vs.similarity_search_with_score(q, k=k, where=where)

    hits: list[AudioSearchHit] = []
    base = _absolute_base(request)

    for doc, score in docs_scores:
        md = doc.metadata or {}
        audio_id = str(md.get("audio_id") or "")
        segment_id = str(md.get("segment_id") or "")
        start_ms = int(md.get("start_ms") or 0)
        end_ms = int(md.get("end_ms") or 0)
        text = (doc.page_content or "").strip()

        if not audio_id or not segment_id:
            continue

        clip_url = f"{base}/audio/docs/{audio_id}/clip?start_ms={start_ms}&end_ms={end_ms}"

        hits.append(
            AudioSearchHit(
                audio_id=audio_id,
                segment_id=segment_id,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                score=float(score) if score is not None else None,
                clip_url=clip_url,
            )
        )

    return AudioSearchResp(q=q, k=k, allowed_visibilities=allowed_vis, hits=hits)


@router.get("/docs/{audio_id}/clip")
def get_audio_clip(
    audio_id: str,
    background_tasks: BackgroundTasks, # 后台任务，和多线程不太一样，可以被当作轻量的多线程
    start_ms: Optional[int] = Query(default=None, ge=0),
    end_ms: Optional[int] = Query(default=None, ge=0),
    segment_id: Optional[str] = Query(default=None),
    current_user: UserInDB = Depends(get_current_user),):
    doc = audio_db.get_audio_document(audio_id)
    if not doc:
        raise HTTPException(status_code=404, detail="audio not found")

    _ensure_can_access_visibility(current_user, doc.get("visibility") or "")

    if segment_id:
        if ":" not in segment_id:
            raise HTTPException(status_code=400, detail="invalid segment_id format")
        seg_audio_id, seg_idx_str = segment_id.split(":", 1)
        if seg_audio_id != audio_id:
            raise HTTPException(status_code=400, detail="segment_id does not match audio_id")
        try:
            seg_idx = int(seg_idx_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid segment_idx in segment_id")

        seg = audio_db.get_audio_segment(audio_id, seg_idx)
        if not seg:
            raise HTTPException(status_code=404, detail="segment not found")

        start_ms = int(seg["start_ms"])
        end_ms = int(seg["end_ms"])
    else:
        if start_ms is None or end_ms is None:
            raise HTTPException(
                status_code=400,
                detail="start_ms and end_ms are required when segment_id is not provided",
            )

    if end_ms <= start_ms:
        raise HTTPException(status_code=400, detail="end_ms must be greater than start_ms")

    max_clip_ms = 5 * 60 * 1000
    if (end_ms - start_ms) > max_clip_ms:
        raise HTTPException(status_code=400, detail="clip too long")

    src_path = Path(doc["stored_path"])
    if not src_path.exists():
        raise HTTPException(status_code=404, detail="stored audio file missing")

    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    clip_name = f"{audio_id}_{start_ms}_{end_ms}_{uuid.uuid4().hex[:8]}.mp3"
    clip_path = CLIP_DIR / clip_name

    try:
        clip_audio_to_mp3(
            src_path=src_path,
            dst_path=clip_path,
            start_ms=int(start_ms),
            end_ms=int(end_ms),
        )
    except Exception:
        raise HTTPException(status_code=500, detail="failed to generate clip")

    background_tasks.add_task(lambda p=str(clip_path): Path(p).unlink(missing_ok=True))

    return FileResponse(
        path=str(clip_path),
        media_type="audio/mpeg",
        filename=clip_name,
    )
