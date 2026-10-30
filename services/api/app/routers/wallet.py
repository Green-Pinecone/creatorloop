import hashlib
import hmac
import time

from eth_account import Account as EthAccount
from eth_account.messages import encode_defunct
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_account, ensure_active
from ..config import get_settings
from ..db.models import Account, Post
from ..db.session import get_session
from ..outbox import enqueue

router = APIRouter(prefix="/me/wallet", tags=["me"])
NONCE_TTL = 600


def _mac(account_id: str, exp: int) -> str:
    key = (get_settings().signal_hash_salt or get_settings().dev_auth_secret or "dev").encode()
    return hmac.new(key, f"wallet-link|{account_id}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def make_nonce(account_id: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + NONCE_TTL)
    return f"{exp}.{_mac(account_id, exp)}"


def check_nonce(account_id: str, nonce: str, now: float | None = None) -> bool:
    try:
        exp_s, mac = nonce.split(".")
        exp = int(exp_s)
    except ValueError:
        return False
    return exp >= (now or time.time()) and hmac.compare_digest(mac, _mac(account_id, exp))


def link_message(nonce: str) -> str:
    return f"Link this wallet to CreatorLoop\nnonce: {nonce}"


@router.get("/nonce")
async def nonce(acct: Account = Depends(current_account)):
    n = make_nonce(str(acct.id))
    return {"nonce": n, "message": link_message(n)}


class LinkIn(BaseModel):
    nonce: str
    signature: str


@router.post("")
async def link(body: LinkIn, acct: Account = Depends(current_account), s: AsyncSession = Depends(get_session)):
    """The signature proves control of the address, so nobody can claim someone else's wallet."""
    ensure_active(acct)
    if not check_nonce(str(acct.id), body.nonce):
        raise HTTPException(400, "invalid or expired nonce")
    try:
        addr = EthAccount.recover_message(encode_defunct(text=link_message(body.nonce)), signature=body.signature)
    except Exception as e:  # noqa: BLE001 - any malformed signature is a client error
        raise HTTPException(400, "bad signature") from e
    addr = addr.lower()
    if acct.wallet_address and acct.wallet_address != addr:
        raise HTTPException(409, "a different wallet is already linked")
    other = await s.scalar(select(Account).where(Account.wallet_address == addr, Account.id != acct.id))
    if other:
        raise HTTPException(409, "wallet already linked to another account")
    acct.wallet_address = addr
    s.add(acct)
    # attach posts that were indexed before the wallet was linked
    await s.execute(update(Post).where(Post.author_wallet == addr, Post.account_id.is_(None)).values(account_id=acct.id))
    await enqueue(s, "graph.upsert", {"kind": "OWNS_WALLET", "src": str(acct.id), "dst": addr})
    try:
        await s.commit()
    except IntegrityError as e:
        await s.rollback()
        raise HTTPException(409, "wallet already linked to another account") from e
    return {"wallet": addr}
