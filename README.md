# Meshtastic Dashboard

A full-featured web dashboard for monitoring and managing Meshtastic devices on your LAN. Connect via TCP, view mesh nodes, send messages, visualize network topology, run traceroutes, monitor statistics, and tap into the MQTT mesh network — all from your browser.

![Python](https://img.shields.io/badge/python-3.9+-blue)
![Flask](https://img.shields.io/badge/flask-3.x-green)
![Meshtastic](https://img.shields.io/badge/meshtastic-TCP-orange)
![MQTT](https://img.shields.io/badge/MQTT-TLS-blueviolet)
![License](https://img.shields.io/badge/license-MIT-brightgreen)

## Screenshots

> Dark and light theme support with real-time WebSocket updates.

## Features

### Local Device Management
- **Device Overview** — Connection status, firmware version, hardware model, online node count
- **Mesh Nodes** — Full node table with battery, SNR, hops, position, last heard, searchable
- **Real-time Messaging** — Send/receive text messages via any connected device, channel selection
- **Interactive Map** — Leaflet map with all GPS-positioned nodes, click-to-fly sidebar
- **Signal Topology Graph** — D3.js force-directed graph showing node connections colored by SNR
- **Traceroute Tool** — Trace the path to any node, visualize hops on a map
- **Node Statistics** — Charts for battery levels, SNR distribution, channel utilization, hardware models
- **Device Remote Config** — View/edit device owner name, set fixed position, view LoRa & channel settings
- **Device Reboot** — One-click reboot with automatic reconnection
- **Disconnect / Reconnect** — Manage device connections without restarting the server

### MQTT Integration
- **MQTT Live Feed** — Real-time stream of decoded packets from the mesh network via MQTT broker
- **MQTT Node Map** — View all MQTT-discovered nodes on an interactive map
- **MQTT Nodes Table** — Browse all nodes seen on the MQTT network
- **MQTT Send** — Publish text messages to the mesh via MQTT
- **MQTT Device Config** — View and edit your devices' MQTT settings (broker address, username, password, root topic, uplink/downlink toggles, TLS, encryption, JSON output)
- **Encrypted Packet Decryption** — Automatic AES-128-CTR decryption using device channel keys and the Meshtastic default key
- **Multi-key Decryption** — Tries all configured channel PSKs before falling back to the default key
- **Encrypted Message Filter** — Hide or show encrypted/undecryptable packets in the live feed
- **TLS Support** — Connect to MQTT brokers over TLS (port 8883)

### General
- **Dark / Light Theme** — Toggle with persistence via localStorage
- **Message Notifications** — Browser notifications + audio alerts for incoming messages
- **Multi-device** — Connect to multiple Meshtastic devices simultaneously
- **Live Updates** — WebSocket-powered real-time message & telemetry delivery

## Prerequisites

- **Python 3.9+**
- **Meshtastic device(s)** with WiFi enabled, connected to your LAN
- Devices must have the TCP API accessible (default port `4403`)

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/supamanluva/meshtasticboard.git
cd meshtasticboard
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure your devices

```bash
cp config.example.py config.py
```

Edit `config.py` with your Meshtastic device IPs and MQTT broker:

```python
DEVICES = [
    {"name": "Device 1", "host": "192.168.1.100", "port": 4403},
    {"name": "Device 2", "host": "192.168.1.101", "port": 4403},
]

# MQTT broker settings (optional — for mesh-wide monitoring)
MQTT_BROKER = "mqtt.meshtastic.org"
MQTT_PORT = 1883
MQTT_USERNAME = "meshdev"
MQTT_PASSWORD = "large4cats"
MQTT_ROOT_TOPIC = "msh/EU_868"   # or msh/US, msh/ANZ, etc.
MQTT_TLS = False                  # set True + port 8883 for TLS brokers
```

### 5. Run the dashboard

```bash
python app.py
```

Open **http://localhost:5000** in your browser.

## Deployment

### Run with a custom port

```bash
FLASK_PORT=8080 python app.py
```

### Run in production with Gunicorn

```bash
pip install gunicorn
gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
    -w 1 -b 0.0.0.0:5000 app:app
```

> **Note:** Use exactly 1 worker (`-w 1`) because Meshtastic TCP connections are stateful and held in process memory.

### Run as a systemd service

Create `/etc/systemd/system/meshtasticboard.service`:

```ini
[Unit]
Description=Meshtastic Dashboard
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/meshtasticboard
Environment=FLASK_PORT=5000
ExecStart=/path/to/meshtasticboard/venv/bin/python app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable meshtasticboard
sudo systemctl start meshtasticboard
sudo systemctl status meshtasticboard
```

### Run with Docker

Create a `Dockerfile`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["python", "app.py"]
```

```bash
docker build -t meshtasticboard .
docker run -d --name meshtasticboard \
    --network host \
    -v $(pwd)/config.py:/app/config.py \
    meshtasticboard
```

> Using `--network host` so the container can reach Meshtastic devices on your LAN.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FLASK_PORT` | `5000` | Port to serve the dashboard on |
| `FLASK_DEBUG` | `false` | Enable Flask debug mode |
| `SECRET_KEY` | (random) | Flask session secret key |

## Project Structure

```
meshtasticboard/
├── app.py                  # Flask backend, Meshtastic TCP connections, API routes
├── config.example.py       # Example configuration (copy to config.py)
├── config.py               # Your local config (gitignored)
├── requirements.txt        # Python dependencies
├── static/
│   ├── css/
│   │   └── style.css       # Dark/light theme styles
│   └── js/
│       └── dashboard.js    # Frontend: tabs, map, topology, charts, messaging
└── templates/
    └── index.html          # Main HTML template
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Flask + Flask-SocketIO |
| Async | gevent + gevent-websocket |
| Meshtastic | meshtastic Python library (TCP) |
| MQTT | paho-mqtt + protobuf decryption |
| Encryption | cryptography (AES-128-CTR) |
| Map | Leaflet.js + CartoDB/OSM tiles |
| Topology | D3.js force-directed graph |
| Charts | Chart.js |
| Real-time | Socket.IO |

## API Endpoints

### Device Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/devices` | All device info and nodes |
| GET | `/api/devices/<name>` | Single device info |
| GET | `/api/nodes` | All mesh nodes |
| GET | `/api/messages` | Message history |
| POST | `/api/send` | Send a text message |
| POST | `/api/reconnect` | Reconnect a device |
| POST | `/api/disconnect` | Disconnect a device |
| POST | `/api/reboot` | Reboot a device (auto-reconnects) |
| GET | `/api/config/<device>` | Get device configuration |
| POST | `/api/config/<device>/set` | Update device settings |

### Network Analysis

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/topology` | Network topology graph data |
| POST | `/api/traceroute` | Start a traceroute |
| GET | `/api/traceroute/<id>` | Get traceroute result |
| GET | `/api/stats` | Node statistics history |
| GET | `/api/stats/<node_id>` | Statistics for a specific node |
| GET | `/api/stats/summary` | Current stats summary |

### MQTT

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/mqtt/status` | MQTT connection status & stats |
| GET | `/api/mqtt/feed` | Decoded MQTT packet feed |
| GET | `/api/mqtt/nodes` | All MQTT-discovered nodes |
| POST | `/api/mqtt/connect` | Connect to MQTT broker |
| POST | `/api/mqtt/disconnect` | Disconnect from MQTT broker |
| POST | `/api/mqtt/send` | Send a message via MQTT |
| GET | `/api/mqtt/device-config` | Get MQTT config from devices |
| POST | `/api/mqtt/device-config/set` | Update device MQTT settings |

## License

MIT
