import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Pagination } from "@clickhouse/click-ui/Pagination";

const SIZES = [25, 50, 100];

/** A list's page size, remembered per view in this browser (50 until someone picks). */
export function usePageSize(view: string, fallback = 50): [number, (size: number) => void] {
  const key = `loom_page_size_${view}_v1`;
  const [size, setSize] = useState(() => {
    try {
      const n = Number(localStorage.getItem(key));
      return SIZES.includes(n) ? n : fallback;
    } catch {
      return fallback;
    }
  });
  const choose = (n: number) => {
    try {
      localStorage.setItem(key, String(n));
    } catch {
      /* storage refused: the size holds for this visit */
    }
    setSize(n);
  };
  return [size, choose];
}

export interface ListPagerProps {
  /** The navigation landmark's name ("Memories pages"). */
  label: string;
  /** Zero-based. */
  page: number;
  pageSize: number;
  /** Rows on the current page. */
  shown: number;
  /** Every row the list holds, when the server says; without it, a short page is the last. */
  total?: number;
  onPage: (page: number) => void;
  /** With it, a page-size choice sits beside the pages. */
  onPageSize?: (size: number) => void;
  /** The id of the list's heading (tabIndex -1): a new page scrolls to it and takes focus. */
  topId?: string;
}

/**
 * ListPager — Click UI's Pagination under a list, as the full bar Click UI draws: the range
 * on the left ("1–50 of 1,234 rows"), the page field and arrows on the right, and the page
 * size when the view offers one. A page change scrolls to the list's top, moves focus to
 * its heading and announces the page, so a keyboard or screen-reader user lands where the
 * new rows start. Hidden while one page holds everything.
 */
export function ListPager({ label, page, pageSize, shown, total, onPage, onPageSize, topId }: ListPagerProps) {
  const [said, setSaid] = useState("");
  // The page being typed into the field: Click UI reports every keystroke as a page, so the
  // pager goes there only on Enter or when the field loses focus.
  const [draft, setDraft] = useState<number | null>(null);
  const phone = usePhone();
  // Click UI calls `onChange` for a typed number and, just before its own click handler,
  // for an arrow; the arrows go at once and take the reader to the list's top.
  const pending = useRef(page + 1);
  // Click UI names its arrows after their icons ("chevron-left") and its page field after
  // the number in it; these are the names they read as.
  const nav = useRef<HTMLElement>(null);
  // Click UI resets them as the page or the size changes.
  useLayoutEffect(() => {
    const el = nav.current;
    if (!el) return;
    el.querySelector('[data-testid="prev-btn"]')?.setAttribute("aria-label", "Previous page");
    el.querySelector('[data-testid="next-btn"]')?.setAttribute("aria-label", "Next page");
    el.querySelector('input[type="number"]')?.setAttribute("aria-label", "Page");
    el.querySelector('[data-testid="select-trigger"]')?.setAttribute("aria-label", "Rows per page");
  }, [page, pageSize, draft, phone]);
  const pages = total !== undefined ? Math.max(1, Math.ceil(total / pageSize)) : undefined;
  // A page past the end (its last rows deleted) steps back to the last one.
  const pastEnd = pages !== undefined && page > pages - 1;
  useEffect(() => {
    if (pastEnd && pages !== undefined) onPage(pages - 1);
  }, [pastEnd, pages, onPage]);
  const lastKnown = pages !== undefined ? page >= pages - 1 : shown < pageSize;
  // With a page size picked, the choice stays reachable while the list is longer than the
  // smallest page; a list that fits even that has no pager.
  const sizeChoice = !!onPageSize && !phone && (total ?? shown) > SIZES[0];
  if (page === 0 && lastKnown && !sizeChoice) return null;
  const start = page * pageSize + 1;
  const end = page * pageSize + shown;
  const span = shown === 0 ? `${start.toLocaleString()}` : `${start.toLocaleString()}–${end.toLocaleString()}`;
  const range = total !== undefined ? `${span} of ${total.toLocaleString()}` : span;

  const go = (n: number, { focusList }: { focusList: boolean }) => {
    setDraft(null);
    if (n === page + 1) return;
    onPage(n - 1);
    setSaid(pages !== undefined ? `Page ${n} of ${pages}` : `Page ${n}`);
    const top = focusList && topId ? document.getElementById(topId) : null;
    if (top) {
      top.scrollIntoView?.({ block: "start" });
      top.focus({ preventScroll: true });
    }
  };

  return (
    // biome-ignore lint/a11y/noNoninteractiveElementInteractions: Enter in the page field, which Click UI's NumberField does not report
    <nav
      ref={nav}
      aria-label={label}
      className="list-pager mt-4 border-t border-border pt-3"
      onKeyDown={(e) => {
        if (e.key === "Enter" && e.target instanceof HTMLInputElement && draft !== null)
          go(draft, { focusList: false });
      }}
    >
      <Pagination
        rowCount={range}
        totalPages={pages}
        currentPage={draft ?? page + 1}
        onChange={(n) => {
          pending.current = n;
          setDraft(n);
        }}
        onPrevPageClick={() => go(pending.current, { focusList: true })}
        onNextPageClick={() => go(pending.current, { focusList: true })}
        onPageNumberBlur={() => draft !== null && go(draft, { focusList: false })}
        disableNextButton={lastKnown}
        {...(sizeChoice
          ? {
              pageSize,
              maxRowsPerPageList: SIZES,
              onPageSizeChange: (n: number) => {
                onPageSize?.(n);
                setSaid(`${n} rows per page, page 1`);
              },
              allowAllRows: false,
            }
          : {})}
      />
      <p role="status" className="sr-only">
        {said}
      </p>
    </nav>
  );
}

/** True under 640 px, where the pager has no room for the page size. */
function usePhone(): boolean {
  const query = "(max-width: 639px)";
  const [phone, setPhone] = useState(() => typeof matchMedia === "function" && matchMedia(query).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const m = matchMedia(query);
    const on = () => setPhone(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return phone;
}
