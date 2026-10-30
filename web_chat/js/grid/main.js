/**
 * The shell of the map page (web_chat/grid.html): find its nodes, hand them to
 * the page and let it show the project named by ?project=<id>.
 */

import { GridPage, projectFromSearch } from "./page.js";

// The build badge lives in the header, beside Refresh: the page sets its words
// once it has the map's `server` object (a short commit, or a reason).
const actions = document.querySelector(".gridActions");
const build = document.createElement("span");
build.id = "grid-build";
build.className = "gridBrand gridBuild";
build.setAttribute("role", "status");
build.setAttribute("aria-live", "polite");
actions?.prepend(build);

const page = new GridPage(
  {
    status: document.getElementById("grid-status"),
    title: document.getElementById("grid-title"),
    breadcrumb: document.getElementById("grid-breadcrumb"),
    map: document.getElementById("grid-map"),
    overview: document.getElementById("grid-overview"),
    refresh: document.getElementById("grid-refresh"),
    build,
  },
  { projectId: projectFromSearch(location.search) },
);

// A page leaving - or going into the back/forward cache - holds no timer.
addEventListener("pagehide", () => page.stop());
addEventListener("pageshow", (event) => {
  // Back from the back/forward cache: the snapshot is stale, the timer gone.
  if (event.persisted) void page.start();
});

void page.start();
