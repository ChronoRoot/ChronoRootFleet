"""Endpoints for Fleet Commander host configuration (self-update, time/NTP, network)."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core import state as fleet_state
from app.core.system_ops import (
    apply_system_time_config,
    get_commander_network_status,
    get_commander_time_status,
    run_git_update,
    schedule_service_restart,
)

router = APIRouter(prefix="/api/commander", tags=["Commander Host"])


class CommanderTimeRequest(BaseModel):
    mode: str = Field(..., pattern="^(network|manual)$")
    timezone: Optional[str] = None
    ntp_server: Optional[str] = None
    date: Optional[str] = None


class CommanderNetworkRequest(BaseModel):
    subnet: str = Field(..., min_length=5, max_length=15)


@router.post("/update")
async def update_commander_software():
    """Pull latest Fleet Commander code via git; restart service if changed."""
    payload = run_git_update()
    if payload.get("result") and payload.get("changed"):
        schedule_service_restart()

    status_code = 200 if payload.get("result") else 400
    if payload.get("code") == "timeout":
        status_code = 504
    elif payload.get("code") == "git_unavailable":
        status_code = 500
    return JSONResponse(content=payload, status_code=status_code)


@router.get("/time")
async def get_commander_time():
    """Current commander host clock and NTP settings."""
    return get_commander_time_status()


@router.post("/time")
async def set_commander_time(req: CommanderTimeRequest):
    """Set commander NTP server / manual time on the host OS."""
    if req.mode == "manual" and not req.date:
        raise HTTPException(status_code=422, detail="date is required for manual mode.")

    success, message = apply_system_time_config(
        mode=req.mode,
        date_str=req.date,
        timezone=req.timezone,
        ntp_server=req.ntp_server,
    )
    if not success:
        raise HTTPException(status_code=500, detail=message)

    status = get_commander_time_status()
    return {"result": True, "message": message, **status}


@router.get("/network")
async def get_commander_network():
    """Current discovery subnet, host addresses, and install-mode hint."""
    return get_commander_network_status()


@router.put("/network")
async def set_commander_network(req: CommanderNetworkRequest):
    """Change discovery subnet, persist it, and restart the Fleet Commander service."""
    try:
        subnet = fleet_state.persist_target_subnet(req.subnet)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not write runtime config: {e}",
        ) from e

    schedule_service_restart()
    status = get_commander_network_status()
    return {
        "result": True,
        "message": (
            f"Discovery subnet set to {subnet}. "
            "The Fleet Commander service is restarting to apply it."
        ),
        "restarting": True,
        **status,
    }
