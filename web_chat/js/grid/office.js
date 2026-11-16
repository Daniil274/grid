/**
 * Office: a recognisable, interactive WebGL2 cutaway of one project.
 *
 * office_model.js owns the honest geography. This file only draws its furniture
 * (one instanced cube call), its typed routes (one batched line call), and a
 * bounded pool of DOM labels. There are no assets, dependencies, network reads
 * or perpetual animation. Orbit/pan/zoom, Home and explicit Focus are available
 * by pointer and keyboard; the native Office directory selects every object
 * without a canvas. Layers remains the non-WebGL/mobile/reduced-motion path.
 */

import { relatedIds } from "./scene.js";
import {
  DEG,
  clamp,
  clampCamera,
  createCamera,
  createCameraTransition,
  createRenderScheduler,
  cameraPosition,
  cameraViewProjection,
  fitCamera,
  focusGoal,
  mixColor,
  orbitCamera,
  panCamera,
  pickNode,
  projectPoint,
  screenRay,
  zoomCamera,
} from "./space_math.js";
import {
  MAX_OFFICE_LABELS,
  OFFICE_COLORS,
  OFFICE_DPR_CAP,
  createOfficeModel,
  officeModelStats,
  officeObjectLabel,
  officeRelations,
} from "./office_model.js";

export { MAX_OFFICE_LABELS, OFFICE_DPR_CAP } from "./office_model.js";

const BACKGROUND = OFFICE_COLORS.background;
const CLICK_SLOP = 6;
const ORBIT_SENSITIVITY = 0.0055;
const KEY_ZOOM_STEP = 1.13;

const CUBE_VERTEX = `#version 300 es
precision highp float;
layout(location = 0) in vec3 aCorner;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec3 aCenter;
layout(location = 3) in vec3 aHalf;
layout(location = 4) in vec3 aColor;
layout(location = 5) in float aGlow;
uniform mat4 uViewProjection;
out vec3 vNormal;
out vec3 vWorld;
out vec3 vColor;
out float vGlow;
void main() {
  vec3 world = aCenter + aCorner * aHalf;
  vWorld = world;
  vNormal = normalize(aNormal / max(abs(aHalf), vec3(0.001)));
  vColor = aColor;
  vGlow = aGlow;
  gl_Position = uViewProjection * vec4(world, 1.0);
}`;

const CUBE_FRAGMENT = `#version 300 es
precision highp float;
in vec3 vNormal;
in vec3 vWorld;
in vec3 vColor;
in float vGlow;
uniform vec3 uCameraPos;
uniform vec3 uLightDir;
uniform vec3 uFogColor;
uniform vec2 uFogRange;
out vec4 outColor;
void main() {
  vec3 normal = normalize(vNormal);
  vec3 viewDir = normalize(uCameraPos - vWorld);
  float diffuse = max(dot(normal, uLightDir), 0.0);
  float hemi = 0.5 + 0.5 * normal.y;
  // The cutaway is information first: even a distant ticket stays readable.
  vec3 lit = vColor * (0.68 + 0.48 * diffuse + 0.18 * hemi);
  float rim = pow(1.0 - max(dot(normal, viewDir), 0.0), 3.0);
  lit += vColor * rim * (0.15 + 0.65 * vGlow);
  lit += vec3(0.85, 0.93, 1.0) * vGlow * 0.12;
  float fog = smoothstep(uFogRange.x, uFogRange.y, distance(uCameraPos, vWorld));
  outColor = vec4(mix(lit, uFogColor, fog), 1.0);
}`;

const LINE_VERTEX = `#version 300 es
precision highp float;
layout(location = 0) in vec3 aPosition;
layout(location = 1) in vec4 aColor;
uniform mat4 uViewProjection;
uniform vec3 uCameraPos;
uniform vec3 uFogColor;
uniform vec2 uFogRange;
out vec4 vColor;
void main() {
  float fog = smoothstep(uFogRange.x, uFogRange.y, distance(uCameraPos, aPosition));
  vColor = vec4(mix(aColor.rgb, uFogColor, fog), aColor.a);
  gl_Position = uViewProjection * vec4(aPosition, 1.0);
}`;

const LINE_FRAGMENT = `#version 300 es
precision highp float;
in vec4 vColor;
out vec4 outColor;
void main() { outColor = vColor; }
`;

const CUBE_FACES = [
  { normal: [1, 0, 0], corners: [[1, -1, -1], [1, 1, -1], [1, 1, 1], [1, -1, 1]] },
  { normal: [-1, 0, 0], corners: [[-1, -1, 1], [-1, 1, 1], [-1, 1, -1], [-1, -1, -1]] },
  { normal: [0, 1, 0], corners: [[-1, 1, -1], [-1, 1, 1], [1, 1, 1], [1, 1, -1]] },
  { normal: [0, -1, 0], corners: [[-1, -1, 1], [-1, -1, -1], [1, -1, -1], [1, -1, 1]] },
  { normal: [0, 0, 1], corners: [[-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]] },
  { normal: [0, 0, -1], corners: [[1, -1, -1], [-1, -1, -1], [-1, 1, -1], [1, 1, -1]] },
];

function cubeGeometry() {
  const vertices = [];
  const indices = [];
  CUBE_FACES.forEach((face, faceIndex) => {
    for (const corner of face.corners) vertices.push(...corner, ...face.normal);
    const base = faceIndex * 4;
    indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
  });
  return { vertices: new Float32Array(vertices), indices: new Uint16Array(indices) };
}

function makeElement(document, tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function radiusOf(bounds) {
  const min = bounds?.min ?? [0, 0, 0];
  const max = bounds?.max ?? [0, 0, 0];
  const radius = Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) / 2;
  return Number.isFinite(radius) ? Math.max(1, radius) : 1;
}

function officeCamera(camera) {
  const next = clampCamera(camera);
  // A cutaway is inspected from above; it never rolls under the floor.
  next.pitch = clamp(next.pitch, 0.16, 1.28);
  next.distance = clamp(next.distance, 8, 40000);
  return next;
}

const ROUTE_COLORS = {
  depends: [0.95, 0.48, 0.22, 0.48],
  turn: [0.16, 0.75, 0.84, 0.34],
  plan: [0.52, 0.59, 0.64, 0.25],
  reference: [0.60, 0.49, 0.75, 0.22],
  delegates: [0.95, 0.65, 0.22, 0.72],
  "message-execution": [0.30, 0.64, 0.82, 0.48],
  "contains-operation": [0.48, 0.56, 0.62, 0.34],
};

/**
 * Create the office synchronously. A missing WebGL2 context is a readable
 * error for GridPage to catch; no empty canvas is left behind.
 */
export function createOfficeView(container, options = {}) {
  if (!container || typeof container.appendChild !== "function") {
    throw new TypeError("createOfficeView needs a DOM element to draw the Office in");
  }
  const document = container.ownerDocument;
  const window = document?.defaultView ?? null;
  if (!document) throw new TypeError("createOfficeView needs an element that belongs to a document");

  const root = makeElement(document, "div", "grid-office-root");
  const stage = makeElement(document, "div", "grid-office-stage");
  const canvas = makeElement(document, "canvas", "grid-office-canvas");
  canvas.tabIndex = 0;
  canvas.setAttribute("aria-label", "3D cutaway office. Drag to orbit, Shift-drag to pan, wheel to zoom. Arrow keys orbit, plus and minus zoom, Home frames the project, F focuses the selection. The Office directory below selects every object without WebGL gestures.");
  canvas.setAttribute("role", "img");
  const labelLayer = makeElement(document, "div", "grid-office-labels");
  labelLayer.setAttribute("aria-hidden", "true");
  stage.append(canvas, labelLayer);

  const gl = canvas.getContext("webgl2", {
    alpha: false,
    antialias: true,
    depth: true,
    stencil: false,
    premultipliedAlpha: false,
    preserveDrawingBuffer: false,
    powerPreference: "high-performance",
  });
  if (!gl) throw new Error("The 3D Office needs WebGL2, which this browser did not provide. Showing Layers instead.");

  const toolbar = makeElement(document, "div", "grid-office-toolbar");
  const homeButton = makeElement(document, "button", "grid-office-action", "⌂ Whole office");
  homeButton.type = "button";
  homeButton.title = "Frame the whole office (Home)";
  const focusButton = makeElement(document, "button", "grid-office-action", "◎ Focus selection");
  focusButton.type = "button";
  focusButton.title = "Focus the selected object (F)";
  focusButton.disabled = true;
  toolbar.append(homeButton, focusButton);
  stage.append(toolbar);

  const hint = makeElement(document, "p", "grid-office-hint");
  hint.setAttribute("role", "status");
  hint.setAttribute("aria-live", "polite");
  stage.append(hint);

  const footer = makeElement(document, "div", "grid-office-footer");
  const snapshot = makeElement(document, "p", "grid-office-snapshot");
  const legend = makeElement(document, "p", "grid-office-legend", "● In progress · ◌ Waiting · ! Blocked · △ Failed · ✓ Closed/done · ? Unknown");
  const routeLegend = makeElement(document, "p", "grid-office-legend", "— Turn/dependency · ┄ Plan/reference · ↗ Delegation. Select an object for relation words; files and closure reasons are not verified results.");
  const relations = makeElement(document, "p", "grid-office-relations");
  const directory = makeElement(document, "details", "grid-office-directory");
  const directorySummary = makeElement(document, "summary", "grid-office-directorySummary");
  const directoryBody = makeElement(document, "div", "grid-office-directoryBody");
  directory.append(directorySummary, directoryBody);
  footer.append(snapshot, legend, routeLegend, relations, directory);
  root.append(stage, footer);

  const clock = () => window?.performance?.now?.() ?? Date.now();
  const requestFrame = typeof window?.requestAnimationFrame === "function"
    ? (callback) => window.requestAnimationFrame(callback)
    : (callback) => setTimeout(() => callback(clock()), 16);
  const cancelFrame = typeof window?.cancelAnimationFrame === "function"
    ? (handle) => window.cancelAnimationFrame(handle)
    : (handle) => clearTimeout(handle);

  const state = {
    destroyed: false,
    lost: false,
    scene: options.scene && typeof options.scene === "object" ? options.scene : { nodes: [], edges: [] },
    map: options.map && typeof options.map === "object" ? options.map : {},
    detailChatIds: arrayCopy(options.detailChatIds),
    detailStates: options.detailStates && typeof options.detailStates === "object" ? options.detailStates : {},
    selectedId: typeof options.selectedId === "string" ? options.selectedId : null,
    related: new Set(),
    model: null,
    byId: new Map(),
    reducedMotion: Boolean(options.reducedMotion),
    camera: createCamera({ target: [0, 0, 0], distance: 100, yaw: -0.56, pitch: 0.58, fov: 48 * DEG }),
    fitted: false,
    width: 1,
    height: 1,
    dpr: 1,
    drawCalls: 0,
    labelCount: 0,
    frames: 0,
    onSelect: typeof options.onSelect === "function" ? options.onSelect : () => {},
    onUnavailable: typeof options.onUnavailable === "function" ? options.onUnavailable : () => {},
    pointers: new Map(),
    drag: null,
    pinch: null,
    resources: null,
    uniforms: null,
  };
  const counts = { instances: 0, routeVertices: 0 };
  const transition = createCameraTransition({ reducedMotion: state.reducedMotion });
  const scheduler = createRenderScheduler({ requestFrame, cancelFrame, onFrame: drawFrame });

  function compile(type, source, name) {
    const shader = gl.createShader(type);
    if (!shader) throw new Error(`The 3D Office could not create its ${name} shader`);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(shader) ?? "no reason given";
      gl.deleteShader(shader);
      throw new Error(`The 3D Office could not compile its ${name} shader: ${log}`);
    }
    return shader;
  }

  function link(vertexSource, fragmentSource, name) {
    const program = gl.createProgram();
    if (!program) throw new Error(`The 3D Office could not create its ${name} program`);
    const vertex = compile(gl.VERTEX_SHADER, vertexSource, `${name} vertex`);
    const fragment = compile(gl.FRAGMENT_SHADER, fragmentSource, `${name} fragment`);
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(program) ?? "no reason given";
      gl.deleteProgram(program);
      throw new Error(`The 3D Office could not link its ${name} program: ${log}`);
    }
    return program;
  }

  function buildResources() {
    disposeResources();
    const cube = link(CUBE_VERTEX, CUBE_FRAGMENT, "furniture");
    const line = link(LINE_VERTEX, LINE_FRAGMENT, "route");
    const uniforms = {
      cube: {
        viewProjection: gl.getUniformLocation(cube, "uViewProjection"),
        cameraPos: gl.getUniformLocation(cube, "uCameraPos"),
        lightDir: gl.getUniformLocation(cube, "uLightDir"),
        fogColor: gl.getUniformLocation(cube, "uFogColor"),
        fogRange: gl.getUniformLocation(cube, "uFogRange"),
      },
      line: {
        viewProjection: gl.getUniformLocation(line, "uViewProjection"),
        cameraPos: gl.getUniformLocation(line, "uCameraPos"),
        fogColor: gl.getUniformLocation(line, "uFogColor"),
        fogRange: gl.getUniformLocation(line, "uFogRange"),
      },
    };
    const geometry = cubeGeometry();
    const cubeVao = gl.createVertexArray();
    const cornerBuffer = gl.createBuffer();
    const indexBuffer = gl.createBuffer();
    const instanceBuffer = gl.createBuffer();
    gl.bindVertexArray(cubeVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, cornerBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, geometry.vertices, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 24, 0);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 24, 12);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, geometry.indices, gl.STATIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, instanceBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(0), gl.DYNAMIC_DRAW);
    const stride = 40;
    for (const [location, size, offset] of [[2, 3, 0], [3, 3, 12], [4, 3, 24], [5, 1, 36]]) {
      gl.enableVertexAttribArray(location);
      gl.vertexAttribPointer(location, size, gl.FLOAT, false, stride, offset);
      gl.vertexAttribDivisor(location, 1);
    }

    const routeVao = gl.createVertexArray();
    const routeBuffer = gl.createBuffer();
    gl.bindVertexArray(routeVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, routeBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(0), gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 28, 0);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 28, 12);
    gl.bindVertexArray(null);

    state.resources = { programs: { cube, line }, cubeVao, cornerBuffer, indexBuffer, instanceBuffer, routeVao, routeBuffer };
    state.uniforms = uniforms;
  }

  function disposeResources() {
    const resources = state.resources;
    if (!resources) return;
    if (!gl.isContextLost?.()) {
      gl.deleteProgram(resources.programs.cube);
      gl.deleteProgram(resources.programs.line);
      gl.deleteVertexArray(resources.cubeVao);
      gl.deleteVertexArray(resources.routeVao);
      gl.deleteBuffer(resources.cornerBuffer);
      gl.deleteBuffer(resources.indexBuffer);
      gl.deleteBuffer(resources.instanceBuffer);
      gl.deleteBuffer(resources.routeBuffer);
    }
    state.resources = null;
    state.uniforms = null;
  }

  function normalizeModel() {
    state.model = createOfficeModel(state.scene, {
      map: state.map,
      selectedId: state.selectedId,
      detailChatIds: state.detailChatIds,
      detailStates: state.detailStates,
    });
    state.byId = new Map(state.model.nodes.map((node) => [node.id, node]));
    state.related = new Set(state.selectedId ? relatedIds(state.scene, state.selectedId).slice(1) : []);
    snapshot.textContent = `${state.model.summary.snapshot} · ${state.model.counts.tasks} tasks · ${state.model.counts.chats} desks · ${state.model.counts.files} files · ${state.model.counts.reports} closure reports`;
    hint.textContent = state.model.detail.hint;
    focusButton.disabled = !state.selectedId || !state.byId.has(state.selectedId);
    const relationWords = officeRelations(state.model, state.selectedId);
    relations.textContent = relationWords.length ? relationWords.join(" · ") : state.model.summary.results;
    relations.hidden = !relationWords.length && !state.model.summary.results;
    rebuildDirectory();
  }

  function partColor(part) {
    const id = part.nodeId;
    const selected = id && id === state.selectedId;
    const related = id && state.related.has(id);
    let color = part.color;
    let glow = 0;
    if (selected) {
      color = mixColor(color, [0.9, 0.97, 1], 0.52);
      glow = 0.9;
    } else if (related) {
      color = mixColor(color, OFFICE_COLORS.active, 0.28);
      glow = 0.35;
    } else if (state.selectedId && id) {
      color = mixColor(color, BACKGROUND, 0.38);
      glow = 0.06;
    } else if (["monitor-status", "active-screen-bar", "approval-barrier"].includes(part.role)) {
      glow = 0.16;
    }
    return [color[0], color[1], color[2], glow];
  }

  function rebuildBuffers() {
    if (!state.resources || !state.model) return;
    const data = new Float32Array(state.model.parts.length * 10);
    let at = 0;
    for (const part of state.model.parts) {
      data.set(part.position, at);
      data.set(part.size.map((value) => Math.max(0.0125, value / 2)), at + 3);
      data.set(partColor(part), at + 6);
      at += 10;
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, state.resources.instanceBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
    counts.instances = state.model.parts.length;
    counts.routeVertices = writeRoutes();
  }

  function writeRoutes() {
    const values = [];
    const segment = (a, b, color, dashed = false) => {
      const length = Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
      const pieces = dashed ? Math.max(1, Math.ceil(length / 1.25)) : 1;
      for (let index = 0; index < pieces; index += 1) {
        if (dashed && index % 2) continue;
        const t0 = index / pieces;
        const t1 = dashed ? Math.min(1, (index + 0.72) / pieces) : 1;
        values.push(
          a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0, a[2] + (b[2] - a[2]) * t0, ...color,
          a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1, a[2] + (b[2] - a[2]) * t1, ...color,
        );
      }
    };
    for (const route of state.model.routes) {
      const base = ROUTE_COLORS[route.kind] ?? OFFICE_COLORS.neutral;
      let color = base.length === 4 ? [...base] : [...base, 0.35];
      if (state.selectedId) {
        const touches = route.from === state.selectedId || route.to === state.selectedId;
        const related = state.related.has(route.from) || state.related.has(route.to);
        color[3] = touches ? 0.95 : related ? Math.min(0.65, color[3] * 1.5) : color[3] * 0.12;
        if (touches) {
          const bright = mixColor(color.slice(0, 3), [1, 1, 1], 0.35);
          color = [...bright, color[3]];
        }
      }
      for (let index = 0; index < route.points.length - 1; index += 1) {
        segment(route.points[index], route.points[index + 1], color, route.kind === "plan" || route.kind === "reference");
      }
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, state.resources.routeBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(values), gl.DYNAMIC_DRAW);
    return values.length / 7;
  }

  function arrayCopy(value) {
    return Array.isArray(value) ? [...value] : [];
  }

  // Directory is a native, searchable alternative to precise 3D picking. Its
  // links keep modified/middle-click browser behaviour and real entity hrefs.
  let directoryQuery = "";
  let directoryKind = "all";
  function rebuildDirectory() {
    directorySummary.textContent = `Office directory · ${state.model.nodes.length} objects · keyboard selection`;
    const controls = makeElement(document, "div", "grid-office-directoryControls");
    const searchLabel = makeElement(document, "label", "grid-office-searchLabel", "Find object");
    const search = makeElement(document, "input", "grid-office-search");
    search.type = "search";
    search.value = directoryQuery;
    search.placeholder = "Title, ID, path or recorded agent";
    searchLabel.append(search);
    const filter = makeElement(document, "select", "grid-office-filter");
    filter.setAttribute("aria-label", "Object type");
    for (const [value, label] of [["all", "All types"], ["task", "Tasks"], ["chat", "Chat desks"], ["file", "Files"], ["execution", "Sessions"], ["operation", "Operations"], ["message", "Messages"]]) {
      const option = makeElement(document, "option", "", label);
      option.value = value;
      filter.append(option);
    }
    filter.value = directoryKind;
    controls.append(searchLabel, filter);
    const list = makeElement(document, "ul", "grid-office-directoryList");
    const count = makeElement(document, "p", "grid-office-directoryCount");
    const paintList = () => {
      const needle = directoryQuery.trim().toLowerCase();
      const visible = state.model.nodes.filter((node) =>
        (directoryKind === "all" || node.kind === directoryKind) &&
        (!needle || [node.title, node.id, node.item?.path, node.agent, node.dimensions?.agent].some((value) => String(value ?? "").toLowerCase().includes(needle))));
      const rows = visible.map((node) => {
        const item = makeElement(document, "li", "grid-office-directoryItem");
        const link = makeElement(document, node.href ? "a" : "button", "grid-office-object", `${node.kind} · ${officeObjectLabel(node)}`);
        if (node.href) link.href = node.href;
        else link.type = "button";
        link.dataset.sceneId = node.id;
        link.setAttribute("aria-current", node.id === state.selectedId ? "true" : "false");
        link.addEventListener("click", (event) => {
          if (event?.button > 0 || event?.metaKey || event?.ctrlKey || event?.shiftKey || event?.altKey) return;
          event?.preventDefault?.();
          emitSelect(node.id);
        });
        item.append(link);
        return item;
      });
      list.replaceChildren(...rows);
      count.textContent = `${visible.length} shown · ${state.model.nodes.length - visible.length} hidden by directory filters. The office layout is unchanged.`;
    };
    search.addEventListener("input", () => { directoryQuery = search.value; paintList(); });
    filter.addEventListener("change", () => { directoryKind = filter.value; paintList(); });
    directoryBody.replaceChildren(controls, count, list);
    paintList();
  }

  const labelPool = [];
  for (let index = 0; index < MAX_OFFICE_LABELS; index += 1) {
    const element = makeElement(document, "span", "grid-office-label");
    element.hidden = true;
    labelLayer.append(element);
    labelPool.push(element);
  }

  function updateLabels(vp) {
    const candidates = [];
    for (const zone of state.model.zones) {
      candidates.push({ id: zone.id, point: zone.position, text: zone.title, priority: -3, zone: true });
    }
    for (const node of state.model.nodes) {
      if (node.kind === "collection") continue;
      const selected = node.id === state.selectedId;
      const priority = selected ? -5 : state.related.has(node.id) ? -2 : node.kind === "project" ? -2.5 :
        ["active", "waiting", "blocked", "failed"].includes(node.status.tone) ? -1 : 0;
      candidates.push({ id: node.id, point: node.labelPosition, text: officeObjectLabel(node), priority, selected });
    }
    // Closure reports point at their canonical task; they are not extra facts.
    for (const report of state.model.reports) {
      if (report.id === state.selectedId) candidates.push({ id: `report:${report.id}`, point: report.labelPosition, text: `▤ Closure reason · ${report.title}`, priority: -4 });
    }
    const projected = candidates.map((candidate) => ({ ...candidate, projected: projectPoint(vp, candidate.point, state.width, state.height) }))
      .filter((candidate) => candidate.projected.visible)
      .sort((a, b) => a.priority - b.priority || a.projected.depth - b.projected.depth);
    const occupied = [];
    const accepted = [];
    for (const candidate of projected) {
      if (accepted.length >= MAX_OFFICE_LABELS) break;
      const width = Math.min(300, Math.max(80, candidate.text.length * 5.8 + 18));
      const x = clamp(candidate.projected.x, width / 2 + 5, Math.max(width / 2 + 5, state.width - width / 2 - 5));
      const y = candidate.projected.y;
      const rect = { left: x - width / 2, right: x + width / 2, top: y - 13, bottom: y + 13 };
      if (!candidate.selected && occupied.some((other) => rect.left < other.right + 6 && rect.right + 6 > other.left && rect.top < other.bottom + 5 && rect.bottom + 5 > other.top)) continue;
      occupied.push(rect);
      accepted.push({ ...candidate, x, y });
    }
    labelPool.forEach((element, index) => {
      const candidate = accepted[index];
      element.hidden = !candidate;
      if (!candidate) return;
      element.textContent = candidate.text;
      element.dataset.zone = candidate.zone ? "true" : "false";
      element.dataset.selected = candidate.selected ? "true" : "false";
      element.style.transform = `translate(-50%, -50%) translate(${Math.round(candidate.x)}px, ${Math.round(candidate.y)}px)`;
    });
    state.labelCount = accepted.length;
  }

  function drawFrame() {
    if (state.destroyed || state.lost || !state.resources) return;
    const advance = transition.step(clock());
    if (advance.camera) state.camera = officeCamera(advance.camera);
    render();
    if (transition.active) scheduler.schedule();
  }

  function render() {
    const resources = state.resources;
    const uniforms = state.uniforms;
    if (!resources || !uniforms || gl.isContextLost?.()) return;
    const camera = officeCamera(state.camera);
    const eye = cameraPosition(camera);
    const vp = cameraViewProjection(camera, state.width / Math.max(1, state.height), radiusOf(state.model.bounds));
    const fogNear = Math.max(camera.distance * 1.5, 90);
    const fogFar = Math.max(camera.distance * 4, fogNear + radiusOf(state.model.bounds) * 2);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(...BACKGROUND, 1);
    gl.clearDepth(1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.disable(gl.CULL_FACE);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    let draws = 0;
    if (counts.instances) {
      gl.useProgram(resources.programs.cube);
      gl.uniformMatrix4fv(uniforms.cube.viewProjection, false, vp);
      gl.uniform3fv(uniforms.cube.cameraPos, new Float32Array(eye));
      gl.uniform3f(uniforms.cube.lightDir, -0.35, 0.84, 0.42);
      gl.uniform3fv(uniforms.cube.fogColor, new Float32Array(BACKGROUND));
      gl.uniform2f(uniforms.cube.fogRange, fogNear, fogFar);
      gl.bindVertexArray(resources.cubeVao);
      gl.drawElementsInstanced(gl.TRIANGLES, 36, gl.UNSIGNED_SHORT, 0, counts.instances);
      draws += 1;
    }
    if (counts.routeVertices) {
      gl.useProgram(resources.programs.line);
      gl.uniformMatrix4fv(uniforms.line.viewProjection, false, vp);
      gl.uniform3fv(uniforms.line.cameraPos, new Float32Array(eye));
      gl.uniform3fv(uniforms.line.fogColor, new Float32Array(BACKGROUND));
      gl.uniform2f(uniforms.line.fogRange, fogNear, fogFar);
      gl.bindVertexArray(resources.routeVao);
      gl.drawArrays(gl.LINES, 0, counts.routeVertices);
      draws += 1;
    }
    gl.bindVertexArray(null);
    state.drawCalls = draws;
    state.frames += 1;
    updateLabels(vp);
  }

  function measure() {
    const rect = stage.getBoundingClientRect?.() ?? container.getBoundingClientRect?.();
    const width = Math.max(1, Math.round(rect?.width || stage.clientWidth || container.clientWidth || 1));
    const height = Math.max(1, Math.round(rect?.height || stage.clientHeight || 600));
    const ratio = Number.isFinite(window?.devicePixelRatio) ? window.devicePixelRatio : 1;
    return { width, height, dpr: clamp(ratio, 1, OFFICE_DPR_CAP) };
  }

  function resize() {
    if (state.destroyed) return api;
    const wasTiny = state.width <= 1 || state.height <= 1;
    Object.assign(state, measure());
    canvas.width = Math.max(1, Math.round(state.width * state.dpr));
    canvas.height = Math.max(1, Math.round(state.height * state.dpr));
    if (wasTiny && state.fitted && state.width > 2) frameImmediately();
    else schedule();
    return api;
  }

  function schedule() {
    if (!state.destroyed && !state.lost) scheduler.schedule();
  }
  function stopTransition() { transition.cancel(); }
  function frameImmediately() {
    const fit = fitCamera(state.model.bounds, { aspect: state.width / Math.max(1, state.height), fov: state.camera.fov, margin: 1.05 });
    state.camera = officeCamera({ ...state.camera, target: fit.target, distance: fit.distance });
    state.fitted = true;
    schedule();
  }
  function moveTo(goal) {
    state.camera = officeCamera(transition.start(state.camera, goal, clock()).camera);
    schedule();
  }
  function home() {
    if (state.destroyed) return false;
    const fit = fitCamera(state.model.bounds, { aspect: state.width / Math.max(1, state.height), fov: state.camera.fov, margin: 1.05 });
    moveTo({ target: fit.target, distance: fit.distance, yaw: -0.56, pitch: 0.58 });
    return true;
  }
  function focus(id) {
    if (state.destroyed) return false;
    const node = state.byId.get(id);
    if (!node) return false;
    moveTo({ ...focusGoal(node), distance: Math.max(node.kind === "chat" ? 25 : 16, focusGoal(node).distance), pitch: 0.58 });
    return true;
  }
  function emitSelect(id) { if (!state.destroyed) state.onSelect(id); }
  function selectAt(clientX, clientY, event = {}) {
    const rect = canvas.getBoundingClientRect?.();
    const width = Math.max(1, rect?.width || state.width);
    const height = Math.max(1, rect?.height || state.height);
    const ray = screenRay(state.camera, width / height,
      ((clientX - (rect?.left ?? 0)) / width) * 2 - 1,
      1 - ((clientY - (rect?.top ?? 0)) / height) * 2, radiusOf(state.model.bounds));
    const hit = pickNode(state.model.pickTargets, ray, { padding: 0.08 });
    if (hit && (event.ctrlKey || event.metaKey || event.button === 1) && hit.node.href) {
      window?.open?.(hit.node.href, "_blank", "noopener");
    } else if (!event.ctrlKey && !event.metaKey && event.button !== 1) emitSelect(hit?.id ?? null);
    return hit?.id ?? null;
  }

  const listeners = [];
  function listen(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    listeners.push({ target, type, handler, options });
  }
  function pointerDown(event) {
    if (state.destroyed || event.button === 2 && event.ctrlKey) return;
    try { canvas.setPointerCapture?.(event.pointerId); } catch { /* synthetic pointer */ }
    canvas.focus?.({ preventScroll: true });
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (state.pointers.size === 1) state.drag = {
      mode: event.button === 2 || event.shiftKey ? "pan" : "orbit",
      lastX: event.clientX, lastY: event.clientY,
      startX: event.clientX, startY: event.clientY,
      button: event.button, moved: false,
    };
    else { state.pinch = null; if (state.drag) state.drag.moved = true; }
  }
  function pointerMove(event) {
    if (!state.pointers.has(event.pointerId)) return;
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    stopTransition();
    if (state.pointers.size >= 2) {
      const [a, b] = [...state.pointers.values()];
      const x = (a.x + b.x) / 2;
      const y = (a.y + b.y) / 2;
      const spread = Math.max(1, Math.hypot(a.x - b.x, a.y - b.y));
      if (state.pinch) {
        state.camera = officeCamera(zoomCamera(panCamera(state.camera, x - state.pinch.x, y - state.pinch.y, state.height), state.pinch.spread / spread));
      }
      state.pinch = { x, y, spread };
    } else if (state.drag) {
      const drag = state.drag;
      const dx = event.clientX - drag.lastX;
      const dy = event.clientY - drag.lastY;
      drag.lastX = event.clientX;
      drag.lastY = event.clientY;
      if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) > CLICK_SLOP) drag.moved = true;
      if (drag.moved) state.camera = officeCamera(drag.mode === "pan"
        ? panCamera(state.camera, dx, dy, state.height)
        : orbitCamera(state.camera, dx, dy, ORBIT_SENSITIVITY));
    }
    schedule();
  }
  function pointerUp(event) {
    if (!state.pointers.has(event.pointerId)) return;
    state.pointers.delete(event.pointerId);
    try { canvas.releasePointerCapture?.(event.pointerId); } catch { /* already released */ }
    if (!state.pointers.size) {
      const drag = state.drag;
      state.drag = null;
      state.pinch = null;
      if (drag && !drag.moved && drag.button !== 2) selectAt(event.clientX, event.clientY, { ...event, button: drag.button, ctrlKey: event.ctrlKey, metaKey: event.metaKey });
    } else {
      const remaining = [...state.pointers.values()][0];
      state.pinch = null;
      state.drag = { mode: "orbit", lastX: remaining.x, lastY: remaining.y, startX: remaining.x, startY: remaining.y, moved: true };
    }
  }
  function keyDown(event) {
    let handled = true;
    switch (event.key) {
      case "ArrowLeft":
      case "ArrowRight":
        state.camera = officeCamera(event.shiftKey
          ? panCamera(state.camera, event.key === "ArrowLeft" ? -35 : 35, 0, state.height)
          : orbitCamera(state.camera, event.key === "ArrowLeft" ? -35 : 35, 0, ORBIT_SENSITIVITY));
        break;
      case "ArrowUp":
      case "ArrowDown":
        state.camera = officeCamera(event.shiftKey
          ? panCamera(state.camera, 0, event.key === "ArrowUp" ? -35 : 35, state.height)
          : orbitCamera(state.camera, 0, event.key === "ArrowUp" ? 25 : -25, ORBIT_SENSITIVITY));
        break;
      case "+": case "=": state.camera = officeCamera(zoomCamera(state.camera, 1 / KEY_ZOOM_STEP)); break;
      case "-": case "_": state.camera = officeCamera(zoomCamera(state.camera, KEY_ZOOM_STEP)); break;
      case "Home": home(); break;
      case "f": case "F": if (state.selectedId) focus(state.selectedId); break;
      case "Escape": emitSelect(null); break;
      default: handled = false;
    }
    if (!handled) return;
    event.preventDefault?.();
    if (!["Home", "f", "F"].includes(event.key)) stopTransition();
    schedule();
  }
  listen(canvas, "pointerdown", pointerDown);
  listen(canvas, "pointermove", pointerMove);
  listen(canvas, "pointerup", pointerUp);
  listen(canvas, "pointercancel", (event) => {
    state.pointers.delete(event.pointerId);
    state.drag = null;
    state.pinch = null;
  });
  listen(canvas, "wheel", (event) => {
    event.preventDefault?.();
    stopTransition();
    state.camera = officeCamera(zoomCamera(state.camera, Math.exp(clamp(event.deltaY, -240, 240) * 0.0016)));
    schedule();
  }, { passive: false });
  listen(canvas, "keydown", keyDown);
  listen(canvas, "contextmenu", (event) => event.preventDefault?.());
  listen(canvas, "webglcontextlost", (event) => {
    event.preventDefault?.();
    state.lost = true;
    scheduler.cancel();
    transition.cancel();
    hint.textContent = "WebGL context lost. Showing the accessible Layers view instead.";
    state.onUnavailable("The Office WebGL context was lost.");
  });
  listen(homeButton, "click", home);
  listen(focusButton, "click", () => focus(state.selectedId));

  let observer = null;
  if (typeof window?.ResizeObserver === "function") {
    observer = new window.ResizeObserver(() => resize());
    observer.observe(stage);
  } else if (window?.addEventListener) listen(window, "resize", () => resize());

  const api = {
    canvas,
    update(next = {}) {
      if (state.destroyed) return api;
      let changed = false;
      for (const key of ["scene", "map", "selectedId", "detailStates", "detailChatIds"]) {
        if (key in next && next[key] !== state[key]) { state[key] = next[key]; changed = true; }
      }
      if ("reducedMotion" in next) {
        state.reducedMotion = Boolean(next.reducedMotion);
        transition.setReducedMotion(state.reducedMotion);
        if (state.reducedMotion && transition.active) {
          const end = transition.step(clock() + 10000);
          if (end.camera) state.camera = officeCamera(end.camera);
        }
      }
      if (typeof next.onSelect === "function") state.onSelect = next.onSelect;
      if (changed) { normalizeModel(); rebuildBuffers(); }
      schedule();
      return api;
    },
    focus,
    home,
    resize,
    destroy() {
      if (state.destroyed) return;
      state.destroyed = true;
      scheduler.dispose();
      transition.cancel();
      for (const { target, type, handler, options } of listeners.splice(0)) target.removeEventListener?.(type, handler, options);
      observer?.disconnect?.();
      observer = null;
      disposeResources();
      state.pointers.clear();
      root.remove();
      gl.getExtension?.("WEBGL_lose_context")?.loseContext?.();
    },
    stats() {
      return { ...officeModelStats(state.model), drawCalls: state.drawCalls, labels: state.labelCount, frames: state.frames, camera: { ...state.camera, target: [...state.camera.target] }, lost: state.lost };
    },
  };

  try {
    buildResources();
    normalizeModel();
    rebuildBuffers();
    container.appendChild(root);
    resize();
    frameImmediately();
  } catch (error) {
    api.destroy();
    throw error;
  }
  return api;
}
