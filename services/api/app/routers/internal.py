"""Endpoints called by viaSocket cron flows. Static token auth; every job is idempotent."""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..automations import jobs
from ..config import get_settings
from ..db.session import get_session


def require_internal(x_internal_token: str = Header(...)) -> None:
    expected = get_settings().internal_api_token
    if not expected:
        raise HTTPException(503, "internal API token not configured")
    if not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(401, "bad token")


router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal)])


@router.post("/trust/run")
async def trust_run(s: AsyncSession = Depends(get_session)):
    return await jobs.trust_run(s)


@router.post("/epochs/run")
async def epochs_run(s: AsyncSession = Depends(get_session)):
    return await jobs.epoch_run(s)


@router.post("/digests/creators")
async def digests(s: AsyncSession = Depends(get_session)):
    return await jobs.creator_digests(s)


@router.post("/maintenance/retention")
async def retention(s: AsyncSession = Depends(get_session)):
    return await jobs.retention(s)


@router.post("/ledger/check")
async def ledger_check(s: AsyncSession = Depends(get_session)):
    return await jobs.ledger_check(s)
