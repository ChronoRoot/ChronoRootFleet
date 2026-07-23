#!/bin/bash

# ChronoRoot Fleet Controller - Automated Bare-Metal Installer
# Uses hostapd + dnsmasq with automated NAT routing, DHCP fixes, and auto-SSH

echo "=================================================="
echo " Starting ChronoRoot Fleet Controller Setup"
echo "=================================================="

# 1. Install System Dependencies
echo "[1/8] Installing system dependencies silently..."
sudo apt update

# Pre-answer the iptables-persistent prompts to prevent UI popups
echo iptables-persistent iptables-persistent/autosave_v4 boolean true | sudo debconf-set-selections
echo iptables-persistent iptables-persistent/autosave_v6 boolean true | sudo debconf-set-selections

# Run apt install passing the noninteractive flag directly into sudo
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
    nginx python3-pip python3-venv sqlite3 git \
    hostapd dnsmasq iptables iptables-persistent network-manager \
    python3-fastapi python3-uvicorn python3-jinja2 \
    python3-httpx python3-markdown python3-sqlalchemy \
    python3-pydantic python3-websockets python3-uvloop

# 2. Isolate Wi-Fi from NetworkManager
echo "[2/8] Freeing wlan0 from NetworkManager..."
cat << EOF | sudo tee /etc/NetworkManager/conf.d/99-unmanaged-wifi.conf > /dev/null
[keyfile]
unmanaged-devices=interface-name:wlan0
EOF
sudo systemctl restart NetworkManager

# 3. Configure Static IP for wlan0 via Systemd (With Timing Fixes)
echo "[3/8] Setting up static IP service for wlan0..."
cat << EOF | sudo tee /etc/systemd/system/wlan0-ip.service > /dev/null
[Unit]
Description=Assign IP to wlan0 for Fleet AP
After=sys-subsystem-net-devices-wlan0.device hostapd.service
Requires=sys-subsystem-net-devices-wlan0.device

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/bin/sleep 3
ExecStart=/sbin/ip link set dev wlan0 up
ExecStart=/sbin/ip addr add 192.168.50.1/24 dev wlan0
ExecStartPost=/bin/systemctl restart dnsmasq

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable wlan0-ip.service

# 4. Configure dnsmasq and hostapd
echo "[4/8] Configuring DHCP and Wi-Fi Access Point..."

cat << EOF | sudo tee /etc/dnsmasq.conf > /dev/null
interface=wlan0
bind-dynamic
dhcp-authoritative
dhcp-range=192.168.50.10,192.168.50.200,255.255.255.0,24h
domain=fleet.local
address=/commander.fleet.local/192.168.50.1
EOF

cat << EOF | sudo tee /etc/hostapd/hostapd.conf > /dev/null
interface=wlan0
driver=nl80211
ssid=ChronoRootWifi
hw_mode=g
channel=7
wmm_enabled=1
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=chronoroot
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
EOF

sudo sed -i 's|^#DAEMON_CONF=""|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-fleet-forwarding.conf > /dev/null
sudo sysctl --system

sudo systemctl unmask hostapd
sudo systemctl enable dnsmasq hostapd
sudo systemctl start wlan0-ip dnsmasq hostapd

# 5. Configure NAT Routing, Firewall & SSH
echo "[5/8] Configuring NAT routing, Firewall, and SSH..."
ETH_IF=$(ip route | grep default | awk '{print $5}' | head -n 1)

if [ -n "$ETH_IF" ]; then
    echo "      > Routing internet from: $ETH_IF to wlan0"
    sudo iptables -t nat -A POSTROUTING -o "$ETH_IF" -j MASQUERADE
    sudo iptables -A FORWARD -i "$ETH_IF" -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT
    sudo iptables -A FORWARD -i wlan0 -o "$ETH_IF" -j ACCEPT
else
    echo "      > No default internet interface found right now. Skipping NAT."
fi

sudo iptables -I INPUT -i wlan0 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -i wlan0 -p tcp --dport 22 -j ACCEPT

# Save iptables rules persistently so they survive a reboot
sudo netfilter-persistent save

# Enable and start the SSH service
echo "      > Enabling SSH service at boot..."
sudo systemctl enable ssh
sudo systemctl start ssh

# 6. Setup Directory and Virtual Environment
echo "[6/8] Setting up Python virtual environment..."
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 7. Create Systemd Service for FastAPI
echo "[7/8] Configuring Fleet Commander background service..."
CURRENT_DIR=$(pwd)
CURRENT_USER=$(whoami)

cat << EOF | sudo tee /etc/systemd/system/fleetcontrol.service > /dev/null
[Unit]
Description=ChronoRoot Fleet Commander (Uvicorn)
After=network.target hostapd.service

[Service]
User=$CURRENT_USER
Group=www-data
WorkingDirectory=$CURRENT_DIR
Environment="PATH=$CURRENT_DIR/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="FLEET_MAX_CONCURRENT_POLLS=12"
Environment="FLEET_CONNECT_TIMEOUT=3"
Environment="FLEET_READ_TIMEOUT=15"
Environment="FLEET_MAX_FETCH_RETRIES=1"
Environment="FLEET_STALE_AFTER_MISSES=2"
Environment="FLEET_OFFLINE_GRACE_POLLS=4"
Environment="FLEET_FAST_POLL_INTERVAL_LARGE=30"
Environment="FLEET_TARGET_SUBNET=192.168.50"

ExecStart=$CURRENT_DIR/venv/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable fleetcontrol
sudo systemctl start fleetcontrol

# 8. Configure NGINX Reverse Proxy
echo "[8/8] Configuring NGINX reverse proxy..."
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

echo "=================================================="
echo " Setup Complete! "
echo " The Fleet Hotspot 'ChronoRootWifi' (pw: chronoroot) is active."
echo " SSH is enabled and ready on port 22."
echo " Access the dashboard at: http://192.168.50.1"
echo " (or http://commander.fleet.local if connected to the AP)"
echo "=================================================="