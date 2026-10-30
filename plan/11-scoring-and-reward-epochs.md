# Stage 11: Trust-Weighted Scoring and Reward Epochs

**Goal:** at the end of each epoch:
1. compute every creator's share of the LOOP pool from engagement weighted by the **trust of whoever engaged**,
2. exclude frozen or quarantined accounts,
3. build an OpenZeppelin-compatible Merkle tree in Python,
4. pin the allocations to IPFS,
5. publish the root on-chain,
6. serve claim proofs.

This is where TigerGraph (trust), Dodo (paid support) and the chain (claims) meet.

**Prerequisites:** Stage 03 (`RewardDistributor` + publisher key), Stage 04 (data), Stage 05 (trust scores), Stage 07 (payments). Triggered by viaSocket F3 (Stage 08).
**Outputs:** `app/scoring/{score.py, merkle.py, epoch.py}`, `epochs`/`allocations` tables, `POST /internal/epochs/run`, the `GET /me/earnings` proofs, and a Python↔Solidity Merkle parity fixture.

---

## 1. Epoch lifecycle

```
window [start, end)          (demo: 30 min; prod: 7 days)
  1. freeze inputs   → run trust job (Stage 05) first, snapshot trust_score per account
  2. score           → per-creator points (formula §3)
  3. allocate        → pool × share, caps, dust handling
  4. build tree      → leaves (wallet, amount) for creators with linked wallet & amount > 0
  5. pin             → allocations JSON to IPFS (Pinata) → allocationsCid
  6. publish         → RewardDistributor.publishEpoch(root, total, allocationsCid)
  7. serve           → proofs via GET /me/earnings
  8. announce        → viasocket.emit ops summary + creator digest inputs
```

Each step persists its output, so a failure resumes from the last completed step. The idempotency key is the window.

```sql
CREATE TABLE epochs (
  id            bigint PRIMARY KEY,            -- local sequence; matches on-chain epochId after publish
  window_start  timestamptz NOT NULL,
  window_end    timestamptz NOT NULL UNIQUE,
  status        text NOT NULL,                 -- scoring|built|pinned|published|failed
  pool_wei      numeric(78,0) NOT NULL,
  total_wei     numeric(78,0),
  root          text,
  allocations_cid text,
  tx_hash       text,
  onchain_epoch_id bigint,
  excluded      jsonb NOT NULL DEFAULT '[]',
  stats         jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE allocations (
  epoch_id   bigint REFERENCES epochs(id),
  account_id uuid REFERENCES accounts(id),
  wallet     text NOT NULL,
  amount_wei numeric(78,0) NOT NULL,
  points     double precision NOT NULL,
  proof      jsonb NOT NULL,
  claimed    boolean NOT NULL DEFAULT false,
  PRIMARY KEY (epoch_id, account_id)
);
```

## 2. Inputs per creator (from Postgres, within the window)

| Signal | Source | Who counts |
|---|---|---|
| Reactions on the creator's posts | `reactions` | Reactor active; not the author; post not hidden or removed |
| Tips | `tips` | Tipper wallet linked to an account (unlinked wallets count at weight 0.25 × default trust) |
| Supporter payments | `payments` (succeeded, not refunded or disputed) | Payer active |
| Posts | `posts` | Approved by the moderator |

Every engaging account carries its trust weight `w(a)`:

| Tier | `w(a)` |
|---|---|
| trusted (≥ 0.70) | 1.0 |
| normal (0.40–0.70) | 1.0 |
| low (0.20–0.40) | 0.25 |
| quarantine (< 0.20), frozen, banned | 0 |

## 3. Scoring formula

For creator `c`:

```
R(c) = Σ_over reactors a   w(a) · min(reactions_by_a_on_c, 5)           # cap per fan: no single fan dominates
T(c) = Σ_over tippers a    w(a) · log1p(tips_pol_by_a_on_c × 100)
P(c) = Σ_over payers a     w(a) · log1p(paid_inr_by_a_on_c / 50)        # Dodo-backed support
Q(c) = min(approved_posts(c), 14) / 14                                  # activity factor, 0..1

points(c) = (sqrt(R) + 2·T + 3·P) · (0.5 + 0.5·Q)
```

Design rationale (keep this in the README):
- **sqrt(R)** gives diminishing returns, so 1,000 fake likes don't beat 100 real ones by 10×. Fake likes from low-trust accounts are already near zero weight anyway.
- **Paid support weighs most (×3)** because it costs real money and passes Dodo's checkout. It's the hardest signal to fake at scale.
- **Per-fan caps** stop one super-fan (or one bot) from carrying a creator.
- **Creator eligibility:** the creator must have `trust_score ≥ 0.2`, status active, and a linked wallet. Otherwise the allocation is 0, listed in `excluded` with a reason.

## 4. Allocation

```python
def allocate(points: dict[str, float], pool_wei: int, cap_bps: int = 1000) -> dict[str, int]:
    total = sum(points.values())
    if total == 0:
        return {}
    alloc = {c: int(pool_wei * p / total) for c, p in points.items()}
    cap = pool_wei * cap_bps // 10_000                        # no creator > 10% of an epoch
    overflow = sum(max(0, a - cap) for a in alloc.values())
    alloc = {c: min(a, cap) for c, a in alloc.items()}
    # redistribute overflow pro-rata among uncapped creators (one pass is enough at demo scale)
    uncapped = {c: points[c] for c, a in alloc.items() if a < cap}
    s = sum(uncapped.values()) or 1
    for c, p in uncapped.items():
        alloc[c] += int(overflow * p / s)
    return {c: a for c, a in alloc.items() if a >= MIN_CLAIM_WEI}   # drop dust (e.g. < 0.01 LOOP)
```

`total_wei = sum(alloc.values())`, which is ≤ the pool. The unallocated remainder (rounding, dust) stays in the distributor for the next epoch.

## 5. Merkle tree in Python (OpenZeppelin-compatible)

```python
# app/scoring/merkle.py
from eth_abi import encode
from eth_utils import keccak, to_checksum_address

def leaf(addr: str, amount: int) -> bytes:
    return keccak(keccak(encode(["address", "uint256"], [to_checksum_address(addr), amount])))

def _hash_pair(a: bytes, b: bytes) -> bytes:
    return keccak(a + b) if a < b else keccak(b + a)          # sorted pairs = OZ MerkleProof

def build(values: list[tuple[str, int]]) -> tuple[bytes, list[list[bytes]]]:
    leaves = sorted(leaf(a, v) for a, v in values)
    index = {l: i for i, l in enumerate(leaves)}
    layers = [leaves]
    while len(layers[-1]) > 1:
        cur, nxt = layers[-1], []
        for i in range(0, len(cur), 2):
            nxt.append(_hash_pair(cur[i], cur[i + 1]) if i + 1 < len(cur) else cur[i])   # odd node promoted
        layers.append(nxt)
    root = layers[-1][0]

    def proof_for(l: bytes) -> list[bytes]:
        i, proof = index[l], []
        for layer in layers[:-1]:
            sib = i ^ 1
            if sib < len(layer):
                proof.append(layer[sib])
            i //= 2
        return proof

    return root, [proof_for(leaf(a, v)) for a, v in values]
```

Why this verifies on-chain: OpenZeppelin's `MerkleProof.verify` hashes pairs in sorted order, so any tree built with sorted-pair hashing (and the same double-hashed leaf) verifies, even if its shape differs from `StandardMerkleTree`. The promoted odd node needs no sibling, which `verify` handles because the proof simply has one fewer element at that level.

**Parity test (mandatory):**
1. `pytest` builds a tree for 7 random `(address, amount)` pairs and writes `packages/contracts/test/fixtures/python-tree.json` (`root`, `values`, `proofs`).
2. The Hardhat test (Stage 02) publishes that root and claims every leaf with the Python proofs.
3. CI runs Python first, then contracts.

Edge cases to test: 1 leaf (root = leaf, empty proof), 2 leaves, odd counts, 1,000 leaves, and duplicate addresses (must not happen; assert uniqueness before building).

## 6. Publish

```python
async def publish(epoch: Epoch):
    dist = w3.eth.contract(address=DEPLOY["RewardDistributor"], abi=ABI["RewardDistributor"])
    tx = await dist.functions.publishEpoch(epoch.root, epoch.total_wei, epoch.allocations_cid).build_transaction({
        "from": PUBLISHER.address, "nonce": await w3.eth.get_transaction_count(PUBLISHER.address),
        "chainId": 80002})
    signed = PUBLISHER.sign_transaction(tx)
    h = await w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = await w3.eth.wait_for_transaction_receipt(h, timeout=180)
    onchain_id = decode_epoch_published(rcpt)["epochId"]
    await mark_published(epoch.id, tx_hash=h.hex(), onchain_epoch_id=onchain_id)
```

- Before publishing, check the distributor's LOOP balance against `outstanding + total` (the contract reverts `Underfunded` otherwise). Alert via viaSocket if it's low.
- **Freeze sync:** for every account frozen since the last epoch, ensure `setFrozen` was sent (the enforcer key). The scorer refuses to publish while pending freeze txs exist.

Allocations JSON pinned to IPFS (public transparency, no PII):

```json
{"schema":"creatorloop.allocations.v1","epoch":12,"window":["2026-11-02T10:00Z","2026-11-02T10:30Z"],
 "pool":"10000000000000000000000","total":"9998700000000000000000","root":"0x…",
 "formula":"v1: (sqrt(R)+2T+3P)*(0.5+0.5Q), cap 10%",
 "allocations":[{"wallet":"0x…","amount":"1234…","points":42.7}],
 "excluded":[{"wallet":"0x…","reason":"frozen:case-8f2…"}]}
```

## 7. Claims API

`GET /me/earnings` returns:

```json
{"loop": [{"epochId": 12, "amount": "1234…", "proof": ["0x…"], "claimed": false}],
 "frozen": false,
 "creatorFund": {"available_minor": 10430, "held_minor": 0, "currency": "INR"}}
```

The indexer flips `allocations.claimed` on the `Claimed` event.

## 8. Tests

- **Formula:** a hand-computed fixture (3 creators, 10 fans with mixed tiers) matches exactly.
- **Sybil resistance (property test with Hypothesis):** adding N quarantined accounts that react to creator X never increases X's allocation. Adding a trusted fan never decreases it.
- **Caps:** one dominant creator is capped at 10% and the overflow is redistributed; the sum is ≤ the pool.
- **Merkle:** the parity fixture plus the edge cases above.
- **Resumability:** kill the process after `pinned`; a re-run publishes without re-pinning a different CID.
- **Exclusions:** a frozen creator gets 0 and appears in `excluded`.

## 9. Acceptance checklist

- [ ] `POST /internal/epochs/run` (from viaSocket F3) completes end-to-end on Amoy.
- [ ] Allocations JSON is pinned and the CID is stored on-chain in the epoch.
- [ ] Creators can claim via the UI; the second claim fails.
- [ ] In the attack sim, the bot-ring creator's allocation is no more than 5% of what it would be without trust weighting (show both numbers in the demo).
- [ ] The Python↔Solidity Merkle parity test runs in CI.

## 10. Pitfalls

- **Window boundary games:** reactions deleted after the window closes. Snapshot inputs at `window_end` and use `created_at < window_end` only. Deletions within 24 h are allowed but not after scoring.
- **Unlinked wallets:** a creator without a linked wallet can't be in the tree. Prompt them in the UI and carry the allocation forward one epoch.
- **Publisher nonce collisions** if two runs overlap: hold a Postgres advisory lock around publish.
- **Float drift:** points are floats; amounts are integers computed once and then stored. Never recompute amounts from points later.
