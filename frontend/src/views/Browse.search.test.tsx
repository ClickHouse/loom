import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BrowseEntry } from "../api";
import { useStore } from "../lib/state";
import { Browse } from "./Browse";

// The browse read asks for a page of 200. A page that comes back full may leave namespaces
// out, so the search box then asks the server (`namespace.browse` `query`) and the page says
// it is cut; a shorter page holds every namespace and the box filters it in place. Either
// way the list draws 50 rows and "Show all N".

const browseNamespaces = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, browseNamespaces: (...args: unknown[]) => browseNamespaces(...args) };
});
vi.mock("../lib/sharedReads", () => ({ sharedMine: () => Promise.resolve([]) }));

const row = (namespace: string): BrowseEntry => ({
  namespace,
  description: "",
  kind: "memory",
  owner_email: "",
  member_count: 1,
  created_at: "",
});
const page = (n: number) => Array.from({ length: n }, (_, i) => row(`team-${String(i).padStart(3, "0")}`));

/** The options of the nth browseNamespaces call. */
const optsOf = (n: number) =>
  browseNamespaces.mock.calls[n][1] as { query?: string; limit?: number; signal?: AbortSignal };

beforeEach(() => {
  browseNamespaces.mockReset();
  useStore.setState({ authed: true, identity: { org: "acme", namespace: "team", agent: "loom-ui" }, view: "browse" });
});
afterEach(() => {
  cleanup();
  useStore.setState({ identity: null });
});

describe("Browse: a full page", () => {
  it("says the list is cut and sends the typed text to the server", async () => {
    browseNamespaces.mockResolvedValueOnce(page(200));
    render(<Browse />);
    expect(await screen.findByText("team-000")).toBeInTheDocument();
    expect(screen.getByText(/Showing the first 200 namespaces/)).toBeInTheDocument();
    expect(optsOf(0)).toMatchObject({ limit: 200 });

    // A namespace past the page: only the server knows it.
    browseNamespaces.mockResolvedValueOnce([row("zeta-ops")]);
    await userEvent.type(screen.getByLabelText("Search namespaces"), "zeta");
    expect(await screen.findByText("zeta-ops")).toBeInTheDocument();
    expect(browseNamespaces).toHaveBeenCalledTimes(2);
    expect(browseNamespaces.mock.calls[1][0]).toBe("acme");
    expect(optsOf(1)).toMatchObject({ query: "zeta", limit: 200 });
    // A search shows its matches, not the cut page's notice.
    expect(screen.queryByText(/Showing the first 200 namespaces/)).toBeNull();
  });

  it("aborts the previous search when the text changes", async () => {
    browseNamespaces.mockResolvedValueOnce(page(200));
    render(<Browse />);
    expect(await screen.findByText("team-000")).toBeInTheDocument();
    const box = screen.getByLabelText("Search namespaces");

    browseNamespaces.mockReturnValueOnce(new Promise(() => {}));
    await userEvent.type(box, "zet");
    await waitFor(() => expect(browseNamespaces).toHaveBeenCalledTimes(2));
    const first = optsOf(1);
    expect(first).toMatchObject({ query: "zet" });
    expect(first.signal?.aborted).toBe(false);

    browseNamespaces.mockResolvedValueOnce([row("zeta-ops")]);
    await userEvent.type(box, "a");
    expect(await screen.findByText("zeta-ops")).toBeInTheDocument();
    expect(first.signal?.aborted).toBe(true);
    expect(optsOf(2)).toMatchObject({ query: "zeta" });
  });

  it("offers a request only once the server has found no such namespace", async () => {
    browseNamespaces.mockResolvedValueOnce(page(200));
    render(<Browse />);
    expect(await screen.findByText("team-000")).toBeInTheDocument();

    let answer: (rows: BrowseEntry[]) => void = () => {};
    browseNamespaces.mockReturnValueOnce(new Promise((r) => (answer = r)));
    await userEvent.type(screen.getByLabelText("Search namespaces"), "secret-ops");
    await waitFor(() => expect(browseNamespaces).toHaveBeenCalledTimes(2));
    // In flight: no card, and no "nothing matches".
    expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();
    expect(screen.queryByText(/No public namespaces match/)).toBeNull();

    await act(async () => answer([]));
    expect(await screen.findByRole("button", { name: "Request access" })).toBeInTheDocument();
  });

  it("points a repository the server finds past the page to Repositories, not to a request", async () => {
    browseNamespaces.mockResolvedValueOnce(page(200));
    render(<Browse />);
    expect(await screen.findByText("team-000")).toBeInTheDocument();

    browseNamespaces.mockResolvedValueOnce([{ ...row("code-repo"), kind: "code" }]);
    await userEvent.type(screen.getByLabelText("Search namespaces"), "code-repo");
    expect(await screen.findByText(/is a repository/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Repositories" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();
  });

  it("says a search that comes back full may be missing matches", async () => {
    browseNamespaces.mockResolvedValueOnce(page(200));
    render(<Browse />);
    expect(await screen.findByText("team-000")).toBeInTheDocument();

    browseNamespaces.mockResolvedValueOnce(page(200));
    await userEvent.type(screen.getByLabelText("Search namespaces"), "team");
    await waitFor(() => expect(browseNamespaces).toHaveBeenCalledTimes(2));
    expect(await screen.findByTestId("search-cut")).toHaveTextContent(/More namespaces may match “team”/);
    expect(screen.getByText("200+ namespaces")).toBeInTheDocument();
    expect(screen.queryByTestId("browse-cut")).toBeNull();

    // A search that holds every match says nothing more.
    browseNamespaces.mockResolvedValueOnce([row("team-ops")]);
    await userEvent.type(screen.getByLabelText("Search namespaces"), "-ops");
    expect(await screen.findByText("1 namespace")).toBeInTheDocument();
    expect(screen.queryByTestId("search-cut")).toBeNull();
  });

  it("shows a failed search as an error with Retry, not as no match", async () => {
    const { ApiError } = await import("../api");
    browseNamespaces.mockResolvedValueOnce(page(200));
    render(<Browse />);
    expect(await screen.findByText("team-000")).toBeInTheDocument();

    browseNamespaces.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "upstream down", requestId: "req-7" }),
    );
    await userEvent.type(screen.getByLabelText("Search namespaces"), "zeta");
    expect(await screen.findByText("Couldn't load namespaces")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();

    browseNamespaces.mockResolvedValueOnce([row("zeta-ops")]);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("zeta-ops")).toBeInTheDocument();
    expect(optsOf(2)).toMatchObject({ query: "zeta" });
  });

  it("drops the last org's search when the org changes", async () => {
    browseNamespaces.mockResolvedValueOnce(page(200));
    render(<Browse />);
    expect(await screen.findByText("team-000")).toBeInTheDocument();
    browseNamespaces.mockResolvedValueOnce([row("zeta-ops")]);
    await userEvent.type(screen.getByLabelText("Search namespaces"), "zeta");
    expect(await screen.findByText("zeta-ops")).toBeInTheDocument();

    // The other org's page is short: its filter is local, and acme's answer is gone.
    browseNamespaces.mockResolvedValueOnce([row("zeta-two"), row("omega")]);
    act(() => useStore.setState({ identity: { org: "other", namespace: "team", agent: "loom-ui" } }));
    expect(await screen.findByText("zeta-two")).toBeInTheDocument();
    expect(screen.queryByText("zeta-ops")).toBeNull();
    expect(screen.queryByText("omega")).toBeNull();
    await new Promise((r) => setTimeout(r, 400));
    expect(browseNamespaces).toHaveBeenCalledTimes(3);
  });
});

describe("Browse: a page under the limit", () => {
  it("filters in place: no notice, no second read", async () => {
    browseNamespaces.mockResolvedValueOnce([row("alpha"), row("beta")]);
    render(<Browse />);
    expect(await screen.findByText("alpha")).toBeInTheDocument();
    expect(screen.queryByText(/Showing the first/)).toBeNull();

    await userEvent.type(screen.getByLabelText("Search namespaces"), "bet");
    await waitFor(() => expect(screen.queryByText("alpha")).toBeNull());
    expect(screen.getByText("beta")).toBeInTheDocument();
    // Past the debounce: still the one read.
    await new Promise((r) => setTimeout(r, 400));
    expect(browseNamespaces).toHaveBeenCalledTimes(1);
  });
});

describe("Browse: Show all", () => {
  it("draws 50 rows, then every row on Show all, and moves the focus to the first new one", async () => {
    browseNamespaces.mockResolvedValueOnce(page(60));
    render(<Browse />);
    expect(await screen.findByText("team-049")).toBeInTheDocument();
    expect(screen.queryByText("team-050")).toBeNull();
    // The count is every namespace, not the rows drawn.
    expect(screen.getByText("60 namespaces")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Show all 60" }));
    expect(await screen.findByText("team-059")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByText("team-050").closest("button")));
  });
});
