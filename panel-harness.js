// Node harness for the Roomprint panel: instantiates roomprint-panel.js
// with a minimal DOM shim and a mocked hass (object-form states, like the
// iPad frontend), drives _load(), and checks the device selector renders.
//
// Run: node ~/workspace/bluetooth-room-positioning/integration/panel-harness.js
const fs = require("fs");
const vm = require("vm");

// ---- minimal DOM shim ----
class FakeShadowRoot {
  constructor() {
    this.innerHTML = "";
    this._listeners = {};
  }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
class FakeElement {
  constructor() {
    this.shadowRoot = null;
    this._hass = null;
  }
  attachShadow() {
    this.shadowRoot = new FakeShadowRoot();
    return this.shadowRoot;
  }
  set hass(v) { this._hass = v; if (this._hassSet) this._hassSet(v); }
  get hass() { return this._hass; }
}
const registry = {};
const sandbox = {
  console,
  HTMLElement: FakeElement,
  customElements: { define: (name, cls) => { registry[name] = cls; } },
  document: { createElement: () => new FakeElement() },
  setTimeout, clearTimeout, setInterval, clearInterval,
  Date, JSON, Promise, Object, Array, Math, String, Number, Boolean,
};
vm.createContext(sandbox);
const src = fs.readFileSync(
  __dirname + "/custom_components/roomprint/frontend/roomprint-panel.js",
  "utf8"
);
vm.runInContext(src, sandbox);
const Panel = registry["roomprint-panel"];
if (!Panel) { console.error("FAIL: roomprint-panel not registered"); process.exit(1); }

const panel = new Panel();
panel.connectedCallback && panel.connectedCallback();

// Mocked hass: object-form states (iPad), callWS for the panel's calls.
const calls = [];
panel._hass = {
  states: {
    "sensor.roomprint_room": { entity_id: "sensor.roomprint_room", state: "Living Room" },
    "sensor.test_phone_room": { entity_id: "sensor.test_phone_room", state: "unknown" },
  },
  callWS: async (msg) => {
    calls.push(msg.type + (msg.entry_id ? `:${msg.entry_id}` : ""));
    switch (msg.type) {
      case "roomprint/list_entries":
        return [
          { entry_id: "e1", label: "Roomprint", address: "AA:BB:CC:DD:EE:FF",
            sensor_entity_id: "sensor.roomprint_room", tracker_entity_id: "device_tracker.roomprint_tracker" },
          { entry_id: "e2", label: "Test Phone", address: "11:22:33:44:55:66",
            sensor_entity_id: "sensor.test_phone_room", tracker_entity_id: "device_tracker.test_phone_tracker" },
        ];
      case "roomprint/list_areas":
        return [{ area_id: "living_room", name: "Living Room", surveyed: false }];
      case "roomprint/get_fingerprints": return {};
      case "roomprint/get_survey_status":
        return { recording: false, area_id: null, started_at: null, sample_count: 0 };
      case "roomprint/get_simulation":
        return { simulation: false, has_sim_fingerprints: false, geometries: {} };
      default: throw new Error("unexpected callWS " + msg.type);
    }
  },
  connection: { subscribeEvents: () => Promise.resolve(() => {}) },
};

(async () => {
  await panel._load();
  await panel._loadSim();
  panel._render();
  const html = panel.shadowRoot.innerHTML;
  const checks = [
    ["device selector present", html.includes('id="device-select"')],
    ["two device options", (html.match(/<option/g) || []).length === 2],
    ["Test Phone option", html.includes("Test Phone")],
    ["Roomprint option", html.includes("Roomprint")],
    ["survey tab present", html.includes("Survey")],
    ["simulate tab present", html.includes("Simulate")],
  ];
  let ok = true;
  for (const [name, pass] of checks) {
    console.log((pass ? "PASS " : "FAIL ") + name);
    if (!pass) ok = false;
  }
  // Switch to the second device and confirm the calls carry its entry_id.
  await panel._selectDevice("e2");
  panel._render();
  const html2 = panel.shadowRoot.innerHTML;
  const sel2 = html2.includes('value="e2" selected');
  console.log((sel2 ? "PASS " : "FAIL ") + "second device selected in UI");
  if (!sel2) ok = false;
  const scoped = calls.some((c) => c === "roomprint/get_survey_status:e2");
  console.log((scoped ? "PASS " : "FAIL ") + "survey status fetched for selected entry");
  if (!scoped) ok = false;
  console.log("calls made:", JSON.stringify(calls));
  console.log(ok ? "HARNESS OK" : "HARNESS FAILED");
  process.exit(ok ? 0 : 1);
})();
