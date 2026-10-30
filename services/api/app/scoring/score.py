"""Trust-weighted creator scoring and allocation. Pure functions: inputs are plain dicts so the formula is
hand-checkable and property-testable. Points are floats; amounts are integers computed once and stored."""

import math
from dataclasses import dataclass, field
from fractions import Fraction

MIN_CLAIM_WEI = 10**16  # 0.01 LOOP; smaller allocations are dust and stay in the distributor
FAN_REACTION_CAP = 5
UNLINKED_TIPPER_WEIGHT = 0.25


def tier_weight(trust_score: float, status: str) -> float:
    """Reward weight of an engaging account. Frozen/banned/quarantined count for nothing."""
    if status in ("frozen", "banned"):
        return 0.0
    if trust_score < 0.20:
        return 0.0
    if trust_score < 0.40 or status == "restricted":
        return 0.25
    return 1.0


@dataclass
class Inputs:
    """Everything the formula needs for one epoch window, keyed by creator id."""

    weights: dict[str, float]  # engaging account id -> w(a)
    reactions: dict[str, dict[str, int]] = field(default_factory=dict)  # creator -> fan -> count
    tips_pol: dict[str, dict[str, float]] = field(default_factory=dict)  # creator -> tipper key -> POL
    paid_inr: dict[str, dict[str, float]] = field(default_factory=dict)  # creator -> payer -> INR
    posts: dict[str, int] = field(default_factory=dict)  # creator -> approved posts in window


def points_for(creator: str, i: Inputs, uniform_weights: bool = False) -> float:
    def w(a: str) -> float:
        if uniform_weights:
            return 1.0
        if a.startswith("0x"):  # tipper with no linked account
            return UNLINKED_TIPPER_WEIGHT
        return i.weights.get(a, 0.0)

    r = sum(w(a) * min(n, FAN_REACTION_CAP) for a, n in i.reactions.get(creator, {}).items())
    t = sum(w(a) * math.log1p(pol * 100) for a, pol in i.tips_pol.get(creator, {}).items())
    p = sum(w(a) * math.log1p(inr / 50) for a, inr in i.paid_inr.get(creator, {}).items())
    q = min(i.posts.get(creator, 0), 14) / 14
    return (math.sqrt(r) + 2 * t + 3 * p) * (0.5 + 0.5 * q)


def score(i: Inputs, creators: set[str], uniform_weights: bool = False) -> dict[str, float]:
    pts = {c: points_for(c, i, uniform_weights) for c in creators}
    return {c: p for c, p in pts.items() if p > 0}


def allocate(points: dict[str, float], pool_wei: int, cap_bps: int = 1000) -> dict[str, int]:
    """Pro-rata split of the pool with a per-creator cap. Overflow from capped creators is redistributed among
    the rest until stable. Exact integer arithmetic (Fractions), so the sum never exceeds the pool."""
    cap = pool_wei * cap_bps // 10_000
    remaining, active = pool_wei, {c: Fraction(p) for c, p in points.items() if p > 0}
    alloc: dict[str, int] = {}
    while active:
        total = sum(active.values())
        tentative = {c: int(remaining * p / total) for c, p in active.items()}
        capped = [c for c, a in tentative.items() if a > cap]
        if not capped:
            alloc.update(tentative)
            break
        for c in capped:
            alloc[c] = cap
            remaining -= cap
            del active[c]
    return {c: a for c, a in alloc.items() if a >= MIN_CLAIM_WEI}
