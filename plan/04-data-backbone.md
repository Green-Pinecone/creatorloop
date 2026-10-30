# Stage 04: Data Backbone (FastAPI + Postgres + Chain Indexer + Signals)

**Goal:** the single backend every other stage plugs into. It does five things:
- authenticates users via Privy JWTs,
- stores the source of truth in Postgres,
- indexes on-chain events,
- captures the device and network signals TigerGraph needs,
- fans changes out reliably to TigerGraph and viaSocket through a **transactional outbox**.

**Prerequisites:** Stage 01 (Postgres up), Stage 03 (ABIs + `deployments.json`).
**Outputs:** `services/api` running locally behind a tunnel; schema migrated; reactions and follows endpoints; indexer filling `posts` and `tips`; outbox worker skeleton.

---

## 1. Service anatomy

```
app/main.py            FastAPI app, lifespan starts background workers
app/config.py          pydantic-settings; validates env at boot
app/auth.py            Privy JWT verification → current_account dependency
app/db/models.py       SQLAlchemy 2.0 models
app/db/migrations/     Alembic
app/routers/public.py  feed, post, creator pages (read-only)
app/routers/me.py      reactions, follows, session signals, my earnings
app/routers/admin.py   cases, freeze/unfreeze, epochs (role-gated)        (05, 11)
app/routers/webhooks.py Dodo webhooks                                       (07)
app/routers/internal.py viaSocket-triggered jobs (token-gated)              (08)
app/chain/indexer.py   getLogs poller → posts, tips, claims
app/outbox.py          enqueue() + worker that dispatches topics
```

Run with `uv run uvicorn app.main:app --reload --port 8000`, and expose it with `ngrok http 8000` (or cloudflared) for Dodo and viaSocket.

## 2. Postgres schema (core tables)

Use Alembic migrations. Shown here as SQL for clarity; later stages add `payments`, `ledger_entries`, `fraud_cases`, `agent_runs`, `epochs` and `allocations`.

```sql
CREATE TYPE account_status AS ENUM ('active', 'restricted', 'frozen', 'banned');

CREATE TABLE accounts (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  privy_user_id   text UNIQUE NOT NULL,
  wallet_address  text UNIQUE,                 -- lowercase 0x…
  email_hash      text,                        -- HMAC(salt, lower(email)) if Privy gives an email
  status          account_status NOT NULL DEFAULT 'active',
  trust_score     real NOT NULL DEFAULT 0.5,   -- written by Stage 05
  trust_updated_at timestamptz,
  risk_flags      jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE creators (
  account_id    uuid PRIMARY KEY REFERENCES accounts(id),
  handle        text UNIQUE NOT NULL CHECK (handle ~ '^[a-z0-9_]{3,24}$'),
  display_name  text NOT NULL,
  bio           text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE posts (
  post_id        bigint PRIMARY KEY,           -- on-chain id
  author_wallet  text NOT NULL,
  account_id     uuid REFERENCES accounts(id), -- null until wallet is linked
  metadata_cid   text NOT NULL,
  media_sha256   text,                         -- filled after metadata fetch
  caption        text,
  moderation     text NOT NULL DEFAULT 'pending',  -- pending|approved|flagged|removed (Stage 09)
  hidden         boolean NOT NULL DEFAULT false,
  block_number   bigint NOT NULL,
  tx_hash        text NOT NULL,
  created_at     timestamptz NOT NULL
);
CREATE INDEX ON posts (created_at DESC) WHERE NOT hidden;

CREATE TABLE reactions (
  account_id uuid REFERENCES accounts(id),
  post_id    bigint REFERENCES posts(post_id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, post_id)
);

CREATE TABLE follows (
  follower_id uuid REFERENCES accounts(id),
  creator_id  uuid REFERENCES accounts(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (follower_id, creator_id),
  CHECK (follower_id <> creator_id)
);

CREATE TABLE tips (
  tx_hash      text,
  log_index    int,
  post_id      bigint REFERENCES posts(post_id),
  from_wallet  text NOT NULL,
  to_wallet    text NOT NULL,
  amount_wei   numeric(78,0) NOT NULL,
  block_number bigint NOT NULL,
  created_at   timestamptz NOT NULL,
  PRIMARY KEY (tx_hash, log_index)
);

CREATE TABLE device_signals (
  account_id     uuid REFERENCES accounts(id),
  device_hash    text NOT NULL,
  ip_bucket_hash text NOT NULL,
  ua_family      text,
  first_seen     timestamptz NOT NULL DEFAULT now(),
  last_seen      timestamptz NOT NULL DEFAULT now(),
  hits           int NOT NULL DEFAULT 1,
  PRIMARY KEY (account_id, device_hash, ip_bucket_hash)
);

CREATE TABLE wallet_funders (
  wallet      text PRIMARY KEY,
  funder      text NOT NULL,
  tx_hash     text NOT NULL,
  amount_wei  numeric(78,0) NOT NULL,
  funded_at   timestamptz NOT NULL
);

CREATE TABLE chain_cursor (name text PRIMARY KEY, last_block bigint NOT NULL);

CREATE TABLE webhook_events (               -- idempotency for all inbound webhooks
  provider    text,
  event_id    text,
  received_at timestamptz NOT NULL DEFAULT now(),
  payload     jsonb NOT NULL,
  processed_at timestamptz,
  PRIMARY KEY (provider, event_id)
);

CREATE TABLE outbox (
  id           bigserial PRIMARY KEY,
  topic        text NOT NULL,      -- graph.upsert | viasocket.emit | moderation.check | ...
  payload      jsonb NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  available_at timestamptz NOT NULL DEFAULT now(),
  attempts     int NOT NULL DEFAULT 0,
  last_error   text,
  done_at      timestamptz
);
CREATE INDEX ON outbox (available_at) WHERE done_at IS NULL;
```

## 3. Authentication (Privy JWT)

The web app sends `Authorization: Bearer <privy access token>`. The API verifies it locally with Privy's published verification key (ES256), with no network call per request.

```python
# app/auth.py
import jwt
from fastapi import Depends, Header, HTTPException
from .config import settings
from .db.session import get_session
from .db.models import Account

async def current_account(authorization: str = Header(...), s = Depends(get_session)) -> Account:
    token = authorization.removeprefix("Bearer ").strip()
    try:
        claims = jwt.decode(token, settings.privy_verification_key, algorithms=["ES256"],
                            audience=settings.privy_app_id, issuer="privy.io")
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid token")
    acct = await Account.get_or_create_by_privy(s, claims["sub"])
    if acct.status in ("frozen", "banned"):
        # frozen accounts can still read and log in; writes are refused in the routers
        pass
    return acct
```

> Check Privy's docs for the current issuer/audience values and how to fetch the verification key. Link the wallet with `POST /me/wallet`, which verifies a signed message (`personal_sign` of a nonce) so nobody can claim someone else's address.

## 4. Engagement endpoints (off-chain)

| Method | Path | Body | Rules |
|---|---|---|---|
| `POST` | `/me/reactions/{post_id}` | — | Account active; not own post; post not hidden; idempotent (PK) |
| `DELETE` | `/me/reactions/{post_id}` | — | Allowed within 24 h (prevents score gaming at epoch boundaries) |
| `POST` | `/me/follows/{creator_id}` | — | Not self; idempotent |
| `POST` | `/me/session` | `{device_id, tz, screen, ua}` | Called once per app load; upserts `device_signals` |
| `GET` | `/me` | — | Profile, trust tier (coarse, not the raw score), status |

**Every write does two things in one DB transaction:** the domain insert **and** `outbox.enqueue("graph.upsert", {...})`. This is the transactional-outbox pattern. TigerGraph can never miss an event because a network call failed after commit.

```python
async def react(post_id: int, acct = Depends(current_account), s = Depends(get_session)):
    ensure_active(acct)
    post = await s.get(Post, post_id) or abort(404)
    if post.account_id == acct.id: abort(400, "self reaction")
    async with s.begin():
        inserted = await insert_reaction_if_absent(s, acct.id, post_id)
        if inserted:
            await enqueue(s, "graph.upsert", {"kind": "REACTED", "src": str(acct.id), "dst": post_id,
                                              "ts": now_iso()})
    return {"ok": True}
```

Rate limits (per account, Postgres- or Redis-backed): 300 reactions/hour, 100 follows/hour. Exceeding them is itself a risk signal; record it in `risk_flags.rate_limited`.

## 5. Signals capture (privacy-preserving)

Client (Stage 10) on app load:
- `device_id`: a random UUID stored in `localStorage` (regenerated if cleared, which is itself a mild signal).
- `ua`, `tz`, `screen`: coarse values only.

Server:

```python
def hmac_hex(value: str) -> str:
    return hmac.new(settings.signal_hash_salt.encode(), value.encode(), "sha256").hexdigest()

def ip_bucket(ip: str) -> str:
    addr = ipaddress.ip_address(ip)
    net = ipaddress.ip_network(f"{ip}/{24 if addr.version == 4 else 48}", strict=False)
    return hmac_hex(str(net))
```

- Only **hashes** are stored. Raw IPs and device ids are never persisted.
- Retention: 90 days, then delete (a scheduled job).
- Take the client IP from the tunnel or proxy header (`X-Forwarded-For`) **only** when the request comes through your known proxy.
- Disclose it in the privacy notice: "we use hashed device and network signals to prevent fraud".

Each new `(account, device_hash, ip_bucket_hash)` enqueues `graph.upsert` edges `USED_DEVICE` and `SEEN_ON_NET`.

## 6. Chain indexer

`app/chain/indexer.py` is a background task polling `eth_getLogs`:

```python
CONFIRMATIONS = 5          # Amoy reorgs are rare but real
BATCH = 2_000              # blocks per getLogs call (respect provider limits)

async def run_indexer():
    while True:
        head = await w3.eth.block_number
        safe = head - CONFIRMATIONS
        cur = await get_cursor("main") or settings.chain_start_block - 1
        if cur >= safe:
            await asyncio.sleep(4); continue
        to = min(cur + BATCH, safe)
        logs = await w3.eth.get_logs({"fromBlock": cur + 1, "toBlock": to,
                                      "address": [POSTS, TIPPING, DISTRIBUTOR]})
        async with session.begin():
            for log in sorted(logs, key=lambda l: (l["blockNumber"], l["logIndex"])):
                await handle_log(session, log)          # decode by topic0 → upsert rows + enqueue outbox
            await set_cursor(session, "main", to)
```

| Event | Effect |
|---|---|
| `PostCreated` | Insert `posts`; link `account_id` by `wallet_address`; enqueue `ipfs.fetch_metadata`, `moderation.check` (Stage 09) and `graph.upsert` (`Post`, `AUTHORED`) |
| `PostHidden` | Update `posts.hidden` |
| `Tipped` | Insert `tips` (PK makes it idempotent); enqueue `graph.upsert` (`TIPPED`); if it's the first time we see `from_wallet`, enqueue `chain.resolve_funder` |
| `EpochPublished` / `Claimed` / `FrozenSet` | Update `epochs`/`allocations` (Stage 11) and account status |

**Funder resolution** (`chain.resolve_funder`): look up the wallet's first incoming native POL transfer via the Etherscan v2 API (`account/txlist`, sorted ascending, first tx with `to == wallet`). Store it in `wallet_funders` and enqueue a `FUNDED_BY` edge. Many wallets funded by one address is the strongest cheap sybil signal on-chain. Respect explorer rate limits (queue with backoff).

**Re-index:** `TRUNCATE` chain-derived tables, reset the cursor to `CHAIN_START_BLOCK`, restart. Everything is idempotent by primary key.

## 7. Outbox worker

```python
HANDLERS = {
  "graph.upsert":       graph_sync.handle,        # Stage 05
  "viasocket.emit":     automations.emit,         # Stage 08
  "moderation.check":   agents.moderate_post,     # Stage 09
  "ipfs.fetch_metadata": ipfs.fetch_metadata,
  "chain.resolve_funder": chain.resolve_funder,
}

async def outbox_worker():
    while True:
        rows = await claim_batch(limit=100)   # SELECT … FOR UPDATE SKIP LOCKED WHERE done_at IS NULL AND available_at<=now()
        for r in rows:
            try:
                await HANDLERS[r.topic](r.payload)
                await mark_done(r.id)
            except Exception as e:
                await retry_later(r.id, error=str(e), backoff=min(2 ** r.attempts, 300))
        await asyncio.sleep(0.5 if rows else 2)
```

- **Batching for TigerGraph:** `graph.upsert` handlers accumulate into one REST++ upsert per tick (Stage 05).
- **Dead letters:** after 10 attempts, stop retrying and enqueue a `viasocket.emit` ops alert.

## 8. Read endpoints (for the frontend)

| Path | Returns |
|---|---|
| `GET /feed?cursor=` | Posts (not hidden, moderation ≠ removed) with author, reaction count, tip totals, and whether the caller reacted. Keyset pagination on `(created_at, post_id)`. |
| `GET /posts/{id}` | Post detail + recent tips |
| `GET /creators/{handle}` | Profile, posts, supporter count, LOOP earned |
| `GET /me/earnings` | Claimable epochs + proofs (Stage 11), creator-fund balance (Stage 07) |

Cache metadata JSON (from IPFS) in `posts.caption` and `media_sha256` so the feed needs no gateway round-trips for text.

## 9. Tests

- [ ] JWT verification: valid, expired, wrong audience, tampered.
- [ ] Reaction: self-reaction refused; idempotent; frozen account refused; an outbox row is written in the same transaction (roll back mid-way and confirm neither row exists).
- [ ] Indexer: decode fixtures for each event; re-processing the same block range creates no duplicates; the cursor advances only on commit.
- [ ] `ip_bucket` collapses `1.2.3.4` and `1.2.3.99` to one hash; IPv6 /48 works.
- [ ] Outbox: a failing handler retries with backoff; a success marks it done; `SKIP LOCKED` allows two workers.

## 10. Acceptance checklist

- [ ] Migrations apply from scratch.
- [ ] Creating a post on Amoy (via script or UI) shows up in `GET /feed` within about 20 s.
- [ ] Tips appear with correct amounts.
- [ ] A reaction via the API creates exactly one `reactions` row and one outbox row.
- [ ] Session signals produce `device_signals` rows with hashed values only.
- [ ] The funder for a new tipping wallet resolves within a minute.

## 11. Pitfalls

- **Large `getLogs` ranges** get rejected by providers. Keep `BATCH` ≤ 2k blocks and shrink it on error.
- **`numeric(78,0)` ↔ Python `int`:** use `Decimal` or `int` carefully; never `float`.
- **Clock skew:** use block timestamps for on-chain events and DB `now()` for off-chain ones, and never mix them in one ordering without care.
- **Wallet linking:** until `wallet_address` is linked, posts can't be joined to an account. Prompt users to link right after login (Privy embedded wallets make this automatic).
