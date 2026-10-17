import { useEffect, useState } from "react";

// Below Tailwind's sm breakpoint a list toolbar holds one control; the rest sit under it.
const PHONE_QUERY = "(max-width: 639px)";

function matchesPhone(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(PHONE_QUERY).matches;
}

/** True while the viewport is phone-wide (under 640 px). */
export function usePhone(): boolean {
  const [phone, setPhone] = useState(matchesPhone);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(PHONE_QUERY);
    const sync = () => setPhone(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);
  return phone;
}
