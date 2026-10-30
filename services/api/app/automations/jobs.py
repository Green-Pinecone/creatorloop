"""Scheduled jobs. Called by /internal/* (viaSocket cron) and by the optional local APScheduler fallback."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Account, Creator, Payment, Post, Reaction, Tip, WebhookEvent, utcnow
from ..graph import sync
from ..graph.trust import run_trust_job
from ..outbox import enqueue
from ..payments.ledger import unbalanced_txns
from ..retention import purge_old_signals

TRUST_MIN_INTERVAL = timedelta(minutes=5)


async def trust_run(s: AsyncSession) -> dict:
    last = await s.scalar(select(func.max(Account.trust_updated_at)))
    if last is not None and utcnow() - last.replace(tzinfo=last.tzinfo or UTC) < TRUST_MIN_INTERVAL:
        return {"skipped": True, "reason": "ran under 5 minutes ago"}
    return await run_trust_job(s, sync.store)


async def retention(s: AsyncSession) -> dict:
    return {"purged_device_signals": await purge_old_signals()}


async def ledger_check(s: AsyncSession) -> dict:
    bad = await unbalanced_txns(s)
    if bad:
        await enqueue(s, "viasocket.emit", {"event": "ops.alert", "severity": 5, "title": "ledger invariant broken",
                                            "detail": f"{len(bad)} unbalanced transactions"})
        await s.commit()
    return {"ok": not bad, "unbalanced": len(bad)}


async def creator_digests(s: AsyncSession, now: datetime | None = None) -> dict:
    """One digest event per creator per ISO week; calling again in the same week emits nothing new."""
    now = now or utcnow()
    year, week, _ = now.isocalendar()
    since = now - timedelta(days=7)
    sent = skipped = 0
    for c in (await s.scalars(select(Creator))).all():
        key = f"{year}-W{week:02d}:{c.account_id}"
        if await s.get(WebhookEvent, ("digest", key)) is not None:
            skipped += 1
            continue
        acct = await s.get(Account, c.account_id)
        supporters = await s.scalar(select(func.count()).select_from(Payment).where(
            Payment.creator_id == c.account_id, Payment.kind == "supporter_pass",
            Payment.status == "succeeded", Payment.created_at >= since))
        tips = (await s.scalars(select(Tip.amount_wei).where(
            Tip.to_wallet == (acct.wallet_address if acct else None), Tip.created_at >= since))).all()
        top = (await s.execute(
            select(Post.post_id, func.count(Reaction.account_id).label("n")).outerjoin(
                Reaction, Reaction.post_id == Post.post_id).where(Post.account_id == c.account_id)
            .group_by(Post.post_id).order_by(func.count(Reaction.account_id).desc()).limit(1))).first()
        s.add(WebhookEvent(provider="digest", event_id=key, payload={"week": f"{year}-W{week:02d}"},
                           processed_at=now))
        await enqueue(s, "viasocket.emit", {
            "event": "digest.creator", "week": f"{year}-W{week:02d}", "handle": c.handle,
            "display_name": c.display_name, "new_supporters": supporters or 0,
            "tip_total_wei": str(sum(tips)), "tip_count": len(tips),
            "top_post_id": top[0] if top else None, "top_post_reactions": top[1] if top else 0})
        sent += 1
    await s.commit()
    return {"week": f"{year}-W{week:02d}", "digests": sent, "already_sent": skipped}
