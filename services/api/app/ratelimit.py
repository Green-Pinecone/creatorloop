from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Account, Follow, Reaction, utcnow

LIMITS = {"reactions": 300, "follows": 100}  # per account per hour


async def enforce(s: AsyncSession, acct: Account, kind: str) -> None:
    """Count-based limit from the domain tables. Exceeding it is also recorded as a risk signal."""
    model, col = (Reaction, Reaction.account_id) if kind == "reactions" else (Follow, Follow.follower_id)
    since = utcnow() - timedelta(hours=1)
    n = await s.scalar(select(func.count()).select_from(model).where(col == acct.id, model.created_at >= since))
    if (n or 0) >= LIMITS[kind]:
        flags = dict(acct.risk_flags or {})
        flags["rate_limited"] = int(flags.get("rate_limited", 0)) + 1
        acct.risk_flags = flags
        s.add(acct)
        await s.commit()
        raise HTTPException(429, f"too many {kind}; try again later")
