import json
import redis
from typing import Any

r = redis.Redis(
    host="127.0.0.1",
    port=6379,
    decode_responses=True,
)

TTL_SECONDS = 604800  # 7 days 键多久会自动过期

# Keys that often contain non-JSON-serializable objects (LangChain Documents, Messages, etc.)
DROP_KEYS = {"docs", "messages", "chat_history", "retrieved_docs"}


def _safe_dumps(obj: Any) -> str:
    """Dump to JSON, falling back to str() for unknown objects."""
    return json.dumps(obj, ensure_ascii=False, default=str)


def load_session(session_id: str) -> dict | None:
    s = r.get(session_id)
    return json.loads(s) if s else None


def save_session(session_id: str, state: dict) -> None:
    # shallow copy and drop problematic keys
    safe_state = {k: v for k, v in state.items() if k not in DROP_KEYS}
    r.setex(session_id, TTL_SECONDS, _safe_dumps(safe_state))

if __name__ == "__main__":
    r.set('tom','jerry')
