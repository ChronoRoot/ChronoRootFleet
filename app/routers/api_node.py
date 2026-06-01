import httpx
from typing import Optional, List
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from datetime import datetime
from sqlmodel import Session
from app.database import engine, RobotModule
from app.core.state import LIVE_FLEET_STATE
from app.core.transformers import digest_node_state 

router = APIRouter(prefix="/api/node", tags=["Node Control"])

# --- VALIDATION MODELS ---

class TimeSyncNodeRequest(BaseModel):
    mode: str = Field(default="network", pattern="^(network|manual)$")
    timezone: Optional[str] = None
    ntp_server: Optional[str] = None
    date_str: Optional[str] = None # Expected format: YYYY-MM-DD HH:MM:SS

class ConfigUpdateRequest(BaseModel):
    TIME_ZONE: Optional[str] = None
    CAMERA_TYPE: Optional[str] = None
    SELECTOR_TYPE: Optional[str] = Field(None, pattern="^(SINGLE|IVPORT)$")
    SYNC_ENABLED: Optional[bool] = None
    SYNC_INTERVAL: Optional[int] = Field(None, ge=1) # Must be >= 1 minute
    SYNC_REMOTE_TYPE: Optional[str] = Field(None, pattern="^(local|sftp|ftp)$")

# --- HELPER FUNCTION ---
def get_node_ip(mac: str) -> str:
    """Helper to safely extract the IP or raise a 404."""
    if mac not in LIVE_FLEET_STATE:
        raise HTTPException(status_code=404, detail=f"Module {mac} is currently offline.")
    return LIVE_FLEET_STATE[mac]["identity"]["ip"]

@router.get("/{mac}/dashboard-state")
async def get_digested_node_state(mac: str):
    master_time_obj = datetime.now()
    raw = LIVE_FLEET_STATE.get(mac, {})
    
    with Session(engine) as session:
        db_mod = session.get(RobotModule, mac)
        
    # Pass the data through our single source of truth
    return digest_node_state(mac, raw, db_mod, master_time_obj)
    
@router.get("/{mac}/config")
async def get_node_config(mac: str):
    """Fetches the current user_config.py state from a specific Pi."""
    ip = get_node_ip(mac)
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(f"http://{ip}/api/config", timeout=3.0)
            res.raise_for_status()
            return res.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch config from {ip}: {e}")

@router.put("/{mac}/config")
async def update_node_config(mac: str, config_data: ConfigUpdateRequest):
    """Pushes a new configuration payload to a specific Pi."""
    ip = get_node_ip(mac)
    
    # exclude_unset=True ensures we only send the keys the user actually provided
    payload = config_data.model_dump(exclude_unset=True)
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.put(f"http://{ip}/api/config", json=payload, timeout=5.0)
            res.raise_for_status()
            return res.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to apply config to {ip}: {e}")

@router.post("/{mac}/time")
async def sync_node_time(mac: str, time_data: TimeSyncNodeRequest):
    """Forces the node to sync its time based on the Master's instructions."""
    ip = get_node_ip(mac)
    payload = time_data.model_dump(exclude_unset=True)
    
    # Auto-fill manual date with Master's time if missing
    if time_data.mode == "manual" and not time_data.date_str:
        payload["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif "date_str" in payload:
        payload["date"] = payload.pop("date_str")

    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(f"http://{ip}/api/config/time", json=payload, timeout=5.0)
            res.raise_for_status()
            return res.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Time sync failed on {ip}: {e}")

@router.post("/{mac}/diagnostic")
async def trigger_node_diagnostic(mac: str):
    """Triggers the 8-minute hardware scan on a specific Pi."""
    ip = get_node_ip(mac)
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(f"http://{ip}/api/diagnostic", timeout=5.0)
            res.raise_for_status()
            return res.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Diagnostic failed to start on {ip}: {e}")

@router.post("/{mac}/reboot")
async def reboot_node(mac: str):
    """Sends the graceful reboot signal."""
    ip = get_node_ip(mac)
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(f"http://{ip}/api/reboot", timeout=2.0)
            return {"status": "success", "message": f"Reboot signal sent to {ip}"}
        except Exception:
            # We expect this might timeout or drop instantly if the Pi reboots fast enough
            return {"status": "success", "message": f"Reboot signal sent to {ip} (Connection dropped as expected)"}