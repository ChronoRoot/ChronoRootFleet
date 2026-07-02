from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Literal
from app.database import RobotModule

HealthClass = Literal["ok", "untested", "failed"]


def _first_present_value(source: Dict[str, Any], keys: list[str]) -> Optional[str]:
    for key in keys:
        value = source.get(key)
        if value not in (None, "", "Unknown", "None"):
            return value
    return None


def _dedupe_issues(issues: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            result.append(issue)
    return result


def classify_health(status: Optional[str]) -> HealthClass:
    if status == "OK":
        return "ok"
    if status == "UNTESTED":
        return "untested"
    return "failed"


def summarize_cameras(cam_reports: Dict[str, Any]) -> Dict[str, Any]:
    cams_ok = 0
    cams_untested = 0
    cams_failed = 0
    camera_issues: list[str] = []
    cameras_by_id: Dict[str, Dict[str, Any]] = {}

    for cam_id, cam in cam_reports.items():
        health = cam.get("health", "UNKNOWN")
        activity = cam.get("activity", "IDLE")
        last_check = cam.get("last_check", "N/A")
        cameras_by_id[str(cam_id)] = {
            "health": health,
            "activity": activity,
            "last_check": last_check,
        }

        if health == "OK":
            cams_ok += 1
        elif health == "UNTESTED":
            cams_untested += 1
            camera_issues.append(f"Camera {cam_id}: UNTESTED")
        else:
            cams_failed += 1
            camera_issues.append(f"Camera {cam_id}: {health}")

    cams_total = len(cam_reports)
    cameras_attention = cams_ok < cams_total

    return {
        "cams_ok": cams_ok,
        "cams_total": cams_total,
        "cams_untested": cams_untested,
        "cams_failed": cams_failed,
        "cameras_attention": cameras_attention,
        "camera_issues": camera_issues,
        "cameras_by_id": cameras_by_id,
    }


def summarize_lights(lights_info: Dict[str, Any]) -> Dict[str, Any]:
    health_check = lights_info.get("health_check", {})
    lights_health = health_check.get("status", "UNKNOWN")
    lights_state = lights_info.get("state", "UNKNOWN")
    return {
        "lights_health": lights_health,
        "lights_state": lights_state,
        "lights_attention": lights_health != "OK",
    }


def summarize_live_job(
    active_job: Dict[str, Any],
    local_exp_id: str,
    cam_reports: Dict[str, Any],
    camera_gaps: list,
    all_cameras_failed: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    progress = active_job.get("progress", {})
    last_capture = active_job.get("last_capture", {})

    experiment_gaps = [
        {"cam": gap.get("cam"), "behind_by": gap.get("behind_by")}
        for gap in camera_gaps
        if gap.get("expid") == local_exp_id
    ]

    per_camera_status = [
        {
            "id": str(cam_id),
            "health": cam.get("health", "UNKNOWN"),
            "activity": cam.get("activity", "IDLE"),
        }
        for cam_id, cam in sorted(cam_reports.items(), key=lambda item: int(item[0]))
    ]

    job_status = active_job.get("status", "")
    has_health_mismatch = any(cam.get("health") != "OK" for cam in cam_reports.values())
    has_camera_mismatch = bool(experiment_gaps) or (
        job_status == "RUNNING" and has_health_mismatch
    )

    job_all_failed = None
    if all_cameras_failed and all_cameras_failed.get("expid") == local_exp_id:
        job_all_failed = all_cameras_failed

    return {
        "last_capture_time": last_capture.get("time"),
        "last_capture_result": last_capture.get("result"),
        "rounds_taken": int(progress.get("taken", 0)),
        "rounds_expected": int(progress.get("expected", 0)),
        "rounds_expected_so_far": int(progress.get("expected_so_far", 0)),
        "experiment_gaps": experiment_gaps,
        "has_camera_mismatch": has_camera_mismatch,
        "per_camera_status": per_camera_status,
        "all_cameras_failed": job_all_failed,
    }


def compute_clock_drift_seconds(
    master_time: datetime,
    node_system_time_str: str,
    telemetry_age_seconds: Optional[int],
) -> Optional[int]:
    """Clock offset at last status poll, stable between UI refreshes."""
    if node_system_time_str == "Unknown" or telemetry_age_seconds is None:
        return None
    try:
        node_time = datetime.strptime(node_system_time_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    master_at_poll = master_time - timedelta(seconds=telemetry_age_seconds)
    return int(abs((master_at_poll - node_time).total_seconds()))


def digest_node_state(
    mac: str,
    raw: dict,
    db_mod: Optional[RobotModule],
    master_time_obj: datetime,
    telemetry_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    is_online = bool(raw)
    meta = telemetry_meta or {}
    telemetry_stale = is_online and not meta.get("is_fresh", True)
    telemetry_age_seconds = meta.get("telemetry_age_seconds")
    last_telemetry_at = meta.get("last_telemetry_at")

    ip = raw.get("identity", {}).get("ip", db_mod.ip_address if db_mod else "Unknown")
    hostname = raw.get("identity", {}).get("hostname", db_mod.hostname if db_mod else "Unknown")
    display_name = db_mod.alias if db_mod and db_mod.alias else hostname

    if db_mod:
        hardware_desc = (
            f"Multiplexer ({db_mod.camera_type})"
            if db_mod.selector_type in ["IVPORT", "TYPE_QUAD2"]
            else f"Single Cam ({db_mod.camera_type})"
        )
        time_mode = "NTP" if db_mod.use_ntp else "Manual"
    else:
        hardware_desc = "Unregistered Node"
        time_mode = "Unknown"

    stale_fields = {
        "telemetry_stale": False,
        "telemetry_age_seconds": None,
        "last_telemetry_at": None,
    }

    empty_job_detail: Dict[str, Any] = {
        "last_capture_time": None,
        "last_capture_result": None,
        "rounds_taken": 0,
        "rounds_expected": 0,
        "rounds_expected_so_far": 0,
        "experiment_gaps": [],
        "has_camera_mismatch": False,
        "per_camera_status": [],
        "all_cameras_failed": None,
    }

    if not is_online:
        return {
            "mac": mac,
            "hostname": display_name,
            "raw_hostname": hostname,
            "ip": ip,
            "is_online": False,
            "description": db_mod.description if db_mod else None,
            "uptime": "OFF",
            "primary_state": "OFFLINE",
            "needs_attention": False,
            "issues": [],
            "hardware_desc": hardware_desc,
            "last_seen": db_mod.last_seen.strftime("%Y-%m-%d %H:%M:%S") if db_mod and db_mod.last_seen else "Unknown",
            "storage_pct": 0,
            "lights_health": "UNKNOWN",
            "lights_state": "UNKNOWN",
            "cams_ok": 0,
            "cams_total": 0,
            "cams_untested": 0,
            "cams_failed": 0,
            "cameras_by_id": {},
            "camera_gaps": [],
            "all_cameras_failed": None,
            "last_diagnostic": None,
            "watchdog_limit_reached": False,
            "job_detail": empty_job_detail,
            "time_sync_status": "Offline",
            "time_mode": time_mode,
            "drift_seconds": "N/A",
            "system_time": "N/A",
            "is_locked": False,
            "lock_owner": "None",
            "has_job": False,
            "is_diagnosing": False,
            "sync_status": {
                "sync_enabled": False,
                "is_syncing": False,
                "status_msg": "Offline",
                "last_success": "Never",
                "last_error": None,
            },
            **stale_fields,
        }

    cam_reports = raw.get("cam_reports", {})
    camera_summary = summarize_cameras(cam_reports)
    lights_summary = summarize_lights(raw.get("lights_info", {}))
    lights_health = lights_summary["lights_health"]
    lights_state = lights_summary["lights_state"]

    camera_gaps = raw.get("camera_gaps", [])
    all_cameras_failed_raw = raw.get("all_cameras_failed")
    last_diagnostic_raw = raw.get("last_diagnostic") or {}
    last_diagnostic = (
        {
            "time": last_diagnostic_raw.get("time"),
            "global_result": last_diagnostic_raw.get("global_result"),
            "message": last_diagnostic_raw.get("message"),
        }
        if last_diagnostic_raw
        else None
    )
    watchdog_limit_reached = raw.get("watchdog", {}).get("limit_reached", False)

    alerts = raw.get("alerts", {})
    issues = list(alerts.get("issues", []))
    has_warnings = alerts.get("has_warnings", False)

    storage_pct = raw.get("system_health", {}).get("storage", {}).get("percent_used", 0)
    if storage_pct > 85.0:
        has_warnings = True
        issues.append(f"Storage {storage_pct}%")

    active_job = None
    local_exp_id = None

    if "system" in raw.get("jobs", {}):
        active_job = raw["jobs"]["system"]
        local_exp_id = "system"
    else:
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
        elif active_job.get("status") in ["RUNNING", "SCHEDULED"]:
            primary_state = "RUNNING"
        elif active_job.get("status") == "ERROR":
            primary_state = "ERROR"
            has_warnings = True
            issues.append("Experiment Error")

    if not is_diagnosing:
        if camera_summary["cameras_attention"]:
            has_warnings = True
            issues.extend(camera_summary["camera_issues"])
        if lights_summary["lights_attention"]:
            has_warnings = True
            issues.append(f"Lights: {lights_health}")

    issues = _dedupe_issues(issues)

    progress_pct = 0
    taken = int(active_job.get("progress", {}).get("taken", 0)) if active_job else 0
    expected = int(active_job.get("progress", {}).get("expected", 1)) if active_job else 1
    if expected > 0 and active_job:
        progress_pct = round((taken / expected) * 100)

    job_detail = empty_job_detail
    if active_job and not is_diagnosing:
        job_detail = summarize_live_job(
            active_job,
            local_exp_id or "",
            cam_reports,
            camera_gaps,
            all_cameras_failed_raw,
        )

    last_pic = raw.get("last_picture") or "Never"
    next_pic = raw.get("next_picture") or "None"

    system_time_str = raw.get("system_time", "Unknown")
    time_sync_status = "Unknown"
    drift_seconds_val = "N/A"

    drift = compute_clock_drift_seconds(
        master_time_obj, system_time_str, telemetry_age_seconds
    )
    if drift is not None:
        drift_seconds_val = drift
        sync_label = f"Synced ({time_mode})"
        desync_label = f"Drift Detected ({time_mode})"
        base_status = sync_label if drift < 45 else desync_label
        time_sync_status = (
            f"{base_status} (at last sync)" if telemetry_stale else base_status
        )

    raw_sync = raw.get("sync", {})
    sync_status = {
        "sync_enabled": raw_sync.get("sync_enabled", False),
        "is_syncing": raw_sync.get("is_syncing", False),
        "status_msg": raw_sync.get("status_msg", "Standby"),
        "last_success": raw_sync.get("last_success", "Never"),
        "last_error": raw_sync.get("last_error", None),
    }

    job_start = _first_present_value(
        active_job or {},
        ["start", "start_time", "started_at", "startAt", "from", "begin"],
    )
    job_end = _first_present_value(
        active_job or {},
        ["end", "end_time", "ended_at", "endAt", "to", "finish"],
    )

    return {
        "mac": mac,
        "hostname": display_name,
        "raw_hostname": hostname,
        "ip": ip,
        "is_online": True,
        "description": db_mod.description if db_mod else None,
        "primary_state": primary_state,
        "needs_attention": has_warnings and not telemetry_stale,
        "is_diagnosing": is_diagnosing,
        "issues": issues,
        "storage_pct": storage_pct,
        "lights_health": lights_health,
        "lights_state": lights_state,
        "cams_ok": camera_summary["cams_ok"],
        "cams_total": camera_summary["cams_total"],
        "cams_untested": camera_summary["cams_untested"],
        "cams_failed": camera_summary["cams_failed"],
        "cameras_by_id": camera_summary["cameras_by_id"],
        "camera_gaps": camera_gaps,
        "all_cameras_failed": all_cameras_failed_raw,
        "last_diagnostic": last_diagnostic,
        "watchdog_limit_reached": watchdog_limit_reached,
        "job_detail": job_detail,
        "hardware_desc": hardware_desc,
        "system_time": system_time_str,
        "time_sync_status": time_sync_status,
        "time_mode": time_mode,
        "drift_seconds": drift_seconds_val,
        "is_locked": raw.get("lock_info", {}).get("status") == "LOCKED",
        "lock_owner": raw.get("lock_info", {}).get("owner", "System"),
        "has_job": active_job is not None,
        "job_name": active_job.get("name") if active_job else None,
        "job_desc": active_job.get("desc") if active_job else None,
        "job_status": active_job.get("status") if active_job else None,
        "job_start": job_start,
        "job_end": job_end,
        "local_exp_id": local_exp_id,
        "progress_pct": progress_pct,
        "taken": taken,
        "expected": expected,
        "last_pic_time": last_pic if last_pic != "Never" else "Never",
        "next_pic_at": next_pic if next_pic != "None" else "None",
        "next_pic_time": next_pic[11:19] if next_pic != "None" else "None",
        "uptime": raw.get("uptime", "Unknown"),
        "sync_status": sync_status,
        "telemetry_stale": telemetry_stale,
        "telemetry_age_seconds": telemetry_age_seconds,
        "last_telemetry_at": last_telemetry_at,
    }
