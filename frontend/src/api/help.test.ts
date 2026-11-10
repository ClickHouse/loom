import { afterEach, describe, expect, it, vi } from "vitest";
import { helpPage } from "./help";
import { navigationHref, useStore } from "../lib/state";

const read = (text: string, truncated: boolean, next_offset: number) => ({
  available: true,
  found: true,
  path: "architecture/assistant.md",
  title: "Assistant design",
  section: "",
  anchor: "",
  audience: "operator",
  text,
  truncated,
  next_offset,
  sections: [{ section: "Assistant design", anchor: "assistant-design", heading: "Assistant design" }],
});

describe("helpPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  it("reads a page cut at the server's limit to its end", async () => {
    const bodies: unknown[] = [];
    const replies = [read("first ", true, 6), read("second ", true, 13), read("end", false, 0)];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        bodies.push(JSON.parse(String(init?.body)));
        return new Response(JSON.stringify(replies.shift()), { status: 200 });
      }),
    );
    const page = await helpPage("architecture/assistant.md");
    expect(page.text).toBe("first second end");
    expect(page.truncated).toBe(false);
    expect(page.sections).toHaveLength(1);
    expect(bodies).toEqual([
      expect.objectContaining({ path: "architecture/assistant.md" }),
      expect.objectContaining({ path: "architecture/assistant.md", offset: 6 }),
      expect.objectContaining({ path: "architecture/assistant.md", offset: 13 }),
    ]);
  });

  it("keeps a deep link's page and anchor through boot", () => {
    window.history.replaceState(null, "", "/help?page=guides%2Ftokens-and-grants.md#revoke-and-rotate");
    useStore.getState().bootView("help");
    expect(new URLSearchParams(window.location.search).get("page")).toBe("guides/tokens-and-grants.md");
    expect(window.location.hash).toBe("#revoke-and-rotate");
    expect(navigationHref("help")).toContain("page=guides%2Ftokens-and-grants.md");
  });

  it("gives a move to another section of the open page its own history entry", () => {
    useStore.getState().openDocsPage("guides/tokens-and-grants.md", "mint-a-token");
    const before = window.history.length;
    useStore.getState().openDocsPage("guides/tokens-and-grants.md", "revoke-and-rotate");
    expect(window.history.length).toBe(before + 1);
    expect(window.location.hash).toBe("#revoke-and-rotate");
  });

  it("keeps the page in the Help view's address, and the section as its anchor", () => {
    expect(navigationHref("help", { page: "guides/tokens-and-grants.md" })).toContain(
      "page=guides%2Ftokens-and-grants.md",
    );
    useStore.getState().openDocsPage("guides/tokens-and-grants.md", "revoke-and-rotate");
    expect(useStore.getState().view).toBe("help");
    expect(new URLSearchParams(window.location.search).get("page")).toBe("guides/tokens-and-grants.md");
    expect(window.location.hash).toBe("#revoke-and-rotate");
  });
});
