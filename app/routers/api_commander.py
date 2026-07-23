"""Endpoints for Fleet Commander host configuration (self-update, time/NTP)."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.system_ops import (
    apply_system_time_config,
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


@router.post("/update")
async def update_commander_software():
    """Pull latest Fleet Commander code via git; restart service if changed."""
    success, message, changed = run_git_update()
    if not success:
        raise HTTPException(
            status_code=400,
            detail={"result": False, "message": message, "changed": False},
        )

    if changed:
        schedule_service_restart()

    return {"result": True, "message": message, "changed": changed}


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
