# app/routers/api_fleet.py
import asyncio
import httpx
import secrets
import time
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
from sqlmodel import Session, select

from app.database import RobotModule, engine, ExperimentalBatch, ExperimentRun
from app.core.state import (
    LIVE_FLEET_STATE,
    HTTP_CLIENT,
    get_telemetry_meta,
    normalize_mac,
    remove_module_from_memory,
    get_poll_diagnostics,
    count_presence_grace_modules,
)
from app.core.transformers import digest_node_state 

router = APIRouter(prefix="/api/fleet", tags=["Fleet Orchestration"])

# --- VALIDATION MODELS ---
class BulkActionRequest(BaseModel):
    target_macs: List[str]

class BulkUpdateRequest(BulkActionRequest):
    force: bool = False
    authorization_token: Optional[str] = None

class AuthorizedModuleActionRequest(BulkActionRequest):
    authorization_token: str

class BulkTimeSyncRequest(BulkActionRequest):
    mode: str = Field(default="network", pattern="^(network|manual)$")
    timezone: Optional[str] = None
    ntp_server: Optional[str] = None
    date_str: Optional[str] = None 

class BulkConfigRequest(BulkActionRequest):
    TIME_ZONE: Optional[str] = None
    NTP_SERVER: Optional[str] = None   
    USE_NTP: Optional[bool] = None    
    SYNC_ENABLED: Optional[bool] = None
    SYNC_INTERVAL: Optional[int] = Field(None, ge=1)
    remote_type: Optional[str] = Field(None, pattern="^(local|sftp|ftp|advanced)$")
    destination_path: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None

class BulkSyncTestRequest(BulkActionRequest):
    remote_type: str = Field(..., pattern="^(sftp|ftp)$")
    host: str
    user: str
    password: str
    port: Optional[int] = None

# --- ASYNC BROADCAST HELPER ---
async def broadcast_to_nodes(macs: List[str], method: str, endpoint: str, get_payload_fn=None, timeout: float = 5.0):
    """
    Generic concurrency helper. 
    `get_payload_fn` is an optional callable that takes (mac, node_state) 
    and returns the specific JSON payload for that exact node.
    """
    client = HTTP_CLIENT
    owns_client = False
    if client is None:
        client = httpx.AsyncClient()
        owns_client = True

    try:
        async def call_node(mac):
            norm_mac = normalize_mac(mac)
            if norm_mac not in LIVE_FLEET_STATE:
                return {"mac": mac, "status": "error", "message": "Offline"}

            node_state = LIVE_FLEET_STATE[norm_mac]
            
            try:
                ip = node_state["identity"]["ip"]
                payload = get_payload_fn(mac, node_state) if get_payload_fn else None
                req_kwargs = {"timeout": timeout}
                if payload is not None:
                    req_kwargs["json"] = payload

                if method == "POST":
                    res = await client.post(f"http://{ip}/api/{endpoint}", **req_kwargs)
                elif method == "PUT":
                    res = await client.put(f"http://{ip}/api/{endpoint}", **req_kwargs)
                else:
                    res = await client.get(f"http://{ip}/api/{endpoint}", **req_kwargs)
                    
                res.raise_for_status()
                
                try:
                    resp_data = res.json() if res.content else {}
                except Exception:
                    resp_data = {"text": res.text}

                return {
                    "mac": mac,
                    "status": "success",
                    "http_status": res.status_code,
                    "data": resp_data,
                }
            except httpx.HTTPStatusError as e:
                try:
                    resp_data = e.response.json() if e.response.content else {}
                except Exception:
                    resp_data = {"text": e.response.text}
                message = (
                    resp_data.get("message")
                    or resp_data.get("error")
                    or str(e)
                )
                return {
                    "mac": mac,
                    "status": "error",
                    "http_status": e.response.status_code,
                    "message": message,
                    "data": resp_data,
                }
            except Exception as e:
                return {
                    "mac": mac,
                    "status": "error",
                    "error_type": type(e).__name__,
                    "message": str(e),
                }
            
        return await asyncio.gather(*[call_node(m) for m in macs])
    finally:
        if owns_client:
            await client.aclose()

@router.get("/diagnostics")
async def get_fleet_diagnostics():
    """Poll monitor health: last cycle stats, error breakdown, live counts."""
    return JSONResponse(
        content={
            "monitor": get_poll_diagnostics(),
            "live_online_count": len(LIVE_FLEET_STATE),
            "presence_grace_count": count_presence_grace_modules(),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/live-digested")
async def get_digested_fleet_state():
    digested_list = []
    master_time_obj = datetime.now()

    with Session(engine) as session:
        db_modules = {m.mac_address: m for m in session.exec(select(RobotModule)).all()}

    for mac, db_mod in db_modules.items():
        norm_mac = normalize_mac(mac)
        raw = LIVE_FLEET_STATE.get(norm_mac, LIVE_FLEET_STATE.get(mac, {}))
        telemetry_meta = get_telemetry_meta(norm_mac)
        digested_node = digest_node_state(mac, raw, db_mod, master_time_obj, telemetry_meta)
        digested_list.append(digested_node)

    return JSONResponse(
        content={
            "master_time": master_time_obj.strftime("%Y-%m-%d %H:%M:%S"),
            "nodes": digested_list,
        },
        headers={"Cache-Control": "no-store"},
    )

# --- 2. SWARM COMMANDS ---
@router.post("/bulk/diagnostic")
async def bulk_diagnostic(req: BulkActionRequest):
    results = await broadcast_to_nodes(req.target_macs, "POST", "diagnostic")
    return {"status": "complete", "results": results}

@router.post("/bulk/reboot")
async def bulk_reboot(req: BulkActionRequest):
    # Short timeout expected because the Pi drops the connection to restart
    results = await broadcast_to_nodes(req.target_macs, "POST", "reboot", timeout=2.0)
    return {"status": "complete", "results": results}


UPDATE_SUCCESS_CODES = {
    "updated",
    "force_updated",
    "up_to_date",
}
UPDATE_ERROR_CODES = {
    "network_error",
    "authentication_error",
    "repository_error",
    "permission_error",
    "timeout",
    "git_unavailable",
    "invalid_request",
    "git_error",
}
UPDATE_RESPONSE_CODES = {
    *UPDATE_SUCCESS_CODES,
    *UPDATE_ERROR_CODES,
    "force_required",
}
UPDATE_AUTHORIZATION_TTL_SECONDS = 30 * 60
UPDATE_AUTHORIZATIONS: Dict[str, Dict[str, Any]] = {}


def _issue_update_authorization(mac: str, action: str) -> str:
    """Create a short-lived, one-time authorization bound to one module/action."""
    now = time.monotonic()
    expired = [
        token
        for token, authorization in UPDATE_AUTHORIZATIONS.items()
        if authorization["expires_at"] <= now
    ]
    for token in expired:
        UPDATE_AUTHORIZATIONS.pop(token, None)

    token = secrets.token_urlsafe(32)
    UPDATE_AUTHORIZATIONS[token] = {
        "mac": normalize_mac(mac),
        "action": action,
        "expires_at": now + UPDATE_AUTHORIZATION_TTL_SECONDS,
    }
    return token


def _consume_update_authorization(token: str, mac: str, action: str) -> bool:
    """Validate and consume an authorization so it cannot be replayed."""
    authorization = UPDATE_AUTHORIZATIONS.pop(token, None)
    if not authorization:
        return False
    return (
        authorization["expires_at"] > time.monotonic()
        and authorization["mac"] == normalize_mac(mac)
        and authorization["action"] == action
    )


def _invalid_update_action(mac: str, message: str) -> Dict[str, Any]:
    return {
        "mac": mac,
        "status": "error",
        "http_status": None,
        "result": False,
        "code": "invalid_request",
        "message": message,
        "changed": False,
        "can_force": False,
    }


def _normalize_update_result(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Expose only the stable update contract, never raw HTTP or Git output."""
    mac = raw.get("mac")
    http_status = raw.get("http_status")
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}

    if http_status is None:
        if raw.get("error_type") == "ReadTimeout":
            return {
                "mac": mac,
                "status": "error",
                "http_status": None,
                "result": False,
                "code": "timeout",
                "message": (
                    "Fleet Commander timed out waiting for the module's "
                    "update response."
                ),
                "changed": False,
                "can_force": False,
            }
        return {
            "mac": mac,
            "status": "error",
            "http_status": None,
            "result": False,
            "code": "module_unreachable",
            "message": "Fleet Commander could not contact this module.",
            "changed": False,
            "can_force": False,
        }

    code = data.get("code")
    message = data.get("message")
    result = data.get("result") is True
    changed = data.get("changed") is True
    can_force = data.get("can_force") is True

    legacy_shape = (
        "code" not in data
        and "can_force" not in data
        and isinstance(message, str)
        and isinstance(data.get("result"), bool)
        and isinstance(data.get("changed"), bool)
    )
    if legacy_shape:
        if 200 <= http_status < 300 and result:
            return {
                "mac": mac,
                "status": "success",
                "http_status": http_status,
                "result": True,
                "code": "updated" if changed else "up_to_date",
                "message": message,
                "changed": changed,
                "can_force": False,
            }
        if http_status >= 400 and not result and not changed:
            return {
                "mac": mac,
                "status": "error",
                "http_status": http_status,
                "result": False,
                "code": "git_error",
                "message": message,
                "changed": False,
                "can_force": False,
            }

    valid_shape = (
        isinstance(code, str)
        and code in UPDATE_RESPONSE_CODES
        and isinstance(message, str)
        and isinstance(data.get("result"), bool)
        and isinstance(data.get("changed"), bool)
        and isinstance(data.get("can_force"), bool)
    )
    if not valid_shape:
        return {
            "mac": mac,
            "status": "error",
            "http_status": http_status,
            "result": False,
            "code": "invalid_request",
            "message": (
                "The module returned an unsupported update response. "
                "It may need a one-time manual ChronoRootControl upgrade."
            ),
            "changed": False,
            "can_force": False,
            "_restart_fallback": True,
        }

    is_changed_success = (
        200 <= http_status < 300
        and result
        and code in {"updated", "force_updated"}
        and changed
        and not can_force
    )
    is_up_to_date = (
        200 <= http_status < 300
        and result
        and code == "up_to_date"
        and not changed
        and not can_force
    )
    is_force_required = (
        http_status == 409
        and not result
        and code == "force_required"
        and not changed
        and can_force
    )
    is_classified_error = (
        http_status >= 400
        and not result
        and code in UPDATE_ERROR_CODES
        and not changed
        and not can_force
    )
    if is_changed_success or is_up_to_date:
        status = "success"
    elif is_force_required:
        status = "warning"
    elif is_classified_error:
        status = "error"
    else:
        return {
            "mac": mac,
            "status": "error",
            "http_status": http_status,
            "result": False,
            "code": "invalid_request",
            "message": "The module returned an inconsistent update result.",
            "changed": False,
            "can_force": False,
            "_restart_fallback": True,
        }

    return {
        "mac": mac,
        "status": status,
        "http_status": http_status,
        "result": result,
        "code": code,
        "message": message,
        "changed": changed,
        "can_force": can_force,
    }


@router.post("/bulk/update")
async def bulk_software_update(req: BulkUpdateRequest):
    """
    Run one explicit update step on each selected module.

    The UI always calls this first with force=false. A force retry is sent only
    for an individual module after the operator confirms the destructive step.
    Changed updates and unsupported legacy responses receive a one-time restart
    authorization so the UI can restart them automatically.
    """
    if req.force:
        valid_force_request = (
            len(req.target_macs) == 1
            and bool(req.authorization_token)
            and _consume_update_authorization(
                req.authorization_token,
                req.target_macs[0],
                "force",
            )
        )
        if not valid_force_request:
            return {
                "status": "complete",
                "results": [
                    _invalid_update_action(
                        mac,
                        "Force update authorization is missing, expired, or invalid.",
                    )
                    for mac in req.target_macs
                ],
            }

    def get_update_payload(mac, state):
        return {"force": True} if req.force else {}

    raw_results = await broadcast_to_nodes(
        req.target_macs,
        "POST",
        "update",
        get_payload_fn=get_update_payload,
        timeout=130.0,
    )
    results = [_normalize_update_result(r) for r in raw_results]
    for result in results:
        restart_fallback = result.pop("_restart_fallback", False)
        if (
            not req.force
            and result["http_status"] == 409
            and result["result"] is False
            and result["code"] == "force_required"
            and result["can_force"] is True
        ):
            result["force_token"] = _issue_update_authorization(
                result["mac"],
                "force",
            )
        if (
            (result["result"] is True and result["changed"] is True)
            or restart_fallback
        ):
            result["restart_token"] = _issue_update_authorization(
                result["mac"],
                "restart",
            )

    return {"status": "complete", "results": results}


@router.post("/bulk/update/restart")
async def restart_updated_modules(req: AuthorizedModuleActionRequest):
    """Consume an update-issued authorization and restart its bound module."""
    valid_restart_request = (
        len(req.target_macs) == 1
        and _consume_update_authorization(
            req.authorization_token,
            req.target_macs[0],
            "restart",
        )
    )
    if not valid_restart_request:
        return {
            "status": "complete",
            "results": [
                _invalid_update_action(
                    mac,
                    "Restart authorization is missing, expired, or invalid.",
                )
                for mac in req.target_macs
            ],
        }

    raw_results = await broadcast_to_nodes(
        req.target_macs,
        "GET",
        "restart_service",
        timeout=1.5,
    )
    results = []
    expected_disconnects = {"ReadTimeout", "RemoteProtocolError"}
    for raw in raw_results:
        request_dispatched = (
            raw.get("http_status") is not None
            or raw.get("status") == "success"
            or raw.get("error_type") in expected_disconnects
        )
        if request_dispatched:
            results.append({
                "mac": raw.get("mac"),
                "status": "progress",
                "result": True,
                "code": "restarting",
                "message": (
                    "Restart command sent. The module may be briefly unavailable "
                    "while its services restart."
                ),
                "changed": True,
                "can_force": False,
            })
        else:
            results.append({
                "mac": raw.get("mac"),
                "status": "error",
                "result": False,
                "code": "module_unreachable",
                "message": "Fleet Commander could not contact this module.",
                "changed": True,
                "can_force": False,
            })

    return {"status": "complete", "results": results}


@router.post("/bulk/time")
async def bulk_time_sync(req: BulkTimeSyncRequest):
    base_payload = req.model_dump(exclude={"target_macs", "date_str"}, exclude_unset=True)
    
    if req.mode == "manual" and not req.date_str:
        base_payload["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif req.date_str:
        base_payload["date"] = req.date_str

    def get_time_payload(mac, state):
        return base_payload

    results = await broadcast_to_nodes(req.target_macs, "POST", "config/time", get_payload_fn=get_time_payload)

    # Keep Fleet DB clock-mode flags aligned when mode is known
    if req.mode in ("network", "manual"):
        use_ntp = req.mode == "network"
        with Session(engine) as session:
            for mac in req.target_macs:
                mod = session.get(RobotModule, normalize_mac(mac)) or session.get(RobotModule, mac)
                if not mod:
                    continue
                mod.use_ntp = use_ntp
                if req.ntp_server:
                    mod.ntp_server = req.ntp_server
                session.add(mod)
            session.commit()

    return {"status": "complete", "results": results}


# 3. ADD THE UWSGI RESTART (Inside bulk_config_update)
@router.put("/bulk/config")
async def bulk_config_update(req: BulkConfigRequest):
    req_dict = req.model_dump(exclude={"target_macs"}, exclude_unset=True)
    
    # --- 1. Map General System Keys ---
    general_keys = ["TIME_ZONE", "NTP_SERVER", "USE_NTP"]
    general_payload = {k: v for k, v in req_dict.items() if k in general_keys}
    
    # --- 2. Map Sync Keys (Translate Case for Flask API) ---
    sync_payload = {}
    if "SYNC_ENABLED" in req_dict: sync_payload["sync_enabled"] = req_dict["SYNC_ENABLED"]
    if "SYNC_INTERVAL" in req_dict: sync_payload["sync_interval"] = req_dict["SYNC_INTERVAL"]
    
    for k in ["remote_type", "destination_path", "host", "port", "user", "password"]:
        if k in req_dict: sync_payload[k] = req_dict[k]
        
    results = []
    successful_macs = set()
    
    # --- 3. Dispatch General Config ---
    if general_payload:
        def get_gen_payload(mac, state): return general_payload
        res_gen = await broadcast_to_nodes(req.target_macs, "PUT", "config", get_payload_fn=get_gen_payload)
        results.extend(res_gen)
        successful_macs.update([r["mac"] for r in res_gen if r["status"] == "success"])
        
    # --- 4. Dispatch Sync Config ---
    if sync_payload:
        def get_sync_payload(mac, state): return sync_payload
        res_sync = await broadcast_to_nodes(req.target_macs, "POST", "sync/config", get_payload_fn=get_sync_payload)
        results.extend(res_sync)
        successful_macs.update([r["mac"] for r in res_sync if r["status"] == "success"])

    # --- 4b. Apply OS time/NTP when clock settings were changed ---
    time_keys_touched = any(k in general_payload for k in ("USE_NTP", "NTP_SERVER", "TIME_ZONE"))
    if time_keys_touched and successful_macs:
        use_ntp = general_payload.get("USE_NTP")
        timezone = general_payload.get("TIME_ZONE")
        ntp_server = general_payload.get("NTP_SERVER")
        master_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Resolve per-MAC mode from request, else current Fleet DB flag
        db_ntp_by_mac = {}
        with Session(engine) as session:
            for mac in successful_macs:
                mod = session.get(RobotModule, normalize_mac(mac)) or session.get(RobotModule, mac)
                if mod:
                    db_ntp_by_mac[normalize_mac(mac)] = mod.use_ntp

        def get_os_time_payload(mac, state):
            mac_ntp = use_ntp
            if mac_ntp is None:
                mac_ntp = db_ntp_by_mac.get(normalize_mac(mac), False)
            if mac_ntp:
                payload = {"mode": "network"}
                if timezone:
                    payload["timezone"] = timezone
                if ntp_server:
                    payload["ntp_server"] = ntp_server
                return payload
            payload = {"mode": "manual", "date": master_time}
            if timezone:
                payload["timezone"] = timezone
            return payload

        res_time = await broadcast_to_nodes(
            list(successful_macs),
            "POST",
            "config/time",
            get_payload_fn=get_os_time_payload,
            timeout=10.0,
        )
        results.extend(res_time)

        # Persist Fleet DB flags for successful config targets
        with Session(engine) as session:
            for mac in successful_macs:
                mod = session.get(RobotModule, normalize_mac(mac)) or session.get(RobotModule, mac)
                if not mod:
                    continue
                if use_ntp is not None:
                    mod.use_ntp = bool(use_ntp)
                if ntp_server:
                    mod.ntp_server = ntp_server
                session.add(mod)
            session.commit()
        
    # --- 5. Restart Updated Nodes ---
    if successful_macs:
        # Give the file system a split second to flush writes
        await asyncio.sleep(0.5) 
        # Expect timeouts here as the service dies and resurrects
        await broadcast_to_nodes(list(successful_macs), "GET", "restart_service", timeout=1.5)
        
    return {"status": "complete", "results": results}

class ExperimentLaunchRequest(BaseModel):
    name: str = Field(default=None, max_length=16)
    description: str = ""   
    target_macs: List[str]
    start_time: str
    end_time: str
    interval: int = Field(ge=5) # Minimum 5 minutes
    ir_enabled: bool = True
    cameras: List[int] = [] # Empty means auto-detect all available
    strict_mode: bool = True # If true, aborts if ANY node fails pre-flight

def parse_dt(dt_str: str) -> datetime:
    clean_str = dt_str.replace("T", " ")
    if len(clean_str) == 16: clean_str += ":00"
    return datetime.strptime(clean_str[:19], "%Y-%m-%d %H:%M:%S")

@router.post("/experiment/launch")
async def launch_global_experiment(req: ExperimentLaunchRequest):
    try:
        req_start = parse_dt(req.start_time)
        req_end = parse_dt(req.end_time)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid date format: {e}")

    # Basic Math
    if req_end < (req_start + timedelta(minutes=5)):
        raise HTTPException(status_code=422, detail="Experiment duration must be > 5 minutes.")
        
    total_mins = (req_end - req_start).total_seconds() / 60.0
    expected_per_cam = int(total_mins / req.interval)
    IMAGE_SIZE_MB = 3.0 # Calibrated to 3MB per high-res picture

    valid_nodes = []
    
    # ==========================================
    # PHASE 1: STRICT PRE-FLIGHT VALIDATION
    # ==========================================
    for mac in req.target_macs:
        norm_mac = normalize_mac(mac)
        if norm_mac not in LIVE_FLEET_STATE:
            if req.strict_mode:
                raise HTTPException(status_code=409, detail=f"Node {mac} is offline. Aborting launch.")
            continue # Skip offline node

        node = LIVE_FLEET_STATE[norm_mac]
        hostname = node["identity"]["hostname"]
        
        # 1A. Hardware Validation
        available_cams = [int(c) for c in node.get("cam_reports", {}).keys()]
        if not available_cams:
            raise HTTPException(status_code=409, detail=f"[{hostname}] reports 0 connected cameras.")
            
        target_cameras = req.cameras if req.cameras else available_cams
        for req_cam in target_cameras:
            if req_cam not in available_cams:
                raise HTTPException(status_code=409, detail=f"[{hostname}] lacks physical Camera {req_cam}.")

        # 1B. Storage Validation
        required_mb = expected_per_cam * len(target_cameras) * IMAGE_SIZE_MB
        required_gb = required_mb / 1024.0
        free_gb = node.get("system_health", {}).get("storage", {}).get("free_gb", 0)
        
        if required_gb >= free_gb:
            raise HTTPException(
                status_code=409, 
                detail=f"[{hostname}] Insufficient storage! Needs ~{required_gb:.2f}GB, but only has {free_gb}GB free."
            )

        # 1C. Scheduling Conflict Detection
        for job_data in node.get("jobs", {}).values():
            if job_data.get("status") in ["CANCELLED", "ERROR", "FINISHED"]: 
                continue
            
            job_start = parse_dt(job_data["start"])
            job_end_str = job_data.get("end", "Unknown")
            job_end = parse_dt(job_end_str) if job_end_str != "Unknown" else job_start + timedelta(days=7)
            
            # Check for timeline overlap
            if max(req_start, job_start) < min(req_end, job_end):
                raise HTTPException(
                    status_code=409, 
                    detail=f"[{hostname}] Timeline conflict with existing job '{job_data.get('name', 'Job')}'."
                )

        # Node passed all checks!
        valid_nodes.append({
            "mac": mac, 
            "ip": node["identity"]["ip"], 
            "hostname": hostname,
            "target_cameras": target_cameras
        })

    if not valid_nodes:
        raise HTTPException(status_code=400, detail="No valid nodes remained to launch against.")

    # ==========================================
    # PHASE 2: DATABASE BATCH CREATION
    # ==========================================
    # We create the batch *before* broadcasting so we have a valid ID to attach runs to.
    try:
        with Session(engine) as session:
            new_batch = ExperimentalBatch(
                name=req.name, 
                interval_minutes=req.interval, 
                ir_enabled=req.ir_enabled,
                launched_at=datetime.utcnow()
            )
            session.add(new_batch)
            session.commit()
            session.refresh(new_batch)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Master DB Failure: {e}")

    # ==========================================
    # PHASE 3: FLEET BROADCAST
    # ==========================================
    successes = []
    failures = []
    
    async with httpx.AsyncClient() as client:
        async def launch_on_node(node_info):
            payload = {
                "name": req.name,
                "desc": req.description,  
                "start": req_start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": req_end.strftime("%Y-%m-%d %H:%M:%S"),
                "interval": req.interval,
                "ir": req.ir_enabled,
                "cameras": node_info["target_cameras"]
            }
            try:
                # Calls Pi API -> POST /
                res = await client.post(f"http://{node_info['ip']}/api/", json=payload, timeout=5.0)
                if res.status_code == 201:
                    local_exp_id = res.json().get("expid")
                    return {"status": "success", "mac": node_info["mac"], "local_id": local_exp_id, "expected": expected_per_cam}
                else:
                    return {"status": "error", "hostname": node_info["hostname"], "msg": f"Rejected: {res.text}"}
            except Exception as e:
                return {"status": "error", "hostname": node_info["hostname"], "msg": f"Network drop: {str(e)}"}
        
        # Execute concurrently
        broadcast_results = await asyncio.gather(*[launch_on_node(n) for n in valid_nodes])
        
        for r in broadcast_results:
            if r["status"] == "success": successes.append(r)
            else: failures.append(f"[{r['hostname']}] {r['msg']}")

    # ==========================================
    # PHASE 4: DATABASE RECORD LINKING
    # ==========================================
    try:
        with Session(engine) as session:
            for s in successes:
                run = ExperimentRun(
                    batch_id=new_batch.id,
                    module_mac=s["mac"],
                    local_exp_id=s["local_id"],
                    status="SCHEDULED",
                    expected_total=s["expected"],
                    start_time=req_start.strftime("%Y-%m-%d %H:%M:%S"), 
                    end_time=req_end.strftime("%Y-%m-%d %H:%M:%S")      
                )
                session.add(run)
            session.commit()
    except Exception as e:
        # Note: If this fails, the experiment is still running on the Pis, but DB tracking is fractured.
        failures.append(f"Master DB Error saving run records: {e}")

    # ==========================================
    # PHASE 5: RESPONSE
    # ==========================================
    if failures:
        # Return a 200/207-style response if some succeeded, so the frontend knows it was a partial success
        return {
            "status": "partial_error", 
            "message": f"Broadcast complete, but {len(failures)} errors occurred.", 
            "failures": failures
        }
    
    return {
        "status": "success", 
        "message": f"Batch Experiment '{req.name}' safely launched on {len(successes)} modules."
    }
    
# --- DATABASE & HISTORY VIEWS ---

@router.post("/db/sync_archives")
async def sync_archives_to_db():
    """Connects to all known nodes, downloads their history, and backfills the Master DB accurately."""
    with Session(engine) as session:
        known_macs = [m.mac_address for m in session.exec(select(RobotModule)).all()]
        
    results = await broadcast_to_nodes(known_macs, "GET", "history")
    
    updates_count = 0
    new_runs_count = 0
    
    with Session(engine) as session:
        for res in results:
            if res["status"] != "success" or not res.get("data"): continue
            
            mac = res["mac"]
            history_data = res["data"]
            
            for local_exp_id, job_data in history_data.items():
                existing_run = session.exec(select(ExperimentRun).where(ExperimentRun.local_exp_id == local_exp_id)).first()
                
                expected = int(job_data.get("expected_pictures") or 0)
                taken = int(job_data.get("taken_pictures") or 0)
                start_str = job_data.get("start", "")
                end_str = job_data.get("end", "")
                msg_str = job_data.get("message", "")
                status_str = job_data.get("status", "FINISHED")

                # --- 1. UPDATE EXISTING RUNS ---
                if existing_run:
                    existing_run.expected_total = expected
                    existing_run.taken_so_far = taken
                    existing_run.missed_frames = max(0, expected - taken)
                    existing_run.status = status_str
                    
                    if start_str: existing_run.start_time = start_str
                    if end_str: existing_run.end_time = end_str
                    if msg_str: existing_run.message = msg_str
                    
                    session.add(existing_run)
                    updates_count += 1
                    continue
                
                # --- 2. CREATE NEW BATCHES (SPLIT BY TIME) ---
                batch_name = job_data.get("name") or f"Orphan - {local_exp_id}"
                
                try:
                    launched_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    launched_dt = datetime.utcnow()
                    
                # Find all existing batchs with this exact name
                batchs_with_name = session.exec(select(ExperimentalBatch).where(ExperimentalBatch.name == batch_name)).all()
                
                matching_batch = None
                for c in batchs_with_name:
                    # If launched within 2 minutes of each other, it's the same global run!
                    delta = abs((c.launched_at - launched_dt).total_seconds())
                    if delta < 120:
                        matching_batch = c
                        break
                        
                # If no matching timeframe was found, spawn a brand new separate batch
                if not matching_batch:
                    matching_batch = ExperimentalBatch(
                        name=batch_name,
                        interval_minutes=int(job_data.get("interval") or 15),
                        ir_enabled=False,
                        launched_at=launched_dt
                    )
                    session.add(matching_batch)
                    session.commit()
                    session.refresh(matching_batch)
                    
                # --- 3. CREATE THE RUN ---
                new_run = ExperimentRun(
                    batch_id=matching_batch.id,
                    module_mac=mac,
                    local_exp_id=local_exp_id,
                    status=status_str,
                    expected_total=expected,
                    taken_so_far=taken,
                    missed_frames=max(0, expected - taken),
                    start_time=start_str,
                    end_time=end_str,
                    message=msg_str
                )
                session.add(new_run)
                new_runs_count += 1
                
        session.commit()
        
    return {"status": "success", "message": f"Sync complete. Added {new_runs_count} new runs and updated {updates_count} existing runs."}


@router.get("/db/experiments")
async def get_db_experiments():
    with Session(engine) as session:
        batchs = session.exec(select(ExperimentalBatch).order_by(ExperimentalBatch.id.desc())).all()
        result = []
        for c in batchs:
            runs = session.exec(select(ExperimentRun).where(ExperimentRun.batch_id == c.id)).all()
            if not runs: continue 
            
            total_expected = sum([r.expected_total or 0 for r in runs])
            total_taken = sum([r.taken_so_far or 0 for r in runs])
            missed = sum([r.missed_frames or 0 for r in runs])
            
            statuses = [r.status for r in runs]
            if "RUNNING" in statuses: global_status = "RUNNING"
            elif "ERROR" in statuses: global_status = "ERROR"
            else: global_status = "FINISHED"

            nodes_data = []
            for r in runs:
                mod = session.get(RobotModule, r.module_mac)
                display_name = mod.alias if (mod and mod.alias) else (mod.hostname if mod else "Unknown Node")
                
                nodes_data.append({
                    "mac": r.module_mac, 
                    "hostname": display_name,
                    "ip": mod.ip_address if mod else None,
                    "local_exp_id": r.local_exp_id,
                    "status": r.status, 
                    "taken": r.taken_so_far or 0, 
                    "expected": r.expected_total or 0, 
                    "missed": r.missed_frames or 0,
                    "start_time": r.start_time, 
                    "end_time": r.end_time,     
                    "message": r.message        
                })

            if isinstance(c.launched_at, str):
                launch_str = c.launched_at[:16]
            else:
                launch_str = c.launched_at.strftime("%Y-%m-%d %H:%M")

            result.append({
                "id": c.id, "name": c.name, "launched_at": launch_str,
                "interval": c.interval_minutes or 0, "node_count": len(runs), "global_status": global_status,
                "progress": {"taken": total_taken, "expected": total_expected, "missed": missed},
                "nodes": nodes_data
            })
        return result

@router.get("/db/modules/{mac}/history")
async def get_module_history(mac: str):
    """Powers the 'Experiment History' modal for a single module."""
    with Session(engine) as session:
        runs = session.exec(select(ExperimentRun).where(ExperimentRun.module_mac == mac)).all()
        history_runs = []
        for r in runs:
            c = session.get(ExperimentalBatch, r.batch_id)
            history_runs.append({
                "name": c.name if c else "Unknown batch",
                "status": r.status,
                "taken": r.taken_so_far,
                "expected": r.expected_total,
                "missed": r.missed_frames,
                "local_exp_id": r.local_exp_id,
                "start": r.start_time,
                "end": r.end_time,
                "message": r.message
            })
            
        return {"runs": sorted(history_runs, key=lambda x: x["start"], reverse=True)}

@router.delete("/db/purge")
async def purge_all_history():
    """WIPES the Master Database of all experiments (Does not affect physical Pi storage)."""
    with Session(engine) as session:
        runs = session.exec(select(ExperimentRun)).all()
        for r in runs: session.delete(r)
        
        batchs = session.exec(select(ExperimentalBatch)).all()
        for c in batchs: session.delete(c)
        
        session.commit()
        return {"status": "success", "message": "Master Database history has been completely purged."}    

@router.delete("/db/experiments/{batch_id}")
async def delete_batch(batch_id: int):
    with Session(engine) as session:
        # 1. Find the batch
        batch = session.get(ExperimentalBatch, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Experimental batch not found.")
        
        # 2. Prevent Foreign Key Integrity Errors by deleting associated runs first
        runs = session.exec(select(ExperimentRun).where(ExperimentRun.batch_id == batch_id)).all()
        for r in runs:
            session.delete(r)
            
        # 3. Delete the master batch
        session.delete(batch)
        session.commit()
        
        return {"status": "success", "message": f"batch {batch_id} successfully deleted."}

class IdentityUpdateRequest(BaseModel):
    alias: str
    description: str

@router.put("/db/modules/{mac}/identity")
async def update_module_identity(mac: str, req: IdentityUpdateRequest):
    """Assigns a custom name and physical location description to a specific module."""
    with Session(engine) as session:
        mod = session.get(RobotModule, mac)
        if not mod:
            raise HTTPException(status_code=404, detail="Module not found in DB.")
            
        mod.alias = req.alias.strip() if req.alias.strip() else None
        mod.description = req.description.strip() if req.description.strip() else None
        
        session.add(mod)
        session.commit()
        return {"status": "success", "alias": mod.alias, "description": mod.description}


@router.delete("/db/modules/{mac}")
async def remove_module_from_fleet(mac: str):
    """Remove a module from this commander's registry. Experiment history is preserved."""
    norm_mac = normalize_mac(mac)
    with Session(engine) as session:
        mod = session.get(RobotModule, norm_mac) or session.get(RobotModule, mac)
        if not mod:
            raise HTTPException(status_code=404, detail="Module not found in DB.")
        session.delete(mod)
        session.commit()

    remove_module_from_memory(norm_mac)
    remove_module_from_memory(mac)
    return {"status": "success", "message": f"Module {norm_mac} removed from fleet registry."}


@router.post("/bulk/time_sync_manual")
async def sync_time_for_manual_nodes(req: BulkActionRequest):
    """Pushes Master time ONLY to nodes operating in Manual Time mode."""
    with Session(engine) as session:
        db_modules = session.exec(select(RobotModule).where(RobotModule.mac_address.in_(req.target_macs))).all()
        
    # Filter for nodes that are strictly NOT using NTP
    manual_macs = [m.mac_address for m in db_modules if not m.use_ntp]
    
    if not manual_macs:
        return {"status": "success", "message": "None of the selected nodes are in Manual mode. NTP handles them automatically."}

    master_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    base_payload = {"mode": "manual", "date": master_time_str}
    
    def get_time_payload(mac, state): return base_payload
    
    results = await broadcast_to_nodes(manual_macs, "POST", "config/time", get_payload_fn=get_time_payload)
    return {"status": "complete", "results": results, "message": f"Time synced to {len(manual_macs)} offline/manual modules."}

@router.post("/bulk/sync/test")
async def bulk_sync_test(req: BulkSyncTestRequest):
    """Broadcasts test credentials to modules without saving them."""
    payload = req.model_dump(exclude={"target_macs"})
    
    def get_test_payload(mac, state):
        return payload

    results = await broadcast_to_nodes(
        req.target_macs, 
        "POST", 
        "sync/test", 
        get_payload_fn=get_test_payload,
        timeout=15.0 
    )
    return {"status": "complete", "results": results}

@router.post("/bulk/sync/trigger")
async def bulk_sync_trigger(req: BulkActionRequest):
    """Forces an immediate one-shot background sync on the selected modules."""
    results = await broadcast_to_nodes(req.target_macs, "POST", "sync/trigger")
    return {"status": "complete", "results": results}

@router.post("/bulk/sync/cancel")
async def bulk_sync_cancel(req: BulkActionRequest):
    """Emergency abort for active sync operations."""
    results = await broadcast_to_nodes(req.target_macs, "POST", "sync/cancel")
    return {"status": "complete", "results": results}