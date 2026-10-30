from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_account, ensure_active
from ..config import get_settings
from ..db.models import Account, Creator, LedgerEntry, Post, Subscription
from ..db.session import get_session
from ..payments import dodo, ledger
from ..payments.handlers import BadSignature, card_testing_blocked, process_webhook

router = APIRouter(tags=["payments"])


class SupporterCheckout(BaseModel):
    creator_handle: str


class BoostCheckout(BaseModel):
    post_id: int


async def _guard(s: AsyncSession, acct: Account) -> None:
    ensure_active(acct)
    if (acct.risk_flags or {}).get("tier") == "quarantine":
        raise HTTPException(403, "account under review")
    if await card_testing_blocked(s, acct):
        raise HTTPException(429, "too many failed payments; try again later")


@router.post("/checkout/supporter-pass")
async def supporter_checkout(body: SupporterCheckout, acct: Account = Depends(current_account),
                             s: AsyncSession = Depends(get_session)):
    await _guard(s, acct)
    creator = await s.scalar(select(Creator).where(Creator.handle == body.creator_handle))
    if creator is None:
        raise HTTPException(404, "creator not found")
    if creator.account_id == acct.id:
        raise HTTPException(400, "cannot support yourself")
    active = await s.scalar(select(func.count()).select_from(Subscription).where(
        Subscription.account_id == acct.id, Subscription.creator_id == creator.account_id,
        Subscription.status == "active"))
    if active:
        raise HTTPException(409, "already supporting")
    st = get_settings()
    return await dodo.create_checkout(
        product_id=st.dodo_product_supporter_pass, name="CreatorLoop fan", email=None,
        return_url=f"{st.web_base_url}/c/{creator.handle}?checkout=done",
        metadata={"kind": "supporter_pass", "account_id": str(acct.id), "creator_id": str(creator.account_id)})


@router.post("/checkout/boost")
async def boost_checkout(body: BoostCheckout, acct: Account = Depends(current_account),
                         s: AsyncSession = Depends(get_session)):
    await _guard(s, acct)
    post = await s.get(Post, body.post_id)
    if post is None or post.hidden:
        raise HTTPException(404, "post not found")
    if post.account_id != acct.id:
        raise HTTPException(403, "you can only boost your own posts")
    st = get_settings()
    return await dodo.create_checkout(
        product_id=st.dodo_product_boost_pack, name="CreatorLoop creator", email=None,
        return_url=f"{st.web_base_url}/post/{post.post_id}?checkout=done",
        metadata={"kind": "boost", "account_id": str(acct.id), "post_id": str(post.post_id)})


@router.post("/webhooks/dodo")
async def dodo_webhook(request: Request, s: AsyncSession = Depends(get_session)):
    """Verified on the raw body (Standard Webhooks). The redirect back from checkout never fulfils anything."""
    raw = await request.body()
    headers = {k: request.headers.get(k) for k in ("webhook-id", "webhook-timestamp", "webhook-signature")}
    try:
        return await process_webhook(s, raw, headers)
    except BadSignature as e:
        raise HTTPException(401, "bad signature") from e
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e


@router.get("/me/creator-fund")
async def creator_fund(acct: Account = Depends(current_account), s: AsyncSession = Depends(get_session)):
    bal = await ledger.balance(s, acct.id)
    entries = (await s.scalars(select(LedgerEntry).where(LedgerEntry.account == f"creator_fund:{acct.id}")
                               .order_by(LedgerEntry.id.desc()).limit(20))).all()
    return {**bal, "currency": entries[0].currency if entries else "INR",
            "last_entries": [{"amount_minor": e.amount_minor, "held": e.held, "ref": e.ref,
                              "at": e.created_at.isoformat()} for e in entries]}


@router.get("/me/earnings")
async def earnings(acct: Account = Depends(current_account), s: AsyncSession = Depends(get_session)):
    """Claimable LOOP epochs with Merkle proofs, plus the fiat creator-fund balance."""
    from ..db.models import Allocation, Epoch

    rows = (await s.execute(select(Allocation, Epoch).join(Epoch, Epoch.id == Allocation.epoch_id).where(
        Allocation.account_id == acct.id, Epoch.status == "published").order_by(Epoch.id.desc()))).all()
    bal = await ledger.balance(s, acct.id)
    return {"loop": [{"epochId": e.onchain_epoch_id, "amount": str(a.amount_wei), "proof": a.proof,
                      "claimed": a.claimed} for a, e in rows],
            "frozen": acct.status == "frozen",
            "creatorFund": {"available_minor": bal["available_minor"], "held_minor": bal["held_minor"],
                            "currency": "INR"}}


@router.get("/creators/{handle}/supporters")
async def supporters(handle: str, s: AsyncSession = Depends(get_session)):
    c = await s.scalar(select(Creator).where(Creator.handle == handle))
    if c is None:
        raise HTTPException(404, "creator not found")
    n = await s.scalar(select(func.count()).select_from(Subscription).where(
        Subscription.creator_id == c.account_id, Subscription.status == "active"))
    return {"count": n or 0}
