/**
 * The shell of the map page (web_chat/grid.html): find its nodes, hand them to
 * the page and let it show the project named by ?project=<id>.
 */

import { GridPage, projectFromSearch } from "./page.js";

const page = new GridPage(
  {
    status: document.getElementById("grid-status"),
    title: document.getElementById("grid-title"),
    breadcrumb: document.getElementById("grid-breadcrumb"),
    map: document.getElementById("grid-map"),
    overview: document.getElementById("grid-overview"),
    refresh: document.getElementById("grid-refresh"),
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
