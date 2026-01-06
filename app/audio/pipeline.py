from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional, Any

from langchain_core.documents import Document

from app.audio.audio_loader import ffprobe_duration_ms, transcode_to_wav_16k_mono
from app.audio.asr import ASR
from app.audio.segmenter import merge_by_max_duration
from app.db import audio_db
from app.deps import get_audio_vs

ProgressFn = Callable[[int, str], None]


def run_audio_ingest_pipeline(
        *,
        audio_id: str,
        raw_path: Path,
        original_filename: str,
        visibility: str,
        language: str | None,
        wav_dir: Path,
        model_name: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        max_chunk_ms: int = 25_000,
        min_chunk_ms: int = 6_000,
        on_progress: Optional[ProgressFn] = None,
) -> dict[str, Any]:
    def prog(p: int, msg: str) -> None:
        if on_progress:
            try:
                on_progress(int(p), str(msg))
            except Exception:
                pass

    if not audio_id:
        raise RuntimeError("audio_id required")
    if not raw_path.exists():
        raise RuntimeError(f"raw audio not found: {raw_path}")
    if not visibility:
        raise RuntimeError("visibility required")

    wav_dir.mkdir(parents=True, exist_ok=True)
    wav_path = wav_dir / f"{audio_id}.wav"

    prog(12, "transcoding")
    transcode_to_wav_16k_mono(raw_path, wav_path)

    prog(18, "probing duration")
    duration_ms = ffprobe_duration_ms(wav_path)

    prog(30, "transcribing")
    asr = ASR(model_name=model_name, device=device, compute_type=compute_type)
    asr_segs, detected_lang = asr.transcribe(str(wav_path), language=language)
    lang = language or detected_lang

    if not asr_segs:
        raise RuntimeError("ASR produced no segments")

    prog(45, "segmenting")
    chunks = merge_by_max_duration(asr_segs, max_ms=max_chunk_ms, min_ms=min_chunk_ms)
    if not chunks:
        raise RuntimeError("no chunks produced")

    prog(55, "building documents")
    docs: list[Document] = []
    segment_rows: list[dict[str, Any]] = []

    now_ts = int(time.time())
    seg_idx = 0
    for c in chunks:
        text = (c.text or "").strip()
        if not text:
            continue

        segment_id = f"{audio_id}:{seg_idx}"
        meta = {
            "doc_type": "audio",

            "audio_id": audio_id,
            "segment_id": segment_id,
            "start_ms": int(c.start_ms),
            "end_ms": int(c.end_ms),
            "visibility": visibility,

            "segment_idx": int(seg_idx),
            "original_filename": original_filename,
            "stored_path": str(raw_path),
            "wav_path": str(wav_path),
            "language": lang,
            "ingested_at": now_ts,
        }

        docs.append(Document(page_content=text, metadata=meta))
        segment_rows.append(
            {
                "segment_idx": int(seg_idx),
                "start_ms": int(c.start_ms),
                "end_ms": int(c.end_ms),
                "text": text,
            }
        )
        seg_idx += 1

    if not docs:
        raise RuntimeError("no transcript text produced")

    prog(72, "writing vectorstore")
    vs = get_audio_vs()
    vs.add_documents(docs)

    prog(85, "writing database segments")
    audio_db.replace_audio_segments(audio_id, segment_rows)
    prog(95, "done")

    return {
        "duration_ms": int(duration_ms),
        "language": lang,
        "segments": int(len(segment_rows)),
        "wav_path": str(wav_path),
    }
