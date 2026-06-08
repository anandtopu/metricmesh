from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from storage.timescale import get_session

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditItem(BaseModel):
    id: int
    at: str
    action: str
    principal: str | None
    outcome: str | None
    resource: str | None
    source_ip: str | None
    detail: dict[str, Any]


class AuditPage(BaseModel):
    items: list[AuditItem]
    count: int
    total: int
    limit: int
    offset: int


@router.get("", response_model=AuditPage, summary="Inspect the audit trail (who/what/when)")
async def list_audit(
    action: str | None = Query(
        None, description="Filter by action, e.g. auth.denied, feedback.submitted, alert.routed"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> AuditPage:
    """Paginated audit trail, newest first (MM-9.5)."""
    from storage.timescale import fetch_audit_log

    items, total = await fetch_audit_log(session, action=action, limit=limit, offset=offset)
    rows = [AuditItem(**i) for i in items]
    return AuditPage(items=rows, count=len(rows), total=total, limit=limit, offset=offset)
