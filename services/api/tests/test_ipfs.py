import hashlib
import struct
import zlib

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import config, ipfs
from app.auth import make_dev_token
from app.db import session as dbs
from app.db.models import Account, Base, Post
from app.main import app
from app.metadata import clean_caption, fetch_metadata, parse_metadata

SECRET = "test-secret-test-secret-test-secret"


def png(w=2, h=2) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("DEV_AUTH_SECRET", SECRET)
    monkeypatch.setenv("PRIVY_APP_ID", "")
    monkeypatch.setenv("DEV_IPFS", "true")
    monkeypatch.setenv("IPFS_LOCAL_DIR", str(tmp_path / "ipfs"))
    config.get_settings.cache_clear()
    engine = dbs.init_engine()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()
    config.get_settings.cache_clear()


def test_cid_is_deterministic_and_verifiable():
    cid = ipfs.cid_for(b"hello")
    assert cid.startswith("bafkrei") and ipfs.CID_RE.match(cid)
    assert ipfs.verify(cid, b"hello") and not ipfs.verify(cid, b"hellp")


def test_caption_cleaning_and_metadata_rules():
    assert clean_caption("  hi\x00\x07 there \n") == "hi there"
    good = {"schema": "creatorloop.post.v1", "kind": "image", "caption": "x", "tags": ["goa"],
            "createdAt": "2026-11-02T10:15:00Z", "author": "0x" + "ab" * 20,
            "media": {"cid": ipfs.cid_for(b"m"), "mime": "image/webp", "width": 10, "height": 10,
                      "bytes": 5, "sha256": "0" * 64}}
    import json

    assert parse_metadata(json.dumps(good).encode()).caption == "x"
    for patch in ({"tags": ["Bad Tag"]}, {"tags": ["a"] * 6}, {"schema": "v2"}, {"caption": "x" * 501}):
        with pytest.raises(ValueError):
            parse_metadata(json.dumps({**good, **patch}).encode())


async def test_upload_flow_and_indexer_handler(env):
    h = {"Authorization": f"Bearer {make_dev_token('creator', SECRET)}"}
    wallet = "0x" + "cd" * 20
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/dev/ipfs/media", content=png())).status_code in (401, 422)  # no auth header
        data = png(8, 8)
        up = await c.post("/dev/ipfs/media", content=data, headers=h)
        assert up.status_code == 200 and up.json()["mime"] == "image/png"
        assert (await c.post("/dev/ipfs/media", content=b"<script>alert(1)</script>", headers=h)).status_code == 415
        media = {"cid": up.json()["cid"], "mime": "image/png", "width": 8, "height": 8, "bytes": len(data),
                 "sha256": hashlib.sha256(data).hexdigest()}
        body = {"media": media, "caption": "<script>x</script> sunset", "tags": ["goa"]}
        assert (await c.post("/dev/ipfs/metadata", json=body, headers=h)).status_code == 403  # no wallet yet
        async with dbs.session_maker()() as s, s.begin():
            (await s.scalar(select(Account).where(Account.privy_user_id == "creator"))).wallet_address = wallet
        bad = await c.post("/dev/ipfs/metadata", json={**body, "media": {**media, "sha256": "0" * 64}}, headers=h)
        assert bad.status_code == 400
        meta_cid = (await c.post("/dev/ipfs/metadata", json=body, headers=h)).json()["metadataCid"]
        # the gateway serves the stored bytes and they hash to the CID
        served = await c.get(f"/dev/ipfs/{meta_cid}")
        assert ipfs.verify(meta_cid, served.content)

    async with dbs.session_maker()() as s, s.begin():
        from datetime import UTC, datetime
        s.add(Post(post_id=1, author_wallet=wallet, metadata_cid=meta_cid, block_number=1, tx_hash="0x1",
                   created_at=datetime.now(UTC)))
        s.add(Post(post_id=2, author_wallet="0x" + "11" * 20, metadata_cid=meta_cid, block_number=1,
                   tx_hash="0x2", created_at=datetime.now(UTC)))
    await fetch_metadata({"post_id": 1, "cid": meta_cid})
    await fetch_metadata({"post_id": 2, "cid": meta_cid})  # CID authored by a different wallet
    async with dbs.session_maker()() as s:
        ok, spoof = await s.get(Post, 1), await s.get(Post, 2)
    assert ok.caption == "<script>x</script> sunset" and ok.media_sha256 == media["sha256"]
    assert ok.moderation == "pending"
    assert spoof.caption is None and spoof.moderation == "flagged"


async def test_dev_routes_hidden_when_disabled(env, monkeypatch):
    monkeypatch.setenv("DEV_IPFS", "false")
    config.get_settings.cache_clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get(f"/dev/ipfs/{ipfs.cid_for(b'x')}")).status_code == 404
