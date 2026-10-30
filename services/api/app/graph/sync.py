from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Account, DeviceSignal, Follow, Payment, Post, Reaction, Tip, WalletFunder
from ..outbox import register
from .store import GraphStore

store = GraphStore()  # process-wide instance; Postgres remains the source of truth


@register("graph.upsert")
async def handle(payload: dict) -> None:
    store.apply(payload)


async def rebuild(s: AsyncSession, target: GraphStore | None = None) -> GraphStore:
    """Rebuild the whole graph from Postgres (after a restart or a schema change)."""
    g = target or store
    g.clear()
    for a in (await s.scalars(select(Account))).all():
        g.accounts.add(str(a.id))
        if a.wallet_address:
            g.apply({"kind": "OWNS_WALLET", "src": str(a.id), "dst": a.wallet_address})
    for p in (await s.scalars(select(Post))).all():
        g.apply({"kind": "AUTHORED", "src": str(p.account_id) if p.account_id else p.author_wallet,
                 "dst": p.post_id})
    for r in (await s.scalars(select(Reaction))).all():
        g.apply({"kind": "REACTED", "src": str(r.account_id), "dst": r.post_id})
    for f in (await s.scalars(select(Follow))).all():
        g.apply({"kind": "FOLLOWS", "src": str(f.follower_id), "dst": str(f.creator_id)})
    for d in (await s.scalars(select(DeviceSignal))).all():
        g.apply({"kind": "USED_DEVICE", "src": str(d.account_id), "dst": d.device_hash})
        g.apply({"kind": "SEEN_ON_NET", "src": str(d.account_id), "dst": d.ip_bucket_hash})
    for t in (await s.scalars(select(Tip))).all():
        g.apply({"kind": "TIPPED", "src": t.from_wallet, "dst": t.to_wallet, "post_id": t.post_id,
                 "amount_wei": t.amount_wei, "tx_hash": t.tx_hash, "log_index": t.log_index})
    for w in (await s.scalars(select(WalletFunder))).all():
        g.apply({"kind": "FUNDED_BY", "src": w.wallet, "dst": w.funder})
    for p in (await s.scalars(select(Payment).where(Payment.account_id.is_not(None)))).all():
        g.apply({"kind": "PAID", "src": str(p.account_id), "dst": str(p.creator_id or p.account_id),
                 "payment_id": p.dodo_payment_id, "amount_minor": p.amount_minor, "status": p.status,
                 "product": p.kind})
        if p.payer_hash:
            g.apply({"kind": "PAID_WITH", "src": str(p.account_id), "dst": p.payer_hash})
    return g
