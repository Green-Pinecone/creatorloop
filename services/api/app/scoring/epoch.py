"""Epoch lifecycle: score -> allocate -> Merkle tree -> pin -> publish. Each step persists its output, so a
re-run of the same window resumes from the last completed step. The idempotency key is the window end."""

import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ipfs
from ..chain import publisher
from ..config import get_settings
from ..db.models import (
    Account,
    Allocation,
    Epoch,
    Outbox,
    Payment,
    Post,
    Reaction,
    Tip,
    utcnow,
)
from ..graph import sync
from ..graph.trust import run_trust_job
from ..outbox import enqueue
from . import merkle
from .score import Inputs, allocate, score, tier_weight

log = structlog.get_logger()
WEI = 10**18
FORMULA = "v1: (sqrt(R)+2T+3P)*(0.5+0.5Q), per-fan reaction cap 5, creator cap"


def window_for(now: datetime, minutes: int) -> tuple[datetime, datetime]:
    """The most recently completed fixed-length window, aligned to the epoch grid."""
    secs = minutes * 60
    end = datetime.fromtimestamp(int(now.timestamp()) // secs * secs, UTC)
    return end - timedelta(seconds=secs), end


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def collect_inputs(s: AsyncSession, start: datetime, end: datetime) -> tuple[Inputs, dict[str, Account]]:
    """Snapshot of the window. Only rows created before `end` count, so later deletions cannot change it."""
    accts = {str(a.id): a for a in (await s.scalars(select(Account))).all()}
    by_wallet = {a.wallet_address: str(a.id) for a in accts.values() if a.wallet_address}
    weights = {i: tier_weight(a.trust_score, a.status) for i, a in accts.items()}
    inp = Inputs(weights=weights)
    posts = {p.post_id: p for p in (await s.scalars(select(Post).where(
        Post.hidden.is_(False), Post.moderation.not_in(["removed", "flagged"])))).all()}
    for r in (await s.scalars(select(Reaction))).all():
        if not (start <= _aware(r.created_at) < end):
            continue
        p = posts.get(r.post_id)
        creator = str(p.account_id) if p and p.account_id else None
        if creator and creator != str(r.account_id):
            inp.reactions.setdefault(creator, defaultdict(int))[str(r.account_id)] += 1
    for t in (await s.scalars(select(Tip))).all():
        if not (start <= _aware(t.created_at) < end):
            continue
        creator = by_wallet.get(t.to_wallet)
        if creator is None or t.post_id not in posts:
            continue
        tipper = by_wallet.get(t.from_wallet, t.from_wallet)  # unlinked wallets keep their 0x key
        if tipper != creator:
            d = inp.tips_pol.setdefault(creator, defaultdict(float))
            d[tipper] += t.amount_wei / WEI
    for p in (await s.scalars(select(Payment).where(Payment.status == "succeeded",
                                                    Payment.kind == "supporter_pass"))).all():
        if p.creator_id and p.account_id and start <= _aware(p.created_at) < end:
            d = inp.paid_inr.setdefault(str(p.creator_id), defaultdict(float))
            d[str(p.account_id)] += p.amount_minor / 100  # points only; the amount itself stays integer wei
    for p in posts.values():
        if p.account_id and start <= _aware(p.created_at) < end:
            inp.posts[str(p.account_id)] = inp.posts.get(str(p.account_id), 0) + 1
    return inp, accts


def eligibility(acct: Account | None) -> str | None:
    """Reason a creator is excluded from this epoch, or None."""
    if acct is None:
        return "unknown"
    if acct.status in ("frozen", "banned"):
        return acct.status
    if acct.status == "restricted":
        return "restricted"
    if acct.trust_score < 0.20:
        return "low_trust"
    if not acct.wallet_address:
        return "no_wallet"
    return None


async def _build(s: AsyncSession, ep: Epoch) -> None:
    inp, accts = await collect_inputs(s, _aware(ep.window_start), _aware(ep.window_end))
    candidates = set(inp.reactions) | set(inp.tips_pol) | set(inp.paid_inr)
    excluded, creators = [], set()
    for c in sorted(candidates):
        reason = eligibility(accts.get(c))
        if reason:
            a = accts.get(c)
            excluded.append({"account_id": c, "wallet": a.wallet_address if a else None, "reason": reason})
        else:
            creators.add(c)
    cap = get_settings().epoch_creator_cap_bps
    points = score(inp, creators)
    alloc = allocate(points, ep.pool_wei, cap)
    # What the same engagement would pay with every account weighted equally: shows what trust weighting prevents.
    naive = allocate(score(inp, creators | {c for c in candidates if c in accts}, uniform_weights=True),
                     ep.pool_wei, cap)
    ep.excluded = excluded
    ep.stats = {"creators_scored": len(points), "creators_paid": len(alloc), "naive_allocations": {
        c: str(v) for c, v in naive.items()}, "points": {c: points[c] for c in alloc}}
    if not alloc:
        ep.status, ep.total_wei = "empty", 0
        return
    ordered = sorted(alloc)
    values = [(accts[c].wallet_address, alloc[c]) for c in ordered]
    root, proofs = merkle.build(values)
    ep.root, ep.total_wei = "0x" + root.hex(), sum(alloc.values())
    for c, (wallet, amount), proof in zip(ordered, values, proofs, strict=True):
        s.add(Allocation(epoch_id=ep.id, account_id=uuid.UUID(c), wallet=wallet, amount_wei=amount,
                         points=points[c], proof=["0x" + p.hex() for p in proof]))
    ep.status = "built"


async def _allocations_json(s: AsyncSession, ep: Epoch) -> bytes:
    """Built only from stored rows, so re-pinning after a crash always yields the same CID."""
    rows = (await s.scalars(select(Allocation).where(Allocation.epoch_id == ep.id)
                            .order_by(Allocation.wallet))).all()
    doc = {"schema": "creatorloop.allocations.v1", "epoch": ep.id,
           "window": [_aware(ep.window_start).isoformat(), _aware(ep.window_end).isoformat()],
           "pool": str(ep.pool_wei), "total": str(ep.total_wei), "root": ep.root, "formula": FORMULA,
           "allocations": [{"wallet": r.wallet, "amount": str(r.amount_wei), "points": round(r.points, 6)}
                           for r in rows],
           "excluded": [{"wallet": e["wallet"], "reason": e["reason"]} for e in ep.excluded]}
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


async def _pending_freezes(s: AsyncSession) -> int:
    return await s.scalar(select(func.count()).select_from(Outbox).where(
        Outbox.topic == "chain.set_frozen", Outbox.done_at.is_(None))) or 0


async def run_epoch(s: AsyncSession, now: datetime | None = None) -> dict:
    st = get_settings()
    start, end = window_for(now or utcnow(), st.epoch_window_minutes)
    ep = await s.scalar(select(Epoch).where(Epoch.window_end == end))
    if ep is None:
        await run_trust_job(s, sync.store)  # snapshot fresh trust before scoring
        ep = Epoch(window_start=start, window_end=end, status="scoring", pool_wei=st.epoch_pool_loop_wei)
        s.add(ep)
        await s.flush()
        await _build(s, ep)
        await _tick_restrictions(s)
        await s.commit()
    if ep.status == "built":
        blob = await _allocations_json(s, ep)
        ep.allocations_cid = ipfs.put_local(blob)  # swap for Pinata when configured
        ep.status = "pinned"
        await s.commit()
    if ep.status == "pinned":
        if not publisher.publisher_configured():
            log.info("epoch.publish_skipped_no_publisher_key", epoch=ep.id)
        elif await _pending_freezes(s):
            ep.error = "pending freeze transactions; publish deferred"
            await s.commit()
        else:
            try:
                ep.tx_hash, ep.onchain_epoch_id = await publisher.publish_epoch(ep.root, ep.total_wei,
                                                                                ep.allocations_cid)
                ep.status, ep.error = "published", None
            except publisher.Underfunded as e:
                ep.error = str(e)
                await enqueue(s, "viasocket.emit", {"event": "ops.alert", "severity": 4,
                                                    "title": "reward distributor underfunded", "detail": str(e)})
            await s.commit()
    if ep.status in ("published", "empty"):
        await _announce(s, ep)
    return summary(ep)


async def _announce(s: AsyncSession, ep: Epoch) -> None:
    flags = dict(ep.stats or {})
    if flags.get("announced"):
        return
    ep.stats = {**flags, "announced": True}
    await enqueue(s, "viasocket.emit", {"event": "epoch.published", "epoch": ep.id,
                                        "onchain_epoch_id": ep.onchain_epoch_id, "creators": flags.get("creators_paid", 0),
                                        "total_loop": str((ep.total_wei or 0) // WEI), "excluded": len(ep.excluded),
                                        "tx": ep.tx_hash})
    await s.commit()


async def _tick_restrictions(s: AsyncSession) -> None:
    """`restrict` lasts 2 epochs: count down and reinstate accounts when it runs out."""
    for a in (await s.scalars(select(Account).where(Account.status == "restricted"))).all():
        flags = dict(a.risk_flags or {})
        left = int(flags.get("restricted_epochs", 0)) - 1
        flags["restricted_epochs"] = max(left, 0)
        a.risk_flags = flags
        if left <= 0:
            a.status = "active"


def summary(ep: Epoch) -> dict:
    return {"epoch": ep.id, "status": ep.status, "window_end": _aware(ep.window_end).isoformat(),
            "creators": (ep.stats or {}).get("creators_paid", 0), "total_loop": str((ep.total_wei or 0) // WEI),
            "excluded": len(ep.excluded or []), "root": ep.root, "allocations_cid": ep.allocations_cid,
            "tx": ep.tx_hash, "onchain_epoch_id": ep.onchain_epoch_id, "error": ep.error}
