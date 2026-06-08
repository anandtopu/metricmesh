from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from storage.timescale import get_session

router = APIRouter(prefix="/dead-letters", tags=["dead-letters"])


class DeadLetterItem(BaseModel):
    id: int
    failed_at: str
    task_name: str
    task_id: str | None
    queue: str | None
    retries: int
    exception: str | None
    args: list[Any]
    kwargs: dict[str, Any]
    traceback: str | None


class DeadLettersPage(BaseModel):
    items: list[DeadLetterItem]
    count: int
    total: int
    limit: int
    offset: int


@router.get("", response_model=DeadLettersPage, summary="Inspect dead-lettered (poison) tasks")
async def list_dead_letters(
    task_name: str | None = Query(None, description="Filter by exact task name"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> DeadLettersPage:
    """
    Paginated view of tasks that exhausted their retries (MM-4.6), newest first,
    so failures can be inspected rather than vanishing.
    """
    from storage.timescale import fetch_dead_letters

    items, total = await fetch_dead_letters(
        session, task_name=task_name, limit=limit, offset=offset
    )
    rows = [DeadLetterItem(**i) for i in items]
    return DeadLettersPage(
        items=rows, count=len(rows), total=total, limit=limit, offset=offset
    )
