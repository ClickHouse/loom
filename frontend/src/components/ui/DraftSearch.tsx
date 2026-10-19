import { forwardRef, useEffect, useRef, useState } from "react";
import { SearchField, type SearchFieldProps } from "@clickhouse/click-ui/SearchField";

export interface DraftSearchProps extends Omit<SearchFieldProps, "value" | "onChange"> {
  /** The settled text, handed over as `settle` says. */
  onSearch: (text: string) => void;
  /** "enter": on Enter, and when the box is emptied. `{ debounceMs }`: that long after the
   *  last key, when the text differs from what it last handed over (so not on mount). */
  settle: "enter" | { debounceMs: number };
}

/** A search box over a server read: it keeps what is typed to itself, so a keystroke renders
 *  the box, not the page, and the page reads again only when the search settles. A `key`
 *  change empties it (a page's Clear filters). */
export const DraftSearch = forwardRef<HTMLInputElement, DraftSearchProps>(function DraftSearch(
  { onSearch, settle, onKeyDown, ...field },
  ref,
) {
  const [text, setText] = useState("");
  const debounceMs = settle === "enter" ? null : settle.debounceMs;
  // The timer calls the latest render's handler.
  const onSearchRef = useRef(onSearch);
  onSearchRef.current = onSearch;
  const handed = useRef(text);
  useEffect(() => {
    if (debounceMs === null || text === handed.current) return;
    const t = setTimeout(() => {
      handed.current = text;
      onSearchRef.current(text);
    }, debounceMs);
    return () => clearTimeout(t);
  }, [text, debounceMs]);
  return (
    <SearchField
      ref={ref}
      {...field}
      value={text}
      onChange={(value) => {
        setText(value);
        if (settle === "enter" && value === "") onSearch("");
      }}
      onKeyDown={(e) => {
        onKeyDown?.(e);
        if (settle === "enter" && e.key === "Enter") onSearch(text);
      }}
    />
  );
});
