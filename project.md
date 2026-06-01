# ChronoRoot Fleet Controller: Master Technical Specification

## 1. Executive Summary and Engineering Philosophy

The ChronoRoot Fleet Controller is a centralized operations hub designed to orchestrate, monitor, and maintain dozens of individual ChronoRoot imaging modules across a local network.

The core engineering philosophy of this system is **Passive Aggregation, Resilient Orchestration, and Total Transparency**. The Master Controller must never become a single point of failure. It does not dictate real-time hardware operations; rather, it listens to the autonomous modules, organizes their data into scientific cohorts, and alerts operators to anomalies. If the Master Controller is destroyed or loses power, the individual Raspberry Pis in the growth chambers must continue executing their imaging loops, completely unaware that the Master is offline.

To achieve this, the Fleet Controller relies heavily on a "Pull" discovery architecture, stateless UI rendering for volatile data, highly relational database tracking for permanent scientific metadata, and an auto-salvage framework designed to keep human intervention to an absolute minimum.

---

## 2. Network Topology and Device Discovery

The system operates on a dedicated subnetwork (e.g., `192.168.8.x/24`), physically isolated from enterprise university networks. This bypasses strict firewall policies and multicast blocking. The Master Controller (running on a PC or a dedicated Raspberry Pi acting as a router via RaspAP) serves as the central node.

### The "Smart Sweep" Protocol

Because we require **Zero-Touch Module Integration**, individual modules are not re-programmed to broadcast to the Master. Instead, the Master finds them autonomously.

1. **The Subnet Sweep:** Every 60 seconds, an asynchronous background task sweeps the local `/24` subnet. It sends a highly concurrent HTTP GET request to `http://<ip>/api/status` for all 254 possible addresses, using a strict 1-second timeout.
2. **The Handshake:** Any IP that responds with a valid ChronoRoot JSON payload is immediately registered or updated in the Master's database.
3. **Zombie Node Detection (The Three-Layer Check):** * If a known module stops responding to the HTTP API, the Master checks the local ARP table to see if the MAC address is still physically connected to the router.
* If the MAC is present but the API is dead, the Master categorizes the node as "OS Alive / App Dead." This triggers the Auto-Salvage protocol.



---

## 3. The API Payload Contract (Data Ingestion)

The entire Fleet Controller relies on the richness of the JSON payload served by the individual modules. The Master does not calculate module-level image progress; it consumes the absolute truth provided by the module's RAM-disk state.

Below is the exact JSON structure the Master expects to ingest:

```json
{
  "identity": {
    "hostname": "chronoroot-rpi",      
    "ip": "192.168.1.100",             
    "mac": "B8:27:EB:XX:XX:XX"         
  },
  "system_health": {
    "storage": {
      "total_gb": 64.0,                
      "free_gb": 12.5,                 
      "percent_used": 80.4,            
      "last_check": "2026-05-25 10:00:00" 
    }
  },
  "uptime": "2h 15m 30s",              
  "status": "running",                 
  "multiplexer": "TYPE_QUAD2",         
  "lock_info": {
    "status": "FREE",                  
    "owner": null,                     
    "details": null,                   
    "acquired_at": null                
  },
  "cam_reports": {                     
    "1": {
      "health": "OK",                  
      "last_check": "2026-05-25 12:00",
      "path": "system/1/...png"        
    }
  },
  "lights_info": {                     
    "state": "OFF",                    
    "health_check": {
      "last_test": "2026-05-25 12:00", 
      "status": "WORKING",             
      "intensity_off_mean": 45.2,
      "intensity_off_std": 5.1,
      "intensity_on_mean": 85.6,
      "intensity_on_std": 12.3,
      "difference": 40.4,              
      "path_off": "system/lights/...off.png", 
      "path_on": "system/lights/...on.png"    
    }
  },
  "last_diagnostic": {                 
    "time": "2026-05-25 12:00:00",
    "global_result": "PASS",           
    "message": "All cameras responsive",
    "cam_snapshot": {}
  },
  "last_picture": "2026-05-25 12:15:00",
  "next_picture": "2026-05-25 12:30:00",
  "active_jobs_count": 1,              
  "jobs": {                            
    "wheat_rpi_2026-05": {
      "name": "Wheat Root Test",
      "start": "2026-05-20 08:00:00",
      "interval": 15,                  
      "status": "RUNNING",             
      "next_run_time": "2026-05-25 12:30",
      "last_capture": {
        "time": "2026-05-25 12:15",
        "result": "SUCCESS"
      },
      "progress": {
        "taken": 45,                   
        "expected": 100,               
        "expected_so_far": 45          
      }
    }
  },
  "sync": {                            
    "sync_enabled": true,
    "is_syncing": false,
    "status_msg": "Idle",
    "last_success": "2026-05-25 12:20:00",
    "next_sync": "2026-05-25 13:20:00"
  },
  "alerts": {                          
    "has_warnings": false,             
    "lock_stuck": false,               
    "picture_overdue": false,          
    "issues": []                       
  }
}

```

### Key Field Utilization by the Master:

* **`identity.mac`:** The absolute Primary Key. Ensures historical data is always tied to the correct physical robot regardless of IP changes.
* **`progress` (Missed Frames Math):** The Master computes `progress.expected_so_far - progress.taken` to calculate missed frames, translating directly into exact downtime (in minutes) during power failures.
* **`lights_info` Distributions:** Extracted `mean` and `std` values are plotted as overlapping distributions in the UI. If ON and OFF distributions completely overlap, the user can visually determine a relay is stuck or a strip is dead without relying on hardcoded thresholds.
* **Image Proxying:** The `cam_reports.path` is used to render images directly in the Master UI by constructing a proxy URL (`http://<module_ip>/api/system_images/<path>`), avoiding the need to download heavy image data to the Master Controller's local storage.

---

## 4. Technology Stack Selection

* **Backend: FastAPI (Python 3.10+)**
Using Python's `asyncio`, FastAPI can ping 50+ modules concurrently. A full network sweep takes 1-2 seconds without blocking the web server.
* **Database: SQLite**
A serverless, zero-configuration database stored as a single local file (`fleet_data.db`). Backing up the entire lab's history is as simple as copying this file.
* **ORM: SQLModel**
Bridges Pydantic (data validation) and SQLAlchemy (database interaction), allowing a single Python class to act as both the database schema and the API payload validator.
* **Frontend: Jinja2 + Bootstrap 5 + Vanilla JS**
Utilizes Server-Side Rendering via Jinja2 and Bootstrap 5 for layout. Auto-refreshing dashboards are handled by native Vanilla JavaScript `fetch()` loops, allowing current developers to maintain the codebase without learning React/Vue.
* **Deployment: Docker & Docker Compose**
The entire Master Controller is containerized. Installation requires only downloading the `docker-compose.yml` file and running `docker-compose up -d`.

---

## 5. Database Architecture (SQLModel Schemas)

The database is strictly relational, segregating the physical hardware from the scientific experiments.

1. **`RobotModule` (The Hardware Table):** * *Primary Key:* `mac_address`.
* *Fields:* `hostname`, `ip_address`, `last_seen`.
* *Purpose:* Tracks physical devices. Volatile live stats are not saved here.


2. **`FleetCohort` (The Global Science Table):**
* *Primary Key:* `id` (Auto-increment integer).
* *Fields:* `name`, `launched_at`, `interval_minutes`, `ir_enabled`.
* *Purpose:* Created when the technician clicks "Launch Cohort." Acts as the master record for the scientific parameters.


3. **`ExperimentRun` (The Relational Bridge):**
* *Primary Key:* `id`.
* *Foreign Keys:* `cohort_id` (links to `FleetCohort`), `module_mac` (links to `RobotModule`).
* *Fields:* `local_exp_id`, `status` (RUNNING, FINISHED), `expected_total`, `taken_so_far`, `missed_frames`.
* *Purpose:* Tracks how an individual piece of hardware performed during a global cohort launch. Serves as a permanent scientific logbook.


---

## 6. Core Background Processes (The Event Loop)

The FastAPI application runs a continuous background loop responsible for anomaly detection and the "Resurrection Protocol."

### The Blast Radius Calculation (Blackout vs. Node Failure)

When the 60-second sweep completes, the Master evaluates failures:

* If *>50%* of known active nodes drop simultaneously, the Master flags an active "Facility Power Loss / Network Drop."

### The Resurrection Protocol

Upon rebooting after a power loss, the Master triggers its startup sequence:

1. Sweeps the network to find modules powering back up.
2. Pulls JSON payloads and checks the `jobs` dictionary.
3. Calculates exact downtime for each job: `(progress.expected_so_far - progress.taken) * interval = lost_minutes`.
4. Suppresses individual error spam and compiles a single Post-Mortem email report (e.g., "Fleet recovered from 3h 15m blackout").

---

## 7. The Standard Operating Procedure & UI Workflows

The system architecture assumes a **single primary user profile**: The Lab Technician / Fleet Operator. This user is responsible for setup, maintenance, calibration, and data offloading. Main researchers consume the data *after* synchronization is complete and do not interact directly with the Fleet Controller.

The User Interface is designed to perfectly facilitate the Technician's daily Standard Operating Procedure (SOP).

### Step 1: Pre-Experiment Prep (The Fleet Hub)

The landing page provides a macro view of all physical hardware.

* **Action:** The technician clicks a global **"Run Fleet Diagnostics"** button. The Master broadcasts `POST /api/diagnostic` to all idle modules.
* **Verification:** The Master UI updates with the results. If a module fails severely (e.g., Multiplexer not recognized), the technician calls an external Hardware Maintainer.

### Step 2: Physical Calibration (Module Redirection)

If diagnostics pass, the technician must physically adjust the setup.

* **Action:** From the Fleet Hub, the technician clicks the `hostname` or IP address of a specific module.
* **UI Behavior:** The Master opens a new browser tab pointed directly at the native module interface (`http://<module_ip>/preview`). The technician uses the native UI to set the camera-to-plate distance and adjust the focus wheel for non-autofocus cameras.

### Step 3: Launching the Experiment (Cohort Creation)

With calibration complete, the biological experiment begins.

* **Action:** The technician returns to the Fleet Hub, uses checkboxes to select the specific modules required for the biological run, and opens the "Launch Parameters" sidebar.
* **UI Behavior:** The technician defines the Start/End dates, Interval, and Lighting. Clicking "Launch" broadcasts the payload to the selected modules and generates a new `FleetCohort` group in the database.

### Step 4: Daily Monitoring (Module Deep-Dive)

The technician logs in daily to ensure the experiment is running smoothly.

* **Action:** The technician navigates to the Individual Dashboard for a module running an active experiment.
* **UI Behavior:** * The UI displays the live `progress` bar (e.g., 450/1000 frames).
* **Crucial Developer Note:** The UI renders an image gallery showing the *latest pictures taken*. To do this without crashing the Master's hard drive, the UI must construct `<img>` tags where the `src` attribute is a direct cross-origin link to the module's REST API using the `cam_reports.path` provided in the JSON (e.g., `<img src="http://192.168.8.15/api/system_images/system/1/...png">`).
* If a camera is physically stuck, the technician utilizes the "Remote Reboot" button from this view.



### Step 5: End of Experiment (Cohort Synchronization)

When the biological timeline finishes, data must be sent to the computation cluster.

* **Action:** The technician navigates to the **Cohort Center**, selects the completed cohort, and clicks **"Synchronize Cohort Data"**.
* **UI Behavior:** The Master Controller sends the `POST /api/sync/trigger` command exclusively to the modules belonging to that cohort. The UI listens to the `sync.status_msg` field to display real-time network transfer progress bars for all modules simultaneously.

---

## 8. The Auto-Salvage and Flexible Alerting Framework

The Master Controller employs an **Auto-Salvage Protocol**—a self-healing mechanism where the Master acts as an automated system administrator before alerting a human.

* **Execution:** Using an asynchronous SSH library and standardized module credentials, the Master can log into an unresponsive node and issue commands (like restarting the `uwsgi` service or rebooting the OS).
* **Escalation:** Only if the Auto-Salvage protocol fails to restore the module during the next sweep does the system escalate to the Alert Engine.
* **Rule Flexibility:** Alerts are not rigidly hardcoded. The system will feature a configurable Rule Engine where the technician can define triggers based on JSON keys (e.g., `IF alerts.lock_stuck == true THEN Send Email`).