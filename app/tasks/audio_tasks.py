from __future__ import annotations

from pathlib import Path

from celery.exceptions import Ignore

from app import celery_app
from app.config import settings
from app.db import audio_db
from app.db import audio_job_db
from app.audio.pipeline import run_audio_ingest_pipeline
from app.rag.chroma_admin import delete_by_audio_id


def _check_cancel(job_id: str):
    if audio_job_db.is_cancel_requested(job_id):
        audio_job_db.mark_cancelled(job_id)
        raise Ignore()


@celery_app.celery_app.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},)
def audio_ingest_task(self, job_id: str, audio_id: str):
    flags = audio_job_db.get_job_flags(job_id)
    old_path = flags.get("old_stored_path")
    delete_old_file = bool(int(flags.get("delete_old_file", 0) or 0))

    audio_job_db.update_job(job_id, status="running", progress=1, message="starting")
    audio_db.update_audio_status(audio_id, status="running")

    _check_cancel(job_id)

    doc = audio_db.get_audio_document(audio_id)
    if not doc:
        raise RuntimeError("audio_document not found")

    raw_path = Path(doc["stored_path"])
    if not raw_path.exists():
        raise RuntimeError("stored audio file missing")

    audio_job_db.update_job(job_id, progress=5, message="cleaning old vectors")
    delete_by_audio_id(audio_id)

    _check_cancel(job_id)

    audio_job_db.update_job(job_id, progress=10, message="transcribing/indexing")
    res = run_audio_ingest_pipeline(
        audio_id=audio_id,
        raw_path=raw_path,
        original_filename=doc["original_filename"],
        visibility=doc["visibility"],
        language=doc.get("language"),
        wav_dir=Path(settings.audio_wav_dir),
    )

    _check_cancel(job_id)

    audio_db.update_audio_indexed(
        audio_id=audio_id,
        duration_ms=int(res["duration_ms"]),
        language=res.get("language"),
        segment_count=int(res["segments"]),
        status="indexed",
    )

    audio_job_db.update_job(job_id, status="succeeded", progress=100, message=f"indexed {res['segments']} segments")

    if delete_old_file and old_path and old_path != str(raw_path):
        try:
            p = Path(str(old_path))
            if p.exists() and p.is_file():
                p.unlink()
        except Exception:
            print('注意异常！！！！！！！！！！！！！！！！1')

    return res
