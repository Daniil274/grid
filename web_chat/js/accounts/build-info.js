/**
 * The server card's build line: which code the running server actually loaded.
 *
 * A restart that seems to change nothing is often a server that still runs the
 * old commit (or an image built from another checkout), so the version and the
 * short commit stand next to "Restart server" - quiet, but there to compare.
 * Values come from GET /api/admin/server (`build`), computed once at startup.
 */

/** "Running Grid 0.2.0 · f244d18"; "" when the server reported no build. */
export function buildLabel(build) {
  if (!build) return "";
  const version = build.version || "unknown";
  const commit = build.commit || "unknown";
  return `Running Grid ${version} · ${commit}`;
}

/** The full commit for the line's tooltip, or "" when it is unknown. */
export function buildTitle(build) {
  const commit = build?.commit_full;
  return commit && commit !== "unknown" ? `Commit ${commit}${build.source ? ` (from ${build.source})` : ""}` : "";
}
