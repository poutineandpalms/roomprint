/**
 * Room Survey panel for Roomprint.
 *
 * A plain custom element served from inside Home Assistant. It talks to
 * the backend ONLY through hass.callWS (the roomprint/* commands) and
 * hass.connection.subscribeEvents (roomprint_live, roomprint_room_changed).
 * No fetch, no external URLs, no tokens.
 *
 * Two tabs:
 *  - Survey: room list + recording screen, matching the survey mockup.
 *  - Simulate: a dummy floor map. Drag the phone dot around; the backend
 *    computes fake RSSI from the map and runs it through the real matcher
 *    and sensor. No hardware needed.
 */
class RoomprintPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._tab = "survey";
    this._areas = [];
    this._fingerprints = {};
    this._selectedAreaId = null;
    this._recording = false;
    this._startedAt = null;
    this._sampleCount = 0;
    this._liveReadings = {};
    this._liveRoom = null;
    this._pollTimer = null;
    this._unsubLive = null;
    this._unsubRoomChanged = null;
    this._error = null;
    this._justFinished = null;
    // Simulator state.
    this._sim = { simulation: false, has_sim_fingerprints: false, geometries: {} };
    this._simMapping = null;
    this._simPos = { x: 2.8, y: 3.8 }; // living-room centre on the dummy map
    this._roomEvents = [];
    this._dragging = false;
    this._lastSimSent = 0;
    this._simTimer = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (hass && hass.connection) {
      if (!this._unsubLive) {
        // Live signal bars, fed by the roomprint_live bus event.
        hass.connection
          .subscribeEvents((event) => this._onLiveEvent(event), "roomprint_live")
          .then((unsub) => {
            this._unsubLive = unsub;
          })
          .catch(() => {
            /* live bars stay empty; polling still shows timer + count */
          });
      }
      if (!this._unsubRoomChanged) {
        hass.connection
          .subscribeEvents(
            (event) => this._onRoomChanged(event),
            "roomprint_room_changed"
          )
          .then((unsub) => {
            this._unsubRoomChanged = unsub;
          })
          .catch(() => {
            /* event feed stays empty */
          });
      }
    }
    if (!this._loaded) {
      this._loaded = true;
      this._load();
    }
    this._render();
  }

  disconnectedCallback() {
    this._stopPolling();
    this._stopSimPolling();
    if (this._unsubLive) {
      this._unsubLive();
      this._unsubLive = null;
    }
    if (this._unsubRoomChanged) {
      this._unsubRoomChanged();
      this._unsubRoomChanged = null;
    }
  }

  /* ---------------- backend ---------------- */

  async _load() {
    try {
      const [areas, fingerprints, status] = await Promise.all([
        this._hass.callWS({ type: "roomprint/list_areas" }),
        this._hass.callWS({ type: "roomprint/get_fingerprints" }),
        this._hass.callWS({ type: "roomprint/get_survey_status" }),
      ]);
      this._areas = areas || [];
      this._fingerprints = fingerprints || {};
      this._applyStatus(status);
      this._error = null;
    } catch (err) {
      this._error = "Could not load Roomprint data. Is the integration set up?";
    }
    await this._loadSim();
    this._render();
  }

  async _loadSim() {
    try {
      const sim = await this._hass.callWS({ type: "roomprint/get_simulation" });
      this._sim = {
        simulation: !!sim.simulation,
        has_sim_fingerprints: !!sim.has_sim_fingerprints,
        geometries: sim.geometries || {},
      };
    } catch (err) {
      /* sim section stays empty; survey still works */
    }
    this._syncSimPolling();
  }

  _applyStatus(status) {
    if (!status) return;
    this._recording = !!status.recording;
    this._startedAt = status.started_at || null;
    this._sampleCount = status.sample_count || 0;
    if (this._recording) {
      this._selectedAreaId = status.area_id;
      this._startPolling();
    } else {
      this._stopPolling();
    }
  }

  async _startSurvey(areaId) {
    try {
      const status = await this._hass.callWS({
        type: "roomprint/start_survey",
        area_id: areaId,
      });
      this._recording = true;
      this._startedAt = status.started_at;
      this._sampleCount = 0;
      this._liveReadings = {};
      this._justFinished = null;
      this._startPolling();
    } catch (err) {
      this._error = "Could not start the survey: " + (err.message || err);
    }
    this._render();
  }

  async _stopSurvey() {
    try {
      const result = await this._hass.callWS({ type: "roomprint/stop_survey" });
      this._justFinished = result;
      this._recording = false;
      this._stopPolling();
      await this._load();
    } catch (err) {
      this._error = "Could not stop the survey: " + (err.message || err);
    }
    this._render();
  }

  async _pollStatus() {
    try {
      const status = await this._hass.callWS({
        type: "roomprint/get_survey_status",
      });
      this._applyStatus(status);
      this._render();
    } catch (err) {
      /* keep the last known state; the next poll retries */
    }
  }

  _startPolling() {
    if (this._pollTimer) return;
    this._pollTimer = setInterval(() => this._pollStatus(), 1000);
  }

  _stopPolling() {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  _onLiveEvent(event) {
    const data = event && event.data ? event.data : {};
    if (data.readings) {
      this._liveReadings = data.readings;
      this._liveRoom = data.room || null;
      // Never re-render mid-drag: it rebuilds the SVG and drops the
      // pointer capture. The drag-end send re-renders instead.
      if ((this._recording || this._tab === "simulate") && !this._dragging) {
        this._render();
      }
    }
  }

  _onRoomChanged(event) {
    const data = event && event.data ? event.data : {};
    const to = data.to_room ? this._areaName(data.to_room) : "Unknown";
    const from = data.from_room ? this._areaName(data.from_room) : null;
    const text = from ? `Entered ${to} (left ${from})` : `Entered ${to}`;
    this._roomEvents.unshift({
      text,
      time: new Date().toLocaleTimeString(),
    });
    this._roomEvents = this._roomEvents.slice(0, 8);
    if (this._tab === "simulate" && !this._dragging) this._render();
  }

  /* ---------------- simulator backend ---------------- */

  _startSimPolling() {
    if (this._simTimer) return;
    this._simTimer = setInterval(() => {
      if (!this._dragging) this._sendSimPosition();
    }, 1000);
  }

  _stopSimPolling() {
    if (this._simTimer) {
      clearInterval(this._simTimer);
      this._simTimer = null;
    }
  }

  // Keep feeding the current dot position while simulating, like a real
  // phone keeps advertising when still — without it, an ambiguous move
  // would freeze mid-dwell the moment the user stops dragging.
  _syncSimPolling() {
    if (this._sim.simulation && this._tab === "simulate") {
      this._startSimPolling();
    } else {
      this._stopSimPolling();
    }
  }

  async _generateSim() {
    try {
      const result = await this._hass.callWS({
        type: "roomprint/generate_sim_fingerprints",
      });
      this._simMapping = result.mapping || {};
      await this._loadSim();
      await this._sendSimPosition();
    } catch (err) {
      this._error =
        "Could not generate dummy fingerprints: " + (err.message || err);
    }
    this._syncSimPolling();
    this._render();
  }

  async _toggleSimulation() {
    const enabled = !this._sim.simulation;
    try {
      const result = await this._hass.callWS({
        type: "roomprint/set_simulation",
        enabled,
      });
      this._sim.simulation = !!result.simulation;
      if (enabled) await this._sendSimPosition();
    } catch (err) {
      this._error = "Could not toggle simulation: " + (err.message || err);
    }
    this._syncSimPolling();
    this._render();
  }

  async _sendSimPosition() {
    if (!this._sim.simulation) return;
    this._lastSimSent = Date.now();
    try {
      const result = await this._hass.callWS({
        type: "roomprint/sim_position",
        x: this._simPos.x,
        y: this._simPos.y,
      });
      if (result.readings) this._liveReadings = result.readings;
      if (result.room) this._liveRoom = result.room;
      if (!this._dragging) this._render();
    } catch (err) {
      /* keep the last known state; the next drag retries */
    }
  }

  _moveDot(e, force) {
    const svg = this.shadowRoot.querySelector("#sim-map");
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const x = Math.min(
      10,
      Math.max(0, ((e.clientX - rect.left) / rect.width) * 10)
    );
    const y = Math.min(
      6,
      Math.max(0, ((e.clientY - rect.top) / rect.height) * 6)
    );
    this._simPos = { x, y };
    const dot = this.shadowRoot.querySelector("#phone-dot");
    if (dot) {
      dot.setAttribute("transform", `translate(${x * 100},${y * 100})`);
    }
    if (force || Date.now() - this._lastSimSent >= 500) {
      this._sendSimPosition();
    }
  }

  /* ---------------- helpers ---------------- */

  _areaName(key) {
    const area = this._areas.find((a) => a.area_id === key);
    if (area) return area.name;
    return "Scanner " + String(key).slice(-5);
  }

  _currentRoomName() {
    const state =
      this._hass && this._hass.states
        ? this._hass.states.find((s) => s.entity_id === "sensor.roomprint_room")
        : null;
    if (state && state.state && state.state !== "unknown") return state.state;
    if (this._liveRoom) return this._areaName(this._liveRoom);
    return "—";
  }

  _elapsed() {
    if (!this._startedAt) return "00:00";
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - this._startedAt));
    const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
    const ss = String(seconds % 60).padStart(2, "0");
    return mm + ":" + ss;
  }

  _barWidth(rssi) {
    // -90 dBm (barely heard) -> 4%, -45 dBm (very close) -> 100%.
    return Math.max(4, Math.min(100, ((rssi + 90) / 45) * 100));
  }

  _renderBars(emptyText) {
    const entries = Object.entries(this._liveReadings).sort(
      (a, b) => b[1] - a[1]
    );
    if (!entries.length) {
      return `<p class="hint">${emptyText}</p>`;
    }
    let bars = `<p class="sub">Live signal per sensor:</p>`;
    for (const [key, rssi] of entries) {
      bars += `
        <div class="bar-row">
          <div class="label">${this._areaName(key)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${this._barWidth(rssi)}%"></div></div>
          <div class="bar-val">${rssi}</div>
        </div>`;
    }
    return bars;
  }

  /* ---------------- render ---------------- */

  _render() {
    if (!this.shadowRoot) return;
    const style = `
      :host { display: block; height: 100%; background: var(--primary-background-color, #fafafa); color: var(--primary-text-color, #212121); font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
      .wrap { max-width: 560px; margin: 0 auto; padding: 16px; }
      h1 { font-size: 24px; margin: 8px 0 4px; }
      .sub { color: var(--secondary-text-color, #727272); margin: 0 0 16px; }
      .card { background: var(--card-background-color, #fff); border: 1px solid var(--divider-color, #e0e0e0); border-radius: 12px; padding: 14px 16px; margin-bottom: 12px; }
      .room { display: flex; align-items: center; justify-content: space-between; cursor: pointer; }
      .room.selected { border: 2px solid #e06e4a; }
      .room .name { font-size: 18px; font-weight: 600; }
      .room .status { color: var(--secondary-text-color, #727272); margin-top: 4px; }
      .room .status.done { color: #22a06b; }
      .room .chev { color: #e06e4a; font-size: 22px; font-weight: 700; }
      .error { background: #fdecea; color: #b3261e; border-radius: 12px; padding: 12px 16px; margin-bottom: 12px; }
      .note { background: var(--card-background-color, #fff); border: 1px solid var(--divider-color, #e0e0e0); border-radius: 12px; padding: 16px; color: var(--secondary-text-color, #727272); }
      .btn { display: block; width: 100%; border: none; border-radius: 28px; padding: 16px; font-size: 18px; font-weight: 600; cursor: pointer; }
      .btn.start { background: #e06e4a; color: #fff; margin-top: 8px; }
      .btn.stop { background: #e06e4a; color: #fff; }
      .btn.small { width: auto; padding: 8px 18px; font-size: 15px; border-radius: 20px; background: #e06e4a; color: #fff; }
      .rec-dot { color: #e06e4a; }
      .timer { font-size: 56px; font-weight: 700; line-height: 1.1; }
      .samples { color: var(--secondary-text-color, #727272); margin-left: 12px; }
      .bar-row { display: flex; align-items: center; gap: 12px; margin: 10px 0; }
      .bar-row .label { width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .bar-track { flex: 1; height: 14px; }
      .bar-fill { height: 14px; border-radius: 7px; background: #4285c8; }
      .bar-val { width: 44px; text-align: right; color: var(--secondary-text-color, #727272); }
      .hint { color: var(--secondary-text-color, #727272); margin: 16px 0; }
      .finished { background: #e6f4ea; color: #137333; border-radius: 12px; padding: 12px 16px; margin-bottom: 12px; }
      .tabs { display: flex; border-bottom: 1px solid var(--divider-color, #e0e0e0); margin-bottom: 16px; }
      .tab { flex: 1; padding: 12px 16px; border: none; background: transparent; color: var(--secondary-text-color, #727272); font-size: 16px; font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent; }
      .tab.active { color: var(--primary-text-color, #212121); border-bottom-color: #e06e4a; }
      .sim-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
      .badge { font-size: 13px; font-weight: 700; letter-spacing: 1px; padding: 6px 10px; border-radius: 6px; background: var(--divider-color, #e0e0e0); color: var(--secondary-text-color, #727272); }
      .badge.on { background: #e06e4a; color: #fff; }
      .map-wrap { border: 1px solid var(--divider-color, #e0e0e0); border-radius: 12px; overflow: hidden; margin-bottom: 12px; background: var(--card-background-color, #fff); }
      .map-wrap svg { display: block; width: 100%; touch-action: none; }
      .room-circle { fill: #dbeafe; stroke: #4285c8; stroke-width: 2; }
      .sensor-dot { fill: #1c4e80; }
      .room-label { font-size: 22px; font-weight: 600; fill: #1c4e80; }
      .dot-hit { fill: transparent; cursor: grab; }
      .dot { fill: #e06e4a; stroke: #fff; stroke-width: 3; cursor: grab; }
      .room-big { font-size: 32px; font-weight: 700; margin: 4px 0 8px; }
      .feed-item { display: flex; justify-content: space-between; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--divider-color, #e0e0e0); }
      .feed-item .time { color: var(--secondary-text-color, #727272); white-space: nowrap; }
    `;
    let body = "";
    if (this._error) {
      body += `<div class="error">${this._error}</div>`;
    }
    body += `<div class="tabs">
      <button class="tab ${this._tab === "survey" ? "active" : ""}" data-tab="survey">Survey</button>
      <button class="tab ${this._tab === "simulate" ? "active" : ""}" data-tab="simulate">Simulate</button>
    </div>`;
    body +=
      this._tab === "simulate"
        ? this._renderSimulate()
        : this._recording
          ? this._renderRecording()
          : this._renderList();
    this.shadowRoot.innerHTML = `<style>${style}</style><div class="wrap">${body}</div>`;
    this._attachHandlers();
  }

  _renderList() {
    if (!this._areas.length) {
      return `
        <h1>Room survey</h1>
        <div class="note">No areas yet. Create your rooms in Home Assistant
        first (Settings &rarr; Areas &amp; zones), assign one Bluetooth proxy
        to each room, then come back here to survey them.</div>`;
    }
    let html = `<h1>Room survey</h1>
      <p class="sub">Tap a room to start recording.</p>`;
    if (this._justFinished) {
      html += `<div class="finished">Saved ${this._justFinished.sample_count} samples for
        ${this._areaName(this._justFinished.area_id)}.</div>`;
    }
    for (const area of this._areas) {
      const surveyed = !!this._fingerprints[area.area_id] || area.surveyed;
      const selected = this._selectedAreaId === area.area_id;
      html += `
        <div class="card room ${selected ? "selected" : ""}" data-area="${area.area_id}">
          <div>
            <div class="name">${area.name}</div>
            <div class="status ${surveyed ? "done" : ""}">${surveyed ? "Done ✓" : "Not yet"}</div>
          </div>
          ${selected ? `<div class="chev">›</div>` : ""}
        </div>`;
    }
    const selected = this._areas.find((a) => a.area_id === this._selectedAreaId);
    if (selected) {
      html += `<button class="btn start" id="start-btn">Start survey — ${selected.name}</button>`;
    }
    html += `<p class="sub">Survey with all doors open. About 2 minutes per room.</p>`;
    return html;
  }

  _renderRecording() {
    const roomName = this._areaName(this._selectedAreaId);
    return `
      <h1><span class="rec-dot">●</span> Recording</h1>
      <p class="sub">${roomName}</p>
      <div><span class="timer">${this._elapsed()}</span><span class="samples">${this._sampleCount} samples</span></div>
      ${this._renderBars("Waiting for the first advertisement… make sure your phone's Bluetooth is on.")}
      <p class="hint">Walk the edges · Pause at the door.</p>
      <button class="btn stop" id="stop-btn">■&nbsp;&nbsp;Stop</button>`;
  }

  _renderSimulate() {
    if (!this._areas.length) {
      return `
        <h1>Simulate</h1>
        <div class="note">No areas yet. Create your rooms in Home Assistant
        first (Settings &rarr; Areas &amp; zones), then come back here — the
        simulator maps your areas onto a dummy floor plan by name.</div>`;
    }
    const on = this._sim.simulation;
    let html = `<h1>Simulate</h1>
      <p class="sub">Drag the phone around the map. No hardware needed.</p>
      <div class="sim-row">
        <span class="badge ${on ? "on" : ""}">${on ? "SIMULATION ON" : "SIMULATION OFF"}</span>
        <button class="btn small" id="sim-toggle">${on ? "Turn off" : "Turn on"}</button>
      </div>`;
    if (!this._sim.has_sim_fingerprints) {
      html += `<div class="note">No dummy fingerprints yet. Generate them
        from the map and the matcher will run on fake readings instead of
        real Bluetooth.</div>
        <button class="btn start" id="sim-generate">Generate dummy fingerprints</button>`;
      return html;
    }
    if (this._simMapping) {
      const mapped = Object.entries(this._simMapping)
        .map(([areaId, slug]) => `${this._areaName(areaId)} → ${slug}`)
        .join(" · ");
      html += `<p class="sub">Mapped: ${mapped}</p>`;
    }
    if (!on) {
      html += `<div class="note">Dummy fingerprints are ready. Turn
        simulation on to drag the phone around the map.</div>`;
      return html;
    }
    let circles = "";
    for (const [areaId, geo] of Object.entries(this._sim.geometries)) {
      const cx = geo.x * 100;
      const cy = geo.y * 100;
      const r = geo.radius * 100;
      circles += `
        <circle cx="${cx}" cy="${cy}" r="${r}" class="room-circle" />
        <circle cx="${cx}" cy="${cy}" r="7" class="sensor-dot" />
        <text x="${cx}" y="${cy - r + 30}" class="room-label" text-anchor="middle">${this._areaName(areaId)}</text>`;
    }
    html += `<div class="map-wrap"><svg id="sim-map" viewBox="0 0 1000 600">${circles}
      <g id="phone-dot" transform="translate(${this._simPos.x * 100},${this._simPos.y * 100})">
        <circle r="36" class="dot-hit" />
        <circle r="15" class="dot" />
      </g></svg></div>`;
    html += `<p class="sub">Current room:</p><div class="room-big">${this._currentRoomName()}</div>`;
    html += this._renderBars("No readings yet — drag the phone dot.");
    html += `<p class="sub">Room changes:</p>`;
    if (!this._roomEvents.length) {
      html += `<p class="hint">No room changes yet — drag the phone across a room boundary.</p>`;
    } else {
      for (const entry of this._roomEvents) {
        html += `<div class="feed-item"><span>${entry.text}</span><span class="time">${entry.time}</span></div>`;
      }
    }
    return html;
  }

  _attachHandlers() {
    const root = this.shadowRoot;
    if (!root) return;
    root.querySelectorAll(".tab").forEach((el) => {
      el.addEventListener("click", () => {
        this._tab = el.getAttribute("data-tab");
        this._syncSimPolling();
        this._render();
      });
    });
    root.querySelectorAll(".room").forEach((el) => {
      el.addEventListener("click", () => {
        this._selectedAreaId = el.getAttribute("data-area");
        this._render();
      });
    });
    const startBtn = root.querySelector("#start-btn");
    if (startBtn) {
      startBtn.addEventListener("click", () => this._startSurvey(this._selectedAreaId));
    }
    const stopBtn = root.querySelector("#stop-btn");
    if (stopBtn) {
      stopBtn.addEventListener("click", () => this._stopSurvey());
    }
    const toggleBtn = root.querySelector("#sim-toggle");
    if (toggleBtn) {
      toggleBtn.addEventListener("click", () => this._toggleSimulation());
    }
    const genBtn = root.querySelector("#sim-generate");
    if (genBtn) {
      genBtn.addEventListener("click", () => this._generateSim());
    }
    const svg = root.querySelector("#sim-map");
    const dot = root.querySelector("#phone-dot");
    if (svg && dot) {
      dot.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        this._dragging = true;
        svg.setPointerCapture(e.pointerId);
        this._moveDot(e);
      });
      svg.addEventListener("pointermove", (e) => {
        if (!this._dragging) return;
        this._moveDot(e);
      });
      const endDrag = (e) => {
        if (!this._dragging) return;
        this._dragging = false;
        this._moveDot(e, true);
        this._render();
      };
      svg.addEventListener("pointerup", endDrag);
      svg.addEventListener("pointercancel", endDrag);
    }
  }
}

customElements.define("roomprint-panel", RoomprintPanel);
