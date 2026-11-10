import { useEffect, useState } from "react";

/** Under Tailwind's `sm` breakpoint: a phone, where a toolbar holds one control. */
export const PHONE_QUERY = "(max-width: 639px)";

/** A touch screen: the token form's search order moves rows by arrows there, with no grip. */
export const COARSE_POINTER = "(pointer: coarse)";

/** Whether `query` matches, kept current as the viewport changes. */
export function useMedia(query: string): boolean {
  const [on, setOn] = useState(() => typeof matchMedia === "function" && matchMedia(query).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const mq = matchMedia(query);
    const sync = () => setOn(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, [query]);
  return on;
}
