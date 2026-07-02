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
* **Bulk fleet management:** Push SFTP/FTP/Rclone configs, trigger background transfers, run diagnostics, or send mass reboot commands to selected modules.

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

The Fleet Commander can run in Docker (recommended for PCs/servers) or directly on a Raspberry Pi.

### Option 1: Docker (PC / Server)

```bash
git clone https://github.com/ChronoRoot/ChronoRootFleet.git
cd ChronoRootFleet
docker compose up -d --build
```

Open `http://localhost:8000` (or the host machine's IP). Docker uses `network_mode: host` so the commander can reach edge nodes on the local subnet.

The SQLite database is stored in the `chronoroot_fleet_data` Docker volume at `/data/fleet_data.db`.

### Option 2: Raspberry Pi

Running without Docker is recommended on low-resource devices. The setup script installs dependencies, creates a `systemd` service, and configures NGINX as a reverse proxy.

**Important:** The systemd unit expects the project at `/srv/ChronoRootFleet`. Clone there, or edit `WorkingDirectory` in the generated service file.

```bash
sudo mkdir -p /srv
sudo git clone https://github.com/ChronoRoot/ChronoRootFleet.git /srv/ChronoRootFleet
cd /srv/ChronoRootFleet
chmod +x setup.sh
./setup.sh
```

Access the dashboard at `http://<pi-ip>/`.

#### Manual bare-metal install

```bash
sudo apt update && sudo apt install -y nginx python3-pip python3-venv sqlite3 git
sudo git clone https://github.com/ChronoRoot/ChronoRootFleet.git /srv/ChronoRootFleet
cd /srv/ChronoRootFleet
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create `/etc/systemd/system/fleetcontrol.service` pointing `WorkingDirectory` and `ExecStart` at `/srv/ChronoRootFleet`, then configure NGINX to proxy port 80 → `127.0.0.1:8000`. See `setup.sh` for reference templates.

## Usage

### Fleet operations (triage)

The home page lists all registered modules. Storage warnings, camera faults, and connectivity issues are surfaced automatically. Use bulk actions to sync time, push configs, or reboot selected nodes.

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
| `POST /api/fleet/bulk/*` | Bulk reboot, diagnostic, config, sync, time |
| `GET /api/fleet/db/experiments` | Historical batch data |
| `GET /proxy/{mac}/{path}` | Reverse proxy to edge node UI |

Full interactive docs are available at `/docs` when the server is running.

## Configuration (environment variables)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `FLEET_TARGET_SUBNET` | `192.168.1` | Subnet base for manual discovery (e.g. `10.42.0`) |
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

Legacy: `FLEET_POLL_BATCH_SIZE` is an alias for `FLEET_MAX_CONCURRENT_POLLS` if the latter is unset.

When modules flap yellow or offline, check `GET /api/fleet/diagnostics` for `modules_responded` vs `modules_polled`, `last_cycle_duration_seconds`, and the `errors` breakdown (`ReadTimeout`, `PoolTimeout`, `ConnectError`).

## Project Structure

```text
ChronoRootFleet/
├── app/
│   ├── core/                 # In-memory state, poll sweeper, UI transformers
│   ├── doc/                  # Architecture and about markdown (rendered in /about)
│   ├── routers/              # API routes, views, reverse proxy
│   ├── templates/            # Jinja2 dashboards and components
│   ├── database.py           # SQLModel schema (RobotModule, ExperimentalBatch, ExperimentRun)
│   └── main.py               # FastAPI app, lifespan, proxy middleware
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
