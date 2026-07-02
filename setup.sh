#!/bin/bash

# ChronoRoot Fleet Controller - Automated Bare-Metal Installer
# Designed for Raspberry Pi Zero / Debian-based systems

echo "=================================================="
echo " Starting ChronoRoot Fleet Controller Setup"
echo "=================================================="

# 1. Install System Dependencies
echo "[1/5] Installing system dependencies and pre-compiled Python libraries..."
sudo apt update
sudo apt install -y \
    nginx python3-pip python3-venv sqlite3 git \
    python3-fastapi python3-uvicorn python3-jinja2 \
    python3-httpx python3-markdown python3-sqlalchemy \
    python3-pydantic python3-websockets python3-uvloop

# 2. Setup Directory and Virtual Environment
echo "[2/5] Setting up virtual environment with system packages..."
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 3. Create Systemd Service
echo "[3/5] Configuring Systemd background service..."
CURRENT_DIR=$(pwd)
CURRENT_USER=$(whoami)

cat << EOF | sudo tee /etc/systemd/system/fleetcontrol.service > /dev/null
[Unit]
Description=ChronoRoot Fleet Commander (Uvicorn)
After=network.target

[Service]
User=$CURRENT_USER
Group=www-data
WorkingDirectory=$CURRENT_DIR
Environment="PATH=$CURRENT_DIR/venv/bin"
Environment="FLEET_MAX_CONCURRENT_POLLS=12"
Environment="FLEET_CONNECT_TIMEOUT=3"
Environment="FLEET_READ_TIMEOUT=15"
Environment="FLEET_MAX_FETCH_RETRIES=1"
Environment="FLEET_STALE_AFTER_MISSES=2"
Environment="FLEET_OFFLINE_GRACE_POLLS=4"
Environment="FLEET_FAST_POLL_INTERVAL_LARGE=30"

ExecStart=$CURRENT_DIR/venv/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable fleetcontrol
sudo systemctl start fleetcontrol

# 4. Configure NGINX Reverse Proxy
echo "[4/5] Configuring NGINX reverse proxy..."
cat << 'EOF' | sudo tee /etc/nginx/sites-available/fleetcontrol > /dev/null
server {
    listen 80;
    server_name _;

    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support for continuous async streams
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        proxy_read_timeout 300;
        proxy_connect_timeout 300;
    }
}
EOF

# Enable the site and remove default
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/fleetcontrol /etc/nginx/sites-enabled/

# 5. Restart NGINX
echo "[5/5] Restarting NGINX..."
sudo systemctl restart nginx

echo "=================================================="
echo " Setup Complete! "
echo " The Fleet Controller is now running in the background."
echo " Access the dashboard at: http://$(hostname -I | awk '{print $1}')"
echo "=================================================="