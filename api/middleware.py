"""
Authentication middleware, rate limiting, and auth routes.

Provides JWT-based authentication and in-memory rate limiting for the dashboard API.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


auth_router = APIRouter()
security = HTTPBearer(auto_error=False)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Models ────────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ── Rate Limiter ──────────────────────────────────────────────────────


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory fixed-window rate limiter for API endpoints."""

    def __init__(self, app: Any, *, requests: int, window_seconds: int) -> None:
        super().__init__(app)
        self.max_requests = requests
        self.window = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if not path.startswith("/api") and not path.startswith("/dashboard") and not path.startswith("/trading") and not path.startswith("/auth"):
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        bucket = self._buckets[ip]
        now = time.time()

        while bucket and bucket[0] <= now - self.window:
            bucket.popleft()

        if len(bucket) >= self.max_requests:
            return Response(
                content="Too Many Requests",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        bucket.append(now)
        return await call_next(request)


# ── Token Handling ────────────────────────────────────────────────────


def _get_settings(request: Request | None = None):
    """Get settings from app state or default."""
    if request and hasattr(request, "app") and hasattr(request.app, "state"):
        return getattr(request.app.state, "settings", None)
    # Lazy import for non-request contexts
    try:
        from config.settings import Settings
        return Settings()
    except Exception:
        return None


def create_access_token(username: str, request: Request | None = None) -> tuple[str, int]:
    """Create a JWT access token."""
    settings = _get_settings(request)
    expire_minutes = settings.jwt.expire_minutes if settings else 1440
    secret = settings.jwt.secret_key if settings else "default_secret"
    algorithm = settings.jwt.algorithm if settings else "HS256"

    expires = timedelta(minutes=expire_minutes)
    expire_at = datetime.utcnow() + expires

    payload = {
        "sub": username,
        "exp": expire_at,
        "iat": datetime.utcnow(),
    }
    token = jwt.encode(payload, secret, algorithm=algorithm)
    return token, expire_minutes * 60


def verify_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    """Verify JWT token and return username."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = _get_settings(request)
    secret = settings.jwt.secret_key if settings else "default_secret"
    algorithm = settings.jwt.algorithm if settings else "HS256"

    try:
        payload = jwt.decode(credentials.credentials, secret, algorithms=[algorithm])
        username = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return username
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired or invalid",
        )


# ── Auth Routes ───────────────────────────────────────────────────────


@auth_router.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request):
    """Authenticate and receive a JWT token.

    Verifies against a bcrypt hash (DASHBOARD_PASSWORD_HASH) when configured;
    otherwise falls back to a constant-time plaintext compare and refuses the
    insecure default password outright.
    """
    settings = _get_settings(request)
    valid_user = settings.dashboard.username if settings else "admin"
    pw_hash = settings.dashboard.password_hash if settings else ""
    plain = settings.dashboard.password if settings else "changeme"

    user_ok = secrets.compare_digest(body.username, valid_user)
    if pw_hash:
        try:
            pass_ok = pwd_context.verify(body.password, pw_hash)
        except Exception:
            pass_ok = False
    elif plain in ("", "changeme"):
        logger.error(
            "Login blocked: dashboard password not configured securely "
            "(set DASHBOARD_PASSWORD_HASH or a non-default DASHBOARD_PASSWORD)."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard password not configured securely",
        )
    else:
        pass_ok = secrets.compare_digest(body.password, plain)

    if not (user_ok and pass_ok):
        logger.warning(f"Failed login attempt: {body.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    token, expires_in = create_access_token(body.username, request)
    logger.info(f"User '{body.username}' logged in")
    return TokenResponse(access_token=token, expires_in=expires_in)


@auth_router.get("/auth/me")
async def get_me(username: str = Depends(verify_token)):
    """Get current authenticated user."""
    return {"username": username}
