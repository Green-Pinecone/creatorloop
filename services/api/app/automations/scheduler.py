"""Fallback scheduler (LOCAL_SCHEDULER=true) for when viaSocket is unreachable, e.g. on demo day.
Runs the same jobs the viaSocket cron flows call through /internal/*."""

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..db.session import session_maker
from . import jobs

log = structlog.get_logger()


def _wrap(fn):
    async def run():
        try:
            async with session_maker()() as s:
                log.info("scheduler.job", job=fn.__name__, result=await fn(s))
        except Exception:  # noqa: BLE001 - one failed run must not stop the schedule
            log.exception("scheduler.job_failed", job=fn.__name__)

    run.__name__ = fn.__name__
    return run


def build() -> AsyncIOScheduler:
    sch = AsyncIOScheduler(timezone="UTC")
    sch.add_job(_wrap(jobs.trust_run), "interval", minutes=15, id="trust_run")
    sch.add_job(_wrap(jobs.ledger_check), "interval", hours=1, id="ledger_check")
    sch.add_job(_wrap(jobs.retention), "cron", hour=3, id="retention")
    sch.add_job(_wrap(jobs.creator_digests), "cron", day_of_week="mon", hour=3, minute=30, id="digests")
    return sch
