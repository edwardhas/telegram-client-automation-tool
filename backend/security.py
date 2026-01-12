from __future__ import annotations

from fastapi import Header, HTTPException, status

from .settings import settings


def require_admin(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    if not settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_TOKEN is not configured on the server",
        )

    if x_admin_token != settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )
