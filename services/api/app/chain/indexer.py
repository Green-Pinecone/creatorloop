import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import structlog
from eth_utils import event_abi_to_log_topic
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3._utils.events import get_event_data

from ..config import get_settings
from ..db.models import ChainCursor
from ..db.session import session_maker
from .events import apply_event

log = structlog.get_logger()
ABI_DIR = Path(__file__).parent / "abis"
CONTRACTS = ("CreatorPosts", "Tipping", "RewardDistributor")
CONFIRMATIONS = 5
BATCH = 2_000


def load_deployment() -> dict:
    return json.loads((ABI_DIR / "deployments.json").read_text())


def event_registry() -> dict[bytes, dict]:
    reg = {}
    for c in CONTRACTS:
        for item in json.loads((ABI_DIR / f"{c}.json").read_text()):
            if item["type"] == "event":
                reg[bytes(event_abi_to_log_topic(item))] = item
    return reg


async def index_range(w3: AsyncWeb3, frm: int, to: int, addresses: list[str], reg: dict) -> int:
    """Index blocks frm..to atomically with the cursor update. Returns the number of events applied."""
    logs = await w3.eth.get_logs({"fromBlock": frm, "toBlock": to, "address": addresses})
    block_ts: dict[int, datetime] = {}
    n = 0
    async with session_maker()() as s, s.begin():
        for lg in sorted(logs, key=lambda x: (x["blockNumber"], x["logIndex"])):
            abi = reg.get(bytes(lg["topics"][0]))
            if abi is None:
                continue
            ev = get_event_data(w3.codec, abi, lg)
            bn = lg["blockNumber"]
            if bn not in block_ts:
                blk = await w3.eth.get_block(bn)
                block_ts[bn] = datetime.fromtimestamp(blk["timestamp"], UTC)
            await apply_event(
                s, abi["name"], dict(ev["args"]),
                {"block_number": bn, "tx_hash": lg["transactionHash"].to_0x_hex(),
                 "log_index": lg["logIndex"], "timestamp": block_ts[bn]},
            )
            n += 1
        cur = await s.get(ChainCursor, "main")
        if cur is None:
            s.add(ChainCursor(name="main", last_block=to))
        else:
            cur.last_block = to
    return n


async def run_indexer(stop: asyncio.Event) -> None:
    st = get_settings()
    dep = load_deployment()
    w3 = AsyncWeb3(AsyncHTTPProvider(st.amoy_rpc_url))
    reg = event_registry()
    addresses = [dep[c] for c in CONTRACTS]
    start = st.chain_start_block if st.chain_start_block is not None else dep["startBlock"]
    batch = BATCH
    while not stop.is_set():
        try:
            safe = await w3.eth.block_number - CONFIRMATIONS
            async with session_maker()() as s:
                row = await s.get(ChainCursor, "main")
            cur = row.last_block if row else start - 1
            if cur >= safe:
                await asyncio.sleep(4)
                continue
            to = min(cur + batch, safe)
            await index_range(w3, cur + 1, to, addresses, reg)
            batch = BATCH
        except Exception as e:  # noqa: BLE001 - provider hiccups must not kill the loop
            batch = max(batch // 2, 50)  # shrink on provider range errors
            log.warning("indexer.error", error=str(e), batch=batch)
            await asyncio.sleep(3)
