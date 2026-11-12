/** The index after the last character of `s` that is not `/`. */
function endBeforeSlashes(s: string): number {
  let end = s.length;
  while (end > 0 && s[end - 1] === "/") end--;
  return end;
}

/**
 * `s` without its trailing run of `/`, in one pass: the regex `/\/+$/` restarts at
 * every slash of a long run, which is quadratic.
 */
export function trimTrailingSlashes(s: string): string {
  return s.slice(0, endBeforeSlashes(s));
}

/** `s` without its leading and trailing runs of `/`, in one pass. */
export function trimSlashes(s: string): string {
  const end = endBeforeSlashes(s);
  let start = 0;
  while (start < end && s[start] === "/") start++;
  return s.slice(start, end);
}
