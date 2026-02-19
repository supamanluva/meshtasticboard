#!/usr/bin/env python3
"""
Meshtastic Dashboard - Backend Server

Connects to Meshtastic devices via TCP and provides a web dashboard
for monitoring nodes, telemetry, messaging, topology, traceroute,
statistics, and remote configuration.
"""

import os
import json
import time
import uuid
import struct
import base64
import threading
import traceback
from datetime import datetime, timezone
from collections import defaultdict

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO

import meshtastic
import meshtastic.tcp_interface
from pubsub import pub
from google.protobuf.json_format import MessageToDict as _pb_to_dict

import paho.mqtt.client as paho_mqtt
from meshtastic.protobuf import mqtt_pb2, mesh_pb2, portnums_pb2
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import config

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", os.urandom(24).hex())
socketio = SocketIO(app, async_mode="gevent", cors_allowed_origins="*")

# ── State ────────────────────────────────────────────────────────────────────

devices = {}          # name -> { interface, info, nodes, channels }
messages = []         # list of message dicts
lock = threading.Lock()

# Statistics history: node_id -> list of { timestamp, battery, snr, chUtil, airTx, uptime }
stats_history = defaultdict(list)
MAX_STATS_ENTRIES = 500

# Topology data: edges between nodes
topology_edges = {}   # "from_id->to_id" -> { from, to, snr, rssi, lastSeen }

# Traceroute results
traceroute_results = {}  # request_id -> { status, route, ... }
traceroute_counter = 0

# ── MQTT State ───────────────────────────────────────────────────────────────

mqtt_client = None
mqtt_connected = False
mqtt_feed = []            # recent MQTT packets (ring buffer)
mqtt_nodes = {}           # node_id -> { longName, shortName, hwModel, position, lastSeen, ... }
mqtt_stats = {
    "connected": False,
    "broker": "",
    "msg_count": 0,
    "msg_rate": 0,          # msgs/sec (rolling avg)
    "decoded_count": 0,
    "last_msg_at": None,
    "subscriptions": [],
    "start_time": None,
}
mqtt_rate_window = []     # timestamps for rolling rate calculation
MAX_MQTT_FEED = 500
MQTT_DEFAULT_KEY = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==")[:16]  # default Meshtastic AES key


def _get_decryption_keys():
    """Build a list of AES keys to try: device channel keys + default key."""
    keys = []
    for name, dev in devices.items():
        iface = dev.get("interface")
        if not iface or not dev.get("connected"):
            continue
        try:
            for ch in iface.localNode.channels:
                if ch.role and ch.settings.psk:
                    psk = bytes(ch.settings.psk)
                    if len(psk) in (16, 32) and psk not in keys:
                        keys.append(psk)
        except Exception:
            pass
    if MQTT_DEFAULT_KEY not in keys:
        keys.append(MQTT_DEFAULT_KEY)
    return keys


# ── Meshtastic Connection Helpers ────────────────────────────────────────────

def on_receive(packet, interface):
    """Callback for any received packet."""
    try:
        decoded = packet.get("decoded", {})
        portnum = decoded.get("portnum", "")
        from_id = packet.get("fromId", "")
        to_id = packet.get("toId", "")

        # Track topology from every packet
        _update_topology(packet)

        if portnum == "TEXT_MESSAGE_APP":
            msg = {
                "id": packet.get("id", ""),
                "from": from_id,
                "to": to_id,
                "text": decoded.get("text", ""),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "rxSnr": packet.get("rxSnr", 0),
                "rxRssi": packet.get("rxRssi", 0),
                "hopStart": packet.get("hopStart", 0),
                "hopLimit": packet.get("hopLimit", 0),
                "device": _device_name_for(interface),
                "channel": packet.get("channel", 0),
            }
            with lock:
                messages.append(msg)
                if len(messages) > config.MAX_MESSAGES:
                    messages.pop(0)
            socketio.emit("new_message", msg)

        elif portnum == "POSITION_APP":
            position = decoded.get("position", {})
            if hasattr(position, 'DESCRIPTOR'):
                position = _pb_to_dict(position)
            elif not isinstance(position, dict):
                try:
                    position = {k: v for k, v in position.items()} if hasattr(position, 'items') else {}
                except Exception:
                    position = {}
            try:
                socketio.emit("position_update", {
                    "from": from_id,
                    "position": position,
                })
            except (TypeError, ValueError):
                pass  # skip unserializable position

        elif portnum == "TELEMETRY_APP":
            telemetry = decoded.get("telemetry", {})
            # Convert protobuf to dict if needed
            if hasattr(telemetry, 'DESCRIPTOR'):
                telemetry = _pb_to_dict(telemetry)
            elif not isinstance(telemetry, dict):
                try:
                    telemetry = {k: v for k, v in telemetry.items()} if hasattr(telemetry, 'items') else {}
                except Exception:
                    telemetry = {}
            _record_stats(from_id, packet, telemetry)
            try:
                socketio.emit("telemetry_update", {
                    "from": from_id,
                    "telemetry": telemetry,
                })
            except TypeError:
                pass  # skip unserializable telemetry

        elif portnum == "TRACEROUTE_APP":
            _handle_traceroute_response(packet, interface)

        elif portnum == "NEIGHBORINFO_APP":
            _handle_neighbor_info(packet)

    except Exception:
        traceback.print_exc()


def _update_topology(packet):
    """Record a link between sender and receiver."""
    from_id = packet.get("fromId", "")
    snr = packet.get("rxSnr", None)
    rssi = packet.get("rxRssi", None)
    if not from_id:
        return
    # The receiver is our local node on whichever device got the packet
    for name, dev in devices.items():
        iface = dev.get("interface")
        if iface:
            try:
                my_id = f"!{iface.myInfo.my_node_num:08x}"
                if from_id != my_id:
                    key = f"{from_id}->{my_id}"
                    topology_edges[key] = {
                        "from": from_id,
                        "to": my_id,
                        "snr": snr,
                        "rssi": rssi,
                        "lastSeen": datetime.now(timezone.utc).isoformat(),
                    }
            except Exception:
                pass


def _handle_neighbor_info(packet):
    """Process NEIGHBORINFO_APP packets for topology."""
    decoded = packet.get("decoded", {})
    neighbor_info = decoded.get("neighborinfo", decoded.get("neighbors", {}))
    from_id = packet.get("fromId", "")
    if isinstance(neighbor_info, dict):
        neighbors = neighbor_info.get("neighbors", [])
        for nb in neighbors:
            nb_num = nb.get("nodeId", 0)
            if nb_num:
                nb_id = f"!{nb_num:08x}"
                key = f"{from_id}->{nb_id}"
                topology_edges[key] = {
                    "from": from_id,
                    "to": nb_id,
                    "snr": nb.get("snr", None),
                    "rssi": None,
                    "lastSeen": datetime.now(timezone.utc).isoformat(),
                }


def _record_stats(node_id, packet, telemetry):
    """Record statistics snapshot for a node."""
    device_metrics = telemetry.get("deviceMetrics", {})
    if not device_metrics:
        return
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "batteryLevel": device_metrics.get("batteryLevel"),
        "voltage": device_metrics.get("voltage"),
        "channelUtilization": device_metrics.get("channelUtilization"),
        "airUtilTx": device_metrics.get("airUtilTx"),
        "uptimeSeconds": device_metrics.get("uptimeSeconds"),
        "snr": packet.get("rxSnr"),
    }
    stats_history[node_id].append(entry)
    if len(stats_history[node_id]) > MAX_STATS_ENTRIES:
        stats_history[node_id].pop(0)


def _handle_traceroute_response(packet, interface):
    """Handle traceroute response packets."""
    global traceroute_results
    decoded = packet.get("decoded", {})
    from_id = packet.get("fromId", "")

    # Find matching traceroute request
    for req_id, tr in traceroute_results.items():
        if tr.get("status") == "pending" and tr.get("destination") == from_id:
            route_raw = decoded.get("traceroute", decoded)
            route_nodes = []
            if isinstance(route_raw, dict):
                for node_num in route_raw.get("route", []):
                    route_nodes.append(f"!{node_num:08x}")
                for node_num in route_raw.get("routeBack", []):
                    route_nodes.append(f"!{node_num:08x}")

            tr["status"] = "complete"
            tr["route"] = route_nodes
            tr["completedAt"] = datetime.now(timezone.utc).isoformat()
            tr["snr"] = packet.get("rxSnr")
            socketio.emit("traceroute_result", tr)
            break


def on_connection(interface, topic=pub.AUTO_TOPIC):
    """Called when we successfully connect to a device."""
    name = _device_name_for(interface)
    print(f"✓ Connected to {name}")


def _device_name_for(interface):
    """Look up the friendly name for an interface."""
    for name, dev in devices.items():
        if dev.get("interface") is interface:
            return name
    return "unknown"


def connect_device(dev_cfg):
    """Connect to a single Meshtastic device."""
    name = dev_cfg["name"]
    host = dev_cfg["host"]
    port = dev_cfg.get("port", 4403)

    print(f"→ Connecting to {name} at {host}:{port} …")
    try:
        iface = meshtastic.tcp_interface.TCPInterface(
            hostname=host,
            portNumber=port,
            noProto=False,
        )
        devices[name] = {
            "interface": iface,
            "host": host,
            "port": port,
            "connected": True,
            "connected_at": time.time(),
        }
        print(f"✓ {name} connected  (my node: {iface.myInfo})")
        # Collect initial stats snapshot for all known nodes
        _collect_initial_stats(name, iface)
        return True
    except Exception as e:
        print(f"✗ Failed to connect to {name}: {e}")
        devices[name] = {
            "interface": None,
            "host": host,
            "port": port,
            "connected": False,
            "error": str(e),
        }
        return False


# ── Device Health Watchdog ───────────────────────────────────────────────────

WATCHDOG_INTERVAL = 60  # seconds between health checks
WATCHDOG_AUTO_RECONNECT = True  # automatically try to reconnect lost devices
WATCHDOG_FAIL_THRESHOLD = 3  # consecutive failures before marking device dead
_watchdog_thread = None
_watchdog_fail_counts = {}  # name -> consecutive fail count


def _check_device_health(name, dev):
    """Check if a device's TCP connection is still alive.

    Returns True if healthy, False if dead/lost.
    """
    iface = dev.get("interface")
    if not iface or not dev.get("connected"):
        return False
    try:
        # Check the underlying TCP stream
        stream = getattr(iface, "stream", None)
        if stream is None:
            return False
        # Check if the socket is still open
        sock = getattr(stream, "_socket", None) or getattr(stream, "socket", None)
        if sock is None:
            sock = getattr(iface, "_socket", None)
        if sock and sock.fileno() == -1:
            return False
        return True
    except Exception:
        return False


def _device_watchdog():
    """Background thread that monitors device connections and auto-reconnects."""
    print("🔍 Device watchdog started (interval={WATCHDOG_INTERVAL}s, threshold={WATCHDOG_FAIL_THRESHOLD})")
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        for dev_cfg in config.DEVICES:
            name = dev_cfg["name"]
            dev = devices.get(name)
            if not dev:
                continue

            was_connected = dev.get("connected", False)
            if not was_connected:
                # Device already known to be offline — try auto-reconnect
                if WATCHDOG_AUTO_RECONNECT:
                    _try_auto_reconnect(name, dev_cfg)
                continue

            # Skip health check if device connected recently (grace period)
            connected_at = dev.get("connected_at", 0)
            if time.time() - connected_at < 90:
                continue

            # Device thinks it's connected — verify
            if not _check_device_health(name, dev):
                _watchdog_fail_counts[name] = _watchdog_fail_counts.get(name, 0) + 1
                count = _watchdog_fail_counts[name]
                print(f"⚠ Watchdog: {name} health check failed ({count}/{WATCHDOG_FAIL_THRESHOLD})")

                if count < WATCHDOG_FAIL_THRESHOLD:
                    continue  # not enough failures yet, wait and recheck

                # Confirmed dead after multiple consecutive failures
                _watchdog_fail_counts[name] = 0
                print(f"⚠ Watchdog: {name} connection lost (confirmed after {WATCHDOG_FAIL_THRESHOLD} checks)")
                dev["connected"] = False
                dev["error"] = "connection lost"
                try:
                    iface = dev.get("interface")
                    if iface:
                        iface.close()
                except Exception:
                    pass
                dev["interface"] = None

                socketio.emit("device_status_change", {
                    "device": name,
                    "connected": False,
                    "reason": "connection lost",
                })
                socketio.emit("toast", {
                    "message": f"⚠ Lost connection to {name}",
                    "type": "warning",
                })

                if WATCHDOG_AUTO_RECONNECT:
                    _try_auto_reconnect(name, dev_cfg)
            else:
                # Healthy — reset fail counter
                _watchdog_fail_counts[name] = 0


def _try_auto_reconnect(name, dev_cfg):
    """Attempt to reconnect a device."""
    try:
        print(f"↻ Watchdog: attempting reconnect to {name} …")
        success = connect_device(dev_cfg)
        if success:
            print(f"✓ Watchdog: {name} reconnected!")
            socketio.emit("device_status_change", {
                "device": name,
                "connected": True,
                "reason": "auto-reconnected",
            })
            socketio.emit("toast", {
                "message": f"✓ {name} reconnected automatically",
                "type": "success",
            })
        else:
            print(f"✗ Watchdog: {name} reconnect failed")
    except Exception as e:
        print(f"✗ Watchdog: {name} reconnect error: {e}")


def _collect_initial_stats(dev_name, iface):
    """Grab initial device metrics from all known nodes."""
    try:
        if iface.nodes:
            for nid, node in iface.nodes.items():
                user = node.get("user", {})
                node_id = user.get("id", nid)
                metrics = node.get("deviceMetrics", {})
                if metrics:
                    entry = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "batteryLevel": metrics.get("batteryLevel"),
                        "voltage": metrics.get("voltage"),
                        "channelUtilization": metrics.get("channelUtilization"),
                        "airUtilTx": metrics.get("airUtilTx"),
                        "uptimeSeconds": metrics.get("uptimeSeconds"),
                        "snr": node.get("snr"),
                    }
                    stats_history[node_id].append(entry)
    except Exception:
        pass


def connect_all():
    """Connect to all configured devices."""
    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_connection, "meshtastic.connection.established")
    for dev_cfg in config.DEVICES:
        connect_device(dev_cfg)


def disconnect_all():
    """Gracefully close all device connections."""
    for name, dev in devices.items():
        iface = dev.get("interface")
        if iface:
            try:
                iface.close()
                print(f"⨯ Disconnected {name}")
            except Exception:
                pass


# ── MQTT Module ──────────────────────────────────────────────────────────────

def _mqtt_decrypt(mp, key=None):
    """Decrypt a MeshPacket's encrypted field using AES-128-CTR."""
    if key is None:
        key = MQTT_DEFAULT_KEY
    try:
        nonce = struct.pack("<QII", mp.id, getattr(mp, "from"), 0)
        cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
        dec = cipher.decryptor()
        plaintext = dec.update(mp.encrypted) + dec.finalize()
        data = mesh_pb2.Data()
        data.ParseFromString(plaintext)
        if data.portnum and data.portnum > 0:
            return data
        return None
    except Exception:
        return None


def _mqtt_decode_payload(portnum, payload):
    """Decode a Meshtastic payload bytes into a dict based on portnum."""
    result = {}
    try:
        if portnum == portnums_pb2.TEXT_MESSAGE_APP:
            result["text"] = payload.decode("utf-8", errors="replace")
        elif portnum == portnums_pb2.POSITION_APP:
            p = mesh_pb2.Position()
            p.ParseFromString(payload)
            result["latitude"] = p.latitude_i / 1e7 if p.latitude_i else None
            result["longitude"] = p.longitude_i / 1e7 if p.longitude_i else None
            result["altitude"] = p.altitude if p.altitude else None
            result["time"] = p.time if p.time else None
        elif portnum == portnums_pb2.NODEINFO_APP:
            u = mesh_pb2.User()
            u.ParseFromString(payload)
            result["longName"] = u.long_name
            result["shortName"] = u.short_name
            result["hwModel"] = u.hw_model
            result["hwModelName"] = mesh_pb2.HardwareModel.Name(u.hw_model) if u.hw_model else "?"
            result["macaddr"] = u.macaddr.hex() if u.macaddr else ""
            result["role"] = mesh_pb2.Config.DeviceConfig.Role.Name(u.role) if u.role else ""
        elif portnum == portnums_pb2.TELEMETRY_APP:
            from meshtastic.protobuf import telemetry_pb2
            t = telemetry_pb2.Telemetry()
            t.ParseFromString(payload)
            from google.protobuf.json_format import MessageToDict
            result = MessageToDict(t)
        elif portnum == portnums_pb2.NEIGHBORINFO_APP:
            from meshtastic.protobuf import mesh_pb2 as mpb
            ni = mpb.NeighborInfo()
            ni.ParseFromString(payload)
            from google.protobuf.json_format import MessageToDict
            result = MessageToDict(ni)
        elif portnum == portnums_pb2.MAP_REPORT_APP:
            result["type"] = "map_report"
    except Exception:
        pass
    return result


def _mqtt_process_packet(se):
    """Process a ServiceEnvelope from MQTT into usable data."""
    global mqtt_stats
    mp = se.packet
    from_id = f"!{getattr(mp, 'from'):08x}"
    to_id = f"!{mp.to:08x}" if mp.to != 0xFFFFFFFF else "^all"
    channel = se.channel_id or ""
    gateway = se.gateway_id or ""
    ts = datetime.now(timezone.utc).isoformat()

    data = None
    portnum = 0
    portname = "ENCRYPTED"

    # Try decoded field first (some brokers send pre-decoded)
    if mp.HasField("decoded"):
        data = mp.decoded
        portnum = data.portnum
        portname = portnums_pb2.PortNum.Name(portnum) if portnum else "UNKNOWN"
    elif mp.encrypted:
        # Try all known keys (device channel keys + default)
        for key in _get_decryption_keys():
            dec = _mqtt_decrypt(mp, key)
            if dec and dec.portnum:
                data = dec
                portnum = dec.portnum
                portname = portnums_pb2.PortNum.Name(portnum) if portnum else "UNKNOWN"
                break

    # Decode the payload
    decoded_payload = {}
    if data and data.payload:
        decoded_payload = _mqtt_decode_payload(portnum, data.payload)
        mqtt_stats["decoded_count"] += 1

    # Update MQTT node database
    _mqtt_update_node(from_id, portnum, decoded_payload, mp, channel, gateway, ts)

    # Build feed entry
    entry = {
        "timestamp": ts,
        "from": from_id,
        "to": to_id,
        "channel": channel,
        "gateway": gateway,
        "portnum": portname,
        "rxSnr": mp.rx_snr if mp.rx_snr else None,
        "rxRssi": mp.rx_rssi if mp.rx_rssi else None,
        "hopStart": mp.hop_start if mp.hop_start else None,
        "hopLimit": mp.hop_limit if mp.hop_limit else None,
        "viaMqtt": mp.via_mqtt,
        "encrypted": bool(mp.encrypted) and not data,
        "decoded": bool(data),
        "payload": decoded_payload,
    }

    mqtt_feed.append(entry)
    if len(mqtt_feed) > MAX_MQTT_FEED:
        mqtt_feed.pop(0)

    # Update rate stats
    now = time.time()
    mqtt_rate_window.append(now)
    # Keep last 60s of timestamps
    cutoff = now - 60
    while mqtt_rate_window and mqtt_rate_window[0] < cutoff:
        mqtt_rate_window.pop(0)
    mqtt_stats["msg_rate"] = round(len(mqtt_rate_window) / 60.0, 1)
    mqtt_stats["msg_count"] += 1
    mqtt_stats["last_msg_at"] = ts

    # Emit to dashboard
    socketio.emit("mqtt_packet", entry)
    return entry


def _mqtt_update_node(node_id, portnum, payload, mp, channel, gateway, ts):
    """Update the MQTT-discovered node database."""
    if node_id not in mqtt_nodes:
        mqtt_nodes[node_id] = {
            "id": node_id,
            "longName": None,
            "shortName": None,
            "hwModel": None,
            "latitude": None,
            "longitude": None,
            "altitude": None,
            "role": None,
            "lastSeen": ts,
            "lastChannel": channel,
            "gateway": gateway,
            "snr": None,
            "rssi": None,
            "packetCount": 0,
        }

    node = mqtt_nodes[node_id]
    node["lastSeen"] = ts
    node["packetCount"] += 1
    if mp.rx_snr:
        node["snr"] = mp.rx_snr
    if mp.rx_rssi:
        node["rssi"] = mp.rx_rssi
    if gateway:
        node["gateway"] = gateway
    if channel:
        node["lastChannel"] = channel

    if portnum == portnums_pb2.NODEINFO_APP and payload:
        if payload.get("longName"):
            node["longName"] = payload["longName"]
        if payload.get("shortName"):
            node["shortName"] = payload["shortName"]
        if payload.get("hwModelName"):
            node["hwModel"] = payload["hwModelName"]
        if payload.get("role"):
            node["role"] = payload["role"]

    if portnum == portnums_pb2.POSITION_APP and payload:
        if payload.get("latitude") and payload.get("longitude"):
            node["latitude"] = payload["latitude"]
            node["longitude"] = payload["longitude"]
        if payload.get("altitude"):
            node["altitude"] = payload["altitude"]


def _mqtt_on_connect(client, userdata, flags, reason_code, properties):
    """MQTT on_connect callback."""
    global mqtt_connected
    if reason_code == 0:
        mqtt_connected = True
        root = getattr(config, "MQTT_ROOT_TOPIC", "msh/EU_868")
        subs = [f"{root}/2/e/#", f"{root}/2/json/#"]
        for s in subs:
            client.subscribe(s, qos=0)
        mqtt_stats["connected"] = True
        mqtt_stats["broker"] = getattr(config, "MQTT_BROKER", "")
        mqtt_stats["subscriptions"] = subs
        mqtt_stats["start_time"] = datetime.now(timezone.utc).isoformat()
        print(f"✓ MQTT connected to {mqtt_stats['broker']}")
        print(f"  Subscribed: {', '.join(subs)}")
        socketio.emit("mqtt_status", {"connected": True, "broker": mqtt_stats["broker"]})
    else:
        mqtt_connected = False
        mqtt_stats["connected"] = False
        print(f"✗ MQTT connection failed: {reason_code}")


def _mqtt_on_disconnect(client, userdata, flags, reason_code, properties):
    """MQTT on_disconnect callback."""
    global mqtt_connected
    mqtt_connected = False
    mqtt_stats["connected"] = False
    print(f"⨯ MQTT disconnected (reason: {reason_code})")
    socketio.emit("mqtt_status", {"connected": False})


def _mqtt_on_message(client, userdata, msg):
    """MQTT on_message callback - parse ServiceEnvelope."""
    try:
        # Handle JSON topic
        if "/json/" in msg.topic:
            try:
                jdata = json.loads(msg.payload)
                entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "from": jdata.get("from", "?"),
                    "to": jdata.get("to", "?"),
                    "channel": jdata.get("channel", ""),
                    "gateway": "",
                    "portnum": jdata.get("type", "JSON"),
                    "rxSnr": jdata.get("snr"),
                    "rxRssi": jdata.get("rssi"),
                    "hopStart": None,
                    "hopLimit": None,
                    "viaMqtt": True,
                    "encrypted": False,
                    "decoded": True,
                    "payload": jdata.get("payload", {}),
                }
                mqtt_feed.append(entry)
                if len(mqtt_feed) > MAX_MQTT_FEED:
                    mqtt_feed.pop(0)
                mqtt_stats["msg_count"] += 1
                socketio.emit("mqtt_packet", entry)
            except json.JSONDecodeError:
                pass
            return

        # Parse protobuf ServiceEnvelope
        se = mqtt_pb2.ServiceEnvelope()
        se.ParseFromString(msg.payload)
        if se.packet:
            _mqtt_process_packet(se)
    except Exception:
        pass


def mqtt_connect():
    """Connect to the configured MQTT broker."""
    global mqtt_client
    if not getattr(config, "MQTT_ENABLE", False):
        print("MQTT disabled in config")
        return

    broker = getattr(config, "MQTT_BROKER", "mqtt.meshtastic.org")
    port = getattr(config, "MQTT_PORT", 1883)
    username = getattr(config, "MQTT_USERNAME", "meshdev")
    password = getattr(config, "MQTT_PASSWORD", "large4cats")
    use_tls = getattr(config, "MQTT_TLS", False)

    client_id = f"meshtastic-dashboard-{uuid.uuid4().hex[:8]}"
    print(f"→ MQTT connecting to {broker}:{port} (TLS={'yes' if use_tls else 'no'}) …")

    mqtt_client = paho_mqtt.Client(
        paho_mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
    )
    mqtt_client.username_pw_set(username, password)
    if use_tls:
        import ssl
        mqtt_client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    mqtt_client.on_connect = _mqtt_on_connect
    mqtt_client.on_disconnect = _mqtt_on_disconnect
    mqtt_client.on_message = _mqtt_on_message

    try:
        mqtt_client.connect(broker, port, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"✗ MQTT connection failed: {e}")
        mqtt_stats["connected"] = False
        mqtt_stats["error"] = str(e)


def mqtt_disconnect():
    """Disconnect from MQTT broker."""
    global mqtt_client, mqtt_connected
    if mqtt_client:
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception:
            pass
        mqtt_client = None
    mqtt_connected = False
    mqtt_stats["connected"] = False
    print("⨯ MQTT disconnected")


def mqtt_send_message(text, channel="LongFast"):
    """Send a text message via MQTT."""
    global mqtt_client
    if not mqtt_client or not mqtt_connected:
        return {"error": "MQTT not connected"}

    root = getattr(config, "MQTT_ROOT_TOPIC", "msh/EU_868")
    topic = f"{root}/2/e/{channel}"

    # Build the MeshPacket
    mp = mesh_pb2.MeshPacket()
    mp.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
    mp.decoded.payload = text.encode("utf-8")
    import random
    mp.id = random.getrandbits(32)
    mp.to = 0xFFFFFFFF  # broadcast

    # Wrap in ServiceEnvelope
    se = mqtt_pb2.ServiceEnvelope()
    se.packet.CopyFrom(mp)
    se.channel_id = channel

    try:
        result = mqtt_client.publish(topic, se.SerializeToString())
        return {"status": "sent", "topic": topic, "mid": result.mid}
    except Exception as e:
        return {"error": str(e)}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _serialize_node(node):
    """Convert a node dict into something JSON-safe."""
    user = node.get("user", {})
    pos = node.get("position", {})
    metrics = node.get("deviceMetrics", {})
    snr = node.get("snr", None)
    last_heard = node.get("lastHeard", 0)

    return {
        "num": node.get("num", ""),
        "id": user.get("id", ""),
        "longName": user.get("longName", "Unknown"),
        "shortName": user.get("shortName", "??"),
        "macaddr": user.get("macaddr", ""),
        "hwModel": user.get("hwModel", ""),
        "role": user.get("role", ""),
        "latitude": pos.get("latitude", None),
        "longitude": pos.get("longitude", None),
        "altitude": pos.get("altitude", None),
        "batteryLevel": metrics.get("batteryLevel", None),
        "voltage": metrics.get("voltage", None),
        "channelUtilization": metrics.get("channelUtilization", None),
        "airUtilTx": metrics.get("airUtilTx", None),
        "uptimeSeconds": metrics.get("uptimeSeconds", None),
        "snr": snr,
        "lastHeard": last_heard,
        "lastHeardStr": (
            datetime.fromtimestamp(last_heard, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            if last_heard
            else "never"
        ),
        "hopsAway": node.get("hopsAway", None),
    }


def _device_summary(name, dev):
    """Return a JSON-safe summary of a device."""
    iface = dev.get("interface")
    if not iface or not dev.get("connected"):
        return {
            "name": name,
            "host": dev.get("host"),
            "port": dev.get("port"),
            "connected": False,
            "error": dev.get("error", "not connected"),
        }

    my_info = {}
    device_long_name = None
    try:
        my_node_num = iface.myInfo.my_node_num
        # Resolve hardware model name from protobuf enum
        hw_model_str = "?"
        try:
            from meshtastic.protobuf.mesh_pb2 import HardwareModel
            hw_model_int = getattr(iface.metadata, "hw_model", 0)
            hw_model_str = HardwareModel.Name(hw_model_int) if hw_model_int else "?"
        except Exception:
            hw_model_str = str(getattr(iface.metadata, "hw_model", "?"))
        my_info = {
            "my_node_num": my_node_num,
            "firmware_version": getattr(iface.metadata, "firmware_version", ""),
            "hw_model": hw_model_str,
            "num_online_nodes": len(iface.nodes) if iface.nodes else 0,
            "nodedb_count": getattr(iface.myInfo, "nodedb_count", 0),
            "reboot_count": getattr(iface.myInfo, "reboot_count", 0),
        }
        # Get the real device name from the node's own user info
        if iface.nodes:
            my_node_id = f"!{my_node_num:08x}"
            for nid, node in iface.nodes.items():
                if nid == my_node_id or node.get("num") == my_node_num:
                    user = node.get("user", {})
                    device_long_name = user.get("longName")
                    my_info["shortName"] = user.get("shortName", "")
                    break
    except Exception:
        pass

    nodes = []
    try:
        if iface.nodes:
            for nid, node in iface.nodes.items():
                nodes.append(_serialize_node(node))
    except Exception:
        traceback.print_exc()

    channels = []
    try:
        if iface.localNode and iface.localNode.channels:
            for ch in iface.localNode.channels:
                channels.append({
                    "index": ch.index,
                    "role": str(ch.role),
                    "name": ch.settings.name if ch.settings else "",
                })
    except Exception:
        pass

    return {
        "name": name,
        "deviceName": device_long_name or name,
        "host": dev.get("host"),
        "port": dev.get("port"),
        "connected": True,
        "myInfo": my_info,
        "nodes": nodes,
        "channels": channels,
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/devices")
def api_devices():
    """Return info for all devices."""
    result = []
    for name, dev in devices.items():
        result.append(_device_summary(name, dev))
    return jsonify(result)


@app.route("/api/devices/<device_name>")
def api_device(device_name):
    """Return info for a single device."""
    dev = devices.get(device_name)
    if not dev:
        return jsonify({"error": "device not found"}), 404
    return jsonify(_device_summary(device_name, dev))


@app.route("/api/nodes")
def api_nodes():
    """Return all known nodes across all devices."""
    all_nodes = {}
    for name, dev in devices.items():
        iface = dev.get("interface")
        if iface and iface.nodes:
            for nid, node in iface.nodes.items():
                serialized = _serialize_node(node)
                serialized["seenBy"] = name
                all_nodes[nid] = serialized
    return jsonify(list(all_nodes.values()))


@app.route("/api/debug/nodes")
def api_debug_nodes():
    """Return raw node data for debugging position/GPS issues."""
    raw = {}
    for name, dev in devices.items():
        iface = dev.get("interface")
        if not iface or not dev.get("connected"):
            raw[name] = {"error": "not connected"}
            continue
        if iface.nodes:
            dev_nodes = {}
            for nid, node in iface.nodes.items():
                user = node.get("user", {})
                pos = node.get("position", {})
                dev_nodes[nid] = {
                    "longName": user.get("longName"),
                    "shortName": user.get("shortName"),
                    "hwModel": user.get("hwModel"),
                    "hasPosition": bool(pos),
                    "position_raw": {k: v for k, v in pos.items()} if pos else None,
                    "latitude": pos.get("latitude"),
                    "longitude": pos.get("longitude"),
                    "altitude": pos.get("altitude"),
                    "lastHeard": node.get("lastHeard", 0),
                }
            raw[name] = dev_nodes
        else:
            raw[name] = {"nodes": "none found yet"}
    return jsonify(raw)


@app.route("/api/messages")
def api_messages():
    """Return stored messages."""
    with lock:
        return jsonify(messages.copy())


@app.route("/api/send", methods=["POST"])
def api_send():
    """Send a text message from a device."""
    data = request.json
    device_name = data.get("device")
    text = data.get("text", "")
    destination = data.get("destination", "^all")
    channel_index = data.get("channelIndex", 0)

    if not text:
        return jsonify({"error": "text is required"}), 400

    dev = devices.get(device_name)
    if not dev or not dev.get("connected"):
        return jsonify({"error": f"device '{device_name}' not connected"}), 400

    iface = dev["interface"]
    try:
        if destination == "^all":
            iface.sendText(text, channelIndex=channel_index)
        else:
            iface.sendText(text, destinationId=destination, channelIndex=channel_index)

        msg = {
            "id": "",
            "from": "local",
            "to": destination,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device": device_name,
            "channel": channel_index,
            "sent": True,
        }
        with lock:
            messages.append(msg)
        socketio.emit("new_message", msg)
        return jsonify({"status": "sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reconnect", methods=["POST"])
def api_reconnect():
    """Reconnect to a specific device."""
    data = request.json
    device_name = data.get("device")
    for dev_cfg in config.DEVICES:
        if dev_cfg["name"] == device_name:
            old = devices.get(device_name)
            if old and old.get("interface"):
                try:
                    old["interface"].close()
                except Exception:
                    pass
            success = connect_device(dev_cfg)
            return jsonify({"status": "connected" if success else "failed"})
    return jsonify({"error": "device not found in config"}), 404


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    """Disconnect from a specific device."""
    data = request.json
    device_name = data.get("device")
    dev = devices.get(device_name)
    if not dev:
        return jsonify({"error": "device not found"}), 404
    iface = dev.get("interface")
    if iface:
        try:
            iface.close()
        except Exception:
            pass
    dev["interface"] = None
    dev["connected"] = False
    dev["error"] = "manually disconnected"
    socketio.emit("device_disconnected", {"device": device_name})
    return jsonify({"status": "disconnected", "device": device_name})


@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    """Reboot a specific device."""
    data = request.json
    device_name = data.get("device")
    secs = data.get("secs", 5)
    dev = devices.get(device_name)
    if not dev:
        return jsonify({"error": "device not found"}), 404
    if not dev.get("connected"):
        return jsonify({"error": "device not connected"}), 400
    iface = dev.get("interface")
    try:
        iface.localNode.reboot(secs)
        return jsonify({"status": "rebooting", "device": device_name, "secs": secs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── MQTT API Routes ──────────────────────────────────────────────────────────

@app.route("/api/mqtt/status")
def api_mqtt_status():
    """Return MQTT connection status and stats."""
    return jsonify(mqtt_stats)


@app.route("/api/mqtt/feed")
def api_mqtt_feed():
    """Return recent MQTT packets."""
    limit = request.args.get("limit", 100, type=int)
    portnum = request.args.get("portnum", None)
    feed = mqtt_feed[-limit:]
    if portnum:
        feed = [p for p in feed if p.get("portnum") == portnum]
    return jsonify(feed)


@app.route("/api/mqtt/nodes")
def api_mqtt_nodes():
    """Return nodes discovered via MQTT."""
    return jsonify(list(mqtt_nodes.values()))


@app.route("/api/mqtt/connect", methods=["POST"])
def api_mqtt_connect_route():
    """Connect to the MQTT broker."""
    if mqtt_connected:
        return jsonify({"status": "already connected"})
    mqtt_connect()
    return jsonify({"status": "connecting"})


@app.route("/api/mqtt/disconnect", methods=["POST"])
def api_mqtt_disconnect_route():
    """Disconnect from the MQTT broker."""
    mqtt_disconnect()
    return jsonify({"status": "disconnected"})


@app.route("/api/mqtt/send", methods=["POST"])
def api_mqtt_send():
    """Send a text message via MQTT."""
    data = request.json
    text = data.get("text", "")
    channel = data.get("channel", "LongFast")
    if not text:
        return jsonify({"error": "text is required"}), 400
    result = mqtt_send_message(text, channel)
    if "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/mqtt/device-config")
def api_mqtt_device_config():
    """Return the MQTT configuration from connected devices."""
    configs = []
    for name, dev in devices.items():
        iface = dev.get("interface")
        if not iface or not dev.get("connected"):
            configs.append({"device": name, "connected": False})
            continue
        try:
            mc = iface.localNode.moduleConfig.mqtt
            ch_mqtt = []
            for ch in iface.localNode.channels:
                if ch.role:
                    ch_mqtt.append({
                        "index": ch.index,
                        "name": ch.settings.name or "(default)",
                        "uplink": ch.settings.uplink_enabled,
                        "downlink": ch.settings.downlink_enabled,
                    })
            configs.append({
                "device": name,
                "connected": True,
                "enabled": mc.enabled,
                "address": mc.address or "(default)",
                "username": mc.username or "(default)",
                "root": mc.root or "msh",
                "encryption_enabled": mc.encryption_enabled,
                "json_enabled": mc.json_enabled,
                "tls_enabled": mc.tls_enabled,
                "proxy_to_client_enabled": mc.proxy_to_client_enabled,
                "map_reporting_enabled": mc.map_reporting_enabled,
                "channels": ch_mqtt,
            })
        except Exception as e:
            configs.append({"device": name, "connected": True, "error": str(e)})
    return jsonify(configs)


@app.route("/api/mqtt/device-config/set", methods=["POST"])
def api_mqtt_set_device_config():
    """Update MQTT configuration on a device."""
    data = request.json
    device_name = data.get("device")
    dev = devices.get(device_name)
    if not dev or not dev.get("connected"):
        return jsonify({"error": "device not connected"}), 400

    iface = dev["interface"]
    results = []
    try:
        ln = iface.localNode
        mc = ln.moduleConfig.mqtt
        mqtt_changed = False

        # ── MQTT module config fields ──
        string_fields = {
            "address": "address",
            "username": "username",
            "password": "password",
            "root": "root",
        }
        for key, field in string_fields.items():
            if key in data:
                setattr(mc, field, data[key])
                mqtt_changed = True
                results.append(f"{field}={data[key]}")

        bool_fields = {
            "enabled": "enabled",
            "encryption_enabled": "encryption_enabled",
            "json_enabled": "json_enabled",
            "tls_enabled": "tls_enabled",
            "proxy_to_client_enabled": "proxy_to_client_enabled",
            "map_reporting_enabled": "map_reporting_enabled",
        }
        for key, field in bool_fields.items():
            if key in data:
                setattr(mc, field, bool(data[key]))
                mqtt_changed = True
                results.append(f"{field}={data[key]}")

        if mqtt_changed:
            ln.writeConfig("mqtt")
            results.insert(0, "MQTT config written")

        # ── Channel uplink/downlink ──
        if "channels" in data:
            for ch_cfg in data["channels"]:
                idx = ch_cfg.get("index", 0)
                ch = ln.getChannelByChannelIndex(idx)
                if ch:
                    changed = False
                    if "uplink" in ch_cfg:
                        ch.settings.uplink_enabled = ch_cfg["uplink"]
                        changed = True
                    if "downlink" in ch_cfg:
                        ch.settings.downlink_enabled = ch_cfg["downlink"]
                        changed = True
                    if changed:
                        ln.writeChannel(idx)
                        results.append(f"Ch{idx}: uplink={ch.settings.uplink_enabled}, downlink={ch.settings.downlink_enabled}")

        return jsonify({"status": "ok", "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Topology ─────────────────────────────────────────────────────────────────

@app.route("/api/topology")
def api_topology():
    """Return network topology edges and nodes for graph rendering."""
    # Also build topology from node neighbor data in nodeDB
    for name, dev in devices.items():
        iface = dev.get("interface")
        if not iface or not iface.nodes:
            continue
        try:
            my_id = f"!{iface.myInfo.my_node_num:08x}"
            for nid, node in iface.nodes.items():
                user = node.get("user", {})
                node_id = user.get("id", nid)
                snr = node.get("snr")
                hops = node.get("hopsAway", None)
                if node_id and node_id != my_id and snr is not None:
                    key = f"{node_id}->{my_id}"
                    if key not in topology_edges:
                        topology_edges[key] = {
                            "from": node_id,
                            "to": my_id,
                            "snr": snr,
                            "rssi": None,
                            "hops": hops,
                            "lastSeen": datetime.now(timezone.utc).isoformat(),
                        }
        except Exception:
            pass

    # Build node list for graph
    graph_nodes = {}
    all_known = {}
    for name, dev in devices.items():
        iface = dev.get("interface")
        if iface and iface.nodes:
            for nid, node in iface.nodes.items():
                user = node.get("user", {})
                node_id = user.get("id", nid)
                all_known[node_id] = {
                    "id": node_id,
                    "longName": user.get("longName", "Unknown"),
                    "shortName": user.get("shortName", "??"),
                    "isLocal": False,
                }
            try:
                my_id = f"!{iface.myInfo.my_node_num:08x}"
                if my_id in all_known:
                    all_known[my_id]["isLocal"] = True
            except Exception:
                pass

    return jsonify({
        "nodes": list(all_known.values()),
        "edges": list(topology_edges.values()),
    })


# ── Traceroute ───────────────────────────────────────────────────────────────

@app.route("/api/traceroute", methods=["POST"])
def api_traceroute():
    """Send a traceroute to a destination node."""
    global traceroute_counter
    data = request.json
    device_name = data.get("device")
    destination = data.get("destination")

    if not destination:
        return jsonify({"error": "destination is required"}), 400

    dev = devices.get(device_name)
    if not dev or not dev.get("connected"):
        return jsonify({"error": f"device '{device_name}' not connected"}), 400

    iface = dev["interface"]
    traceroute_counter += 1
    req_id = f"tr-{traceroute_counter}-{int(time.time())}"

    traceroute_results[req_id] = {
        "id": req_id,
        "device": device_name,
        "destination": destination,
        "status": "pending",
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "route": [],
    }

    try:
        iface.sendTraceRoute(dest=destination, hopLimit=7)
        return jsonify({"id": req_id, "status": "pending"})
    except Exception as e:
        traceroute_results[req_id]["status"] = "error"
        traceroute_results[req_id]["error"] = str(e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/traceroute/<req_id>")
def api_traceroute_result(req_id):
    """Check the result of a traceroute."""
    tr = traceroute_results.get(req_id)
    if not tr:
        return jsonify({"error": "traceroute not found"}), 404
    return jsonify(tr)


# ── Statistics ───────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    """Return statistics history for all nodes."""
    return jsonify(dict(stats_history))


@app.route("/api/stats/<node_id>")
def api_stats_node(node_id):
    """Return statistics history for a single node."""
    full_id = node_id if node_id.startswith("!") else f"!{node_id}"
    return jsonify(stats_history.get(full_id, []))


@app.route("/api/stats/summary")
def api_stats_summary():
    """Return current stats summary for all nodes."""
    summary = {}
    for name, dev in devices.items():
        iface = dev.get("interface")
        if not iface or not iface.nodes:
            continue
        for nid, node in iface.nodes.items():
            user = node.get("user", {})
            metrics = node.get("deviceMetrics", {})
            node_id = user.get("id", nid)
            summary[node_id] = {
                "longName": user.get("longName", "Unknown"),
                "shortName": user.get("shortName", "??"),
                "hwModel": user.get("hwModel", ""),
                "batteryLevel": metrics.get("batteryLevel"),
                "voltage": metrics.get("voltage"),
                "channelUtilization": metrics.get("channelUtilization"),
                "airUtilTx": metrics.get("airUtilTx"),
                "uptimeSeconds": metrics.get("uptimeSeconds"),
                "snr": node.get("snr"),
                "lastHeard": node.get("lastHeard", 0),
                "historyPoints": len(stats_history.get(node_id, [])),
            }
    return jsonify(summary)


# ── Remote Config ────────────────────────────────────────────────────────────

@app.route("/api/config/<device_name>")
def api_get_config(device_name):
    """Get current device configuration."""
    dev = devices.get(device_name)
    if not dev or not dev.get("connected"):
        return jsonify({"error": "device not connected"}), 400

    iface = dev["interface"]
    result = {}

    try:
        ln = iface.localNode
        # Owner info
        if iface.nodes:
            my_id = f"!{iface.myInfo.my_node_num:08x}"
            for nid, node in iface.nodes.items():
                if nid == my_id or node.get("num") == iface.myInfo.my_node_num:
                    user = node.get("user", {})
                    result["owner"] = {
                        "longName": user.get("longName", ""),
                        "shortName": user.get("shortName", ""),
                    }
                    break

        # Position config
        if ln and hasattr(ln, "localConfig"):
            lc = ln.localConfig
            if hasattr(lc, "position"):
                pc = lc.position
                result["position"] = {
                    "gps_enabled": pc.gps_enabled if hasattr(pc, "gps_enabled") else False,
                    "fixed_position": pc.fixed_position if hasattr(pc, "fixed_position") else False,
                    "gps_mode": pc.gps_mode if hasattr(pc, "gps_mode") else 0,
                    "position_broadcast_secs": pc.position_broadcast_secs if hasattr(pc, "position_broadcast_secs") else 0,
                }
            if hasattr(lc, "lora"):
                lr = lc.lora
                result["lora"] = {
                    "region": str(lr.region) if hasattr(lr, "region") else "",
                    "modem_preset": str(lr.modem_preset) if hasattr(lr, "modem_preset") else "",
                    "hop_limit": lr.hop_limit if hasattr(lr, "hop_limit") else 3,
                    "tx_power": lr.tx_power if hasattr(lr, "tx_power") else 0,
                    "tx_enabled": lr.tx_enabled if hasattr(lr, "tx_enabled") else True,
                }

        # Channels
        channels = []
        if ln and ln.channels:
            for ch in ln.channels:
                channels.append({
                    "index": ch.index,
                    "role": str(ch.role),
                    "name": ch.settings.name if ch.settings else "",
                })
        result["channels"] = channels

    except Exception as e:
        result["error"] = str(e)
        traceback.print_exc()

    return jsonify(result)


@app.route("/api/config/<device_name>/set", methods=["POST"])
def api_set_config(device_name):
    """Set device configuration values."""
    dev = devices.get(device_name)
    if not dev or not dev.get("connected"):
        return jsonify({"error": "device not connected"}), 400

    iface = dev["interface"]
    data = request.json
    results = []

    try:
        # Set owner name
        if "longName" in data:
            iface.setOwner(long_name=data["longName"])
            results.append(f"longName set to '{data['longName']}'")

        if "shortName" in data:
            iface.setOwner(short_name=data["shortName"])
            results.append(f"shortName set to '{data['shortName']}'")

        # Set position
        if "latitude" in data and "longitude" in data:
            lat = float(data["latitude"])
            lon = float(data["longitude"])
            alt = int(data.get("altitude", 0))
            iface.localNode.setFixedPosition(lat, lon, alt)
            results.append(f"Fixed position set to {lat}, {lon}, alt {alt}")

        if "removePosition" in data and data["removePosition"]:
            iface.localNode.removeFixedPosition()
            results.append("Fixed position removed")

        return jsonify({"status": "ok", "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── SocketIO Events ──────────────────────────────────────────────────────────

@socketio.on("connect")
def handle_ws_connect():
    print("Dashboard client connected")


@socketio.on("send_message")
def handle_ws_send(data):
    """Allow sending messages via WebSocket too."""
    device_name = data.get("device")
    text = data.get("text", "")
    destination = data.get("destination", "^all")
    channel_index = data.get("channelIndex", 0)

    dev = devices.get(device_name)
    if not dev or not dev.get("connected") or not text:
        socketio.emit("error", {"message": "Cannot send: device not connected or empty text"})
        return

    iface = dev["interface"]
    try:
        if destination == "^all":
            iface.sendText(text, channelIndex=channel_index)
        else:
            iface.sendText(text, destinationId=destination, channelIndex=channel_index)

        msg = {
            "id": "",
            "from": "local",
            "to": destination,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device": device_name,
            "channel": channel_index,
            "sent": True,
        }
        with lock:
            messages.append(msg)
        socketio.emit("new_message", msg)
    except Exception as e:
        socketio.emit("error", {"message": str(e)})


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Meshtastic Dashboard")
    print("=" * 60)
    connect_all()
    mqtt_connect()
    # Start device health watchdog
    _watchdog_thread = threading.Thread(target=_device_watchdog, daemon=True)
    _watchdog_thread.start()
    try:
        socketio.run(
            app,
            host=config.FLASK_HOST,
            port=config.FLASK_PORT,
            debug=config.DEBUG,
            use_reloader=False,
        )
    finally:
        mqtt_disconnect()
        disconnect_all()
