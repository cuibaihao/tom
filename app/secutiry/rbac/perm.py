from typing import Any, Iterable

from fastapi import HTTPException
from streamlit import status

from app.db.rbac_db import get_user_permissions
from app.perm import _raise_403, _resolve_perms


def check_permission(user: Any, perm_code: str) -> None:
    """检测user这个参数所表示的用户是否拥有perm_code权限，有就返回，没有就抛出异常"""
    if not perm_code or not isinstance(perm_code, str):
        raise TypeError("perm_code must be a non-empty str")

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,detail="Not authenticated",)

    if isinstance(user, dict):  # user是一个字段，实际上我们要的是类似UserInDB这种东西
        if user.get("is_super_admin", False):
            return  # 超级管理员，直接过，过不了的都抛出异常
        perms = set(user.get("permissions") or [])
        if perm_code not in perms:
            _raise_403(f"Missing permission: {perm_code}")
        return

    if getattr(user, "is_super_admin", False):
        return

    perms = set(getattr(user, "permissions", []) or [])
    if not perms:
        perms = set(get_user_permissions(user.id))

    if perm_code not in perms:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {perm_code}",
        )

def has_permission(*,
                   user_id: int | None = None,
                   perms: Iterable[str] | None = None,
                   perm_code: str) -> bool:
    # 判断perm_code是不是在user_id所拥有的perms里面，即检验是否有权限
    resolved = _resolve_perms(user_id=user_id, perms=perms)
    return perm_code in resolved