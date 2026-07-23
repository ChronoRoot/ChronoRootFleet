# ChronoRoot Fleet Controller: System Architecture

## 1. Executive Summary and Engineering Philosophy

The ChronoRoot Fleet Controller is a centralized operations hub designed to orchestrate, monitor, and maintain dozens of individual imaging modules across a local network.

The core engineering philosophy of this system is **Passive Aggregation, Resilient Orchestration, and Total Transparency**. The Master Controller does not dictate real-time hardware operations; rather, it listens to the autonomous modules, organizes their data into experimental batches, and alerts operators to anomalies. If the Master Controller loses power, the individual Raspberry Pis in the growth chambers continue executing their imaging loops, completely unaware that the Master is offline.

To achieve this, the Fleet Controller relies heavily on a "Pull" discovery architecture, stateless UI rendering for volatile data, highly relational database tracking for permanent scientific metadata, and robust failure detection.

## 2. Network Topology and Device Discovery

The Fleet Commander supports two common bare-metal topologies (see the README installers):

* **LAN consumer (`setup.sh`):** the commander joins an existing lab network. Set discovery to that network’s `/24` base via `FLEET_TARGET_SUBNET` (systemd) or the dashboard **Network** button (persisted in `fleet_runtime.env`).
* **Hotspot + share (`setup_hotspot.sh`):** the commander runs a Wi‑Fi AP on `wlan0` (`ChronoRootWifi`, gateway `192.168.50.1`), DHCP via dnsmasq, and optional NAT from the uplink interface so modules can reach the internet through the commander. Discovery defaults to `192.168.50`.

Discovery itself is configurable via `FLEET_TARGET_SUBNET` (default `192.168.1`; hotspot installs use `192.168.50`). ChronoRoot lab deployments often use `10.42.0.x`. The network is typically kept physically or logically isolated from enterprise networks to bypass strict firewall policies.

Because we require **Zero-Touch Module Integration**, individual modules do not broadcast to the Master, nor do they require any custom software modifications to communicate with it. The Master handles network interactions through two distinct engines:

* **The Dual-Engine Sweeper:**
  * *The Fast Monitor Loop (15–25s):* Rapidly polls known nodes (from the database) via staggered concurrent async HTTP to update an in-memory RAM-disk with live metrics. Missed polls use a grace period before a module is marked offline.
  * *Manual Discovery (on demand):* A full subnet sweep (`.1–.254`) triggered by the **Discover** button on the dashboard. Use this when plugging in new modules or deploying a Fleet Commander in a new chamber. Discovery registers modules and backfills experiment history.

* **The Zero-Touch Reverse Proxy:** To maintain strict network isolation without sacrificing accessibility, the Master Controller features a built-in reverse proxy powered by a custom FastAPI middleware. This allows users to access the native web interface of any individual edge node directly through the Master dashboard. The middleware intercepts and tunnels all traffic (including orphaned static assets) seamlessly, ensuring researchers never need to connect directly to the isolated hardware subnet.

## 3. Database & Hardware Architecture

The database is strictly relational, segregating the physical hardware from the scientific experiments to maintain a pristine logbook.

* **`RobotModule` (The Hardware Table):** Tracks physical devices via their absolute MAC Address. Ensures historical data is always tied to the correct physical robot regardless of IP changes. It also maintains custom human-readable aliases and physical location descriptions (e.g., "Growth Chamber B, Top Shelf") to help technicians map digital nodes to physical hardware.
* **`ExperimentBatch` (The Global Science Table):** Acts as the master record for the scientific parameters (Interval, Lighting, Launch Time) when a global experiment is initiated.
* **`ExperimentRun` (The Relational Bridge):** Tracks exactly how an individual piece of hardware performed during a global batch launch, recording expected yields versus actual taken frames to flag dropped timelines.

## 4. Standard Operating Procedure (SOP)

The UI workflows are designed to facilitate a lab technician's daily maintenance cycle:

* **Step 1: Fleet Triage & Identity Management.** The All Modules dashboard provides a macro view of all physical hardware. Offline nodes, storage warnings, and camera health drops are instantly flagged. From here, technicians can also assign or update the physical location descriptions for modules deployed in the field.
* **Step 2: Hardware Calibration & Remote Access.** If a module requires physical calibration (e.g., adjusting a manual camera focus wheel) or deep log inspection, the technician clicks **Remote View**. The Master securely tunnels into the isolated module, rendering its native UI seamlessly within the same browser tab.
* **Step 3: Configuration & Sync.** The controller can push mass updates to edge nodes, such as assigning Rclone/SFTP targets for data offloading or pushing manual time-syncs to nodes lacking NTP access.
* **Step 4: Global Launch.** The technician selects targeted modules, defines the biological timeline, and initiates a strict pre-flight check. The Master verifies storage constraints and hardware availability before dispatching the batch job.
* **Step 5: Historical Sync.** Once an experimental batch concludes, the Master pulls the final, absolute picture counts from the edge nodes to generate permanent scientific records.