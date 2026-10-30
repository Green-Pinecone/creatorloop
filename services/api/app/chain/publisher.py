"""On-chain writes: publishing epoch roots (publisher key) and freezing accounts (enforcer key).
Both are no-ops with a log line when the relevant key is not configured, so dev works without a chain."""

import asyncio
import json

import structlog
from eth_utils import keccak
from web3 import AsyncHTTPProvider, AsyncWeb3

from ..config import get_settings
from ..outbox import register
from .indexer import ABI_DIR, load_deployment

log = structlog.get_logger()
_tx_lock = asyncio.Lock()  # one in-flight tx per process; avoids nonce collisions between overlapping runs


class Underfunded(RuntimeError):
    pass


def _abi(name: str) -> list:
    return json.loads((ABI_DIR / f"{name}.json").read_text())


def _w3() -> AsyncWeb3:
    return AsyncWeb3(AsyncHTTPProvider(get_settings().amoy_rpc_url))


def publisher_configured() -> bool:
    return bool(get_settings().publisher_private_key)


async def _send(w3: AsyncWeb3, key: str, fn) -> tuple[str, dict]:
    acct = w3.eth.account.from_key(key)
    async with _tx_lock:
        tx = await fn.build_transaction({
            "from": acct.address, "chainId": await w3.eth.chain_id,
            "nonce": await w3.eth.get_transaction_count(acct.address, "pending")})
        h = await w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
        rcpt = await w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if rcpt["status"] != 1:
        raise RuntimeError(f"transaction {h.hex()} reverted")
    return h.to_0x_hex(), rcpt


async def publish_epoch(root_hex: str, total_wei: int, allocations_cid: str) -> tuple[str, int]:
    """Returns (tx_hash, on-chain epochId). Raises Underfunded before sending if the distributor can't cover it."""
    st, dep, w3 = get_settings(), load_deployment(), _w3()
    dist = w3.eth.contract(address=dep["RewardDistributor"], abi=_abi("RewardDistributor"))
    token = w3.eth.contract(address=dep["LoopToken"], abi=_abi("LoopToken"))
    balance = await token.functions.balanceOf(dep["RewardDistributor"]).call()
    outstanding = await dist.functions.outstanding().call()
    if balance < outstanding + total_wei:
        raise Underfunded(f"distributor holds {balance}, needs {outstanding + total_wei}")
    tx, rcpt = await _send(w3, st.publisher_private_key,
                           dist.functions.publishEpoch(bytes.fromhex(root_hex.removeprefix("0x")), total_wei,
                                                       allocations_cid))
    ev = dist.events.EpochPublished().process_receipt(rcpt)
    return tx, int(ev[0]["args"]["epochId"])


@register("chain.set_frozen")
async def set_frozen(payload: dict) -> None:
    st = get_settings()
    if not st.enforcer_private_key:
        log.info("chain.set_frozen.skipped_no_enforcer_key", wallet=payload["wallet"])
        return
    dep, w3 = load_deployment(), _w3()
    dist = w3.eth.contract(address=dep["RewardDistributor"], abi=_abi("RewardDistributor"))
    case_ref = keccak(text=payload.get("case_id", ""))
    await _send(w3, st.enforcer_private_key,
                dist.functions.setFrozen(w3.to_checksum_address(payload["wallet"]), bool(payload["frozen"]), case_ref))
