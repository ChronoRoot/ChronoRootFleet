from fastapi import APIRouter
from app.core.sweeper import execute_discovery_sweep

router = APIRouter(prefix="/api/fleet")

@router.post("/discover")
async def trigger_discovery():
    """Triggers a 3-second subnet sweep and registers new devices to the DB."""
    count = await execute_discovery_sweep()
    return {"status": "success", "message": f"Discovery complete. Found {count} registered nodes.", "count": count}