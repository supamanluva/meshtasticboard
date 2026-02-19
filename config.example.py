"""
Meshtastic Dashboard Configuration

Copy this file to config.py and edit with your device IPs:
    cp config.example.py config.py

Add your Meshtastic device IPs below.
Devices must have WiFi enabled and be on the same LAN.
Default Meshtastic TCP port is 4403.
"""

import os

# List of Meshtastic devices on your LAN
# Format: {"name": "friendly name", "host": "IP address", "port": 4403}
DEVICES = [
    {"name": "Device 1", "host": "192.168.1.100", "port": 4403},
    {"name": "Device 2", "host": "192.168.1.101", "port": 4403},
]

# Flask settings
FLASK_HOST = "0.0.0.0"
FLASK_PORT = int(os.environ.get("FLASK_PORT", 5000))
DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

# How often to poll device telemetry (seconds)
TELEMETRY_INTERVAL = 30

# Maximum number of messages to keep in memory
MAX_MESSAGES = 500

# Map default center (latitude, longitude) and zoom level
# Set to your location so maps open where your devices are
MAP_CENTER = [63.9058, 19.7617]   # e.g. your home coordinates
MAP_ZOOM = 13                      # 1=world, 13=city, 18=street

# ── MQTT Configuration ──────────────────────────────────────────────────
# Broker your Meshtastic devices are publishing to
MQTT_BROKER = os.environ.get("MQTT_BROKER", "mqtt.meshtastic.org")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "meshdev")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "large4cats")
MQTT_ROOT_TOPIC = os.environ.get("MQTT_ROOT_TOPIC", "msh/EU_868")
MQTT_ENABLE = os.environ.get("MQTT_ENABLE", "true").lower() == "true"
