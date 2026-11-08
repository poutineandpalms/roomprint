/**
 * Room Survey panel for Roomprint.
 *
 * A plain custom element served from inside Home Assistant. It talks to
 * the backend ONLY through hass.callWS (the roomprint/* commands) and
 * hass.connection.subscribeEvents (roomprint_live). No fetch, no external
 * URLs, no tokens.
 *
 * Two screens, matching the survey mockup:
 *  1. Room list — areas from Home Assistant, Done / Not yet per room.
 *  2. Recording — timer, sample count, live signal bars per sensor,
 *     hint text, big Stop button.
 */
class RoomprintPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
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
    this._error = null;
    this._justFinished = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._unsubLive && hass && hass.connection) {
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
    if (!this._loaded) {
      this._loaded = true;
      this._load();
    }
    this._render();
  }

  disconnectedCallback() {
    this._stopPolling();
    if (this._unsubLive) {
      this._unsubLive();
      this._unsubLive = null;
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
    this._render();
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
      if (this._recording) this._render();
    }
  }

  /* ---------------- helpers ---------------- */

  _areaName(key) {
    const area = this._areas.find((a) => a.area_id === key);
    if (area) return area.name;
    return "Scanner " + String(key).slice(-5);
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
    `;
    let body = "";
    if (this._error) {
      body += `<div class="error">${this._error}</div>`;
    }
    body += this._recording ? this._renderRecording() : this._renderList();
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
    const entries = Object.entries(this._liveReadings).sort(
      (a, b) => b[1] - a[1]
    );
    let bars = "";
    for (const [key, rssi] of entries) {
      bars += `
        <div class="bar-row">
          <div class="label">${this._areaName(key)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${this._barWidth(rssi)}%"></div></div>
          <div class="bar-val">${rssi}</div>
        </div>`;
    }
    if (!bars) {
      bars = `<p class="hint">Waiting for the first advertisement… make sure
        your phone's Bluetooth is on.</p>`;
    }
    return `
      <h1><span class="rec-dot">●</span> Recording</h1>
      <p class="sub">${roomName}</p>
      <div><span class="timer">${this._elapsed()}</span><span class="samples">${this._sampleCount} samples</span></div>
      <p class="sub">Live signal per sensor:</p>
      ${bars}
      <p class="hint">Walk the edges · Pause at the door.</p>
      <button class="btn stop" id="stop-btn">■&nbsp;&nbsp;Stop</button>`;
  }

  _attachHandlers() {
    const root = this.shadowRoot;
    if (!root) return;
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
  }
}

customElements.define("roomprint-panel", RoomprintPanel);
