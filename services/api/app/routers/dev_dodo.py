"""Mock Dodo (DEV_DODO=true): completes a mock checkout and simulates later events. Every delivery is signed with
the webhook secret and goes through process_webhook, the same code path as the real endpoint."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db.session import get_session
from ..payments import dodo
from ..payments.handlers import process_webhook, sign

SEED_PRICES = {"supporter_pass": 14900, "boost": 9900}


def _enabled() -> None:
    if not get_settings().dev_dodo:
        raise HTTPException(404, "not found")


router = APIRouter(prefix="/dev/dodo", tags=["dev-dodo"], dependencies=[Depends(_enabled)])


async def deliver(s: AsyncSession, event_type: str, data: dict, msg_id: str | None = None) -> dict:
    raw, headers = sign({"type": event_type, "timestamp": "2026-01-01T00:00:00Z", "data": data}, msg_id)
    return await process_webhook(s, raw, headers)


@router.get("/pay/{session_id}")
async def pay(session_id: str, s: AsyncSession = Depends(get_session)):
    """Stands in for the hosted checkout page: 'pays' successfully and fires the same events Dodo would."""
    sess = dodo.MOCK_SESSIONS.get(session_id)
    if sess is None:
        raise HTTPException(404, "unknown session")
    kind, pid = sess["kind"], "pay_" + uuid.uuid4().hex[:12]
    data = {"payment_id": pid, "total_amount": SEED_PRICES.get(kind, 9900), "currency": "INR",
            "customer": {"customer_id": "cus_" + uuid.uuid4().hex[:8], "email": sess["email"] or "fan@example.com"},
            "metadata": sess["metadata"]}
    if kind == "supporter_pass":
        sub_id = "sub_" + uuid.uuid4().hex[:12]
        data["subscription_id"] = sub_id
        await deliver(s, "subscription.active", {"subscription_id": sub_id, "metadata": sess["metadata"]})
    await deliver(s, "payment.succeeded", data)
    return {"ok": True, "payment_id": pid}


class Simulate(BaseModel):
    type: str  # refund.succeeded | dispute.opened | payment.failed | ...
    data: dict
    webhook_id: str | None = None  # reuse to exercise idempotency


@router.post("/event")
async def simulate(body: Simulate, s: AsyncSession = Depends(get_session)):
    return await deliver(s, body.type, body.data, body.webhook_id)
