/**
 * The Space view: a real WebGL2 renderer of the SceneModel
 * (web_chat/js/grid/space.js).
 *
 * The scene (``grid/scene.js``) says where everything stands; this module puts
 * a perspective camera in that space and draws it with raw WebGL2 - no CDN, no
 * framework, no external asset. Nothing is fetched and nothing is loaded: the
 * geometry is a unit cube, five shaders and a handful of buffers.
 *
 * ## What is drawn
 *
 * - every node as an **instanced cube** (one instanced draw call for all of
 *   them), sized and coloured by the scene, with directional light, a rim
 *   light and fog, so a node reads as a solid object rather than a dot;
 * - every edge of the scene as one **batched GL_LINES** draw call, coloured by
 *   its kind (``contains`` / ``turn`` / ``plan`` / ``depends`` /
 *   ``message-execution`` / ``delegates`` / ``operation`` /
 *   ``contains-operation`` / ``reference``);
 *   an edge whose end is not in the current scene is never drawn, so a filtered
 *   scene cannot leave a line hanging in the air;
 * - the canonical hierarchy of ``grid/scene.js`` stands as it is laid out: the
 *   project and its collection platforms on top, the task, chat and file
 *   streets below, and a selected chat's messages, executions and operations
 *   hanging beside it, each on its own level;
 * - a subtle ground grid and the three axes, also batched lines, regenerated
 *   for the scene's own extents;
 * - **at most 24 DOM labels**, pooled and reused - the selected node, the
 *   related and structural ones, and the nearest visible ones. There is no
 *   element per node.
 *
 * The selection is what a reader reads first: the selected and its neighbours
 * brighten and glow, everything unrelated fades back, and edges that touch the
 * selection stand out.
 *
 * ## Staying idle
 *
 * Nothing animates on its own. A frame is drawn when something changed - a new
 * scene, a new selection, a gesture, a resize, a camera move - and then the
 * canvas rests. A focus move is the only animation, and only while it lasts
 * (immediately when ``reducedMotion`` is set).
 *
 * ## Contract
 *
 * ```js
 * const view = createSpaceView(container, {
 *   scene, selectedId = null, onSelect = () => {}, reducedMotion = false,
 * });                       // throws a readable Error when WebGL2 is missing
 * view.update({ scene, selectedId, reducedMotion });
 * view.focus(nodeId);       // camera move to a node, animated unless reducedMotion
 * view.resize();            // also automatic via ResizeObserver
 * view.destroy();           // listeners, observer, pending frame, GL objects, DOM
 * view.stats();             // {nodes, edges, drawCalls, labels, frames}
 * ```
 *
 * @module grid/space
 */

import { relatedIds } from "./scene.js";
import {
  CAMERA_LIMITS,
  DEG,
  DEFAULT_CAMERA,
  clamp,
  clampCamera,
  createCamera,
  createCameraTransition,
  createRenderScheduler,
  cameraPosition,
  cameraViewProjection,
  edgeColor,
  fitCamera,
  focusGoal,
  mixColor,
  nodeColor,
  orbitCamera,
  panCamera,
  pickNode,
  projectPoint,
  screenRay,
  zoomCamera,
} from "./space_math.js";

/** How many DOM labels may exist at once. */
export const MAX_LABELS = 24;

/** The base colour of a node kind; the palette is the one the scene's kinds use. */
export function nodeBaseColor(kind) {
  return nodeColor(kind);
}

/** The colour of an edge kind; the palette is the one the scene's edges use. */
export function edgeBaseColor(kind) {
  return edgeColor(kind);
}

/** The device pixel ratio the renderer allows itself; above this the canvas is capped. */
export const DPR_CAP = 1.75;

/** A pointer that moved less than this many pixels is a click, not a drag. */
const CLICK_SLOP = 6;

/** The background, and the colour distant geometry fades into. */
const BACKGROUND = [0.043, 0.055, 0.075];

/** The keyboard step of one arrow press, in radians, and of one zoom press. */
const KEY_ORBIT_STEP = 0.11;
const KEY_ZOOM_STEP = 1.13;

/** Radians of orbit per pointer pixel; the same sensitivity for mouse, touch and keys. */
const ORBIT_SENSITIVITY = 0.0055;

/* ------------------------------------------------------------------------- */
/* Shaders                                                                    */
/* ------------------------------------------------------------------------- */

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
  vec3 base = vColor;

  vec3 lit = base * (0.30 + 0.58 * diffuse + 0.18 * hemi);
  float rim = pow(1.0 - max(dot(normal, viewDir), 0.0), 3.0);
  lit += base * rim * (0.22 + 0.9 * vGlow);
  lit += vec3(0.85, 0.93, 1.0) * vGlow * 0.12;

  float depth = distance(uCameraPos, vWorld);
  float fog = smoothstep(uFogRange.x, uFogRange.y, depth);
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
  float depth = distance(uCameraPos, aPosition);
  float fog = smoothstep(uFogRange.x, uFogRange.y, depth);
  vColor = vec4(mix(aColor.rgb, uFogColor, fog), aColor.a);
  gl_Position = uViewProjection * vec4(aPosition, 1.0);
}`;

const LINE_FRAGMENT = `#version 300 es
precision highp float;

in vec4 vColor;
out vec4 outColor;

void main() {
  outColor = vColor;
}`;

/* ------------------------------------------------------------------------- */
/* Geometry                                                                   */
/* ------------------------------------------------------------------------- */

/** The six faces of a unit cube: corners wound counter-clockwise from outside. */
const CUBE_FACES = [
  { normal: [1, 0, 0], corners: [[1, -1, -1], [1, 1, -1], [1, 1, 1], [1, -1, 1]] },
  { normal: [-1, 0, 0], corners: [[-1, -1, 1], [-1, 1, 1], [-1, 1, -1], [-1, -1, -1]] },
  { normal: [0, 1, 0], corners: [[-1, 1, -1], [-1, 1, 1], [1, 1, 1], [1, 1, -1]] },
  { normal: [0, -1, 0], corners: [[-1, -1, 1], [-1, -1, -1], [1, -1, -1], [1, -1, 1]] },
  { normal: [0, 0, 1], corners: [[-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]] },
  { normal: [0, 0, -1], corners: [[1, -1, -1], [-1, -1, -1], [-1, 1, -1], [1, 1, -1]] },
];

/** A cube as 24 corners with normals plus 36 indices. */
function cubeGeometry() {
  const vertices = [];
  const indices = [];
  CUBE_FACES.forEach((face, faceIndex) => {
    for (const corner of face.corners) {
      vertices.push(corner[0], corner[1], corner[2], face.normal[0], face.normal[1], face.normal[2]);
    }
    const base = faceIndex * 4;
    indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
  });
  return {
    vertices: new Float32Array(vertices),
    indices: new Uint16Array(indices),
  };
}

/** A nice round grid step near *target* (1, 2 or 5 times a power of ten). */
function niceStep(target) {
  const wanted = Math.max(Number.isFinite(target) ? target : 1, 1e-3);
  const power = Math.pow(10, Math.floor(Math.log10(wanted)));
  for (const multiple of [1, 2, 5, 10]) {
    if (wanted <= multiple * power) return multiple * power;
  }
  return 10 * power;
}

/* ------------------------------------------------------------------------- */
/* The view                                                                   */
/* ------------------------------------------------------------------------- */

/**
 * Create a Space view in *container*.
 *
 * Synchronous: the canvas, the GL context and the programs exist by the time
 * this returns, or it throws an Error a caller can show ("this browser has no
 * WebGL2"). Nothing is fetched.
 *
 * @param {HTMLElement} container the element the canvas fills
 * @param {{scene?: object, selectedId?: string|null, onSelect?: Function, reducedMotion?: boolean}} [options]
 * @returns {{
 *   canvas: HTMLCanvasElement,
 *   update: (next?: object) => object,
 *   focus: (nodeId: string) => boolean,
 *   resize: () => object,
 *   destroy: () => void,
 *   stats: () => {nodes: number, edges: number, drawCalls: number, labels: number, frames: number},
 * }}
 */
export function createSpaceView(container, options = {}) {
  if (!container || typeof container.appendChild !== "function") {
    throw new TypeError("createSpaceView needs a DOM element to draw the Space in");
  }
  const document = container.ownerDocument;
  const window = document?.defaultView ?? null;
  if (!document) throw new TypeError("createSpaceView needs an element that belongs to a document");

  const root = document.createElement("div");
  root.className = "grid-space-root";
  root.style.position = "relative";
  root.style.width = "100%";
  root.style.height = "100%";
  root.style.overflow = "hidden";
  root.style.background = "rgb(11, 14, 19)";

  const canvas = document.createElement("canvas");
  canvas.className = "grid-space-canvas";
  canvas.tabIndex = 0;
  canvas.style.display = "block";
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.touchAction = "none";
  canvas.style.outline = "none";
  canvas.style.cursor = "grab";
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", "Three-dimensional project space. Drag to orbit, wheel to zoom.");
  root.appendChild(canvas);

  const labelLayer = document.createElement("div");
  labelLayer.className = "grid-space-labels";
  labelLayer.style.position = "absolute";
  labelLayer.style.inset = "0";
  labelLayer.style.pointerEvents = "none";
  labelLayer.style.overflow = "hidden";
  root.appendChild(labelLayer);

  const gl = canvas.getContext("webgl2", {
    alpha: false,
    antialias: true,
    depth: true,
    stencil: false,
    premultipliedAlpha: false,
    preserveDrawingBuffer: false,
    powerPreference: "high-performance",
  });
  if (!gl) {
    throw new Error(
      "The 3D Space needs WebGL2, which this browser did not provide. Use the Flat view instead.",
    );
  }

  // Only now, with a working context in hand, does anything touch the page.
  container.appendChild(root);

  const clock = () => (window?.performance?.now?.() ?? Date.now());
  const requestFrame =
    typeof window?.requestAnimationFrame === "function"
      ? (callback) => window.requestAnimationFrame(callback)
      : (callback) => setTimeout(() => callback(clock()), 16);
  const cancelFrame =
    typeof window?.cancelAnimationFrame === "function"
      ? (handle) => window.cancelAnimationFrame(handle)
      : (handle) => clearTimeout(handle);

  const state = {
    destroyed: false,
    lost: false,
    scene: normalizeScene(options.scene),
    selectedId: typeof options.selectedId === "string" ? options.selectedId : null,
    related: new Set(),
    reducedMotion: Boolean(options.reducedMotion),
    camera: createCamera(),
    fitted: false,
    nodes: [],
    edges: [],
    nodesById: new Map(),
    radius: 0,
    width: 1,
    height: 1,
    dpr: 1,
    drawCalls: 0,
    labelCount: 0,
    frames: 0,
    onSelect: typeof options.onSelect === "function" ? options.onSelect : () => {},
    pointers: new Map(),
    drag: null,
    pinch: null,
    resources: null,
    uniforms: null,
  };

  const transition = createCameraTransition({ reducedMotion: state.reducedMotion });
  const scheduler = createRenderScheduler({ requestFrame, cancelFrame, onFrame: drawFrame });

  /* ----------------------------------------------------------------------- */
  /* GL resources                                                            */
  /* ----------------------------------------------------------------------- */

  function compile(type, source, name) {
    const shader = gl.createShader(type);
    if (!shader) throw new Error(`The 3D Space could not create its ${name} shader`);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(shader) ?? "no reason given";
      gl.deleteShader(shader);
      throw new Error(`The 3D Space could not compile its ${name} shader: ${log}`);
    }
    return shader;
  }

  function link(vertexSource, fragmentSource, name) {
    const program = gl.createProgram();
    if (!program) throw new Error(`The 3D Space could not create its ${name} program`);
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
      throw new Error(`The 3D Space could not link its ${name} program: ${log}`);
    }
    return program;
  }

  /** All GL objects of the view; built once, rebuilt after a context restore. */
  function buildResources() {
    disposeResources();

    const cubeProgram = link(CUBE_VERTEX, CUBE_FRAGMENT, "node");
    const lineProgram = link(LINE_VERTEX, LINE_FRAGMENT, "line");

    const uniforms = {
      cube: {
        viewProjection: gl.getUniformLocation(cubeProgram, "uViewProjection"),
        cameraPos: gl.getUniformLocation(cubeProgram, "uCameraPos"),
        lightDir: gl.getUniformLocation(cubeProgram, "uLightDir"),
        fogColor: gl.getUniformLocation(cubeProgram, "uFogColor"),
        fogRange: gl.getUniformLocation(cubeProgram, "uFogRange"),
      },
      line: {
        viewProjection: gl.getUniformLocation(lineProgram, "uViewProjection"),
        cameraPos: gl.getUniformLocation(lineProgram, "uCameraPos"),
        fogColor: gl.getUniformLocation(lineProgram, "uFogColor"),
        fogRange: gl.getUniformLocation(lineProgram, "uFogRange"),
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
    const stride = 10 * 4;
    gl.enableVertexAttribArray(2);
    gl.vertexAttribPointer(2, 3, gl.FLOAT, false, stride, 0);
    gl.vertexAttribDivisor(2, 1);
    gl.enableVertexAttribArray(3);
    gl.vertexAttribPointer(3, 3, gl.FLOAT, false, stride, 12);
    gl.vertexAttribDivisor(3, 1);
    gl.enableVertexAttribArray(4);
    gl.vertexAttribPointer(4, 3, gl.FLOAT, false, stride, 24);
    gl.vertexAttribDivisor(4, 1);
    gl.enableVertexAttribArray(5);
    gl.vertexAttribPointer(5, 1, gl.FLOAT, false, stride, 36);
    gl.vertexAttribDivisor(5, 1);
    gl.bindVertexArray(null);

    const edgeVao = gl.createVertexArray();
    const edgeBuffer = gl.createBuffer();
    gl.bindVertexArray(edgeVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(0), gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 28, 0);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 28, 12);
    gl.bindVertexArray(null);

    const gridVao = gl.createVertexArray();
    const gridBuffer = gl.createBuffer();
    gl.bindVertexArray(gridVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, gridBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(0), gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 28, 0);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 28, 12);
    gl.bindVertexArray(null);

    state.resources = {
      programs: { cube: cubeProgram, line: lineProgram },
      cubeVao,
      cornerBuffer,
      indexBuffer,
      instanceBuffer,
      edgeVao,
      edgeBuffer,
      gridVao,
      gridBuffer,
    };
    state.uniforms = uniforms;
  }

  function disposeResources() {
    const resources = state.resources;
    if (!resources) return;
    if (gl.isContextLost?.()) {
      state.resources = null;
      return;
    }
    gl.deleteProgram(resources.programs.cube);
    gl.deleteProgram(resources.programs.line);
    gl.deleteVertexArray(resources.cubeVao);
    gl.deleteVertexArray(resources.edgeVao);
    gl.deleteVertexArray(resources.gridVao);
    gl.deleteBuffer(resources.cornerBuffer);
    gl.deleteBuffer(resources.indexBuffer);
    gl.deleteBuffer(resources.instanceBuffer);
    gl.deleteBuffer(resources.edgeBuffer);
    gl.deleteBuffer(resources.gridBuffer);
    state.resources = null;
    state.uniforms = null;
  }

  buildResources();

  /* ----------------------------------------------------------------------- */
  /* Scene bookkeeping                                                       */
  /* ----------------------------------------------------------------------- */

  function normalizeLocal() {
    const scene = state.scene;
    const nodes = scene.nodes.filter(
      (node) =>
        node &&
        typeof node.id === "string" &&
        Array.isArray(node.position) &&
        node.position.length >= 3 &&
        node.position.every((value) => Number.isFinite(value)),
    );
    const ids = new Set(nodes.map((node) => node.id));
    const edges = scene.edges.filter(
      (edge) => edge && ids.has(edge.from) && ids.has(edge.to) && edge.from !== edge.to,
    );
    state.nodes = nodes;
    state.edges = edges;
    state.nodesById = new Map(nodes.map((node) => [node.id, node]));
    state.related = new Set(
      state.selectedId === null ? [] : relatedIds({ nodes, edges }, state.selectedId).slice(1),
    );
    state.radius = radiusOf(nodes);
  }

  function radiusOf(nodes) {
    const bounds = state.scene.bounds;
    const min = Array.isArray(bounds?.min) ? bounds.min : null;
    const max = Array.isArray(bounds?.max) ? bounds.max : null;
    if (min && max && [...min, ...max].every((value) => Number.isFinite(value))) {
      const radius = Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) / 2;
      if (Number.isFinite(radius) && radius > 0) return radius;
    }
    if (!nodes.length) return 0;
    let furthest = 0;
    for (const node of nodes) furthest = Math.max(furthest, Math.hypot(node.position[0], node.position[1], node.position[2]));
    return Number.isFinite(furthest) ? furthest : 0;
  }

  normalizeLocal();

  /* ----------------------------------------------------------------------- */
  /* Buffers                                                                 */
  /* ----------------------------------------------------------------------- */

  /** One instance per node: centre, half size, colour and glow. */
  function writeInstances() {
    const resources = state.resources;
    if (!resources) return 0;
    const count = state.nodes.length;
    const data = new Float32Array(count * 10);
    const dimmed = state.selectedId !== null;
    let at = 0;
    for (const node of state.nodes) {
      const size = Array.isArray(node.size) ? node.size : [1, 1, 1];
      const half = [
        Math.max(Math.abs(Number(size[0])) / 2, 0.6),
        Math.max(Math.abs(Number(size[1])) / 2, 0.4),
        Math.max(Math.abs(Number(size[2])) / 2, 0.3),
      ];
      const color = instanceColor(node, dimmed);
      data[at] = node.position[0];
      data[at + 1] = node.position[1];
      data[at + 2] = node.position[2];
      data[at + 3] = half[0];
      data[at + 4] = half[1];
      data[at + 5] = half[2];
      data[at + 6] = color[0];
      data[at + 7] = color[1];
      data[at + 8] = color[2];
      data[at + 9] = color[3];
      at += 10;
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, resources.instanceBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
    return count;
  }

  /** The colour and glow of one node under the current selection. */
  function instanceColor(node, dimmed) {
    const base = nodeBaseColor(node.kind);
    const selected = state.selectedId === node.id;
    const related = state.related.has(node.id);
    let color = base;
    let glow = 0;

    if (selected) {
      color = mixColor(base, [1, 1, 1], 0.55);
      glow = 1;
    } else if (related) {
      color = mixColor(base, [1, 1, 1], 0.22);
      glow = 0.55;
    } else if (dimmed) {
      color = mixColor(base, BACKGROUND, 0.72);
      glow = 0;
    } else {
      glow = node.state === "in_progress" || node.state === "awaiting_approval" ? 0.22 : 0.06;
    }
    if (node.state === "closed" || node.state === "done") {
      color = mixColor(color, BACKGROUND, dimmed || selected || related ? 0.15 : 0.34);
    }
    return [color[0], color[1], color[2], glow];
  }

  /** The batched edge lines of the current scene, one vertex per end. */
  function writeEdges() {
    const resources = state.resources;
    if (!resources) return 0;
    const byId = state.nodesById;
    const values = [];
    let drawn = 0;
    for (const edge of state.edges) {
      const from = byId.get(edge.from);
      const to = byId.get(edge.to);
      if (!from || !to) continue;
      const base = edgeBaseColor(edge.kind);
      let alpha = base[3];
      let rgb = [base[0], base[1], base[2]];
      const touches = state.selectedId !== null && (edge.from === state.selectedId || edge.to === state.selectedId);
      if (state.selectedId !== null) {
        if (touches) {
          alpha = Math.min(0.95, base[3] * 2.1);
          rgb = mixColor(rgb, [1, 1, 1], 0.35);
        } else {
          alpha = base[3] * 0.22;
          rgb = mixColor(rgb, BACKGROUND, 0.5);
        }
      }
      values.push(...from.position, rgb[0], rgb[1], rgb[2], alpha);
      values.push(...to.position, rgb[0], rgb[1], rgb[2], alpha);
      drawn += 1;
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, resources.edgeBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(values), gl.DYNAMIC_DRAW);
    return drawn * 2;
  }

  /** The ground grid and the three axes of the scene's extents. */
  function writeGrid() {
    const resources = state.resources;
    if (!resources) return 0;
    const nodes = state.nodes;
    if (!nodes.length) {
      gl.bindBuffer(gl.ARRAY_BUFFER, resources.gridBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(0), gl.DYNAMIC_DRAW);
      return 0;
    }

    let minX = Infinity;
    let minY = Infinity;
    let minZ = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    let maxZ = -Infinity;
    for (const node of nodes) {
      minX = Math.min(minX, node.position[0]);
      maxX = Math.max(maxX, node.position[0]);
      minY = Math.min(minY, node.position[1]);
      maxY = Math.max(maxY, node.position[1]);
      minZ = Math.min(minZ, node.position[2]);
      maxZ = Math.max(maxZ, node.position[2]);
    }

    const span = Math.max(maxX - minX, maxZ - minZ, state.radius * 1.4, 40);
    const step = niceStep(span / 8);
    const half = Math.min(Math.ceil((span / 2 + step) / step) * step, step * 24);
    const floorY = Number.isFinite(minY) ? minY - step * 0.6 : 0;
    const centreX = (minX + maxX) / 2;
    const centreZ = (minZ + maxZ) / 2;

    const common = [0.26, 0.34, 0.44, 0.2];
    const strong = [0.36, 0.48, 0.62, 0.34];
    const values = [];
    const line = (x1, y1, z1, x2, y2, z2, color) => {
      values.push(x1, y1, z1, color[0], color[1], color[2], color[3]);
      values.push(x2, y2, z2, color[0], color[1], color[2], color[3]);
    };

    const steps = Math.round(half / step);
    for (let index = -steps; index <= steps; index += 1) {
      const offset = index * step;
      const color = index === 0 ? strong : common;
      line(centreX - half, floorY, centreZ + offset, centreX + half, floorY, centreZ + offset, color);
      line(centreX + offset, floorY, centreZ - half, centreX + offset, floorY, centreZ + half, color);
    }

    // The semantic axis (Y) and the horizon axes (X and Z) in their own colours.
    const topY = Number.isFinite(maxY) ? maxY + step : floorY + step * 4;
    line(centreX, floorY, centreZ, centreX, topY, centreZ, [0.5, 0.68, 0.86, 0.42]);
    line(centreX - half, floorY, centreZ, centreX + half, floorY, centreZ, [0.85, 0.6, 0.4, 0.3]);
    line(centreX, floorY, centreZ - half, centreX, floorY, centreZ + half, [0.45, 0.62, 0.85, 0.3]);

    gl.bindBuffer(gl.ARRAY_BUFFER, resources.gridBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(values), gl.DYNAMIC_DRAW);
    return values.length / 7;
  }

  const counts = { instances: 0, edgeVertices: 0, gridVertices: 0 };

  function rebuildBuffers() {
    counts.instances = writeInstances();
    counts.edgeVertices = writeEdges();
    counts.gridVertices = writeGrid();
  }

  rebuildBuffers();

  /* ----------------------------------------------------------------------- */
  /* Labels                                                                  */
  /* ----------------------------------------------------------------------- */

  const labelPool = [];
  for (let index = 0; index < MAX_LABELS; index += 1) {
    const element = document.createElement("span");
    element.className = "grid-space-label";
    element.style.position = "absolute";
    element.style.left = "0";
    element.style.top = "0";
    element.style.padding = "2px 6px";
    element.style.borderRadius = "5px";
    element.style.background = "rgba(7, 11, 16, 0.62)";
    element.style.border = "1px solid rgba(120, 150, 180, 0.18)";
    element.style.color = "rgba(206, 220, 236, 0.86)";
    element.style.font = "11px/1.3 system-ui, -apple-system, Segoe UI, sans-serif";
    element.style.whiteSpace = "nowrap";
    element.style.pointerEvents = "none";
    element.style.willChange = "transform, opacity";
    element.style.opacity = "0";
    element.style.display = "none";
    labelLayer.appendChild(element);
    labelPool.push({ element, text: "", x: NaN, y: NaN, opacity: -1, shown: false });
  }

  /** Choose at most 24 labels: selection first, then related, then the nearest. */
  function labelCandidates(viewProjection) {
    const width = state.width;
    const height = state.height;
    const selected = state.selectedId === null ? null : state.nodesById.get(state.selectedId);
    const candidates = [];
    const seen = new Set();

    const push = (node, priority) => {
      if (!node || seen.has(node.id)) return;
      seen.add(node.id);
      const point = projectPoint(viewProjection, node.position, width, height);
      if (!point.visible) return;
      const top = projectPoint(viewProjection, [
        node.position[0], node.position[1] + node.size[1] / 2, node.position[2],
      ], width, height);
      // Float labels above object faces so text never competes with shading.
      candidates.push({ node, point: { ...point, y: top.visible ? top.y - 10 : point.y - 18 }, priority });
    };

    if (selected) push(selected, -2);
    for (const node of state.nodes) {
      if (state.related.has(node.id)) push(node, -1);
    }
    // The hierarchy's own backbone - the project and its collection platforms -
    // outranks an ordinary node: it is what tells a reader where they are, and
    // the pool is short. The collision test still decides what really fits.
    for (const node of state.nodes) {
      if (node.kind === "project") push(node, -0.75);
      else if (node.kind === "collection") push(node, -0.5);
    }

    const rest = [];
    for (const node of state.nodes) {
      if (seen.has(node.id)) continue;
      const point = projectPoint(viewProjection, node.position, width, height);
      if (!point.visible) continue;
      rest.push({ node, point, distance: point.depth });
    }
    rest.sort((a, b) => a.distance - b.distance);
    for (const item of rest) candidates.push({ node: item.node, point: item.point, priority: 0 });
    candidates.sort((a, b) => a.priority - b.priority || a.point.depth - b.point.depth);
    const accepted = [];
    const occupied = [];
    for (const candidate of candidates) {
      if (accepted.length >= MAX_LABELS) break;
      const label = shortTitle(candidate.node.title);
      const labelWidth = Math.min(width - 24, Math.max(54, label.length * 6.8 + 18));
      const rect = {
        left: candidate.point.x - labelWidth / 2,
        right: candidate.point.x + labelWidth / 2,
        top: candidate.point.y - 13,
        bottom: candidate.point.y + 13,
      };
      const overlaps = occupied.some((other) =>
        rect.left < other.right + 10 && rect.right + 10 > other.left
        && rect.top < other.bottom + 8 && rect.bottom + 8 > other.top);
      if (overlaps) continue;
      occupied.push(rect);
      accepted.push(candidate);
    }
    return accepted;
  }

  function updateLabels(viewProjection) {
    if (!state.nodes.length) {
      for (const slot of labelPool) hideLabel(slot);
      state.labelCount = 0;
      return;
    }
    const candidates = labelCandidates(viewProjection);
    let shown = 0;
    for (let index = 0; index < labelPool.length; index += 1) {
      const slot = labelPool[index];
      const candidate = candidates[index];
      if (!candidate) {
        hideLabel(slot);
        continue;
      }
      const title = shortTitle(candidate.node.title);
      const x = Math.round(candidate.point.x);
      const y = Math.round(candidate.point.y);
      const selected = state.selectedId === candidate.node.id;
      if (slot.text !== title) {
        slot.element.textContent = title;
        slot.text = title;
      }
      if (!slot.shown) {
        slot.element.style.display = "block";
        slot.shown = true;
      }
      if (slot.x !== x || slot.y !== y) {
        slot.element.style.transform = `translate(-50%, -50%) translate(${x}px, ${y}px)`;
        slot.x = x;
        slot.y = y;
      }
      const opacity = selected ? 0.98 : candidate.priority === -1 ? 0.85 : 0.62;
      if (slot.opacity !== opacity) {
        slot.element.style.opacity = String(opacity);
        slot.opacity = opacity;
      }
      if (slot.selected !== selected) {
        slot.element.style.borderColor = selected
          ? "rgba(180, 214, 244, 0.55)"
          : "rgba(120, 150, 180, 0.18)";
        slot.selected = selected;
      }
      shown += 1;
    }
    state.labelCount = shown;
  }

  function hideLabel(slot) {
    if (!slot.shown) return;
    slot.element.style.display = "none";
    slot.shown = false;
    slot.text = "";
    slot.x = NaN;
    slot.y = NaN;
    slot.opacity = -1;
    slot.selected = null;
  }

  function shortTitle(title) {
    const text = typeof title === "string" ? title.trim() : "";
    if (!text) return "untitled";
    return text.length > 42 ? `${text.slice(0, 41)}…` : text;
  }

  /* ----------------------------------------------------------------------- */
  /* Rendering                                                               */
  /* ----------------------------------------------------------------------- */

  function drawFrame() {
    if (state.destroyed || state.lost || !state.resources) return;
    const advance = transition.step(clock());
    if (advance.camera) state.camera = clampCamera(advance.camera);
    render();
    if (transition.active) scheduler.schedule();
  }

  function render() {
    const resources = state.resources;
    const uniforms = state.uniforms;
    if (!resources || !uniforms) return;
    if (gl.isContextLost?.()) return;

    const width = state.width;
    const height = state.height;
    const aspect = width / Math.max(height, 1);
    const camera = clampCamera(state.camera);
    const eye = cameraPosition(camera);
    const viewProjection = cameraViewProjection(camera, aspect, state.radius);
    const fogNear = Math.max(camera.distance * 1.3, 40);
    const fogFar = Math.max(camera.distance * 3.2, fogNear + state.radius * 1.6 + 60);

    gl.viewport(0, 0, Math.max(1, Math.round(width * state.dpr)), Math.max(1, Math.round(height * state.dpr)));
    gl.clearColor(BACKGROUND[0], BACKGROUND[1], BACKGROUND[2], 1);
    gl.clearDepth(1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.disable(gl.CULL_FACE);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    let draws = 0;

    // 1. The ground grid and axes.
    if (counts.gridVertices > 0) {
      gl.useProgram(resources.programs.line);
      gl.uniformMatrix4fv(uniforms.line.viewProjection, false, viewProjection);
      gl.uniform3fv(uniforms.line.cameraPos, new Float32Array(eye));
      gl.uniform3fv(uniforms.line.fogColor, new Float32Array(BACKGROUND));
      gl.uniform2f(uniforms.line.fogRange, fogNear, fogFar);
      gl.bindVertexArray(resources.gridVao);
      gl.drawArrays(gl.LINES, 0, counts.gridVertices);
      draws += 1;
    }

    // 2. Every edge, in one batched call.
    if (counts.edgeVertices > 0) {
      gl.useProgram(resources.programs.line);
      gl.uniformMatrix4fv(uniforms.line.viewProjection, false, viewProjection);
      gl.uniform3fv(uniforms.line.cameraPos, new Float32Array(eye));
      gl.uniform3fv(uniforms.line.fogColor, new Float32Array(BACKGROUND));
      gl.uniform2f(uniforms.line.fogRange, fogNear, fogFar);
      gl.bindVertexArray(resources.edgeVao);
      gl.drawArrays(gl.LINES, 0, counts.edgeVertices);
      draws += 1;
    }

    // 3. Every node, in one instanced call.
    if (counts.instances > 0) {
      gl.useProgram(resources.programs.cube);
      gl.uniformMatrix4fv(uniforms.cube.viewProjection, false, viewProjection);
      gl.uniform3fv(uniforms.cube.cameraPos, new Float32Array(eye));
      gl.uniform3f(uniforms.cube.lightDir, 0.42, 0.82, 0.38);
      gl.uniform3fv(uniforms.cube.fogColor, new Float32Array(BACKGROUND));
      gl.uniform2f(uniforms.cube.fogRange, fogNear, fogFar);
      gl.bindVertexArray(resources.cubeVao);
      gl.drawElementsInstanced(gl.TRIANGLES, 36, gl.UNSIGNED_SHORT, 0, counts.instances);
      draws += 1;
    }

    gl.bindVertexArray(null);
    state.drawCalls = draws;
    state.frames += 1;
    updateLabels(viewProjection);
  }

  /* ----------------------------------------------------------------------- */
  /* Size                                                                    */
  /* ----------------------------------------------------------------------- */

  function measure() {
    const rect = typeof container.getBoundingClientRect === "function" ? container.getBoundingClientRect() : null;
    const width = Math.max(1, Math.round(rect?.width || container.clientWidth || 1));
    const height = Math.max(1, Math.round(rect?.height || container.clientHeight || 1));
    const ratio = Number.isFinite(window?.devicePixelRatio) ? window.devicePixelRatio : 1;
    return { width, height, dpr: Math.min(Math.max(ratio, 1), DPR_CAP) };
  }

  function resize() {
    if (state.destroyed) return api;
    const wasTiny = state.width <= 1 || state.height <= 1;
    const size = measure();
    state.width = size.width;
    state.height = size.height;
    state.dpr = size.dpr;
    const pixelWidth = Math.max(1, Math.round(size.width * size.dpr));
    const pixelHeight = Math.max(1, Math.round(size.height * size.dpr));
    if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
    if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
    // The view may have been created in a hidden panel: the first real size is
    // the first moment a sane framing is possible, so frame again then.
    if (wasTiny && state.fitted && size.width > 2 && size.height > 2 && state.nodes.length) frameImmediately();
    else schedule();
    return api;
  }

  /* ----------------------------------------------------------------------- */
  /* Camera moves                                                            */
  /* ----------------------------------------------------------------------- */

  function schedule() {
    if (state.destroyed || state.lost) return;
    scheduler.schedule();
  }

  function stopTransition() {
    if (transition.active) transition.cancel();
  }
  /** The camera that frames the scene, without any motion (also the first frame). */
  function frameImmediately() {
    const size = measure();
    const aspect = size.width / Math.max(size.height, 1);
    const fit = fitCamera(state.scene.bounds, { aspect });
    state.camera = clampCamera({ ...state.camera, target: fit.target, distance: fit.distance });
    state.fitted = true;
    schedule();
  }

  /** Frame the whole scene: a short move, or none at all under reduced motion. */
  function fitToScene() {
    const size = measure();
    const aspect = size.width / Math.max(size.height, 1);
    const fit = fitCamera(state.scene.bounds, { aspect });
    const goal = { target: fit.target, distance: fit.distance };
    const result = transition.start(state.camera, goal, clock());
    state.camera = clampCamera(result.camera);
    state.fitted = true;
    schedule();
  }

  function focus(nodeId) {
    if (state.destroyed) return false;
    const node = state.nodesById.get(nodeId);
    if (!node) return false;
    const goal = focusGoal(node);
    const result = transition.start(state.camera, goal, clock());
    state.camera = clampCamera(result.camera);
    schedule();
    return true;
  }

  /* ----------------------------------------------------------------------- */
  /* Selection                                                               */
  /* ----------------------------------------------------------------------- */

  function emitSelect(nodeId) {
    try {
      state.onSelect(nodeId);
    } catch (error) {
      // A broken callback must not stop the view from drawing.
      if (typeof window?.setTimeout === "function") window.setTimeout(() => { throw error; }, 0);
    }
  }

  function selectAt(clientX, clientY) {
    const rect = typeof canvas.getBoundingClientRect === "function" ? canvas.getBoundingClientRect() : null;
    const width = Math.max(1, rect?.width || state.width);
    const height = Math.max(1, rect?.height || state.height);
    const left = rect?.left ?? 0;
    const top = rect?.top ?? 0;
    const ndcX = ((clientX - left) / width) * 2 - 1;
    const ndcY = 1 - ((clientY - top) / height) * 2;
    const ray = screenRay(state.camera, width / height, ndcX, ndcY, state.radius);
    const hit = pickNode(state.nodes, ray);
    emitSelect(hit ? hit.id : null);
    return hit ? hit.id : null;
  }

  /* ----------------------------------------------------------------------- */
  /* Pointer, wheel, keyboard                                                */
  /* ----------------------------------------------------------------------- */

  const listeners = [];
  function listen(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    listeners.push({ target, type, handler, options });
  }

  function onPointerDown(event) {
    if (state.destroyed) return;
    if (typeof canvas.setPointerCapture === "function") {
      try {
        canvas.setPointerCapture(event.pointerId);
      } catch (error) {
        // Some synthetic pointers cannot be captured; the view still works.
      }
    }
    if (typeof canvas.focus === "function") {
      try {
        canvas.focus({ preventScroll: true });
      } catch (error) {
        canvas.focus();
      }
    }
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (state.pointers.size === 1) {
      state.drag = {
        mode: event.button === 2 || event.shiftKey ? "pan" : "orbit",
        lastX: event.clientX,
        lastY: event.clientY,
        startX: event.clientX,
        startY: event.clientY,
        startedAt: clock(),
        moved: false,
      };
      canvas.style.cursor = "grabbing";
    } else {
      state.drag = null;
      state.pinch = null;
    }
    schedule();
  }

  function onPointerMove(event) {
    const pointer = state.pointers.get(event.pointerId);
    if (!pointer) return;
    pointer.x = event.clientX;
    pointer.y = event.clientY;
    if (state.destroyed) return;

    if (state.pointers.size >= 2) {
      handlePinch();
      return;
    }
    const drag = state.drag;
    if (!drag) return;
    const dx = event.clientX - drag.lastX;
    const dy = event.clientY - drag.lastY;
    drag.lastX = event.clientX;
    drag.lastY = event.clientY;
    if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) > CLICK_SLOP) drag.moved = true;
    if (!dx && !dy) return;

    stopTransition();
    state.camera = clampCamera(
      drag.mode === "pan"
        ? panCamera(state.camera, dx, dy, state.height)
        : orbitCamera(state.camera, dx, dy, ORBIT_SENSITIVITY),
    );
    schedule();
  }

  function handlePinch() {
    const points = [...state.pointers.values()].slice(0, 2);
    if (points.length < 2) return;
    const centreX = (points[0].x + points[1].x) / 2;
    const centreY = (points[0].y + points[1].y) / 2;
    const spread = Math.max(Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y), 1);
    if (!state.pinch) {
      state.pinch = { centreX, centreY, spread };
      return;
    }
    const dx = centreX - state.pinch.centreX;
    const dy = centreY - state.pinch.centreY;
    const ratio = spread / state.pinch.spread;
    state.pinch = { centreX, centreY, spread };
    stopTransition();
    let camera = state.camera;
    if (Math.abs(dx) > 0.2 || Math.abs(dy) > 0.2) camera = panCamera(camera, dx, dy, state.height);
    if (Math.abs(ratio - 1) > 0.004) camera = zoomCamera(camera, 1 / ratio);
    state.camera = clampCamera(camera);
    schedule();
  }

  function onPointerUp(event) {
    const pointer = state.pointers.get(event.pointerId);
    if (!pointer) return;
    state.pointers.delete(event.pointerId);
    if (typeof canvas.releasePointerCapture === "function") {
      try {
        canvas.releasePointerCapture(event.pointerId);
      } catch (error) {
        // Already released, or never captured.
      }
    }

    const drag = state.drag;
    if (state.pointers.size === 0) {
      state.drag = null;
      state.pinch = null;
      canvas.style.cursor = "grab";
      if (drag && !drag.moved && clock() - drag.startedAt < 700) {
        selectAt(event.clientX, event.clientY);
      }
      return;
    }
    // One finger left after a pinch: continue with an orbit from where it is.
    const remaining = [...state.pointers.values()][0];
    state.pinch = null;
    state.drag = {
      mode: "orbit",
      lastX: remaining.x,
      lastY: remaining.y,
      startX: remaining.x,
      startY: remaining.y,
      startedAt: clock(),
      moved: true,
    };
  }

  function onPointerCancel(event) {
    state.pointers.delete(event.pointerId);
    if (state.pointers.size === 0) {
      state.drag = null;
      state.pinch = null;
      canvas.style.cursor = "grab";
    }
  }

  function onWheel(event) {
    if (state.destroyed) return;
    if (typeof event.preventDefault === "function") event.preventDefault();
    const delta = clamp(event.deltaY, -240, 240);
    stopTransition();
    state.camera = zoomCamera(state.camera, Math.exp(delta * 0.0016));
    schedule();
  }

  function onKeyDown(event) {
    if (state.destroyed) return;
    const key = event.key;
    let handled = true;
    switch (key) {
      case "ArrowLeft":
        state.camera = orbitCamera(state.camera, -60, 0, ORBIT_SENSITIVITY);
        break;
      case "ArrowRight":
        state.camera = orbitCamera(state.camera, 60, 0, ORBIT_SENSITIVITY);
        break;
      case "ArrowUp":
        state.camera = orbitCamera(state.camera, 0, KEY_ORBIT_STEP / ORBIT_SENSITIVITY, ORBIT_SENSITIVITY);
        break;
      case "ArrowDown":
        state.camera = orbitCamera(state.camera, 0, -KEY_ORBIT_STEP / ORBIT_SENSITIVITY, ORBIT_SENSITIVITY);
        break;
      case "+":
      case "=":
        state.camera = zoomCamera(state.camera, 1 / KEY_ZOOM_STEP);
        break;
      case "-":
      case "_":
        state.camera = zoomCamera(state.camera, KEY_ZOOM_STEP);
        break;
      case "Home":
        fitToScene();
        break;
      case "Enter":
        if (state.selectedId !== null && state.nodesById.has(state.selectedId)) emitSelect(state.selectedId);
        break;
      default:
        handled = false;
    }
    if (!handled) return;
    if (typeof event.preventDefault === "function") event.preventDefault();
    if (key === "Home" || key === "Enter") return;
    stopTransition();
    schedule();
  }

  function onContextMenu(event) {
    if (typeof event.preventDefault === "function") event.preventDefault();
  }

  listen(canvas, "pointerdown", onPointerDown);
  listen(canvas, "pointermove", onPointerMove);
  listen(canvas, "pointerup", onPointerUp);
  listen(canvas, "pointercancel", onPointerCancel);
  listen(canvas, "wheel", onWheel, { passive: false });
  listen(canvas, "keydown", onKeyDown);
  listen(canvas, "contextmenu", onContextMenu);
  listen(canvas, "webglcontextlost", (event) => {
    if (typeof event.preventDefault === "function") event.preventDefault();
    state.lost = true;
    scheduler.cancel();
  });
  listen(canvas, "webglcontextrestored", () => {
    state.lost = false;
    buildResources();
    rebuildBuffers();
    resize();
    schedule();
  });

  // Resize, with a listener fallback for a browser without ResizeObserver.
  let observer = null;
  if (typeof window?.ResizeObserver === "function") {
    observer = new window.ResizeObserver(() => resize());
    observer.observe(container);
  } else if (window && typeof window.addEventListener === "function") {
    listen(window, "resize", () => resize());
  }

  /* ----------------------------------------------------------------------- */
  /* Public API                                                              */
  /* ----------------------------------------------------------------------- */

  const api = {
    canvas,

    /**
     * Replace the scene, the selection and the motion preference. Keys that are
     * absent are left as they are. The first scene is framed automatically;
     * later ones keep the reader's camera where it is.
     */
    update(next = {}) {
      if (state.destroyed) return api;
      const options = next && typeof next === "object" ? next : {};
      let sceneChanged = false;
      if ("scene" in options) {
        state.scene = normalizeScene(options.scene);
        sceneChanged = true;
      }
      if ("selectedId" in options) {
        state.selectedId = typeof options.selectedId === "string" ? options.selectedId : null;
        sceneChanged = true;
      }
      if ("reducedMotion" in options) {
        state.reducedMotion = Boolean(options.reducedMotion);
        transition.setReducedMotion(state.reducedMotion);
      }
      if ("onSelect" in options && typeof options.onSelect === "function") {
        state.onSelect = options.onSelect;
      }
      if (sceneChanged) {
        normalizeLocal();
        rebuildBuffers();
        if (!state.fitted && state.nodes.length) frameImmediately();
        else schedule();
      } else {
        schedule();
      }
      return api;
    },

    /** Move the camera to a node: instantly under reduced motion, briefly otherwise. */
    focus(nodeId) {
      return focus(nodeId);
    },

    /** Re-read the container size (also happens automatically). */
    resize() {
      return resize();
    },

    /** Release every listener, observer, pending frame, GL object and DOM node. */
    destroy() {
      if (state.destroyed) return;
      state.destroyed = true;
      scheduler.dispose();
      transition.cancel();
      for (const { target, type, handler, options } of listeners.splice(0)) {
        target.removeEventListener?.(type, handler, options);
      }
      if (observer) {
        observer.disconnect();
        observer = null;
      }
      disposeResources();
      state.pointers.clear();
      if (typeof root.remove === "function") root.remove();
      else if (root.parentNode) root.parentNode.removeChild(root);
    },

    /** What the last frame cost: nodes, edges, draw calls, labels and frames drawn. */
    stats() {
      return {
        nodes: state.nodes.length,
        edges: state.edges.length,
        drawCalls: state.drawCalls,
        labels: state.labelCount,
        frames: state.frames,
      };
    },
  };

  // The very first frame: size the canvas and draw the scene that came with it.
  resize();
  if (state.nodes.length) {
    rebuildBuffers();
    frameImmediately();
  }
  schedule();

  return api;
}

/**
 * A scene the renderer can trust: nodes with a real finite position, edges
 * whose both ends are in this very scene, and bounds computed here when the
 * scene does not carry usable ones. A scene that is not an object is empty.
 */
function normalizeScene(scene) {
  const source = scene && typeof scene === "object" ? scene : {};
  const rawNodes = Array.isArray(source.nodes) ? source.nodes : [];
  const nodes = [];
  for (const node of rawNodes) {
    if (
      !node ||
      typeof node.id !== "string" ||
      !Array.isArray(node.position) ||
      !node.position.every((value) => Number.isFinite(value))
    ) {
      continue;
    }
    nodes.push(node);
  }
  const ids = new Set(nodes.map((node) => node.id));
  const rawEdges = Array.isArray(source.edges) ? source.edges : [];
  const edges = rawEdges.filter((edge) => edge && ids.has(edge.from) && ids.has(edge.to) && edge.from !== edge.to);

  let bounds = source.bounds;
  if (
    !bounds ||
    !Array.isArray(bounds.min) ||
    !Array.isArray(bounds.max) ||
    ![...bounds.min, ...bounds.max].every((value) => Number.isFinite(value))
  ) {
    bounds = computeBounds(nodes);
  }

  return {
    version: typeof source.version === "number" ? source.version : 1,
    projectId: typeof source.projectId === "string" ? source.projectId : null,
    nodes,
    edges,
    bounds,
    timeRange: source.timeRange ?? { from: null, to: null },
  };
}

/** The extents of a node set: position ± size / 2, never NaN. */
function computeBounds(nodes) {
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (const node of nodes) {
    for (let axis = 0; axis < 3; axis += 1) {
      const at = node.position[axis];
      const half = Array.isArray(node.size) && Number.isFinite(node.size[axis]) ? Math.abs(node.size[axis]) / 2 : 0;
      min[axis] = Math.min(min[axis], at - half);
      max[axis] = Math.max(max[axis], at + half);
    }
  }
  for (let axis = 0; axis < 3; axis += 1) {
    if (!Number.isFinite(min[axis]) || !Number.isFinite(max[axis])) {
      min[axis] = 0;
      max[axis] = 0;
    }
  }
  return { min, max };
}

/** The limits and defaults this module honours, for an integrator that wants them. */
export const SPACE_LIMITS = Object.freeze({
  dprCap: DPR_CAP,
  maxLabels: MAX_LABELS,
  minDistance: CAMERA_LIMITS.minDistance,
  maxDistance: CAMERA_LIMITS.maxDistance,
  fov: DEFAULT_CAMERA.fov / DEG,
});
