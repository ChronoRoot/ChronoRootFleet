import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response

from app.database import create_db_and_tables
from app.core.sweeper import fast_monitor_loop, slow_discovery_loop
from app.routers import views, api_fleet, api_node, api_discovery

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    loop = asyncio.get_event_loop()
    
    # Fire up both independent engines
    task_fast = loop.create_task(fast_monitor_loop())
    task_slow = loop.create_task(slow_discovery_loop())
    
    yield
    task_fast.cancel()
    task_slow.cancel()

app = FastAPI(title="ChronoRoot Fleet Controller", lifespan=lifespan)

app.include_router(views.router)
app.include_router(api_fleet.router)
app.include_router(api_node.router)
app.include_router(api_discovery.router) 

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Returns a 204 (No Content) to instantly satisfy the browser and stop the 404 logs
    return Response(status_code=204)