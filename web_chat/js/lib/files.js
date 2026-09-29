/** Uploaded documents are workspace links in the ordinary message text.
 * Keeping their paths there makes them available to routing, queues, edits
 * and saved conversations without a second attachment format.
 */
export function withFiles(text, files) {
  if (!files.length) return text;
  const links = files.map(({ name, path, url }) => {
    // Brackets delimit markdown links; show parentheses in the link label.
    // The workspace path below and the download retain the exact filename.
    const label = name.replaceAll("[", "(").replaceAll("]", ")");
    return `- [${label}](${url}) — workspace path: ${JSON.stringify(path)}`;
  });
  return [text, `Attached files:\n${links.join("\n")}`].filter(Boolean).join("\n\n");
}

export function fileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
