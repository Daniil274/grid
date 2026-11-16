/**
 * Pure geometry of the Space renderer (web_chat/js/grid/space_math.js).
 *
 * Everything the WebGL view needs to decide *where the camera is* and *what a
 * click hits*, without a canvas: 4x4 matrices, an orbit camera, a ray/AABB pick,
 * the framing of a scene, the palettes, a frame scheduler and a camera
 * transition. Nothing here touches the DOM, WebGL or a real clock, so the same
 * inputs always give the same numbers and the whole module is testable in
 * plain node.
 *
 * Conventions:
 *
 * - a vector is a plain ``[x, y, z]`` array; a matrix is a column-major
 *   ``Float32Array(16)`` exactly as WebGL expects it;
 * - world space is the SceneModel's own space (see ``grid/scene.js``):
 *   X across, Y up by semantic level, Z by day;
 * - the camera looks at ``target`` from ``distance`` away, at ``yaw`` around Y
 *   and ``pitch`` above the horizon, with a perspective projection.
 *
 * ## Contract
 *
 * ```text
 * mat4Identity() mat4Multiply(a, b) mat4Perspective(fovY, aspect, near, far)
 * mat4LookAt(eye, target, up) mat4Invert(m) mat4TransformPoint(m, p)
 * createCamera(opts?) orbitCamera(cam, dx, dy) panCamera(cam, dx, dy, viewport)
 * zoomCamera(cam, factor) clampCamera(cam) cameraPosition(cam)
 * cameraView(cam) cameraProjection(cam, aspect) cameraViewProjection(cam, aspect)
 * viewProjectionOf(cam, aspect) cameraClip(cam, sceneRadius)
 * boundsCenter(bounds) boundsRadius(bounds) fitCamera(bounds, view)
 * screenRay(cam, aspect, ndcX, ndcY) projectPoint(vp, point, width, height)
 * rayAabb(origin, dir, min, max) nodeAabb(node, padding) pickNode(nodes, ray)
 * focusGoal(node) createCameraTransition(opts?) createRenderScheduler(opts?)
 * NODE_COLORS EDGE_COLORS CAMERA_LIMITS DEFAULT_CAMERA
 * ```
 *
 * @module grid/space_math
 */

/** Radians in one degree. */
export const DEG = Math.PI / 180;

/** A number that is really finite, or the fallback: never NaN in geometry. */
export function finite(value, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** A scalar clamped into an inclusive range. */
export function clamp(value, min, max) {
  const at = typeof value === "number" ? value : min;
  if (Number.isNaN(at)) return min;
  if (at < min) return min;
  if (at > max) return max;
  return at;
}

/** A finite `[x, y, z]` from anything shaped like a vector. */
export function vec3(value, fallback = [0, 0, 0]) {
  const at = Array.isArray(value) || ArrayBuffer.isView(value) ? value : null;
  return [
    finite(at?.[0], fallback[0]),
    finite(at?.[1], fallback[1]),
    finite(at?.[2], fallback[2]),
  ];
}

export function v3add(a, b) {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

export function v3sub(a, b) {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

export function v3scale(a, k) {
  return [a[0] * k, a[1] * k, a[2] * k];
}

export function v3dot(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

export function v3cross(a, b) {
  return [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
}

export function v3length(a) {
  return Math.hypot(a[0], a[1], a[2]);
}

export function v3normalize(a) {
  const length = v3length(a);
  if (!(length > 1e-12)) return [0, 0, 0];
  return [a[0] / length, a[1] / length, a[2] / length];
}

export function v3lerp(a, b, t) {
  const k = clamp(t, 0, 1);
  return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];
}

/* ------------------------------------------------------------------------- */
/* Matrices (column-major, WebGL order)                                       */
/* ------------------------------------------------------------------------- */

/** A fresh identity matrix. */
export function mat4Identity(out = new Float32Array(16)) {
  out.fill(0);
  out[0] = 1;
  out[5] = 1;
  out[10] = 1;
  out[15] = 1;
  return out;
}

/**
 * ``out = a * b`` - the product applies ``b`` first, then ``a``, matching the
 * ``projection * view`` order WebGL shaders expect.
 */
export function mat4Multiply(a, b, out = new Float32Array(16)) {
  const left = Array.from(a);
  const right = Array.from(b);
  for (let column = 0; column < 4; column += 1) {
    for (let row = 0; row < 4; row += 1) {
      let sum = 0;
      for (let k = 0; k < 4; k += 1) sum += left[k * 4 + row] * right[column * 4 + k];
      out[column * 4 + row] = sum;
    }
  }
  return out;
}

/**
 * The classic perspective projection. ``fovY`` is the vertical field of view
 * in radians; ``aspect`` is width / height (a degenerate aspect is treated as
 * 1 so a zero-height panel cannot produce NaN).
 */
export function mat4Perspective(fovY, aspect, near, far, out = new Float32Array(16)) {
  const fov = clamp(fovY, 1 * DEG, 179 * DEG);
  const ratio = aspect > 0 && Number.isFinite(aspect) ? aspect : 1;
  const n = Math.max(finite(near, 0.1), 1e-4);
  const f = Math.max(finite(far, 1000), n * 1.0001);
  const focal = 1 / Math.tan(fov / 2);
  out.fill(0);
  out[0] = focal / ratio;
  out[5] = focal;
  out[10] = (f + n) / (n - f);
  out[11] = -1;
  out[14] = (2 * f * n) / (n - f);
  return out;
}

/** A view matrix for an eye looking at a target, up being roughly world up. */
export function mat4LookAt(eye, target, up = [0, 1, 0], out = new Float32Array(16)) {
  const from = vec3(eye);
  const at = vec3(target);
  let back = v3normalize(v3sub(from, at));
  if (!back[0] && !back[1] && !back[2]) back = [0, 0, 1];
  let right = v3normalize(v3cross(vec3(up, [0, 1, 0]), back));
  if (!right[0] && !right[1] && !right[2]) {
    // Looking straight up or down: any perpendicular right vector will do.
    right = v3normalize(v3cross([0, 0, 1], back));
    if (!right[0] && !right[1] && !right[2]) right = [1, 0, 0];
  }
  const up2 = v3cross(back, right);
  out[0] = right[0];
  out[1] = up2[0];
  out[2] = back[0];
  out[3] = 0;
  out[4] = right[1];
  out[5] = up2[1];
  out[6] = back[1];
  out[7] = 0;
  out[8] = right[2];
  out[9] = up2[2];
  out[10] = back[2];
  out[11] = 0;
  out[12] = -v3dot(right, from);
  out[13] = -v3dot(up2, from);
  out[14] = -v3dot(back, from);
  out[15] = 1;
  return out;
}

/** The inverse of a general 4x4 matrix, or a zero matrix when it is singular. */
export function mat4Invert(m, out = new Float32Array(16)) {
  const a = Array.from(m);
  const a00 = a[0], a01 = a[1], a02 = a[2], a03 = a[3];
  const a10 = a[4], a11 = a[5], a12 = a[6], a13 = a[7];
  const a20 = a[8], a21 = a[9], a22 = a[10], a23 = a[11];
  const a30 = a[12], a31 = a[13], a32 = a[14], a33 = a[15];

  const b00 = a00 * a11 - a01 * a10;
  const b01 = a00 * a12 - a02 * a10;
  const b02 = a00 * a13 - a03 * a10;
  const b03 = a01 * a12 - a02 * a11;
  const b04 = a01 * a13 - a03 * a11;
  const b05 = a02 * a13 - a03 * a12;
  const b06 = a20 * a31 - a21 * a30;
  const b07 = a20 * a32 - a22 * a30;
  const b08 = a20 * a33 - a23 * a30;
  const b09 = a21 * a32 - a22 * a31;
  const b10 = a21 * a33 - a23 * a31;
  const b11 = a22 * a33 - a23 * a32;

  let det = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07 + b05 * b06;
  if (!det || !Number.isFinite(det)) {
    out.fill(0);
    return out;
  }
  det = 1 / det;
  out[0] = (a11 * b11 - a12 * b10 + a13 * b09) * det;
  out[1] = (a02 * b10 - a01 * b11 - a03 * b09) * det;
  out[2] = (a31 * b05 - a32 * b04 + a33 * b03) * det;
  out[3] = (a22 * b04 - a21 * b05 - a23 * b03) * det;
  out[4] = (a12 * b08 - a10 * b11 - a13 * b07) * det;
  out[5] = (a00 * b11 - a02 * b08 + a03 * b07) * det;
  out[6] = (a32 * b02 - a30 * b05 - a33 * b01) * det;
  out[7] = (a20 * b05 - a22 * b02 + a23 * b01) * det;
  out[8] = (a10 * b10 - a11 * b08 + a13 * b06) * det;
  out[9] = (a01 * b08 - a00 * b10 - a03 * b06) * det;
  out[10] = (a30 * b04 - a31 * b02 + a33 * b00) * det;
  out[11] = (a21 * b02 - a20 * b04 - a23 * b00) * det;
  out[12] = (a11 * b07 - a10 * b09 - a12 * b06) * det;
  out[13] = (a00 * b09 - a01 * b07 + a02 * b06) * det;
  out[14] = (a31 * b01 - a30 * b03 - a32 * b00) * det;
  out[15] = (a20 * b03 - a21 * b01 + a22 * b00) * det;
  return out;
}

/** ``m * [x, y, z, 1]`` with the perspective divide. */
export function mat4TransformPoint(m, point, out = [0, 0, 0]) {
  const x = finite(point?.[0]);
  const y = finite(point?.[1]);
  const z = finite(point?.[2]);
  const w = m[3] * x + m[7] * y + m[11] * z + m[15];
  const scale = Math.abs(w) > 1e-12 ? 1 / w : 0;
  out[0] = (m[0] * x + m[4] * y + m[8] * z + m[12]) * scale;
  out[1] = (m[1] * x + m[5] * y + m[9] * z + m[13]) * scale;
  out[2] = (m[2] * x + m[6] * y + m[10] * z + m[14]) * scale;
  return out;
}

/* ------------------------------------------------------------------------- */
/* The orbit camera                                                           */
/* ------------------------------------------------------------------------- */

/** What the camera keeps between two frames. */
export const DEFAULT_CAMERA = Object.freeze({
  target: Object.freeze([0, 0, 0]),
  distance: 140,
  yaw: 0.62,
  pitch: 0.42,
  fov: 50 * DEG,
});

/** The hard limits of an orbit camera: it never turns over or flies inside a node. */
export const CAMERA_LIMITS = Object.freeze({
  minPitch: -1.45,
  maxPitch: 1.45,
  minDistance: 5,
  maxDistance: 40000,
});

/** A camera from partial options, every field finite and inside its limit. */
export function createCamera(options = {}) {
  const base = options.base ?? DEFAULT_CAMERA;
  return clampCamera({
    target: vec3(options.target ?? base.target),
    distance: options.distance ?? base.distance,
    yaw: options.yaw ?? base.yaw,
    pitch: options.pitch ?? base.pitch,
    fov: options.fov ?? base.fov,
  });
}

/** The same camera with pitch and distance inside their limits and no NaN. */
export function clampCamera(camera) {
  const base = camera ?? DEFAULT_CAMERA;
  return {
    target: vec3(base.target),
    distance: clamp(base.distance ?? DEFAULT_CAMERA.distance, CAMERA_LIMITS.minDistance, CAMERA_LIMITS.maxDistance),
    yaw: finite(base.yaw ?? DEFAULT_CAMERA.yaw),
    pitch: clamp(base.pitch ?? DEFAULT_CAMERA.pitch, CAMERA_LIMITS.minPitch, CAMERA_LIMITS.maxPitch),
    fov: clamp(base.fov ?? DEFAULT_CAMERA.fov, 20 * DEG, 90 * DEG),
  };
}

/** Where the camera stands: ``target`` pushed back by ``distance`` along its orbit. */
export function cameraPosition(camera) {
  const cam = clampCamera(camera);
  const cosPitch = Math.cos(cam.pitch);
  return [
    cam.target[0] + cam.distance * cosPitch * Math.sin(cam.yaw),
    cam.target[1] + cam.distance * Math.sin(cam.pitch),
    cam.target[2] + cam.distance * cosPitch * Math.cos(cam.yaw),
  ];
}

/** The camera's own axes: forward (to the target), right and up, in world space. */
export function cameraBasis(camera) {
  const cam = clampCamera(camera);
  const position = cameraPosition(cam);
  const forward = v3normalize(v3sub(cam.target, position));
  const right = v3normalize(v3cross(forward, [0, 1, 0]));
  const up = v3cross(right, forward);
  return { position, forward, right, up };
}

/** A new camera turned by a pointer delta in pixels. */
export function orbitCamera(camera, dx, dy, sensitivity = 0.0055) {
  const cam = clampCamera(camera);
  const step = finite(sensitivity, 0.0055);
  return clampCamera({
    ...cam,
    yaw: cam.yaw - finite(dx) * step,
    pitch: cam.pitch + finite(dy) * step,
  });
}

/**
 * A new camera whose target slid across the view plane, so the scene follows
 * the hand. ``viewportHeight`` is in CSS pixels; the scale is the world size of
 * one pixel at the target's depth.
 */
export function panCamera(camera, dx, dy, viewportHeight = 800, fov = null) {
  const cam = clampCamera(camera);
  const height = Math.max(finite(viewportHeight, 800), 1);
  const worldPerPixel = (2 * cam.distance * Math.tan((fov ?? cam.fov) / 2)) / height;
  const basis = cameraBasis(cam);
  const target = v3add(
    cam.target,
    v3add(v3scale(basis.right, -finite(dx) * worldPerPixel), v3scale(basis.up, finite(dy) * worldPerPixel)),
  );
  return clampCamera({ ...cam, target });
}

/** A new camera at ``distance * factor``, never inside or beyond the limits. */
export function zoomCamera(camera, factor) {
  const cam = clampCamera(camera);
  const k = finite(factor, 1);
  if (k <= 0) return cam;
  return clampCamera({ ...cam, distance: cam.distance * k });
}

/** The near and far planes of a camera, kept tight enough for a 24-bit depth buffer. */
export function cameraClip(camera, sceneRadius = 0) {
  const cam = clampCamera(camera);
  const radius = Math.max(finite(sceneRadius, 0), 0);
  const near = Math.max(cam.distance * 0.006, 0.05);
  const far = Math.max(cam.distance * 9, radius * 14, near * 40);
  return { near, far: Math.min(far, near * 60000) };
}

/** The view matrix of a camera. */
export function cameraView(camera) {
  return mat4LookAt(cameraPosition(camera), clampCamera(camera).target, [0, 1, 0]);
}

/** The projection matrix of a camera at a given aspect ratio. */
export function cameraProjection(camera, aspect, sceneRadius = 0) {
  const cam = clampCamera(camera);
  const clip = cameraClip(cam, sceneRadius);
  return mat4Perspective(cam.fov, aspect, clip.near, clip.far);
}

/** ``projection * view`` - what a shader gets and what a picking ray is built from. */
export function cameraViewProjection(camera, aspect, sceneRadius = 0) {
  return mat4Multiply(cameraProjection(camera, aspect, sceneRadius), cameraView(camera));
}

/* ------------------------------------------------------------------------- */
/* Framing a scene                                                            */
/* ------------------------------------------------------------------------- */

/** The centre of a scene's bounds (the origin for an empty or broken one). */
export function boundsCenter(bounds) {
  const min = vec3(bounds?.min);
  const max = vec3(bounds?.max);
  return [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
}

/** The radius of the sphere around a scene's bounds. */
export function boundsRadius(bounds) {
  const min = vec3(bounds?.min);
  const max = vec3(bounds?.max);
  const span = [max[0] - min[0], max[1] - min[1], max[2] - min[2]];
  const radius = Math.hypot(span[0], span[1], span[2]) / 2;
  return Number.isFinite(radius) && radius > 0 ? radius : 0;
}

/**
 * The target and distance that frame a scene in a viewport: the whole bounds
 * sphere fits both the vertical and the horizontal field of view, with a small
 * margin. An empty scene gets a sane, finite default instead of a division by
 * zero, and a huge one stays finite too.
 */
export function fitCamera(bounds, { aspect = 1, fov = DEFAULT_CAMERA.fov, margin = 1.18 } = {}) {
  const target = boundsCenter(bounds);
  const radius = boundsRadius(bounds);
  const vertical = clamp(fov, 20 * DEG, 90 * DEG);
  const ratio = aspect > 0 && Number.isFinite(aspect) ? aspect : 1;
  const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * ratio);
  const half = Math.min(vertical, horizontal) / 2;
  if (!(radius > 0)) {
    return { target, distance: 90, radius: 0 };
  }
  const distance = (radius * clamp(margin, 1, 3)) / Math.max(Math.sin(half), 0.02);
  return {
    target,
    distance: clamp(distance, CAMERA_LIMITS.minDistance, CAMERA_LIMITS.maxDistance),
    radius,
  };
}

/** The camera that frames *bounds* for a viewport, keeping the current orbit angles. */
export function frameScene(bounds, { aspect = 1, fov = DEFAULT_CAMERA.fov, camera = null } = {}) {
  const fit = fitCamera(bounds, { aspect, fov });
  return clampCamera({
    ...(camera ?? DEFAULT_CAMERA),
    target: fit.target,
    distance: fit.distance,
    fov,
  });
}

/* ------------------------------------------------------------------------- */
/* Picking                                                                    */
/* ------------------------------------------------------------------------- */

/**
 * The world-space ray through a normalized device coordinate (``ndcX`` and
 * ``ndcY`` in [-1, 1], Y up). Built by unprojecting the near and far points of
 * the view-projection, so it is exactly the ray the shader drew.
 */
export function screenRay(camera, aspect, ndcX, ndcY, sceneRadius = 0) {
  const cam = clampCamera(camera);
  const vp = cameraViewProjection(cam, aspect, sceneRadius);
  const inverse = mat4Invert(vp);
  const near = mat4TransformPoint(inverse, [finite(ndcX), finite(ndcY), -1]);
  const far = mat4TransformPoint(inverse, [finite(ndcX), finite(ndcY), 1]);
  let direction = v3normalize(v3sub(far, near));
  if (!direction[0] && !direction[1] && !direction[2]) {
    direction = cameraBasis(cam).forward;
  }
  return { origin: near, direction };
}

/**
 * The slabs test: where a ray enters an axis-aligned box, or ``null`` when it
 * misses. A ray starting inside the box enters at 0. A direction component of
 * exactly zero is handled without producing NaN.
 */
export function rayAabb(origin, direction, min, max) {
  const o = vec3(origin);
  const d = vec3(direction);
  const lo = vec3(min);
  const hi = vec3(max);
  let entry = -Infinity;
  let exit = Infinity;
  for (let axis = 0; axis < 3; axis += 1) {
    const low = Math.min(lo[axis], hi[axis]);
    const high = Math.max(lo[axis], hi[axis]);
    if (Math.abs(d[axis]) < 1e-12) {
      if (o[axis] < low || o[axis] > high) return null;
      continue;
    }
    let t0 = (low - o[axis]) / d[axis];
    let t1 = (high - o[axis]) / d[axis];
    if (t0 > t1) {
      const swap = t0;
      t0 = t1;
      t1 = swap;
    }
    if (t0 > entry) entry = t0;
    if (t1 < exit) exit = t1;
    if (entry > exit) return null;
  }
  if (exit < 0) return null;
  const hit = entry >= 0 ? entry : 0;
  return { t: hit, inside: entry < 0 };
}

/** The world-space box of a SceneNode, widened by ``padding`` scene units. */
export function nodeAabb(node, padding = 0) {
  const centre = vec3(node?.position);
  const size = vec3(node?.size, [1, 1, 1]);
  const pad = Math.max(finite(padding, 0), 0);
  const half = [
    Math.max(Math.abs(size[0]) / 2, 0.35) + pad,
    Math.max(Math.abs(size[1]) / 2, 0.35) + pad,
    Math.max(Math.abs(size[2]) / 2, 0.35) + pad,
  ];
  return {
    min: [centre[0] - half[0], centre[1] - half[1], centre[2] - half[2]],
    max: [centre[0] + half[0], centre[1] + half[1], centre[2] + half[2]],
  };
}

/**
 * The node a ray hits first, by real ray/AABB distance - not by a screen-space
 * guess. Nodes without a finite position are skipped rather than stolen by
 * another node's box.
 */
export function pickNode(nodes, ray, { padding = 0.7 } = {}) {
  const origin = vec3(ray?.origin);
  const direction = vec3(ray?.direction);
  let best = null;
  for (const node of Array.isArray(nodes) ? nodes : []) {
    if (!node || typeof node.id !== "string") continue;
    const position = node.position;
    if (!Array.isArray(position) || !position.every((value) => Number.isFinite(value))) continue;
    const box = nodeAabb(node, padding);
    const hit = rayAabb(origin, direction, box.min, box.max);
    if (!hit) continue;
    if (best === null || hit.t < best.t) best = { id: node.id, node, t: hit.t };
  }
  return best;
}

/** Where a world point lands on screen, in CSS pixels, plus whether it is in front. */
export function projectPoint(viewProjection, point, width, height) {
  const p = vec3(point);
  const w = viewProjection[3] * p[0] + viewProjection[7] * p[1] + viewProjection[11] * p[2] + viewProjection[15];
  if (!(Math.abs(w) > 1e-9) || w < 0) return { x: 0, y: 0, depth: Infinity, visible: false };
  const x = (viewProjection[0] * p[0] + viewProjection[4] * p[1] + viewProjection[8] * p[2] + viewProjection[12]) / w;
  const y = (viewProjection[1] * p[0] + viewProjection[5] * p[1] + viewProjection[9] * p[2] + viewProjection[13]) / w;
  const z = (viewProjection[2] * p[0] + viewProjection[6] * p[1] + viewProjection[10] * p[2] + viewProjection[14]) / w;
  return {
    x: (x * 0.5 + 0.5) * width,
    y: (1 - (y * 0.5 + 0.5)) * height,
    depth: z,
    visible: x >= -1.05 && x <= 1.05 && y >= -1.05 && y <= 1.05 && z >= -1 && z <= 1,
  };
}

/* ------------------------------------------------------------------------- */
/* Focus and transition                                                       */
/* ------------------------------------------------------------------------- */

/**
 * Where the camera should stand to look at one node: at its position, close
 * enough that its own size reads, but never so close that neighbours vanish.
 */
export function focusGoal(node) {
  const size = vec3(node?.size, [12, 6, 4]);
  const radius = Math.max(size[0], size[1], size[2]) / 2;
  return {
    target: vec3(node?.position),
    distance: clamp(radius * 5, 22, 120),
  };
}

/** The ease of a camera move: slow at both ends, quick in the middle. */
export function easeInOutCubic(t) {
  const k = clamp(t, 0, 1);
  return k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
}

/**
 * A short camera move toward a goal. With ``reducedMotion`` the move is not
 * animated at all: the goal is applied in the same tick, so nothing on screen
 * travels. The transition only asks for frames while it is active.
 */
export function createCameraTransition({ duration = 340, reducedMotion = false } = {}) {
  const state = {
    active: false,
    from: null,
    to: null,
    startedAt: 0,
    duration: Math.max(0, finite(duration, 340)),
    reducedMotion: Boolean(reducedMotion),
  };

  const goalCamera = (camera, goal) => {
    const cam = clampCamera(camera);
    return clampCamera({
      ...cam,
      target: vec3(goal?.target, cam.target),
      distance: goal?.distance ?? cam.distance,
      yaw: goal?.yaw ?? cam.yaw,
      pitch: goal?.pitch ?? cam.pitch,
    });
  };

  return {
    get active() {
      return state.active;
    },
    get reducedMotion() {
      return state.reducedMotion;
    },
    setReducedMotion(value) {
      state.reducedMotion = Boolean(value);
    },
    /** Start a move from *camera* to *goal* at *now* (a monotonic millisecond clock). */
    start(camera, goal, now = 0) {
      const from = clampCamera(camera);
      const to = goalCamera(from, goal);
      if (state.reducedMotion || state.duration <= 0) {
        state.active = false;
        state.from = null;
        state.to = to;
        return { camera: to, done: true };
      }
      state.from = from;
      state.to = to;
      state.startedAt = finite(now, 0);
      state.active = true;
      return { camera: from, done: false };
    },
    /** Advance to *now*; the returned camera is the frame's camera. */
    step(now) {
      if (!state.active || !state.from || !state.to) return { camera: null, done: true, t: 1 };
      const elapsed = finite(now, 0) - state.startedAt;
      if (elapsed >= state.duration) {
        state.active = false;
        const camera = state.to;
        state.from = null;
        state.to = null;
        return { camera, done: true, t: 1 };
      }
      const raw = state.duration > 0 ? elapsed / state.duration : 1;
      const k = easeInOutCubic(raw);
      const camera = clampCamera({
        target: v3lerp(state.from.target, state.to.target, k),
        distance: state.from.distance + (state.to.distance - state.from.distance) * k,
        yaw: state.from.yaw + (state.to.yaw - state.from.yaw) * k,
        pitch: state.from.pitch + (state.to.pitch - state.from.pitch) * k,
        fov: state.from.fov + (state.to.fov - state.from.fov) * k,
      });
      return { camera, done: false, t: k };
    },
    cancel() {
      state.active = false;
      state.from = null;
      state.to = null;
    },
  };
}

/* ------------------------------------------------------------------------- */
/* Render-on-demand scheduling                                                */
/* ------------------------------------------------------------------------- */

/**
 * Coalesce every reason to draw into one frame. ``schedule()`` asks for a
 * frame only when none is pending, so a burst of pointer events still paints
 * once; when the frame runs and nothing marks the view dirty again, no next
 * frame is requested and the canvas rests. ``dispose()`` cancels a pending
 * frame for good.
 *
 * The frame source is injected (``requestFrame`` / ``cancelFrame``) so this
 * logic is testable with a fake clock and a fake requestAnimationFrame.
 */
export function createRenderScheduler({ requestFrame, cancelFrame, onFrame } = {}) {
  const request = typeof requestFrame === "function" ? requestFrame : (fn) => setTimeout(fn, 16);
  const cancel = typeof cancelFrame === "function" ? cancelFrame : (handle) => clearTimeout(handle);
  const frame = typeof onFrame === "function" ? onFrame : () => {};
  let handle = null;
  let disposed = false;
  let frames = 0;

  const run = () => {
    handle = null;
    if (disposed) return;
    frames += 1;
    frame();
  };

  return {
    /** Ask for a frame; returns true when this call actually queued one. */
    schedule() {
      if (disposed || handle !== null) return false;
      handle = request(run);
      return true;
    },
    /** Drop a pending frame without running it. */
    cancel() {
      if (handle === null) return;
      cancel(handle);
      handle = null;
    },
    /** Whether a frame is queued right now. */
    get pending() {
      return handle !== null;
    },
    /** How many frames actually ran through the scheduler. */
    get frames() {
      return frames;
    },
    get disposed() {
      return disposed;
    },
    dispose() {
      disposed = true;
      this.cancel();
    },
  };
}

/* ------------------------------------------------------------------------- */
/* Palette                                                                    */
/* ------------------------------------------------------------------------- */

/** The base colour of each node kind, as linear-ish RGB in [0, 1]. */
export const NODE_COLORS = Object.freeze({
  project: Object.freeze([0.82, 0.8, 0.9]),
  collection: Object.freeze([0.58, 0.62, 0.72]),
  task: Object.freeze([0.91, 0.64, 0.29]),
  chat: Object.freeze([0.34, 0.72, 0.87]),
  file: Object.freeze([0.63, 0.53, 0.91]),
  message: Object.freeze([0.47, 0.8, 0.6]),
  execution: Object.freeze([0.4, 0.82, 0.85]),
  operation: Object.freeze([0.72, 0.66, 0.52]),
});

/** The colour and alpha of each edge kind; unknown and filtered edges never draw. */
export const EDGE_COLORS = Object.freeze({
  contains: Object.freeze([0.55, 0.6, 0.68, 0.32]),
  depends: Object.freeze([0.94, 0.49, 0.35, 0.62]),
  turn: Object.freeze([0.36, 0.74, 0.9, 0.5]),
  plan: Object.freeze([0.72, 0.57, 0.93, 0.5]),
  "message-execution": Object.freeze([0.5, 0.85, 0.78, 0.46]),
  delegates: Object.freeze([0.4, 0.82, 0.85, 0.55]),
  operation: Object.freeze([0.62, 0.7, 0.56, 0.42]),
  "contains-operation": Object.freeze([0.66, 0.62, 0.5, 0.34]),
  reference: Object.freeze([0.45, 0.82, 0.62, 0.42]),
});

/** The colour of a node kind; a kind the palette does not know stays neutral. */
export function nodeColor(kind) {
  return NODE_COLORS[kind] ?? [0.72, 0.72, 0.72];
}

/** The colour of an edge kind; a kind the palette does not know stays neutral. */
export function edgeColor(kind) {
  return EDGE_COLORS[kind] ?? [0.6, 0.6, 0.6, 0.4];
}

/** Mix two RGB colours; used for the dimmed / highlighted states. */
export function mixColor(a, b, t) {
  const k = clamp(t, 0, 1);
  return [
    finite(a?.[0]) + (finite(b?.[0]) - finite(a?.[0])) * k,
    finite(a?.[1]) + (finite(b?.[1]) - finite(a?.[1])) * k,
    finite(a?.[2]) + (finite(b?.[2]) - finite(a?.[2])) * k,
  ];
}
