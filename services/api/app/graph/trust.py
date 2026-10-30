import hashlib
import math
from collections import defaultdict
from datetime import timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Account, FraudCase, Payment, utcnow
from ..outbox import enqueue
from ..payments.ledger import set_held
from .features import Analysis, Feat, analyse, build_linkage
from .store import GraphStore

log = structlog.get_logger()
REOPEN_AFTER = timedelta(days=7)


def tier(score: float) -> str:
    return "trusted" if score >= 0.70 else "normal" if score >= 0.40 else "low" if score >= 0.20 else "quarantine"


def compose(f: Feat, *, age_days: float, paid_count: int = 0, disputes: int = 0, in_clone_group: bool = False
            ) -> tuple[float, dict[str, float]]:
    """Explainable score: every term is returned so admins and the investigator agent can see why."""
    parts = {
        "trustrank": 1.6 * f.trustrank_norm,
        "age": 0.4 * min(age_days / 30, 1),
        "paid_clean": (0.4 * min(paid_count, 3) / 3) if disputes == 0 else -0.8,
        "linkage": -0.25 * max(0, f.linkage_cluster_size - 2),
        "ring": -2.0 if (f.community_size >= 5 and f.community_internal_ratio > 0.8
                         and f.community_reciprocity > 0.6) else 0.0,
        "clone": -1.2 if in_clone_group else 0.0,
    }
    z = -0.6 + sum(parts.values())
    return 1 / (1 + math.exp(-3 * z)), parts


def _fingerprint(kind: str, members) -> str:
    return hashlib.sha256((kind + "|" + ",".join(sorted(members))).encode()).hexdigest()


def payment_abuse_candidates(store: GraphStore, max_shared_degree: int = 25) -> list[dict]:
    """Self-dealing (payer linked to the creator they pay) and one PayerId/Device behind 3+ payers of a creator."""
    paid = [p for p in store.payments.values() if p.get("status") == "succeeded" and p["src"] != p["dst"]]
    links = build_linkage(store, max_shared_degree)
    out = []
    for p in paid:
        pair = tuple(sorted((p["src"], p["dst"])))
        if pair in links:
            ids = [q["payment_id"] for q in paid if {q["src"], q["dst"]} == set(pair)]
            out.append({"kind": "payment_abuse", "members": set(pair), "severity": 4,
                        "evidence": {"reason": "self_dealing", "shared": sorted(links[pair]), "payment_ids": ids}})
    payers_of: dict[str, set[str]] = {}
    for p in paid:
        payers_of.setdefault(p["dst"], set()).add(p["src"])
    for creator, fans in payers_of.items():
        for kind in ("PayerId", "Device"):
            for members in store.signals[kind].values():
                group = members & fans
                if len(group) >= 3 and len(members) <= max_shared_degree:
                    ids = [q["payment_id"] for q in paid if q["dst"] == creator and q["src"] in group]
                    out.append({"kind": "payment_abuse", "members": group | {creator}, "severity": 4,
                                "evidence": {"reason": f"shared_{kind.lower()}", "payment_ids": ids}})
    return out


def candidate_cases(a: Analysis, extra: list[dict] | None = None) -> list[dict]:
    """Case opening rules from the plan. payment_abuse arrives with Stage 07 payments."""
    out = list(extra or [])
    creators_of: dict[str, set[str]] = defaultdict(set)
    for (fan, creator) in a.engagement:
        creators_of[fan].add(creator)
    for c in a.clusters:
        m = c["members"]
        shared_creators = defaultdict(int)
        for fan in m:
            for cr in creators_of.get(fan, ()):
                shared_creators[cr] += 1
        if len(m) >= 4 and any(n >= 2 for n in shared_creators.values()):
            out.append({"kind": "linkage_cluster", "members": m, "severity": min(5, 2 + len(m) // 4),
                        "evidence": {"signals": c["signals"], "size": len(m)}})
    for c in a.communities:
        if len(c["members"]) >= 5 and c["internal_ratio"] > 0.8 and c["reciprocity"] > 0.6:
            out.append({"kind": "collusion_ring", "members": c["members"], "severity": min(5, 3 + len(c["members"]) // 6),
                        "evidence": {"internal_ratio": round(c["internal_ratio"], 3),
                                     "reciprocity": round(c["reciprocity"], 3), "size": len(c["members"])}})
    for g in a.clone_groups:
        if len(g) >= 3:
            pairs = [(x, y, n, round(j, 3)) for x, y, n, j in a.clone_pairs if x in g]
            out.append({"kind": "clone_pairs", "members": g, "severity": 3, "evidence": {"pairs": pairs[:20]}})
    return out


async def open_cases(s: AsyncSession, a: Analysis, extra: list[dict] | None = None) -> list[FraudCase]:
    opened = []
    for c in candidate_cases(a, extra):
        fp = _fingerprint(c["kind"], c["members"])
        existing = await s.scalar(select(FraudCase).where(FraudCase.fingerprint == fp))
        if existing is not None:
            stale = existing.status == "dismissed" and existing.decided_at is not None and \
                utcnow() - existing.decided_at.replace(tzinfo=existing.decided_at.tzinfo or utcnow().tzinfo) > REOPEN_AFTER
            if not stale:
                continue
            existing.status, existing.decision, existing.opened_at = "open", None, utcnow()
            case = existing
        else:
            case = FraudCase(kind=c["kind"], severity=c["severity"], fingerprint=fp,
                             member_ids=sorted(c["members"]), evidence=c["evidence"])
            s.add(case)
        await s.flush()
        if pids := c["evidence"].get("payment_ids"):
            await set_held(s, pids, True)  # hold the creator fund until the case closes
        await enqueue(s, "agents.investigate", {"case_id": str(case.id)})
        await enqueue(s, "viasocket.emit", {"event": "fraud.case_opened", "case_id": str(case.id),
                                            "kind": case.kind, "severity": case.severity,
                                            "size": len(case.member_ids)})
        opened.append(case)
    return opened


async def run_trust_job(s: AsyncSession, store: GraphStore, *, max_shared_degree: int = 25,
                        min_reactions: int = 10, min_jaccard: float = 0.8) -> dict:
    """Score every account present in the graph, persist score + explanation, and open fraud cases."""
    rows = {str(x.id): x for x in (await s.scalars(select(Account))).all()}
    pays = (await s.scalars(select(Payment).where(Payment.account_id.is_not(None)))).all()
    paid_count: dict[str, int] = defaultdict(int)
    bad_count: dict[str, int] = defaultdict(int)
    for p in pays:
        if p.status == "succeeded":
            paid_count[str(p.account_id)] += 1
        elif p.status in ("refunded", "disputed"):
            bad_count[str(p.account_id)] += 1
    now = utcnow()

    def old_enough(x: Account, days: int) -> bool:
        return (now - x.created_at.replace(tzinfo=x.created_at.tzinfo or now.tzinfo)).days >= days

    seeds = {i for i, x in rows.items() if x.status == "active" and (
        (x.risk_flags or {}).get("seed_trusted")
        or (paid_count[i] >= 1 and not bad_count[i] and old_enough(x, 7)))}  # real money is an anti-sybil signal
    a = analyse(store, seeds, max_shared_degree, min_reactions, min_jaccard)
    in_clone = {m for g in a.clone_groups if len(g) >= 2 for m in g}
    for aid, f in a.feats.items():
        acct = rows.get(aid)
        if acct is None:
            continue
        age = (now - acct.created_at.replace(tzinfo=acct.created_at.tzinfo or now.tzinfo)).total_seconds() / 86400
        score, parts = compose(f, age_days=age, paid_count=paid_count[aid], disputes=bad_count[aid],
                                in_clone_group=aid in in_clone)
        reasons = list(f.reasons)
        if parts["ring"]:
            reasons.append(f"in a closed engagement ring of {f.community_size} accounts")
        flags = dict(acct.risk_flags or {})
        flags.update({"parts": {k: round(v, 3) for k, v in parts.items()}, "reasons": reasons, "tier": tier(score)})
        acct.risk_flags, acct.trust_score, acct.trust_updated_at = flags, score, now
    cases = await open_cases(s, a, payment_abuse_candidates(store, max_shared_degree))
    await s.commit()
    log.info("trust.run", accounts=len(a.feats), cases=len(cases))
    return {"accounts": len(a.feats), "cases_opened": len(cases), "seeds": len(seeds)}
