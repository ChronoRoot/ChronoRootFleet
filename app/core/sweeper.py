import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import httpx
from sqlmodel import Session, select

from app.core.state import (
    TARGET_SUBNET_BASE,
    MAX_FETCH_RETRIES,
    POLL_TASK_STAGGER,
    USE_LIVENESS_PROBE,
    DISCOVERY_MAX_CONCURRENT,
    effective_poll_interval,
    effective_max_concurrent,
    first_usable_ip,
    is_usable_module_ip,
    apply_poll_results,
    record_poll_success,
    normalize_mac,
    poll_http_timeout,
    probe_http_timeout,
    discover_http_timeout,
    update_poll_diagnostics,
    HTTP_CLIENT,
    begin_discovery,
    update_discovery,
    finish_discovery,
    get_discovery_progress,
)
from app.core import state as fleet_state
from app.database import engine, RobotModule, ExperimentRun, find_robot_module

logger = logging.getLogger(__name__)

RETRY_BACKOFF = (0.2, 0.5)


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "ConnectError"
    if isinstance(exc, httpx.ReadTimeout):
        return "ReadTimeout"
    if isinstance(exc, httpx.WriteTimeout):
        return "WriteTimeout"
    if isinstance(exc, httpx.PoolTimeout):
        return "PoolTimeout"
    if isinstance(exc, httpx.TimeoutException):
        return "Timeout"
    if isinstance(exc, httpx.NetworkError):
        return "NetworkError"
    return type(exc).__name__


async def fetch_endpoint(
    client: httpx.AsyncClient,
    ip: str,
    endpoint: str,
    *,
    timeout: Optional[httpx.Timeout] = None,
) -> Tuple[str, Any, Optional[str]]:
    """Returns (ip, payload_or_none, error_type_or_none)."""
    url = f"http://{ip}/api/{endpoint}"
    req_timeout = timeout or poll_http_timeout()
    last_exc: Optional[Exception] = None

    for attempt in range(MAX_FETCH_RETRIES + 1):
        try:
            res = await client.get(url, timeout=req_timeout)
            if res.status_code == 200:
                return ip, res.json(), None
            logger.debug("Poll %s returned HTTP %s", url, res.status_code)
            return ip, None, "HTTPError"
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
            last_exc = e
            if attempt < MAX_FETCH_RETRIES:
                delay = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                delay += random.uniform(0, 0.1)
                await asyncio.sleep(delay)
        except Exception as e:
            logger.warning("Poll failed for %s: %s", url, type(e).__name__)
            return ip, None, type(e).__name__

    if last_exc:
        err_type = _classify_error(last_exc)
        logger.warning("Poll failed for %s after retries: %s", url, err_type)
        return ip, None, err_type
    return ip, None, "Unknown"


@dataclass
class PollResult:
    ip: str
    payload: Optional[Dict[str, Any]]
    error: Optional[str]


@dataclass
class PollCycleStats:
    modules_polled: int
    modules_responded: int
    duration_seconds: float
    errors: Dict[str, int]
    failed_ips: List[str]
    poll_interval_seconds: int


def _build_poll_targets(
    known_modules: List[RobotModule],
) -> Tuple[List[str], Dict[str, str]]:
    """Deduplicate IPs; first MAC per IP wins."""
    unique_ips: List[str] = []
    ip_to_db_mac: Dict[str, str] = {}
    seen_ips: Dict[str, str] = {}
    skipped_unusable = 0

    for m in known_modules:
        if not m.ip_address:
            continue
        ip = m.ip_address.strip()
        if not is_usable_module_ip(ip):
            skipped_unusable += 1
            continue
        mac = normalize_mac(m.mac_address)
        if ip in seen_ips and seen_ips[ip] != mac:
            logger.warning(
                "Duplicate IP %s in DB for MACs %s and %s — polling once",
                ip,
                seen_ips[ip],
                mac,
            )
            continue
        if ip not in seen_ips:
            seen_ips[ip] = mac
            unique_ips.append(ip)
            ip_to_db_mac[ip] = mac

    if skipped_unusable:
        logger.warning(
            "Skipping %d module(s) whose stored address is not usable "
            "(loopback or otherwise not a LAN address). "
            "Run Discover Nodes to record the address that answers.",
            skipped_unusable,
        )

    return unique_ips, ip_to_db_mac


def stamp_reachable_ip(
    payload: Dict[str, Any],
    contacted_ip: str,
) -> Optional[Dict[str, Any]]:
    """Keep the address that answered. Never publish 127.0.0.1 as the module IP."""
    identity = payload.get("identity") or {}
    reported = identity.get("ip")
    chosen = first_usable_ip(contacted_ip, reported)
    if not chosen:
        return None
    if isinstance(reported, str) and reported.strip() == chosen:
        return payload
    if not is_usable_module_ip(reported):
        logger.warning(
            "Module %s at %s reported unusable address %r",
            identity.get("mac"),
            chosen,
            reported,
        )
    updated = dict(payload)
    updated["identity"] = dict(identity)
    updated["identity"]["ip"] = chosen
    return updated


def _sanitize_poll_result(result: PollResult) -> PollResult:
    if not result.payload or "identity" not in result.payload:
        return result
    stamped = stamp_reachable_ip(result.payload, result.ip)
    if stamped is None:
        return PollResult(ip=result.ip, payload=None, error="UnusableIP")
    return PollResult(ip=result.ip, payload=stamped, error=None)


async def _poll_one_module(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    ip: str,
) -> PollResult:
    async with sem:
        if USE_LIVENESS_PROBE:
            ip, payload, err = await fetch_endpoint(
                client, ip, "status", timeout=probe_http_timeout()
            )
            if payload and "identity" in payload:
                return PollResult(ip=ip, payload=payload, error=None)
            return PollResult(ip=ip, payload=None, error=err)

        ip, payload, err = await fetch_endpoint(client, ip, "status")
        if payload and "identity" in payload:
            return PollResult(ip=ip, payload=payload, error=None)
        return PollResult(ip=ip, payload=None, error=err)


async def _fetch_all_modules(
    client: httpx.AsyncClient,
    ip_to_db_mac: Dict[str, str],
) -> List[PollResult]:
    concurrent = effective_max_concurrent(len(ip_to_db_mac))
    sem = asyncio.Semaphore(concurrent)
    tasks: List[asyncio.Task] = []

    for ip in ip_to_db_mac:
        tasks.append(asyncio.create_task(_poll_one_module(client, sem, ip)))
        if POLL_TASK_STAGGER > 0:
            await asyncio.sleep(POLL_TASK_STAGGER)

    raw = await asyncio.gather(*tasks, return_exceptions=True)
    results: List[PollResult] = []
    for item in raw:
        if isinstance(item, Exception):
            logger.exception("Unexpected poll task failure: %s", item)
            continue
        results.append(item)
    return results


def _results_to_polled_ips(
    results: List[PollResult],
    ip_to_db_mac: Dict[str, str],
) -> Dict[str, Optional[Dict[str, Any]]]:
    polled: Dict[str, Optional[Dict[str, Any]]] = {ip: None for ip in ip_to_db_mac}
    for r in results:
        if r.payload and "identity" in r.payload:
            polled[r.ip] = r.payload
    return polled


def _summarize_poll_results(results: List[PollResult]) -> Tuple[int, Dict[str, int], List[str]]:
    responded = 0
    errors: Dict[str, int] = {}
    failed_ips: List[str] = []

    for r in results:
        if r.payload and "identity" in r.payload:
            responded += 1
        elif r.error:
            errors[r.error] = errors.get(r.error, 0) + 1
            failed_ips.append(r.ip)
        else:
            errors["Unknown"] = errors.get("Unknown", 0) + 1
            failed_ips.append(r.ip)

    return responded, errors, failed_ips


def _merge_history_into_db(session: Session, history_map: Dict[str, Any]) -> None:
    for ip, history_data in history_map.items():
        for local_exp_id, job_data in history_data.items():
            run = session.exec(
                select(ExperimentRun).where(ExperimentRun.local_exp_id == local_exp_id)
            ).first()
            if not run:
                continue
            safe_expected = int(job_data.get("expected_pictures") or run.expected_total)
            safe_taken = int(job_data.get("taken_pictures") or run.taken_so_far)
            run.expected_total = safe_expected
            run.taken_so_far = safe_taken
            run.missed_frames = max(0, safe_expected - safe_taken)
            run.status = job_data.get("status", "FINISHED")
            if job_data.get("start"):
                run.start_time = job_data.get("start")
            if job_data.get("end"):
                run.end_time = job_data.get("end")
            if job_data.get("message"):
                run.message = job_data.get("message")
            session.add(run)


def _sync_payload_to_db(session: Session, payload: Dict[str, Any]) -> None:
    mac = normalize_mac(payload["identity"]["mac"])
    reported_ip = payload["identity"].get("ip")
    reachable_ip = reported_ip.strip() if is_usable_module_ip(reported_ip) else None

    mod = find_robot_module(session, mac)
    db_mac = mod.mac_address if mod else mac

    if mod:
        mod.last_seen = datetime.utcnow()
        if reachable_ip:
            mod.ip_address = reachable_ip
        else:
            logger.warning(
                "Refusing to store unusable address %r for %s",
                reported_ip,
                mac,
            )
            if is_usable_module_ip(mod.ip_address):
                reachable_ip = mod.ip_address.strip()
        session.add(mod)

    active_local_ids = list(payload.get("jobs", {}).keys())

    for local_exp_id, job_data in payload.get("jobs", {}).items():
        run_record = session.exec(
            select(ExperimentRun)
            .where(ExperimentRun.module_mac == db_mac)
            .where(ExperimentRun.local_exp_id == local_exp_id)
        ).first()

        safe_taken = int(job_data.get("progress", {}).get("taken") or 0)

        if run_record:
            pi_expected = job_data.get("progress", {}).get("expected")
            if pi_expected:
                run_record.expected_total = int(pi_expected)
            run_record.status = job_data.get("status", "RUNNING")
            run_record.taken_so_far = safe_taken
            run_record.missed_frames = max(
                0,
                int(job_data.get("progress", {}).get("expected_so_far") or 0) - safe_taken,
            )
            session.add(run_record)

    active_db_runs = session.exec(
        select(ExperimentRun)
        .where(ExperimentRun.module_mac == db_mac)
        .where(ExperimentRun.status.in_(["RUNNING", "SCHEDULED"]))
    ).all()

    for db_run in active_db_runs:
        if db_run.local_exp_id not in active_local_ids:
            db_run.status = "FINISHED"
            db_run.taken_so_far = db_run.expected_total
            session.add(db_run)
            if reachable_ip:
                asyncio.create_task(trigger_targeted_history_sync(db_mac, reachable_ip))


async def run_fast_poll_cycle() -> PollCycleStats:
    """Load targets, poll all modules, batch-update presence, sync DB."""
    cycle_started = datetime.utcnow()
    t0 = time.monotonic()

    with Session(engine) as session:
        known_modules = list(session.exec(select(RobotModule)).all())

    unique_ips, ip_to_db_mac = _build_poll_targets(known_modules)
    poll_interval = effective_poll_interval(len(unique_ips))
    concurrent = effective_max_concurrent(len(unique_ips))

    update_poll_diagnostics(
        started_at=cycle_started,
        modules_polled=len(unique_ips),
        poll_interval_seconds=poll_interval,
    )

    if not unique_ips:
        duration = time.monotonic() - t0
        update_poll_diagnostics(
            completed_at=datetime.utcnow(),
            duration_seconds=duration,
            modules_polled=0,
            modules_responded=0,
            poll_interval_seconds=poll_interval,
        )
        return PollCycleStats(0, 0, duration, {}, [], poll_interval)

    limits = httpx.Limits(
        max_connections=concurrent + 4,
        max_keepalive_connections=concurrent,
    )
    async with httpx.AsyncClient(limits=limits, timeout=poll_http_timeout()) as client:
        results = await _fetch_all_modules(client, ip_to_db_mac)
    results = [_sanitize_poll_result(r) for r in results]

    polled_ips = _results_to_polled_ips(results, ip_to_db_mac)
    apply_poll_results(polled_ips, ip_to_db_mac)

    responded, errors, failed_ips = _summarize_poll_results(results)
    duration = time.monotonic() - t0

    update_poll_diagnostics(
        completed_at=datetime.utcnow(),
        duration_seconds=duration,
        modules_polled=len(unique_ips),
        modules_responded=responded,
        errors=errors,
        failed_ips=failed_ips,
        poll_interval_seconds=poll_interval,
    )

    logger.info(
        "Fast poll cycle: %d/%d modules responded (max %d concurrent, %.1fs)",
        responded,
        len(unique_ips),
        concurrent,
        duration,
    )

    active_payloads = [
        r.payload for r in results if r.payload and "identity" in r.payload
    ]
    with Session(engine) as session:
        for payload in active_payloads:
            _sync_payload_to_db(session, payload)
        session.commit()

    return PollCycleStats(
        modules_polled=len(unique_ips),
        modules_responded=responded,
        duration_seconds=duration,
        errors=errors,
        failed_ips=failed_ips,
        poll_interval_seconds=poll_interval,
    )


# ==========================================
# ENGINE 1: FAST POLL (Live Monitoring)
# ==========================================
async def fast_monitor_loop():
    logger.info("Fast monitor loop started")
    while True:
        try:
            stats = await run_fast_poll_cycle()
            elapsed = stats.duration_seconds
            sleep_for = max(1.0, stats.poll_interval_seconds - elapsed)
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Fast monitor cycle failed")
            await asyncio.sleep(effective_poll_interval(0))


async def trigger_targeted_history_sync(mac: str, ip: str):
    discover_timeout = discover_http_timeout()
    if HTTP_CLIENT is None:
        async with httpx.AsyncClient(timeout=discover_timeout) as tmp:
            _, history_data, _ = await fetch_endpoint(tmp, ip, "history", timeout=discover_timeout)
    else:
        _, history_data, _ = await fetch_endpoint(
            HTTP_CLIENT, ip, "history", timeout=discover_timeout
        )

    if not history_data:
        return

    with Session(engine) as session:
        for local_exp_id, job_data in history_data.items():
            run = session.exec(
                select(ExperimentRun).where(ExperimentRun.local_exp_id == local_exp_id)
            ).first()
            if run:
                safe_expected = int(job_data.get("expected_pictures") or run.expected_total)
                safe_taken = int(job_data.get("taken_pictures") or run.taken_so_far)
                run.expected_total = safe_expected
                run.taken_so_far = safe_taken
                run.missed_frames = max(0, safe_expected - safe_taken)
                run.status = job_data.get("status", "FINISHED")
                if job_data.get("start"):
                    run.start_time = job_data.get("start")
                if job_data.get("end"):
                    run.end_time = job_data.get("end")
                if job_data.get("message"):
                    run.message = job_data.get("message")
                session.add(run)
        session.commit()


async def _bounded_fetch(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    ip: str,
    endpoint: str,
    timeout: httpx.Timeout,
) -> Tuple[str, Any, Optional[str]]:
    async with sem:
        return await fetch_endpoint(client, ip, endpoint, timeout=timeout)


# ==========================================
# ENGINE 2: MANUAL DISCOVERY (full subnet sweep)
# ==========================================

async def execute_discovery_sweep() -> Dict[str, Any]:
    """
    Full subnet sweep. Returns ``{count, subnet, duration_seconds, message}``.
    Raises ``RuntimeError`` if a discovery is already running.
    """
    progress = get_discovery_progress()
    if progress.get("running"):
        raise RuntimeError("Discovery is already running. Please wait for it to finish.")

    subnet = fleet_state.TARGET_SUBNET_BASE
    t0 = time.monotonic()
    ips = [f"{subnet}.{i}" for i in range(1, 255)]
    begin_discovery(subnet, targets=len(ips))

    try:
        discover_timeout = discover_http_timeout()
        sem = asyncio.Semaphore(DISCOVERY_MAX_CONCURRENT)
        limits = httpx.Limits(
            max_connections=DISCOVERY_MAX_CONCURRENT + 4,
            max_keepalive_connections=DISCOVERY_MAX_CONCURRENT,
        )

        async with httpx.AsyncClient(limits=limits, timeout=discover_timeout) as client:
            raw = await asyncio.gather(
                *[_bounded_fetch(client, sem, ip, "status", discover_timeout) for ip in ips]
            )
        results = [(ip, data) for ip, data, _ in raw]
        active_payloads = []
        for scan_ip, data in results:
            if not data or "identity" not in data:
                continue
            stamped = stamp_reachable_ip(data, scan_ip)
            if stamped is None:
                logger.warning(
                    "Discovery skipping %s: no usable address (reported %r)",
                    scan_ip,
                    (data.get("identity") or {}).get("ip"),
                )
                continue
            active_payloads.append(stamped)

        update_discovery(
            phase="enriching",
            responders=len(active_payloads),
            detail=(
                f"Found {len(active_payloads)} responder"
                f"{'' if len(active_payloads) == 1 else 's'} on {subnet}.x; "
                "loading config and history…"
            ),
        )

        async with httpx.AsyncClient(limits=limits, timeout=discover_timeout) as client:
            config_tasks = [
                _bounded_fetch(
                    client, sem, p["identity"]["ip"], "config", discover_timeout
                )
                for p in active_payloads
            ]
            history_tasks = [
                _bounded_fetch(
                    client, sem, p["identity"]["ip"], "history", discover_timeout
                )
                for p in active_payloads
            ]
            config_raw = await asyncio.gather(*config_tasks)
            history_raw = await asyncio.gather(*history_tasks)

        config_results = [(ip, data) for ip, data, _ in config_raw]
        history_results = [(ip, data) for ip, data, _ in history_raw]

        config_map = {ip: cfg for ip, cfg in config_results if cfg}
        history_map = {ip: hist for ip, hist in history_results if hist}

        update_discovery(
            phase="saving",
            detail="Registering / refreshing modules in the fleet database…",
        )

        with Session(engine) as session:
            known_modules = {
                normalize_mac(m.mac_address): m
                for m in session.exec(select(RobotModule)).all()
            }
            for payload in active_payloads:
                mac = normalize_mac(payload["identity"]["mac"])
                ip = payload["identity"].get("ip")
                if not is_usable_module_ip(ip):
                    logger.warning(
                        "Refusing to register %s with unusable address %r",
                        mac,
                        ip,
                    )
                    continue
                ip = ip.strip()
                node_cfg = config_map.get(ip, {})
                sel_type = node_cfg.get("SELECTOR_TYPE", "UNKNOWN")
                cam_type = node_cfg.get("CAMERA_TYPE", "UNKNOWN")
                use_ntp = bool(node_cfg.get("USE_NTP", False)) if "USE_NTP" in node_cfg else None
                ntp_server = node_cfg.get("NTP_SERVER")

                if mac not in known_modules:
                    mod = RobotModule(
                        mac_address=mac,
                        hostname=payload["identity"]["hostname"],
                        ip_address=ip,
                        last_seen=datetime.utcnow(),
                        selector_type=sel_type,
                        camera_type=cam_type,
                        use_ntp=use_ntp if use_ntp is not None else False,
                        ntp_server=ntp_server or "pool.ntp.org",
                    )
                    session.add(mod)
                else:
                    mod = known_modules[mac]
                    mod.ip_address = ip
                    mod.last_seen = datetime.utcnow()
                    if sel_type != "UNKNOWN":
                        mod.selector_type = sel_type
                    if cam_type != "UNKNOWN":
                        mod.camera_type = cam_type
                    if use_ntp is not None:
                        mod.use_ntp = use_ntp
                    if ntp_server:
                        mod.ntp_server = ntp_server
                    session.add(mod)

                sync_type = node_cfg.get("SYNC_REMOTE_TYPE")
                if sync_type:
                    mod.sync_remote_type = sync_type
                sync_interval = node_cfg.get("SYNC_INTERVAL")
                if sync_interval:
                    try:
                        mod.sync_interval = int(sync_interval)
                    except (TypeError, ValueError):
                        pass

                record_poll_success(mac, payload)

            _merge_history_into_db(session, history_map)
            session.commit()

        duration = time.monotonic() - t0
        count = len(active_payloads)
        finish_discovery(registered=count, duration_seconds=duration)
        logger.info(
            "Manual discovery complete: %d modules found (%.1fs)",
            count,
            duration,
        )
        message = (
            f"Discovery complete on {subnet}.x ({duration:.1f}s). "
            f"Found {count} module{'s' if count != 1 else ''}; "
            "config and history refreshed."
        )
        return {
            "count": count,
            "subnet": subnet,
            "duration_seconds": round(duration, 1),
            "message": message,
        }
    except Exception as exc:
        duration = time.monotonic() - t0
        finish_discovery(registered=0, duration_seconds=duration, error=str(exc))
        raise
