"""API-key authentication (MM-9.1) + auth audit (MM-9.5).

A lightweight header-based scheme: requests to protected (data) endpoints must
send a valid ``X-API-Key``. Health/readiness probes are intentionally left
unauthenticated so orchestrators can poll them.

Auth is DISABLED when no keys are configured (``API_KEYS`` empty) — convenient
for local dev — and ENFORCED as soon as one or more keys are set.

The dependency returns a **principal** string identifying the caller — a
non-reversible key id (never the raw key), or ``"anonymous"`` in dev mode — so
routes can attribute audited actions to who performed them.
"""
from __future__ import annotations

import hashlib
from typing import NamedTuple

from fastapi import Header, HTTPException, Request, status

from config import get_settings


class Identity(NamedTuple):
    """The authenticated caller: a non-reversible key id and their tenant (MM-9.3)."""

    principal: str
    tenant: str


def principal_for(key: str) -> str:
    """A short, non-reversible identifier for an API key (never the raw secret)."""
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:8]


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> Identity:
    settings = get_settings()
    mapping = settings.key_tenant_map
    if not mapping:
        # No keys configured → auth disabled (dev mode); everything is one tenant.
        return Identity("anonymous", settings.default_tenant)
    if x_api_key is not None and x_api_key in mapping:
        return Identity(principal_for(x_api_key), mapping[x_api_key])

    # Denied — record the failed attempt (who/what/when), best-effort (MM-9.5).
    from storage.timescale import record_audit_async

    await record_audit_async(
        "auth.denied",
        principal=principal_for(x_api_key) if x_api_key else "none",
        outcome="denied",
        resource=request.url.path,
        source_ip=request.client.host if request.client else None,
        detail={"method": request.method, "reason": "missing" if x_api_key is None else "invalid"},
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "API-Key"},
    )
