import { forwardRef, useEffect, useRef, useState } from "react";
import { SearchField, type SearchFieldProps } from "@clickhouse/click-ui/SearchField";

export interface DraftSearchProps extends Omit<SearchFieldProps, "value" | "onChange"> {
  /** The settled text: after `debounceMs` without a key, or, without `debounceMs`, on Enter
   *  and when the box is emptied. */
  onSearch: (text: string) => void;
  debounceMs?: number;
}

/** A list's search box that keeps what is typed to itself: a keystroke renders the box, not
 *  the page and its rows, which render again only when the search settles. A `key` change
 *  empties it (a page's Clear filters). */
export const DraftSearch = forwardRef<HTMLInputElement, DraftSearchProps>(function DraftSearch(
  { onSearch, debounceMs, onKeyDown, ...field },
  ref,
) {
  const [text, setText] = useState("");
  // The timer calls the latest render's handler.
  const onSearchRef = useRef(onSearch);
  onSearchRef.current = onSearch;
  useEffect(() => {
    if (debounceMs === undefined) return;
    const t = setTimeout(() => onSearchRef.current(text), debounceMs);
    return () => clearTimeout(t);
  }, [text, debounceMs]);
  return (
    <SearchField
      ref={ref}
      {...field}
      value={text}
      onChange={(value) => {
        setText(value);
        if (debounceMs === undefined && value === "") onSearch("");
      }}
      onKeyDown={(e) => {
        onKeyDown?.(e);
        if (debounceMs === undefined && e.key === "Enter") onSearch(text);
      }}
    />
  );
});
