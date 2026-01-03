from __future__ import annotations

from app.deps import get_audio_vs

def audio_similarity_search_for_user(query: str, user, k: int = 6):
    vs = get_audio_vs()
    docs = vs.similarity_search(query, k=k, filter={"visibility": {"$in":['public']}})
    return docs, ['public']
