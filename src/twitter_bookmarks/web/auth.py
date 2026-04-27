"""HTTP Basic Auth dependency for all dashboard routes."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from twitter_bookmarks.config import get_settings

_security = HTTPBasic()


def require_basic_auth(
    credentials: Annotated[HTTPBasicCredentials, Depends(_security)],
) -> str:
    """Validate Basic Auth against env-supplied username/password.

    Returns the username on success; raises 401 on mismatch. Uses
    `secrets.compare_digest` to defeat timing attacks on the
    constant-time comparison.
    """
    settings = get_settings()
    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.DASHBOARD_USERNAME.encode("utf-8"),
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.DASHBOARD_PASSWORD.encode("utf-8"),
    )
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


AuthedUser = Annotated[str, Depends(require_basic_auth)]
