/** Agent-encoded values.
 *
 *  Loom stores a memory's `value` verbatim. Some agents wrap what they store
 *  as base64 — the whole value, or an envelope such as
 *  `{"encoding": "…base64…", "sha256": "…", "payload": "<base64>"}` — which
 *  the viewer would otherwise show as a wall of characters. This detects the
 *  two shapes client-side and decodes for display only; the stored value is
 *  never rewritten from here. */
import { isRecord } from "./guards";

export interface EncodedValue {
  kind: "envelope" | "base64";
  /** Envelope field that held the payload, or "value" for a whole-value encoding. */
  field: string;
  /** The envelope's declared encoding, when it named one. */
  encoding: string;
  /** Decoded text; pretty-printed when it parses as JSON. */
  decoded: string;
  decodedIsJson: boolean;
}

const MIN_LEN = 64;
const PAYLOAD_KEYS = ["payload", "data", "content", "body", "value", "blob"];
const B64 = /^[A-Za-z0-9+/\s]+={0,2}$/;
const B64_URL = /^[A-Za-z0-9_\-\s]+={0,2}$/;

function decodeBase64Text(candidate: string): string | null {
  const s = candidate.replace(/\s+/g, "");
  if (s.length < MIN_LEN || s.length % 4 !== 0) return null;
  if (!B64.test(s) && !B64_URL.test(s)) return null;
  try {
    const std = s.replace(/-/g, "+").replace(/_/g, "/");
    const bin = atob(std);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    if (!text) return null;
    let printable = 0;
    for (const ch of text) {
      const code = ch.codePointAt(0) ?? 0;
      if (code >= 32 || ch === "\n" || ch === "\r" || ch === "\t") printable++;
    }
    return printable / text.length >= 0.9 ? text : null;
  } catch {
    return null;
  }
}

function pretty(text: string): { text: string; isJson: boolean } {
  const t = text.trim();
  if (t.startsWith("{") || t.startsWith("[")) {
    try {
      return { text: JSON.stringify(JSON.parse(t), null, 2), isJson: true };
    } catch {
      /* not JSON */
    }
  }
  return { text, isJson: false };
}

/** The envelope shape on an already-parsed object (a card that holds the parsed value
 *  asks here instead of serialising and re-parsing it), or null when no field decodes. */
export function detectEncodedRecord(obj: unknown): EncodedValue | null {
  if (!isRecord(obj)) return null;
  const encoding = String(obj.encoding ?? obj.content_encoding ?? "").toLowerCase();
  for (const key of PAYLOAD_KEYS) {
    const inner = obj[key];
    if (typeof inner === "string" && inner.length >= MIN_LEN) {
      const decoded = decodeBase64Text(inner);
      if (decoded !== null) {
        const p = pretty(decoded);
        return { kind: "envelope", field: key, encoding, decoded: p.text, decodedIsJson: p.isJson };
      }
    }
  }
  return null;
}

/** Returns the decoded view of an agent-encoded value, or null for a plain one. */
export function detectEncodedValue(raw: string | null | undefined): EncodedValue | null {
  if (!raw || raw.length < MIN_LEN) return null;
  const stripped = raw.trim();
  if (stripped.startsWith("{") && stripped.length <= 256 * 1024) {
    let obj: unknown = null;
    try {
      obj = JSON.parse(stripped);
    } catch {
      obj = null;
    }
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      return detectEncodedRecord(obj);
    }
  }
  const whole = decodeBase64Text(stripped);
  if (whole !== null) {
    const p = pretty(whole);
    return { kind: "base64", field: "value", encoding: "", decoded: p.text, decodedIsJson: p.isJson };
  }
  return null;
}
