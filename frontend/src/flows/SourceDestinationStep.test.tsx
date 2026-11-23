import userEvent from "@testing-library/user-event";
import { type FormEvent, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listCollections: vi.fn(),
  createCollection: vi.fn(),
  deleteCollection: vi.fn(),
}));
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast: vi.fn(),
}));

import { type CollectionEntry, type CollectionListPage, createCollection, deleteCollection, listCollections } from "../api";
import { invalidateSharedCollections } from "../lib/collectionPlaces";
import { COLLECTIONS_PAGE, COLLECTIONS_PAGES } from "../lib/collections";
import { axeViolations } from "../test/axe";
import {
  type SourceDestinationCopy,
  type SourceDestinationProps,
  SourceDestinationStep,
} from "./SourceDestinationStep";

const list = vi.mocked(listCollections);
const create = vi.mocked(createCollection);

const copy: SourceDestinationCopy = {
  title: "Destination",
  description: "Choose a collection. Its grants do not grant access to your connection.",
  searchLabel: "Search this page",
  searchHint: "Search names and addresses on this page. Use Load more for the next page.",
  pageScope: "this page",
  destinationsLabel: "Writable collections",
  loading: "Loading collections…",
  loadError: "Collections could not be loaded.",
  things: "collections",
  empty: "No writable ordinary collections on this page.",
  previous: "Previous page",
  next: "Load more",
  refresh: "Refresh collections",
  create: "New collection",
  createHint: "Create at the top level or inside a writable collection on this page.",
  topLevelHint: "Top-level collection. Review its grants before publishing.",
  selectionHint: "Review effective access in the next step. The server checks your permissions again.",
  pagingError: "The next page repeated a cursor. Refresh the collection list.",
  pagingLimit: "More collections exist. This view has reached its page limit.",
  inheritanceHint: (parent) => `Inherits grants from ${parent}. Review effective access before publishing.`,
  created: (namespace) => `Collection confirmed: ${namespace}. Select it after reviewing the list.`,
  selected: (namespace) => `Selected destination: ${namespace}`,
  page: (number) => `Page ${number}`,
};

function row(namespace: string, changes: Partial<CollectionEntry> = {}): CollectionEntry {
  return { namespace, title: namespace, personal: false, parent: "", role: "writer", ...changes };
}
const alpha = row("col-alpha", { title: "Alpha" });
const beta = row("col-beta", { title: "Beta", role: "admin", parent: "col-team" });
const pageOf = (collections: CollectionEntry[], next_cursor: string | null = null): CollectionListPage => ({
  collections,
  next_cursor,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function Harness(props: SourceDestinationProps) {
  const [value, setValue] = useState(props.value);
  return (
    <SourceDestinationStep
      {...props}
      value={value}
      onChange={(next) => {
        setValue(next);
        props.onChange(next);
      }}
    />
  );
}

function mount(overrides: Partial<SourceDestinationProps> = {}) {
  let props: SourceDestinationProps = {
    org: "acme",
    callerKey: "owner-a:auth-1",
    contextKey: "connection-a:selection-1:snapshot",
    value: null,
    copy,
    onChange: vi.fn(),
    onInvalidated: vi.fn(),
    onCollectionCreated: vi.fn(),
    ...overrides,
  };
  const view = render(<Harness {...props} />);
  return {
    ...view,
    props,
    update: (next: Partial<SourceDestinationProps>) => {
      props = { ...props, ...next };
      view.rerender(<Harness {...props} />);
    },
  };
}

async function chooseAlpha() {
  const choice = await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
  fireEvent.click(choice);
  await waitFor(() => expect(choice).toBeChecked());
}

describe("SourceDestinationStep", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    list.mockResolvedValue(pageOf([alpha, beta]));
  });
  afterEach(cleanup);

  it("reads one cancellable page and excludes personal, readonly and non-collection namespaces", async () => {
    list.mockResolvedValue(
      pageOf([
        alpha,
        row("col-own", { personal: true, role: "admin" }),
        row("col-me-owner", { role: "admin" }),
        row("personal-owner", { role: "admin" }),
        row("u-owner-hash", { role: "admin" }),
        row("col-view", { role: "viewer" }),
      ]),
    );
    const { props } = mount();
    expect(await screen.findByRole("radio", { name: "Alpha (col-alpha)" })).toBeInTheDocument();
    expect(screen.getAllByRole("radio")).toHaveLength(1);
    expect(list).toHaveBeenCalledExactlyOnceWith("acme", {
      cursor: null,
      limit: COLLECTIONS_PAGE,
      signal: expect.any(AbortSignal),
    });
    expect(props.onChange).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
    expect(deleteCollection).not.toHaveBeenCalled();
  });

  it("pages only on request, with one page of choices at a time", async () => {
    list.mockResolvedValueOnce(pageOf([alpha], "page-two")).mockResolvedValueOnce(pageOf([beta]));
    mount();
    await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
    expect(list).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: copy.next }));
    await screen.findByRole("radio", { name: "Beta (col-beta)" });
    expect(screen.getByRole("heading", { name: copy.title })).toHaveFocus();
    expect(screen.queryByRole("radio", { name: "Alpha (col-alpha)" })).not.toBeInTheDocument();
    expect(list.mock.calls[1][1]).toMatchObject({ cursor: "page-two", limit: COLLECTIONS_PAGE });
    expect(screen.getByRole("button", { name: copy.next })).toBeDisabled();
    expect(screen.getByText("Page 2")).toBeInTheDocument();
    list.mockResolvedValueOnce(pageOf([alpha], "page-two"));
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
    expect(list.mock.calls[2][1]?.cursor).toBeNull();
  });

  it("searches the current page by title or address without fetching or changing selection", async () => {
    const { props } = mount();
    await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
    fireEvent.change(screen.getByLabelText("Search this page"), { target: { value: "COL-BETA" } });
    expect(screen.getAllByRole("radio")).toHaveLength(1);
    expect(screen.getByRole("radio", { name: "Beta (col-beta)" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search this page"), { target: { value: "missing" } });
    expect(screen.getByText(/missing/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(1);
    expect(props.onChange).not.toHaveBeenCalled();
  });

  it("uses the existing collection title folding for non-ASCII search", async () => {
    list.mockResolvedValueOnce(pageOf([row("col-equipe", { title: "Équipe" }), alpha]));
    mount();
    await screen.findByRole("radio", { name: "Équipe (col-equipe)" });
    fireEvent.change(screen.getByLabelText(copy.searchLabel), { target: { value: "éQUIPE" } });
    expect(screen.getAllByRole("radio")).toHaveLength(1);
    expect(screen.getByRole("radio", { name: "Équipe (col-equipe)" })).toBeInTheDocument();
  });

  it("shows loading, an error and explicit retry without retrying a mutation", async () => {
    const pending = deferred<CollectionListPage>();
    list.mockReturnValueOnce(pending.promise);
    mount();
    expect(await screen.findByText(copy.loading)).toBeInTheDocument();
    await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
    await act(async () => pending.reject(new Error("denied")));
    expect(await screen.findByRole("alert")).toHaveTextContent(copy.loadError);
    expect(list).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
    expect(list).toHaveBeenCalledTimes(2);
    expect(create).not.toHaveBeenCalled();
  });

  it("distinguishes an empty writable page from a failed read and allows later pages", async () => {
    list.mockResolvedValueOnce(pageOf([row("col-view", { role: "viewer" })], "more"));
    mount();
    expect(await screen.findByText(copy.empty)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: copy.next })).toBeEnabled();
  });

  it("stops a repeated cursor without looping or hiding the current page", async () => {
    list.mockResolvedValueOnce(pageOf([alpha], "repeat")).mockResolvedValueOnce(pageOf([beta], "repeat"));
    mount();
    await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
    fireEvent.click(screen.getByRole("button", { name: copy.next }));
    await screen.findByRole("radio", { name: "Beta (col-beta)" });
    expect(screen.getByRole("alert")).toHaveTextContent(copy.pagingError);
    expect(screen.getByRole("button", { name: copy.next })).toBeDisabled();
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("bounds cursor history using the existing collection page limit", async () => {
    list.mockImplementation(async (_org, options) => {
      const index = Number(options?.cursor ?? 0);
      return pageOf([alpha], String(index + 1));
    });
    mount();
    for (let index = 1; index <= COLLECTIONS_PAGES; index++) {
      await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
      expect(screen.getByText(`Page ${index}`)).toBeInTheDocument();
      if (index < COLLECTIONS_PAGES) fireEvent.click(screen.getByRole("button", { name: copy.next }));
    }
    expect(screen.getByText(copy.pagingLimit)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: copy.next })).toBeDisabled();
    expect(list).toHaveBeenCalledTimes(COLLECTIONS_PAGES);
  });

  it("ignores a late page response after an explicit refresh", async () => {
    const stale = deferred<CollectionListPage>();
    const gamma = row("col-gamma", { title: "Gamma" });
    list
      .mockResolvedValueOnce(pageOf([alpha], "two"))
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(pageOf([gamma]));
    const { props } = mount();
    await chooseAlpha();
    fireEvent.click(screen.getByRole("button", { name: copy.next }));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    const signal = list.mock.calls[1][1]?.signal;
    fireEvent.click(screen.getByRole("button", { name: "Refresh collections" }));
    await screen.findByRole("radio", { name: "Gamma (col-gamma)" });
    await act(async () => stale.resolve(pageOf([beta])));
    expect(signal?.aborted).toBe(true);
    expect(screen.queryByRole("radio", { name: "Beta (col-beta)" })).not.toBeInTheDocument();
    expect(props.onChange).toHaveBeenLastCalledWith(null);
  });

  it.each([
    { org: "other-org" },
    { callerKey: "owner-b:auth-2" },
    { contextKey: "connection-b:selection-2:snapshot" },
    { collectionVersion: 2 },
  ])("aborts an old context read and ignores its late result after %o", async (change) => {
    const stale = deferred<CollectionListPage>();
    list.mockReturnValueOnce(stale.promise).mockResolvedValueOnce(pageOf([beta]));
    const { update, props } = mount();
    await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
    const signal = list.mock.calls[0][1]?.signal;
    update(change);
    await screen.findByRole("radio", { name: "Beta (col-beta)" });
    expect(signal?.aborted).toBe(true);
    await act(async () => stale.resolve(pageOf([alpha])));
    expect(screen.queryByRole("radio", { name: "Alpha (col-alpha)" })).not.toBeInTheDocument();
    expect(props.onInvalidated).toHaveBeenCalledWith("context_changed");
    expect(props.onChange).toHaveBeenCalledWith(null);
    expect(props.onChange).not.toHaveBeenCalledWith(alpha);
  });

  it("clears a chosen destination and its preview on a caller change", async () => {
    const { props, update } = mount();
    await chooseAlpha();
    expect(props.onChange).toHaveBeenLastCalledWith(alpha);
    update({ callerKey: "owner-b:auth-2" });
    await waitFor(() => expect(props.onChange).toHaveBeenLastCalledWith(null));
    expect(await screen.findByRole("radio", { name: "Alpha (col-alpha)" })).not.toBeChecked();
  });

  it.each([{ parent: "col-other" }, { role: "viewer" as const }, { personal: true }])(
    "invalidates a restored destination when fresh metadata changes: %o",
    async (change) => {
      list.mockResolvedValueOnce(pageOf([row(alpha.namespace, { ...alpha, ...change })]));
      const { props } = mount({ value: alpha });
      await waitFor(() => expect(props.onChange).toHaveBeenCalledWith(null));
      expect(props.onInvalidated).toHaveBeenCalledWith("collections_changed");
    },
  );

  it("refreshes and invalidates on collection changes and window focus", async () => {
    const { props } = mount();
    await chooseAlpha();
    act(() => invalidateSharedCollections({ org: "other-org", name: "col-away" }));
    expect(list).toHaveBeenCalledTimes(1);
    act(() => invalidateSharedCollections({ org: "acme", name: "col-alpha" }));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    expect(props.onChange).toHaveBeenLastCalledWith(null);
    fireEvent.focus(window);
    await waitFor(() => expect(list).toHaveBeenCalledTimes(3));
  });

  it("keeps refresh content visible and busy while clearing the previous selection", async () => {
    const pending = deferred<CollectionListPage>();
    const { props } = mount();
    await chooseAlpha();
    list.mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByRole("button", { name: "Refresh collections" }));
    const oldChoice = screen.getByRole("radio", { name: "Alpha (col-alpha)" });
    expect(oldChoice.closest("[aria-busy]")).toHaveAttribute("aria-busy", "true");
    expect(oldChoice).toBeDisabled();
    expect(props.onChange).toHaveBeenLastCalledWith(null);
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    await act(async () => pending.resolve(pageOf([beta])));
    expect(await screen.findByRole("radio", { name: "Beta (col-beta)" })).not.toBeChecked();
    expect(screen.queryByRole("radio", { name: "Alpha (col-alpha)" })).not.toBeInTheDocument();
  });

  it("states parent inheritance without claiming a private destination or a complete ACL", async () => {
    mount();
    const choice = await screen.findByRole("radio", { name: "Beta (col-beta)" });
    expect(choice).toHaveAccessibleDescription(copy.inheritanceHint("col-team"));
    expect(screen.getByText(copy.topLevelHint)).toBeInTheDocument();
    expect(screen.getByText(copy.description)).toBeInTheDocument();
    expect(screen.getByText(copy.selectionHint)).toBeInTheDocument();
    expect(screen.queryByText(/private collection/i)).not.toBeInTheDocument();
  });

  it("reuses real collection creation and reports its exact identity even if catalog refresh fails", async () => {
    create.mockResolvedValue({ namespace: "col-notes-actual", parent: "", created: true, title: "Notes" });
    const { props } = mount();
    await chooseAlpha();
    fireEvent.click(screen.getByRole("button", { name: "New collection" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Notes" } });
    list.mockRejectedValueOnce(new Error("catalog unavailable"));
    fireEvent.click(screen.getByRole("button", { name: "Create", exact: true }));
    await waitFor(() =>
      expect(props.onCollectionCreated).toHaveBeenCalledExactlyOnceWith({
        org: "acme",
        callerKey: "owner-a:auth-1",
        namespace: "col-notes-actual",
      }),
    );
    expect(create).toHaveBeenCalledExactlyOnceWith("acme", "col-notes", {
      parent: "",
      description: "",
      title: "Notes",
      deriveName: true,
    });
    expect(await screen.findByText(copy.created("col-notes-actual"))).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(copy.loadError);
    expect(props.onChange).toHaveBeenLastCalledWith(null);
    expect(props.onChange).not.toHaveBeenCalledWith(expect.objectContaining({ namespace: "col-notes-actual" }));
    expect(deleteCollection).not.toHaveBeenCalled();
  });

  it("keeps a refused creation draft for explicit recovery without automatic retry or deletion", async () => {
    create.mockRejectedValue(new Error("outcome unknown"));
    const { props } = mount();
    await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
    fireEvent.click(screen.getByRole("button", { name: "New collection" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Keep this draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Create", exact: true }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("Title")).toHaveValue("Keep this draft");
    expect(create).toHaveBeenCalledTimes(1);
    expect(props.onCollectionCreated).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel", exact: true }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(deleteCollection).not.toHaveBeenCalled();
  });

  it("passes the selected ordinary parent through the existing creation dialog", async () => {
    create.mockResolvedValue({ namespace: "col-child", parent: beta.namespace, created: true });
    mount();
    const user = userEvent.setup();
    await screen.findByRole("radio", { name: "Beta (col-beta)" });
    await user.click(screen.getByRole("button", { name: "New collection" }));
    await user.type(screen.getByLabelText("Title"), "Child");
    await user.click(screen.getByRole("combobox", { name: "Inside" }));
    await user.click(screen.getByRole("option", { name: "Beta", exact: true }));
    expect(screen.getByText(/A collection inside another inherits its grants/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create", exact: true }));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith("acme", "col-child", expect.objectContaining({ parent: beta.namespace })),
    );
  });

  it("never applies a late creation to a different caller or selects it automatically", async () => {
    const pending = deferred<Awaited<ReturnType<typeof createCollection>>>();
    create.mockReturnValueOnce(pending.promise);
    const { props, update } = mount();
    await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
    fireEvent.click(screen.getByRole("button", { name: "New collection" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Old draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Create", exact: true }));
    update({ callerKey: "owner-b:auth-2" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => pending.resolve({ namespace: "col-old-draft", parent: "", created: true }));
    expect(props.onCollectionCreated).not.toHaveBeenCalled();
    expect(screen.queryByText(copy.created("col-old-draft"))).not.toBeInTheDocument();
    expect(props.onChange).not.toHaveBeenCalledWith(expect.objectContaining({ namespace: "col-old-draft" }));
    expect(deleteCollection).not.toHaveBeenCalled();
  });

  it("cancels creation without a mutation and drains an outstanding list on unmount", async () => {
    const pending = deferred<CollectionListPage>();
    const { unmount } = mount();
    await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
    fireEvent.click(screen.getByRole("button", { name: "New collection" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel", exact: true }));
    list.mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByRole("button", { name: "Refresh collections" }));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    const signal = list.mock.calls[1][1]?.signal;
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(pageOf([alpha])));
    expect(create).not.toHaveBeenCalled();
  });

  it("supports labelled keyboard choice and passes axe", async () => {
    const { container, props } = mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("radio", { name: "Alpha (col-alpha)" }));
    await user.keyboard("{ArrowDown>}");
    await waitFor(() => expect(screen.getByRole("radio", { name: "Beta (col-beta)" })).toBeChecked());
    await user.keyboard("{/ArrowDown}");
    expect(props.onChange).toHaveBeenLastCalledWith(beta);
    expect(screen.getByRole("radiogroup", { name: copy.destinationsLabel })).toBeInTheDocument();
    expect(screen.getByLabelText(copy.searchLabel)).toHaveAccessibleDescription(copy.searchHint);
    expect(await axeViolations(container)).toEqual([]);
  });

  it("does not submit the surrounding setup form when searching with Enter", async () => {
    const submit = vi.fn((event: FormEvent) => event.preventDefault());
    render(
      <form onSubmit={submit}>
        <SourceDestinationStep
          org="acme"
          callerKey="owner-a"
          contextKey="selection-1"
          value={null}
          copy={copy}
          onChange={vi.fn()}
          onInvalidated={vi.fn()}
          onCollectionCreated={vi.fn()}
        />
      </form>,
    );
    await screen.findByRole("radio", { name: "Alpha (col-alpha)" });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(copy.searchLabel), "alpha{Enter}");
    expect(submit).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
  });
});
