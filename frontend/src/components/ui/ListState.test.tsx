import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../test/rtl";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError } from "../../lib/errors";
import { ListState } from "./ListState";

// What a list shows in place of its rows: nothing yet, nothing that matches, or a failed
// read, and a failed read never says the list is empty.
describe("ListState", () => {
  it("empty: the big state with its one action", () => {
    const onAdd = vi.fn();
    render(
      <ListState
        kind="empty"
        icon="folder-open"
        title="Your library is empty"
        description="Add a document to search it."
        action={{ label: "Add document", onClick: onAdd }}
      />,
    );
    expect(screen.getByRole("heading", { name: "Your library is empty" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add document" }));
    expect(onAdd).toHaveBeenCalled();
  });

  it("filtered: one inline line naming the filter, and Clear filters", () => {
    const onClear = vi.fn();
    render(<ListState kind="filtered" things="documents" filter="tag:billing" onClear={onClear} />);
    expect(screen.getByRole("status")).toHaveTextContent("No documents match tag:billing");
    expect(screen.queryByRole("heading")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(onClear).toHaveBeenCalled();
  });

  // A live region says a change to its text: the row mounts its status empty, so the
  // text that follows is announced.
  it("filtered: the status mounts empty and then says the line", () => {
    const first = renderToStaticMarkup(<ListState kind="filtered" things="runs" filter="failed" onClear={() => {}} />);
    const mounted = new DOMParser().parseFromString(first, "text/html").querySelector('[role="status"]');
    expect(mounted?.textContent).toBe("");
    render(<ListState kind="filtered" things="runs" filter="failed" onClear={() => {}} />);
    expect(screen.getByRole("status")).toHaveTextContent("No runs match failed");
  });

  it("error: what failed, the humanized message, the request id and a secondary Retry", () => {
    const onRetry = vi.fn();
    const err = new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "ch down", requestId: "req-123" });
    render(<ListState kind="error" things="documents" error={err} onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load documents");
    expect(screen.getByRole("heading", { name: "Couldn't load documents" })).toBeInTheDocument();
    expect(screen.getByText(/Loom's database didn't answer/)).toBeInTheDocument();
    // The id is the identifier, in code; its caption is prose.
    const rid = screen.getByText("req-123");
    expect(rid.tagName).toBe("CODE");
    expect(rid.parentElement).toHaveTextContent("request id req-123");
    expect(screen.queryByText(/empty/i)).toBeNull();
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry.className).toMatch(/secondary/);
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalled();
  });

  it("error: a message the page already humanized, and its request id", () => {
    render(<ListState kind="error" things="memories" error="Loom took too long." requestId="r-9" onRetry={() => {}} />);
    expect(screen.getByText("Loom took too long.")).toBeInTheDocument();
    expect(screen.getByText("r-9").tagName).toBe("CODE");
  });

  // Retry leaves with the error state, so focus goes to the page's heading, not the page.
  it("puts focus on the page heading when Retry is pressed", () => {
    const onRetry = vi.fn();
    render(
      <main>
        <h1>Library</h1>
        <ListState kind="error" things="documents" error="boom" onRetry={onRetry} />
      </main>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Library" })).toHaveFocus();
  });

  // Clear filters leaves with the filtered state, so focus goes to the list's search field,
  // else to the page's heading, never to <body>.
  it("puts focus on the list's search field when Clear filters is pressed", () => {
    const onClear = vi.fn();
    render(
      <main>
        <h1>Library</h1>
        <search aria-label="Search and filters">
          <input type="search" aria-label="Search library" />
        </search>
        <ListState kind="filtered" things="documents" filter="tag:billing" onClear={onClear} />
      </main>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(onClear).toHaveBeenCalled();
    expect(screen.getByRole("searchbox", { name: "Search library" })).toHaveFocus();
  });

  it("puts focus on the page heading when a list without a search clears its filters", () => {
    render(
      <main>
        <h1>Runs</h1>
        <ListState kind="filtered" things="runs" filter="failed" onClear={() => {}} />
      </main>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("heading", { name: "Runs" })).toHaveFocus();
  });
});
