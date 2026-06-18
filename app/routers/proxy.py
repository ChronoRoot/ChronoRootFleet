import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session

from app.core.state import LIVE_FLEET_STATE, HTTP_CLIENT, resolve_proxy_target, normalize_mac
from app.database import engine, RobotModule

router = APIRouter(tags=["Zero-Touch Proxy"])

PROXY_TIMEOUT = httpx.Timeout(20.0)


def _normalize_content_type(headers: dict) -> str:
    return headers.get("content-type", "").split(";")[0].strip().lower()


async def stream_proxy_request(target_url: str, request: Request, mac: str):
    client = HTTP_CLIENT
    owns_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=PROXY_TIMEOUT)
        owns_client = True

    req_headers = dict(request.headers)
    req_headers.pop("host", None)
    req_headers["X-Fleet-Proxy"] = "1"

    body = await request.body()
    req = client.build_request(request.method, target_url, headers=req_headers, content=body)

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
            if owns_client:
                await client.aclose()

    resp_headers = dict(r.headers)
    resp_headers.pop("content-encoding", None)
    resp_headers.pop("content-length", None)
    resp_headers.pop("transfer-encoding", None)

    content_type = _normalize_content_type(resp_headers)
    if content_type == "text/html":
        resp_headers.pop("etag", None)
        resp_headers.pop("ETag", None)
        resp_headers.pop("cache-control", None)
        resp_headers.pop("Cache-Control", None)
        resp_headers["Cache-Control"] = "no-store"

    if r.status_code in (301, 302, 303, 307, 308) and "location" in resp_headers:
        original_location = resp_headers["location"]
        if original_location.startswith("/"):
            resp_headers["location"] = f"/proxy/{mac}{original_location}"

    return StreamingResponse(stream_and_close(), status_code=r.status_code, headers=resp_headers)


def _resolve_target_ip(mac: str) -> str:
    norm_mac = normalize_mac(mac)
    if norm_mac in LIVE_FLEET_STATE:
        return LIVE_FLEET_STATE[norm_mac]["identity"]["ip"]

    db_ip = None
    db_last_seen = None
    with Session(engine) as session:
        mod = session.get(RobotModule, norm_mac) or session.get(RobotModule, mac)
        if mod:
            db_ip = mod.ip_address
            db_last_seen = mod.last_seen

    ip, _warning = resolve_proxy_target(norm_mac, db_ip, db_last_seen)
    if not ip:
        raise HTTPException(status_code=404, detail="Module is currently offline.")
    return ip


@router.api_route("/proxy/{mac}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_node(mac: str, path: str, request: Request):
    target_ip = _resolve_target_ip(mac)
    target_url = f"http://{target_ip}/{path}"

    if request.url.query:
        target_url += f"?{request.url.query}"

    return await stream_proxy_request(target_url, request, mac)
