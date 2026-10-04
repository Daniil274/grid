/** Uploaded documents are workspace links in the ordinary message text.
 * Keeping their paths there makes them available to routing, queues, edits
 * and saved conversations without a second attachment format.
 */
const HEADER = "Attached files:";
// One line of the block withFiles writes; only workspace downloads count.
const LINE = /^- \[[^\n]*\]\((\/api\/workspace\/files\/[^)\s]+)\) — workspace path: ("(?:[^"\\]|\\.)*")$/;

export function withFiles(text, files) {
  if (!files.length) return text;
  const links = files.map(({ name, path, url }) => {
    // Brackets delimit markdown links; show parentheses in the link label.
    // The workspace path below and the download retain the exact filename.
    const label = name.replaceAll("[", "(").replaceAll("]", ")");
    return `- [${label}](${url}) — workspace path: ${JSON.stringify(path)}`;
  });
  return [text, `${HEADER}\n${links.join("\n")}`].filter(Boolean).join("\n\n");
}

/**
 * The inverse of withFiles, for display: the message's own text and the files
 * its closing block names. A text without that exact block is all text.
 * @returns {{text: string, files: {name: string, path: string, url: string}[]}}
 */
export function splitFiles(content) {
  const start = content.lastIndexOf(HEADER);
  if (start < 0 || (start > 0 && !content.slice(0, start).endsWith("\n\n"))) return { text: content, files: [] };
  const lines = content.slice(start + HEADER.length).replace(/^\n/, "").split("\n");
  const files = [];
  for (const line of lines) {
    const match = LINE.exec(line);
    if (!match) return { text: content, files: [] };
    let path;
    try {
      path = JSON.parse(match[2]);
    } catch {
      return { text: content, files: [] };
    }
    files.push({ name: path.split("/").pop() || path, path, url: match[1] });
  }
  if (!files.length) return { text: content, files: [] };
  return { text: content.slice(0, start).trimEnd(), files };
}

export function fileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
