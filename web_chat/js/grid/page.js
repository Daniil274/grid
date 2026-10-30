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
import { fetchProjectMap, fetchProjects } from "./api.js";
import { REFRESH_MS, createPoller } from "./live.js";
import { renderBreadcrumb, renderMap, renderOverview, renderProjectPicker } from "./render.js";

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

/**
 * The page. `nodes` are the shell's elements (web_chat/grid.html); tests pass
 * their own, and a stubbed `loadMap`/`listProjects`.
 */
export class GridPage {
  /**
   * @param {object} nodes status, title, breadcrumb, map, overview, refresh
   * @param {object} [options] projectId, loadMap, listProjects, intervalMs, doc, timers
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
    this.error = "";
    this.staleAt = "";
    this.checkedAt = "";
    this.loading = false;
    this._abort = null;

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
    const snapshot = await this.load();
    if (this.projectId) this.poller.start();
    return snapshot;
  }

  stop() {
    this.poller.stop();
  }

  setStatus(text, { error = false } = {}) {
    const node = this.nodes.status;
    if (!node) return;
    node.textContent = text;
    if (node.dataset) node.dataset.tone = error ? "error" : "";
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
      this.map = answer?.map ?? answer ?? null;
      this.etag = answer?.etag ?? "";
      this.error = "";
      this.staleAt = "";
      this.render();
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
    if (!this.projectId) {
      if (map) map.hidden = true;
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
    if (map) renderMap(map, this.map, { projectId: this.projectId });
    if (overview) renderOverview(overview, this.map, { projectId: this.projectId });
    if (this.staleAt && overview) {
      overview.append(h("p.gridOverview__note.gridOverview__note--off", { text: `Stale data · last successful update ${this.staleAt}` }));
    }
  }
}
