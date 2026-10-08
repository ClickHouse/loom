/** A plain JSON object: not null and not an array. Untrusted values (a server body, a stored
 *  or parsed string) narrow through this instead of a cast, so a wrong shape reads as absent. */
export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
