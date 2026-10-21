import { useRef, useState } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, render, screen } from "../test/rtl";
import { useLastPageFocus } from "./useLastPageFocus";

// A list of named rows with a Load more that a test finishes by hand: `finish(rows, more)`
// ends the read with these rows shown and says whether a page is left.
let finish: (rows: string[], more: boolean) => void = () => {};

function List({ first }: { first: string[] }) {
  const [rows, setRows] = useState(first);
  const [loading, setLoading] = useState(false);
  const [more, setMore] = useState(true);
  const box = useRef<HTMLUListElement | null>(null);
  const search = useRef<HTMLInputElement | null>(null);
  const mark = useLastPageFocus({
    loading,
    more,
    rows: () =>
      [...(box.current?.querySelectorAll<HTMLElement>("[data-name]") ?? [])].map(
        (el) => [el.dataset.name ?? "", el] as [string, HTMLElement],
      ),
    fallback: () => search.current,
  });
  finish = (next, left) => {
    setRows(next);
    setMore(left);
    setLoading(false);
  };
  return (
    <>
      <input ref={search} aria-label="Search" />
      <ul ref={box}>
        {rows.map((r) => (
          <li key={r}>
            <button type="button" data-name={r}>
              {r}
            </button>
          </li>
        ))}
      </ul>
      {more && (
        <button
          type="button"
          onClick={() => {
            mark(rows);
            setLoading(true);
          }}
        >
          Load more
        </button>
      )}
    </>
  );
}

// The button the last page removes leaves focus on the body, as a browser does.
async function pressLoadMore() {
  await userEvent.click(screen.getByRole("button", { name: "Load more" }));
}

afterEach(cleanup);

describe("useLastPageFocus", () => {
  it("focuses the first row the last page added", async () => {
    render(<List first={["a"]} />);
    await pressLoadMore();
    act(() => finish(["a", "b", "c"], false));
    expect(screen.getByRole("button", { name: "b" })).toHaveFocus();
  });

  it("focuses the last row shown when the page added none that show", async () => {
    render(<List first={["a", "b"]} />);
    await pressLoadMore();
    act(() => finish(["a", "b"], false));
    expect(screen.getByRole("button", { name: "b" })).toHaveFocus();
  });

  it("focuses the fallback when no row shows", async () => {
    render(<List first={[]} />);
    await pressLoadMore();
    act(() => finish([], false));
    expect(screen.getByRole("textbox", { name: "Search" })).toHaveFocus();
  });

  it("leaves focus alone while a page is left, or when it did not fall to the page", async () => {
    render(<List first={["a"]} />);
    await pressLoadMore();
    // Focus on the body, so only the page left keeps it there.
    act(() => screen.getByRole("button", { name: "Load more" }).blur());
    act(() => finish(["a", "b"], true));
    expect(document.body).toHaveFocus();

    await pressLoadMore();
    screen.getByRole("textbox", { name: "Search" }).focus();
    act(() => finish(["a", "b", "c"], false));
    expect(screen.getByRole("textbox", { name: "Search" })).toHaveFocus();
  });
});
