import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Pagination } from "@clickhouse/click-ui/Pagination";
import { PHONE_QUERY, useMedia } from "../../lib/useMedia";

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
  /** `total` is what a server that pages no further answered (its newest N), not a count. */
  capped?: boolean;
  /** The largest offset the server serves: no page that starts past it is offered. */
  maxOffset?: number;
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
export function ListPager({
  label,
  page,
  pageSize,
  shown,
  total,
  capped,
  maxOffset,
  onPage,
  onPageSize,
  topId,
}: ListPagerProps) {
  const [said, setSaid] = useState("");
  // The page the reader asked for: when the view sends them back from it to the last page
  // (a typed page past the end), the page they land on is announced instead.
  const saidPage = useRef<number | null>(null);
  // The page being typed into the field: Click UI reports every keystroke as a page, so the
  // pager goes there only on Enter or when the field loses focus.
  const [draft, setDraft] = useState<number | null>(null);
  const phone = useMedia(PHONE_QUERY);
  // Click UI calls `onChange` for a typed number and, just before its own click handler,
  // for an arrow; the arrows go at once and take the reader to the list's top.
  const pending = useRef(page + 1);
  // Click UI names its arrows after their icons ("chevron-left") and its page field after
  // the number in it; these are the names they read as.
  const nav = useRef<HTMLElement>(null);
  // Click UI resets them as the page or the size changes, and the bar may first render
  // nothing: they are set again after every render, as SplitButton does.
  useLayoutEffect(() => {
    const el = nav.current;
    if (!el) return;
    el.querySelector('[data-testid="prev-btn"]')?.setAttribute("aria-label", "Previous page");
    el.querySelector('[data-testid="next-btn"]')?.setAttribute("aria-label", "Next page");
    el.querySelector('input[type="number"]')?.setAttribute("aria-label", "Page");
    el.querySelector('[data-testid="select-trigger"]')?.setAttribute("aria-label", "Rows per page");
  });
  // The pages the server can serve: one that starts past `maxOffset` would be refused.
  const reachable = maxOffset !== undefined ? Math.floor(maxOffset / pageSize) + 1 : undefined;
  const counted = total !== undefined ? Math.max(1, Math.ceil(total / pageSize)) : undefined;
  const cut = counted !== undefined && reachable !== undefined && counted > reachable;
  const pages = cut ? reachable : counted;
  // A page past the end (its last rows deleted) steps back to the last one.
  const pastEnd = pages !== undefined && page > pages - 1;
  useEffect(() => {
    if (pastEnd && pages !== undefined) onPage(pages - 1);
  }, [pastEnd, pages, onPage]);
  useEffect(() => {
    const asked = saidPage.current;
    if (asked === null) return;
    if (page === asked) {
      // The page asked for is there: nothing will send the reader back from it.
      if (pages !== undefined && page <= pages - 1) saidPage.current = null;
      return;
    }
    saidPage.current = null;
    // A filter, a search or a sort also moves the page (to the first): only a move back to
    // the last page is the view's answer to the page asked for.
    if (pages !== undefined && page < asked && page === pages - 1) setSaid(`Page ${page + 1} of ${pages}`);
  }, [page, pages]);
  const lastKnown =
    pages !== undefined ? page >= pages - 1 : shown < pageSize || (reachable !== undefined && page >= reachable - 1);
  // With a page size picked, the choice stays reachable while the list is longer than the
  // smallest page; a list that fits even that has no pager.
  const sizeChoice = !!onPageSize && !phone && (total ?? shown) > SIZES[0];
  if (page === 0 && lastKnown && !sizeChoice) return null;
  const start = page * pageSize + 1;
  const end = page * pageSize + shown;
  const span = shown === 0 ? `${start.toLocaleString()}` : `${start.toLocaleString()}–${end.toLocaleString()}`;
  const counts = total === undefined ? span : `${span} of ${capped ? "the newest " : ""}${total.toLocaleString()}`;
  const range =
    cut && reachable !== undefined ? `${counts} · pages 1–${reachable.toLocaleString()} can be opened` : counts;

  const go = (typed: number, { focusList }: { focusList: boolean }) => {
    setDraft(null);
    const n = reachable !== undefined ? Math.min(typed, reachable) : typed;
    if (n === page + 1) return;
    onPage(n - 1);
    saidPage.current = n - 1;
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
                saidPage.current = null;
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
