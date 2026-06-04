import asyncio
import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import create_db_and_tables
from app.core.sweeper import fast_monitor_loop, slow_discovery_loop
from app.routers import views, api_fleet, api_node, api_discovery, proxy

# --- ZERO TOUCH PROXY MIDDLEWARE ---
class ProxySandboxMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        
        # 1. If the browser is correctly asking for the proxy, let it pass
        if path.startswith("/proxy/"):
            return await call_next(request)
            
        # 2. Check if this request originated from inside the Pi's iframe
        referer = request.headers.get("referer", "")
        match = re.search(r'/proxy/([A-Fa-f0-9:-]+)/', referer)
        
        if match:
            mac = match.group(1)
            # The Pi's HTML asked for an absolute path like '/' or '/static/css'
            # We force the browser to redirect back into the proxy sandbox!
            new_url = f"/proxy/{mac}{path}"
            if request.url.query:
                new_url += f"?{request.url.query}"
                
            # A 307 Redirect tells the browser: "Try again at this new URL, 
            # and keep the exact same GET/POST method."
            return RedirectResponse(url=new_url, status_code=307)
            
        # 3. Normal Master Controller traffic passes through unaffected
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    loop = asyncio.get_event_loop()
    
    task_fast = loop.create_task(fast_monitor_loop())
    task_slow = loop.create_task(slow_discovery_loop())
    
    yield
    task_fast.cancel()
    task_slow.cancel()

app = FastAPI(title="ChronoRoot Fleet Controller", lifespan=lifespan)

# Attach our new Middleware to the app
app.add_middleware(ProxySandboxMiddleware)

app.include_router(views.router)
app.include_router(api_fleet.router)
app.include_router(api_node.router)
app.include_router(api_discovery.router) 
app.include_router(proxy.router) 

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)