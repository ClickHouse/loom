import { describe, expect, it } from "vitest";
import {
  credentialLabel,
  emptySourceForm,
  kindLabel,
  folderIds,
  probeCount,
  retiredMessage,
  sourceAddBody,
  sourceFormFingerprint,
  sourceFormProblems,
  syncBadge,
  syncCounts,
  toggleInclude,
  unconvertedDetail,
  type KnowledgeSourceSync,
} from "./knowledgeSources";

function sync(over: Partial<KnowledgeSourceSync> = {}): KnowledgeSourceSync {
  return {
    requested_at: "",
    last_synced_at: "",
    status: "",
    documents_seen: 0,
    changed: 0,
    removed: 0,
    unconverted: 0,
    unconverted_reasons: {},
    skipped: 0,
    error: "",
    ...over,
  };
}

const FOLDER = "1AbCdEfGhIjKlMnOp";

describe("syncBadge", () => {
  it("names each state with its tone", () => {
    expect(syncBadge(sync())).toMatchObject({ text: "Never synced", tone: "muted" });
    expect(syncBadge(sync({ status: "ok" }))).toMatchObject({ text: "Synced", tone: "ok" });
    expect(syncBadge(sync({ status: "rate_limited" }))).toMatchObject({ tone: "warn" });
    expect(syncBadge(sync({ status: "credential_invalid" }))).toMatchObject({ tone: "danger" });
    expect(syncBadge(sync({ status: "failed", error: "http_403" })).tip).toContain("http_403");
  });

  it("reads an ok turn with an error as incomplete, and an unknown status as itself", () => {
    expect(syncBadge(sync({ status: "ok", error: "pass bound" }))).toMatchObject({ text: "Incomplete", tone: "warn" });
    expect(syncBadge(sync({ status: "no_transcript" }))).toMatchObject({ text: "no_transcript", tone: "warn" });
  });
});

describe("syncCounts", () => {
  it("says nothing before a first turn, then only the counts that are not zero past the first two", () => {
    expect(syncCounts(sync())).toBe("");
    expect(syncCounts(sync({ status: "ok", documents_seen: 1 }))).toBe("1 file listed · 0 written");
    expect(
      syncCounts(sync({ status: "ok", documents_seen: 40, changed: 3, removed: 1, unconverted: 2, skipped: 1 })),
    ).toBe("40 files listed · 3 written · 1 retired · 2 not converted · 1 skipped");
  });

  it("explains the unconverted reasons, most first, keeping unknown ones as written", () => {
    expect(unconvertedDetail({ too_large: 1, no_extractor: 2, http_404: 1 })).toBe(
      "no extractor for the format: 2; http_404: 1; over the size limit: 1",
    );
  });
});

describe("the add form", () => {
  it("takes folder ids or links, one per line or comma-separated, without duplicates", () => {
    expect(folderIds(`${FOLDER}\nhttps://example.com/drive/folders/${FOLDER}?usp=sharing, 2ZyXwVuTsRqPoNm`)).toEqual([
      FOLDER,
      "2ZyXwVuTsRqPoNm",
    ]);
  });

  it("lists every problem until the form can be checked", () => {
    expect(sourceFormProblems(emptySourceForm())).toHaveLength(3);
    const ok = { ...emptySourceForm(), name: " Design-Docs ", folders: FOLDER, credential: "{}" };
    expect(sourceFormProblems(ok)).toEqual([]);
    expect(sourceFormProblems({ ...ok, folders: "short" })).toEqual(["Not a folder id: short."]);
    expect(sourceFormProblems({ ...ok, include: [] })).toEqual(["Pick at least one file kind."]);
  });

  it("sends the normalised name and the config, and fingerprints what the check read", () => {
    const f = { ...emptySourceForm(), name: " Design-Docs ", folders: FOLDER, credential: " {} " };
    expect(sourceAddBody("o", "code-x", f, true)).toEqual({
      org: "o",
      namespace: "code-x",
      name: "design-docs",
      kind: "gdrive",
      config: { folder_ids: [FOLDER], include: ["docs", "slides", "pdf", "pptx"] },
      credential: "{}",
      dry_run: true,
    });
    expect(sourceFormFingerprint(f)).toBe(sourceFormFingerprint({ ...f, name: "design-docs" }));
    expect(sourceFormFingerprint(f)).not.toBe(sourceFormFingerprint({ ...f, credential: "{ }" }));
  });

  it("counts a probe, a lower bound when there are more", () => {
    expect(probeCount({ documents: 1, more: false, sample: [] })).toBe("1 document");
    expect(probeCount({ documents: 1000, more: true, sample: [] })).toBe("1000+ documents");
  });
});

describe("toggleInclude", () => {
  it("keeps the options' order, so unticking and ticking again gives the same list", () => {
    const all = emptySourceForm().include;
    const without = toggleInclude(all, "docs");
    expect(without).toEqual(["slides", "pdf", "pptx"]);
    expect(toggleInclude(without, "docs")).toEqual(all);
    expect(toggleInclude(["pptx"], "pdf")).toEqual(["pdf", "pptx"]);
  });
});

describe("retiredMessage", () => {
  it("says whether the documents stay, how many go, and that they wait while sync is off", () => {
    expect(retiredMessage("d", false, 5, true)).toBe("Retired d; its documents stay");
    expect(retiredMessage("d", true, 0, true)).toBe("Retired d; it has no documents left to retire");
    expect(retiredMessage("d", true, 1, true)).toBe("Retired d; its 1 document is retired over the next sync passes");
    expect(retiredMessage("d", true, 3, false)).toBe(
      "Retired d; its 3 documents are retired once sync is on for this deployment",
    );
  });
});

describe("kind words", () => {
  it("names each served kind and its credential, and an unknown one by its wire name", () => {
    expect(kindLabel("gdrive")).toBe("shared folder");
    expect(kindLabel("linear")).toBe("project documents");
    expect(kindLabel("other")).toBe("other");
    expect(credentialLabel("linear")).toMatch(/^Read-only API key/);
    expect(credentialLabel("other")).toMatch(/^Credential/);
  });
});
