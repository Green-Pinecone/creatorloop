# Stage 05: TigerGraph Trust Graph

**Goal:** TigerGraph becomes CreatorLoop's trust engine. It mirrors every account, device, network bucket, wallet, funder, post, reaction, tip and payment as a graph, then runs graph algorithms to produce:
1. a per-account **trust score** (0–1) with explainable components,
2. **linkage clusters** (accounts sharing devices, networks or funders),
3. **collusion communities** (rings that engage mostly with each other),
4. **fraud cases** for the AI investigator (Stage 09) and the admin.

The trust score drives reward weighting (Stage 11) and payment risk (Stage 07). This stage replaces identity-based sybil checks entirely.

> Context: Hacker House Goa 2026's shortlisting Task 4 was "agentic fraud investigation on TigerGraph". This stage is the production-shaped version of that idea, applied to a creator economy.

**Prerequisites:** Stage 04 (outbox emits `graph.upsert`). A TigerGraph Savanna workspace or Community Edition (Stage 01).
**Outputs:** the `CreatorLoop` graph schema, a loader (incremental + full rebuild), installed GSQL queries, `app/graph/trust.py` writing `accounts.trust_score`, a `fraud_cases` table, and admin endpoints.

---

## 1. Graph model

```
             OWNS_WALLET            FUNDED_BY (directed, wallet → funder)
   Account ─────────────── Wallet ───────────────► Wallet
     │  │ \
     │  │  \ USED_DEVICE        SEEN_ON_NET
     │  │   └──────── Device     Account ───── NetBucket
     │  │
     │  ├─ AUTHORED ─► Post ◄─ REACTED ─ Account
     │  ├─ FOLLOWS ──► Account(creator)
     │  ├─ PAID ─────► Account(creator)     (Dodo: amount, product, status)
     │  └─ PAID_WITH ─ PayerId              (hashed Dodo customer email / id)
     │
     ├─ LINKED ─── Account        (derived: share a non-supernode Device/NetBucket/funder/PayerId)
     └─ ENGAGED ─► Account        (derived: weighted fan → creator engagement)
   Wallet ─ TIPPED ─► Post        (on-chain tips)
   Case ─ INVOLVES ─ Account      (fraud cases)
```

### Schema (GSQL)

Run in GSQL (GraphStudio or `conn.gsql(...)`). Check syntax against your TigerGraph version; Savanna and 4.x use the same GSQL DDL, with schema changes done via a **schema change job**.

```gsql
CREATE VERTEX Account (PRIMARY_ID id STRING, status STRING, created_at DATETIME,
                       is_creator BOOL, seed_trusted BOOL, trust_score FLOAT)
       WITH primary_id_as_attribute="true"
CREATE VERTEX Wallet    (PRIMARY_ID id STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX Device    (PRIMARY_ID id STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX NetBucket (PRIMARY_ID id STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX PayerId   (PRIMARY_ID id STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX Post      (PRIMARY_ID id STRING, created_at DATETIME, hidden BOOL)
       WITH primary_id_as_attribute="true"
CREATE VERTEX Case      (PRIMARY_ID id STRING, kind STRING, status STRING, opened_at DATETIME)
       WITH primary_id_as_attribute="true"

CREATE UNDIRECTED EDGE OWNS_WALLET (FROM Account, TO Wallet)
CREATE UNDIRECTED EDGE USED_DEVICE (FROM Account, TO Device, first_seen DATETIME, hits INT)
CREATE UNDIRECTED EDGE SEEN_ON_NET (FROM Account, TO NetBucket, first_seen DATETIME, hits INT)
CREATE UNDIRECTED EDGE PAID_WITH   (FROM Account, TO PayerId)
CREATE DIRECTED EDGE FUNDED_BY (FROM Wallet, TO Wallet, amount_wei STRING, ts DATETIME)
       WITH REVERSE_EDGE="reverse_FUNDED_BY"
CREATE DIRECTED EDGE AUTHORED  (FROM Account, TO Post) WITH REVERSE_EDGE="reverse_AUTHORED"
CREATE DIRECTED EDGE REACTED   (FROM Account, TO Post, ts DATETIME) WITH REVERSE_EDGE="reverse_REACTED"
CREATE DIRECTED EDGE FOLLOWS   (FROM Account, TO Account, ts DATETIME) WITH REVERSE_EDGE="reverse_FOLLOWS"
CREATE DIRECTED EDGE TIPPED    (FROM Wallet, TO Post, amount_pol FLOAT, ts DATETIME) WITH REVERSE_EDGE="reverse_TIPPED"
CREATE DIRECTED EDGE PAID      (FROM Account, TO Account, amount_minor INT, product STRING,
                                status STRING, ts DATETIME) WITH REVERSE_EDGE="reverse_PAID"
CREATE UNDIRECTED EDGE LINKED  (FROM Account, TO Account, reasons SET<STRING>, strength INT)
CREATE DIRECTED EDGE ENGAGED   (FROM Account, TO Account, weight FLOAT) WITH REVERSE_EDGE="reverse_ENGAGED"
CREATE UNDIRECTED EDGE INVOLVES (FROM Case, TO Account, role STRING)

CREATE GRAPH CreatorLoop (*)
```

Notes:
- Vertex ids are the Postgres `accounts.id` UUID, the lowercase wallet address, salted hashes for Device/NetBucket/PayerId, and the on-chain post id as a string.
- `LINKED` and `ENGAGED` are **derived** edges, rebuilt by queries each trust run. Algorithms run on these compact projections, which is faster and easier to explain than running on the raw graph.
- `seed_trusted` marks the TrustRank seeds: team accounts, early beta users vouched for manually, and paying customers with 30+ days and no disputes (Stage 07).

## 2. Keeping the graph in sync

### Incremental (outbox → TigerGraph)

`app/graph/client.py`:

```python
from pyTigerGraph import TigerGraphConnection

def connect() -> TigerGraphConnection:
    conn = TigerGraphConnection(host=settings.tg_host, graphname=settings.tg_graph,
                                username=settings.tg_username, password=settings.tg_password,
                                gsqlSecret=settings.tg_secret)
    conn.getToken(settings.tg_secret)   # REST++ token; refresh before expiry
    return conn
```

> pyTigerGraph's auth API has changed across versions (token vs. secret vs. JWT, and Savanna specifics). Check the current pyTigerGraph docs for Savanna. There's also an async connection class in recent releases; prefer it inside FastAPI.

`app/graph/sync.py` batches outbox rows into one upsert per tick:

```python
class GraphBatch:
    def __init__(self): self.v = defaultdict(list); self.e = defaultdict(list)
    def add_vertex(self, vtype, vid, **attrs): self.v[vtype].append((vid, attrs))
    def add_edge(self, stype, sid, etype, ttype, tid, **attrs): self.e[(stype, etype, ttype)].append((sid, tid, attrs))
    def flush(self, conn):
        for vtype, rows in self.v.items(): conn.upsertVertices(vtype, rows)
        for (s, e, t), rows in self.e.items(): conn.upsertEdges(s, e, t, rows)

def apply(batch: GraphBatch, ev: dict):
    match ev["kind"]:
        case "REACTED":
            batch.add_vertex("Account", ev["src"])
            batch.add_edge("Account", ev["src"], "REACTED", "Post", str(ev["dst"]), ts=ev["ts"])
        case "USED_DEVICE":
            batch.add_vertex("Device", ev["device"])
            batch.add_edge("Account", ev["account"], "USED_DEVICE", "Device", ev["device"],
                           first_seen=ev["ts"], hits=ev["hits"])
        # … one case per edge kind: SEEN_ON_NET, OWNS_WALLET, FUNDED_BY, AUTHORED, FOLLOWS, TIPPED, PAID, PAID_WITH
```

Upserts are idempotent, so retries are safe. Target freshness: an event reaches TigerGraph within 10 s of the DB commit.

### Full rebuild (`make graph-rebuild`)

`app/graph/rebuild.py` streams every table from Postgres in pages of 5,000 and upserts in order: vertices first, then edges. Use it after schema changes, after a TigerGraph reset, and in the demo reset script. For more than about 1M edges, switch to CSV export plus a GSQL **loading job**. At hackathon scale, batched REST upserts are fine.

## 3. Installed queries

Install the official algorithm library pieces you need from `tigergraph/gsql-graph-algorithms` (GraphStudio can also install them): **WCC** (`tg_wcc`), **Louvain** (`tg_louvain`), **personalized PageRank** (`tg_pagerank_pers`). Check the exact names and parameters in the library README for your version.

Then add these custom queries (`app/graph/gsql/*.gsql`, installed by `scripts/install_queries.py`).

### 3.1 `build_linkage` — who shares a device, network or funder?

```gsql
CREATE OR REPLACE QUERY build_linkage(INT max_shared_degree = 25) FOR GRAPH CreatorLoop {
  // Ignore "supernodes": a venue Wi-Fi /24 or a public faucet funder links everyone and means nothing.
  SetAccum<VERTEX<Account>> @members;
  SetAccum<STRING> @reason;

  Shared = {Device.*, NetBucket.*, PayerId.*};
  Shared = SELECT s FROM Shared:s -((USED_DEVICE|SEEN_ON_NET|PAID_WITH):e)- Account:a
           ACCUM s.@members += a
           POST-ACCUM s.@reason += s.type
           HAVING s.@members.size() >= 2 AND s.@members.size() <= max_shared_degree;

  // Funder linkage: Account – OWNS_WALLET – Wallet – FUNDED_BY → Wallet(funder)
  Funders = {Wallet.*};
  Funders = SELECT f FROM Account:a -(OWNS_WALLET)- Wallet:w -(FUNDED_BY>)- Wallet:f
            ACCUM f.@members += a
            POST-ACCUM f.@reason += "Funder"
            HAVING f.@members.size() >= 2 AND f.@members.size() <= max_shared_degree;

  All = Shared UNION Funders;
  All = SELECT s FROM All:s
        POST-ACCUM
          FOREACH a IN s.@members DO
            FOREACH b IN s.@members DO
              IF a != b THEN INSERT INTO LINKED VALUES (a, b, s.@reason, 1) END
            END
          END;
  PRINT All.size() AS shared_vertices;
}
```

> Before rebuilding, clear old `LINKED` edges (a small `delete_derived()` query, or `DELETE e FROM … :e` in GSQL), or the derived edges accumulate stale reasons. Exclude known-good supernodes explicitly with an allowlist vertex attribute: the Polygon faucet funder and your own demo funder.

### 3.2 `build_engagement` — weighted fan → creator edges

```gsql
CREATE OR REPLACE QUERY build_engagement(DATETIME since) FOR GRAPH CreatorLoop {
  MapAccum<VERTEX<Account>, SumAccum<FLOAT>> @out;

  Fans = {Account.*};
  // reactions: fan -REACTED-> post <-AUTHORED- creator
  X = SELECT c FROM Fans:f -(REACTED>:r)- Post:p -(reverse_AUTHORED>)- Account:c
      WHERE r.ts >= since AND f != c
      ACCUM f.@out += (c -> 1.0);
  // payments (Dodo): fan -PAID-> creator
  Y = SELECT c FROM Fans:f -(PAID>:pd)- Account:c
      WHERE pd.ts >= since AND pd.status == "succeeded"
      ACCUM f.@out += (c -> 5.0 * log(1 + pd.amount_minor / 100.0));
  // tips: fan -OWNS_WALLET- wallet -TIPPED-> post <-AUTHORED- creator
  Z = SELECT c FROM Fans:f -(OWNS_WALLET)- Wallet:w -(TIPPED>:t)- Post:p -(reverse_AUTHORED>)- Account:c
      WHERE t.ts >= since AND f != c
      ACCUM f.@out += (c -> 3.0 * log(1 + t.amount_pol * 100));

  Fans = SELECT f FROM Fans:f
         POST-ACCUM FOREACH (c, w) IN f.@out DO INSERT INTO ENGAGED VALUES (f, c, w) END;
}
```

### 3.3 `co_reaction_pairs` — bot-like identical behaviour

```gsql
CREATE OR REPLACE QUERY co_reaction_pairs(INT min_reactions = 10, FLOAT min_jaccard = 0.8) FOR GRAPH CreatorLoop {
  TYPEDEF TUPLE<STRING a, STRING b, INT common, FLOAT jaccard> Pair;
  SumAccum<INT> @deg;
  MapAccum<VERTEX<Account>, SumAccum<INT>> @common;
  HeapAccum<Pair>(500, jaccard DESC) @@pairs;

  A = {Account.*};
  A = SELECT a FROM A:a -(REACTED>)- Post:p ACCUM a.@deg += 1 HAVING a.@deg >= min_reactions;
  B = SELECT b FROM A:a -(REACTED>)- Post:p -(reverse_REACTED>)- Account:b
      WHERE a != b AND b.@deg >= min_reactions
      ACCUM a.@common += (b -> 1);
  A = SELECT a FROM A:a
      POST-ACCUM FOREACH (b, c) IN a.@common DO
        IF a.id < b.id AND c * 1.0 / (a.@deg + b.@deg - c) >= min_jaccard THEN
          @@pairs += Pair(a.id, b.id, c, c * 1.0 / (a.@deg + b.@deg - c))
        END
      END;
  PRINT @@pairs AS pairs;
}
```

### 3.4 `account_features` — one row per account for the trust model

Returns, for each account:
- `linkage_cluster_id` and `linkage_cluster_size` (from `tg_wcc` on `LINKED`, results stored in a vertex attribute or returned)
- `community_id` and `community_internal_ratio`: the share of the account's outgoing `ENGAGED` weight that stays inside its Louvain community
- `community_reciprocity`: the fraction of ENGAGED edges in the community that have a reverse edge
- `trustrank` (from `tg_pagerank_pers` seeded on `seed_trusted` accounts over `ENGAGED`)
- `account_age_days`, `paid_count`, `refund_or_dispute_count`

Implementation tip: have the algorithm queries write results to attributes (most library queries support `result_attribute`/`file_path`/print options), then let `account_features` read the attributes. One `runInstalledQuery("account_features")` call then gives Python everything.

### 3.5 Investigator tool queries (used by Stage 09 agents)

Small, parameterised, read-only queries an LLM can call safely:

| Query | Params | Returns |
|---|---|---|
| `cluster_members(cluster_id)` | string | Accounts, statuses, ages, trust |
| `shared_signals(account_a, account_b)` | ids | Shared devices, net buckets, funders, payers |
| `engagement_flows(account_ids, since)` | set, datetime | Who engages whom inside the set, with weights |
| `payment_trail(creator_id, since)` | id, datetime | Payers, amounts, shared PayerIds, refunds/disputes |
| `neighbourhood(account_id, hops ≤ 2)` | id, int | A small subgraph for the admin UI visualisation |

**Never** expose a free-form GSQL tool to the agent. Only these installed queries, with validated parameters.

## 4. Trust score

`app/graph/trust.py` runs the pipeline and composes an **explainable** score:

```python
async def run_trust_job(since_days: int = 30):
    conn = graph.connect()
    conn.runInstalledQuery("delete_derived")
    conn.runInstalledQuery("build_linkage", {"max_shared_degree": 25})
    conn.runInstalledQuery("build_engagement", {"since": iso(days_ago(since_days))})
    conn.runInstalledQuery("tg_wcc", {...})              # on LINKED → cluster attrs
    conn.runInstalledQuery("tg_louvain", {...})          # on ENGAGED → community attrs
    conn.runInstalledQuery("tg_pagerank_pers", {...})    # TrustRank from seed_trusted
    feats = conn.runInstalledQuery("account_features")[0]["rows"]
    pairs = conn.runInstalledQuery("co_reaction_pairs")[0]["pairs"]
    scores = compose(feats, pairs)
    await write_scores(scores)          # accounts.trust_score, risk_flags, + Account.trust_score in TG
    await open_cases(feats, pairs)      # §5
```

Composition (start simple and tune with the attack simulation in Stage 13):

```python
def compose(f, jaccard_hits) -> tuple[float, dict]:
    z = 0.0
    parts = {}
    parts["trustrank"]   = 1.6 * normalized_log(f.trustrank)                       # 0..1.6
    parts["age"]         = 0.4 * min(f.account_age_days / 30, 1)
    parts["paid_clean"]  = 0.4 * min(f.paid_count, 3) / 3 if f.refund_or_dispute_count == 0 else -0.8
    parts["linkage"]     = -0.25 * max(0, f.linkage_cluster_size - 2)              # shared devices/funders
    parts["ring"]        = -2.0 if (f.community_size >= 5 and f.community_internal_ratio > 0.8
                                    and f.community_reciprocity > 0.6) else 0.0
    parts["clone"]       = -1.2 if f.id in jaccard_hits else 0.0
    z = -0.6 + sum(parts.values())
    return 1 / (1 + math.exp(-3 * z)), parts
```

| Tier | Score | Effect |
|---|---|---|
| `trusted` | ≥ 0.70 | Full reward weight; eligible as a TrustRank seed after 30 days |
| `normal` | 0.40–0.70 | Full weight |
| `low` | 0.20–0.40 | Engagement counts at 25% weight; payouts held for 1 epoch |
| `quarantine` | < 0.20 | Engagement weight 0; a case opens if not already open |

**Explainability:** `risk_flags` stores `parts` plus human-readable reasons (for example `"shares a device with 6 accounts"`). Users see only their tier; admins and the investigator agent see everything.

Run cadence: every 15 min in the demo and hourly in "production", triggered by viaSocket cron (Stage 08) via `POST /internal/trust/run`. Also run it right before each epoch.

## 5. Fraud cases

```sql
CREATE TABLE fraud_cases (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind          text NOT NULL,          -- linkage_cluster | collusion_ring | clone_pairs | payment_abuse
  status        text NOT NULL DEFAULT 'open',  -- open → investigating → recommended → actioned | dismissed
  severity      int  NOT NULL,          -- 1..5
  fingerprint   text UNIQUE NOT NULL,   -- sorted member ids hash: prevents duplicate cases
  member_ids    uuid[] NOT NULL,
  evidence      jsonb NOT NULL,         -- features, shared signals, flows
  agent_report  jsonb,                  -- Stage 09
  decision      text,                   -- freeze | restrict | dismiss
  decided_by    uuid REFERENCES accounts(id),
  decided_at    timestamptz,
  opened_at     timestamptz NOT NULL DEFAULT now()
);
```

Opening rules:
- **linkage_cluster:** a WCC cluster of size ≥ 4 where at least 2 members engage the same creator.
- **collusion_ring:** a Louvain community of size ≥ 5 with internal ratio > 0.8 and reciprocity > 0.6.
- **clone_pairs:** 3 or more accounts connected by Jaccard ≥ 0.8 pairs.
- **payment_abuse:** one `PayerId` or `Device` behind payments from 3 or more accounts to the same creator, or a dispute rate above 20% (Stage 07).

On open:
1. Mirror the case as a `Case` vertex with `INVOLVES` edges.
2. Enqueue `agents.investigate` (Stage 09).
3. Enqueue a `viasocket.emit` `fraud.case_opened` event (Stage 08).

**Enforcement** (admin action from the console, `POST /admin/cases/{id}/decision`):
- `freeze`: `accounts.status = 'frozen'` for the members; enqueue `chain.set_frozen` (calls `RewardDistributor.setFrozen(addr, true, keccak(caseId))`); exclude them from scoring; hold their creator-fund balance.
- `restrict`: force tier `low` for 2 epochs.
- `dismiss`: mark members `reviewed_ok` so the same fingerprint doesn't re-open for 7 days.

Every decision is reversible and logged. Never auto-freeze without a human in the loop (the agent only recommends).

## 6. Admin endpoints

| Path | Purpose |
|---|---|
| `GET /admin/cases?status=` | Case list with severity, size, kind |
| `GET /admin/cases/{id}` | Evidence, agent report, `neighbourhood` subgraph JSON for visualisation |
| `POST /admin/cases/{id}/decision` | `{decision, note}` → enforcement |
| `POST /admin/trust/run` | Manual trust run |
| `GET /admin/accounts/{id}/trust` | Score history + parts |

Admin role: a static allowlist of Privy user ids in env for the hackathon.

## 7. Tests

- **Unit:** `compose()` with handcrafted feature rows: an honest power user scores ≥ 0.7, a ring member ≤ 0.2, and a venue-Wi-Fi-only shared user is unaffected (supernode cap).
- **Integration** (`pytest -m live`, against a scratch graph `CreatorLoopTest`):
  - Load the fixture world: 30 honest users, 1 ring of 8, 1 device-farm of 12, 1 shared-funder group of 6.
  - Run the job and assert: all three bad groups have cases; fewer than 2 honest users are below 0.4; supernode venue Wi-Fi with 30 users creates **no** LINKED edges.
- **Query tests:** each investigator tool query returns a stable JSON shape (snapshot tests).

## 8. Acceptance checklist

- [ ] Schema created; queries installed by script (reproducible).
- [ ] Incremental sync lag under 10 s; full rebuild works from an empty graph.
- [ ] The trust job completes in under 60 s at demo scale (~1k accounts, ~20k edges).
- [ ] The fixture world produces the expected cases and scores.
- [ ] GraphStudio screenshots of a detected ring are saved for the pitch.

## 9. Pitfalls

- **Supernodes** (venue Wi-Fi, mobile-carrier CGNAT, faucet funders) will link everyone, which is fatal at a hacker house where all judges share one network. The `max_shared_degree` cap plus an allowlist is mandatory.
- **Derived edge staleness:** always delete and rebuild `LINKED`/`ENGAGED` per run, or version them with a `run_id` attribute.
- **Cold start:** with few seeds, TrustRank is noisy. Seed the team plus first paying customers, and keep the ring/linkage penalties as the main signal early on.
- **False positives:** fully legitimate families share devices. That's why cases go to a human, and why `restrict` exists as a softer action than `freeze`.
- **Query install time:** installing GSQL queries can take minutes. Do it in setup, not on demo day.
