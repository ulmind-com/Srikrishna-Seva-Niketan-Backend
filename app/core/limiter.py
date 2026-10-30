"""SlowAPI rate limiter singleton.

Keyed by client IP (with Cloudflare & proxy header extraction). Applied globally
via the app state and per-route through the ``@limiter.limit(...)`` decorator.
"""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter

from app.config import settings


def get_real_client_ip(request: Request) -> str:
    """Extract real client IP behind Cloudflare and reverse proxies.

    Prevents all users behind Cloudflare sharing a single proxy IP and hitting
    the global rate limit.
    """
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()

    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()

    x_real = request.headers.get("X-Real-IP")
    if x_real:
        return x_real.strip()

    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


limiter = Limiter(
    key_func=get_real_client_ip,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
    headers_enabled=True,
)
