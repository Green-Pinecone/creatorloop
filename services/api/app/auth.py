import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db.models import Account
from .db.session import get_session

DEV_ISSUER = "creatorloop-dev"


def make_dev_token(privy_user_id: str, secret: str | None = None) -> str:
    secret = secret or get_settings().dev_auth_secret
    return jwt.encode(
        {"sub": privy_user_id, "iss": DEV_ISSUER, "aud": "creatorloop"}, secret, algorithm="HS256"
    )


def verify_token(token: str) -> dict:
    """Privy ES256 JWT when configured; otherwise HS256 dev tokens (only if DEV_AUTH_SECRET is set)."""
    s = get_settings()
    try:
        if s.privy_app_id and s.privy_verification_key:
            return jwt.decode(
                token, s.privy_verification_key, algorithms=["ES256"], audience=s.privy_app_id, issuer="privy.io"
            )
        if s.dev_auth_secret:
            return jwt.decode(
                token, s.dev_auth_secret, algorithms=["HS256"], audience="creatorloop", issuer=DEV_ISSUER
            )
    except jwt.PyJWTError as e:
        raise HTTPException(401, "invalid token") from e
    raise HTTPException(503, "authentication is not configured")


async def current_account(authorization: str = Header(...), s: AsyncSession = Depends(get_session)) -> Account:
    claims = verify_token(authorization.removeprefix("Bearer ").strip())
    acct = await s.scalar(select(Account).where(Account.privy_user_id == claims["sub"]))
    if acct is None:
        acct = Account(privy_user_id=claims["sub"])
        s.add(acct)
        await s.commit()
    return acct


def ensure_active(acct: Account) -> None:
    """Frozen/banned accounts can read and log in, but every write is refused."""
    if acct.status in ("frozen", "banned"):
        raise HTTPException(403, f"account {acct.status}")
