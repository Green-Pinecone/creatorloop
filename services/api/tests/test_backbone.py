from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import config, outbox
from app.auth import make_dev_token
from app.chain.events import apply_event
from app.db import session as dbs
from app.db.models import Account, Base, DeviceSignal, Outbox, Post, Reaction, Tip
from app.main import app
from app.signals import ip_bucket

SECRET = "test-secret-test-secret-test-secret"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("DEV_AUTH_SECRET", SECRET)
    monkeypatch.setenv("PRIVY_APP_ID", "")
    config.get_settings.cache_clear()
    engine = dbs.init_engine()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()
    config.get_settings.cache_clear()


def hdr(user: str) -> dict:
    return {"Authorization": f"Bearer {make_dev_token(user, SECRET)}"}


async def make_post(post_id=1, author="0xabc", account_id=None):
    async with dbs.session_maker()() as s, s.begin():
        s.add(Post(post_id=post_id, author_wallet=author, account_id=account_id, metadata_cid="bafy",
                   block_number=1, tx_hash="0x1", created_at=datetime.now(UTC)))


async def count(model):
    async with dbs.session_maker()() as s:
        return await s.scalar(select(func.count()).select_from(model))


@pytest.fixture
async def client(env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_bad_token_rejected(client):
    r = await client.get("/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = await client.get("/me", headers=hdr("u1"))
    assert r.status_code == 200 and r.json()["trust_tier"] == "medium"


async def test_reaction_idempotent_with_one_outbox_row(client):
    await make_post()
    for _ in range(2):
        assert (await client.post("/me/reactions/1", headers=hdr("u1"))).status_code == 200
    assert await count(Reaction) == 1
    assert await count(Outbox) == 1


async def test_self_reaction_and_frozen_refused(client):
    await client.get("/me", headers=hdr("author"))
    async with dbs.session_maker()() as s:
        author = await s.scalar(select(Account).where(Account.privy_user_id == "author"))
    await make_post(account_id=author.id)
    assert (await client.post("/me/reactions/1", headers=hdr("author"))).status_code == 400
    await client.get("/me", headers=hdr("bad"))
    async with dbs.session_maker()() as s, s.begin():
        (await s.scalar(select(Account).where(Account.privy_user_id == "bad"))).status = "frozen"
    assert (await client.post("/me/reactions/1", headers=hdr("bad"))).status_code == 403
    assert await count(Reaction) == 0


async def test_session_stores_only_hashes(client):
    body = {"device_id": "device-123456", "ua": "Mozilla Chrome/120"}
    await client.post("/me/session", json=body, headers=hdr("u1"))
    await client.post("/me/session", json=body, headers=hdr("u1"))
    async with dbs.session_maker()() as s:
        row = await s.scalar(select(DeviceSignal))
    assert row.hits == 2 and "device-123456" not in row.device_hash and len(row.device_hash) == 64
    assert await count(Outbox) == 2  # USED_DEVICE + SEEN_ON_NET, only on first sight


def test_ip_bucket():
    assert ip_bucket("1.2.3.4") == ip_bucket("1.2.3.99") != ip_bucket("1.2.4.4")
    assert ip_bucket("2001:db8:1::1") == ip_bucket("2001:db8:1:ffff::2")


META = {"block_number": 5, "tx_hash": "0xaa", "log_index": 0, "timestamp": datetime.now(UTC)}


async def test_events_are_idempotent_and_feed_works(client):
    big = 10**30 + 7
    for _ in range(2):  # replaying the same range must not duplicate
        async with dbs.session_maker()() as s, s.begin():
            await apply_event(s, "PostCreated", {"postId": 1, "author": "0xABC", "metadataCid": "bafy"}, META)
            await apply_event(s, "Tipped", {"postId": 1, "from": "0xF", "to": "0xABC", "amount": big}, META)
    assert await count(Post) == 1 and await count(Tip) == 1
    async with dbs.session_maker()() as s:
        assert (await s.scalar(select(Tip))).amount_wei == big  # no precision loss
    feed = (await client.get("/feed")).json()
    assert feed["items"][0]["tip_total_wei"] == str(big)
    async with dbs.session_maker()() as s, s.begin():
        await apply_event(s, "PostHidden", {"postId": 1, "hidden": True}, META)
    assert (await client.get("/feed")).json()["items"] == []


async def test_outbox_retry_and_success(env):
    calls = {"n": 0}

    @outbox.register("t.flaky")
    async def flaky(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    async with dbs.session_maker()() as s, s.begin():
        await outbox.enqueue(s, "t.flaky", {})
    await outbox.process_once()
    async with dbs.session_maker()() as s, s.begin():
        row = await s.scalar(select(Outbox))
        assert row.attempts == 1 and row.done_at is None and row.last_error == "boom"
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await outbox.process_once()
    async with dbs.session_maker()() as s:
        assert (await s.scalar(select(Outbox))).done_at is not None


async def test_wallet_link_requires_valid_signature(client):
    from eth_account import Account as Eth
    from eth_account.messages import encode_defunct

    from app.routers.wallet import link_message

    wallet = Eth.create()
    await make_post(author=wallet.address.lower())  # indexed before the wallet was linked
    n = (await client.get("/me/wallet/nonce", headers=hdr("u1"))).json()["nonce"]
    sig = wallet.sign_message(encode_defunct(text=link_message(n))).signature.hex()
    bad = await client.post("/me/wallet", json={"nonce": n, "signature": "0x" + "00" * 65}, headers=hdr("u1"))
    assert bad.status_code == 400
    ok = await client.post("/me/wallet", json={"nonce": n, "signature": sig}, headers=hdr("u1"))
    assert ok.status_code == 200 and ok.json()["wallet"] == wallet.address.lower()
    async with dbs.session_maker()() as s:
        assert (await s.get(Post, 1)).account_id is not None
    # another account cannot claim the same wallet, and a nonce is bound to its account
    n2 = (await client.get("/me/wallet/nonce", headers=hdr("u2"))).json()["nonce"]
    sig2 = wallet.sign_message(encode_defunct(text=link_message(n2))).signature.hex()
    assert (await client.post("/me/wallet", json={"nonce": n2, "signature": sig2}, headers=hdr("u2"))).status_code == 409
    assert (await client.post("/me/wallet", json={"nonce": n, "signature": sig}, headers=hdr("u2"))).status_code == 400


async def test_rate_limit_sets_risk_flag(client, monkeypatch):
    from app import ratelimit

    monkeypatch.setitem(ratelimit.LIMITS, "reactions", 2)
    for i in range(1, 5):
        await make_post(post_id=i)
    codes = [(await client.post(f"/me/reactions/{i}", headers=hdr("u1"))).status_code for i in range(1, 5)]
    assert codes == [200, 200, 429, 429]
    async with dbs.session_maker()() as s:
        assert (await s.scalar(select(Account))).risk_flags["rate_limited"] == 2


def test_jwt_edge_cases():
    import jwt
    from fastapi import HTTPException

    from app.auth import DEV_ISSUER, verify_token

    def tok(**over):
        claims = {"sub": "u", "iss": DEV_ISSUER, "aud": "creatorloop", **over}
        return jwt.encode(claims, SECRET, algorithm="HS256")

    import os
    os.environ["DEV_AUTH_SECRET"] = SECRET
    config.get_settings.cache_clear()
    try:
        assert verify_token(tok())["sub"] == "u"
        for bad in (tok(exp=1), tok(aud="other"), tok(iss="x"), tok() + "x"):
            with pytest.raises(HTTPException) as e:
                verify_token(bad)
            assert e.value.status_code == 401
    finally:
        del os.environ["DEV_AUTH_SECRET"]
        config.get_settings.cache_clear()


async def test_funder_parsing_and_retention(env):
    from app.chain.funders import first_incoming
    from app.retention import purge_old_signals

    txs = [{"to": "0xother", "value": "5"}, {"to": "0xw", "value": "0"}, {"to": "0xW", "value": "9", "hash": "h"}]
    assert first_incoming(txs, "0xw")["hash"] == "h"
    async with dbs.session_maker()() as s, s.begin():
        a = Account(privy_user_id="x")
        s.add(a)
        await s.flush()
        old = datetime.now(UTC) - timedelta(days=100)
        s.add(DeviceSignal(account_id=a.id, device_hash="d", ip_bucket_hash="i", last_seen=old))
    assert await purge_old_signals() == 1 and await count(DeviceSignal) == 0
