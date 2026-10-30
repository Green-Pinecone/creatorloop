import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import config
from app.auth import make_dev_token
from app.db import session as dbs
from app.db.models import Account, Base, FraudCase, Outbox
from app.graph.features import analyse, build_linkage
from app.graph.store import GraphStore
from app.graph.trust import compose, run_trust_job, tier
from app.main import app

SECRET = "test-secret-test-secret-test-secret"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("DEV_AUTH_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_PRIVY_USER_IDS", "admin1")
    monkeypatch.setenv("PRIVY_APP_ID", "")
    config.get_settings.cache_clear()
    engine = dbs.init_engine()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()
    config.get_settings.cache_clear()


class World:
    """Fixture world from the plan: 30 honest users, a ring of 8, a device farm of 12, a shared-funder group
    of 6, plus 3 trusted seed creators and a venue Wi-Fi shared by 30 honest users."""

    def __init__(self):
        self.g = GraphStore()
        self.ids: dict[str, list[str]] = {}
        self.rows: list[Account] = []
        self.rng = random.Random(1)
        self.post = 0

    def account(self, group: str, seed: bool = False) -> str:
        a = Account(id=uuid.uuid4(), privy_user_id=f"{group}{len(self.rows)}", status="active",
                    created_at=datetime.now(UTC) - timedelta(days=40),
                    risk_flags={"seed_trusted": True} if seed else {})
        self.rows.append(a)
        self.ids.setdefault(group, []).append(str(a.id))
        self.g.accounts.add(str(a.id))
        return str(a.id)

    def new_post(self, author: str) -> str:
        self.post += 1
        self.g.apply({"kind": "AUTHORED", "src": author, "dst": self.post})
        return str(self.post)

    def react(self, fan: str, post: str):
        self.g.apply({"kind": "REACTED", "src": fan, "dst": post})


def build_world() -> World:
    w = World()
    creators = [w.account("creator", seed=True) for _ in range(3)]
    cposts = {c: [w.new_post(c) for _ in range(6)] for c in creators}
    all_cposts = [p for ps in cposts.values() for p in ps]

    for i in range(30):  # honest: unique device, shared venue Wi-Fi, a few creators' posts
        u = w.account("honest")
        w.g.apply({"kind": "USED_DEVICE", "src": u, "dst": f"dev-h{i}"})
        w.g.apply({"kind": "SEEN_ON_NET", "src": u, "dst": "venue-wifi"})
        for p in w.rng.sample(all_cposts, 4):
            w.react(u, p)
    ring = [w.account("ring") for _ in range(8)]
    rposts = {r: w.new_post(r) for r in ring}
    for r in ring:
        for other in ring:
            if other != r:
                w.react(r, rposts[other])
    for _ in range(12):  # device farm hammering one creator
        u = w.account("farm")
        w.g.apply({"kind": "USED_DEVICE", "src": u, "dst": "dev-farm"})
        for p in cposts[creators[0]][:3]:
            w.react(u, p)
    for i in range(6):  # wallets funded by one address
        u = w.account("funded")
        w.g.apply({"kind": "OWNS_WALLET", "src": u, "dst": f"0xw{i}"})
        w.g.apply({"kind": "FUNDED_BY", "src": f"0xw{i}", "dst": "0xfunder"})
        for p in cposts[creators[1]][:3]:
            w.react(u, p)
    return w


async def test_fixture_world_scores_and_cases(env):
    w = build_world()
    async with dbs.session_maker()() as s:
        s.add_all(w.rows)
        await s.commit()
        result = await run_trust_job(s, w.g)
    assert result["seeds"] == 3

    async with dbs.session_maker()() as s:
        score = {str(a.id): a.trust_score for a in (await s.scalars(select(Account))).all()}
        cases = (await s.scalars(select(FraudCase))).all()
        assert (await s.scalars(select(Outbox).where(Outbox.topic == "agents.investigate"))).all()
    assert all(score[i] < 0.2 for i in w.ids["ring"]), [round(score[i], 2) for i in w.ids["ring"]]
    assert all(score[i] < 0.4 for i in w.ids["farm"])
    assert sum(score[i] < 0.4 for i in w.ids["honest"]) < 2
    assert all(score[i] >= 0.7 for i in w.ids["creator"])

    by_kind = {}
    for c in cases:
        by_kind.setdefault(c.kind, []).append(set(c.member_ids))
    assert set(w.ids["ring"]) in by_kind["collusion_ring"]
    linked = by_kind["linkage_cluster"]
    assert set(w.ids["farm"]) in linked and set(w.ids["funded"]) in linked
    honest = set(w.ids["honest"])
    assert not any(honest & m for ms in by_kind.values() for m in ms), "honest users must not be in any case"

    # re-running opens no duplicate cases (fingerprint)
    async with dbs.session_maker()() as s:
        again = await run_trust_job(s, w.g)
    assert again["cases_opened"] == 0


def test_venue_wifi_supernode_creates_no_links():
    w = build_world()
    links = build_linkage(w.g, max_shared_degree=25)
    honest = set(w.ids["honest"])
    assert not any(a in honest or b in honest for a, b in links)
    assert not any("NetBucket" in r for r in links.values())


def test_clone_pairs_detected():
    g = GraphStore()
    for i in range(4):
        g.apply({"kind": "AUTHORED", "src": f"c{i}", "dst": i})
    bots = ["b1", "b2", "b3"]
    for b in bots:
        for p in range(4):
            g.apply({"kind": "REACTED", "src": b, "dst": p})
    a = analyse(g, min_reactions=3)
    assert {"b1", "b2", "b3"} in a.clone_groups


def test_compose_and_tiers():
    from app.graph.features import Feat

    honest = compose(Feat(trustrank_norm=0.8), age_days=40)[0]
    ring = compose(Feat(community_size=8, community_internal_ratio=1, community_reciprocity=1), age_days=40)[0]
    assert honest >= 0.7 and ring <= 0.2
    assert [tier(x) for x in (0.9, 0.5, 0.3, 0.1)] == ["trusted", "normal", "low", "quarantine"]
    assert compose(Feat(trustrank_norm=0.8), age_days=40, paid_count=3, disputes=1)[0] < honest


async def test_admin_flow_freeze_and_dismiss(env):
    w = build_world()
    async with dbs.session_maker()() as s:
        s.add_all(w.rows + [Account(privy_user_id="admin1")])
        await s.commit()
    hdr = lambda u: {"Authorization": f"Bearer {make_dev_token(u, SECRET)}"}  # noqa: E731
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/admin/cases", headers=hdr("nobody"))).status_code == 403
        from app.graph import sync

        sync.store.clear()
        sync.store.__dict__.update(w.g.__dict__)
        assert (await c.post("/admin/trust/run", headers=hdr("admin1"))).json()["cases_opened"] >= 3
        cases = (await c.get("/admin/cases?status=open", headers=hdr("admin1"))).json()
        ring = next(x for x in cases if x["kind"] == "collusion_ring")
        r = await c.post(f"/admin/cases/{ring['id']}/decision", json={"decision": "freeze"}, headers=hdr("admin1"))
        assert r.json()["status"] == "actioned"
        detail = (await c.get(f"/admin/cases/{ring['id']}", headers=hdr("admin1"))).json()
        assert {a["status"] for a in detail["accounts"]} == {"frozen"}
        assert detail["accounts"][0]["risk_flags"]["reasons"]
        # reversible
        await c.post(f"/admin/cases/{ring['id']}/decision", json={"decision": "dismiss"}, headers=hdr("admin1"))
        detail = (await c.get(f"/admin/cases/{ring['id']}", headers=hdr("admin1"))).json()
        assert {a["status"] for a in detail["accounts"]} == {"active"}
        bad = await c.post(f"/admin/cases/{ring['id']}/decision", json={"decision": "nuke"}, headers=hdr("admin1"))
        assert bad.status_code == 422
