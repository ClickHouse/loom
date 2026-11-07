// Test helper for recalled-data blocks (lib/prompt-recall.mjs).

/** `text` with each block's id (derived from its text) removed, so text around it
 *  can be compared. */
export const noIds = (text) => String(text).replace(/ id="[0-9a-f]{16}"/g, "");
