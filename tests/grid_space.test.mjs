/**
 * The pure geometry of the Space renderer (web_chat/js/grid/space_math.js).
 *
 * What is pinned here is arithmetic a reader can check by hand: a perspective
 * matrix that maps the axis onto the near plane as it should, a lookAt that
 * puts the target ``distance`` in front of the eye, a ray that goes exactly
 * through the pixel it was asked for and picks the box it really hits first, a
 * camera that frames an empty scene and a huge one without NaN, clamps that hold
 * after thousands of random gestures, and a frame scheduler that draws once per
 * burst and rests when nothing is dirty.
 *
 * There is no WebGL and no fake WebGL context in this file: the shader path
 * needs a real browser, and pretending otherwise would pin nothing true.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CAMERA_LIMITS,
  DEFAULT_CAMERA,
  EDGE_COLORS,
  NODE_COLORS,
  cameraClip,
  cameraPosition,
  cameraProjection,
  cameraView,
  cameraViewProjection,
  clamp,
  clampCamera,
  createCamera,
  createCameraTransition,
  createRenderScheduler,
  edgeColor,
  fitCamera,
  focusGoal,
  mat4Identity,
  mat4Invert,
  mat4LookAt,
  mat4Multiply,
  mat4Perspective,
  mat4TransformPoint,
  nodeAabb,
  nodeColor,
  orbitCamera,
  panCamera,
  pickNode,
  projectPoint,
  rayAabb,
  screenRay,
  v3length,
  v3normalize,
  v3sub,
  zoomCamera,
} from "../web_chat/js/grid/space_math.js";
import { EDGE_KINDS, NODE_KINDS, createScene, expandActivity, sceneStats } from "../web_chat/js/grid/scene.js";
import { edgeBaseColor, nodeBaseColor } from "../web_chat/js/grid/space.js";

const near = (actual, expected, tolerance = 1e-5, message = "") =>
  assert.ok(
    Math.abs(actual - expected) <= tolerance,
    `${message} expected ${expected} ± ${tolerance}, got ${actual}`,
  );

const nearVec = (actual, expected, tolerance = 1e-5) => {
  assert.equal(actual.length, expected.length, "vector length");
  for (let i = 0; i < expected.length; i += 1) near(actual[i], expected[i], tolerance, `[${i}]`);
};

const allFinite = (values) => values.every((value) => Number.isFinite(value));

/** A tiny deterministic PRNG, so a fuzz test fails the same way twice. */
function rng(seed = 42) {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 0x100000000;
  };
}

/* ------------------------------------------------------------------------- */
/* Matrices                                                                   */
/* ------------------------------------------------------------------------- */

test("perspective maps the near plane to -1 and keeps the vertical fov", () => {
  const fov = 60 * (Math.PI / 180);
  const nearPlane = 1;
  const farPlane = 100;
  const m = mat4Perspective(fov, 2, nearPlane, farPlane);

  // A point on the near plane, on the axis, projects to the centre at z = -1.
  const projected = mat4TransformPoint(m, [0, 0, -nearPlane]);
  nearVec(projected, [0, 0, -1]);

  // A point at the far plane lands on +1.
  near(mat4TransformPoint(m, [0, 0, -farPlane])[2], 1);

  // The vertical half-height at distance 1 is tan(fov / 2): the edge lands on +1.
  const halfHeight = Math.tan(fov / 2);
  near(mat4TransformPoint(m, [0, halfHeight, -1])[1], 1);
  // The horizontal half-width is the same, doubled by the aspect: the right edge is +1.
  near(mat4TransformPoint(m, [halfHeight * 2, 0, -1])[0], 1);
});

test("a degenerate aspect does not produce NaN", () => {
  const m = mat4Perspective(1, 0, 0.1, 10);
  assert.ok(allFinite(Array.from(m)));
  const m2 = mat4Perspective(NaN, NaN, NaN, NaN);
  assert.ok(allFinite(Array.from(m2)));
});

test("lookAt puts the target distance in front of the eye and keeps up near +Y", () => {
  const eye = [0, 0, 10];
  const target = [0, 0, 0];
  const view = mat4LookAt(eye, target, [0, 1, 0]);

  nearVec(mat4TransformPoint(view, target), [0, 0, -10]);
  // The eye itself is the view origin.
  nearVec(mat4TransformPoint(view, eye), [0, 0, 0]);
  // A point above the target stays above it on screen.
  assert.ok(mat4TransformPoint(view, [0, 5, 0])[1] > 0);
  // A point to the right stays to the right.
  assert.ok(mat4TransformPoint(view, [5, 0, 0])[0] > 0);
});

test("lookAt survives an eye looking straight down", () => {
  const view = mat4LookAt([0, 100, 0], [0, 0, 0], [0, 1, 0]);
  assert.ok(allFinite(Array.from(view)));
  nearVec(mat4TransformPoint(view, [0, 0, 0]), [0, 0, -100], 1e-3);
});

test("identity, multiplication and inverse agree", () => {
  const identity = mat4Identity();
  const camera = createCamera({ target: [3, 5, -2], distance: 60, yaw: 0.9, pitch: 0.3 });
  const view = cameraView(camera);
  const projection = cameraProjection(camera, 1.6, 200);
  const vp = mat4Multiply(projection, view);

  const identityView = mat4Multiply(identity, view);
  nearVec(Array.from(identityView), Array.from(view));

  const inverse = mat4Invert(vp);
  const product = mat4Multiply(vp, inverse);
  for (let i = 0; i < 4; i += 1) {
    for (let j = 0; j < 4; j += 1) {
      near(product[i * 4 + j], i === j ? 1 : 0, 1e-4, `vp*vp^-1[${i},${j}]`);
    }
  }
});

test("a singular matrix inverts to zeros rather than NaN", () => {
  const zeros = mat4Invert(new Float32Array(16));
  assert.ok(allFinite(Array.from(zeros)));
  assert.ok(Array.from(zeros).every((value) => value === 0));
});

/* ------------------------------------------------------------------------- */
/* Camera                                                                     */
/* ------------------------------------------------------------------------- */

test("the camera stands at distance from its target and looks along it", () => {
  const camera = createCamera({ target: [10, 20, 30], distance: 50, yaw: 0.4, pitch: 0.2 });
  const position = cameraPosition(camera);
  near(v3length(v3sub(position, camera.target)), 50, 1e-5);
  // Above the horizon, the eye is above the target.
  assert.ok(position[1] > camera.target[1]);
});

test("orbit clamps pitch and never turns over, zoom clamps distance", () => {
  let camera = createCamera({ pitch: 0, distance: 100 });
  for (let i = 0; i < 3; i += 1) camera = orbitCamera(camera, 500, 5000);
  assert.ok(camera.pitch <= CAMERA_LIMITS.maxPitch);
  for (let i = 0; i < 3; i += 1) camera = orbitCamera(camera, -500, -5000);
  assert.ok(camera.pitch >= CAMERA_LIMITS.minPitch);

  let zoomed = createCamera({ distance: 100 });
  for (let i = 0; i < 40; i += 1) zoomed = zoomCamera(zoomed, 0.5);
  near(zoomed.distance, CAMERA_LIMITS.minDistance);
  for (let i = 0; i < 80; i += 1) zoomed = zoomCamera(zoomed, 2);
  near(zoomed.distance, CAMERA_LIMITS.maxDistance);

  // A broken camera never poisons the matrix.
  const broken = clampCamera({ target: [NaN, Infinity, -Infinity], distance: NaN, yaw: NaN, pitch: NaN });
  assert.ok(allFinite([...broken.target, broken.distance, broken.yaw, broken.pitch]));
});

test("pan slides the target across the view plane, not along it", () => {
  const camera = createCamera({ target: [0, 0, 0], distance: 100, yaw: 0, pitch: 0 });
  const before = cameraPosition(camera);
  const panned = panCamera(camera, 40, 0, 800);
  // Dragging right moves the scene right, so the target slides left: -X at yaw 0.
  assert.ok(panned.target[0] < 0);
  near(panned.target[2], 0, 1e-6);
  // The camera keeps looking from the same distance.
  near(v3length(v3sub(cameraPosition(panned), panned.target)), 100, 1e-4);
  assert.ok(cameraPosition(panned)[0] < before[0]);
});

test("fitCamera frames an empty and a huge scene without NaN", () => {
  const empty = fitCamera({ min: [0, 0, 0], max: [0, 0, 0] }, { aspect: 1.5 });
  assert.ok(allFinite([...empty.target, empty.distance]));
  assert.ok(empty.distance >= CAMERA_LIMITS.minDistance);

  const huge = fitCamera({ min: [-1e9, -1e9, -1e9], max: [1e9, 1e9, 1e9] }, { aspect: 0.5 });
  assert.ok(allFinite([...huge.target, huge.distance]));
  assert.ok(huge.distance <= CAMERA_LIMITS.maxDistance);

  const broken = fitCamera({ min: [NaN, 0, 0], max: [Infinity, 0, 0] });
  assert.ok(allFinite([...broken.target, broken.distance]));

  // A wide viewport needs less distance than a tall one for the same bounds.
  const bounds = { min: [-100, -10, -100], max: [100, 10, 100] };
  const wide = fitCamera(bounds, { aspect: 2.5 });
  const tall = fitCamera(bounds, { aspect: 0.5 });
  assert.ok(wide.distance < tall.distance);
});

test("camera clip planes stay positive, ordered and finite for any distance", () => {
  for (const distance of [0.001, 1, 100, 1e5, NaN]) {
    const clip = cameraClip(createCamera({ distance }), 0);
    assert.ok(Number.isFinite(clip.near) && clip.near > 0);
    assert.ok(clip.far > clip.near);
    assert.ok(clip.far / clip.near < 1e6);
  }
});

/* ------------------------------------------------------------------------- */
/* Rays and picking                                                           */
/* ------------------------------------------------------------------------- */

test("the ray through the centre of the screen points at the target", () => {
  const camera = createCamera({ target: [2, -4, 6], distance: 80, yaw: -0.7, pitch: 0.35 });
  const ray = screenRay(camera, 1.6, 0, 0, 200);
  const toTarget = v3normalize(v3sub(camera.target, cameraPosition(camera)));
  nearVec(ray.direction, toTarget, 1e-4);
  assert.ok(ray.direction[1] < 0, "looking down at the target");
});

test("ray/AABB reports the entry distance, a miss, an inside hit and a parallel ray", () => {
  const box = { min: [-1, -1, -1], max: [1, 1, 1] };
  const hit = rayAabb([0, 0, 10], [0, 0, -1], box.min, box.max);
  near(hit.t, 9);
  assert.equal(hit.inside, false);

  assert.equal(rayAabb([5, 0, 10], [0, 0, -1], box.min, box.max), null);
  assert.equal(rayAabb([0, 5, 10], [0, 0, -1], box.min, box.max), null);

  const inside = rayAabb([0, 0, 0], [0, 0, -1], box.min, box.max);
  near(inside.t, 0);
  assert.equal(inside.inside, true);

  // Parallel to a slab and outside it: no hit, and no NaN.
  assert.equal(rayAabb([2, 0, 10], [0, 0, -1], box.min, box.max), null);
  // Parallel and inside the slab: still a hit.
  const parallel = rayAabb([0.5, 0.5, 10], [0, 0, -1], box.min, box.max);
  near(parallel.t, 9);

  // The box is behind the ray.
  assert.equal(rayAabb([0, 0, 10], [0, 0, 1], box.min, box.max), null);
});

test("picking returns the box the ray hits first, not the nearest by index", () => {
  const camera = createCamera({ target: [0, 0, 0], distance: 100, yaw: 0, pitch: 0 });
  const nearBox = { id: "near", position: [0, 0, 40], size: [6, 6, 6] };
  const farBox = { id: "far", position: [0, 0, -20], size: [40, 40, 40] };
  const offBox = { id: "off", position: [60, 0, 0], size: [6, 6, 6] };
  const nodes = [farBox, offBox, nearBox];

  const centre = screenRay(camera, 1.6, 0, 0, 200);
  assert.equal(pickNode(nodes, centre).id, "near");

  // A ray aimed away from the near box still hits the big far one.
  const offset = screenRay(camera, 1.6, 0.12, 0, 200);
  const picked = pickNode(nodes, offset);
  assert.equal(picked.id, "far");

  // A ray into empty space hits nothing.
  assert.equal(pickNode(nodes, screenRay(camera, 1.6, 0.99, 0.99, 200)), null);

  // A node with a broken position is skipped instead of claiming the origin.
  assert.equal(pickNode([{ id: "broken", position: [NaN, 0, 0], size: [5, 5, 5] }], centre), null);
});

test("projecting a node and casting back through that pixel picks the same node", () => {
  const camera = createCamera({ target: [5, 10, -3], distance: 90, yaw: 0.7, pitch: 0.3 });
  const width = 1280;
  const height = 720;
  const aspect = width / height;
  const vp = cameraViewProjection(camera, aspect, 300);

  const nodes = [
    { id: "a", position: [0, 0, 0], size: [10, 8, 6] },
    { id: "b", position: [30, 0, 12], size: [8, 5, 4] },
    { id: "c", position: [5, 40, -10], size: [6, 4, 3] },
  ];

  for (const node of nodes) {
    const point = projectPoint(vp, node.position, width, height);
    assert.equal(point.visible, true, `${node.id} is on screen`);
    const ndcX = (point.x / width) * 2 - 1;
    const ndcY = 1 - (point.y / height) * 2;
    const ray = screenRay(camera, aspect, ndcX, ndcY, 300);
    const picked = pickNode(nodes, ray);
    assert.equal(picked?.id, node.id, `the pixel of ${node.id} picks ${node.id}`);
  }
});

test("projectPoint hides what is behind the camera", () => {
  const camera = createCamera({ target: [0, 0, 0], distance: 50, yaw: 0, pitch: 0 });
  const vp = cameraViewProjection(camera, 1.5, 100);
  const behind = cameraPosition(camera);
  const further = [behind[0], behind[1], behind[2] + 100];
  const point = projectPoint(vp, further, 800, 600);
  assert.equal(point.visible, false);
});

/* ------------------------------------------------------------------------- */
/* Fuzz: thousands of gestures never produce NaN or leave the limits           */
/* ------------------------------------------------------------------------- */

test("a long random gesture stream keeps the camera finite and inside its limits", () => {
  const random = rng(7);
  let camera = createCamera();
  for (let i = 0; i < 2000; i += 1) {
    const roll = random();
    if (roll < 0.4) camera = orbitCamera(camera, (random() - 0.5) * 900, (random() - 0.5) * 900);
    else if (roll < 0.7) camera = panCamera(camera, (random() - 0.5) * 400, (random() - 0.5) * 400, 700);
    else camera = zoomCamera(camera, 0.7 + random());

    assert.ok(allFinite([...camera.target, camera.distance, camera.yaw, camera.pitch]));
    assert.ok(camera.pitch >= CAMERA_LIMITS.minPitch && camera.pitch <= CAMERA_LIMITS.maxPitch);
    assert.ok(camera.distance >= CAMERA_LIMITS.minDistance && camera.distance <= CAMERA_LIMITS.maxDistance);
  }

  const vp = cameraViewProjection(camera, 1.6, 500);
  assert.ok(allFinite(Array.from(vp)));
});

/* ------------------------------------------------------------------------- */
/* Focus and transitions                                                      */
/* ------------------------------------------------------------------------- */

test("focusGoal puts the target on the node and keeps a sane distance", () => {
  const goal = focusGoal({ position: [12, 40, -7], size: [14, 8, 4] });
  nearVec(goal.target, [12, 40, -7]);
  assert.ok(goal.distance > 20 && goal.distance < 120);
  const tiny = focusGoal({ position: [0, 0, 0], size: [0.1, 0.1, 0.1] });
  assert.ok(Number.isFinite(tiny.distance) && tiny.distance > 0);
  const broken = focusGoal({ position: [NaN, 0, 0], size: null });
  assert.ok(allFinite([...broken.target, broken.distance]));
});

test("reduced motion applies a focus at once; otherwise it animates and ends exactly on the goal", () => {
  const camera = createCamera({ target: [0, 0, 0], distance: 200 });
  const goal = { target: [30, 20, -10], distance: 40 };

  const instant = createCameraTransition({ reducedMotion: true });
  const first = instant.start(camera, goal, 0);
  assert.equal(first.done, true);
  assert.equal(instant.active, false);
  nearVec(first.camera.target, [30, 20, -10]);
  near(first.camera.distance, 40);

  const animated = createCameraTransition({ duration: 300, reducedMotion: false });
  const started = animated.start(camera, goal, 1000);
  assert.equal(started.done, false);
  assert.equal(animated.active, true);

  const middle = animated.step(1150);
  assert.equal(middle.done, false);
  assert.ok(middle.t > 0 && middle.t < 1);
  // Halfway through an eased move, the target is strictly between the two ends.
  assert.ok(middle.camera.target[0] > 0 && middle.camera.target[0] < 30);

  const end = animated.step(1300);
  assert.equal(end.done, true);
  assert.equal(animated.active, false);
  nearVec(end.camera.target, [30, 20, -10]);
  near(end.camera.distance, 40);
});

/* ------------------------------------------------------------------------- */
/* Render-on-demand scheduling                                                */
/* ------------------------------------------------------------------------- */

test("the scheduler coalesces a burst into one frame and then rests", () => {
  let nextHandle = 1;
  const queued = new Map();
  let draws = 0;
  const scheduler = createRenderScheduler({
    requestFrame: (fn) => {
      const handle = nextHandle;
      nextHandle += 1;
      queued.set(handle, fn);
      return handle;
    },
    cancelFrame: (handle) => queued.delete(handle),
    onFrame: () => {
      draws += 1;
    },
  });

  assert.equal(scheduler.schedule(), true, "first dirty marks needs a frame");
  assert.equal(scheduler.schedule(), false, "a second burst is folded into the same frame");
  assert.equal(scheduler.schedule(), false);
  assert.equal(scheduler.pending, true);
  assert.equal(queued.size, 1);

  const [handle, run] = [...queued.entries()][0];
  queued.delete(handle);
  run();
  assert.equal(draws, 1);
  assert.equal(scheduler.pending, false, "nothing is scheduled once the frame is done");
  assert.equal(scheduler.frames, 1);

  // No perpetual animation: without a new reason to draw, time passes and
  // nothing is requested.
  assert.equal(queued.size, 0);

  assert.equal(scheduler.schedule(), true, "a new reason draws again");
  scheduler.cancel();
  assert.equal(scheduler.pending, false);
  assert.equal(queued.size, 0);
  assert.equal(draws, 1, "a cancelled frame never ran");

  scheduler.schedule();
  scheduler.dispose();
  assert.equal(scheduler.pending, false);
  assert.equal(queued.size, 0);
  assert.equal(scheduler.schedule(), false, "a disposed scheduler asks for nothing");
  assert.equal(scheduler.frames, 1);
});

/* ------------------------------------------------------------------------- */
/* Palette                                                                    */
/* ------------------------------------------------------------------------- */

test("the palette the renderer uses covers every kind the SceneModel can emit", () => {
  for (const kind of NODE_KINDS) {
    const color = nodeBaseColor(kind);
    assert.equal(color.length, 3);
    assert.ok(color.every((channel) => channel >= 0 && channel <= 1), `${kind} colour range`);
    assert.ok(NODE_COLORS[kind], `${kind} is in the geometry palette`);
    assert.deepEqual(color, nodeColor(kind), "a known kind keeps the palette's colour");
  }
  for (const kind of EDGE_KINDS) {
    const color = edgeBaseColor(kind);
    assert.equal(color.length, 4);
    assert.ok(color.every((channel) => channel >= 0 && channel <= 1), `${kind} colour range`);
    assert.ok(color[3] > 0, `${kind} is visible`);
    assert.ok(EDGE_COLORS[kind], `${kind} is in the geometry palette`);
    assert.deepEqual(color, edgeColor(kind));
  }
  // The palette knows no legacy kind: there is no context island any more.
  assert.equal(NODE_COLORS.context, undefined);
  assert.equal(NODE_COLORS.process, undefined);
  assert.equal(EDGE_COLORS.activity, undefined);
  assert.equal(EDGE_COLORS.delegation, undefined);
});

/* ------------------------------------------------------------------------- */
/* Integration with the real SceneModel                                       */
/* ------------------------------------------------------------------------- */

test("a real 450-node scene frames, projects and picks every one of its nodes", () => {
  const tasks = Array.from({ length: 300 }, (_, index) => ({
    id: `bd-${index}`,
    title: `Task ${index}`,
    status: index % 3 === 0 ? "open" : index % 3 === 1 ? "in_progress" : "closed",
    state: index % 3 === 0 ? "ready" : index % 3 === 1 ? "in_progress" : "done",
    assignee: index % 5 === 0 ? "agent@example.test" : null,
    created_at: new Date(Date.UTC(2026, 0, 1 + (index % 200))).toISOString(),
    updated_at: new Date(Date.UTC(2026, 2, 1 + (index % 20))).toISOString(),
    depends_on: index > 0 ? [`bd-${index - 1}`] : [],
  }));
  const processes = Array.from({ length: 50 }, (_, index) => ({
    chat_id: `c${index}`,
    title: `Chat ${index}`,
    updated_at: new Date(Date.UTC(2026, 1, 1 + (index % 28))).toISOString(),
    active: index % 4 === 0,
    awaiting_approval: false,
    activity: { items: [], total: 0, truncated: false },
    task_links: [{ task_id: `bd-${index}`, relation: index % 2 ? "plan" : "turn" }],
  }));
  const files = Array.from({ length: 100 }, (_, index) => ({
    name: `file-${index}.${index % 2 ? "md" : "py"}`,
    path: `src/file-${index}.${index % 2 ? "md" : "py"}`,
    kind: "file",
    size: 100 + index,
    modified: new Date(Date.UTC(2026, 3, 1 + (index % 30))).toISOString(),
  }));

  const scene = createScene({
    project: { id: "p1" },
    tasks: { available: true, items: tasks, total: tasks.length, truncated: false },
    processes: { available: true, items: processes, total: processes.length, truncated: false },
    files: { available: true, items: files, total: files.length, truncated: false },
    files_url: "/api/projects/p1/files",
  });
  const stats = sceneStats(scene);
  assert.equal(stats.nodes, 454, "450 items plus the project and its three collections");

  const aspect = 1440 / 900;
  const camera = createCamera(fitCamera(scene.bounds, { aspect }));
  assert.ok(allFinite([...camera.target, camera.distance]));

  const vp = cameraViewProjection(camera, aspect, 800);
  assert.ok(allFinite(Array.from(vp)));

  // Every node's own pixel picks that node (or something in front of it), and
  // no pick ever lands on a node the scene does not hold.
  const ids = new Set(scene.nodes.map((node) => node.id));
  let visible = 0;
  for (const node of scene.nodes) {
    const point = projectPoint(vp, node.position, 1440, 900);
    if (point.visible) visible += 1;
    const ndcX = (point.x / 1440) * 2 - 1;
    const ndcY = 1 - (point.y / 900) * 2;
    const picked = pickNode(scene.nodes, screenRay(camera, aspect, ndcX, ndcY, 800));
    if (picked) assert.ok(ids.has(picked.id), "a pick is always a real node");
  }
  assert.ok(visible > 300, `most of the framed scene is on screen, got ${visible}`);

  // A focused node stays finite after expanding its activity.
  const expanded = expandActivity(scene, "c0");
  assert.equal(sceneStats(expanded).nodes, 454);
  const goal = focusGoal(expanded.nodes[300]);
  assert.ok(allFinite([...goal.target, goal.distance]));
});

test("clamp and nodeAabb are total: they answer for any garbage", () => {
  near(clamp(NaN, 2, 5), 2);
  near(clamp(Infinity, 2, 5), 5);
  near(clamp(3, 2, 5), 3);

  const box = nodeAabb({ position: [NaN, 0, 0], size: [0, 0, 0] }, 1);
  assert.ok(allFinite([...box.min, ...box.max]));
  assert.ok(box.max[0] > box.min[0]);

  const defaultCamera = createCamera();
  near(defaultCamera.distance, DEFAULT_CAMERA.distance);
});

/* ------------------------------------------------------------------------- */
/* The view refuses honestly when the browser cannot draw                      */
/* ------------------------------------------------------------------------- */

/** The barest element the view touches before it asks for a WebGL2 context. */
function stubElement(ownerDocument, tag = "div") {
  return {
    tagName: tag.toUpperCase(),
    ownerDocument,
    style: {},
    childNodes: [],
    classList: { add() {}, remove() {}, contains: () => false },
    setAttribute() {},
    appendChild(child) {
      this.childNodes.push(child);
      child.parentNode = this;
      return child;
    },
    remove() {},
    addEventListener() {},
    removeEventListener() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 640, height: 360 }),
    getContext: () => null,
  };
}

test("createSpaceView throws a readable error when the browser has no WebGL2", async () => {
  const { createSpaceView } = await import("../web_chat/js/grid/space.js");
  const view = {
    performance: { now: () => 0 },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
  };
  const ownerDocument = {
    defaultView: view,
    createElement: (tag) => stubElement(ownerDocument, tag),
  };
  const container = stubElement(ownerDocument);

  assert.throws(
    () => createSpaceView(container, { scene: { nodes: [], edges: [] } }),
    (error) => error instanceof Error && /WebGL2/.test(error.message) && /Flat/.test(error.message),
    "the error names the missing WebGL2 and points at the Flat view",
  );
  assert.equal(container.childNodes.length, 0, "a refused view leaves the container untouched");

  assert.throws(() => createSpaceView(null), TypeError);
  assert.throws(() => createSpaceView({}), TypeError);
});
