import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import config, outbox
from app.automations import events, jobs
from app.db import session as dbs
from app.db.models import Account, Base, Creator, LedgerEntry, Outbox, Payment, utcnow
from app.main import app
from app.payments import ledger

TOKEN = "internal-token-123"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)
    monkeypatch.setenv("VIASOCKET_SIGNING_SECRET", "sign-secret")
    monkeypatch.setenv("VIASOCKET_WEBHOOK_PAYMENT", "https://flows.test/payment")
    monkeypatch.setenv("VIASOCKET_WEBHOOK_FRAUD", "https://flows.test/fraud")
    monkeypatch.setenv("VIASOCKET_WEBHOOK_LEDGER", "https://flows.test/ledger")
    monkeypatch.setenv("VIASOCKET_WEBHOOK_DIGEST", "https://flows.test/digest")
    monkeypatch.setenv("VIASOCKET_SINK_PATH", str(tmp_path / "sink.jsonl"))
    monkeypatch.setenv("PRIVY_APP_ID", "")
    config.get_settings.cache_clear()
    engine = dbs.init_engine()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    yield tmp_path
    await engine.dispose()
    config.get_settings.cache_clear()


def test_signature_matches_reference_implementation():
    event, body = events.build({"event": "payment.succeeded", "payment_id": "p1", "amount_minor": 14900})
    ts = "1700000000"
    ref = hmac.new(b"sign-secret", f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    assert events.sign_body("sign-secret", ts, body) == ref
    # the id is stable across retries and differs for different content
    assert json.loads(events.build({"event": event, "payment_id": "p1", "amount_minor": 14900})[1])["id"] == \
        json.loads(body)["id"]
    assert json.loads(events.build({"event": event, "payment_id": "p2"})[1])["id"] != json.loads(body)["id"]


def test_every_event_has_a_flow_url_field():
    st = config.Settings(_env_file=None)
    for event, field in events.FLOW_FIELDS.items():
        assert hasattr(st, field), event


@respx.mock
async def test_emit_posts_signed_event_and_retries_on_failure(env):
    route = respx.post("https://flows.test/payment").mock(return_value=httpx.Response(200))
    await events.emit({"event": "payment.succeeded", "payment_id": "p1", "amount_minor": 14900})
    req = route.calls.last.request
    body = req.content.decode()
    assert req.headers["x-cl-event"] == "payment.succeeded"
    assert req.headers["x-cl-signature"] == events.sign_body("sign-secret", req.headers["x-cl-timestamp"], body)
    respx.post("https://flows.test/fraud").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await events.emit({"event": "ops.alert", "title": "x"})
    with pytest.raises(ValueError):
        await events.emit({"event": "made.up"})


async def test_unconfigured_drops_and_sink_records(env, monkeypatch):
    monkeypatch.delenv("VIASOCKET_SIGNING_SECRET")
    config.get_settings.cache_clear()
    await events.emit({"event": "ops.alert", "title": "dropped"})  # no secret, no sink: no error, no retry loop
    monkeypatch.setenv("VIASOCKET_LOCAL_SINK", "true")
    config.get_settings.cache_clear()
    await events.emit({"event": "ops.alert", "title": "kept"})
    lines = (env / "sink.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["body"]["data"]["title"] == "kept"


async def test_emitted_payloads_contain_no_pii_keys(env):
    async with dbs.session_maker()() as s, s.begin():
        ledger.record_supporter_payment(s, payment_id="p", creator_id=uuid.uuid4(), amount_minor=14900, currency="INR")
    async with dbs.session_maker()() as s:
        rows = (await s.scalars(select(Outbox).where(Outbox.topic == "viasocket.emit"))).all()
    assert len(rows) == 3  # one ledger.entry per line
    for r in rows:
        assert r.payload["event"] == "ledger.entry"
        assert not (set(r.payload) & events.PII_KEYS)
        assert "creator_fund:" not in json.dumps(r.payload)  # the creator id never leaves the API


async def test_dead_letter_raises_ops_alert(env, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 1)

    @outbox.register("t.broken")
    async def broken(payload):
        raise RuntimeError("nope")

    async with dbs.session_maker()() as s, s.begin():
        await outbox.enqueue(s, "t.broken", {})
    await outbox.process_once()
    async with dbs.session_maker()() as s:
        alerts = [o.payload for o in (await s.scalars(select(Outbox).where(Outbox.topic == "viasocket.emit"))).all()]
    assert len(alerts) == 1 and alerts[0]["event"] == "ops.alert" and "t.broken" in alerts[0]["title"]


@pytest.fixture
async def client(env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_internal_endpoints_require_token(client):
    for path in ("/internal/trust/run", "/internal/digests/creators", "/internal/maintenance/retention",
                 "/internal/ledger/check", "/internal/epochs/run"):
        assert (await client.post(path)).status_code == 422  # header missing
        assert (await client.post(path, headers={"X-Internal-Token": "wrong"})).status_code == 401
    ok = await client.post("/internal/maintenance/retention", headers={"X-Internal-Token": TOKEN})
    assert ok.json() == {"purged_device_signals": 0}
    epoch = await client.post("/internal/epochs/run", headers={"X-Internal-Token": TOKEN})
    assert epoch.status_code == 200 and epoch.json()["status"] == "empty"  # nothing to reward in an empty database
    again = await client.post("/internal/epochs/run", headers={"X-Internal-Token": TOKEN})
    assert again.json()["epoch"] == epoch.json()["epoch"]  # same window -> same epoch


async def test_internal_503_when_token_unset(client, monkeypatch):
    monkeypatch.delenv("INTERNAL_API_TOKEN")
    config.get_settings.cache_clear()
    assert (await client.post("/internal/ledger/check", headers={"X-Internal-Token": ""})).status_code == 503


async def test_trust_run_is_throttled(client):
    h = {"X-Internal-Token": TOKEN}
    async with dbs.session_maker()() as s, s.begin():
        s.add(Account(privy_user_id="a", trust_updated_at=utcnow()))
    assert (await client.post("/internal/trust/run", headers=h)).json()["skipped"] is True


async def test_ledger_check_alerts_when_broken(client):
    h = {"X-Internal-Token": TOKEN}
    assert (await client.post("/internal/ledger/check", headers=h)).json() == {"ok": True, "unbalanced": 0}
    async with dbs.session_maker()() as s, s.begin():
        s.add(LedgerEntry(txn_id=uuid.uuid4(), account="x", amount_minor=5, currency="INR", ref="r"))
    assert (await client.post("/internal/ledger/check", headers=h)).json() == {"ok": False, "unbalanced": 1}
    async with dbs.session_maker()() as s:
        topics = [o.payload.get("event") for o in (await s.scalars(select(Outbox))).all()]
    assert "ops.alert" in topics


async def test_digest_once_per_week_per_creator(env):
    cid = uuid.uuid4()
    async with dbs.session_maker()() as s, s.begin():
        s.add(Account(id=cid, privy_user_id="c"))
        s.add(Creator(account_id=cid, handle="alice", display_name="Alice"))
        s.add(Payment(dodo_payment_id="p1", creator_id=cid, kind="supporter_pass", status="succeeded",
                      amount_minor=14900, currency="INR", raw={}, created_at=datetime.now(UTC)))
    async with dbs.session_maker()() as s:
        first = await jobs.creator_digests(s)
    async with dbs.session_maker()() as s:
        second = await jobs.creator_digests(s)
        digests = [o.payload for o in (await s.scalars(select(Outbox))).all() if o.payload.get("event") == "digest.creator"]
    assert first["digests"] == 1 and second == {"week": first["week"], "digests": 0, "already_sent": 1}
    assert len(digests) == 1 and digests[0]["handle"] == "alice" and digests[0]["new_supporters"] == 1
    assert not (set(digests[0]) & events.PII_KEYS - {"name"})


def test_local_scheduler_registers_all_jobs():
    from app.automations import scheduler

    ids = {j.id for j in scheduler.build().get_jobs()}
    assert ids == {"trust_run", "epoch_run", "ledger_check", "retention", "digests"}
