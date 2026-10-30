from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Creator, Post, Reaction, Tip, utcnow
from ..db.session import get_session

router = APIRouter(tags=["public"])


async def _post_view(s: AsyncSession, p: Post) -> dict:
    reactions = await s.scalar(select(func.count()).select_from(Reaction).where(Reaction.post_id == p.post_id))
    tips = (await s.scalars(select(Tip.amount_wei).where(Tip.post_id == p.post_id))).all()
    return {
        "post_id": p.post_id, "author_wallet": p.author_wallet, "caption": p.caption,
        "metadata_cid": p.metadata_cid, "created_at": p.created_at.isoformat(),
        "boosted": bool(p.boost_until and p.boost_until.replace(tzinfo=p.boost_until.tzinfo or UTC) > utcnow()),
        "reactions": reactions or 0, "tip_total_wei": str(sum(tips)), "tip_count": len(tips),
    }


@router.get("/feed")
async def feed(cursor: str | None = None, limit: int = Query(20, ge=1, le=50),
               s: AsyncSession = Depends(get_session)):
    """Keyset pagination on (created_at, post_id). Cursor is '<iso>|<post_id>'."""
    q = select(Post).where(Post.hidden.is_(False), Post.moderation != "removed")
    if cursor:
        try:
            ts_s, pid_s = cursor.split("|")
            ts, pid = datetime.fromisoformat(ts_s), int(pid_s)
        except ValueError as e:
            raise HTTPException(422, "bad cursor") from e
        q = q.where(or_(Post.created_at < ts, and_(Post.created_at == ts, Post.post_id < pid)))
    posts = (await s.scalars(q.order_by(Post.created_at.desc(), Post.post_id.desc()).limit(limit + 1))).all()
    page = posts[:limit]
    nxt = f"{page[-1].created_at.isoformat()}|{page[-1].post_id}" if len(posts) > limit else None
    return {"items": [await _post_view(s, p) for p in page], "next_cursor": nxt}


@router.get("/posts/{post_id}")
async def post_detail(post_id: int, s: AsyncSession = Depends(get_session)):
    p = await s.get(Post, post_id)
    if p is None or p.hidden:
        raise HTTPException(404, "post not found")
    recent = (await s.scalars(select(Tip).where(Tip.post_id == post_id).order_by(Tip.block_number.desc()).limit(10))).all()
    return {**await _post_view(s, p),
            "recent_tips": [{"from": t.from_wallet, "amount_wei": str(t.amount_wei)} for t in recent]}


@router.get("/creators/{handle}")
async def creator(handle: str, s: AsyncSession = Depends(get_session)):
    c = await s.scalar(select(Creator).where(Creator.handle == handle))
    if c is None:
        raise HTTPException(404, "creator not found")
    posts = (await s.scalars(select(Post).where(Post.account_id == c.account_id, Post.hidden.is_(False))
                             .order_by(Post.created_at.desc()).limit(20))).all()
    return {"handle": c.handle, "display_name": c.display_name, "bio": c.bio,
            "posts": [await _post_view(s, p) for p in posts]}
