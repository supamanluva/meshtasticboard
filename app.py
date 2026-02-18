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
import threading
import traceback
from datetime import datetime, timezone
from collections import defaultdict

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO

import meshtastic
import meshtastic.tcp_interface
from pubsub import pub

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
            socketio.emit("position_update", {
                "from": from_id,
                "position": decoded.get("position", {}),
            })

        elif portnum == "TELEMETRY_APP":
            telemetry = decoded.get("telemetry", {})
            # Convert protobuf to dict if needed
            if hasattr(telemetry, 'DESCRIPTOR'):
                from google.protobuf.json_format import MessageToDict
                telemetry = MessageToDict(telemetry)
            _record_stats(from_id, packet, telemetry)
            socketio.emit("telemetry_update", {
                "from": from_id,
                "telemetry": telemetry,
            })

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
        my_info = {
            "my_node_num": my_node_num,
            "firmware_version": getattr(iface.metadata, "firmware_version", ""),
            "hw_model": str(getattr(iface.myInfo, "hw_model", "")),
            "num_online_nodes": getattr(iface.myInfo, "num_online_local_nodes", 0),
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
    try:
        socketio.run(
            app,
            host=config.FLASK_HOST,
            port=config.FLASK_PORT,
            debug=config.DEBUG,
            use_reloader=False,
        )
    finally:
        disconnect_all()
