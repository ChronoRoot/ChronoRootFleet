import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import httpx

# --- NETWORK CONFIGURATION (fleet_runtime.env, then env, then default) ---
_REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ENV_PATH = Path(
    os.environ.get("FLEET_RUNTIME_ENV", str(_REPO_ROOT / "fleet_runtime.env"))
)
_SUBNET_RE = re.compile(r"^(?:\d{1,3}\.){2}\d{1,3}$")


def _read_runtime_env_value(key: str) -> Optional[str]:
    try:
        with open(RUNTIME_ENV_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def resolve_target_subnet() -> str:
    """Prefer fleet_runtime.env (Network UI), then process env (installer), then 192.168.1."""
    file_val = _read_runtime_env_value("FLEET_TARGET_SUBNET")
    if file_val:
        return file_val
    env_val = (os.getenv("FLEET_TARGET_SUBNET") or "").strip()
    if env_val:
        return env_val
    return "192.168.1"


def has_persisted_subnet() -> bool:
    return bool(_read_runtime_env_value("FLEET_TARGET_SUBNET"))


def validate_subnet_base(subnet: str) -> str:
    subnet = (subnet or "").strip()
    if not _SUBNET_RE.match(subnet):
        raise ValueError("Subnet must look like A.B.C (e.g. 192.168.1 or 192.168.50).")
    parts = [int(p) for p in subnet.split(".")]
    if any(p < 0 or p > 255 for p in parts):
        raise ValueError("Each subnet octet must be between 0 and 255.")
    return subnet


def set_target_subnet(subnet: str) -> str:
    """Update in-memory discovery subnet (does not persist)."""
    global TARGET_SUBNET_BASE
    TARGET_SUBNET_BASE = validate_subnet_base(subnet)
    return TARGET_SUBNET_BASE


def persist_target_subnet(subnet: str) -> str:
    """Validate, apply in memory, and write FLEET_TARGET_SUBNET to fleet_runtime.env."""
    subnet = validate_subnet_base(subnet)
    lines = []
    replaced = False
    try:
        with open(RUNTIME_ENV_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("FLEET_TARGET_SUBNET="):
                    lines.append(f"FLEET_TARGET_SUBNET={subnet}\n")
                    replaced = True
                else:
                    lines.append(line)
    except OSError:
        lines = []

    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(f"FLEET_TARGET_SUBNET={subnet}\n")

    RUNTIME_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNTIME_ENV_PATH, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    return set_target_subnet(subnet)


TARGET_SUBNET_BASE = resolve_target_subnet()
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

# Manual Discover Nodes progress (polled by the dashboard while a sweep runs)
DISCOVERY_PROGRESS: Dict[str, Any] = {
    "running": False,
    "phase": "idle",  # idle|scanning|enriching|saving|done|error
    "subnet": "",
    "targets": 254,
    "responders": 0,
    "registered": 0,
    "detail": "",
    "started_at": None,
    "finished_at": None,
    "duration_seconds": None,
    "error": None,
}


def get_discovery_progress() -> Dict[str, Any]:
    return dict(DISCOVERY_PROGRESS)


def begin_discovery(subnet: str, targets: int = 254) -> None:
    if DISCOVERY_PROGRESS.get("running"):
        raise RuntimeError("Discovery is already running. Please wait for it to finish.")
    now = datetime.utcnow().isoformat() + "Z"
    DISCOVERY_PROGRESS.update(
        {
            "running": True,
            "phase": "scanning",
            "subnet": subnet,
            "targets": targets,
            "responders": 0,
            "registered": 0,
            "detail": f"Scanning {subnet}.1–.254 for ChronoRoot modules…",
            "started_at": now,
            "finished_at": None,
            "duration_seconds": None,
            "error": None,
        }
    )


def update_discovery(**kwargs: Any) -> None:
    for key, value in kwargs.items():
        if key in DISCOVERY_PROGRESS:
            DISCOVERY_PROGRESS[key] = value


def finish_discovery(
    *,
    registered: int,
    duration_seconds: float,
    error: Optional[str] = None,
) -> None:
    now = datetime.utcnow().isoformat() + "Z"
    subnet = DISCOVERY_PROGRESS.get("subnet") or TARGET_SUBNET_BASE
    if error:
        DISCOVERY_PROGRESS.update(
            {
                "running": False,
                "phase": "error",
                "registered": registered,
                "duration_seconds": round(duration_seconds, 1),
                "finished_at": now,
                "error": error,
                "detail": error,
            }
        )
        return

    DISCOVERY_PROGRESS.update(
        {
            "running": False,
            "phase": "done",
            "registered": registered,
            "responders": max(DISCOVERY_PROGRESS.get("responders") or 0, registered),
            "duration_seconds": round(duration_seconds, 1),
            "finished_at": now,
            "error": None,
            "detail": (
                f"Discovery complete on {subnet}.x "
                f"({duration_seconds:.1f}s). Found {registered} modules; "
                "config and history refreshed."
            ),
        }
    )


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
