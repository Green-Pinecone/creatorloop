import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Outbox, utcnow
from .db.session import session_maker

log = structlog.get_logger()
MAX_ATTEMPTS = 10
Handler = Callable[[dict], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


def register(topic: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[topic] = fn
        return fn

    return deco


async def enqueue(s: AsyncSession, topic: str, payload: dict) -> None:
    """Add to the caller's transaction so the domain write and the event commit (or roll back) together."""
    s.add(Outbox(topic=topic, payload=payload))


async def claim_batch(s: AsyncSession, limit: int = 100) -> list[Outbox]:
    q = (
        select(Outbox)
        .where(Outbox.topic.in_(list(HANDLERS)), Outbox.done_at.is_(None), Outbox.available_at <= utcnow(), Outbox.attempts < MAX_ATTEMPTS)
        .order_by(Outbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)  # ignored on SQLite
    )
    return list((await s.scalars(q)).all())


async def process_once(limit: int = 100) -> int:
    async with session_maker()() as s, s.begin():
        rows = await claim_batch(s, limit)
        for r in rows:
            handler = HANDLERS.get(r.topic)
            try:
                if handler is None:
                    raise LookupError(f"no handler for topic {r.topic}")
                await handler(r.payload)
                r.done_at = utcnow()
            except Exception as e:  # noqa: BLE001 - any handler failure is retried
                r.attempts += 1
                r.last_error = str(e)[:500]
                r.available_at = utcnow() + timedelta(seconds=min(2**r.attempts, 300))
                if r.attempts >= MAX_ATTEMPTS:
                    log.error("outbox.dead_letter", id=r.id, topic=r.topic, error=r.last_error)
                    if r.payload.get("event") != "ops.alert":  # never alert about a failed alert
                        s.add(Outbox(topic="viasocket.emit", payload={
                            "event": "ops.alert", "severity": 4, "title": f"outbox gave up on {r.topic}",
                            "detail": (r.last_error or "")[:200], "outbox_id": r.id}))
        return len(rows)


async def outbox_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        n = await process_once()
        await asyncio.sleep(0.5 if n else 2)
