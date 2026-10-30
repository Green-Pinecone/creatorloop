import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import config, outbox
from app.auth import make_dev_token
from app.db import session as dbs
from app.db.models import Base, Creator, FraudCase, LedgerEntry, Payment, Post
from app.graph import sync
from app.graph.trust import run_trust_job
from app.main import app
from app.payments import ledger
from app.payments.handlers import sign

SECRET = "test-secret-test-secret-test-secret"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("DEV_AUTH_SECRET", SECRET)
    monkeypatch.setenv("DEV_DODO", "true")
    monkeypatch.setenv("ADMIN_PRIVY_USER_IDS", "admin1")
    monkeypatch.setenv("PRIVY_APP_ID", "")
    config.get_settings.cache_clear()
    engine = dbs.init_engine()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    sync.store.clear()
    yield
    await engine.dispose()
    config.get_settings.cache_clear()


@pytest.fixture
async def client(env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def hdr(user):
    return {"Authorization": f"Bearer {make_dev_token(user, SECRET)}"}


async def account(client, user) -> uuid.UUID:
    return uuid.UUID((await client.get("/me", headers=hdr(user))).json()["id"])


async def make_creator(client, user="creator", handle="alice") -> uuid.UUID:
    cid = await account(client, user)
    async with dbs.session_maker()() as s, s.begin():
        s.add(Creator(account_id=cid, handle=handle, display_name="Alice"))
    return cid


async def fund(client, cid):
    async with dbs.session_maker()() as s:
        return await ledger.balance(s, cid)


async def buy_pass(client, fan="fan", handle="alice"):
    r = await client.post("/checkout/supporter-pass", json={"creator_handle": handle}, headers=hdr(fan))
    assert r.status_code == 200, r.text
    return (await client.get(r.json()["checkout_url"].replace("http://localhost:8000", ""))).json()["payment_id"]


def test_split_rounds_down_and_conserves():
    assert ledger.split(14900, 7000) == (10430, 4470)
    assert ledger.split(101, 7000) == (70, 31)  # remainder goes to the platform
    for amt in (1, 99, 14900, 12345678):
        a, b = ledger.split(amt, 7000)
        assert a + b == amt


async def test_signature_and_idempotency(client):
    cid = await make_creator(client)
    fan = await account(client, "fan")
    meta = {"kind": "supporter_pass", "account_id": str(fan), "creator_id": str(cid)}
    ev = {"type": "payment.succeeded", "data": {"payment_id": "pay_1", "total_amount": 14900, "currency": "INR",
                                                "metadata": meta, "customer": {"email": "F@x.com"}}}
    raw, h = sign(ev, "msg_1")
    bad = await client.post("/webhooks/dodo", content=raw.replace(b"14900", b"99999"), headers=h)
    assert bad.status_code == 401
    assert (await client.post("/webhooks/dodo", content=raw, headers={**h, "webhook-signature": "v1,AAAA"})).status_code == 401
    assert (await client.post("/webhooks/dodo", content=raw, headers=h)).json() == {"ok": True}
    assert (await client.post("/webhooks/dodo", content=raw, headers=h)).json()["duplicate"] is True
    assert (await fund(client, cid))["available_minor"] == 10430  # credited exactly once


async def test_supporter_pass_end_to_end(client):
    cid = await make_creator(client)
    pid = await buy_pass(client)
    assert (await fund(client, cid)) == {"available_minor": 10430, "held_minor": 0, "lifetime_minor": 10430}
    assert (await client.get("/creators/alice/supporters")).json() == {"count": 1}
    mine = (await client.get("/me/creator-fund", headers=hdr("creator"))).json()
    assert mine["available_minor"] == 10430 and mine["last_entries"][0]["ref"] == pid
    r = await client.post("/checkout/supporter-pass", json={"creator_handle": "alice"}, headers=hdr("fan"))
    assert r.status_code == 409  # already supporting
    assert (await client.post("/checkout/supporter-pass", json={"creator_handle": "alice"},
                              headers=hdr("creator"))).status_code == 400
    summary = (await client.get("/admin/ledger/summary", headers=hdr("admin1"))).json()
    assert summary["invariant_ok"] and summary["totals_minor"]["platform_revenue"] == 4470
    assert sum(summary["totals_minor"].values()) == 0
    # graph + viaSocket events were queued in the same transaction
    async with dbs.session_maker()() as s:
        topics = [o.topic for o in (await s.scalars(select(outbox.Outbox))).all()]
    assert "viasocket.emit" in topics and "graph.upsert" in topics


async def test_boost_marks_post_and_is_all_platform_revenue(client):
    cid = await make_creator(client)
    async with dbs.session_maker()() as s, s.begin():
        s.add(Post(post_id=7, author_wallet="0xabc", account_id=cid, metadata_cid="b", block_number=1,
                   tx_hash="0x", created_at=datetime.now(UTC)))
    assert (await client.post("/checkout/boost", json={"post_id": 7}, headers=hdr("fan"))).status_code == 403
    r = await client.post("/checkout/boost", json={"post_id": 7}, headers=hdr("creator"))
    await client.get(r.json()["checkout_url"].replace("http://localhost:8000", ""))
    assert (await client.get("/feed")).json()["items"][0]["boosted"] is True
    async with dbs.session_maker()() as s:
        assert (await s.scalar(select(LedgerEntry).where(LedgerEntry.account == "platform_revenue"))).amount_minor == 9900
    assert (await fund(client, cid))["lifetime_minor"] == 0


async def test_refund_reverts_balances_exactly_and_is_idempotent(client):
    cid = await make_creator(client)
    pid = await buy_pass(client)
    for _ in range(2):
        await client.post("/dev/dodo/event", json={"type": "refund.succeeded", "webhook_id": f"m{_}",
                                                   "data": {"payment_id": pid, "refund_id": "rf_1"}})
    assert (await fund(client, cid))["lifetime_minor"] == 0
    async with dbs.session_maker()() as s:
        assert (await s.get(Payment, pid)).status == "refunded"
        assert await ledger.unbalanced_txns(s) == []
        rows = (await s.scalars(select(LedgerEntry))).all()
    assert sum(e.amount_minor for e in rows) == 0 and len(rows) == 6  # 3 original + 3 reversing, once


async def test_dispute_holds_then_releases(client):
    cid = await make_creator(client)
    pid = await buy_pass(client)
    await client.post("/dev/dodo/event", json={"type": "dispute.opened", "data": {"payment_id": pid}})
    assert (await fund(client, cid)) == {"available_minor": 0, "held_minor": 10430, "lifetime_minor": 10430}
    await client.post("/dev/dodo/event", json={"type": "dispute.won", "data": {"payment_id": pid}})
    assert (await fund(client, cid))["available_minor"] == 10430


async def test_card_testing_blocks_checkout(client):
    await make_creator(client)
    fan = await account(client, "fan")
    for i in range(6):
        await client.post("/dev/dodo/event", json={"type": "payment.failed", "data": {
            "payment_id": f"f{i}", "total_amount": 100, "currency": "INR",
            "metadata": {"kind": "supporter_pass", "account_id": str(fan)}}})
    r = await client.post("/checkout/supporter-pass", json={"creator_handle": "alice"}, headers=hdr("fan"))
    assert r.status_code == 429


async def test_self_dealing_opens_case_and_holds_funds(client):
    cid = await make_creator(client)
    fan = await account(client, "fan")
    for who in ("fan", "creator"):  # same device behind both accounts
        await client.post("/me/session", json={"device_id": "same-device-123", "ua": "Chrome"}, headers=hdr(who))
    await buy_pass(client)
    while await outbox.process_once():  # drain graph.upsert into the in-memory graph
        pass
    async with dbs.session_maker()() as s:
        await run_trust_job(s, sync.store)
        cases = (await s.scalars(select(FraudCase).where(FraudCase.kind == "payment_abuse"))).all()
    assert len(cases) == 1 and set(cases[0].member_ids) == {str(fan), str(cid)}
    assert cases[0].evidence["reason"] == "self_dealing"
    assert (await fund(client, cid)) == {"available_minor": 0, "held_minor": 10430, "lifetime_minor": 10430}
    r = await client.post(f"/admin/cases/{cases[0].id}/decision", json={"decision": "dismiss"}, headers=hdr("admin1"))
    assert r.status_code == 200
    assert (await fund(client, cid))["available_minor"] == 10430  # released


async def test_webhook_without_secret_is_503(client, monkeypatch):
    monkeypatch.setenv("DEV_DODO", "false")
    config.get_settings.cache_clear()
    r = await client.post("/webhooks/dodo", content=b"{}", headers={"webhook-id": "x", "webhook-timestamp": "1",
                                                                     "webhook-signature": "v1,x"})
    assert r.status_code == 503
