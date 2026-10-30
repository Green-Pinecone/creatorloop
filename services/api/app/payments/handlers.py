"""Verified Dodo events -> payments, subscriptions, ledger, boosts, graph + viaSocket outbox events.
Everything for one event happens in the caller's transaction, so a crash can never half-apply it."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from standardwebhooks.webhooks import Webhook

from ..db.models import Account, FraudCase, Payment, Post, Subscription, WebhookEvent, utcnow
from ..outbox import enqueue
from ..signals import hmac_hex
from . import ledger
from .dodo import webhook_secret

log = structlog.get_logger()
OPEN_CASE = ("open", "investigating", "recommended")
BOOST_HOURS = 24


class BadSignature(Exception):
    pass


def verify(raw: bytes, headers: dict[str, str | None]) -> dict:
    secret = webhook_secret()
    if not secret:
        raise RuntimeError("webhook secret not configured")
    try:
        return Webhook(secret).verify(raw, {k: v or "" for k, v in headers.items()})
    except Exception as e:  # noqa: BLE001 - library raises its own error types for bad sig / stale timestamp
        raise BadSignature(str(e)) from e


def sign(payload: dict, msg_id: str | None = None) -> tuple[bytes, dict[str, str]]:
    """Build a correctly signed delivery (tests and the dev simulator)."""
    raw = json.dumps(payload).encode()
    msg_id = msg_id or "msg_" + uuid.uuid4().hex
    now = datetime.now(UTC)
    sig = Webhook(webhook_secret()).sign(msg_id, now, raw.decode())
    return raw, {"webhook-id": msg_id, "webhook-timestamp": str(int(now.timestamp())), "webhook-signature": sig}


async def process_webhook(s: AsyncSession, raw: bytes, headers: dict[str, str | None]) -> dict:
    event = verify(raw, headers)
    msg_id = headers["webhook-id"] or ""
    if await s.get(WebhookEvent, ("dodo", msg_id)) is not None:
        return {"ok": True, "duplicate": True}
    s.add(WebhookEvent(provider="dodo", event_id=msg_id, payload=event, processed_at=utcnow()))
    await dispatch(s, event)
    try:
        await s.commit()
    except IntegrityError:  # concurrent redelivery of the same webhook-id
        await s.rollback()
        return {"ok": True, "duplicate": True}
    return {"ok": True}


def _uuid(v) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _in_open_case(s: AsyncSession, *ids: uuid.UUID | None) -> bool:
    want = {str(i) for i in ids if i}
    cases = (await s.scalars(select(FraudCase).where(FraudCase.status.in_(OPEN_CASE)))).all()
    return any(want & set(c.member_ids) for c in cases)


async def dispatch(s: AsyncSession, event: dict) -> None:
    kind, data = event.get("type", ""), event.get("data") or {}
    if kind in ("payment.succeeded", "payment.failed"):
        await _payment(s, kind, data)
    elif kind.startswith("subscription."):
        await _subscription(s, kind, data)
    elif kind == "refund.succeeded":
        await _refund(s, data)
    elif kind.startswith("dispute."):
        await _dispute(s, kind, data)
    else:
        log.info("dodo.unhandled_event", type=kind)  # stored in webhook_events; 200 so Dodo stops retrying


async def _payment(s: AsyncSession, kind: str, d: dict) -> None:
    pid = d["payment_id"]
    meta = d.get("metadata") or {}
    sub_id = d.get("subscription_id")
    acct_id, creator_id = _uuid(meta.get("account_id")), _uuid(meta.get("creator_id"))
    if sub_id and (acct_id is None or creator_id is None):  # renewals can arrive without our metadata
        sub = await s.get(Subscription, sub_id)
        if sub:
            acct_id, creator_id = acct_id or sub.account_id, creator_id or sub.creator_id
    email = ((d.get("customer") or {}).get("email") or "").lower()
    ok = kind == "payment.succeeded"
    amount = int(d.get("total_amount", 0))
    pay = await s.get(Payment, pid)
    if pay is None:
        pay = Payment(dodo_payment_id=pid, kind=meta.get("kind", "unknown"), raw=d, created_at=utcnow(),
                      amount_minor=amount, currency=d.get("currency", "INR"), status="failed")
        s.add(pay)
    elif pay.status in ("refunded", "disputed"):
        return  # never regress a payment that already moved on
    already_ok = pay.status == "succeeded"
    pay.account_id, pay.creator_id = acct_id, creator_id
    pay.subscription_id, pay.dodo_customer_id = sub_id, (d.get("customer") or {}).get("customer_id")
    pay.payer_hash = hmac_hex(email) if email else None
    pay.post_id = int(meta["post_id"]) if str(meta.get("post_id", "")).isdigit() else None
    pay.status = "succeeded" if ok else "failed"
    if not ok:
        await enqueue(s, "viasocket.emit", {"event": "payment.failed", "payment_id": pid})
        return
    if already_ok:
        return
    if pay.kind == "supporter_pass" and creator_id:
        held = await _in_open_case(s, acct_id, creator_id)
        ledger.record_supporter_payment(s, payment_id=pid, creator_id=creator_id, amount_minor=amount,
                                        currency=pay.currency, held=held)
    else:
        ledger.record_boost_payment(s, payment_id=pid, amount_minor=amount, currency=pay.currency)
        if pay.kind == "boost" and pay.post_id is not None and (post := await s.get(Post, pay.post_id)):
            now = utcnow()
            base = max(_aware(post.boost_until), now) if post.boost_until else now  # stacking extends
            post.boost_until = base + timedelta(hours=BOOST_HOURS)
    if acct_id:
        await enqueue(s, "graph.upsert", {"kind": "PAID", "src": str(acct_id), "dst": str(creator_id or acct_id),
                                          "payment_id": pid, "amount_minor": amount, "status": "succeeded",
                                          "product": pay.kind, "ts": utcnow().isoformat()})
        if pay.payer_hash:
            await enqueue(s, "graph.upsert", {"kind": "PAID_WITH", "src": str(acct_id), "dst": pay.payer_hash})
    await enqueue(s, "viasocket.emit", {"event": "payment.succeeded", "payment_id": pid, "kind": pay.kind,
                                        "creator_id": str(creator_id) if creator_id else None,
                                        "amount_minor": amount, "currency": pay.currency})


async def _subscription(s: AsyncSession, kind: str, d: dict) -> None:
    sid = d["subscription_id"]
    status = {"subscription.active": "active", "subscription.renewed": "active",
              "subscription.on_hold": "on_hold", "subscription.failed": "failed",
              "subscription.cancelled": "cancelled", "subscription.expired": "expired"}.get(kind)
    if status is None:
        return
    meta = d.get("metadata") or {}
    sub = await s.get(Subscription, sid)
    if sub is None:
        sub = Subscription(dodo_subscription_id=sid, status=status,
                           account_id=_uuid(meta.get("account_id")), creator_id=_uuid(meta.get("creator_id")))
        s.add(sub)
    sub.status, sub.updated_at = status, utcnow()
    if end := d.get("next_billing_date"):
        sub.current_period_end = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    if status in ("on_hold", "failed"):
        await enqueue(s, "viasocket.emit", {"event": "subscription.at_risk", "subscription_id": sid,
                                            "account_id": str(sub.account_id) if sub.account_id else None})


async def _refund(s: AsyncSession, d: dict) -> None:
    pay = await s.get(Payment, d["payment_id"])
    if pay is None:
        return
    rid = d.get("refund_id") or f"refund:{pay.dodo_payment_id}"
    reversed_ = await ledger.reverse_payment(s, pay.dodo_payment_id, rid)
    if reversed_ or pay.status == "succeeded":
        pay.status = "refunded"
        if pay.account_id:
            await enqueue(s, "graph.upsert", {"kind": "PAID", "src": str(pay.account_id),
                                              "dst": str(pay.creator_id or pay.account_id),
                                              "payment_id": pay.dodo_payment_id, "amount_minor": pay.amount_minor,
                                              "status": "refunded", "product": pay.kind})


async def _dispute(s: AsyncSession, kind: str, d: dict) -> None:
    pay = await s.get(Payment, d.get("payment_id", ""))
    if pay is None:
        return
    if kind == "dispute.opened":
        pay.status = "disputed"
        await ledger.set_held(s, [pay.dodo_payment_id], True)  # hold until the dispute is resolved
        await enqueue(s, "viasocket.emit", {"event": "dispute.opened", "payment_id": pay.dodo_payment_id})
    elif kind in ("dispute.won", "dispute.cancelled"):
        pay.status = "succeeded"
        await ledger.set_held(s, [pay.dodo_payment_id], False)
    elif kind in ("dispute.lost", "dispute.accepted"):
        pay.status = "refunded"
        await ledger.reverse_payment(s, pay.dodo_payment_id, f"dispute:{pay.dodo_payment_id}")


async def card_testing_blocked(s: AsyncSession, acct: Account) -> bool:
    """More than 5 failed payments within any 10 minutes in the last hour blocks checkout for that hour."""
    since = utcnow() - timedelta(hours=1)
    rows = (await s.scalars(select(Payment).where(Payment.account_id == acct.id, Payment.status == "failed"))).all()
    fails = sorted(t for p in rows if (t := _aware(p.created_at)) >= since)
    return any(fails[i + 5] - fails[i] <= timedelta(minutes=10) for i in range(len(fails) - 5))
