import { useLayoutEffect, useRef, useState } from "react";
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
  // Click UI names its arrows after their icons ("chevron-left") and its page field after
  // the number in it; these are the names they read as.
  const nav = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const el = nav.current;
    if (!el) return;
    el.querySelector('[data-testid="prev-btn"]')?.setAttribute("aria-label", "Previous page");
    el.querySelector('[data-testid="next-btn"]')?.setAttribute("aria-label", "Next page");
    el.querySelector('input[type="number"]')?.setAttribute("aria-label", "Page");
  });
  const pages = total !== undefined ? Math.max(1, Math.ceil(total / pageSize)) : undefined;
  const lastKnown = pages !== undefined ? page >= pages - 1 : shown < pageSize;
  if (page === 0 && lastKnown && pages !== undefined && pages <= 1) return null;
  const start = shown === 0 ? 0 : page * pageSize + 1;
  const end = page * pageSize + shown;
  const range =
    total !== undefined
      ? `${start.toLocaleString()}–${end.toLocaleString()} of ${total.toLocaleString()}`
      : `${start.toLocaleString()}–${end.toLocaleString()}`;

  const go = (n: number) => {
    onPage(n - 1);
    setSaid(pages !== undefined ? `Page ${n} of ${pages}` : `Page ${n}`);
    const top = topId ? document.getElementById(topId) : null;
    if (top) {
      top.scrollIntoView?.({ block: "start" });
      top.focus({ preventScroll: true });
    }
  };

  return (
    <nav ref={nav} aria-label={label} className="list-pager mt-4 border-t border-border pt-3">
      <Pagination
        rowCount={range}
        totalPages={pages}
        currentPage={page + 1}
        onChange={go}
        disableNextButton={lastKnown}
        {...(onPageSize
          ? { pageSize, maxRowsPerPageList: SIZES, onPageSizeChange: onPageSize, allowAllRows: false }
          : {})}
      />
      <p role="status" className="sr-only">
        {said}
      </p>
    </nav>
  );
}
