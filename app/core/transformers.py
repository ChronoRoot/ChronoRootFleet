from datetime import datetime
from typing import Dict, Any, Optional
from app.database import RobotModule

def digest_node_state(mac: str, raw: dict, db_mod: Optional[RobotModule], master_time_obj: datetime) -> Dict[str, Any]:
    is_online = bool(raw)
    
    # 1. Identity (Alias Priority)
    ip = raw.get("identity", {}).get("ip", db_mod.ip_address if db_mod else "Unknown")
    hostname = raw.get("identity", {}).get("hostname", db_mod.hostname if db_mod else "Unknown")
    display_name = db_mod.alias if db_mod and db_mod.alias else hostname

    # 2. Hardware (Preserved even when offline!)
    if db_mod:
        hardware_desc = f"Multiplexer ({db_mod.camera_type})" if db_mod.selector_type in ["IVPORT", "TYPE_QUAD2"] else f"Single Cam ({db_mod.camera_type})"
        time_mode = "NTP" if db_mod.use_ntp else "Manual"
    else:
        hardware_desc = "Unregistered Node"
        time_mode = "Unknown"

    # --- OFFLINE FAST RETURN (Now preserves config data) ---
    if not is_online:
        return {
            "mac": mac, "hostname": display_name, "raw_hostname": hostname, "ip": ip, "is_online": False,
            "primary_state": "OFFLINE", "needs_attention": True,
            "issues": ["Node Offline"], "hardware_desc": hardware_desc, # Preserved!
            "last_seen": db_mod.last_seen.strftime("%Y-%m-%d %H:%M:%S") if db_mod and db_mod.last_seen else "Unknown",
            "storage_pct": 0, "ir_status": "UNKNOWN", "cams_ok": 0, "cams_total": 0,
            "time_sync_status": "Offline", "time_mode": time_mode, "drift_seconds": "N/A", "system_time": "N/A",
            "is_locked": False, "lock_owner": "None", "has_job": False, "is_diagnosing": False
        }

    # --- ONLINE LOGIC ---
    alerts = raw.get("alerts", {})
    issues = list(alerts.get("issues", []))
    has_warnings = alerts.get("has_warnings", False)

    storage_pct = raw.get("system_health", {}).get("storage", {}).get("percent_used", 0)
    if storage_pct > 85.0:
        has_warnings = True
        issues.append(f"Storage {storage_pct}%")

    ir_status = raw.get("lights_info", {}).get("health_check", {}).get("status", "UNKNOWN")
    if ir_status == "NOT DETECTED":
        has_warnings = True
        issues.append("IR Failure")

    active_job = None
    local_exp_id = None
    
    # 1. Explicitly check for the diagnostic override first
    if "system" in raw.get("jobs", {}):
        active_job = raw["jobs"]["system"]
        local_exp_id = "system"
    else:
        # 2. Otherwise, look for standard biological experiments
        for jid, job in raw.get("jobs", {}).items():
            if job.get("status") in ["RUNNING", "SCHEDULED", "ERROR"]:
                active_job = job
                local_exp_id = jid
                break

    primary_state = "IDLE"
    is_diagnosing = False
    
    if active_job:
        if local_exp_id == "system" or active_job.get("name") == "System Diagnostic":
            primary_state = "DIAGNOSING"
            is_diagnosing = True
        elif active_job.get("status") == "RUNNING":
            primary_state = "RUNNING"
        elif active_job.get("status") == "SCHEDULED":
            primary_state = "WAITING"
        elif active_job.get("status") == "ERROR":
            primary_state = "ERROR"
            has_warnings = True
            issues.append("Experiment Error")

    progress_pct = 0
    taken = int(active_job.get("progress", {}).get("taken", 0)) if active_job else 0
    expected = int(active_job.get("progress", {}).get("expected", 1)) if active_job else 1
    if expected > 0 and active_job:
        progress_pct = round((taken / expected) * 100)

    last_pic = raw.get("last_picture") or "Never"
    next_pic = raw.get("next_picture") or "None"
        
    # 6. Smart Time Sync Math
    system_time_str = raw.get("system_time", "Unknown")
    time_sync_status = "Unknown"
    drift_seconds_val = "N/A"
    
    if system_time_str != "Unknown":
        try:
            node_time_obj = datetime.strptime(system_time_str, "%Y-%m-%d %H:%M:%S")
            drift = abs((master_time_obj - node_time_obj).total_seconds())
            drift_seconds_val = int(drift) 
            
            # Label contextually based on their config
            sync_label = f"Synced ({time_mode})"
            desync_label = f"Drift Detected ({time_mode})"
            
            time_sync_status = sync_label if drift < 45 else desync_label
        except Exception as e:
            time_sync_status = "Error"
            drift_seconds_val = f"Err: {str(e)}"

    return {
        "mac": mac, "hostname": display_name, "raw_hostname": hostname, "ip": ip, "is_online": True,
        "primary_state": primary_state, "needs_attention": has_warnings, "is_diagnosing": is_diagnosing,
        "issues": issues, "storage_pct": storage_pct, "ir_status": ir_status,
        "cams_ok": sum(1 for cam in raw.get("cam_reports", {}).values() if cam.get("health") == "OK"),
        "cams_total": len(raw.get("cam_reports", {})),
        "hardware_desc": hardware_desc, "system_time": system_time_str,
        "time_sync_status": time_sync_status, "time_mode": time_mode, "drift_seconds": drift_seconds_val,
        "is_locked": raw.get("lock_info", {}).get("status") == "LOCKED",
        "lock_owner": raw.get("lock_info", {}).get("owner", "System"),
        "has_job": active_job is not None, "job_name": active_job.get("name") if active_job else None,
        "job_desc": active_job.get("desc") if active_job else None,
        "job_status": active_job.get("status") if active_job else None,
        "local_exp_id": local_exp_id, "progress_pct": progress_pct,
        "taken": taken, "expected": expected,
        "last_pic_time": last_pic[11:19] if last_pic != "Never" else "Never",
        "next_pic_time": next_pic[11:19] if next_pic != "None" else "None"
    }