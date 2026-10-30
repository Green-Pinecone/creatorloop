"""Applies decoded on-chain events to the database. Pure over (session, name, args, meta) so it is testable
without a node; every write is idempotent by primary key so re-indexing a range is safe."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Account, Allocation, Epoch, Post, Tip, WalletFunder
from ..outbox import enqueue


async def _account_for_wallet(s: AsyncSession, wallet: str) -> Account | None:
    return await s.scalar(select(Account).where(Account.wallet_address == wallet))


async def apply_event(s: AsyncSession, name: str, args: dict, meta: dict) -> None:
    """meta: block_number, tx_hash, log_index, timestamp (datetime)."""
    ts: datetime = meta["timestamp"]
    if name == "PostCreated":
        if await s.get(Post, args["postId"]):
            return
        author = args["author"].lower()
        acct = await _account_for_wallet(s, author)
        s.add(
            Post(
                post_id=args["postId"],
                author_wallet=author,
                account_id=acct.id if acct else None,
                metadata_cid=args["metadataCid"],
                block_number=meta["block_number"],
                tx_hash=meta["tx_hash"],
                created_at=ts,
            )
        )
        await enqueue(s, "ipfs.fetch_metadata", {"post_id": args["postId"], "cid": args["metadataCid"]})
        await enqueue(s, "moderation.check", {"post_id": args["postId"]})
        await enqueue(
            s, "graph.upsert",
            {"kind": "AUTHORED", "src": str(acct.id) if acct else author, "dst": args["postId"], "ts": ts.isoformat()},
        )
    elif name == "PostHidden":
        post = await s.get(Post, args["postId"])
        if post:
            post.hidden = bool(args["hidden"])
    elif name == "Tipped":
        key = (meta["tx_hash"], meta["log_index"])
        if await s.get(Tip, key):
            return
        frm, to = args["from"].lower(), args["to"].lower()
        s.add(
            Tip(
                tx_hash=key[0], log_index=key[1], post_id=args["postId"], from_wallet=frm, to_wallet=to,
                amount_wei=args["amount"], block_number=meta["block_number"], created_at=ts,
            )
        )
        await enqueue(
            s, "graph.upsert",
            {"kind": "TIPPED", "src": frm, "dst": to, "post_id": args["postId"],
             "amount_wei": str(args["amount"]), "ts": ts.isoformat()},
        )
        if await s.get(WalletFunder, frm) is None:
            await enqueue(s, "chain.resolve_funder", {"wallet": frm})
    elif name == "EpochPublished":
        # Normally already recorded by the scorer; this repairs a crash between sending the tx and saving it.
        root = "0x" + bytes(args["root"]).hex() if not isinstance(args["root"], str) else args["root"]
        ep = await s.scalar(select(Epoch).where(Epoch.root == root))
        if ep is not None and ep.onchain_epoch_id is None:
            ep.onchain_epoch_id, ep.tx_hash, ep.status = args["epochId"], meta["tx_hash"], "published"
    elif name == "Claimed":
        ep = await s.scalar(select(Epoch).where(Epoch.onchain_epoch_id == args["epochId"]))
        if ep is not None:
            row = await s.scalar(select(Allocation).where(Allocation.epoch_id == ep.id,
                                                          Allocation.wallet == args["account"].lower()))
            if row is not None:
                row.claimed = True
