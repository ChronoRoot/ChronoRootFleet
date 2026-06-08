import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

from app.core.state import LIVE_FLEET_STATE

router = APIRouter(tags=["Zero-Touch Proxy"])

# Update your stream_proxy_request function to accept the mac address
async def stream_proxy_request(target_url: str, request: Request, mac: str):
    client = httpx.AsyncClient(timeout=20.0)
    
    req_headers = dict(request.headers)
    req_headers.pop("host", None)
    
    body = await request.body()
    req = client.build_request(request.method, target_url, headers=req_headers, content=body)
    
    # We do NOT want httpx to follow redirects automatically! 
    # We want to catch the 302/303 response and rewrite it for the browser.
    r = await client.send(req, stream=True, follow_redirects=False) 

    async def stream_and_close():
        try:
            async for chunk in r.aiter_bytes():
                if await request.is_disconnected():
                    break
                yield chunk
        except httpx.ReadError:
            pass
        finally:
            await r.aclose()
            await client.aclose()

    resp_headers = dict(r.headers)
    resp_headers.pop("content-encoding", None)
    resp_headers.pop("content-length", None)
    resp_headers.pop("transfer-encoding", None)

    # ==========================================
    # THE MAGIC FIX: Rewrite the Location Header
    # ==========================================
    if r.status_code in (301, 302, 303, 307, 308) and "location" in resp_headers:
        original_location = resp_headers["location"]
        
        # If the Pi returned an absolute path (e.g., /storage/), 
        # prepend our proxy tunnel path so the browser stays in the iframe.
        if original_location.startswith("/"):
            resp_headers["location"] = f"/proxy/{mac}{original_location}"

    return StreamingResponse(stream_and_close(), status_code=r.status_code, headers=resp_headers)


@router.api_route("/proxy/{mac}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_node(mac: str, path: str, request: Request):
    if mac not in LIVE_FLEET_STATE:
        raise HTTPException(status_code=404, detail="Module is currently offline.")
        
    target_ip = LIVE_FLEET_STATE[mac]["identity"]["ip"]
    target_url = f"http://{target_ip}/{path}"
    
    if request.url.query:
        target_url += f"?{request.url.query}"

    # Pass the MAC address down so the proxy knows how to rewrite the headers
    return await stream_proxy_request(target_url, request, mac)