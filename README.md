# ChronoRoot Fleet Controller - Master Node

## Overview

The ChronoRoot Fleet Controller is a centralized operations hub designed to orchestrate, monitor, and maintain dozens of individual ChronoRoot imaging modules across a local network using a **Passive Aggregation** architecture.

The system relies on a "Pull" discovery architecture where the Master autonomously discovers modules, monitors their health via RAM-disk state, and records scientific metadata to a persistent SQLite database.

## Project Structure

```text
chronoroot-master/
├── docker-compose.yml       # Infrastructure, virtual network, and mock nodes
├── Dockerfile               # Python 3.10 container blueprint
├── requirements.txt         # FastAPI, SQLModel, Jinja2, httpx
├── README.md                # This file
└── app/
    ├── __init__.py          
    ├── database.py          # SQLModel Schemas (Hardware, Cohorts)
    ├── master_app.py        # Core FastAPI Engine & Smart Sweep Loop
    ├── mock_module.py       # Time-bending ghost container simulator
    ├── data/                # Persistent SQLite database storage
    └── templates/           # Jinja2 SSR UI 
        ├── base.html        # Global layout & navigation
        ├── index.html       # Triage & Fleet Operations View
        ├── experiments.html # Global Biological Run tracking

```

## Infrastructure Configuration

### 1. The `Dockerfile`

The environment uses a lightweight Python 3.10 image.

```dockerfile
FROM python:3.10-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

```

### 2. The `docker-compose.yml`

This configuration spins up the Master Controller and four distinct "Ghost" modules (Workhorse, Veteran, Rookie, Broken) on an isolated `192.168.8.x` subnet.

```yaml
version: '3.8'
networks:
  chrono_lab_net:
    ipam:
      config: [{ subnet: 192.168.8.0/24 }]

services:
  fleet-master:
    build: .
    container_name: chronoroot-fleet-master
    ports: ["8000:8000"]
    volumes: [".:/app", "fleet_db_volume:/data"]
    networks: [chrono_lab_net]
    command: uvicorn app.master_app:app --host 0.0.0.0 --port 8000 --reload

  mock-alpha:
    build: .
    container_name: mock-alpha
    volumes: [".:/app"]
    networks:
      chrono_lab_net: { ipv4_address: 192.168.8.15 }
    environment:
      - MODULE_HOSTNAME=chamber-alpha
      - MODULE_PROFILE=active
    command: uvicorn app.mock_module:app --host 0.0.0.0 --port 80 --reload
    
  # (Additional mock nodes follow same pattern with different IPs and PROFILES)

volumes:
  fleet_db_volume:
    name: chronoroot_fleet_data

```

## Setup & Launch

### Prerequisites

1. **Docker & Docker Compose V2:** Ensure you use `docker compose` (with a space).
2. **Permissions:**

```bash
sudo usermod -aG docker $USER
newgrp docker

```

### Launching

From the root directory, build and start:

```bash
docker compose up -d --build

```

## Operations Guide

### 1. Fleet Hub (Triage)

Navigate to `http://localhost:8000`. The **Fleet Operations** view uses a high-density table to monitor health.

* **Triage:** Modules with errors float to the top automatically.
* **Global Experiments:** Select modules via checkboxes and click "Launch Global Exp" to initiate a synchronized cohort launch.

### 2. Global Experiments View

Navigate to `http://localhost:8000/experiments`.

* View aggregated progress across all modules in a cohort.
* Click any row to expand and view the progress of **individual hardware nodes** participating in that run.

### 3. Resilience Reporting

Navigate to `http://localhost:8000/reports`.

* Tracks the **Blast Radius**: Distinguishes between single-node hardware failures and facility-wide power losses.
* Calculates exact frame loss/downtime for post-mortem analysis.

## Maintenance

* **Diagnostics:** Use the "Run Diagnostics" button in the Fleet Hub to trigger a global hardware scan.
