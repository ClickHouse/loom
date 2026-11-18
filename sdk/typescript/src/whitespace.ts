/** Whitespace removed by the server's identity filters, at either edge. */
export function hasIdentityWhitespace(value: string): boolean {
  return [value.charAt(0), value.slice(-1)].some(
    (edge) => /\p{White_Space}/u.test(edge) || (edge >= "\u001c" && edge <= "\u001f"),
  );
}
