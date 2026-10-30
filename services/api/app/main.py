import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from . import metadata  # noqa: F401  (registers the ipfs.fetch_metadata outbox handler)
from .automations import events as _events  # noqa: F401  (registers the viasocket.emit outbox handler)
from .automations import scheduler
from .chain import funders  # noqa: F401  (registers the chain.resolve_funder outbox handler)
from .chain.indexer import run_indexer
from .config import get_settings
from .db.models import Base
from .db.session import get_engine
from .graph import sync as graph_sync  # noqa: F401  (registers the graph.upsert outbox handler)
from .outbox import outbox_worker
from .retention import purge_old_signals
from .routers import admin, dev_dodo, dev_ipfs, internal, me, payments, public, wallet


async def _daily_retention(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await purge_old_signals()
        await asyncio.sleep(86_400)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if s.database_url.startswith("sqlite"):  # dev convenience; Postgres uses Alembic migrations
        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    stop = asyncio.Event()
    tasks = [asyncio.create_task(outbox_worker(stop)), asyncio.create_task(_daily_retention(stop))]
    if s.run_indexer:
        tasks.append(asyncio.create_task(run_indexer(stop)))
    sch = None
    if s.local_scheduler:
        sch = scheduler.build()
        sch.start()
    yield
    if sch:
        sch.shutdown(wait=False)
    stop.set()
    for t in tasks:
        t.cancel()
        with suppress(asyncio.CancelledError):
            await t


app = FastAPI(title="CreatorLoop API", version="0.1.0", lifespan=lifespan)
app.include_router(public.router)
app.include_router(me.router)
app.include_router(wallet.router)
app.include_router(admin.router)
app.include_router(internal.router)
app.include_router(payments.router)
app.include_router(dev_dodo.router)  # 404s unless DEV_DODO=true
app.include_router(dev_ipfs.router)  # 404s unless DEV_IPFS=true


@app.get("/health")
async def health() -> dict:
    s = get_settings()
    # Report which integrations are configured (never their values) to make setup gaps obvious.
    return {
        "ok": True,
        "integrations": {
            "privy": bool(s.privy_app_id and s.privy_verification_key),
            "tigergraph": bool(s.tg_host and s.tg_secret),
            "dodo": bool(s.dodo_payments_api_key and s.dodo_payments_webhook_secret),
            "viasocket": bool(s.viasocket_signing_secret),
            "ai": bool(s.ai_base_url and s.ai_api_key),
            "chain_publisher": bool(s.publisher_private_key),
        },
    }
