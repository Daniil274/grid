/**
 * The page behind /map?project=<id>: one project on a map.
 *
 * Three things it does, and nothing else: it takes the snapshot the projects
 * API answers with (GET /api/projects/{id}/map), it draws it through
 * ./render.js, and it refreshes that snapshot every ~20 seconds while somebody
 * looks at the page (./live.js). A refresh carries the previous answer's ETag,
 * so an unchanged map costs a 304 and no redraw; a request in flight is
 * aborted when a newer one starts, and a hidden tab asks nothing at all.
 *
 * With no ?project the page is a picker - every project is a way in - because
 * a map of nothing would be a lie about the workspace.
 */

import { h, replace } from "../lib/dom.js";
import { fetchChatDetail, fetchProjectMap, fetchProjects } from "./api.js";
import { REFRESH_MS, createPoller } from "./live.js";
import { Inspector, applySelection, focusKeys, normalizeSelection } from "./inspector.js";
import { layoutMap } from "./layout.js";
import { createProjectionView } from "./layers.js";
import { buildLabel, renderBreadcrumb, renderMap, renderOverview, renderProjectPicker } from "./render.js";
import { createScene } from "./scene.js";
import {
  VIEWS,
  VIEW_LABELS,
  applyView,
  chatIdOfSelection,
  createViewSwitch,
  readViewPreference,
  resolveInitialView,
  sceneIdOfSelection,
  selectionOfSceneId,
  writeViewPreference,
} from "./view.js";

/** The ?project=<id> of a URL, or "" when the page is the picker. */
export function projectFromSearch(search) {
  try {
    return new URLSearchParams(search ?? "").get("project") ?? "";
  } catch {
    return "";
  }
}

const clock = (date = new Date()) =>
  [date.getHours(), date.getMinutes(), date.getSeconds()].map((part) => String(part).padStart(2, "0")).join(":");

/** The reader's own storage, or null when the browser refuses it. */
function readLocalStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/**
 * The page. `nodes` are the shell's elements (web_chat/grid.html); tests pass
 * their own, and a stubbed `loadMap`/`listProjects`.
 */
export class GridPage {
  /**
   * @param {object} nodes status, title, breadcrumb, map, overview, refresh
   * @param {object} [options] projectId, loadMap, listProjects, intervalMs, doc,
   *   timers, inspector, fetchJson
   */
  constructor(nodes = {}, options = {}) {
    this.nodes = nodes;
    this.projectId = options.projectId ?? "";
    this.loadMap = options.loadMap ?? fetchProjectMap;
    this.listProjects = options.listProjects ?? fetchProjects;
    this.intervalMs = options.intervalMs ?? REFRESH_MS;
    this.doc = options.doc ?? globalThis.document;

    this.map = null;
    this.projects = null;
    this.etag = "";
    // Chat detail is lazy and per current map revision: only a selected chat's
    // bounded detail is read, once, and a 304 never touches it.
    this.loadChatDetail = options.loadChatDetail ?? fetchChatDetail;
    this.chatDetails = new Map();
    this.chatDetailState = new Map();
    this._chatRequests = new Map();
    this._detailSeq = 0;
    this.error = "";
    this.staleAt = "";
    this.checkedAt = "";
    this.loading = false;
    this._abort = null;
    this.canvas = null;
    this.geometry = null;

    // -- the views -----------------------------------------------------------
    // One scene model per new map answer (a 304 rebuilds nothing); the renderers
    // are a view at a time, and the 3D one is imported only when it is asked for.
    this.win = options.win ?? globalThis.window ?? null;
    this.storage = options.storage ?? readLocalStorage();
    this.scene = null;
    this.viewScene = null;
    this.spaceView = null;
    this.officeView = null;
    this.projection = null;
    this._stopped = false;
    this._viewToken = 0;
    this.viewReady = Promise.resolve();
    this._pushedScene = null;
    this._pushedKey = "";
    this._spaceLoading = null;
    this.loadSpace = options.loadSpace ?? (() => import("./space.js"));
    this.loadOffice = options.loadOffice ?? (() => import("./office.js"));
    this.createProjection = options.createProjection ?? createProjectionView;
    this.sceneFor = options.sceneFor ?? createScene;
    this.reducedMotion = Boolean(
      options.reducedMotion ??
        this.win?.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ??
        false,
    );
    const width = Number(
      options.width ?? this.win?.innerWidth ?? 0,
    );
    const finePointer =
      options.finePointer ?? this.win?.matchMedia?.("(pointer: fine)")?.matches ?? null;
    const webgl = options.webgl ?? false;
    const resolved =
      options.view && VIEWS.includes(options.view)
        ? { view: options.view, source: "option", note: "" }
        : resolveInitialView({
            stored: options.storedView ?? readViewPreference(this.storage),
            width,
            reducedMotion: this.reducedMotion,
            finePointer,
            webgl,
          });
    this.view = resolved.view;
    this.viewSource = resolved;
    this.viewNote = resolved.note;
    this.viewSwitch = createViewSwitch({
      select: nodes.view ?? null,
      view: this.view,
      onChange: (next) => void this.chooseView(next),
    });
    applyView(this.doc, this.view);

    this._onResize = () => {
      this.spaceView?.resize?.();
      this.officeView?.resize?.();
    };
    this.win?.addEventListener?.("resize", this._onResize);

    // The inspector owns what is selected; the page owns what the map draws.
    this.inspector =
      options.inspector ??
      new Inspector({
        projectId: this.projectId,
        fetchJson: options.fetchJson ?? null,
        onUpdate: () => this.renderOverview(),
        onSelect: (selection) => void this.select(selection, { focus: true }),
        onClose: () => this.clearSelection(),
      });
    // Escape lets the selection go, from anywhere on the page.
    this._onKeydown = (event) => {
      if (event?.key === "Escape" && this.inspector.selection) this.clearSelection();
    };
    this.doc?.addEventListener?.("keydown", this._onKeydown);

    nodes.refresh?.addEventListener?.("click", () => void this.load({ force: true }));
    this.poller = createPoller({
      load: () => this.load(),
      intervalMs: this.intervalMs,
      doc: this.doc,
      timers: options.timers ?? globalThis,
    });
  }

  /** The first snapshot, then the rhythm that keeps it honest. A picker needs no rhythm. */
  async start() {
    this._stopped = false;
    this.doc?.removeEventListener?.("keydown", this._onKeydown);
    this.doc?.addEventListener?.("keydown", this._onKeydown);
    const snapshot = await this.load();
    if (this.projectId) this.poller.start();
    // Back from the back/forward cache a 304 answers at once and no render
    // happens: the renderer stopped by `stop()` is still owed a restart.
    if (this.map) void this.syncView();
    return snapshot;
  }

  stop() {
    this._stopped = true;
    this._viewToken += 1;
    this.poller.stop();
    this.doc?.removeEventListener?.("keydown", this._onKeydown);
    // A page going away holds no WebGL context, no observer and no frame.
    this.destroySpace();
    this.destroyOffice();
    this.destroyProjection();
    this._abort?.abort?.();
  }

  setStatus(text, { error = false } = {}) {
    const node = this.nodes.status;
    if (!node) return;
    node.textContent = text;
    if (node.dataset) node.dataset.tone = error ? "error" : "";
  }

  /**
   * The commit the server runs, in the header: an operator restarts the site
   * and needs to see that the running code is the code they deployed - or the
   * server's reason why it cannot say. Nothing else is shown, and no path.
   */
  renderBuild() {
    const node = this.nodes.build;
    if (!node) return;
    const server = this.map?.server;
    if (!server) {
      node.textContent = "";
      if ("title" in node) node.title = "";
      if (node.dataset) node.dataset.state = "";
      return;
    }
    const label = buildLabel(server);
    node.textContent = label.text;
    if ("title" in node) node.title = label.title;
    if (node.dataset) node.dataset.state = label.known ? "known" : "unknown";
  }

  async load({ force = false } = {}) {
    if (!this.projectId) return this.loadPicker();
    // A refresh that overtakes another: the older answer is no longer wanted.
    this._abort?.abort?.();
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    this._abort = controller;
    this.loading = true;
    this.nodes.map?.setAttribute?.("aria-busy", "true");
    this.setStatus(this.map ? "Refreshing…" : "Loading the map…");
    try {
      const answer = await this.loadMap(this.projectId, {
        etag: force ? "" : this.etag,
        signal: controller?.signal,
      });
      if (answer?.notModified) {
        this.error = "";
        this.checkedAt = clock();
        if (this.staleAt) {
          this.staleAt = "";
          this.render();
        }
        this.setStatus(`Unchanged · checked at ${this.checkedAt}`);
        return this.map;
      }
      this.checkedAt = clock();
      // A new map answer is a new revision: detail read for the previous one is
      // no longer trusted. The selected chat is read again, once, below.
      this.clearChatDetails();
      this.map = answer?.map ?? answer ?? null;
      this.etag = answer?.etag ?? "";
      this.error = "";
      this.staleAt = "";
      this.rebuildScene();
      this.render();
      void this.ensureSelectedDetail();
      const seconds = Math.round(this.intervalMs / 1000);
      this.setStatus(`Updated ${clock()} · refreshes every ${seconds} s while this page is open`);
      return this.map;
    } catch (error) {
      if (error?.name === "AbortError") return this.map;
      this.error = error?.message || "The map could not be loaded";
      if ([401, 403, 404].includes(error?.status)) {
        this.map = null;
        this.etag = "";
        this.staleAt = "";
        this.scene = null;
        this.viewScene = null;
        this.clearChatDetails();
      } else if (this.map) {
        this.staleAt = this.checkedAt;
      }
      this.render();
      this.setStatus(this.map ? `Offline · showing stale data from ${this.staleAt} · ${this.error}` : this.error, { error: true });
      return null;
    } finally {
      this.loading = false;
      this.nodes.map?.setAttribute?.("aria-busy", "false");
      if (this._abort === controller) this._abort = null;
    }
  }

  /** The picker: /map with no project named. */
  async loadPicker() {
    this.setStatus("Loading projects…");
    try {
      this.projects = await this.listProjects();
      this.error = "";
      this.render();
      this.setStatus(`${this.projects.length} ${this.projects.length === 1 ? "project" : "projects"} · choose one`);
    } catch (error) {
      this.projects = [];
      this.error = error?.message || "The projects could not be loaded";
      this.render();
      this.setStatus(this.error, { error: true });
    }
    return null;
  }

  render() {
    const { title, breadcrumb, map, overview } = this.nodes;
    this.renderBuild();
    if (!this.projectId) {
      if (map) map.hidden = true;
      this.showView(false, false, false);
      this.destroySpace();
      this.destroyOffice();
      this.destroyProjection();
      if (title) title.textContent = "Grid map";
      if (breadcrumb) replace(breadcrumb);
      if (overview) renderProjectPicker(overview, this.projects ?? []);
      return;
    }
    if (!this.map) {
      if (map) {
        map.hidden = true;
        replace(map);
      }
      this.showView(false, false, false);
      this.destroySpace();
      this.destroyOffice();
      this.destroyProjection();
      if (breadcrumb) replace(breadcrumb);
      if (title) title.textContent = "Project map";
      if (this.doc && "title" in this.doc) this.doc.title = "Project map · Grid";
      if (overview) {
        replace(
          overview,
          h("h2.gridOverview__title", { text: "Overview" }),
          h("p.gridOverview__note", { text: this.error ? `No data: ${this.error}` : "Loading the map…" }),
        );
      }
      return;
    }
    if (map) map.hidden = false;
    const name = String(this.map.project?.title ?? "") || String(this.projectId);
    if (title) title.textContent = name;
    if (this.doc && "title" in this.doc) this.doc.title = `${name} · Grid map`;
    if (breadcrumb) renderBreadcrumb(breadcrumb, this.map, { projectId: this.projectId });
    const geometry = layoutMap(this.map);
    this.geometry = geometry;
    this.inspector.setMap(this.map, geometry);
    if (this.view === "map" && map) this.renderMapNow();
    this.applyFocus();
    this.renderOverview();
    void this.syncView();
  }

  /** Draw the existing DOM/SVG map: the fallback view, and the desktop default before Space. */
  renderMapNow() {
    const map = this.nodes.map;
    if (!map || !this.map) return null;
    this.canvas = renderMap(map, this.map, {
      projectId: this.projectId,
      geometry: this.geometry ?? layoutMap(this.map),
      onSelect: (selection) => void this.select(selection),
    });
    return this.canvas;
  }

  /** The panel beside the map: the inspector (when something is selected), then the overview. */
  renderOverview() {
    const overview = this.nodes.overview;
    if (!overview || !this.map) return;
    renderOverview(overview, this.map, {
      projectId: this.projectId,
      inspector: this.inspector.element(),
    });
    if (this.staleAt) {
      overview.append(h("p.gridOverview__note.gridOverview__note--off", { text: `Stale data · last successful update ${this.staleAt}` }));
    }
  }

  /** Select a card: the map lights what belongs to it, the panel says what it is. */
  select(selection, { focus = false } = {}) {
    const pending = this.inspector.select(selection);
    // A chat, message, execution or operation needs that chat's bounded detail;
    // a task or a file does not, and never triggers a chat read.
    void this.ensureSelectedDetail();
    this.applyFocus();
    this.syncExpansion();
    this.updateViewSelection({ focus });
    this.renderOverview();
    return pending;
  }

  /** Let the selection go (Escape, the close button): every card is plain again. */
  clearSelection() {
    this.inspector.clear();
    this.applyFocus();
    this.syncExpansion();
    this.updateViewSelection({ focus: false });
  }

  /** Weigh the drawn map by the current selection, without moving anything. */
  applyFocus() {
    if (!this.canvas) return;
    const selection = this.inspector.selection;
    applySelection(this.canvas, selection ? focusKeys(selection, this.geometry, this.map) : null);
  }

  // -- the scene and the views ------------------------------------------------

  /** The scene id the current selection names, or "". */
  sceneId() {
    return sceneIdOfSelection(this.inspector.selection, { map: this.map });
  }

  /**
   * One scene model per new map answer. A 304 never reaches here: the previous
   * scene, the previous selection and the previous lazy reads all stay.
   */
  rebuildScene() {
    this.scene = this.map
      ? this.sceneFor(this.map, {
          projectId: this.projectId,
          chatDetails: Object.fromEntries(this.chatDetails),
        })
      : null;
    this.viewScene = this.scene;
    this.syncExpansion();
    return this.scene;
  }

  /**
   * The scene already carries exactly the detail that was read, so every view
   * sees the same nodes; nothing is expanded or guessed here.
   */
  syncExpansion() {
    this.viewScene = this.scene;
    return this.viewScene;
  }

  /** Forget every chat detail and stop a read that is still in flight. */
  clearChatDetails() {
    this._detailSeq += 1;
    for (const request of this._chatRequests.values()) request.controller?.abort?.();
    this._chatRequests.clear();
    this.chatDetails.clear();
    this.chatDetailState.clear();
    this.inspector?.setChatDetail?.("", null);
  }

  /**
   * Read the selected chat's detail once. A cached detail, a read already in
   * flight or a map revision that has moved on are all answered without a new
   * request; the exact selection is preserved across the rebuild.
   */
  async ensureSelectedDetail() {
    const selection = this.inspector.selection;
    const chatId = chatIdOfSelection(selection);
    if (!this.map || !chatId) return this.chatDetails.get(chatId) ?? null;
    if (this.chatDetails.has(chatId)) {
      this.inspector.setChatDetail(chatId, { status: "ready", detail: this.chatDetails.get(chatId) });
      return this.chatDetails.get(chatId);
    }
    if (this._chatRequests.has(chatId)) return this._chatRequests.get(chatId).promise;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const seq = this._detailSeq;
    const state = { status: "loading" };
    this.chatDetailState.set(chatId, state);
    this.inspector.setChatDetail(chatId, state);
    this.updateOfficeState();
    this.renderOverview();
    const promise = (async () => {
      try {
        const answer = await this.loadChatDetail(this.projectId, chatId, {
          signal: controller?.signal,
        });
        if (seq !== this._detailSeq) return null;
        if (answer?.notModified && this.chatDetails.has(chatId)) {
          this.chatDetailState.set(chatId, { status: "ready" });
          this.inspector.setChatDetail(chatId, { status: "ready", detail: this.chatDetails.get(chatId) });
          return this.chatDetails.get(chatId);
        }
        const detail = answer?.detail ?? null;
        if (!detail) {
          this.chatDetailState.set(chatId, { status: "error", error: "The chat detail was empty." });
          this.inspector.setChatDetail(chatId, this.chatDetailState.get(chatId));
          this.updateOfficeState();
          this.renderOverview();
          return null;
        }
        this.chatDetails.set(chatId, detail);
        this.chatDetailState.set(chatId, { status: "ready" });
        this.inspector.setChatDetail(chatId, { status: "ready", detail });
        this.refreshScene();
        return detail;
      } catch (error) {
        if (error?.name === "AbortError" || seq !== this._detailSeq) return null;
        const failure = { status: "error", error: error?.message || "The chat detail could not be loaded." };
        this.chatDetailState.set(chatId, failure);
        this.inspector.setChatDetail(chatId, failure);
        this.updateOfficeState();
        this.renderOverview();
        return null;
      } finally {
        const current = this._chatRequests.get(chatId);
        if (current?.promise === promise) this._chatRequests.delete(chatId);
      }
    })();
    this._chatRequests.set(chatId, { controller, promise });
    return promise;
  }

  /**
   * Rebuild the scene after a detail arrives, keeping the selected scene id and
   * pushing the new nodes to every live view without switching a tab.
   */
  refreshScene() {
    this.rebuildScene();
    this.applyFocus();
    this.updateViewSelection({ focus: false });
    this.renderOverview();
  }

  /** Show the one view that is active, and hide the two that are not. */
  showView(mapOn, spaceOn, projectionOn, officeOn = false) {
    const set = (node, visible) => {
      if (!node) return;
      node.hidden = !visible;
      node.setAttribute?.("aria-hidden", visible ? "false" : "true");
    };
    set(this.nodes.map, Boolean(mapOn));
    set(this.nodes.space, Boolean(spaceOn));
    set(this.nodes.projection, Boolean(projectionOn));
    set(this.nodes.office, Boolean(officeOn));
  }

  /** The header's control asked for a view: the page makes it real, or says why not. */
  async chooseView(next) {
    if (!VIEWS.includes(next)) return this.view;
    const view = await this.setView(next);
    // Only a view that really answered is worth remembering.
    if (view === next) writeViewPreference(this.storage, view);
    return view;
  }

  /**
   * Switch the view: the renderer that is no longer shown is destroyed, and the
   * one that is shown is created - with the 3D module imported at this moment,
   * never on page load.
   */
  async setView(next, { explicit = false } = {}) {
    const view = VIEWS.includes(next) ? next : "map";
    this.view = view;
    applyView(this.doc, view);
    this.viewSwitch?.set(view);
    if (explicit) writeViewPreference(this.storage, view);
    if (this.map) await this.syncView(++this._viewToken);
    return this.view;
  }

  /**
   * Bring the renderers in line with `this.view`. Safe to call repeatedly: an
   * inactive renderer is destroyed, an active one is created once and updated.
   */
  syncView(token = ++this._viewToken) {
    this.viewReady = this.runSyncView(token);
    return this.viewReady;
  }

  async runSyncView(token) {
    if (token !== this._viewToken || this._stopped) return;
    const active = Boolean(this.map);
    this.showView(
      active && this.view === "map",
      active && this.view === "space",
      active && (this.view === "layers" || this.view === "timeline"),
      active && this.view === "office",
    );
    if (this.view !== "space") this.destroySpace();
    if (this.view !== "office") this.destroyOffice();
    if (this.view !== "layers" && this.view !== "timeline") this.destroyProjection();
    if (!active) return;
    if (this.view === "map") {
      if (!this.canvas) this.renderMapNow();
      this.applyFocus();
      return;
    }
    if (this.view === "office") {
      await this.ensureOffice(token);
      return;
    }
    if (this.view === "space") {
      await this.ensureSpace(token);
      return;
    }
    if (!this.ensureProjection()) return;
    this.updateViewSelection({ focus: false });
  }

  /** The Layers/Timeline renderer, created on first use and reused afterwards. */
  ensureProjection() {
    if (this.projection) return this.projection;
    const container = this.nodes.projection;
    if (!container) {
      void this.fallback("The Layers view has no place on this page.");
      return null;
    }
    try {
      this.projection = this.createProjection(container, {
        scene: this.viewScene ?? this.scene,
        mode: this.view === "timeline" ? "timeline" : "layers",
        selectedId: this.sceneId() || null,
        onSelect: (id) => this.pickSceneId(id),
      });
    } catch (error) {
      this.projection = null;
      void this.fallback(error?.message || "The Layers view could not start.");
      return null;
    }
    return this.projection;
  }

  /**
   * The 3D view: native `space.js` is imported only when Space is really about
   * to be drawn. A browser without WebGL2 gets an honest line and Layers, not a
   * blank panel.
   */
  async ensureSpace(token) {
    if (this.spaceView) {
      this.updateViewSelection({ focus: false });
      return this.spaceView;
    }
    const container = this.nodes.space;
    if (!container) {
      await this.fallback("The Space view has no place on this page.");
      return null;
    }
    let module;
    try {
      module = await this.loadSpace();
    } catch (error) {
      if (token !== this._viewToken) return null;
      await this.fallback(error?.message || "The Space renderer could not be loaded.");
      return null;
    }
    if (token !== this._viewToken) return null;
    const create = module?.createSpaceView;
    if (typeof create !== "function") {
      await this.fallback("The Space renderer could not be loaded.");
      return null;
    }
    let view;
    try {
      view = create(container, {
        scene: this.viewScene ?? this.scene,
        selectedId: this.sceneId() || null,
        onSelect: (id) => this.pickSceneId(id),
        reducedMotion: this.reducedMotion,
      });
    } catch (error) {
      if (token !== this._viewToken) return null;
      await this.fallback(error?.message || "The Space view could not start.");
      return null;
    }
    if (token !== this._viewToken || this.view !== "space") {
      view?.destroy?.();
      return null;
    }
    this.spaceView = view;
    return view;
  }

  /** Only Office is downloaded, and only while its panel is actually active. */
  async ensureOffice(token) {
    if (this.officeView) {
      this.updateOfficeState();
      return this.officeView;
    }
    if (!this.nodes.office) {
      await this.fallback("The Office view has no place on this page.");
      return null;
    }
    try {
      const module = await this.loadOffice();
      if (token !== this._viewToken || this._stopped || this.view !== "office") return null;
      if (typeof module?.createOfficeView !== "function") throw new Error("The Office renderer could not be loaded.");
      const view = module.createOfficeView(this.nodes.office, {
        ...this.officeState(),
        reducedMotion: this.reducedMotion,
        onSelect: (id) => {
          // Selecting opens the inspector, not an unsolicited camera flight.
          if (!id) return this.clearSelection();
          const selection = selectionOfSceneId(id, { scene: this.scene, map: this.map });
          if (selection) void this.select(selection, { focus: false });
        },
        onUnavailable: (reason) => {
          if (this.view === "office" && !this._stopped) void this.fallback(reason);
        },
      });
      if (token !== this._viewToken || this._stopped || this.view !== "office") {
        view?.destroy?.();
        return null;
      }
      this.officeView = view;
      return view;
    } catch (error) {
      if (token === this._viewToken && !this._stopped) await this.fallback(error?.message || "The Office could not start.");
      return null;
    }
  }

  officeState() {
    return {
      scene: this.viewScene ?? this.scene,
      map: this.map,
      selectedId: this.sceneId() || null,
      detailChatIds: [...this.chatDetails.keys()],
      detailStates: Object.fromEntries(this.chatDetailState),
    };
  }

  updateOfficeState() {
    this.officeView?.update?.(this.officeState());
  }

  destroyOffice() {
    const view = this.officeView;
    this.officeView = null;
    try { view?.destroy?.(); } catch { /* teardown cannot break the page */ }
  }

  /** Push the scene and the selection into whichever renderer is alive. */
  updateViewSelection({ focus = false } = {}) {
    const scene = this.viewScene ?? this.scene;
    const selectedId = this.sceneId() || null;
    const key = `${this.view}|${selectedId ?? ""}`;
    // The same scene and the same selection (a 304, a re-render of the panel)
    // are not pushed again: the renderer keeps the DOM it already owns.
    if (!focus && this._pushedScene === scene && this._pushedKey === key) return scene;
    this._pushedScene = scene;
    this._pushedKey = key;
    if (this.projection) {
      this.projection.update({
        scene,
        mode: this.view === "timeline" ? "timeline" : "layers",
        selectedId,
      });
    }
    if (this.spaceView) {
      this.spaceView.update({ scene, selectedId });
      if (focus && selectedId) this.spaceView.focus(selectedId);
    }
    this.updateOfficeState();
    if (focus && selectedId) this.officeView?.focus?.(selectedId);
    return scene;
  }

  /** A Space/Layers renderer selected a scene node: the inspector shows it. */
  pickSceneId(id) {
    if (id == null || id === "") {
      this.clearSelection();
      return;
    }
    const selection = selectionOfSceneId(id, { scene: this.viewScene ?? this.scene, map: this.map });
    if (!selection) return;
    void this.select(selection, { focus: true });
  }

  /**
   * A view that cannot be drawn steps aside for one that can: Layers where it
   * has a place, the Map otherwise - with a line saying what happened.
   */
  async fallback(message) {
    this.destroySpace();
    this.destroyOffice();
    const to = this.nodes.projection ? "layers" : "map";
    this.view = to;
    applyView(this.doc, to);
    this.viewSwitch?.set(to);
    this.setStatus(`${message} Showing ${VIEW_LABELS[to]} instead.`, { error: true });
    await this.syncView(++this._viewToken);
  }

  destroySpace() {
    const view = this.spaceView;
    this.spaceView = null;
    if (!view) return;
    try {
      view.destroy?.();
    } catch {
      /* a renderer that fails to release is still gone from the page */
    }
  }

  destroyProjection() {
    const view = this.projection;
    this.projection = null;
    if (!view) return;
    try {
      view.destroy?.();
    } catch {
      /* the same: a broken teardown must not break the page */
    }
  }
}
