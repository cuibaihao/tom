# ----------------helper-----------------
def _resolve_perms(*,
                   user: UserInDB | None = None,
                   user_id: int | None = None,
                   perms: Iterable[str] | None = None) -> set[str]:
    if perms:
        return set(perms)

    if user is not None:
        p = getattr(user, "permissions", None)
        if p:
            return set(p)
        user_id = user_id or getattr(user, "id", None)

    if user_id is not None:
        return set(get_user_permissions(int(user_id)))

    return set()


def _raise_403(detail: str) -> None:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
# ----------------helper-----------------