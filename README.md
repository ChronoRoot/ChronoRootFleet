# ChronoRoot Fleet Controller

The Master Controller for the ChronoRoot automated plant phenotyping network. This application provides a centralized, asynchronous orchestration layer to monitor hardware health, manage network configurations, and deploy synchronized biological imaging batches across a distributed fleet of Raspberry Pi edge nodes.

## 🚀 Core Features

* **Passive Aggregation Architecture:** The Master relies on a non-blocking "Pull" architecture. Edge nodes operate entirely autonomously; the Master merely observes, orchestrates, and logs their progress.
* **Zero-Touch Discovery:** Background sweepers continuously scan the subnet (`10.42.0.x`) to automatically discover, register, and configure newly connected modules without manual IP entry.
* **Zero-Touch Reverse Proxy:** Seamlessly access the native web interface of any individual edge node directly through the Master dashboard. A custom FastAPI middleware intercepts and tunnels all traffic, ensuring strict network isolation without requiring any code changes on the edge modules.
* **Global Batch Orchestration:** Launch synchronized time-lapse experiments. The system runs strict pre-flight checks to prevent jobs from starting on nodes with insufficient storage, overlapping schedules, or hardware faults.
* **Autonomous Time Synchronization:** Detects clock drift on offline/non-NTP edge nodes and pushes the master time to ensure perfect chronological alignment of phenotyping data.
* **Bulk Fleet Management:** Push SFTP/FTP/Rclone configurations, trigger background network transfers, or send mass-reboot commands to selected module batches.

## 🛠️ Technology Stack

| Component | Technology | Description |
| :--- | :--- | :--- |
| **Backend Framework** | FastAPI (Python 3.10+) | High-performance async web framework capable of sweeping 250+ IPs in seconds. |
| **Database Tracking** | SQLModel / SQLite | Highly relational tracking of Batches, Hardware Nodes, and Module Runs. |
| **Network Client** | HTTPX | Asynchronous HTTP client for non-blocking edge device communication. |
| **Frontend UI** | Jinja2 + Bootstrap 5 | Server-Side Rendered templates with vanilla JS polling for accessible maintenance. |

## ⚙️ Installation & Setup

The Fleet Controller can be deployed in two ways: via Docker (recommended for PCs/Servers) or directly on bare-metal hardware (recommended if using a Raspberry Pi as the Master Router).

### Option 1: Docker Deployment (PCs / Servers)

1. Clone the repository to your Master Node:
```bash
git clone [https://github.com/your-org/ChronoRoot-FleetControl.git](https://github.com/your-org/ChronoRoot-FleetControl.git)
cd ChronoRoot-FleetControl

```

2. Start the Fleet Master via Docker Compose:

```bash
docker-compose up -d --build

```

3. Open your browser and navigate to `http://localhost:8000` (or the IP address of your Master Node).

---

### Option 2: Raspberry Pi Deployment

Running without Docker is highly recommended for low-resource devices like the Raspberry Pi. This method sets up the application as a background `systemd` service behind an `NGINX` reverse proxy.

#### Automated Installation (Easiest)

We provide an automated setup script that handles all system dependencies, service creation, and NGINX routing.

```bash
git clone [https://github.com/your-org/ChronoRoot-FleetControl.git](https://github.com/your-org/ChronoRoot-FleetControl.git)
cd ChronoRoot-FleetControl
chmod +x setup.sh
./setup.sh

```

#### Manual Installation (Copy & Paste)

If you prefer to set up the system manually, copy and paste the following blocks into your terminal.

**1. Install Dependencies & Setup Virtual Environment:**

```bash
sudo apt update && sudo apt install -y nginx python3-pip python3-venv sqlite3 git
git clone [https://github.com/ChronoRoot/ChronoRoot-FleetControl.git](https://github.com/ChronoRoot/ChronoRoot-FleetControl.git)
cd ChronoRoot-FleetControl
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

```

**2. Create the Systemd Background Service:**

```bash
cat << EOF | sudo tee /etc/systemd/system/fleetcontrol.service
[Unit]
Description=ChronoRoot Fleet Controller (Uvicorn)
After=network.target

[Service]
User=$USER
Group=www-data
WorkingDirectory=/srv/FleetControl
Environment="PATH=/srv/FleetControl/venv/bin"

ExecStart=/srv/FleetControl/venv/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable fleetcontrol --now

```

**3. Configure NGINX:**

```bash
cat << 'EOF' | sudo tee /etc/nginx/sites-available/fleetcontrol
server {
    listen 80;
    server_name _;
    client_max_body_size 50M;

    location / {
        proxy_pass [http://127.0.0.1:8000](http://127.0.0.1:8000);
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300;
        proxy_connect_timeout 300;
    }
}
EOF

sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/fleetcontrol /etc/nginx/sites-enabled/
sudo systemctl restart nginx

```

## 📁 Project Structure

```text
FleetControl/
├── app/
│   ├── core/                 # Shared in-memory state and background monitoring loops
│   ├── doc/                  # System Architecture and About markdown files
│   ├── data/                 # Directory for the persistent SQLite DB
│   ├── routers/              # API endpoints for fleet orchestration and single-node control
│   ├── templates/            # HTML/JS Frontend UI dashboards
│   ├── database.py           # SQLModel schema definitions (ExperimentBatch, ModuleRun)
│   └── main.py               # FastAPI application factory & lifespan events
├── setup.sh                  # Automated bare-metal installer script
├── docker-compose.yml
├── Dockerfile
└── requirements.txt

```

## 🖥️ Usage Guide

### 1. Fleet Operations (Triage)

Navigate to the home page to view the All Modules table. The system evaluates the health of the entire network. Modules with storage warnings, disconnected cameras, or system faults are automatically flagged. Use the Bulk Action bar to send mass network sync commands or reboots.

### 2. Launching a Biological Batch

Select your target modules from the All Modules table and click **Launch Global Batch**. Define your parameters (Timeline, Interval, Lighting). The Master will automatically run a Pre-Flight validation check against all selected nodes before dispatching the payload.

### 3. Reviewing Historical Data

Navigate to the **Experiment Status** tab. This view tracks the global progress of active batches. If a module dropped offline during a run, click **Sync Archive History** to force the Master to connect to the node and pull the true, final picture counts directly from the edge node's local disk.

### 4. Remote Module Access

Need to physically calibrate a camera's focus wheel or check local system logs? Click **Remote View** on any active node in the Fleet Operations table. The Master Controller will securely tunnel into the node, allowing you to interact with its native interface without ever leaving the Fleet Commander.