import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

import httpx

# --- NETWORK CONFIGURATION (env-overridable) ---
TARGET_SUBNET_BASE = os.getenv("FLEET_TARGET_SUBNET", "192.168.1")
FAST_POLL_INTERVAL = int(os.getenv("FLEET_FAST_POLL_INTERVAL", "15"))
FAST_POLL_INTERVAL_LARGE = int(os.getenv("FLEET_FAST_POLL_INTERVAL_LARGE", "30"))
LARGE_FLEET_THRESHOLD = int(os.getenv("FLEET_LARGE_FLEET_THRESHOLD", "10"))

OFFLINE_GRACE_POLLS = int(os.getenv("FLEET_OFFLINE_GRACE_POLLS", "4"))
STALE_AFTER_MISSES = int(os.getenv("FLEET_STALE_AFTER_MISSES", "2"))
MAX_FETCH_RETRIES = int(os.getenv("FLEET_MAX_FETCH_RETRIES", "1"))
PROXY_DB_FALLBACK_MINUTES = int(os.getenv("FLEET_PROXY_DB_FALLBACK_MINUTES", "5"))

CONNECT_TIMEOUT = float(os.getenv("FLEET_CONNECT_TIMEOUT", "3.0"))
READ_TIMEOUT = float(os.getenv("FLEET_READ_TIMEOUT", "15.0"))
POLL_TASK_STAGGER = float(os.getenv("FLEET_POLL_TASK_STAGGER", "0"))

USE_LIVENESS_PROBE = os.getenv("FLEET_USE_LIVENESS_PROBE", "").lower() in ("1", "true", "yes")
DISCOVERY_MAX_CONCURRENT = int(os.getenv("FLEET_DISCOVERY_MAX_CONCURRENT", "32"))

# Deprecated aliases (kept for backward compatibility)
POLL_BATCH_SIZE = int(os.getenv("FLEET_POLL_BATCH_SIZE", "6"))
POLL_BATCH_DELAY = float(os.getenv("FLEET_POLL_BATCH_DELAY", "0.3"))

_env_concurrent = os.getenv("FLEET_MAX_CONCURRENT_POLLS")
if _env_concurrent is not None:
    MAX_CONCURRENT_POLLS = int(_env_concurrent)
elif os.getenv("FLEET_POLL_BATCH_SIZE") is not None:
    MAX_CONCURRENT_POLLS = int(os.getenv("FLEET_POLL_BATCH_SIZE", "6"))
else:
    MAX_CONCURRENT_POLLS = 12

# --- SHARED RAM-DISK ---
LIVE_FLEET_STATE: Dict[str, Any] = {}

# Per-MAC presence: {"payload": dict, "miss_count": int, "last_success": datetime}
# State machine: fresh (miss < STALE_AFTER_MISSES) → stale UI (miss >= STALE_AFTER_MISSES)
# → offline evicted (miss >= OFFLINE_GRACE_POLLS)
MODULE_PRESENCE: Dict[str, Dict[str, Any]] = {}

HTTP_CLIENT: Optional[Any] = None

POLL_DIAGNOSTICS: Dict[str, Any] = {
    "last_cycle_started_at": None,
    "last_cycle_completed_at": None,
    "last_cycle_duration_seconds": None,
    "modules_polled": 0,
    "modules_responded": 0,
    "errors": {},
    "failed_ips": [],
    "config": {},
}


def poll_http_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=READ_TIMEOUT,
        write=5.0,
        pool=5.0,
    )


def probe_http_timeout() -> httpx.Timeout:
    """Short timeout for optional liveness pre-check before full status fetch."""
    return httpx.Timeout(connect=2.0, read=3.0, write=5.0, pool=5.0)


def discover_http_timeout() -> httpx.Timeout:
    """Timeouts for manual subnet discovery sweeps."""
    return httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=5.0)


def effective_max_concurrent(module_count: int) -> int:
    if module_count <= 0:
        return MAX_CONCURRENT_POLLS
    return min(module_count, MAX_CONCURRENT_POLLS)


def get_poll_config_snapshot(poll_interval_seconds: Optional[int] = None) -> Dict[str, Any]:
    return {
        "max_concurrent_polls": MAX_CONCURRENT_POLLS,
        "connect_timeout": CONNECT_TIMEOUT,
        "read_timeout": READ_TIMEOUT,
        "max_retries": MAX_FETCH_RETRIES,
        "poll_interval_seconds": poll_interval_seconds,
        "offline_grace_polls": OFFLINE_GRACE_POLLS,
        "stale_after_misses": STALE_AFTER_MISSES,
        "poll_task_stagger": POLL_TASK_STAGGER,
        "use_liveness_probe": USE_LIVENESS_PROBE,
    }


def update_poll_diagnostics(
    *,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    duration_seconds: Optional[float] = None,
    modules_polled: int = 0,
    modules_responded: int = 0,
    errors: Optional[Dict[str, int]] = None,
    failed_ips: Optional[list] = None,
    poll_interval_seconds: Optional[int] = None,
) -> None:
    if started_at is not None:
        POLL_DIAGNOSTICS["last_cycle_started_at"] = started_at.strftime("%Y-%m-%d %H:%M:%S")
    if completed_at is not None:
        POLL_DIAGNOSTICS["last_cycle_completed_at"] = completed_at.strftime("%Y-%m-%d %H:%M:%S")
    if duration_seconds is not None:
        POLL_DIAGNOSTICS["last_cycle_duration_seconds"] = round(duration_seconds, 2)
    POLL_DIAGNOSTICS["modules_polled"] = modules_polled
    POLL_DIAGNOSTICS["modules_responded"] = modules_responded
    POLL_DIAGNOSTICS["errors"] = errors or {}
    POLL_DIAGNOSTICS["failed_ips"] = failed_ips or []
    POLL_DIAGNOSTICS["config"] = get_poll_config_snapshot(poll_interval_seconds)


def get_poll_diagnostics() -> Dict[str, Any]:
    return dict(POLL_DIAGNOSTICS)


def normalize_mac(mac: str) -> str:
    return mac.strip().upper()


def effective_poll_interval(known_module_count: int) -> int:
    if known_module_count > LARGE_FLEET_THRESHOLD:
        return FAST_POLL_INTERVAL_LARGE
    return FAST_POLL_INTERVAL


def record_poll_success(mac: str, payload: Dict[str, Any]) -> None:
    mac = normalize_mac(mac)
    MODULE_PRESENCE[mac] = {
        "payload": payload,
        "miss_count": 0,
        "last_success": datetime.utcnow(),
    }
    LIVE_FLEET_STATE[mac] = payload


def record_poll_miss(mac: str) -> None:
    mac = normalize_mac(mac)
    entry = MODULE_PRESENCE.get(mac)
    if not entry and mac in LIVE_FLEET_STATE:
        entry = {
            "payload": LIVE_FLEET_STATE[mac],
            "miss_count": 0,
            "last_success": None,
        }
        MODULE_PRESENCE[mac] = entry

    if entry:
        entry["miss_count"] = entry.get("miss_count", 0) + 1
        if entry["miss_count"] >= OFFLINE_GRACE_POLLS:
            MODULE_PRESENCE.pop(mac, None)
            LIVE_FLEET_STATE.pop(mac, None)


def apply_poll_results(
    polled_ips: Dict[str, Optional[Dict[str, Any]]],
    ip_to_mac: Dict[str, str],
) -> None:
    """Update presence from a completed poll cycle. polled_ips maps ip -> payload or None."""
    responded_ips: set[str] = set()

    for ip, payload in polled_ips.items():
        if payload and "identity" in payload:
            responded_ips.add(ip)
            mac = normalize_mac(payload["identity"]["mac"])
            record_poll_success(mac, payload)

    for ip, mac in ip_to_mac.items():
        if ip not in responded_ips:
            record_poll_miss(normalize_mac(mac))


def get_telemetry_meta(mac: str) -> Dict[str, Any]:
    mac = normalize_mac(mac)
    entry = MODULE_PRESENCE.get(mac)

    if not entry and mac in LIVE_FLEET_STATE:
        entry = {
            "payload": LIVE_FLEET_STATE[mac],
            "miss_count": 0,
            "last_success": None,
        }
        MODULE_PRESENCE[mac] = entry

    if not entry:
        return {
            "miss_count": 0,
            "last_success": None,
            "is_fresh": False,
            "telemetry_age_seconds": None,
            "last_telemetry_at": None,
        }

    miss_count = entry.get("miss_count", 0)
    last_success = entry.get("last_success")
    is_fresh = miss_count < STALE_AFTER_MISSES
    age_seconds = None
    last_at = None
    if last_success:
        age_seconds = int((datetime.utcnow() - last_success).total_seconds())
        last_at = last_success.strftime("%Y-%m-%d %H:%M:%S")

    return {
        "miss_count": miss_count,
        "last_success": last_success,
        "is_fresh": is_fresh,
        "telemetry_age_seconds": age_seconds,
        "last_telemetry_at": last_at,
    }


def remove_module_from_memory(mac: str) -> None:
    mac = normalize_mac(mac)
    LIVE_FLEET_STATE.pop(mac, None)
    MODULE_PRESENCE.pop(mac, None)


def get_presence_miss_count(mac: str) -> int:
    entry = MODULE_PRESENCE.get(normalize_mac(mac))
    return entry.get("miss_count", 0) if entry else OFFLINE_GRACE_POLLS


def count_presence_grace_modules() -> int:
    return sum(
        1
        for entry in MODULE_PRESENCE.values()
        if entry.get("miss_count", 0) >= STALE_AFTER_MISSES
    )


def resolve_proxy_target(
    mac: str,
    db_ip: Optional[str] = None,
    db_last_seen: Optional[datetime] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve IP for proxy routing.
    Returns (ip, warning_message) or (None, None) if unreachable.
    """
    mac = normalize_mac(mac)
    if mac in LIVE_FLEET_STATE:
        return LIVE_FLEET_STATE[mac]["identity"]["ip"], None

    entry = MODULE_PRESENCE.get(mac)
    if entry and entry.get("miss_count", 0) > 0:
        payload = entry.get("payload", {})
        ip = payload.get("identity", {}).get("ip")
        if ip:
            return ip, "Module in grace period — telemetry may be stale."

    if db_ip and db_last_seen:
        cutoff = datetime.utcnow() - timedelta(minutes=PROXY_DB_FALLBACK_MINUTES)
        if db_last_seen >= cutoff:
            return db_ip, "Module recently seen — using last known IP."

    return None, None
