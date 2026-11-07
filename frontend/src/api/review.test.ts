import { afterEach, describe, expect, it, vi } from "vitest";
import { collapseMemoryDuplicates, consolidateMemories, listMemoryDuplicates, listProposals } from "./review";

afterEach(() => {
  vi.unstubAllGlobals();
});

const scope = { org: "acme", namespace: "team" };

// The tab's copy names what each call reads, so the call must send it.
describe("review requests", () => {
  it("sends the read sizes the tab's copy quotes", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => Response.json({}));
    vi.stubGlobal("fetch", fetchMock);
    await consolidateMemories(scope, "claude-code");
    await collapseMemoryDuplicates(scope, true);
    await listMemoryDuplicates(scope);
    await listProposals(scope);
    const sent = new Map(
      fetchMock.mock.calls.map(([url, init]) => [url.split("/v1/")[1], JSON.parse(String(init?.body))]),
    );
    expect(sent.get("memory.consolidate")).toMatchObject({
      ...scope,
      agent: "claude-code",
      limit: 200,
      min_age_seconds: 3600,
    });
    expect(sent.get("memory.dedup_collapse")).toMatchObject({ ...scope, dry_run: true, limit: 5000 });
    expect(sent.get("memory.dedup_list")).toMatchObject({ ...scope, limit: 50 });
    expect(sent.get("memory.proposals")).toMatchObject({ ...scope, limit: 100 });
  });
});
