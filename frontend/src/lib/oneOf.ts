/** `v` when it is one of `allowed`, else null: the checked way to read a `<select>` or
 *  Tabs value into union-typed state. Callers leave their state unchanged on null. */
export function oneOf<T extends string>(allowed: readonly T[], v: string | null): T | null {
  return allowed.find((a) => a === v) ?? null;
}
