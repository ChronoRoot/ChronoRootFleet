#!/bin/bash

# ChronoRoot Fleet - Network Reconfiguration Script
# Updates IP, DHCP, NAT, Fleet Commander subnet, and Wi-Fi credentials.

# 1. Default Variables
PI_IP="192.168.50.1"
NEW_SSID=""
NEW_PASS=""
HIDE_SSID=0

# 2. Parse Command Line Flags
usage() {
    echo "Usage: $0 [-i ip_address] [-s ssid] [-p password] [-h]"
    echo "  -i  Set the Gateway IP (Default: 192.168.50.1)"
    echo "  -s  Change the Wi-Fi SSID"
    echo "  -p  Change the Wi-Fi Password (must be 8+ characters)"
    echo "  -h  Make the Wi-Fi network hidden"
    exit 1
}

while getopts "i:s:p:h" opt; do
    case $opt in
        i) PI_IP="$OPTARG" ;;
        s) NEW_SSID="$OPTARG" ;;
        p) NEW_PASS="$OPTARG" ;;
        h) HIDE_SSID=1 ;;
        *) usage ;;
    esac
done

# Validate IP format
if [[ ! "$PI_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: Please provide a valid IPv4 address (e.g., 192.168.60.1)"
    exit 1
fi

# Derive Subnet parameters
SUBNET_PREFIX=$(echo "$PI_IP" | cut -d. -f1-3)
SUBNET_CIDR="${SUBNET_PREFIX}.0/24"
DHCP_START="${SUBNET_PREFIX}.10"
DHCP_END="${SUBNET_PREFIX}.200"

echo "=================================================="
echo " Reconfiguring ChronoRoot Network..."
echo " Pi / Gateway IP : $PI_IP"
echo " Subnet CIDR     : $SUBNET_CIDR"
echo " DHCP Range      : $DHCP_START - $DHCP_END"
echo "=================================================="

# 3. Update Wi-Fi AP Settings (hostapd)
echo "> Updating Wi-Fi configuration..."
if [ -n "$NEW_SSID" ]; then
    echo "  -> Setting SSID to: $NEW_SSID"
    sudo sed -i "s|^ssid=.*|ssid=$NEW_SSID|" /etc/hostapd/hostapd.conf
fi

if [ -n "$NEW_PASS" ]; then
    if [ ${#NEW_PASS} -lt 8 ]; then
        echo "Error: Wi-Fi password must be at least 8 characters long."
        exit 1
    fi
    echo "  -> Updating Wi-Fi Password"
    sudo sed -i "s|^wpa_passphrase=.*|wpa_passphrase=$NEW_PASS|" /etc/hostapd/hostapd.conf
fi

if [ "$HIDE_SSID" -eq 1 ]; then
    echo "  -> Hiding SSID broadcast"
    sudo sed -i "s|^ignore_broadcast_ssid=.*|ignore_broadcast_ssid=1|" /etc/hostapd/hostapd.conf
else
    # Ensure it's visible if the -h flag isn't passed
    sudo sed -i "s|^ignore_broadcast_ssid=.*|ignore_broadcast_ssid=0|" /etc/hostapd/hostapd.conf
fi

# 4. Update Systemd wlan0 IP assignment
echo "> Updating wlan0 static IP..."
sudo sed -i "s|ExecStart=/sbin/ip addr add .* dev wlan0|ExecStart=/sbin/ip addr add $PI_IP/24 dev wlan0|" /etc/systemd/system/wlan0-ip.service

# 5. Update dnsmasq (DHCP & Local DNS)
echo "> Updating DHCP pool and local DNS..."
sudo sed -i "s|^dhcp-range=.*|dhcp-range=$DHCP_START,$DHCP_END,255.255.255.0,24h|" /etc/dnsmasq.conf
sudo sed -i "s|^address=/commander.fleet.local/.*|address=/commander.fleet.local/$PI_IP|" /etc/dnsmasq.conf

# 6. Update FastAPI Service Subnet Variable
echo "> Updating Fleet Commander environment variables..."
sudo sed -i "s|^Environment=\"FLEET_TARGET_SUBNET=.*|Environment=\"FLEET_TARGET_SUBNET=$SUBNET_PREFIX\"|" /etc/systemd/system/fleetcontrol.service

# 7. Update iptables (Dynamic NAT Routing)
echo "> Rebuilding Firewall and NAT rules for $SUBNET_CIDR..."
sudo iptables -t nat -F POSTROUTING
sudo iptables -F FORWARD

sudo iptables -t nat -A POSTROUTING -s "$SUBNET_CIDR" ! -o wlan0 -j MASQUERADE
sudo iptables -A FORWARD -i wlan0 -j ACCEPT
sudo iptables -A FORWARD -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT

sudo netfilter-persistent save > /dev/null

# 8. Restart Services to Apply Changes
echo "> Restarting network and commander services..."
sudo systemctl daemon-reload
sudo systemctl restart hostapd
sudo systemctl restart wlan0-ip
sudo systemctl restart dnsmasq
sudo systemctl restart fleetcontrol

echo "=================================================="
echo " Network successfully updated!"
echo "=================================================="