import asyncio
import httpx
from datetime import datetime
from typing import List, Dict, Any
from sqlmodel import Session, select

from app.core.state import LIVE_FLEET_STATE, TARGET_SUBNET_BASE, API_TIMEOUT
from app.database import engine, RobotModule, ExperimentRun

FAST_POLL_INTERVAL = 15
SLOW_DISCOVERY_INTERVAL = 900  # 15 minutes

async def fetch_endpoint(client: httpx.AsyncClient, ip: str, endpoint: str) -> tuple[str, Any]:
    url = f"http://{ip}/api/{endpoint}"
    try:
        res = await client.get(url, timeout=API_TIMEOUT)
        if res.status_code == 200:
            data = res.json()
            return ip, data
    except Exception:
        pass
    return ip, None

# ==========================================
# ENGINE 1: FAST POLL (Live Monitoring)
# ==========================================
async def fast_monitor_loop():
    while True:
        with Session(engine) as session:
            known_modules = session.exec(select(RobotModule)).all()
            known_ips = [m.ip_address for m in known_modules if m.ip_address]

        if not known_ips:
            await asyncio.sleep(FAST_POLL_INTERVAL)
            continue

        limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
        async with httpx.AsyncClient(limits=limits) as client:
            tasks = [fetch_endpoint(client, ip, "status") for ip in known_ips]
            results = await asyncio.gather(*tasks)

        active_payloads = [data for ip, data in results if data and "identity" in data]
        
        # 1. Update the RAM-Disk Pool
        new_state = {p["identity"]["mac"]: p for p in active_payloads}
        LIVE_FLEET_STATE.clear()
        LIVE_FLEET_STATE.update(new_state)

        # 2. Database Sync & Drop-off Detection
        with Session(engine) as session:
            for payload in active_payloads:
                mac = payload["identity"]["mac"]
                ip = payload["identity"]["ip"]
                hostname = payload["identity"]["hostname"]
                
                mod = session.get(RobotModule, mac)
                if mod:
                    mod.last_seen = datetime.utcnow()
                    session.add(mod)
                    
                    # --- NEW: AUTONOMOUS MANUAL TIME SYNC ---
                    # If the node relies on Manual Time, act as its NTP server!
                    if not mod.use_ntp:
                        sys_time_str = payload.get("system_time")
                        if sys_time_str and sys_time_str != "Unknown":
                            try:
                                node_time = datetime.strptime(sys_time_str, "%Y-%m-%d %H:%M:%S")
                                # If it has drifted by more than 15 seconds, silently push the Master's time
                                drift = abs((datetime.now() - node_time).total_seconds())
                                if drift > 15:
                                    # We use asyncio.create_task to shoot and forget without slowing down the loop
                                    asyncio.create_task(client.post(
                                        f"http://{ip}/api/config/time", 
                                        json={"mode": "manual", "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                        timeout=2.0
                                    ))
                            except Exception:
                                pass
                
                # Get the IDs of jobs currently running in the Pi's RAM
                active_local_ids = list(payload.get("jobs", {}).keys())
                
                # A. UPDATE LIVE JOBS
                for local_exp_id, job_data in payload.get("jobs", {}).items():
                    run_record = session.exec(
                        select(ExperimentRun)
                        .where(ExperimentRun.module_mac == mac)
                        .where(ExperimentRun.local_exp_id == local_exp_id)
                    ).first()
                    
                    safe_taken = int(job_data.get("progress", {}).get("taken") or 0)
                    
                    if run_record:
                        # DYNAMIC MATH: Always trust the Pi's 'expected' over our launch guess!
                        pi_expected = job_data.get("progress", {}).get("expected")
                        if pi_expected:
                            run_record.expected_total = int(pi_expected)
                            
                        run_record.status = job_data.get("status", "RUNNING")
                        run_record.taken_so_far = safe_taken
                        run_record.missed_frames = max(0, int(job_data.get("progress", {}).get("expected_so_far") or 0) - safe_taken)
                        session.add(run_record)
                    else:
                        # (Keep your existing ADOPT ORPHAN logic here if you have it)
                        pass

                # B. DETECT FINISHED JOBS (Drop-Off Detection)
                active_db_runs = session.exec(
                    select(ExperimentRun)
                    .where(ExperimentRun.module_mac == mac)
                    .where(ExperimentRun.status.in_(["RUNNING", "SCHEDULED"]))
                ).all()

                for db_run in active_db_runs:
                    if db_run.local_exp_id not in active_local_ids:
                        # 1. Optimistic Instant UI Update (Assume it finished perfectly)
                        db_run.status = "FINISHED"
                        db_run.taken_so_far = db_run.expected_total
                        session.add(db_run)
                        
                        # 2. Fire the background micro-task to get the absolute truth from the Pi's disk
                        asyncio.create_task(trigger_targeted_history_sync(mac, ip))
                        
            session.commit()

        await asyncio.sleep(FAST_POLL_INTERVAL)
        

async def trigger_targeted_history_sync(mac: str, ip: str):
    """Fired instantly when an experiment finishes to grab the true final count from the Pi's disk."""
    limits = httpx.Limits(max_connections=5)
    async with httpx.AsyncClient(limits=limits) as client:
        # We reuse the same fetch_endpoint logic you already have in sweeper
        history_data = await fetch_endpoint(client, ip, "history")
        
    if not history_data: 
        return
        
    with Session(engine) as session:
        for local_exp_id, job_data in history_data.items():
            run = session.exec(select(ExperimentRun).where(ExperimentRun.local_exp_id == local_exp_id)).first()
            if run:
                safe_expected = int(job_data.get("expected_pictures") or run.expected_total)
                safe_taken = int(job_data.get("taken_pictures") or run.taken_so_far)
                
                # Overwrite the frozen RAM numbers with the true disk numbers
                run.expected_total = safe_expected
                run.taken_so_far = safe_taken
                run.missed_frames = max(0, safe_expected - safe_taken)
                run.status = job_data.get("status", "FINISHED")
                
                if job_data.get("start"): run.start_time = job_data.get("start")
                if job_data.get("end"): run.end_time = job_data.get("end")
                if job_data.get("message"): run.message = job_data.get("message")
                
                session.add(run)
        session.commit()

# ==========================================
# ENGINE 2: SLOW DISCOVERY & HISTORY SYNC
# ==========================================

async def slow_discovery_loop():
    """Wakes up every 15 mins, sweeps the subnet for nodes, updates configs, and syncs history."""
    while True:
        ips = [f"{TARGET_SUBNET_BASE}.{i}" for i in range(1, 255)]
        limits = httpx.Limits(max_connections=300, max_keepalive_connections=50)
        
        # 1. Broad Ping to find online nodes
        async with httpx.AsyncClient(limits=limits) as client:
            tasks = [fetch_endpoint(client, ip, "status") for ip in ips]
            results = await asyncio.gather(*tasks)
            
        active_payloads = [data for ip, data in results if data and "identity" in data]
        
        if active_payloads:
            # 2. Fetch Configs AND Histories for active nodes concurrently
            async with httpx.AsyncClient(limits=limits) as client:
                config_tasks = [fetch_endpoint(client, p["identity"]["ip"], "config") for p in active_payloads]
                history_tasks = [fetch_endpoint(client, p["identity"]["ip"], "history") for p in active_payloads]
                
                # Run all HTTP calls in parallel
                config_results = await asyncio.gather(*config_tasks)
                history_results = await asyncio.gather(*history_tasks)
                
            config_map = {ip: cfg for ip, cfg in config_results if cfg}
            history_map = {ip: hist for ip, hist in history_results if hist}
            
            # 3. Database Updates
            with Session(engine) as session:
                known_modules = {m.mac_address: m for m in session.exec(select(RobotModule)).all()}
                
                # A. Process Discovery (Robot Modules)
                for payload in active_payloads:
                    mac = payload["identity"]["mac"]
                    ip = payload["identity"]["ip"]
                    node_cfg = config_map.get(ip, {})
                    
                    sel_type = node_cfg.get("SELECTOR_TYPE", "UNKNOWN")
                    cam_type = node_cfg.get("CAMERA_TYPE", "UNKNOWN")
                    
                    if mac not in known_modules:
                        mod = RobotModule(
                            mac_address=mac, 
                            hostname=payload["identity"]["hostname"], 
                            ip_address=ip, 
                            last_seen=datetime.utcnow(),
                            selector_type=sel_type,
                            camera_type=cam_type
                        )
                        session.add(mod)
                    else:
                        mod = known_modules[mac]
                        mod.ip_address = ip
                        mod.last_seen = datetime.utcnow()
                        if sel_type != "UNKNOWN": mod.selector_type = sel_type
                        if cam_type != "UNKNOWN": mod.camera_type = cam_type
                        session.add(mod)
                
                # B. Process Historical Archiving (Experiment Runs)
                for ip, history_data in history_map.items():
                    for local_exp_id, job_data in history_data.items():
                        run = session.exec(
                            select(ExperimentRun).where(ExperimentRun.local_exp_id == local_exp_id)
                        ).first()
                        
                        if run:
                            # Safely merge expected vs taken counts
                            safe_expected = int(job_data.get("expected_pictures") or run.expected_total)
                            safe_taken = int(job_data.get("taken_pictures") or run.taken_so_far)
                            
                            run.expected_total = safe_expected
                            run.taken_so_far = safe_taken
                            run.missed_frames = max(0, safe_expected - safe_taken)
                            run.status = job_data.get("status", "FINISHED")
                            
                            if job_data.get("start"): run.start_time = job_data.get("start")
                            if job_data.get("end"): run.end_time = job_data.get("end")
                            if job_data.get("message"): run.message = job_data.get("message")
                            
                            session.add(run)
                            
                # Commit both module updates and run history updates in one transaction
                session.commit()
                
        # Sleep for 15 minutes before sweeping again
        await asyncio.sleep(SLOW_DISCOVERY_INTERVAL)
        
# ==========================================
# ENGINE 3: MANUAL DISCOVERY 
# ==========================================

async def execute_discovery_sweep():
    ips = [f"{TARGET_SUBNET_BASE}.{i}" for i in range(1, 255)]
    limits = httpx.Limits(max_connections=300, max_keepalive_connections=50)
    
    async with httpx.AsyncClient(limits=limits) as client:
        # 1. Broad Ping
        tasks = [fetch_endpoint(client, ip, "status") for ip in ips]
        results = await asyncio.gather(*tasks)
        
    active_payloads = [data for ip, data in results if data and "identity" in data]
    
    # 2. Fetch Configs for the active nodes
    async with httpx.AsyncClient(limits=limits) as client:
        config_tasks = [fetch_endpoint(client, p["identity"]["ip"], "config") for p in active_payloads]
        config_results = await asyncio.gather(*config_tasks)
        
    # Map IPs to their configs for easy lookup
    config_map = {ip: cfg for ip, cfg in config_results if cfg}
    
    with Session(engine) as session:
        known_modules = {m.mac_address: m for m in session.exec(select(RobotModule)).all()}
        for payload in active_payloads:
            mac = payload["identity"]["mac"]
            ip = payload["identity"]["ip"]
            node_cfg = config_map.get(ip, {})
            
            sel_type = node_cfg.get("SELECTOR_TYPE", "UNKNOWN")
            cam_type = node_cfg.get("CAMERA_TYPE", "UNKNOWN")
            
            if mac not in known_modules:
                mod = RobotModule(
                    mac_address=mac, 
                    hostname=payload["identity"]["hostname"], 
                    ip_address=ip, 
                    last_seen=datetime.utcnow(),
                    selector_type=sel_type,
                    camera_type=cam_type
                )
                session.add(mod)
            else:
                mod = known_modules[mac]
                mod.ip_address = ip
                mod.last_seen = datetime.utcnow()
                # Update config in case it was changed while offline
                if sel_type != "UNKNOWN": mod.selector_type = sel_type
                if cam_type != "UNKNOWN": mod.camera_type = cam_type
                session.add(mod)
        session.commit()
        
    return len(active_payloads)