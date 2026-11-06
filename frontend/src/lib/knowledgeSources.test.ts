import { describe, expect, it } from "vitest";
import {
  credentialLabel,
  emptySourceForm,
  folderIds,
  kindLabel,
  databaseIds,
  meetingIds,
  pageIds,
  probeCount,
  projectIds,
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
    expect(syncCounts(sync({ status: "ok", documents_seen: 1 }))).toBe("1 document listed · 0 written");
    expect(
      syncCounts(sync({ status: "ok", documents_seen: 40, changed: 3, removed: 1, unconverted: 2, skipped: 1 })),
    ).toBe("40 documents listed · 3 written · 1 retired · 2 not converted · 1 skipped");
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
      config: { folder_ids: [FOLDER], include: ["docs", "slides", "pdf", "pptx", "docx"] },
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

const PROJECT = "0f8b2c4e-1a2b-4c3d-8e9f-0123456789ab";
const PAGE = "1a2b3c4d5e6f708192a3b4c5d6e7f809";
const DATABASE = "abcdefabcdefabcdefabcdefabcdef01";

describe("the add form's other kinds", () => {
  const named = { ...emptySourceForm(), name: "notes", credential: "k" };

  it("sends a project tracker's project ids, lower-cased", () => {
    const f = { ...named, kind: "linear" as const, projects: `${PROJECT.toUpperCase()}\n${PROJECT}` };
    expect(sourceFormProblems(f)).toEqual([]);
    expect(sourceAddBody("o", "code-x", f, true)).toEqual({
      org: "o",
      namespace: "code-x",
      name: "notes",
      kind: "linear",
      config: { project_ids: [PROJECT] },
      credential: "k",
      dry_run: true,
    });
    // A project id is lower-cased; anything else, such as a pasted link, is named as typed.
    const link = "https://tracker.example/Team/project/Roadmap-3f2a";
    expect(projectIds(`${PROJECT.toUpperCase()} ${link}`)).toEqual([PROJECT, link]);
    expect(sourceFormProblems({ ...f, projects: link })).toEqual([`Not a project id (a UUID): ${link}.`]);
    expect(sourceFormProblems({ ...f, projects: "" })).toEqual(["Add at least one project."]);
    expect(sourceFormProblems({ ...f, projects: "my-project" })).toEqual(["Not a project id (a UUID): my-project."]);
    expect(sourceFormProblems({ ...f, credential: "" })).toEqual(["Paste the read-only API key."]);
  });

  it("sends a page store's page and database ids, from ids with or without dashes or from links", () => {
    const dashed = `${PAGE.slice(0, 8)}-${PAGE.slice(8, 12)}-${PAGE.slice(12, 16)}-${PAGE.slice(16, 20)}-${PAGE.slice(20)}`;
    expect(pageIds(`${dashed}, https://pages.example/team/Design-Notes-${PAGE}`)).toEqual([PAGE]);
    // A link with no title, and one to a block on the page.
    expect(pageIds(`https://pages.example/${PAGE}?pvs=4`)).toEqual([PAGE]);
    expect(pageIds(`https://pages.example/team/Design-Notes-${PAGE}#${DATABASE}`)).toEqual([PAGE]);
    // A database link's view id (`?v=`) is not the database's id; under Databases it
    // gives the database, under Pages it is named as a database link.
    const dbLink = `https://pages.example/team/${DATABASE}?v=${PAGE}`;
    expect(databaseIds(dbLink)).toEqual([DATABASE]);
    expect(pageIds(dbLink)).toEqual([dbLink]);
    expect(sourceFormProblems({ ...named, kind: "notion", pages: dbLink })).toEqual([
      `Paste a database link under Databases: ${dbLink}.`,
    ]);
    // A peek link opens a page of the database: the page is its `p=`.
    const VIEW = "0123456789abcdef0123456789abcdef";
    const peekLink = `https://pages.example/team/${DATABASE}?v=${VIEW}&p=${PAGE}&pm=s`;
    expect(pageIds(peekLink)).toEqual([PAGE]);
    // A row's peek link on its parent page (a `p=`, no `v=`) is a page: under Pages it
    // gives the row, under Databases it is named, never read as the parent page.
    const rowLink = `https://pages.example/team/Parent-${DATABASE}?p=${PAGE}&pm=s`;
    expect(pageIds(rowLink)).toEqual([PAGE]);
    expect(databaseIds(rowLink)).toEqual([rowLink]);
    expect(sourceFormProblems({ ...named, kind: "notion", databases: rowLink })).toEqual([
      `Paste a page link under Pages: ${rowLink}.`,
    ]);
    // A mistyped page in a peek link is named, never read as the database.
    const badPeek = `https://pages.example/team/${DATABASE}?v=${VIEW}&p=${PAGE}0&pm=s`;
    expect(pageIds(badPeek)).toEqual([badPeek]);
    // In the databases field the same link is its database.
    expect(databaseIds(peekLink)).toEqual([DATABASE]);
    expect(sourceAddBody("o", "x", { ...emptySourceForm(), kind: "notion", databases: peekLink }, true).config).toEqual(
      { page_ids: [], database_ids: [DATABASE] },
    );
    // Kept as typed, so the problem names it: an id one digit long is not cut to 32.
    expect(pageIds(`${PAGE}0`)).toEqual([`${PAGE}0`]);
    // A link whose id is mistyped one digit long is not cut to another id.
    expect(pageIds(`https://pages.example/team/Home-${PAGE}0`)).toEqual([`https://pages.example/team/Home-${PAGE}0`]);
    expect(pageIds(`https://pages.example/team/${PAGE}0`)).toEqual([`https://pages.example/team/${PAGE}0`]);
    const f = { ...named, kind: "notion" as const, pages: PAGE, databases: DATABASE.toUpperCase() };
    expect(sourceFormProblems(f)).toEqual([]);
    expect(sourceAddBody("o", "code-x", f, true).config).toEqual({ page_ids: [PAGE], database_ids: [DATABASE] });
    expect(sourceAddBody("o", "code-x", { ...f, databases: "" }, true).config).toEqual({
      page_ids: [PAGE],
      database_ids: [],
    });
    expect(sourceFormProblems({ ...f, pages: "", databases: "" })).toEqual(["Add at least one page or database."]);
    expect(sourceFormProblems({ ...f, pages: "home" })).toEqual(["Not an id (32 hex digits): home."]);
    const many = Array.from({ length: 11 }, (_, i) => i.toString(16).padStart(32, "0"));
    expect(
      sourceFormProblems({
        ...f,
        pages: many.join("\n"),
        databases: many.map((id) => id.replace(/^0/, "f")).join("\n"),
      }),
    ).toEqual(["At most 20 pages and databases in all."]);
  });

  it("sends a meeting store's provider and ids, each provider with its own id rule", () => {
    const meet = {
      ...named,
      kind: "recording" as const,
      provider: "meet" as const,
      meetings: "spaces/abc-DEF_1\nabc-DEF_1",
    };
    expect(sourceFormProblems(meet)).toEqual([]);
    expect(sourceAddBody("o", "code-x", meet, true).config).toEqual({ provider: "meet", ids: ["abc-DEF_1"] });
    const zoom = { ...meet, provider: "zoom" as const, meetings: "81234567890" };
    expect(sourceFormProblems(zoom)).toEqual([]);
    expect(sourceAddBody("o", "code-x", zoom, true).config).toEqual({ provider: "zoom", ids: ["81234567890"] });
    expect(sourceFormProblems({ ...zoom, meetings: "abc-DEF_1" })).toEqual([
      "Not a meeting id (9-12 digits): abc-DEF_1.",
    ]);
    expect(meetingIds("spaces/x", "zoom")).toEqual(["spaces/x"]);
    // A meeting id as it is shown, with spaces; one per line or comma-separated.
    expect(meetingIds("812 3456 7890\n 912 3456 7890 , 81234567890\n\n", "zoom")).toEqual([
      "81234567890",
      "91234567890",
    ]);
    expect(sourceFormProblems({ ...zoom, meetings: "812 3456 7890" })).toEqual([]);
    // A link is no id: it is named, not parsed.
    expect(sourceFormProblems({ ...zoom, meetings: "https://meet.example/j/81234567890" })).toEqual([
      "Not a meeting id (9-12 digits): https://meet.example/j/81234567890.",
    ]);
    expect(meetingIds("a b", "meet")).toEqual(["a", "b"]);
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
    expect(sourceFormProblems({ ...meet, meetings: "" })).toEqual(["Add at least one space id."]);
  });

  it("sends only the chosen kind's config, so switching kinds keeps the other kinds' ids out of the body", () => {
    const f = { ...named, folders: FOLDER, projects: PROJECT };
    expect(sourceAddBody("o", "x", f, true).config).toEqual({
      folder_ids: [FOLDER],
      include: ["docs", "slides", "pdf", "pptx", "docx"],
    });
    expect(sourceAddBody("o", "x", { ...f, kind: "linear" }, true).config).toEqual({ project_ids: [PROJECT] });
    expect(sourceFormFingerprint(f)).not.toBe(sourceFormFingerprint({ ...f, kind: "linear" }));
  });
});

describe("toggleInclude", () => {
  it("keeps the options' order, so unticking and ticking again gives the same list", () => {
    const all = emptySourceForm().include;
    const without = toggleInclude(all, "docs");
    expect(without).toEqual(["slides", "pdf", "pptx", "docx"]);
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
    expect(kindLabel("notion")).toBe("pages");
    expect(kindLabel("recording")).toBe("meeting transcripts");
    expect(kindLabel("other")).toBe("other");
    expect(credentialLabel("linear")).toMatch(/^Read-only API key/);
    expect(credentialLabel("notion")).toMatch(/^Integration token/);
    expect(credentialLabel("recording")).toMatch(/^Read-only credential/);
    expect(credentialLabel("other")).toMatch(/^Credential/);
  });
});
