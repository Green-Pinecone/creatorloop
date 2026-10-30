import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import config, ipfs
from app.auth import make_dev_token
from app.chain.events import apply_event
from app.db import session as dbs
from app.db.models import Account, Allocation, Base, Epoch, Outbox, Post, Reaction
from app.graph import sync
from app.main import app
from app.scoring import epoch as ep_mod
from app.scoring import merkle

SECRET = "test-secret-test-secret-test-secret"
NOW = datetime(2026, 11, 2, 10, 45, tzinfo=UTC)  # window = [10:00, 10:30)
IN_WINDOW = datetime(2026, 11, 2, 10, 10, tzinfo=UTC)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("DEV_AUTH_SECRET", SECRET)
    monkeypatch.setenv("IPFS_LOCAL_DIR", str(tmp_path / "ipfs"))
    monkeypatch.setenv("PUBLISHER_PRIVATE_KEY", "")  # no chain in unit tests
    monkeypatch.setenv("PRIVY_APP_ID", "")
    config.get_settings.cache_clear()
    engine = dbs.init_engine()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    sync.store.clear()
    yield
    await engine.dispose()
    config.get_settings.cache_clear()


def wallet(i: int) -> str:
    return "0x" + format(0xAB00 + i, "040x")


async def world():
    """Honest creator H, bot-ring creator B (fans are quarantined), frozen creator F, creator without wallet N."""
    ids = {k: uuid.uuid4() for k in ("H", "B", "F", "N")}
    async with dbs.session_maker()() as s, s.begin():
        for i, (k, aid) in enumerate(ids.items()):
            s.add(Account(id=aid, privy_user_id=k, wallet_address=None if k == "N" else wallet(i),
                          status="frozen" if k == "F" else "active", trust_score=0.8,
                          created_at=NOW - timedelta(days=60)))
        n = 0
        for k, aid in ids.items():
            n += 1
            s.add(Post(post_id=n, author_wallet=wallet(n), account_id=aid, metadata_cid="b", block_number=1,
                       tx_hash="0x", created_at=IN_WINDOW))
            fans = 6
            for f in range(fans):
                fan = uuid.uuid4()
                bot = k == "B"
                s.add(Account(id=fan, privy_user_id=f"fan-{k}-{f}", trust_score=0.05 if bot else 0.8,
                              created_at=NOW - timedelta(days=60)))
                await s.flush()
                s.add(Reaction(account_id=fan, post_id=n, created_at=IN_WINDOW))
    return ids


async def test_window_is_aligned_and_exact():
    start, end = ep_mod.window_for(NOW, 30)
    assert (start.minute, end.minute) == (0, 30) and end - start == timedelta(minutes=30)


async def test_epoch_scores_excludes_and_builds_valid_tree(env):
    ids = await world()
    async with dbs.session_maker()() as s:
        out = await ep_mod.run_epoch(s, NOW)
    assert out["status"] == "pinned"  # no publisher key configured, so it stops before the chain
    async with dbs.session_maker()() as s:
        ep = await s.scalar(select(Epoch))
        rows = (await s.scalars(select(Allocation))).all()
    paid = {str(r.account_id): r for r in rows}
    assert set(paid) == {str(ids["H"])}, "only the honest creator is rewarded"
    reasons = {e["account_id"]: e["reason"] for e in ep.excluded}
    assert reasons[str(ids["F"])] == "frozen" and reasons[str(ids["N"])] == "no_wallet"
    assert str(ids["B"]) not in paid and str(ids["B"]) not in reasons  # bot fans carry zero weight: no points at all
    assert ep.total_wei == sum(r.amount_wei for r in rows) <= ep.pool_wei
    # the stored proof really verifies against the stored root
    r = paid[str(ids["H"])]
    assert merkle.verify(bytes.fromhex(ep.root[2:]), r.wallet, r.amount_wei, [bytes.fromhex(p[2:]) for p in r.proof])
    # naive (unweighted) numbers are kept so the demo can show what trust weighting prevented
    assert int(ep.stats["naive_allocations"][str(ids["B"])]) > 0


async def test_rerun_is_idempotent_and_repin_gives_same_cid(env):
    await world()
    async with dbs.session_maker()() as s:
        first = await ep_mod.run_epoch(s, NOW)
    async with dbs.session_maker()() as s:
        second = await ep_mod.run_epoch(s, NOW + timedelta(minutes=5))  # same window
    assert first["epoch"] == second["epoch"] and first["allocations_cid"] == second["allocations_cid"]
    async with dbs.session_maker()() as s:
        assert len((await s.scalars(select(Epoch))).all()) == 1
        ep = await s.scalar(select(Epoch))
        blob = await ep_mod._allocations_json(s, ep)
    assert ipfs.cid_for(blob) == ep.allocations_cid and b"creatorloop.allocations.v1" in blob


async def test_resume_after_crash_between_build_and_pin(env):
    await world()
    async with dbs.session_maker()() as s:
        start, end = ep_mod.window_for(NOW, 30)
        ep = Epoch(window_start=start, window_end=end, status="scoring", pool_wei=10**22)
        s.add(ep)
        await s.flush()
        await ep_mod._build(s, ep)
        await s.commit()
        assert ep.status == "built" and ep.allocations_cid is None
    async with dbs.session_maker()() as s:  # a fresh process picks up from "built"
        out = await ep_mod.run_epoch(s, NOW)
    assert out["status"] == "pinned" and out["allocations_cid"]


async def test_deletions_after_the_window_cannot_change_inputs(env):
    ids = await world()
    async with dbs.session_maker()() as s:
        await ep_mod.run_epoch(s, NOW)
    async with dbs.session_maker()() as s, s.begin():
        for r in (await s.scalars(select(Reaction))).all():
            await s.delete(r)
    async with dbs.session_maker()() as s:
        again = await ep_mod.run_epoch(s, NOW)
        rows = (await s.scalars(select(Allocation))).all()
    assert again["creators"] == 1 and {str(r.account_id) for r in rows} == {str(ids["H"])}


async def test_empty_epoch_when_nobody_engaged(env):
    async with dbs.session_maker()() as s:
        out = await ep_mod.run_epoch(s, NOW)
    assert out["status"] == "empty" and out["total_loop"] == "0"
    async with dbs.session_maker()() as s:
        topics = [o.payload.get("event") for o in (await s.scalars(select(Outbox))).all()]
    assert "epoch.published" in topics


async def test_claimed_event_flips_allocation_and_earnings_api(env):
    ids = await world()
    async with dbs.session_maker()() as s:
        await ep_mod.run_epoch(s, NOW)
    async with dbs.session_maker()() as s, s.begin():  # pretend the publish tx landed
        ep = await s.scalar(select(Epoch))
        ep.status, ep.onchain_epoch_id = "published", 7
    h = {"Authorization": f"Bearer {make_dev_token('H', SECRET)}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = (await c.get("/me/earnings", headers=h)).json()
        assert r["loop"][0]["epochId"] == 7 and r["loop"][0]["claimed"] is False and r["loop"][0]["proof"] is not None
        assert r["frozen"] is False
        meta = {"block_number": 9, "tx_hash": "0xcc", "log_index": 0, "timestamp": NOW}
        async with dbs.session_maker()() as s, s.begin():
            await apply_event(s, "Claimed", {"epochId": 7, "account": wallet(0).upper().replace("0X", "0x"),
                                             "amount": 1}, meta)
        assert (await c.get("/me/earnings", headers=h)).json()["loop"][0]["claimed"] is True
    assert ids  # silence unused warning


async def test_restrict_counts_down_and_reinstates(env):
    async with dbs.session_maker()() as s, s.begin():
        s.add(Account(privy_user_id="r", status="restricted", risk_flags={"restricted_epochs": 2}))
    for n in range(2):
        async with dbs.session_maker()() as s:
            await ep_mod.run_epoch(s, NOW + timedelta(minutes=30 * n))
    async with dbs.session_maker()() as s:
        assert (await s.scalar(select(Account).where(Account.privy_user_id == "r"))).status == "active"
