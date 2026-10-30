"""Resolve who first funded a wallet (strongest cheap sybil signal). Uses the Etherscan v2 API; with no key
(e.g. against a local node) it is a logged no-op so the outbox row completes."""

from datetime import UTC, datetime

import httpx
import structlog

from ..config import get_settings
from ..db.models import WalletFunder
from ..db.session import session_maker
from ..outbox import enqueue, register

log = structlog.get_logger()
ETHERSCAN = "https://api.etherscan.io/v2/api"


def first_incoming(txs: list[dict], wallet: str) -> dict | None:
    """First tx (list is ascending) that sends native value to `wallet`."""
    for tx in txs:
        if tx.get("to", "").lower() == wallet and int(tx.get("value", 0)) > 0 and tx.get("isError") != "1":
            return tx
    return None


@register("chain.resolve_funder")
async def resolve_funder(payload: dict) -> None:
    wallet = payload["wallet"].lower()
    st = get_settings()
    if not st.etherscan_api_key:
        log.info("funder.skipped_no_api_key", wallet=wallet)
        return
    params = {"chainid": 80002, "module": "account", "action": "txlist", "address": wallet, "sort": "asc",
              "startblock": 0, "endblock": 99999999, "page": 1, "offset": 50, "apikey": st.etherscan_api_key}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(ETHERSCAN, params=params)
    r.raise_for_status()  # outbox retries with backoff on rate limits
    body = r.json()
    if body.get("status") != "1":
        if "rate limit" in str(body.get("result", "")).lower():
            raise RuntimeError("etherscan rate limited")
        return  # no transactions yet
    tx = first_incoming(body["result"], wallet)
    if tx is None:
        return
    funder = tx["from"].lower()
    async with session_maker()() as s, s.begin():
        if await s.get(WalletFunder, wallet) is None:
            s.add(WalletFunder(wallet=wallet, funder=funder, tx_hash=tx["hash"], amount_wei=int(tx["value"]),
                               funded_at=datetime.fromtimestamp(int(tx["timeStamp"]), UTC)))
            await enqueue(s, "graph.upsert", {"kind": "FUNDED_BY", "src": wallet, "dst": funder,
                                              "tx_hash": tx["hash"]})
