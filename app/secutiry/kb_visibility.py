from __future__ import annotations

from app.service.rbac_service import _resolve_perms, allowed_kb_visibilities

def normalize_visibility(v: str) -> str:
    vv = (v or "").strip().lower()
    if vv in ("public", "internal"):
        return vv
    raise ValueError(f"invalid visibility: {v}")

def compute_allowed_kb_visibilities(user) -> list[str]:
    perms = _resolve_perms(user=user)
    return allowed_kb_visibilities(perms)
