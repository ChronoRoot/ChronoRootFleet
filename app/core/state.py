from typing import Dict, Any

# --- NETWORK CONFIGURATION ---
TARGET_SUBNET_BASE = "192.168.1"  
SWEEP_INTERVAL = 60 
API_TIMEOUT = 3.0               

# --- SHARED RAM-DISK ---
# This dictionary is imported by all routers to access the real-time state of the Pi's
LIVE_FLEET_STATE: Dict[str, Any] = {}
