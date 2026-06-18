import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app.database import create_db_and_tables
from app.core import state as fleet_state
from app.core.sweeper import fast_monitor_loop
from app.routers import views, api_fleet, api_node, api_discovery, proxy

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class ProxySandboxMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path.startswith("/proxy/"):
            return await call_next(request)

        referer = request.headers.get("referer", "")
        match = re.search(r"/proxy/([A-Fa-f0-9:-]+)/", referer)

        if match:
            mac = match.group(1)
            new_url = f"/proxy/{mac}{path}"
            if request.url.query:
                new_url += f"?{request.url.query}"
            return RedirectResponse(url=new_url, status_code=307)

        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()

    limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
    fleet_state.HTTP_CLIENT = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(20.0))
    app.state.http_client = fleet_state.HTTP_CLIENT

    loop = asyncio.get_event_loop()
    task_fast = loop.create_task(fast_monitor_loop())

    if (
        os.getenv("FLEET_POLL_BATCH_SIZE") is not None
        and os.getenv("FLEET_MAX_CONCURRENT_POLLS") is None
    ):
        logger.warning(
            "FLEET_POLL_BATCH_SIZE is deprecated; use FLEET_MAX_CONCURRENT_POLLS instead"
        )

    logger.info(
        "Fleet sweeper started (subnet=%s, concurrent=%s, connect=%ss, read=%ss, grace=%s polls)",
        fleet_state.TARGET_SUBNET_BASE,
        fleet_state.MAX_CONCURRENT_POLLS,
        fleet_state.CONNECT_TIMEOUT,
        fleet_state.READ_TIMEOUT,
        fleet_state.OFFLINE_GRACE_POLLS,
    )

    yield

    task_fast.cancel()
    await fleet_state.HTTP_CLIENT.aclose()
    fleet_state.HTTP_CLIENT = None


app = FastAPI(title="ChronoRoot Fleet Controller", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(ProxySandboxMiddleware)

app.include_router(views.router)
app.include_router(api_fleet.router)
app.include_router(api_node.router)
app.include_router(api_discovery.router)
app.include_router(proxy.router)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)
