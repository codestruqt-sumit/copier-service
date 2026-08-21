"""Copier service assembly: dashboard app + the reception poller thread.

    uvicorn app.main:app --host 0.0.0.0 --port 8100

Part 3 (the terminal executor) will attach here later as a second worker consuming
the action queue; Parts 1 and 2 never block on it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.dashboard import router as dashboard_router
from app.db import init_db, make_engine, make_session_factory
from app.executor import TerminalWorker
from app.poller import Poller
from app.sender_client import SenderClient
from app.telegram import notifier as telegram
from app.terminal import TerminalGateway

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
)
log = logging.getLogger("copier.main")


def create_app(*, start_poller: bool = True, http_client=None) -> FastAPI:
    engine = make_engine(settings.db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    client = SenderClient(
        settings.sender_base_url,
        settings.copier_key,
        settings.copier_name,
        http=http_client,
        timeout=settings.http_timeout_sec,
    )
    if telegram.enabled:
        log.info("telegram alerts enabled")
    worker = TerminalWorker(
        TerminalGateway(fast_market=settings.market_fast_path),
        client, session_factory, settings, notifier=telegram)
    # the poller reports the executor's terminal health on every heartbeat
    poller = Poller(client, session_factory, settings, health_provider=worker.health,
                    notifier=telegram)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_poller:
            poller.start()
            worker.start()  # dormant unless EXECUTOR_ENABLED=true
            # Best-effort startup ping (fire-and-forget; never delays boot).
            telegram.send(
                "activity",
                f"🟢 {settings.copier_name} started · executor "
                f"{'ON' if settings.executor_enabled else 'off'} · sender {settings.sender_base_url}")
        yield
        if start_poller:
            telegram.send("activity", f"🔴 {settings.copier_name} stopping")
        poller.stop()
        worker.stop()
        client.close()

    app = FastAPI(title=f"Copier · {settings.copier_name}", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.poller = poller
    app.state.worker = worker
    app.state.sender_client = client
    app.include_router(dashboard_router)
    return app


app = create_app()


if __name__ == "__main__":
    # Native launch (the VM path): `python -m app.main` honors DASHBOARD_HOST/DASHBOARD_PORT
    # from .env. (The Docker image launches uvicorn via its CMD instead.)
    import uvicorn

    uvicorn.run(app, host=settings.dashboard_host, port=settings.dashboard_port,
                log_level=settings.log_level.lower())
