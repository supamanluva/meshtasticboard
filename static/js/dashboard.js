/**
 * Meshtastic Dashboard – Frontend
 * Handles tabs, map, topology graph, traceroute, statistics, messaging,
 * theme toggle, notifications, and device remote config.
 */

(() => {
"use strict";

// ── Socket & State ──────────────────────────────────────────────────────

const socket = io();
let devicesData = [];
let nodesData = [];
let messagesData = [];
let meshMap = null;
let mapMarkers = {};
let trMap = null;
let trMarkers = [];
let topoSim = null;
let charts = {};
let notificationsEnabled = false;
const REFRESH_INTERVAL = 30000;

// ── Theme ───────────────────────────────────────────────────────────────

function initTheme() {
    const saved = localStorage.getItem("theme") || "dark";
    applyTheme(saved);
    document.getElementById("btn-theme").addEventListener("click", () => {
        const current = document.documentElement.getAttribute("data-theme");
        const next = current === "dark" ? "light" : "dark";
        applyTheme(next);
        localStorage.setItem("theme", next);
    });
}

function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const iconDark = document.getElementById("theme-icon-dark");
    const iconLight = document.getElementById("theme-icon-light");
    iconDark.style.display = theme === "dark" ? "block" : "none";
    iconLight.style.display = theme === "light" ? "block" : "none";
    // Update Chart.js colors if charts exist
    updateChartColors();
}

function getChartColors() {
    const style = getComputedStyle(document.documentElement);
    return {
        text: style.getPropertyValue("--text-secondary").trim(),
        grid: style.getPropertyValue("--border").trim(),
        accent: style.getPropertyValue("--accent").trim(),
        green: style.getPropertyValue("--green").trim(),
        orange: style.getPropertyValue("--orange").trim(),
        red: style.getPropertyValue("--red").trim(),
        purple: style.getPropertyValue("--purple").trim(),
        cyan: style.getPropertyValue("--cyan").trim(),
        pink: style.getPropertyValue("--pink").trim(),
        bg: style.getPropertyValue("--bg-primary").trim(),
    };
}

function updateChartColors() {
    // Rebuild charts with new colors on theme change
    if (document.querySelector(".tab-btn.active")?.dataset.tab === "tab-stats") {
        loadStats();
    }
}

// ── Notifications ───────────────────────────────────────────────────────

function initNotifications() {
    const btn = document.getElementById("btn-notif");
    const dot = document.getElementById("notif-dot");

    // Restore from localStorage
    notificationsEnabled = localStorage.getItem("notifications") === "true";
    dot.style.display = notificationsEnabled ? "inline-block" : "none";

    btn.addEventListener("click", async () => {
        if (!notificationsEnabled) {
            if ("Notification" in window && Notification.permission !== "granted") {
                await Notification.requestPermission();
            }
            notificationsEnabled = true;
            localStorage.setItem("notifications", "true");
            dot.style.display = "inline-block";
            showToast("Notifications enabled", "success");
        } else {
            notificationsEnabled = false;
            localStorage.setItem("notifications", "false");
            dot.style.display = "none";
            showToast("Notifications disabled", "info");
        }
    });
}

function notifyMessage(msg) {
    if (!notificationsEnabled) return;

    // Play sound
    try {
        const audio = document.getElementById("notif-sound");
        if (audio) { audio.currentTime = 0; audio.play().catch(() => {}); }
    } catch (e) {}

    // Browser notification
    if ("Notification" in window && Notification.permission === "granted") {
        const n = new Notification("Meshtastic Message", {
            body: `${msg.from}: ${msg.text}`,
            icon: "/static/img/icon.png",
            tag: "mesh-msg-" + msg.id,
        });
        setTimeout(() => n.close(), 5000);
    }

    // Toast notification
    const preview = msg.text.length > 50 ? msg.text.substring(0, 50) + "…" : msg.text;
    showToast(`${msg.from}: ${preview}`, "message");
}

// ── Tabs ────────────────────────────────────────────────────────────────

function initTabs() {
    const btns = document.querySelectorAll(".tab-btn");
    const tabs = document.querySelectorAll(".tab-content");
    btns.forEach(btn => {
        btn.addEventListener("click", () => {
            btns.forEach(b => b.classList.remove("active"));
            tabs.forEach(t => t.classList.remove("active"));
            btn.classList.add("active");
            const tabId = btn.dataset.tab;
            document.getElementById(tabId).classList.add("active");

            // Lazy init
            if (tabId === "tab-map" && meshMap) {
                setTimeout(() => meshMap.invalidateSize(), 100);
            }
            if (tabId === "tab-topology") {
                loadTopology();
            }
            if (tabId === "tab-stats") {
                loadStats();
            }
            if (tabId === "tab-traceroute") {
                populateTracerouteSelects();
                if (trMap) setTimeout(() => trMap.invalidateSize(), 100);
            }
            if (tabId === "tab-config") {
                populateConfigDeviceSelect();
            }
        });
    });
}

// ── Toast ───────────────────────────────────────────────────────────────

function showToast(text, type = "info") {
    const container = document.getElementById("toast-container");
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.textContent = text;
    container.appendChild(el);
    setTimeout(() => { el.remove(); }, 4000);
}

// ── Fetch Helpers ───────────────────────────────────────────────────────

async function fetchJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
}

async function postJSON(url, data) {
    const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
    });
    return res.json();
}

// ── Data Loading ────────────────────────────────────────────────────────

async function loadAll() {
    try {
        const [devices, messages] = await Promise.all([
            fetchJSON("/api/devices"),
            fetchJSON("/api/messages"),
        ]);
        devicesData = devices;
        messagesData = messages;

        // Collect all nodes
        nodesData = [];
        const seen = new Set();
        for (const d of devices) {
            if (d.nodes) {
                for (const n of d.nodes) {
                    if (!seen.has(n.id)) {
                        seen.add(n.id);
                        n.seenBy = d.name;
                        nodesData.push(n);
                    }
                }
            }
        }

        renderDevices(devices);
        renderNodes(nodesData);
        renderMessages(messages);
        updateMap(nodesData);
        updateConnectionStatus(devices);
        populateDeviceSelects(devices);
    } catch (e) {
        console.error("Failed to load data:", e);
        showToast("Failed to load data: " + e.message, "error");
    }
}

function updateConnectionStatus(devices) {
    const el = document.getElementById("connection-status");
    const allConnected = devices.every(d => d.connected);
    const anyConnected = devices.some(d => d.connected);
    if (allConnected && devices.length > 0) {
        el.textContent = `${devices.length} devices connected`;
        el.className = "badge badge-online";
    } else if (anyConnected) {
        el.textContent = "Partial connection";
        el.className = "badge badge-online";
    } else {
        el.textContent = "Disconnected";
        el.className = "badge badge-offline";
    }
}

// ── Render: Devices ─────────────────────────────────────────────────────

function renderDevices(devices) {
    const container = document.getElementById("devices-container");
    if (!devices.length) {
        container.innerHTML = '<p class="muted">No devices configured.</p>';
        return;
    }
    const html = devices.map(d => {
        const displayName = d.deviceName || d.name;
        const subtitle = d.deviceName && d.deviceName !== d.name
            ? `<span class="device-card-subtitle">(config: ${d.name})</span>` : "";
        if (!d.connected) {
            return `<div class="device-card">
                <div class="device-card-header">
                    <h3>${displayName} ${subtitle}</h3>
                    <span class="badge badge-offline">Offline</span>
                </div>
                <div class="device-meta"><span>Error: <strong>${d.error || "unknown"}</strong></span></div>
                <div class="device-actions">
                    <button class="btn btn-sm btn-reconnect" onclick="reconnectDevice('${d.name}')">&#x21bb; Reconnect</button>
                </div>
            </div>`;
        }
        const info = d.myInfo || {};
        return `<div class="device-card">
            <div class="device-card-header">
                <h3>${displayName} ${subtitle}</h3>
                <span class="badge badge-online">Online</span>
            </div>
            <div class="device-meta">
                <span>Node#: <strong>${info.my_node_num || "?"}</strong></span>
                <span>FW: <strong>${info.firmware_version || "?"}</strong></span>
                <span>HW: <strong>${info.hw_model || "?"}</strong></span>
                <span>Nodes: <strong>${info.num_online_nodes ?? "?"}</strong></span>
                <span>Host: <strong>${d.host}:${d.port}</strong></span>
                ${info.shortName ? `<span>Short: <strong>${info.shortName}</strong></span>` : ""}
                ${info.reboot_count ? `<span>Reboots: <strong>${info.reboot_count}</strong></span>` : ""}
            </div>
            <div class="device-actions">
                <button class="btn btn-sm btn-disconnect" onclick="disconnectDevice('${d.name}')">&#x2716; Disconnect</button>
                <button class="btn btn-sm btn-reconnect" onclick="reconnectDevice('${d.name}')">&#x21bb; Reconnect</button>
            </div>
        </div>`;
    }).join("");
    container.innerHTML = `<div class="device-cards">${html}</div>`;

    // Render channels
    renderChannels(devices);
}

async function disconnectDevice(name) {
    try {
        const res = await fetch("/api/disconnect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ device: name }),
        });
        const data = await res.json();
        if (res.ok) {
            showToast(`Disconnected from ${name}`, "info");
            loadDevices();
        } else {
            showToast(`Disconnect failed: ${data.error}`, "error");
        }
    } catch (e) {
        showToast(`Disconnect error: ${e.message}`, "error");
    }
}

async function reconnectDevice(name) {
    try {
        showToast(`Reconnecting to ${name}…`, "info");
        const res = await fetch("/api/reconnect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ device: name }),
        });
        const data = await res.json();
        if (res.ok && data.status === "connected") {
            showToast(`Reconnected to ${name}`, "success");
            loadDevices();
        } else {
            showToast(`Reconnect failed: ${data.error || data.status}`, "error");
        }
    } catch (e) {
        showToast(`Reconnect error: ${e.message}`, "error");
    }
}

function renderChannels(devices) {
    const container = document.getElementById("channels-container");
    const allChannels = [];
    for (const d of devices) {
        if (d.channels) {
            for (const ch of d.channels) {
                allChannels.push({ ...ch, device: d.deviceName || d.name });
            }
        }
    }
    if (!allChannels.length) {
        container.innerHTML = '<p class="muted">No channels.</p>';
        return;
    }
    const html = allChannels.map(ch =>
        `<div class="channel-item">
            <span class="channel-idx">${ch.index}</span>
            <span class="channel-name">${ch.name || "(default)"}</span>
            <span class="channel-role">${ch.role}</span>
            <span class="channel-device">${ch.device}</span>
        </div>`
    ).join("");
    container.innerHTML = `<div class="channel-list">${html}</div>`;
}

// ── Render: Nodes ───────────────────────────────────────────────────────

function renderNodes(nodes) {
    const body = document.getElementById("nodes-body");
    document.getElementById("node-count").textContent = nodes.length;
    document.getElementById("node-count-header").textContent = nodes.length;

    if (!nodes.length) {
        body.innerHTML = '<tr><td colspan="11" class="muted">No nodes found.</td></tr>';
        return;
    }

    // Sort: most recently heard first
    const sorted = [...nodes].sort((a, b) => (b.lastHeard || 0) - (a.lastHeard || 0));

    body.innerHTML = sorted.map(n => {
        const bat = n.batteryLevel != null
            ? `<div class="battery-bar"><div class="battery-bar-fill" style="width:${n.batteryLevel}%;background:${n.batteryLevel > 50 ? "var(--green)" : n.batteryLevel > 20 ? "var(--orange)" : "var(--red)"}"></div></div>${n.batteryLevel}%`
            : "—";
        const pos = n.latitude != null && n.longitude != null
            ? `${n.latitude.toFixed(4)}, ${n.longitude.toFixed(4)}` : "—";
        const snr = n.snr != null ? `${n.snr.toFixed(1)} dB` : "—";
        const hops = n.hopsAway != null ? n.hopsAway : "—";
        return `<tr>
            <td>${n.longName}</td>
            <td>${n.shortName}</td>
            <td class="mono">${n.id}</td>
            <td>${n.hwModel || "—"}</td>
            <td>${n.role || "—"}</td>
            <td>${bat}</td>
            <td>${snr}</td>
            <td>${hops}</td>
            <td class="mono">${pos}</td>
            <td>${n.lastHeardStr}</td>
            <td>${n.seenBy || "—"}</td>
        </tr>`;
    }).join("");
}

// Node search
function initNodeSearch() {
    const input = document.getElementById("node-search");
    if (!input) return;
    input.addEventListener("input", () => {
        const q = input.value.toLowerCase();
        const filtered = nodesData.filter(n =>
            (n.longName || "").toLowerCase().includes(q) ||
            (n.shortName || "").toLowerCase().includes(q) ||
            (n.id || "").toLowerCase().includes(q) ||
            (n.hwModel || "").toLowerCase().includes(q)
        );
        renderNodes(filtered);
    });
}

// ── Device Selects ──────────────────────────────────────────────────────

function populateDeviceSelects(devices) {
    const selects = ["send-device", "tr-device", "config-device-select"];
    selects.forEach(id => {
        const sel = document.getElementById(id);
        if (!sel) return;
        const val = sel.value;
        sel.innerHTML = '<option value="">Select device…</option>';
        devices.filter(d => d.connected).forEach(d => {
            const opt = document.createElement("option");
            opt.value = d.name;
            opt.textContent = d.deviceName || d.name;
            sel.appendChild(opt);
        });
        if (val) sel.value = val;
        // Auto-select if only one
        if (devices.filter(d => d.connected).length === 1) {
            sel.value = devices.find(d => d.connected).name;
        }
    });

    // Populate destination/channel selects for messaging
    const dest = document.getElementById("send-destination");
    if (dest) {
        const curVal = dest.value;
        dest.innerHTML = '<option value="^all">All (broadcast)</option>';
        nodesData.forEach(n => {
            if (n.id) {
                const opt = document.createElement("option");
                opt.value = n.id;
                opt.textContent = `${n.longName} (${n.id})`;
                dest.appendChild(opt);
            }
        });
        if (curVal) dest.value = curVal;
    }

    const chanSel = document.getElementById("send-channel");
    if (chanSel) {
        chanSel.innerHTML = "";
        const seen = new Set();
        for (const d of devices) {
            if (d.channels) {
                for (const ch of d.channels) {
                    if (!seen.has(ch.index)) {
                        seen.add(ch.index);
                        const opt = document.createElement("option");
                        opt.value = ch.index;
                        opt.textContent = ch.name ? `Ch ${ch.index}: ${ch.name}` : `Ch ${ch.index}`;
                        chanSel.appendChild(opt);
                    }
                }
            }
        }
        if (chanSel.options.length === 0) {
            chanSel.innerHTML = '<option value="0">Ch 0</option>';
        }
    }
}

// ── Map ─────────────────────────────────────────────────────────────────

function initMap() {
    meshMap = L.map("mesh-map", {
        center: [63.9, 19.76],
        zoom: 6,
        zoomControl: true,
    });
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    addTileLayer(meshMap, isDark);
}

function addTileLayer(map, dark) {
    // Remove old tiles
    map.eachLayer(l => { if (l instanceof L.TileLayer) map.removeLayer(l); });
    if (dark) {
        L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
            subdomains: "abcd",
            maxZoom: 19,
        }).addTo(map);
    } else {
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
            maxZoom: 19,
        }).addTo(map);
    }
}

function updateMap(nodes) {
    if (!meshMap) return;

    // Remove old markers
    Object.values(mapMarkers).forEach(m => meshMap.removeLayer(m));
    mapMarkers = {};

    const withPos = nodes.filter(n => n.latitude != null && n.longitude != null);
    const legend = document.getElementById("map-node-list");

    if (!withPos.length) {
        legend.innerHTML = '<p class="muted">No nodes with GPS position.</p>';
        return;
    }

    const bounds = [];
    let legendHtml = "";

    withPos.forEach(n => {
        const latlng = [n.latitude, n.longitude];
        bounds.push(latlng);

        const marker = L.circleMarker(latlng, {
            radius: 7,
            fillColor: "#58a6ff",
            color: "#1f6feb",
            weight: 2,
            opacity: 0.9,
            fillOpacity: 0.75,
        }).addTo(meshMap);

        const bat = n.batteryLevel != null ? `${n.batteryLevel}%` : "—";
        const snr = n.snr != null ? `${n.snr.toFixed(1)} dB` : "—";
        marker.bindPopup(`
            <strong>${n.longName}</strong> (${n.shortName})<br>
            ID: ${n.id}<br>
            HW: ${n.hwModel || "?"}<br>
            Battery: ${bat}<br>
            SNR: ${snr}<br>
            Position: ${n.latitude.toFixed(5)}, ${n.longitude.toFixed(5)}<br>
            Last heard: ${n.lastHeardStr}
        `);

        mapMarkers[n.id] = marker;

        legendHtml += `
            <div class="map-node-item" data-id="${n.id}">
                <span class="map-node-dot"></span>
                <div>
                    <div class="map-node-name">${n.longName}</div>
                    <div class="map-node-coords">${n.latitude.toFixed(4)}, ${n.longitude.toFixed(4)}</div>
                </div>
            </div>`;
    });

    legend.innerHTML = legendHtml;

    // Click legend item to fly to node
    legend.querySelectorAll(".map-node-item").forEach(item => {
        item.addEventListener("click", () => {
            const id = item.dataset.id;
            const m = mapMarkers[id];
            if (m) {
                meshMap.flyTo(m.getLatLng(), 14);
                m.openPopup();
            }
        });
    });

    if (bounds.length) {
        meshMap.fitBounds(bounds, { padding: [40, 40], maxZoom: 12 });
    }
}

// ── Messages ────────────────────────────────────────────────────────────

function renderMessages(msgs) {
    const list = document.getElementById("messages-list");
    document.getElementById("msg-count").textContent = msgs.length;
    document.getElementById("msg-count-header").textContent = msgs.length;

    if (!msgs.length) {
        list.innerHTML = '<p class="muted">No messages yet.</p>';
        return;
    }

    list.innerHTML = msgs.map(m => {
        const isSent = m.sent || m.from === "local";
        const cls = isSent ? "msg-out" : "msg-in";
        const ts = m.timestamp ? new Date(m.timestamp).toLocaleTimeString() : "";
        const fromLabel = isSent ? `You (${m.device})` : resolveNodeName(m.from);
        const toLabel = m.to === "^all" ? "broadcast" : resolveNodeName(m.to);
        return `<div class="msg-bubble ${cls}">
            <div>${escapeHtml(m.text)}</div>
            <div class="msg-meta">${fromLabel} → ${toLabel} · ${ts}${m.rxSnr ? ` · SNR: ${m.rxSnr}` : ""}</div>
        </div>`;
    }).join("");

    list.scrollTop = list.scrollHeight;
}

function appendMessage(msg) {
    messagesData.push(msg);
    renderMessages(messagesData);
}

function resolveNodeName(id) {
    if (!id) return "?";
    const node = nodesData.find(n => n.id === id);
    return node ? node.longName : id;
}

function escapeHtml(text) {
    const d = document.createElement("div");
    d.textContent = text;
    return d.innerHTML;
}

function initSendForm() {
    const form = document.getElementById("send-form");
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const device = document.getElementById("send-device").value;
        const text = document.getElementById("send-text").value.trim();
        const dest = document.getElementById("send-destination").value;
        const ch = parseInt(document.getElementById("send-channel").value) || 0;

        if (!device) { showToast("Select a device first", "error"); return; }
        if (!text) return;

        try {
            const res = await postJSON("/api/send", {
                device, text, destination: dest, channelIndex: ch,
            });
            if (res.error) throw new Error(res.error);
            document.getElementById("send-text").value = "";
        } catch (e) {
            showToast("Send failed: " + e.message, "error");
        }
    });
}

// ── Topology Graph (D3 Force) ───────────────────────────────────────────

async function loadTopology() {
    const container = document.getElementById("topology-graph");
    try {
        const data = await fetchJSON("/api/topology");
        renderTopologyGraph(container, data);
    } catch (e) {
        container.innerHTML = `<p class="muted" style="padding:2rem">Failed to load topology: ${e.message}</p>`;
    }
}

function renderTopologyGraph(container, data) {
    container.innerHTML = "";
    const width = container.clientWidth || 800;
    const height = container.clientHeight || 550;
    const showLabels = document.getElementById("topo-labels")?.checked ?? true;

    const svg = d3.select(container).append("svg")
        .attr("width", width)
        .attr("height", height)
        .attr("viewBox", [0, 0, width, height]);

    // Add zoom
    const g = svg.append("g");
    svg.call(d3.zoom().scaleExtent([0.2, 5]).on("zoom", (event) => {
        g.attr("transform", event.transform);
    }));

    const nodes = data.nodes || [];
    const edges = data.edges || [];

    if (!nodes.length) {
        container.innerHTML = '<p class="muted" style="padding:2rem">No topology data yet. As packets are received, connections will appear.</p>';
        return;
    }

    // Build index
    const nodeMap = {};
    nodes.forEach((n, i) => { n.index = i; nodeMap[n.id] = n; });

    // Build links (only where both nodes exist)
    const links = [];
    edges.forEach(e => {
        const source = nodeMap[e.from];
        const target = nodeMap[e.to];
        if (source && target) {
            links.push({ source: source.index, target: target.index, snr: e.snr, rssi: e.rssi });
        }
    });

    // Color by SNR
    function edgeColor(snr) {
        if (snr == null) return "#6e7681";
        if (snr > 0) return "#3fb950";
        if (snr > -10) return "#d29922";
        return "#f85149";
    }

    // Force simulation
    const simulation = d3.forceSimulation(nodes)
        .force("link", d3.forceLink(links).distance(100).strength(0.5))
        .force("charge", d3.forceManyBody().strength(-200))
        .force("center", d3.forceCenter(width / 2, height / 2))
        .force("collision", d3.forceCollide().radius(25));

    // Draw edges
    const link = g.append("g").selectAll("line")
        .data(links).enter().append("line")
        .attr("class", "topo-edge")
        .attr("stroke", d => edgeColor(d.snr))
        .attr("stroke-width", 2)
        .on("click", (event, d) => {
            const info = document.getElementById("topo-info-content");
            info.innerHTML = `
                <div class="info-row"><span class="info-label">From</span><span class="info-value">${nodes[d.source.index]?.longName || d.source.id || "?"}</span></div>
                <div class="info-row"><span class="info-label">To</span><span class="info-value">${nodes[d.target.index]?.longName || d.target.id || "?"}</span></div>
                <div class="info-row"><span class="info-label">SNR</span><span class="info-value">${d.snr != null ? d.snr + " dB" : "—"}</span></div>
                <div class="info-row"><span class="info-label">RSSI</span><span class="info-value">${d.rssi != null ? d.rssi + " dBm" : "—"}</span></div>
            `;
        });

    // Draw nodes
    const node = g.append("g").selectAll("g")
        .data(nodes).enter().append("g")
        .attr("class", "topo-node")
        .call(d3.drag()
            .on("start", (event, d) => {
                if (!event.active) simulation.alphaTarget(0.3).restart();
                d.fx = d.x; d.fy = d.y;
            })
            .on("drag", (event, d) => { d.fx = event.x; d.fy = event.y; })
            .on("end", (event, d) => {
                if (!event.active) simulation.alphaTarget(0);
                d.fx = null; d.fy = null;
            })
        );

    node.append("circle")
        .attr("r", d => d.isLocal ? 10 : 7)
        .attr("fill", d => d.isLocal ? "#58a6ff" : "#bc8cff")
        .attr("stroke", d => d.isLocal ? "#1f6feb" : "#8b5cf6")
        .attr("stroke-width", 2)
        .on("click", (event, d) => {
            const info = document.getElementById("topo-info-content");
            const conns = links.filter(l =>
                (l.source.index === d.index || l.target.index === d.index)
            );
            info.innerHTML = `
                <div class="info-row"><span class="info-label">Name</span><span class="info-value">${d.longName}</span></div>
                <div class="info-row"><span class="info-label">Short</span><span class="info-value">${d.shortName}</span></div>
                <div class="info-row"><span class="info-label">ID</span><span class="info-value">${d.id}</span></div>
                <div class="info-row"><span class="info-label">Local</span><span class="info-value">${d.isLocal ? "Yes" : "No"}</span></div>
                <div class="info-row"><span class="info-label">Connections</span><span class="info-value">${conns.length}</span></div>
            `;
        });

    if (showLabels) {
        node.append("text")
            .attr("dy", -14)
            .text(d => d.shortName || d.longName?.substring(0, 8) || "?");
    }

    simulation.on("tick", () => {
        link
            .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
            .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
        node.attr("transform", d => `translate(${d.x},${d.y})`);
    });

    topoSim = simulation;
}

// Topology controls
function initTopologyControls() {
    document.getElementById("btn-topo-refresh")?.addEventListener("click", loadTopology);
    document.getElementById("topo-labels")?.addEventListener("change", loadTopology);
}

// ── Traceroute ──────────────────────────────────────────────────────────

function populateTracerouteSelects() {
    const dest = document.getElementById("tr-destination");
    if (!dest) return;
    const curVal = dest.value;
    dest.innerHTML = '<option value="">Select destination…</option>';
    nodesData.forEach(n => {
        if (n.id) {
            const opt = document.createElement("option");
            opt.value = n.id;
            opt.textContent = `${n.longName} (${n.id})`;
            dest.appendChild(opt);
        }
    });
    if (curVal) dest.value = curVal;
}

function initTraceroute() {
    const form = document.getElementById("traceroute-form");
    form?.addEventListener("submit", async (e) => {
        e.preventDefault();
        const device = document.getElementById("tr-device").value;
        const destination = document.getElementById("tr-destination").value;

        if (!device) { showToast("Select a device", "error"); return; }
        if (!destination) { showToast("Select a destination", "error"); return; }

        showToast("Sending traceroute…", "info");

        try {
            const res = await postJSON("/api/traceroute", { device, destination });
            if (res.error) throw new Error(res.error);

            addTracerouteResult({
                id: res.id,
                device,
                destination,
                status: "pending",
                route: [],
            });

            // Poll for result
            pollTraceroute(res.id);
        } catch (e) {
            showToast("Traceroute failed: " + e.message, "error");
        }
    });

    // Init traceroute map
    if (!trMap && document.getElementById("traceroute-map")) {
        trMap = L.map("traceroute-map", {
            center: [63.9, 19.76],
            zoom: 6,
        });
        const isDark = document.documentElement.getAttribute("data-theme") === "dark";
        addTileLayer(trMap, isDark);
    }
}

function addTracerouteResult(tr) {
    const container = document.getElementById("traceroute-results");
    const destName = resolveNodeName(tr.destination);
    const el = document.createElement("div");
    el.className = "tr-result";
    el.id = `tr-${tr.id}`;
    el.innerHTML = `
        <div class="tr-result-header">
            <strong>→ ${destName}</strong>
            <span class="badge badge-pending" id="tr-status-${tr.id}">Pending…</span>
        </div>
        <div class="tr-hop-list" id="tr-hops-${tr.id}">
            <span class="muted">Waiting for response…</span>
        </div>
    `;
    container.prepend(el);
}

async function pollTraceroute(id) {
    let attempts = 0;
    const maxAttempts = 30;
    const interval = setInterval(async () => {
        attempts++;
        try {
            const res = await fetchJSON(`/api/traceroute/${id}`);
            if (res.status === "complete") {
                clearInterval(interval);
                updateTracerouteResult(id, res);
            } else if (res.status === "error" || attempts >= maxAttempts) {
                clearInterval(interval);
                const statusEl = document.getElementById(`tr-status-${id}`);
                if (statusEl) {
                    statusEl.textContent = "Timeout";
                    statusEl.className = "badge badge-offline";
                }
                const hopsEl = document.getElementById(`tr-hops-${id}`);
                if (hopsEl) hopsEl.innerHTML = '<span class="muted">No response received (node may be out of range)</span>';
            }
        } catch (e) {
            clearInterval(interval);
        }
    }, 2000);
}

function updateTracerouteResult(id, tr) {
    const statusEl = document.getElementById(`tr-status-${id}`);
    if (statusEl) {
        statusEl.textContent = "Complete";
        statusEl.className = "badge badge-complete";
    }

    const hopsEl = document.getElementById(`tr-hops-${id}`);
    if (hopsEl && tr.route && tr.route.length) {
        const hops = tr.route.map(hop => {
            const name = resolveNodeName(hop);
            return `<span class="tr-hop">${name}</span>`;
        });
        // Add source and destination
        const srcName = resolveNodeName(tr.device || "You");
        const destName = resolveNodeName(tr.destination);
        const fullPath = [`<span class="tr-hop">${srcName}</span>`];
        hops.forEach(h => { fullPath.push('<span class="tr-hop-arrow">→</span>'); fullPath.push(h); });
        fullPath.push('<span class="tr-hop-arrow">→</span>');
        fullPath.push(`<span class="tr-hop">${destName}</span>`);
        hopsEl.innerHTML = fullPath.join("");
    } else if (hopsEl) {
        hopsEl.innerHTML = `<span class="tr-hop">${resolveNodeName(tr.destination)}</span> <span class="muted">(direct, no hops)</span>`;
    }

    // Draw on traceroute map
    drawTracerouteOnMap(tr);
    showToast("Traceroute complete!", "success");
}

function drawTracerouteOnMap(tr) {
    if (!trMap) return;

    // Clear old markers/lines
    trMarkers.forEach(m => trMap.removeLayer(m));
    trMarkers = [];

    // Collect positions for all hops
    const allIds = [tr.destination, ...(tr.route || [])];
    const positions = [];
    allIds.forEach(id => {
        const node = nodesData.find(n => n.id === id);
        if (node && node.latitude != null && node.longitude != null) {
            positions.push({ lat: node.latitude, lng: node.longitude, name: node.longName, id });
        }
    });

    if (positions.length < 1) return;

    positions.forEach((pos, i) => {
        const marker = L.circleMarker([pos.lat, pos.lng], {
            radius: 8,
            fillColor: i === 0 ? "#58a6ff" : "#bc8cff",
            color: "#fff",
            weight: 2,
            fillOpacity: 0.9,
        }).addTo(trMap);
        marker.bindPopup(`<strong>${pos.name}</strong><br>${pos.id}`);
        trMarkers.push(marker);
    });

    if (positions.length >= 2) {
        const line = L.polyline(positions.map(p => [p.lat, p.lng]), {
            color: "#58a6ff",
            weight: 3,
            dashArray: "8 4",
        }).addTo(trMap);
        trMarkers.push(line);
    }

    trMap.fitBounds(positions.map(p => [p.lat, p.lng]), { padding: [30, 30] });
}

// ── Statistics ──────────────────────────────────────────────────────────

async function loadStats() {
    try {
        const [nodes, summary] = await Promise.all([
            fetchJSON("/api/nodes"),
            fetchJSON("/api/stats/summary"),
        ]);
        renderStatsOverview(nodes);
        renderStatsCharts(nodes, summary);
        populateStatsNodeSelect(nodes);
    } catch (e) {
        console.error("Stats error:", e);
    }
}

function renderStatsOverview(nodes) {
    const now = Date.now() / 1000;
    const online = nodes.filter(n => n.lastHeard && (now - n.lastHeard) < 900);
    const withGps = nodes.filter(n => n.latitude != null && n.longitude != null);
    const snrs = nodes.filter(n => n.snr != null).map(n => n.snr);
    const avgSnr = snrs.length ? (snrs.reduce((a, b) => a + b, 0) / snrs.length).toFixed(1) : "—";

    document.getElementById("stat-total-nodes").textContent = nodes.length;
    document.getElementById("stat-online-nodes").textContent = online.length;
    document.getElementById("stat-with-gps").textContent = withGps.length;
    document.getElementById("stat-avg-snr").textContent = avgSnr !== "—" ? avgSnr + " dB" : "—";
}

function renderStatsCharts(nodes, summary) {
    const colors = getChartColors();

    // Destroy old charts
    Object.values(charts).forEach(c => c.destroy());
    charts = {};

    // Battery chart - bar chart of nodes with battery
    const batNodes = nodes.filter(n => n.batteryLevel != null).sort((a, b) => a.batteryLevel - b.batteryLevel);
    if (batNodes.length) {
        charts.battery = new Chart(document.getElementById("chart-battery"), {
            type: "bar",
            data: {
                labels: batNodes.map(n => n.shortName || n.longName?.substring(0, 8)),
                datasets: [{
                    label: "Battery %",
                    data: batNodes.map(n => n.batteryLevel),
                    backgroundColor: batNodes.map(n =>
                        n.batteryLevel > 50 ? colors.green : n.batteryLevel > 20 ? colors.orange : colors.red
                    ),
                    borderRadius: 3,
                }],
            },
            options: chartOptions(colors, "Battery Level (%)"),
        });
    }

    // SNR distribution - histogram
    const snrNodes = nodes.filter(n => n.snr != null);
    if (snrNodes.length) {
        const bins = {};
        snrNodes.forEach(n => {
            const bin = Math.round(n.snr / 2) * 2;
            bins[bin] = (bins[bin] || 0) + 1;
        });
        const sortedBins = Object.keys(bins).map(Number).sort((a, b) => a - b);
        charts.snr = new Chart(document.getElementById("chart-snr"), {
            type: "bar",
            data: {
                labels: sortedBins.map(b => `${b} dB`),
                datasets: [{
                    label: "Nodes",
                    data: sortedBins.map(b => bins[b]),
                    backgroundColor: sortedBins.map(b =>
                        b > 0 ? colors.green : b > -10 ? colors.orange : colors.red
                    ),
                    borderRadius: 3,
                }],
            },
            options: chartOptions(colors, "SNR Distribution"),
        });
    }

    // Channel utilization
    const chUtilNodes = nodes.filter(n => n.channelUtilization != null);
    if (chUtilNodes.length) {
        charts.chUtil = new Chart(document.getElementById("chart-channel-util"), {
            type: "bar",
            data: {
                labels: chUtilNodes.map(n => n.shortName || n.longName?.substring(0, 8)),
                datasets: [{
                    label: "Ch Util %",
                    data: chUtilNodes.map(n => n.channelUtilization?.toFixed(1)),
                    backgroundColor: colors.cyan,
                    borderRadius: 3,
                }],
            },
            options: chartOptions(colors, "Channel Utilization (%)"),
        });
    }

    // HW Models - pie chart
    const hwCounts = {};
    nodes.forEach(n => {
        const hw = n.hwModel || "Unknown";
        hwCounts[hw] = (hwCounts[hw] || 0) + 1;
    });
    const hwLabels = Object.keys(hwCounts).sort((a, b) => hwCounts[b] - hwCounts[a]);
    const pieColors = [colors.accent, colors.green, colors.orange, colors.purple, colors.pink, colors.cyan, colors.red, "#6e7681"];
    charts.hwModels = new Chart(document.getElementById("chart-hw-models"), {
        type: "doughnut",
        data: {
            labels: hwLabels,
            datasets: [{
                data: hwLabels.map(h => hwCounts[h]),
                backgroundColor: hwLabels.map((_, i) => pieColors[i % pieColors.length]),
                borderWidth: 0,
            }],
        },
        options: {
            responsive: true,
            plugins: {
                legend: {
                    position: "right",
                    labels: { color: colors.text, font: { size: 11 } },
                },
            },
        },
    });
}

function chartOptions(colors, title) {
    return {
        responsive: true,
        plugins: {
            legend: { display: false },
            title: { display: false },
        },
        scales: {
            x: {
                ticks: { color: colors.text, font: { size: 10 } },
                grid: { color: colors.grid },
            },
            y: {
                ticks: { color: colors.text },
                grid: { color: colors.grid },
                beginAtZero: true,
            },
        },
    };
}

function populateStatsNodeSelect(nodes) {
    const sel = document.getElementById("stats-node-select");
    const curVal = sel.value;
    sel.innerHTML = '<option value="">All nodes overview</option>';
    nodes.forEach(n => {
        const opt = document.createElement("option");
        opt.value = n.id;
        opt.textContent = `${n.longName} (${n.id})`;
        sel.appendChild(opt);
    });
    if (curVal) sel.value = curVal;
}

function initStatsNodeSelect() {
    const sel = document.getElementById("stats-node-select");
    sel?.addEventListener("change", async () => {
        const nodeId = sel.value;
        const detailDiv = document.getElementById("stats-node-detail");
        if (!nodeId) {
            detailDiv.style.display = "none";
            return;
        }
        detailDiv.style.display = "grid";

        try {
            const history = await fetchJSON(`/api/stats/${encodeURIComponent(nodeId)}`);
            renderNodeHistoryCharts(history);
        } catch (e) {
            console.error("Node stats error:", e);
        }
    });
}

function renderNodeHistoryCharts(history) {
    const colors = getChartColors();

    if (charts.nodeBattery) charts.nodeBattery.destroy();
    if (charts.nodeChUtil) charts.nodeChUtil.destroy();

    if (!history.length) {
        document.getElementById("chart-node-battery").parentElement.innerHTML = '<h3>Battery Over Time</h3><p class="muted">No history data for this node.</p>';
        return;
    }

    const labels = history.map(h => new Date(h.timestamp).toLocaleTimeString());

    charts.nodeBattery = new Chart(document.getElementById("chart-node-battery"), {
        type: "line",
        data: {
            labels,
            datasets: [{
                label: "Battery %",
                data: history.map(h => h.batteryLevel),
                borderColor: colors.green,
                backgroundColor: colors.green + "22",
                fill: true,
                tension: 0.3,
                pointRadius: 2,
            }],
        },
        options: chartOptions(colors, "Battery"),
    });

    charts.nodeChUtil = new Chart(document.getElementById("chart-node-battery"), {
        type: "line",
        data: {
            labels,
            datasets: [{
                label: "Channel Util %",
                data: history.map(h => h.channelUtilization),
                borderColor: colors.cyan,
                backgroundColor: colors.cyan + "22",
                fill: true,
                tension: 0.3,
                pointRadius: 2,
            }],
        },
        options: chartOptions(colors, "Channel Utilization"),
    });
}

// ── Remote Config ───────────────────────────────────────────────────────

function populateConfigDeviceSelect() {
    // Already populated by populateDeviceSelects
}

function initConfig() {
    document.getElementById("btn-load-config")?.addEventListener("click", loadDeviceConfig);
    document.getElementById("btn-save-owner")?.addEventListener("click", saveOwner);
    document.getElementById("btn-save-position")?.addEventListener("click", savePosition);
    document.getElementById("btn-remove-position")?.addEventListener("click", removePosition);
}

async function loadDeviceConfig() {
    const device = document.getElementById("config-device-select").value;
    if (!device) { showToast("Select a device first", "error"); return; }

    try {
        const config = await fetchJSON(`/api/config/${encodeURIComponent(device)}`);
        document.getElementById("config-forms").style.display = "block";
        document.getElementById("config-container").innerHTML = "";

        // Owner
        if (config.owner) {
            document.getElementById("cfg-long-name").value = config.owner.longName || "";
            document.getElementById("cfg-short-name").value = config.owner.shortName || "";
        }

        // LoRa
        if (config.lora) {
            const lora = config.lora;
            document.getElementById("lora-info").innerHTML = Object.entries(lora).map(([k, v]) =>
                `<div class="config-info-row"><span class="label">${k}</span><span class="value">${v}</span></div>`
            ).join("");
        } else {
            document.getElementById("lora-info").innerHTML = '<span class="muted">Not available</span>';
        }

        // Channels
        if (config.channels && config.channels.length) {
            document.getElementById("channel-info").innerHTML = config.channels.map(ch =>
                `<div class="config-info-row"><span class="label">Ch ${ch.index}</span><span class="value">${ch.name || "(default)"} (${ch.role})</span></div>`
            ).join("");
        } else {
            document.getElementById("channel-info").innerHTML = '<span class="muted">No channels</span>';
        }

        // Position config
        if (config.position) {
            document.getElementById("position-info").innerHTML = Object.entries(config.position).map(([k, v]) =>
                `<div class="config-info-row"><span class="label">${k}</span><span class="value">${v}</span></div>`
            ).join("");
        } else {
            document.getElementById("position-info").innerHTML = '<span class="muted">Not available</span>';
        }

    } catch (e) {
        showToast("Failed to load config: " + e.message, "error");
    }
}

async function saveOwner() {
    const device = document.getElementById("config-device-select").value;
    const longName = document.getElementById("cfg-long-name").value.trim();
    const shortName = document.getElementById("cfg-short-name").value.trim();
    if (!device) return;

    try {
        const res = await postJSON(`/api/config/${encodeURIComponent(device)}/set`, {
            longName, shortName,
        });
        if (res.error) throw new Error(res.error);
        showToast("Owner saved! " + (res.results || []).join(", "), "success");
    } catch (e) {
        showToast("Failed: " + e.message, "error");
    }
}

async function savePosition() {
    const device = document.getElementById("config-device-select").value;
    const lat = document.getElementById("cfg-lat").value;
    const lon = document.getElementById("cfg-lon").value;
    const alt = document.getElementById("cfg-alt").value || 0;
    if (!device || !lat || !lon) { showToast("Fill in latitude and longitude", "error"); return; }

    try {
        const res = await postJSON(`/api/config/${encodeURIComponent(device)}/set`, {
            latitude: lat, longitude: lon, altitude: alt,
        });
        if (res.error) throw new Error(res.error);
        showToast("Position set! " + (res.results || []).join(", "), "success");
    } catch (e) {
        showToast("Failed: " + e.message, "error");
    }
}

async function removePosition() {
    const device = document.getElementById("config-device-select").value;
    if (!device) return;
    try {
        const res = await postJSON(`/api/config/${encodeURIComponent(device)}/set`, {
            removePosition: true,
        });
        if (res.error) throw new Error(res.error);
        showToast("Fixed position removed", "success");
    } catch (e) {
        showToast("Failed: " + e.message, "error");
    }
}

// ── MQTT Module ─────────────────────────────────────────────────────────

let mqttMap = null;
let mqttMapMarkers = {};
let mqttNodesData = [];
let mqttFeedPaused = false;

function initMqtt() {
    // Sub-tab switching
    document.querySelectorAll(".mqtt-sub-tab").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".mqtt-sub-tab").forEach(b => b.classList.remove("active"));
            document.querySelectorAll(".mqtt-sub-content").forEach(c => {
                c.classList.remove("active");
                c.style.display = "none";
            });
            btn.classList.add("active");
            const target = document.getElementById(btn.dataset.mqttTab);
            if (target) {
                target.classList.add("active");
                target.style.display = "block";
            }
            // Lazy init MQTT map
            if (btn.dataset.mqttTab === "mqtt-map-tab" && !mqttMap) {
                initMqttMap();
            }
            if (btn.dataset.mqttTab === "mqtt-nodes-tab") {
                loadMqttNodes();
            }
            if (btn.dataset.mqttTab === "mqtt-device-tab") {
                loadMqttDeviceConfig();
            }
        });
    });

    // Connect / disconnect buttons
    document.getElementById("btn-mqtt-connect").addEventListener("click", async () => {
        try {
            await fetch("/api/mqtt/connect", { method: "POST" });
            showToast("MQTT connecting…", "info");
            setTimeout(loadMqttStatus, 2000);
        } catch (e) {
            showToast("MQTT connect error: " + e.message, "error");
        }
    });
    document.getElementById("btn-mqtt-disconnect").addEventListener("click", async () => {
        try {
            await fetch("/api/mqtt/disconnect", { method: "POST" });
            showToast("MQTT disconnected", "info");
            loadMqttStatus();
        } catch (e) {
            showToast("MQTT disconnect error: " + e.message, "error");
        }
    });

    // Clear feed
    document.getElementById("btn-mqtt-clear-feed").addEventListener("click", () => {
        const container = document.getElementById("mqtt-feed-container");
        container.innerHTML = getMqttFeedHeader();
    });

    // Feed filter
    document.getElementById("mqtt-feed-filter").addEventListener("change", () => {
        // Reload feed with filter
        loadMqttFeed();
    });

    // MQTT node search
    document.getElementById("mqtt-node-search").addEventListener("input", (e) => {
        renderMqttNodes(mqttNodesData, e.target.value);
    });

    // Send form
    document.getElementById("mqtt-send-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const text = document.getElementById("mqtt-send-text").value.trim();
        const channel = document.getElementById("mqtt-send-channel").value.trim() || "LongFast";
        if (!text) return;
        try {
            const res = await fetch("/api/mqtt/send", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text, channel }),
            });
            const data = await res.json();
            if (res.ok) {
                showToast("Sent via MQTT", "success");
                document.getElementById("mqtt-send-text").value = "";
            } else {
                showToast("MQTT send failed: " + (data.error || "unknown"), "error");
            }
        } catch (e) {
            showToast("MQTT send error: " + e.message, "error");
        }
    });

    // Initial feed header
    const container = document.getElementById("mqtt-feed-container");
    container.innerHTML = getMqttFeedHeader();
}

function getMqttFeedHeader() {
    return `<div class="mqtt-feed-header">
        <span>Time</span><span>From</span><span>To</span><span>Channel</span><span>Type</span><span>Payload</span>
    </div>`;
}

async function loadMqttStatus() {
    try {
        const res = await fetch("/api/mqtt/status");
        const stats = await res.json();
        const badge = document.getElementById("mqtt-connection-badge");
        badge.textContent = stats.connected ? "Connected" : "Disconnected";
        badge.className = stats.connected ? "badge badge-online" : "badge badge-offline";

        document.getElementById("mqtt-stat-broker").textContent = stats.broker || "—";
        document.getElementById("mqtt-stat-msgs").textContent = (stats.msg_count || 0).toLocaleString();
        document.getElementById("mqtt-stat-rate").textContent = (stats.msg_rate || 0) + "/s";
        document.getElementById("mqtt-stat-decoded").textContent = (stats.decoded_count || 0).toLocaleString();
        document.getElementById("mqtt-stat-nodes").textContent = Object.keys(mqttNodesData).length || "—";

        // Tab badge
        document.getElementById("mqtt-rate").textContent = (stats.msg_rate || 0) + "/s";

        // Uptime
        if (stats.start_time) {
            const start = new Date(stats.start_time);
            const diff = Math.floor((Date.now() - start.getTime()) / 1000);
            const h = Math.floor(diff / 3600);
            const m = Math.floor((diff % 3600) / 60);
            document.getElementById("mqtt-stat-uptime").textContent = `${h}h ${m}m`;
        } else {
            document.getElementById("mqtt-stat-uptime").textContent = "—";
        }
    } catch (e) {
        console.error("MQTT status error:", e);
    }
}

async function loadMqttFeed() {
    try {
        const filter = document.getElementById("mqtt-feed-filter").value;
        let url = "/api/mqtt/feed?limit=200";
        if (filter) url += "&portnum=" + filter;
        const res = await fetch(url);
        const feed = await res.json();
        const container = document.getElementById("mqtt-feed-container");
        container.innerHTML = getMqttFeedHeader();
        feed.forEach(p => appendMqttPacket(p));
    } catch (e) {
        console.error("MQTT feed error:", e);
    }
}

function appendMqttPacket(pkt) {
    const filter = document.getElementById("mqtt-feed-filter").value;
    if (filter && pkt.portnum !== filter) return;

    const container = document.getElementById("mqtt-feed-container");

    // Auto-scroll detection
    const isAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 50;

    const ts = pkt.timestamp ? new Date(pkt.timestamp).toLocaleTimeString() : "?";
    const payload = formatMqttPayload(pkt);
    const portClass = `feed-port-${pkt.portnum || "UNKNOWN"}`;

    const row = document.createElement("div");
    row.className = "mqtt-feed-row";
    row.innerHTML = `
        <span class="feed-time">${ts}</span>
        <span class="feed-from">${pkt.from || "?"}</span>
        <span class="feed-to">${pkt.to || "?"}</span>
        <span class="feed-channel">${pkt.channel || "—"}</span>
        <span class="feed-port ${portClass}">${pkt.portnum || "?"}</span>
        <span class="feed-payload" title="${escapeHtml(payload)}">${payload}</span>
    `;
    container.appendChild(row);

    // Keep max 500 rows
    const rows = container.querySelectorAll(".mqtt-feed-row");
    if (rows.length > 500) rows[0].remove();

    if (isAtBottom) {
        container.scrollTop = container.scrollHeight;
    }
}

function formatMqttPayload(pkt) {
    const p = pkt.payload || {};
    if (pkt.portnum === "TEXT_MESSAGE_APP" && p.text) return `"${p.text}"`;
    if (pkt.portnum === "POSITION_APP" && p.latitude) return `${p.latitude.toFixed(4)}, ${p.longitude.toFixed(4)}`;
    if (pkt.portnum === "NODEINFO_APP" && p.longName) return `${p.longName} (${p.shortName || "?"}) ${p.hwModelName || ""}`;
    if (pkt.portnum === "TELEMETRY_APP") {
        const dm = p.deviceMetrics || {};
        if (dm.batteryLevel) return `bat:${dm.batteryLevel}% ch:${(dm.channelUtilization||0).toFixed(1)}%`;
        const em = p.environmentMetrics || {};
        if (em.temperature) return `temp:${em.temperature.toFixed(1)}°C`;
        return JSON.stringify(p).substring(0, 60);
    }
    if (!pkt.decoded) return pkt.encrypted ? "🔒 encrypted" : "—";
    if (Object.keys(p).length > 0) return JSON.stringify(p).substring(0, 80);
    return "—";
}

function escapeHtml(str) {
    return str.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

async function loadMqttNodes() {
    try {
        const res = await fetch("/api/mqtt/nodes");
        mqttNodesData = await res.json();
        document.getElementById("mqtt-node-count").textContent = mqttNodesData.length;
        document.getElementById("mqtt-stat-nodes").textContent = mqttNodesData.length;
        const search = document.getElementById("mqtt-node-search").value;
        renderMqttNodes(mqttNodesData, search);
    } catch (e) {
        console.error("MQTT nodes error:", e);
    }
}

function renderMqttNodes(nodes, search = "") {
    const tbody = document.getElementById("mqtt-nodes-body");
    let filtered = nodes;
    if (search) {
        const q = search.toLowerCase();
        filtered = nodes.filter(n =>
            (n.longName || "").toLowerCase().includes(q) ||
            (n.shortName || "").toLowerCase().includes(q) ||
            (n.id || "").toLowerCase().includes(q) ||
            (n.hwModel || "").toLowerCase().includes(q)
        );
    }
    // Sort by lastSeen desc
    filtered.sort((a, b) => (b.lastSeen || "").localeCompare(a.lastSeen || ""));
    document.getElementById("mqtt-nodes-count-header").textContent = filtered.length;

    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="muted">No nodes found.</td></tr>';
        return;
    }
    tbody.innerHTML = filtered.map(n => {
        const pos = (n.latitude && n.longitude)
            ? `${n.latitude.toFixed(4)}, ${n.longitude.toFixed(4)}` : "—";
        const lastSeen = n.lastSeen ? new Date(n.lastSeen).toLocaleTimeString() : "—";
        return `<tr>
            <td>${n.longName || "<i>unknown</i>"}</td>
            <td>${n.shortName || "—"}</td>
            <td class="mono">${n.id || "—"}</td>
            <td>${n.hwModel || "—"}</td>
            <td>${n.role || "—"}</td>
            <td>${pos}</td>
            <td class="mono">${n.gateway || "—"}</td>
            <td>${n.packetCount || 0}</td>
            <td>${lastSeen}</td>
        </tr>`;
    }).join("");
}

function initMqttMap() {
    const theme = document.documentElement.getAttribute("data-theme");
    const tileUrl = theme === "dark"
        ? "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        : "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";

    mqttMap = L.map("mqtt-map").setView([55.0, 15.0], 5);
    L.tileLayer(tileUrl, {
        attribution: '&copy; OpenStreetMap contributors',
        maxZoom: 19,
    }).addTo(mqttMap);

    // Load existing MQTT nodes onto map
    updateMqttMap();
}

function updateMqttMap() {
    if (!mqttMap) return;
    const nodesWithPos = mqttNodesData.filter(n => n.latitude && n.longitude);

    // Remove stale markers
    for (const id in mqttMapMarkers) {
        if (!nodesWithPos.find(n => n.id === id)) {
            mqttMap.removeLayer(mqttMapMarkers[id]);
            delete mqttMapMarkers[id];
        }
    }

    nodesWithPos.forEach(n => {
        const label = n.longName || n.shortName || n.id;
        if (mqttMapMarkers[n.id]) {
            mqttMapMarkers[n.id].setLatLng([n.latitude, n.longitude]);
            mqttMapMarkers[n.id].setPopupContent(
                `<b>${label}</b><br>ID: ${n.id}<br>HW: ${n.hwModel || "?"}<br>Packets: ${n.packetCount}`
            );
        } else {
            const marker = L.circleMarker([n.latitude, n.longitude], {
                radius: 5,
                fillColor: "#8b5cf6",
                color: "#6d28d9",
                weight: 1,
                fillOpacity: 0.8,
            }).addTo(mqttMap);
            marker.bindPopup(
                `<b>${label}</b><br>ID: ${n.id}<br>HW: ${n.hwModel || "?"}<br>Packets: ${n.packetCount}`
            );
            mqttMapMarkers[n.id] = marker;
        }
    });

    // Fit bounds if there are nodes
    if (nodesWithPos.length > 0) {
        const bounds = nodesWithPos.map(n => [n.latitude, n.longitude]);
        mqttMap.fitBounds(bounds, { padding: [30, 30], maxZoom: 12 });
    }
}

async function loadMqttDeviceConfig() {
    try {
        const res = await fetch("/api/mqtt/device-config");
        const configs = await res.json();
        const container = document.getElementById("mqtt-device-config-container");

        if (!configs.length) {
            container.innerHTML = '<p class="muted">No devices available.</p>';
            return;
        }

        container.innerHTML = configs.map(cfg => {
            if (!cfg.connected) {
                return `<div class="mqtt-device-cfg">
                    <h4>${cfg.device} <span class="badge badge-offline">Offline</span></h4>
                </div>`;
            }
            if (cfg.error) {
                return `<div class="mqtt-device-cfg">
                    <h4>${cfg.device}</h4>
                    <p class="muted">Error: ${cfg.error}</p>
                </div>`;
            }

            const chRows = (cfg.channels || []).map(ch => `
                <tr>
                    <td>${ch.name}</td>
                    <td>
                        <label class="toggle-switch">
                            <input type="checkbox" ${ch.uplink ? "checked" : ""}
                                   onchange="toggleMqttChannel('${cfg.device}', ${ch.index}, 'uplink', this.checked)">
                            <span class="toggle-slider"></span>
                        </label>
                    </td>
                    <td>
                        <label class="toggle-switch">
                            <input type="checkbox" ${ch.downlink ? "checked" : ""}
                                   onchange="toggleMqttChannel('${cfg.device}', ${ch.index}, 'downlink', this.checked)">
                            <span class="toggle-slider"></span>
                        </label>
                    </td>
                </tr>
            `).join("");

            return `<div class="mqtt-device-cfg">
                <h4>${cfg.device} <span class="badge badge-online">Online</span></h4>
                <div class="mqtt-cfg-grid">
                    <span>MQTT Enabled: <strong>${cfg.enabled ? "✓ Yes" : "✗ No"}</strong></span>
                    <span>Broker: <strong>${cfg.address}</strong></span>
                    <span>Username: <strong>${cfg.username}</strong></span>
                    <span>Root Topic: <strong>${cfg.root}</strong></span>
                    <span>Encryption: <strong>${cfg.encryption_enabled ? "✓" : "✗"}</strong></span>
                    <span>JSON: <strong>${cfg.json_enabled ? "✓" : "✗"}</strong></span>
                    <span>TLS: <strong>${cfg.tls_enabled ? "✓" : "✗"}</strong></span>
                    <span>Map Reporting: <strong>${cfg.map_reporting_enabled ? "✓" : "✗"}</strong></span>
                </div>
                ${chRows ? `<h5 style="font-size:.78rem;margin:.5rem 0 .25rem;color:var(--text-secondary)">Channel MQTT Settings</h5>
                <table class="mqtt-ch-table">
                    <thead><tr><th>Channel</th><th>Uplink</th><th>Downlink</th></tr></thead>
                    <tbody>${chRows}</tbody>
                </table>` : ""}
            </div>`;
        }).join("");
    } catch (e) {
        console.error("MQTT device config error:", e);
    }
}

async function toggleMqttChannel(device, index, field, value) {
    try {
        const ch = { index };
        ch[field] = value;
        const res = await fetch("/api/mqtt/device-config/set", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ device, channels: [ch] }),
        });
        const data = await res.json();
        if (res.ok) {
            showToast(`MQTT ${field} ${value ? "enabled" : "disabled"} on Ch${index}`, "success");
        } else {
            showToast("Failed: " + (data.error || "unknown"), "error");
        }
    } catch (e) {
        showToast("Error: " + e.message, "error");
    }
}

// ── WebSocket Events ────────────────────────────────────────────────────

function initSocket() {
    socket.on("connect", () => {
        console.log("WebSocket connected");
    });

    socket.on("new_message", (msg) => {
        if (!msg.sent && msg.from !== "local") {
            notifyMessage(msg);
        }
        appendMessage(msg);
    });

    socket.on("position_update", (data) => {
        // Update node position in memory
        const node = nodesData.find(n => n.id === data.from);
        if (node && data.position) {
            node.latitude = data.position.latitude;
            node.longitude = data.position.longitude;
            updateMap(nodesData);
        }
    });

    socket.on("telemetry_update", (data) => {
        // Could trigger stats refresh
    });

    socket.on("traceroute_result", (tr) => {
        updateTracerouteResult(tr.id, tr);
    });

    socket.on("mqtt_packet", (pkt) => {
        appendMqttPacket(pkt);
        // Update MQTT node in cache
        if (pkt.from && pkt.portnum === "NODEINFO_APP" && pkt.payload) {
            const existing = mqttNodesData.find(n => n.id === pkt.from);
            if (existing) {
                if (pkt.payload.longName) existing.longName = pkt.payload.longName;
                if (pkt.payload.shortName) existing.shortName = pkt.payload.shortName;
                if (pkt.payload.hwModelName) existing.hwModel = pkt.payload.hwModelName;
            }
        }
        if (pkt.from && pkt.portnum === "POSITION_APP" && pkt.payload) {
            const existing = mqttNodesData.find(n => n.id === pkt.from);
            if (existing && pkt.payload.latitude) {
                existing.latitude = pkt.payload.latitude;
                existing.longitude = pkt.payload.longitude;
            }
        }
    });

    socket.on("mqtt_status", (status) => {
        const badge = document.getElementById("mqtt-connection-badge");
        badge.textContent = status.connected ? "Connected" : "Disconnected";
        badge.className = status.connected ? "badge badge-online" : "badge badge-offline";
    });

    socket.on("error", (data) => {
        showToast(data.message || "Error", "error");
    });
}

// ── Init ────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initNotifications();
    initTabs();
    initMap();
    initSendForm();
    initNodeSearch();
    initTraceroute();
    initTopologyControls();
    initStatsNodeSelect();
    initConfig();
    initMqtt();
    initSocket();

    // Initial load
    loadAll();
    loadMqttStatus();

    // Refresh button
    document.getElementById("btn-refresh").addEventListener("click", loadAll);

    // Auto-refresh
    setInterval(loadAll, REFRESH_INTERVAL);

    // MQTT status refresh (every 10s)
    setInterval(loadMqttStatus, 10000);
    // MQTT nodes refresh (every 30s)
    setInterval(() => {
        if (document.querySelector('.mqtt-sub-tab.active')?.dataset.mqttTab === 'mqtt-nodes-tab') {
            loadMqttNodes();
        }
        if (document.querySelector('.mqtt-sub-tab.active')?.dataset.mqttTab === 'mqtt-map-tab') {
            loadMqttNodes().then(() => updateMqttMap());
        }
    }, 30000);

    // Expose device actions to global scope for onclick handlers
    window.disconnectDevice = disconnectDevice;
    window.reconnectDevice = reconnectDevice;
    window.toggleMqttChannel = toggleMqttChannel;
});

})();
