/**
 * The devices page (web_chat/devices_api.py): the browser machines the server
 * runs for the user and the computers the user paired, each with its live
 * screen. Watching never stops the agents; taking control does - their calls
 * wait until the person gives the device back.
 *
 * The live view is a WebSocket of JPEG frames; a person in control sends
 * clicks, wheel turns and keys in the device's own coordinates.
 */

import { h, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { copyText, toast } from "../ui/toast.js";

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 5000;

let current = null; // { id, name, socket, person, width, height }

function statusLine(text, tone = "") {
  const node = $("status");
  node.textContent = text;
  node.dataset.tone = tone;
  node.hidden = !text;
}

function deviceCard(device) {
  const tools = Object.keys(device.tools || {});
  return h(
    "article.deviceCard",
    { dataset: { kind: device.kind } },
    h("header.deviceCard__head",
      {},
      h("strong", { text: device.name }),
      h("span.deviceCard__kind", { text: device.kind }),
    ),
    h("p.deviceCard__status", { text: device.person ? "You have control" : device.status }),
    h("p.deviceCard__meta", { text: `${device.platform || ""} · ${tools.length} tools` }),
    h("div.deviceCard__actions",
      {},
      device.view ? h("button.btn.btn--primary", { type: "button", text: "Watch", on: { click: () => openViewer(device) } }) : null,
      device.origin === "machine"
        ? h("button.btn.btn--ghost", { type: "button", text: "Stop", on: { click: () => stopMachine(device) } })
        : null,
    ),
  );
}

async function refresh() {
  let data;
  try {
    data = await api.devices();
  } catch (error) {
    statusLine(error.message, "error");
    return;
  }
  const machines = data.machines;
  $("start-machine").hidden = !machines;
  $("start-machine").disabled = !!machines && machines.you_may_start < 1;
  $("pair").hidden = !data.pairing;
  replace($("devices"), ...data.devices.map(deviceCard));
  if (!data.devices.length) {
    statusLine(
      machines
        ? "No device is connected. Start a browser machine, or connect your computer."
        : "No device is connected. Connect your computer to let agents use it.",
    );
  } else {
    statusLine("");
  }
  const paired = data.paired || [];
  $("paired-wrap").hidden = !paired.length;
  replace(
    $("paired"),
    ...paired.map((device) =>
      h("li", {},
        h("span", { text: `${device.name} · ${device.kind}` }),
        h("span.devicesPaired__state", { text: device.online ? "online" : "offline" }),
        h("button.btn.btn--ghost", { type: "button", text: "Forget", on: { click: () => forget(device) } }),
      ),
    ),
  );
  if (current && !data.devices.some((d) => d.device === current.id)) closeViewer("The device disconnected.");
}

async function startMachine() {
  const button = $("start-machine");
  button.disabled = true;
  statusLine("Starting a browser machine… (up to a minute)");
  try {
    const { device } = await api.startMachine();
    toast(`Browser machine ${device} is ready`);
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
  await refresh();
}

async function stopMachine(device) {
  if (current?.id === device.device) closeViewer();
  try {
    await api.stopMachine(device.device);
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
  await refresh();
}

async function forget(device) {
  try {
    await api.forgetDevice(device.device);
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
  await refresh();
}

async function pair() {
  try {
    const { code } = await api.newPairing();
    const server = location.origin;
    $("pair-command").textContent =
      `# from a Grid checkout, with: pip install playwright mcp websockets && playwright install chromium\n` +
      `python -m grid_node --server ${server} --pair ${code} --driver browser`;
    $("pairing").hidden = false;
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
}

// -- the live view ------------------------------------------------------------

function openViewer(device) {
  closeViewer();
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/api/devices/${encodeURIComponent(device.device)}/view`);
  current = { id: device.device, name: device.name, socket, person: !!device.person, width: 0, height: 0 };
  $("viewer").hidden = false;
  $("viewer-title").textContent = device.name;
  $("viewer-url").hidden = device.kind !== "browser";
  renderControl();
  const canvas = $("viewer-canvas");
  const context = canvas.getContext("2d");
  socket.addEventListener("message", (event) => {
    const frame = JSON.parse(event.data);
    if (frame.type === "error") {
      toast(frame.detail, { tone: "error" });
      return;
    }
    if (frame.type !== "frame") return;
    const image = new Image();
    image.onload = () => {
      if (canvas.width !== image.width || canvas.height !== image.height) {
        canvas.width = image.width;
        canvas.height = image.height;
      }
      current && Object.assign(current, { width: frame.width || image.width, height: frame.height || image.height });
      context.drawImage(image, 0, 0);
    };
    image.src = `data:${frame.mime};base64,${frame.data}`;
  });
  socket.addEventListener("close", () => {
    if (current?.socket === socket) $("viewer-state").textContent = "disconnected";
  });
  $("viewer").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeViewer(reason = "") {
  if (current) {
    if (current.person) api.deviceControl(current.id, false).catch(() => {});
    current.socket.close();
  }
  current = null;
  $("viewer").hidden = true;
  if (reason) toast(reason);
}

function renderControl() {
  const person = !!current?.person;
  $("viewer-control").textContent = person ? "Give back to agents" : "Take control";
  $("viewer-state").textContent = person ? "You have control" : "Watching";
  $("viewer-canvas").classList.toggle("is-controlled", person);
}

async function toggleControl() {
  if (!current) return;
  const taken = !current.person;
  try {
    await api.deviceControl(current.id, taken);
    current.person = taken;
    renderControl();
    if (taken) $("viewer-canvas").focus();
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
}

function send(event) {
  if (current?.person && current.socket.readyState === WebSocket.OPEN) {
    current.socket.send(JSON.stringify({ type: "input", ...event }));
  }
}

/** A point on the canvas in the device's own coordinates. */
function devicePoint(event) {
  const canvas = $("viewer-canvas");
  const box = canvas.getBoundingClientRect();
  const width = current.width || canvas.width;
  const height = current.height || canvas.height;
  return {
    x: Math.round(((event.clientX - box.left) / box.width) * width),
    y: Math.round(((event.clientY - box.top) / box.height) * height),
  };
}

function wireViewer() {
  const canvas = $("viewer-canvas");
  canvas.addEventListener("click", (event) => current?.person && send({ kind: "click", ...devicePoint(event) }));
  canvas.addEventListener("contextmenu", (event) => {
    if (!current?.person) return;
    event.preventDefault();
    send({ kind: "click", button: "right", ...devicePoint(event) });
  });
  canvas.addEventListener("wheel", (event) => {
    if (!current?.person) return;
    event.preventDefault();
    send({ kind: "wheel", dx: event.deltaX, dy: event.deltaY });
  }, { passive: false });
  canvas.addEventListener("keydown", (event) => {
    if (!current?.person) return;
    event.preventDefault();
    if (["Control", "Shift", "Alt", "Meta"].includes(event.key)) return; // part of a combination, sent with its key
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      send({ kind: "text", text: event.key });
    } else {
      const mods = [event.ctrlKey && "Control", event.altKey && "Alt", event.shiftKey && "Shift", event.metaKey && "Meta"].filter(Boolean);
      send({ kind: "key", key: [...mods, event.key === " " ? "Space" : event.key].join("+") });
    }
  });
  $("viewer-url").addEventListener("submit", (event) => {
    event.preventDefault();
    const url = $("viewer-url-input").value.trim();
    if (url) send({ kind: "navigate", url });
  });
  $("viewer-control").addEventListener("click", toggleControl);
  $("viewer-close").addEventListener("click", () => closeViewer());
}

$("start-machine").addEventListener("click", startMachine);
$("pair").addEventListener("click", pair);
$("copy-command").addEventListener("click", () => copyText($("pair-command").textContent, "Command copied"));
wireViewer();
refresh();
setInterval(refresh, REFRESH_MS);
