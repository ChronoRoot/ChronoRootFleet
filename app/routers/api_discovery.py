from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.core.state import get_discovery_progress
from app.core.sweeper import execute_discovery_sweep

router = APIRouter(prefix="/api/fleet")


@router.get("/discover/status")
async def discovery_status():
    """Live snapshot of the current (or last) manual discovery sweep."""
    return JSONResponse(
        content=get_discovery_progress(),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/discover")
async def trigger_discovery():
    """Triggers a full subnet sweep (.1–.254) and registers discovered devices to the DB."""
    try:
        result = await execute_discovery_sweep()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Discovery failed: {e}",
        ) from e

    return {
        "status": "success",
        "count": result["count"],
        "subnet": result["subnet"],
        "duration_seconds": result["duration_seconds"],
        "message": result["message"],
    }
