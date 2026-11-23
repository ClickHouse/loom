import { describe, expect, it } from "vitest";
import {
  credentialLabel,
  emptySourceForm,
  kindLabel,
  meetingIds,
  probeCount,
  retiredMessage,
  sourceAddBody,
  sourceFormFingerprint,
  sourceFormProblems,
  syncBadge,
  syncCounts,
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

describe("syncBadge", () => {
  it("names each state with its tone", () => {
    expect(syncBadge(sync())).toMatchObject({ text: "Never synced", tone: "muted" });
    expect(syncBadge(sync({ status: "ok" }))).toMatchObject({ text: "Synced", tone: "ok" });
    expect(syncBadge(sync({ status: "rate_limited" }))).toMatchObject({ tone: "warn" });
    expect(syncBadge(sync({ status: "credential_invalid" }))).toMatchObject({ tone: "danger" });
    expect(syncBadge(sync({ status: "failed", error: "http_403" })).tip).toContain("http_403");
  });

  it("describes retired providers without offering replaced source setup", () => {
    expect(syncBadge(sync({ status: "provider_removed" }))).toEqual({
      text: "Provider unavailable",
      tone: "warn",
      tip: "This provider is unavailable. Retained bytes and history do not grant current access.",
    });
  });

  it("reads an ok turn cut short as incomplete, one with another note as synced with a note", () => {
    const cut = "pass bound reached";
    expect(syncBadge(sync({ status: "ok", error: cut }))).toMatchObject({ text: "Incomplete", tone: "warn" });
    expect(syncBadge(sync({ status: "ok", error: "listing: incomplete search" })).text).toBe("Incomplete");
    expect(syncBadge(sync({ status: "ok", error: "listing: an item listed nothing; item 1: http_404" })).text).toBe(
      "Incomplete",
    );
    expect(syncBadge(sync({ status: "ok", error: "item 2: http_404" }))).toMatchObject({
      text: "Synced with a note",
      tone: "warn",
    });
    expect(syncBadge(sync({ status: "no_transcript" }))).toMatchObject({ text: "no_transcript", tone: "warn" });
  });
});

describe("syncCounts", () => {
  it("says nothing before a first turn, then only the counts that are not zero past the first two", () => {
    expect(syncCounts(sync())).toBe("");
    expect(syncCounts(sync({ status: "ok", documents_seen: 1 }))).toBe("1 document listed · 0 written");
    expect(
      syncCounts(sync({ status: "ok", documents_seen: 40, changed: 3, removed: 1, unconverted: 2, skipped: 1 })),
    ).toBe("40 documents listed · 3 written · 1 retired · 2 not converted · 1 skipped");
  });

  it("explains the unconverted reasons, most first, keeping unknown ones as written", () => {
    expect(unconvertedDetail({ too_large: 1, no_extractor: 2, http_404: 1 })).toBe(
      "file type not readable: 2; http_404: 1; over the size limit: 1",
    );
  });
});

describe("recording setup", () => {
  const named = { ...emptySourceForm(), name: "notes", credential: "k" };
  it("validates a name, meeting selection and credential before checking", () => {
    expect(sourceFormProblems(emptySourceForm())).toHaveLength(3);
    expect(sourceFormProblems({ ...named, meetings: "81234567890" })).toEqual([]);
    expect(sourceAddBody("o", "docs", { ...named, meetings: "81234567890" }, false)).toEqual({
      org: "o", namespace: "docs", name: "notes", kind: "recording",
      config: { provider: "zoom", ids: ["81234567890"] }, credential: "k", dry_run: false,
    });
    expect(sourceFormFingerprint({ ...named, meetings: "81234567890" })).not.toBe(sourceFormFingerprint({ ...named, meetings: "91234567890" }));
    expect(probeCount({ documents: 1000, more: true, sample: [] })).toBe("1000+ documents");
  });
  it("sends Zoom meeting ids and rejects space ids and meeting links", () => {
    const zoom = { ...named, kind: "recording" as const, meetings: "81234567890" };
    expect(sourceFormProblems(zoom)).toEqual([]);
    expect(sourceAddBody("o", "code-x", zoom, true).config).toEqual({ provider: "zoom", ids: ["81234567890"] });
    expect(sourceFormProblems({ ...zoom, meetings: "abc-DEF_1" })).toEqual([
      "Not a meeting id (9-12 digits): abc-DEF_1.",
    ]);
    expect(meetingIds("spaces/x")).toEqual(["spaces/x"]);
    expect(sourceFormProblems({ ...zoom, meetings: "spaces/81234567890" })).toEqual([
      "Not a meeting id (9-12 digits): spaces/81234567890.",
    ]);
    // A meeting id as it is shown, with spaces; one per line or comma-separated.
    expect(meetingIds("812 3456 7890\n 912 3456 7890 , 81234567890\n\n")).toEqual(["81234567890", "91234567890"]);
    expect(sourceFormProblems({ ...zoom, meetings: "812 3456 7890" })).toEqual([]);
    // A link is no id: it is named, not parsed.
    expect(sourceFormProblems({ ...zoom, meetings: "https://meet.example/j/81234567890" })).toEqual([
      "Not a meeting id (9-12 digits): https://meet.example/j/81234567890.",
    ]);
    expect(meetingIds("a b")).toEqual(["a b"]);
    // Two ids on one line are named as typed, not joined into one long number.
    expect(sourceFormProblems({ ...zoom, meetings: "812 3456 7890 912 3456 7890" })).toEqual([
      "Put one meeting id per line, or comma-separated: 812 3456 7890 912 3456 7890.",
    ]);
    // One spaced id a digit too long is not two ids.
    expect(sourceFormProblems({ ...zoom, meetings: "812 3456 789012" })).toEqual([
      "Not a meeting id (9-12 digits): 812 3456 789012.",
    ]);
    // Anything else is named as typed, spaces and all.
    expect(sourceFormProblems({ ...zoom, meetings: "Meeting ID: 812 3456 7890" })).toEqual([
      "Not a meeting id (9-12 digits): Meeting ID: 812 3456 7890.",
    ]);
    expect(sourceFormProblems({ ...zoom, meetings: "8123 4567" })).toEqual([
      "Not a meeting id (9-12 digits): 81234567.",
    ]);
    expect(sourceFormProblems({ ...zoom, meetings: "" })).toEqual(["Add at least one meeting id."]);
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
  it("names recording and renders unsupported metadata without a runtime claim", () => {
    expect(kindLabel("recording")).toBe("meeting transcripts");
    for (const kind of ["gdrive", "linear", "notion", "other"]) expect(kindLabel(kind)).toBe(`Unsupported source (${kind})`);
    expect(credentialLabel("recording")).toMatch(/^Read-only credential/);
  });
});
