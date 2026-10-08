// Bounded reads of a session transcript (JSONL).
//
// A long session's transcript runs to tens of megabytes, and every Stop and
// SessionEnd hook reads it on the host's critical path. A hook reads its
// tail, its head and tail, or a byte window from its own cursor; only a Stop
// gate that must see every edit of the session reads it whole, and then only
// under MAX_WHOLE_BYTES. A line without its newline is still being written,
// so a cursor only ever moves past complete lines.
//
// Every function here returns an empty result on any I/O error.

import fs from "node:fs";

/** What the tail readers (usage, run, working memory) look at. */
export const TAIL_BYTES = 1024 * 1024;
/** A transcript larger than this is not read whole: the reader acts as if
 *  there were none. */
export const MAX_WHOLE_BYTES = 64 * 1024 * 1024;

/** Parse JSONL text into objects, skipping empty, partial and foreign lines. */
export function parseLines(text) {
  const out = [];
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    try {
      out.push(JSON.parse(line));
    } catch {
      /* partial or foreign line */
    }
  }
  return out;
}

/** The file's size in bytes; 0 when it cannot be read. */
export function fileSize(file) {
  try {
    return fs.statSync(file).size;
  } catch {
    return 0;
  }
}

/** Bytes [start, start + length) of `file`, clipped to its end. */
export function readRange(file, start, length) {
  let fd;
  try {
    fd = fs.openSync(file, "r");
    const buf = Buffer.alloc(Math.max(0, length));
    const n = fs.readSync(fd, buf, 0, buf.length, Math.max(0, start));
    return buf.subarray(0, n);
  } catch {
    return Buffer.alloc(0);
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        /* already closed */
      }
    }
  }
}

/**
 * The complete lines of `buf`, which was read from byte `base` of the file:
 * `[{ text, start, end }]`, `end` being the offset just past the newline. The
 * last line is left out when it has no newline. With `partialHead` the first
 * line is left out too: the read began inside it.
 */
export function completeLines(buf, base = 0, { partialHead = false } = {}) {
  const out = [];
  let pos = 0;
  if (partialHead) {
    const nl = buf.indexOf(10);
    if (nl < 0) return out;
    pos = nl + 1;
  }
  while (pos < buf.length) {
    const nl = buf.indexOf(10, pos);
    if (nl < 0) break;
    out.push({ text: buf.toString("utf8", pos, nl), start: base + pos, end: base + nl + 1 });
    pos = nl + 1;
  }
  return out;
}

/** The last `maxBytes` of the file as text, from a line boundary. */
export function readTail(file, maxBytes = TAIL_BYTES) {
  const size = fileSize(file);
  const start = Math.max(0, size - maxBytes);
  const buf = readRange(file, start, size - start);
  if (start === 0) return buf.toString("utf8");
  const nl = buf.indexOf(10);
  return nl < 0 ? "" : buf.toString("utf8", nl + 1);
}

/** The first `headBytes` and the last `tailBytes` as text, each cut at a
 *  line boundary; the whole file when it is no larger than both. */
export function readHeadTail(file, headBytes, tailBytes) {
  const size = fileSize(file);
  if (size <= headBytes + tailBytes) return readRange(file, 0, size).toString("utf8");
  const head = readRange(file, 0, headBytes);
  const cut = head.lastIndexOf(10);
  return (cut < 0 ? "" : head.toString("utf8", 0, cut + 1)) + readTail(file, tailBytes);
}

/**
 * Up to `maxBytes` of complete lines from byte `offset`:
 * `{ lines, next }`, `next` being the offset just past the last complete
 * line (or `offset` when none is complete yet). An offset past the end of the
 * file (a file replaced under the cursor) reads from 0. A single line longer
 * than `maxBytes` is stepped over whole, so it cannot stall a cursor.
 */
export function readLinesFrom(file, offset, maxBytes) {
  const size = fileSize(file);
  const from = Number.isInteger(offset) && offset >= 0 && offset <= size ? offset : 0;
  const lines = completeLines(readRange(file, from, maxBytes), from);
  if (lines.length) return { lines, next: lines[lines.length - 1].end };
  if (size - from <= maxBytes) return { lines, next: from }; // only a partial line so far
  // One line longer than the window: find its end and step past it.
  for (let at = from + maxBytes; at < size; at += maxBytes) {
    const nl = readRange(file, at, maxBytes).indexOf(10);
    if (nl >= 0) return { lines, next: at + nl + 1 };
  }
  return { lines, next: from };
}

/** Every JSONL object of the file; [] when it is missing, unreadable or over
 *  `maxBytes`. */
export function readAllLines(file, maxBytes = MAX_WHOLE_BYTES) {
  const size = fileSize(file);
  if (!file || size > maxBytes) return [];
  return parseLines(readRange(file, 0, size).toString("utf8"));
}
