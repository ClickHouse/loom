/**
 * `s` without its trailing run of `/` (and its leading run too when `leading`), in one
 * pass: the regex `/\/+$/` restarts at every slash of a long run, which is quadratic.
 */
export function trimSlashes(s: string, leading = false): string {
  let end = s.length;
  while (end > 0 && s[end - 1] === "/") end--;
  let start = 0;
  while (leading && start < end && s[start] === "/") start++;
  return s.slice(start, end);
}
