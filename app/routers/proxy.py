import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

from app.core.state import LIVE_FLEET_STATE

router = APIRouter(tags=["Zero-Touch Proxy"])

async def stream_proxy_request(target_url: str, request: Request):
    """Safely manages the httpx stream lifecycle and decompresses data."""
    client = httpx.AsyncClient()
    
    # Strip the host header so httpx generates the correct one for the Pi
    req_headers = dict(request.headers)
    req_headers.pop("host", None)
    
    # Read the body in case the user clicked a "Submit" button inside the Pi UI
    body = await request.body()

    req = client.build_request(request.method, target_url, headers=req_headers, content=body)
    r = await client.send(req, stream=True)

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

    # Clean headers so the browser renders unzipped HTML properly
    resp_headers = dict(r.headers)
    resp_headers.pop("content-encoding", None)
    resp_headers.pop("content-length", None)
    resp_headers.pop("transfer-encoding", None)

    return StreamingResponse(stream_and_close(), status_code=r.status_code, headers=resp_headers)


# Notice we use api_route with multiple methods to support clicking buttons in the UI!
@router.api_route("/proxy/{mac}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_node(mac: str, path: str, request: Request):
    """Acts as the main tunnel for the iframe."""
    if mac not in LIVE_FLEET_STATE:
        raise HTTPException(status_code=404, detail="Module is currently offline.")
        
    target_ip = LIVE_FLEET_STATE[mac]["identity"]["ip"]
    target_url = f"http://{target_ip}/{path}"
    
    if request.url.query:
        target_url += f"?{request.url.query}"

    return await stream_proxy_request(target_url, request)