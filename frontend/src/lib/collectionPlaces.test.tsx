import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listCollections } from "../api";
import { invalidateShared } from "./sharedReads";
import {
  isOthersPersonal,
  landingWithCollections,
  readsCollections,
  useSharedCollections,
  useSharedCollectionsRead,
  withTwinNames,
} from "./collectionPlaces";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, listCollections: vi.fn() };
});

const page = (title: string) => ({
  collections: [{ namespace: "col-r", parent: "", personal: false, role: "viewer" as const, title }],
  next_cursor: null,
});

describe("useSharedCollections", () => {
  beforeEach(() => invalidateShared());

  it("drops the last org's rows as the org changes, before the new org answers", async () => {
    let answer: (v: ReturnType<typeof page>) => void = () => {};
    vi.mocked(listCollections)
      .mockResolvedValueOnce(page("Acme research"))
      .mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    // Every value the hook returns, each render's own, with the org it rendered for.
    const seen: [string | null, string | undefined][] = [];
    const { result, rerender } = renderHook(
      ({ org }) => {
        const rows = useSharedCollections(org);
        seen.push([org, rows?.[0]?.title]);
        return rows;
      },
      { initialProps: { org: "acme" as string | null } },
    );
    await waitFor(() => expect(result.current?.[0]?.title).toBe("Acme research"));
    rerender({ org: "globex" });
    expect(result.current).toBeNull();
    expect(seen.filter(([org, title]) => org === "globex" && title === "Acme research")).toEqual([]);
    answer(page("Globex research"));
    await waitFor(() => expect(result.current?.[0]?.title).toBe("Globex research"));
    rerender({ org: null });
    expect(result.current).toBeNull();
  });
});

describe("withTwinNames", () => {
  const titles: Record<string, string> = {
    "col-a": "Research",
    "col-b": "research ",
    "col-x": "col-x",
    "col-y": "col-x",
  };
  const label = (ns: string) => titles[ns] ?? ns;

  it("names each of two that look alike, and leaves the rest", () => {
    const name = withTwinNames(label, ["col-a", "col-b", "team"]);
    expect(name("col-a")).toBe("Research (col-a)");
    expect(name("col-b")).toBe("research  (col-b)");
    expect(name("team")).toBe("team");
    expect(withTwinNames(label, ["col-a"])("col-a")).toBe("Research");
  });

  it("does not repeat a name an untitled collection already shows, alone or in a phrase", () => {
    const name = withTwinNames(label, ["col-x", "col-y"]);
    expect(name("col-x")).toBe("col-x");
    expect(name("col-y")).toBe("col-x (col-y)");
    const phrase = withTwinNames((ns) => `the collection ${label(ns)}`, ["col-x", "col-y"]);
    expect(phrase("col-x")).toBe("the collection col-x");
    expect(phrase("col-y")).toBe("the collection col-x (col-y)");
  });

  it("names a collection whose title reads as a namespace beside it", () => {
    const name = withTwinNames((ns) => (ns === "col-t" ? "team" : ns), ["team", "col-t"]);
    expect(name("col-t")).toBe("team (col-t)");
    expect(name("team")).toBe("team");
  });

  it("names one whose title is another's longer name", () => {
    const longer = (ns: string) => (ns === "col-a" ? "col-ab" : ns);
    const name = withTwinNames(longer, ["col-a", "col-ab"]);
    expect(name("col-a")).toBe("col-ab (col-a)");
    expect(name("col-ab")).toBe("col-ab");
    const phrase = withTwinNames((ns) => `the collection ${longer(ns)}`, ["col-a", "col-ab"]);
    expect(phrase("col-a")).toBe("the collection col-ab (col-a)");
    expect(phrase("col-ab")).toBe("the collection col-ab");
  });
});

describe("isOthersPersonal", () => {
  const row = (namespace: string) => ({ namespace, parent: "", personal: false, role: "viewer" as const });

  it("is someone else's only when the viewer's whole list is read and does not hold it", () => {
    expect(isOthersPersonal("col-me-bob", { rows: [row("col-a")], complete: true })).toBe(true);
    expect(
      isOthersPersonal("col-me-op", { rows: [row("col-a"), { ...row("col-me-op"), personal: true }], complete: true }),
    ).toBe(false);
    // Not read yet: whose it is, is not known.
    expect(isOthersPersonal("col-me-bob", null)).toBe(false);
    expect(isOthersPersonal("col-a", { rows: [], complete: true })).toBe(false);
  });

  it("is unknown on a first page with more after it: the viewer's own may be there", async () => {
    invalidateShared();
    vi.mocked(listCollections).mockResolvedValueOnce({ collections: [row("col-a")], next_cursor: "c2" });
    const { result } = renderHook(() => useSharedCollectionsRead("acme"));
    await waitFor(() => expect(result.current).not.toBeNull());
    expect(result.current?.complete).toBe(false);
    expect(isOthersPersonal("col-me-bob", result.current)).toBe(false);
  });
});

describe("readsCollections", () => {
  const entry = (namespace: string, personal = false) => ({ namespace, parent: "", personal, role: "viewer" as const });
  const mineRow = (namespace: string, over: Record<string, unknown> = {}) =>
    ({ namespace, role: "viewer", ...over }) as never;

  it("is true once a collection row or a shared collection is known", () => {
    expect(readsCollections(null, [mineRow("col-r")])).toBe(true);
    expect(readsCollections([entry("col-team")], null)).toBe(true);
  });

  it("is false for none, one's own only, or nothing known yet", () => {
    expect(readsCollections([], [])).toBe(false);
    expect(readsCollections([entry("col-me-a", true)], [mineRow("team")])).toBe(false);
    expect(readsCollections([], [mineRow("col-me-a")])).toBe(false);
    expect(readsCollections(null, null)).toBe(false);
    // Rows the collection gate does not admit: a claim alone, one read through a creator.
    expect(readsCollections([], [mineRow("col-r", { role: "jwt" })])).toBe(false);
    expect(readsCollections([], [mineRow("col-r", { inherited: true })])).toBe(false);
  });
});

describe("landingWithCollections", () => {
  const listed = (namespace: string, personal = false) => ({
    namespace,
    parent: "",
    personal,
    role: "viewer" as const,
  });
  const own = (namespace: string, over: Record<string, unknown> = {}) =>
    ({ namespace, role: "viewer", status: "active", kind: "collection", ...over }) as never;
  beforeEach(() => invalidateShared());

  it("lands on the Library when collection.list names a shared collection, else on Setup", async () => {
    vi.mocked(listCollections).mockResolvedValueOnce({ collections: [listed("col-team")], next_cursor: null });
    expect(await landingWithCollections("acme", [])).toEqual({ view: "knowledge", namespace: null });
    invalidateShared();
    vi.mocked(listCollections).mockResolvedValueOnce({ collections: [listed("col-me-a", true)], next_cursor: null });
    expect(await landingWithCollections("acme", [])).toEqual({ view: "setup", namespace: null });
  });

  it("lets collection.list overrule rows the gate does not admit: a claim, an inherited row", async () => {
    vi.mocked(listCollections).mockResolvedValue({ collections: [], next_cursor: null });
    for (const row of [own("col-x", { role: "jwt" }), own("col-x", { inherited: true })]) {
      invalidateShared();
      expect(await landingWithCollections("acme", [row])).toEqual({ view: "setup", namespace: null });
    }
  });

  it("falls back to a direct grant when collection.list cannot be read", async () => {
    vi.mocked(listCollections).mockRejectedValue(new Error("down"));
    expect(await landingWithCollections("acme", [own("col-team")])).toEqual({ view: "knowledge", namespace: null });
    invalidateShared();
    expect(await landingWithCollections("acme", [own("col-x", { role: "jwt" })])).toEqual({
      view: "setup",
      namespace: null,
    });
  });
});
