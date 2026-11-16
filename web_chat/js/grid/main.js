/**
 * The shell of the map page (web_chat/grid.html): find its nodes, hand them to
 * the page and let it show the project named by ?project=<id>.
 *
 * The view switch (Space / Layers / Timeline / Map) lives in the header. The
 * page decides which one can be drawn here; this file only tells it what this
 * browser offers - its width, its motion and pointer preferences, and whether
 * WebGL2 exists at all. The WebGL2 probe is a throwaway canvas: the Space
 * renderer itself (`js/grid/space.js`) is imported by the page only when Space
 * is really chosen.
 */

import { GridPage, projectFromSearch } from "./page.js";
import { probeWebGL } from "./view.js";

// The build badge lives in the header, beside Refresh: the page sets its words
// once it has the map's `server` object (a short commit, or a reason).
const actions = document.querySelector(".gridActions");
const build = document.createElement("span");
build.id = "grid-build";
build.className = "gridBrand gridBuild";
build.setAttribute("role", "status");
build.setAttribute("aria-live", "polite");
actions?.prepend(build);

const storage = (() => {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null; // a browser that refuses storage still draws the map
  }
})();

const page = new GridPage(
  {
    status: document.getElementById("grid-status"),
    title: document.getElementById("grid-title"),
    breadcrumb: document.getElementById("grid-breadcrumb"),
    map: document.getElementById("grid-map"),
    space: document.getElementById("grid-space"),
    office: document.getElementById("grid-office"),
    projection: document.getElementById("grid-projection"),
    view: document.getElementById("grid-view"),
    overview: document.getElementById("grid-overview"),
    refresh: document.getElementById("grid-refresh"),
    build,
  },
  {
    projectId: projectFromSearch(location.search),
    win: globalThis,
    doc: document,
    storage,
    webgl: probeWebGL(document),
  },
);

// A page leaving - or going into the back/forward cache - holds no timer and no
// WebGL context; coming back starts again on the scene that is already known.
addEventListener("pagehide", () => page.stop());
addEventListener("pageshow", (event) => {
  // Back from the back/forward cache: the snapshot is stale, the timer gone.
  if (event.persisted) void page.start();
});

void page.start();
