from datetime import timedelta

from sqlalchemy import delete

from .db.models import DeviceSignal, utcnow
from .db.session import session_maker

SIGNAL_RETENTION_DAYS = 90


async def purge_old_signals(days: int = SIGNAL_RETENTION_DAYS) -> int:
    async with session_maker()() as s, s.begin():
        res = await s.execute(delete(DeviceSignal).where(DeviceSignal.last_seen < utcnow() - timedelta(days=days)))
        return res.rowcount or 0
