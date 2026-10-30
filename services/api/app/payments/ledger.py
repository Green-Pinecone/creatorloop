"""Double-entry ledger in integer minor units. No floats anywhere in this module."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db.models import LedgerEntry, Outbox


def split(amount_minor: int, share_bps: int) -> tuple[int, int]:
    """(creator, platform). Creator share rounds down; the remainder goes to the platform."""
    creator = amount_minor * share_bps // 10_000
    return creator, amount_minor - creator


def _row(s: AsyncSession, txn: uuid.UUID, ref: str, currency: str, held: bool, account: str, amount: int) -> None:
    s.add(LedgerEntry(txn_id=txn, account=account, amount_minor=amount, currency=currency, held=held, ref=ref))
    # mirrored to Google Sheets by viaSocket flow F4; the creator id is not sent, only the account type
    s.add(Outbox(topic="viasocket.emit", payload={
        "event": "ledger.entry", "txn_id": str(txn), "ref": ref, "account_type": account.split(":")[0],
        "amount_minor": amount, "currency": currency, "held": held}))


def _add(s: AsyncSession, txn: uuid.UUID, ref: str, currency: str, held: bool, lines: list[tuple[str, int]]):
    assert sum(a for _, a in lines) == 0, "unbalanced transaction"
    for account, amount in lines:
        _row(s, txn, ref, currency, held, account, amount)


def record_supporter_payment(s: AsyncSession, *, payment_id: str, creator_id: uuid.UUID, amount_minor: int,
                             currency: str, held: bool = False, share_bps: int | None = None) -> None:
    share = get_settings().creator_fund_share_bps if share_bps is None else share_bps
    creator, platform = split(amount_minor, share)
    _add(s, uuid.uuid4(), payment_id, currency, held, [
        ("cash:dodo", -amount_minor), (f"creator_fund:{creator_id}", creator), ("platform_revenue", platform)])


def record_boost_payment(s: AsyncSession, *, payment_id: str, amount_minor: int, currency: str) -> None:
    _add(s, uuid.uuid4(), payment_id, currency, False, [("cash:dodo", -amount_minor),
                                                        ("platform_revenue", amount_minor)])


async def reverse_payment(s: AsyncSession, payment_id: str, refund_ref: str) -> int:
    """Exactly mirror every original entry for the payment (so refunds always revert balances precisely).
    Returns the number of reversing entries; a second call for the same refund is a no-op."""
    if await s.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.ref == refund_ref)):
        return 0
    originals = (await s.scalars(select(LedgerEntry).where(LedgerEntry.ref == payment_id))).all()
    if not originals:
        return 0
    txn = uuid.uuid4()
    for e in originals:
        account = "refunds" if e.account == "cash:dodo" else e.account
        _row(s, txn, refund_ref, e.currency, e.held, account, -e.amount_minor)
    return len(originals)


async def set_held(s: AsyncSession, payment_ids: list[str], held: bool) -> None:
    for e in (await s.scalars(select(LedgerEntry).where(LedgerEntry.ref.in_(payment_ids),
                                                         LedgerEntry.account.like("creator_fund:%")))).all():
        e.held = held


async def balance(s: AsyncSession, creator_id: uuid.UUID) -> dict:
    acct = f"creator_fund:{creator_id}"
    rows = (await s.execute(select(LedgerEntry.held, func.sum(LedgerEntry.amount_minor))
                            .where(LedgerEntry.account == acct).group_by(LedgerEntry.held))).all()
    by = {bool(h): int(t or 0) for h, t in rows}
    return {"available_minor": by.get(False, 0), "held_minor": by.get(True, 0),
            "lifetime_minor": by.get(False, 0) + by.get(True, 0)}


async def unbalanced_txns(s: AsyncSession) -> list[uuid.UUID]:
    q = select(LedgerEntry.txn_id).group_by(LedgerEntry.txn_id).having(func.sum(LedgerEntry.amount_minor) != 0)
    return list((await s.scalars(q)).all())
