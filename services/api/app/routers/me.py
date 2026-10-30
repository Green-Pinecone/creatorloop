import uuid
from datetime import UTC, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_account, ensure_active
from ..db.models import Account, DeviceSignal, Follow, Post, Reaction, utcnow
from ..db.session import get_session
from ..outbox import enqueue
from ..ratelimit import enforce
from ..signals import hmac_hex, ip_bucket, ua_family

router = APIRouter(prefix="/me", tags=["me"])
UNDO_WINDOW = timedelta(hours=24)


def _trust_tier(score: float) -> str:
    return "high" if score >= 0.7 else "medium" if score >= 0.4 else "low"


@router.get("")
async def me(acct: Account = Depends(current_account)):
    return {"id": str(acct.id), "wallet": acct.wallet_address, "status": acct.status,
            "trust_tier": _trust_tier(acct.trust_score)}


def _client_ip(req: Request) -> str:
    # Only trust X-Forwarded-For when the direct peer is a known local proxy/tunnel.
    peer = req.client.host if req.client else "0.0.0.0"
    if peer in ("127.0.0.1", "::1") and (xff := req.headers.get("x-forwarded-for")):
        return xff.split(",")[0].strip()
    return peer


@router.post("/reactions/{post_id}")
async def react(post_id: int, acct: Account = Depends(current_account), s: AsyncSession = Depends(get_session)):
    ensure_active(acct)
    post = await s.get(Post, post_id)
    if post is None or post.hidden:
        raise HTTPException(404, "post not found")
    if post.account_id == acct.id:
        raise HTTPException(400, "cannot react to your own post")
    if await s.get(Reaction, (acct.id, post_id)) is None:
        await enforce(s, acct, "reactions")
        # domain row and outbox row commit together (transactional outbox)
        s.add(Reaction(account_id=acct.id, post_id=post_id))
        await enqueue(s, "graph.upsert", {"kind": "REACTED", "src": str(acct.id), "dst": post_id,
                                          "ts": utcnow().isoformat()})
    await s.commit()
    return {"ok": True}


@router.delete("/reactions/{post_id}")
async def unreact(post_id: int, acct: Account = Depends(current_account), s: AsyncSession = Depends(get_session)):
    ensure_active(acct)
    r = await s.get(Reaction, (acct.id, post_id))
    if r is None:
        return {"ok": True}
    if utcnow() - r.created_at.replace(tzinfo=r.created_at.tzinfo or UTC) > UNDO_WINDOW:
        raise HTTPException(409, "reactions can only be removed within 24h")
    await s.delete(r)
    await enqueue(s, "graph.upsert", {"kind": "UNREACTED", "src": str(acct.id), "dst": post_id,
                                      "ts": utcnow().isoformat()})
    await s.commit()
    return {"ok": True}


@router.post("/follows/{creator_id}")
async def follow(creator_id: str, acct: Account = Depends(current_account), s: AsyncSession = Depends(get_session)):
    ensure_active(acct)
    try:
        cid = uuid.UUID(creator_id)
    except ValueError as e:
        raise HTTPException(422, "invalid creator id") from e
    if cid == acct.id:
        raise HTTPException(400, "cannot follow yourself")
    if await s.get(Account, cid) is None:
        raise HTTPException(404, "creator not found")
    if await s.get(Follow, (acct.id, cid)) is None:
        await enforce(s, acct, "follows")
        s.add(Follow(follower_id=acct.id, creator_id=cid))
        await enqueue(s, "graph.upsert", {"kind": "FOLLOWS", "src": str(acct.id), "dst": str(cid),
                                          "ts": utcnow().isoformat()})
    await s.commit()
    return {"ok": True}


class SessionIn(BaseModel):
    device_id: str = Field(min_length=8, max_length=128)
    tz: str | None = None
    screen: str | None = None
    ua: str = ""


@router.post("/session")
async def session_signals(body: SessionIn, request: Request, acct: Account = Depends(current_account),
                          s: AsyncSession = Depends(get_session)):
    dev, net = hmac_hex(body.device_id), ip_bucket(_client_ip(request))
    row = await s.get(DeviceSignal, (acct.id, dev, net))
    if row is None:
        s.add(DeviceSignal(account_id=acct.id, device_hash=dev, ip_bucket_hash=net, ua_family=ua_family(body.ua)))
        ts = utcnow().isoformat()
        await enqueue(s, "graph.upsert", {"kind": "USED_DEVICE", "src": str(acct.id), "dst": dev, "ts": ts})
        await enqueue(s, "graph.upsert", {"kind": "SEEN_ON_NET", "src": str(acct.id), "dst": net, "ts": ts})
    else:
        row.hits += 1
        row.last_seen = utcnow()
    await s.commit()
    return {"ok": True}

