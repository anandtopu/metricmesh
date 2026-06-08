from __future__ import annotations

from typing import Any

import structlog
from celery.signals import task_failure

log = structlog.get_logger(__name__)


@task_failure.connect
def record_dead_letter(
    sender: Any = None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    args: Any = None,
    kwargs: Any = None,
    traceback: Any = None,
    einfo: Any = None,
    **extra: Any,
) -> None:
    """Persist a task that has exhausted its retries to the dead-letter store (MM-4.6).

    Celery fires ``task_failure`` only on a task's **final** failure — a
    ``self.retry()`` raises ``Retry``, which is not a failure — so this captures
    poison tasks *after* their retries are exhausted, rather than letting them
    vanish. Best-effort: a DB error here must never mask the original failure.
    """
    from storage.timescale import persist_dead_letter_sync

    task_name = getattr(sender, "name", None) or "unknown"
    request = getattr(sender, "request", None)
    retries = getattr(request, "retries", 0) or 0
    delivery = getattr(request, "delivery_info", None) or {}
    queue = delivery.get("routing_key") if isinstance(delivery, dict) else None
    tb = str(einfo) if einfo is not None else (str(traceback) if traceback else None)
    exc_str = f"{type(exception).__name__}: {exception}" if exception is not None else "unknown"

    try:
        persist_dead_letter_sync(
            task_name=task_name,
            task_id=task_id,
            queue=queue,
            retries=int(retries),
            exception=exc_str,
            args=list(args) if args is not None else [],
            kwargs=dict(kwargs) if kwargs is not None else {},
            traceback=tb,
        )
        log.warning("task.dead_lettered", task=task_name, task_id=task_id, retries=int(retries))
    except Exception as exc:  # pragma: no cover - defensive, must not re-raise
        log.error("dead_letter.persist_failed", task=task_name, error=str(exc))
