import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_account
from ..config import get_settings
from ..db.models import Account, FraudCase, utcnow
from ..db.session import get_session
from ..graph import sync
from ..graph.trust import run_trust_job
from ..outbox import enqueue
from ..payments.ledger import set_held, unbalanced_txns


async def require_admin(acct: Account = Depends(current_account)) -> Account:
    if acct.privy_user_id not in get_settings().admin_ids:
        raise HTTPException(403, "admin only")
    return acct


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _case_view(c: FraudCase) -> dict:
    return {"id": str(c.id), "kind": c.kind, "status": c.status, "severity": c.severity,
            "size": len(c.member_ids), "members": c.member_ids, "evidence": c.evidence,
            "agent_report": c.agent_report, "decision": c.decision, "opened_at": c.opened_at.isoformat()}


@router.get("/cases")
async def cases(status: str | None = None, s: AsyncSession = Depends(get_session)):
    q = select(FraudCase).order_by(FraudCase.opened_at.desc())
    if status:
        q = q.where(FraudCase.status == status)
    return [_case_view(c) for c in (await s.scalars(q)).all()]


@router.get("/cases/{case_id}")
async def case(case_id: uuid.UUID, s: AsyncSession = Depends(get_session)):
    c = await s.get(FraudCase, case_id)
    if c is None:
        raise HTTPException(404, "case not found")
    members = (await s.scalars(select(Account).where(Account.id.in_([uuid.UUID(m) for m in c.member_ids])))).all()
    return {**_case_view(c), "accounts": [
        {"id": str(m.id), "status": m.status, "trust_score": m.trust_score, "risk_flags": m.risk_flags}
        for m in members]}


class Decision(BaseModel):
    decision: str  # freeze | restrict | dismiss
    note: str = ""


@router.post("/cases/{case_id}/decision")
async def decide(case_id: uuid.UUID, body: Decision, admin: Account = Depends(require_admin),
                 s: AsyncSession = Depends(get_session)):
    """Human-in-the-loop enforcement. Reversible: unfreeze via a later dismiss decision."""
    if body.decision not in ("freeze", "restrict", "dismiss"):
        raise HTTPException(422, "decision must be freeze, restrict or dismiss")
    c = await s.get(FraudCase, case_id)
    if c is None:
        raise HTTPException(404, "case not found")
    members = (await s.scalars(select(Account).where(Account.id.in_([uuid.UUID(m) for m in c.member_ids])))).all()
    for m in members:
        flags = dict(m.risk_flags or {})
        if body.decision == "freeze":
            m.status = "frozen"
            if m.wallet_address:
                await enqueue(s, "chain.set_frozen", {"wallet": m.wallet_address, "frozen": True,
                                                      "case_id": str(c.id)})
        elif body.decision == "restrict":
            m.status = "restricted" if m.status == "active" else m.status
            flags["restricted_epochs"] = 2
        else:
            if m.status in ("frozen", "restricted"):
                m.status = "active"
                if m.wallet_address:
                    await enqueue(s, "chain.set_frozen", {"wallet": m.wallet_address, "frozen": False,
                                                          "case_id": str(c.id)})
            flags["reviewed_ok"] = utcnow().isoformat()
        m.risk_flags = flags
    if body.decision == "dismiss" and (pids := (c.evidence or {}).get("payment_ids")):
        await set_held(s, pids, False)  # release the creator fund
    c.decision, c.decision_note, c.decided_by, c.decided_at = body.decision, body.note, admin.id, utcnow()
    c.status = "dismissed" if body.decision == "dismiss" else "actioned"
    await s.commit()
    return _case_view(c)


@router.post("/trust/run")
async def trust_run(s: AsyncSession = Depends(get_session)):
    return await run_trust_job(s, sync.store)


@router.post("/graph/rebuild")
async def graph_rebuild(s: AsyncSession = Depends(get_session)):
    g = await sync.rebuild(s)
    return {"accounts": len(g.accounts)}


@router.get("/accounts/{account_id}/trust")
async def account_trust(account_id: uuid.UUID, s: AsyncSession = Depends(get_session)):
    a = await s.get(Account, account_id)
    if a is None:
        raise HTTPException(404, "account not found")
    return {"score": a.trust_score, "updated_at": a.trust_updated_at, "status": a.status, **(a.risk_flags or {})}


@router.get("/ledger/summary")
async def ledger_summary(s: AsyncSession = Depends(get_session)):
    from sqlalchemy import func

    from ..db.models import LedgerEntry

    rows = (await s.execute(select(LedgerEntry.account, func.sum(LedgerEntry.amount_minor))
                            .group_by(LedgerEntry.account))).all()
    totals: dict[str, int] = {}
    for account, total in rows:  # collapse per-creator accounts into one total
        key = "creator_fund" if account.startswith("creator_fund:") else account
        totals[key] = totals.get(key, 0) + int(total or 0)
    bad = await unbalanced_txns(s)
    return {"totals_minor": totals, "invariant_ok": not bad, "unbalanced_txns": [str(t) for t in bad]}


@router.get("/epochs")
async def epochs(s: AsyncSession = Depends(get_session)):
    from ..db.models import Epoch
    from ..scoring.epoch import summary

    return [summary(e) for e in (await s.scalars(select(Epoch).order_by(Epoch.id.desc()).limit(50))).all()]


@router.get("/epochs/{epoch_id}")
async def epoch_detail(epoch_id: int, s: AsyncSession = Depends(get_session)):
    """Includes the same engagement paid with and without trust weighting, for the demo comparison."""
    from ..db.models import Allocation, Epoch
    from ..scoring.epoch import summary

    e = await s.get(Epoch, epoch_id)
    if e is None:
        raise HTTPException(404, "epoch not found")
    rows = (await s.scalars(select(Allocation).where(Allocation.epoch_id == epoch_id))).all()
    naive = (e.stats or {}).get("naive_allocations", {})
    return {**summary(e), "excluded_detail": e.excluded,
            "allocations": [{"account_id": str(a.account_id), "wallet": a.wallet, "amount": str(a.amount_wei),
                             "naive_amount": naive.get(str(a.account_id), "0"), "claimed": a.claimed} for a in rows],
            "naive_only": {k: v for k, v in naive.items() if k not in {str(a.account_id) for a in rows}}}
