# ChronoRoot Fleet Commander

Central orchestration hub for the [ChronoRoot](https://chronoroot.github.io/) automated plant phenotyping network. The Fleet Commander monitors hardware health, manages network configurations, launches synchronized imaging batches, and provides secure remote access to edge modules across a distributed fleet of Raspberry Pi nodes.

**Controller Repository:** [github.com/ChronoRoot/ChronoRootFleet](https://github.com/ChronoRoot/ChronoRootControl)

## Core Features

* **Passive aggregation architecture:** Edge nodes run autonomously. The commander observes, orchestrates, and logs progress via a non-blocking pull model — modules keep imaging even if the commander goes offline.
* **Fast monitor loop:** Known modules are polled on a configurable interval (15–30s) with concurrent async HTTP, grace-period presence tracking, and stale/offline UI states.
* **On-demand discovery:** A full subnet sweep (`.1`–`.254`) registers new modules and backfills experiment history. Trigger via the **Discover** button or `POST /api/fleet/discover`.
* **Zero-touch reverse proxy:** Access any edge node's native web UI through the dashboard. FastAPI middleware tunnels traffic (including static assets and video streams) without modifying edge software.
* **Global batch orchestration:** Launch synchronized time-lapse experiments with strict pre-flight checks for storage, camera availability, and schedule conflicts.
* **Autonomous time synchronization:** Detects clock drift on manual-time nodes and pushes master time to align phenotyping timestamps.
* **Bulk fleet management:** Push SFTP/FTP/Rclone configs, trigger background transfers, run diagnostics, mass reboot, or batch **software updates** (`git pull` on modules).
* **Commander self-service:** Update the Fleet Commander itself via git pull, and change the host NTP server or manual clock from the dashboard (buttons next to **Discover Nodes**).
* **Offline UI assets:** Bootstrap 5 and Font Awesome are vendored under `app/static/vendor/` so the dashboard works on isolated networks without CDN access.

## Technology Stack

| Component | Technology | Role |
| :--- | :--- | :--- |
| Backend | FastAPI (Python 3.10+) | Async web framework and API layer |
| Database | SQLModel / SQLite | Persistent tracking of modules, batches, and runs |
| HTTP client | HTTPX | Non-blocking communication with edge nodes |
| Frontend | Jinja2 + Bootstrap 5 | Server-rendered dashboards with vanilla JS polling |

## Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                    Fleet Commander (Master)                 │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐ │
│  │ Fast Monitor │  │   Discovery  │  │  Reverse Proxy     │ │
│  │ Loop (poll)  │  │  (on demand) │  │  /proxy/{mac}/…    │ │
│  └──────┬───────┘  └──────┬───────┘  └─────────┬──────────┘ │
│         │                 │                     │           │
│  ┌──────┴─────────────────┴─────────────────────┴──────────┐│
│  │  In-memory presence (LIVE_FLEET_STATE) + SQLite DB      ││
│  └─────────────────────────────────────────────────────────┘│
└────────────────────────────┬────────────────────────────────┘
                             │ HTTP (isolated subnet)
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
   ┌───────────┐       ┌───────────┐       ┌───────────┐
   │  Module   │       │  Module   │       │  Module   │
   │  (Pi)     │       │  (Pi)     │       │  (Pi)     │
   └───────────┘       └───────────┘       └───────────┘
```

**Presence state machine:** A module stays *fresh* while polls succeed. After consecutive misses it becomes *stale* (yellow UI), then *offline* (evicted from live state). Poll health is exposed at `GET /api/fleet/diagnostics`.

## Installation

Choose how the Fleet Commander attaches to the lab:

| Mode | Script | Use when |
| :--- | :--- | :--- |
| **LAN consumer** | `setup.sh` | The Pi joins an existing Ethernet/Wi‑Fi LAN where modules already live |
| **Hotspot + share** | `setup_hotspot.sh` | The Pi creates a Wi‑Fi AP for modules and can share uplink internet (usually Ethernet) via NAT |
| **Docker** | `docker compose` | PC/server with host networking (see below) |

Clone once, then run the matching installer from the project directory (recommended path `/srv/ChronoRootFleet`):

```bash
sudo mkdir -p /srv
sudo git clone https://github.com/ChronoRoot/ChronoRootFleet.git /srv/ChronoRootFleet
cd /srv/ChronoRootFleet
```

### Option A: LAN consumer (`setup.sh`)

The Pi is a normal client on your lab network. Discovery defaults to subnet `192.168.1` unless you set `FLEET_TARGET_SUBNET` (systemd env or the dashboard **Network** button).

```bash
chmod +x setup.sh
./setup.sh
```

Access the dashboard at `http://<pi-ip>/`. Align discovery with your LAN (e.g. `10.42.0`) via **Network** on the home page, or:

```bash
# in fleetcontrol.service
Environment="FLEET_TARGET_SUBNET=10.42.0"
sudo systemctl daemon-reload && sudo systemctl restart fleetcontrol
```

### Option B: Hotspot + share (`setup_hotspot.sh`)

Creates an access point so modules can join an isolated chamber network while the commander optionally NATs traffic from its default uplink (Ethernet/Wi‑Fi) to `wlan0`.

| Setting | Value |
| :--- | :--- |
| SSID | `ChronoRootWifi` |
| Password | `chronoroot` |
| Commander AP IP | `192.168.50.1` |
| DHCP range | `192.168.50.10`–`.200` |
| Local DNS name | `commander.fleet.local` |
| Discovery subnet | `192.168.50` (set in the systemd unit) |

```bash
chmod +x setup_hotspot.sh
./setup_hotspot.sh
```

- Join modules to **ChronoRootWifi**.
- Open the dashboard at `http://192.168.50.1/` or `http://commander.fleet.local/` when connected to the AP.
- SSH is enabled on port 22 on the AP interface.
- If no default route exists at install time, NAT is skipped until an uplink appears (re-run NAT rules or reconnect Ethernet and refresh iptables as needed).

### Option C: Docker (PC / Server)

```bash
git clone https://github.com/ChronoRoot/ChronoRootFleet.git
cd ChronoRootFleet
docker compose up -d --build
```

Open `http://localhost:8000` (or the host machine's IP). Docker uses `network_mode: host` so the commander can reach edge nodes on the local subnet. Set `FLEET_TARGET_SUBNET` in the compose environment if your modules are not on `192.168.1.x`.

The SQLite database is stored in the `chronoroot_fleet_data` Docker volume at `/data/fleet_data.db`.

### Service PATH, sudo, and offline assets

The systemd unit runs as the installing user (not root). Its `PATH` must include both the project venv **and** system binaries (`/usr/bin`, …) so `git`, `sudo`, and `timedatectl` resolve. New installs get this automatically; existing units that only set `PATH=…/venv/bin` should be updated and the service restarted:

```bash
sudo systemctl edit --full fleetcontrol
# set:
# Environment="PATH=/srv/ChronoRootFleet/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
sudo systemctl restart fleetcontrol
```

Commander time/NTP and post-update restarts use `sudo -n` for `timedatectl`, `date`, `sed`, and `systemctl`. Grant passwordless sudo for those commands to the service user (same pattern as ChronoRootControl modules), or run the unit as root.

Bootstrap 5.3 and Font Awesome 5.15.4 ship under `app/static/vendor/`. To refresh them on a machine with internet before deploying to an isolated Pi:

```bash
./scripts/vendor_assets.sh
```

#### Manual bare-metal install

```bash
sudo apt update && sudo apt install -y nginx python3-pip python3-venv sqlite3 git
sudo git clone https://github.com/ChronoRoot/ChronoRootFleet.git /srv/ChronoRootFleet
cd /srv/ChronoRootFleet
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create `/etc/systemd/system/fleetcontrol.service` pointing `WorkingDirectory` and `ExecStart` at `/srv/ChronoRootFleet`, then configure NGINX to proxy port 80 → `127.0.0.1:8000`. See `setup.sh` / `setup_hotspot.sh` for reference templates.

## Usage

### Fleet operations (triage)

The home page lists all registered modules. Storage warnings, camera faults, and connectivity issues are surfaced automatically. Use bulk actions to sync time, push configs, update module software, or reboot selected nodes.

Next to **Discover Nodes**:

* **Update Software** — `git pull` on the Fleet Commander install directory; restarts `fleetcontrol` if code changed.
* **Commander Time** — set NTP server (network mode) or a manual date/time on the commander host.
* **Network** — view host addresses and change the **discovery subnet** (`FLEET_TARGET_SUBNET`, e.g. `192.168.1` or `192.168.50`) without editing systemd.

Bulk **Update** on modules requires ChronoRootControl builds that expose `POST /api/update`. Older modules return HTTP 405 (Flask treats `update` as an experiment id). Update those Pis once via SSH/`git pull`, then fleet Update will work.

After upgrading, click **Discover** once so each module’s `USE_NTP` flag is copied into the fleet database (fixes System Clock badges that previously always showed NTP).

### Discovering new modules

New hardware is not auto-registered by the fast monitor alone — it only polls modules already in the database. After connecting modules to the subnet, click **Discover** (or call `POST /api/fleet/discover`) to scan the subnet and register them.

### Launching a batch experiment

Select modules, click **Launch Global Batch**, and define timeline, interval, and lighting. Pre-flight validation checks offline nodes, camera availability, storage, and schedule conflicts before dispatch.

### Experiment history

The **Experiment Status** tab tracks batch progress. Use **Sync Archive History** to pull final picture counts from edge nodes after a run.

### Remote module access

Click **Remote View** on any online module to tunnel into its native UI through the commander proxy.

## API Overview

| Endpoint | Description |
| :--- | :--- |
| `GET /api/fleet/live-digested` | Digested fleet state for the dashboard |
| `GET /api/fleet/diagnostics` | Poll monitor health and error breakdown |
| `POST /api/fleet/discover` | Full subnet discovery sweep |
| `POST /api/fleet/experiment/launch` | Launch a global batch experiment |
| `POST /api/fleet/bulk/*` | Bulk reboot, diagnostic, config, sync, time, software update |
| `POST /api/commander/update` | `git pull` on the Fleet Commander; restart if changed |
| `GET /api/commander/time` | Commander host clock / NTP snapshot |
| `POST /api/commander/time` | Set commander NTP or manual time |
| `GET /api/commander/network` | Discovery subnet + host addresses |
| `PUT /api/commander/network` | Persist discovery subnet (`FLEET_TARGET_SUBNET`) |
| `GET /api/fleet/db/experiments` | Historical batch data |
| `GET /proxy/{mac}/{path}` | Reverse proxy to edge node UI |

Full interactive docs are available at `/docs` when the server is running.

## Configuration (environment variables)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `FLEET_TARGET_SUBNET` | `192.168.1` (or file / hotspot `192.168.50`) | Subnet base for Discover (`.1`–`.254`); `fleet_runtime.env` from the Network UI overrides installer env |
| `FLEET_RUNTIME_ENV` | `<repo>/fleet_runtime.env` | Path for persisted runtime settings (subnet); surviving service restarts |
| `FLEET_MAX_CONCURRENT_POLLS` | `12` | Max parallel status polls per cycle |
| `FLEET_CONNECT_TIMEOUT` | `3` | TCP connect timeout (seconds) |
| `FLEET_READ_TIMEOUT` | `15` | `/api/status` read timeout (seconds) |
| `FLEET_MAX_FETCH_RETRIES` | `1` | Retries per module per poll cycle |
| `FLEET_FAST_POLL_INTERVAL` | `15` | Seconds between poll cycles (small fleet) |
| `FLEET_FAST_POLL_INTERVAL_LARGE` | `30` | Seconds between poll cycles (10+ modules) |
| `FLEET_LARGE_FLEET_THRESHOLD` | `10` | Module count threshold for large-fleet interval |
| `FLEET_STALE_AFTER_MISSES` | `2` | Consecutive misses before stale (yellow) UI |
| `FLEET_OFFLINE_GRACE_POLLS` | `4` | Consecutive misses before marking offline |
| `FLEET_POLL_TASK_STAGGER` | `0` | Delay between launching each poll task |
| `FLEET_USE_LIVENESS_PROBE` | off | Short `/api/status` probe before full fetch |
| `FLEET_DISCOVERY_MAX_CONCURRENT` | `32` | Max parallel requests during discovery |
| `FLEET_PROXY_DB_FALLBACK_MINUTES` | `5` | Use last-known IP for proxy when offline |
| `FLEET_REPO_DIR` | auto (parent of `app/`) | Install directory for commander `git pull` |
| `FLEET_SERVICE_NAME` | `fleetcontrol` | systemd unit restarted after commander update |

Legacy: `FLEET_POLL_BATCH_SIZE` is an alias for `FLEET_MAX_CONCURRENT_POLLS` if the latter is unset.

When modules flap yellow or offline, check `GET /api/fleet/diagnostics` for `modules_responded` vs `modules_polled`, `last_cycle_duration_seconds`, and the `errors` breakdown (`ReadTimeout`, `PoolTimeout`, `ConnectError`).

## Project Structure

```text
ChronoRootFleet/
├── app/
│   ├── core/                 # In-memory state, poll sweeper, UI transformers, host ops
│   ├── doc/                  # Architecture and about markdown (rendered in /about)
│   ├── routers/              # API routes, views, reverse proxy
│   ├── static/vendor/        # Offline Bootstrap 5 + Font Awesome
│   ├── templates/            # Jinja2 dashboards and components
│   ├── database.py           # SQLModel schema (RobotModule, ExperimentalBatch, ExperimentRun)
│   └── main.py               # FastAPI app, lifespan, proxy middleware
├── scripts/vendor_assets.sh  # Re-download offline CSS/JS vendors
├── setup.sh                  # Bare-metal installer (systemd + NGINX)
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

## Security Notice

The Fleet Commander is designed for **isolated lab networks**. It has no built-in authentication — all dashboard and API endpoints are open to anyone who can reach the host. Deploy only on trusted subnets; do not expose port 80/8000 to the public internet without adding a reverse proxy with authentication.

Edge communication uses plain HTTP. Credentials for SFTP/FTP sync are forwarded to modules in request bodies.

## Related Projects

| Project | Description |
| :--- | :--- |
| [ChronoRootFleet](https://github.com/ChronoRoot/ChronoRootFleet) | This repository — fleet orchestration |
| [ChronoRootControl](https://github.com/ChronoRoot/ChronoRootControl) | Single-module edge controller |
| [ChronoRoot2](https://github.com/ChronoRoot/ChronoRoot2) | Image analysis pipeline |

## License

Dual-licensed under **CeCILL v2.1** or **GNU GPL v3**. See [LICENSE](LICENSE) and [LICENSE_FR](LICENSE_FR).

## Citation

If you use ChronoRoot in your research, please cite:

> Gaggion, N., Boccardo, N.A., Bonazzola, R., et al. *ChronoRoot 2.0: an open AI-powered platform for 2D temporal plant phenotyping.* GigaScience, 15, giag018 (2026). [doi:10.1093/gigascience/giag018](https://doi.org/10.1093/gigascience/giag018)
