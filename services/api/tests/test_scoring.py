import json
import math
import random
from pathlib import Path

import pytest
from eth_account import Account
from hypothesis import given, settings
from hypothesis import strategies as st

from app.scoring import merkle
from app.scoring.score import MIN_CLAIM_WEI, Inputs, allocate, points_for, score, tier_weight

MNEMONIC = "test test test test test test test test test test test junk"  # Hardhat's default accounts
FIXTURE = Path(__file__).resolve().parents[3] / "packages/contracts/test/fixtures/python-tree.json"
LOOP = 10**18


def hardhat_address(i: int) -> str:
    Account.enable_unaudited_hdwallet_features()
    return Account.from_mnemonic(MNEMONIC, account_path=f"m/44'/60'/0'/0/{i}").address


def addrs(n: int) -> list[str]:
    return ["0x" + format(i + 1, "040x") for i in range(n)]


# ---------------------------------------------------------------- merkle


@pytest.mark.parametrize("n", [1, 2, 3, 5, 7, 8, 33, 1000])
def test_every_proof_verifies_for_any_size(n):
    values = [(a, (i + 1) * LOOP) for i, a in enumerate(addrs(n))]
    root, proofs = merkle.build(values)
    for (a, v), p in zip(values, proofs, strict=True):
        assert merkle.verify(root, a, v, p)
        assert not merkle.verify(root, a, v + 1, p)  # wrong amount
    if n == 1:
        assert root == merkle.leaf(*values[0]) and proofs[0] == []


def test_duplicates_and_bad_amounts_rejected():
    a = addrs(2)
    with pytest.raises(ValueError):
        merkle.build([(a[0], 1), (a[0].upper().replace("X", "x"), 2)])
    with pytest.raises(ValueError):
        merkle.build([(a[0], 0)])
    with pytest.raises(ValueError):
        merkle.build([])


def test_writes_parity_fixture_for_the_solidity_test():
    """Hardhat's RewardDistributor test publishes this root and claims every leaf with these proofs."""
    rng = random.Random(42)
    idx = list(range(1, 8))  # Hardhat accounts 1..7
    values = [(hardhat_address(i), rng.randint(1, 1000) * LOOP // 100) for i in idx]
    root, proofs = merkle.build(values)
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps({
        "root": "0x" + root.hex(),
        "values": [{"privateKeyIndex": i, "amount": str(v), "proof": ["0x" + x.hex() for x in p]}
                   for i, (_, v), p in zip(idx, values, proofs, strict=True)],
    }, indent=2) + "\n")
    assert FIXTURE.exists()


# ---------------------------------------------------------------- formula


def test_hand_computed_fixture():
    """3 creators, 10 fans with mixed tiers, worked out by hand."""
    weights = {"f1": 1.0, "f2": 1.0, "f3": 1.0, "f4": 0.25, "f5": 0.25, "f6": 0.0}
    i = Inputs(
        weights=weights,
        reactions={"A": {"f1": 3, "f2": 9, "f4": 2, "f6": 50}, "B": {"f3": 1}},
        tips_pol={"A": {"f1": 0.1}, "B": {"0xunlinked": 1.0}},
        paid_inr={"A": {"f2": 149.0}},
        posts={"A": 14, "B": 7},
    )
    # A: R = 1*3 + 1*min(9,5) + 0.25*2 + 0*5 = 8.5 ; T = log1p(10) ; P = log1p(149/50) ; Q = 1
    a = (math.sqrt(8.5) + 2 * math.log1p(10) + 3 * math.log1p(149 / 50)) * 1.0
    # B: R = 1 ; T = 0.25*log1p(100) ; P = 0 ; Q = 0.5
    b = (math.sqrt(1) + 2 * 0.25 * math.log1p(100)) * 0.75
    assert points_for("A", i) == pytest.approx(a)
    assert points_for("B", i) == pytest.approx(b)
    assert points_for("C", i) == 0.0 and "C" not in score(i, {"A", "B", "C"})


def test_tier_weights():
    assert [tier_weight(s, "active") for s in (0.9, 0.5, 0.3, 0.1)] == [1.0, 1.0, 0.25, 0.0]
    assert tier_weight(0.9, "frozen") == 0 == tier_weight(0.9, "banned")
    assert tier_weight(0.9, "restricted") == 0.25


@given(n_bots=st.integers(0, 40), reactions_each=st.integers(1, 20))
@settings(max_examples=60, deadline=None)
def test_quarantined_accounts_never_raise_a_creators_points(n_bots, reactions_each):
    base = Inputs(weights={"fan": 1.0}, reactions={"X": {"fan": 3}}, posts={"X": 4})
    bots = {f"bot{i}": 0.0 for i in range(n_bots)}  # quarantined: weight 0
    more = Inputs(weights={**base.weights, **bots},
                  reactions={"X": {"fan": 3, **{b: reactions_each for b in bots}}}, posts={"X": 4})
    assert points_for("X", more) == pytest.approx(points_for("X", base))


@given(extra=st.integers(1, 5))
def test_adding_a_trusted_fan_never_decreases_points(extra):
    base = Inputs(weights={"a": 1.0}, reactions={"X": {"a": 2}}, posts={"X": 1})
    more = Inputs(weights={"a": 1.0, "b": 1.0}, reactions={"X": {"a": 2, "b": extra}}, posts={"X": 1})
    assert points_for("X", more) >= points_for("X", base)


# ---------------------------------------------------------------- allocation


def test_cap_redistributes_and_never_exceeds_pool():
    pool = 10_000 * LOOP
    pts = {"whale": 1000.0, **{f"c{i}": 10.0 for i in range(20)}}
    alloc = allocate(pts, pool, cap_bps=1000)
    assert alloc["whale"] == pool // 10  # capped at 10%
    assert sum(alloc.values()) <= pool
    others = [v for k, v in alloc.items() if k != "whale"]
    assert max(others) == min(others)  # the overflow went to the others, equally


def test_dust_dropped_and_empty_inputs():
    assert allocate({}, 10**22) == {}
    assert allocate({"a": 0.0}, 10**22) == {}
    alloc = allocate({"big": 1e9, "tiny": 1e-9}, 10**22, cap_bps=10_000)
    assert "tiny" not in alloc and all(v >= MIN_CLAIM_WEI for v in alloc.values())


@given(pts=st.dictionaries(st.text("abcdef", min_size=1, max_size=4), st.floats(0.001, 1e6), min_size=1, max_size=30),
       cap=st.integers(500, 10_000))
@settings(max_examples=80, deadline=None)
def test_allocation_invariants(pts, cap):
    pool = 10_000 * LOOP
    alloc = allocate(pts, pool, cap)
    assert sum(alloc.values()) <= pool
    assert all(0 < v <= pool * cap // 10_000 for v in alloc.values())
