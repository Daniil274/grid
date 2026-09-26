/**
 * Images a user attaches to a message.
 *
 * The browser only shrinks what it sends: a picture is scaled down to
 * MAX_SIDE and re-encoded as JPEG, so a phone photo does not travel as a
 * 12 MB frame over the socket. The server checks and re-encodes every image
 * again (`web_chat/attachments.py`); nothing here is trusted.
 */

export const MAX_IMAGES = 8;
export const ACCEPTED_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const MAX_SIDE = 2048;
const QUALITY = 0.9;
const MAX_FILE_BYTES = 15 * 1024 * 1024;

/** The largest size within MAX_SIDE that keeps the aspect ratio. */
export function fitWithin(width, height, maxSide = MAX_SIDE) {
  const scale = Math.min(1, maxSide / Math.max(width, height));
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

/** A user-facing reason the file cannot be attached, or null. */
export function rejectReason(file) {
  if (!ACCEPTED_TYPES.includes(file.type)) return `${file.name || "This file"} is not a PNG, JPEG, WebP or GIF image.`;
  if (file.size > MAX_FILE_BYTES) return `${file.name || "This image"} is larger than 15 MB.`;
  return null;
}

/**
 * The file as a JPEG data URL no larger than MAX_SIDE on its longer side.
 * @param {File} file an accepted image (see rejectReason)
 * @returns {Promise<string>}
 */
export async function imageToDataUrl(file) {
  const bitmap = await createImageBitmap(file);
  try {
    const { width, height } = fitWithin(bitmap.width, bitmap.height);
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    // JPEG has no transparency: paint it on white rather than black.
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, width, height);
    context.drawImage(bitmap, 0, 0, width, height);
    return canvas.toDataURL("image/jpeg", QUALITY);
  } finally {
    bitmap.close();
  }
}
