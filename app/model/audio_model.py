from __future__ import annotations

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class AudioIngestResp(BaseModel):
    audio_id: str
    stored_as: str
    duration_ms: int
    language: Optional[str] = None
    visibility: str
    segments: int


class AudioDocDetail(BaseModel):
    audio_id: str
    original_filename: str
    stored_path: str
    duration_ms: int
    language: Optional[str] = None
    visibility: str
    status: str
    segment_count: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AudioSearchHit(BaseModel):
    audio_id: str
    segment_id: str
    start_ms: int
    end_ms: int
    text: str
    score: Optional[float] = None  # 向量库有些返回不了score就留空
    clip_url: Optional[str] = None


class AudioSearchResp(BaseModel):
    q: str
    k: int
    allowed_visibilities: List[str]
    hits: List[AudioSearchHit]
