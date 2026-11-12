/**
 * A folder into the Library: which folders become collections and which are already
 * there, how deep they may nest, how a dropped folder is read, and that each folder's
 * collection is made once, parents first, before its files go; Stop makes no more.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  createCollection: vi.fn(),
  uploadKnowledgeFile: vi.fn(),
  ingestKnowledge: vi.fn(),
  knowledgeIngestStatus: vi.fn(),
}));
import {
  type CollectionEntry,
  createCollection,
  ingestKnowledge,
  type KnowledgeIngestStatus,
  knowledgeIngestStatus,
  uploadKnowledgeFile,
} from "../api";
import { type BatchRow, type KeptFields, MAX_BATCH_FILES, planBatch, REPEAT, runBatch } from "./batchIngest";
import { ApiError } from "./errors";
import { ingestClock } from "./fileIngest";
import {
  collectionsMade,
  FolderCollections,
  FolderTooLarge,
  folderOf,
  MAX_READ,
  pickedFolder,
  afterPlacing,
  NOT_ASKED,
  placeFolders,
  skipsLine,
  titleProblem,
  lineageLength,
  planFolders,
  readFolders,
  takeDropped,
  tooDeep,
} from "./folderUpload";

const entry = (namespace: string, over: Partial<CollectionEntry> = {}): CollectionEntry => ({
  namespace,
  parent: "",
  personal: false,
  role: "writer",
  ...over,
});
const realSleep = ingestClock.sleep;

describe("planFolders", () => {
  it("makes a collection of every folder that holds a file to send, parents first, and finds the ones there", () => {
    const team = entry("col-team", { title: "Team" });
    const rows = [
      team,
      entry("col-team-q3", { parent: "col-team", title: "q3" }),
      entry("col-drafts", { parent: "col-team-q3" }), // untitled: its name is the one "Drafts" spells
    ];
    const plans = planFolders(["Q3/Drafts/a.md", "Q3/Drafts/b.md", "Q3/Interviews/x/c.md"], rows, team);
    expect(plans.map((p) => [p.path, p.parentPath, p.existing, p.files])).toEqual([
      ["Q3", "", "col-team-q3", 0],
      ["Q3/Drafts", "Q3", "col-drafts", 2],
      ["Q3/Interviews", "Q3", "", 0],
      ["Q3/Interviews/x", "Q3/Interviews", "", 1],
    ]);
  });

  it("predicts an untitled collection for one of two folders whose names spell it, not both", () => {
    const team = entry("col-team", { title: "Team" });
    const rows = [team, entry("col-cafe", { parent: "col-team", role: "admin" })]; // untitled, the person its admin
    const plans = planFolders(["Cafe/a.md", "Café/b.md"], rows, team);
    expect(plans.map((p) => p.existing).sort()).toEqual(["", "col-cafe"]);
  });

  it("predicts an untitled collection for every folder that spells it when the person is not its admin", () => {
    const team = entry("col-team", { title: "Team" });
    const rows = [team, entry("col-foo-bar", { parent: "col-team", role: "writer" })]; // untitled
    const plans = planFolders(["Foo Bar/a.md", "foo_bar/b.md"], rows, team);
    expect(plans.map((p) => p.existing)).toEqual(["col-foo-bar", "col-foo-bar"]);
  });

  it("predicts the same collection for one title under two folders that land on one parent", () => {
    const team = entry("col-team", { title: "Team" });
    const rows = [
      team,
      entry("col-docs", { parent: "col-team" }), // untitled, the person a writer: both folders land on it
      entry("col-docs-x", { parent: "col-docs", title: "x" }),
    ];
    const plans = planFolders(["Docs/x/a.md", "Docs!/x/b.md"], rows, team);
    expect(plans.map((p) => [p.path, p.existing])).toEqual([
      ["Docs", "col-docs"],
      ["Docs!", "col-docs"],
      ["Docs/x", "col-docs-x"],
      ["Docs!/x", "col-docs-x"],
    ]);
  });

  it("gives an untitled collection the person is admin of to the sibling whose file goes first", () => {
    const team = entry("col-team", { title: "Team" });
    const rows = [team, entry("col-foo-bar", { parent: "col-team", role: "admin" })];
    const plans = planFolders(["foo-bar/a.md", "Foo Bar/b.md"], rows, team);
    expect(plans.map((p) => [p.path, p.existing])).toEqual([
      ["foo-bar", "col-foo-bar"],
      ["Foo Bar", ""],
    ]);
  });

  it("reuses the first collection by name that the title names, as the server does", () => {
    const team = entry("col-team", { title: "Team" });
    const rows = [
      team,
      entry("col-docs-ab12", { parent: "col-team", title: "Docs" }),
      entry("col-docs", { parent: "col-team" }), // untitled, and first by name
    ];
    expect(planFolders(["Docs/a.md"], rows, team).map((p) => p.existing)).toEqual(["col-docs"]);
  });

  it("at the top level, finds a folder's collection among the ones the person is admin of", () => {
    const rows = [
      entry("col-q3", { title: "Q3", role: "admin" }),
      entry("col-shared-q3", { title: "Q3", role: "writer" }), // someone else's: not reused
    ];
    expect(planFolders(["Q3/a.md"], rows, null).map((p) => p.existing)).toEqual(["col-q3"]);
    expect(planFolders(["Q3/a.md"], rows.slice(1), null).map((p) => p.existing)).toEqual([""]);
  });

  it("takes sibling folders whose titles match as one collection, made once", async () => {
    const team = entry("col-team", { title: "Team" });
    const plans = planFolders(["A/x.md", "a/y.md", "a/Sub/z.md", "A/sub/w.md"], [team], team);
    // The one whose file goes first stands for the others, as the server titles it.
    expect(collectionsMade(plans).map((p) => [p.path, p.files])).toEqual([
      ["A", 2],
      ["a/Sub", 2],
    ]);
    let made = 0;
    vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => {
      made++;
      return {
        namespace: `col-${(o?.title ?? "").toLowerCase()}`,
        parent: o?.parent ?? "",
        created: true,
        title: o?.title,
      };
    });
    const folders = new FolderCollections("o", new Map(plans.map((p) => [p.path, p])), "col-team", (s) => s());
    const signal = new AbortController().signal;
    const [a, b] = await Promise.all([folders.target("A", signal), folders.target("a", signal)]);
    expect(a).toEqual(b);
    expect(a.fresh).toBe(true);
    await folders.target("A/sub", signal);
    expect(made).toBe(2);
    vi.mocked(createCollection).mockReset();
  });

  it("says which folders would nest deeper than collections may", () => {
    const rows = Array.from({ length: 8 }, (_, i) => entry(`col-${i}`, { parent: i ? `col-${i - 1}` : "" }));
    const shown = rows[7]; // eight deep, itself included: a folder in it has 8 ancestors
    expect(lineageLength(shown, rows)).toBe(8);
    const plans = planFolders(["A/B/c.md", "A/d.md"], rows, shown);
    expect(tooDeep(plans, 8)).toEqual(["A/B"]);
    expect(tooDeep(plans, 7)).toEqual([]);
    expect(folderOf("A/B/c.md")).toBe("A/B");
  });
});

describe("reading a dropped folder", () => {
  it("takes the entries while the drop lasts and reads every batch, leaving hidden folders out", async () => {
    let live = true;
    const file = (name: string) => ({
      isFile: true,
      isDirectory: false,
      name,
      file: (ok: (f: File) => void) => ok(new File(["x"], name)),
    });
    const dir = (name: string, batches: unknown[][]) => ({
      isFile: false,
      isDirectory: true,
      name,
      createReader: () => {
        const left = [...batches, []];
        return { readEntries: (ok: (es: unknown[]) => void) => ok(left.shift() ?? []) };
      },
    });
    const root = dir("Q3", [[file("a.md"), dir(".git", [[file("config")]])], [file("b.md")]]);
    const items = [
      {
        kind: "file",
        webkitGetAsEntry: () => {
          if (!live) throw new Error("read after the drop");
          return root;
        },
        getAsFile: () => null,
      },
      { kind: "file", webkitGetAsEntry: () => null, getAsFile: () => new File(["y"], "loose.md") },
    ] as unknown as DataTransferItem[];
    const { entries, files } = takeDropped(items);
    live = false;
    expect(files.map((f) => f.name)).toEqual(["loose.md"]);
    const { picked, skips } = await readFolders(entries as never);
    expect(picked.map((p) => p.path)).toEqual(["Q3/a.md", "Q3/b.md"]);
    expect([...skips.hidden.keys()]).toEqual(["Q3/.git"]);
  });

  it("leaves a picked folder's hidden folders out, counted in one line each", () => {
    const at = (path: string) => {
      const f = new File(["x"], path.split("/").pop() ?? path);
      Object.defineProperty(f, "webkitRelativePath", { value: path });
      return f;
    };
    const { picked, skips } = pickedFolder([
      at("app/a.md"),
      at("app/.git/HEAD"),
      at("app/.git/objects/x"),
      at("app/b.md"),
    ]);
    expect(picked.map((p) => p.path)).toEqual(["app/a.md", "app/b.md"]);
    expect(skipsLine(skips)).toBe("Hidden folders not read: app/.git (2 files).");
  });

  it("refuses a picked folder with more files than it reads", () => {
    const files = Array.from({ length: MAX_READ + 1 }, (_, i) => {
      const f = new File(["x"], `f${i}.md`);
      Object.defineProperty(f, "webkitRelativePath", { value: `big/f${i}.md` });
      return f;
    });
    expect(() => pickedFolder(files)).toThrow(FolderTooLarge);
  });

  it("stops reading once stopped, reading no more of the folder", async () => {
    const ctrl = new AbortController();
    let opened = 0;
    const file = (name: string) => ({
      isFile: true,
      isDirectory: false,
      name,
      file: (ok: (f: File) => void) => {
        opened++;
        ctrl.abort(); // the dialog closes as the first file is read
        ok(new File(["x"], name));
      },
    });
    const folder = {
      isFile: false,
      isDirectory: true,
      name: "big",
      createReader: () => {
        const left = [[file("a.md"), file("b.md"), file("c.md")], []];
        return { readEntries: (ok: (es: unknown[]) => void) => ok(left.shift() ?? []) };
      },
    };
    await expect(readFolders([folder] as never, undefined, ctrl.signal)).rejects.toMatchObject({ name: "AbortError" });
    expect(opened).toBe(1);
  });

  it("stops reading a folder too large to read here", async () => {
    const many = Array.from({ length: MAX_READ + 1 }, (_, i) => ({
      isFile: true,
      isDirectory: false,
      name: `f${i}.md`,
      file: (ok: (f: File) => void) => ok(new File(["x"], `f${i}.md`)),
    }));
    const big = {
      isFile: false,
      isDirectory: true,
      name: "big",
      createReader: () => {
        const left = [many, []];
        return { readEntries: (ok: (es: unknown[]) => void) => ok(left.shift() ?? []) };
      },
    };
    await expect(readFolders([big] as never)).rejects.toBeInstanceOf(FolderTooLarge);
  });

  it("flags a folder name the server refuses as a title", () => {
    expect(titleProblem("x".repeat(201))).toBe("longer than 200 characters");
    expect(titleProblem("a\u200eb")).toBe("holds an invisible character");
    expect(titleProblem("🧑\u200d💻 Dev")).toBe("");
    expect(titleProblem("   ")).toBe("has no name but spaces");
    // Stripped as the server strips: a byte-order mark stays and is refused, a separator goes.
    expect(titleProblem("\ufeffDocs")).toBe("holds an invisible character");
    expect(titleProblem("\x1cDocs")).toBe("");
  });
});

describe("folder collections", () => {
  beforeEach(() => {
    ingestClock.sleep = async () => {};
    vi.mocked(uploadKnowledgeFile).mockImplementation(async (_id, f) => ({
      upload_id: f.name,
      sha256: "",
      size: f.size,
      filename: f.name,
      format: "markdown",
      expires_at: "",
    }));
    vi.mocked(ingestKnowledge).mockImplementation(async (_id, o) => ({
      ingestion_id: o.source_uri,
      state: "pending",
      source_uri: o.source_uri,
    }));
    vi.mocked(knowledgeIngestStatus).mockResolvedValue({
      found: true,
      state: "ready",
      revision: 1,
    } as KnowledgeIngestStatus);
  });
  afterEach(() => {
    vi.resetAllMocks();
    ingestClock.sleep = realSleep;
  });

  const FORMATS = {
    formats: [{ format: "markdown", extensions: ["md"], media_types: [], available: true }],
    max_upload_bytes: 1_000_000,
    refused_extensions: [],
    default_format: "text",
  };
  const pick = (paths: string[]) =>
    planBatch(
      paths.map((p) => ({ file: new File(["x"], p.split("/").pop() ?? p), path: p })),
      FORMATS,
      () => ({ org: "o", namespace: "col-team" }),
      (e) => ({ folder: folderOf(e.path), sourceUri: e.file.name }),
    );
  // As the dialog sends: the folders' collections made first, then the files.
  const run = async (
    rows: BatchRow[],
    folders: FolderCollections,
    signal = new AbortController().signal,
    holds = async (_ns: string) => new Map<string, KeptFields>(),
  ) => {
    const placed = (await placeFolders(rows, folders, holds, (f) => f, signal)).map((p) => p.row);
    const seen = new Map(placed.map((r) => [r.id, r]));
    await runBatch(placed, { fields: { tags: [], allow_partial: false }, signal, onRow: (r) => seen.set(r.id, r) });
    return [...seen.values()];
  };
  const collections = (paths: string[]) => {
    const team = entry("col-team", { title: "Team" });
    const plans = planFolders(paths, [team], team);
    return new FolderCollections("o", new Map(plans.map((p) => [p.path, p])), "col-team", (step) => step());
  };

  it("makes each folder's collection once, parents first, and sends its files into it", async () => {
    vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => ({
      namespace: `col-${(o?.title ?? "").toLowerCase()}`,
      parent: o?.parent ?? "",
      created: true,
      title: o?.title,
    }));
    const paths = ["Q3/Drafts/a.md", "Q3/Drafts/b.md", "Q3/c.md"];
    const folders = collections(paths);
    const rows = await run(pick(paths), folders);
    expect(vi.mocked(createCollection).mock.calls.map(([, , o]) => [o?.title, o?.parent, o?.deriveName])).toEqual([
      ["Q3", "col-team", true],
      ["Drafts", "col-q3", true],
    ]);
    expect(rows.map((r) => [r.key, r.target.namespace, r.state])).toEqual([
      ["Q3/Drafts/a.md", "col-drafts", "done"],
      ["Q3/Drafts/b.md", "col-drafts", "done"],
      ["Q3/c.md", "col-q3", "done"],
    ]);
    expect(folders.created).toEqual(["col-q3", "col-drafts"]);
  });

  it("fails the files of a folder whose collection could not be made, and waits out a parent other replicas have not seen", async () => {
    let refusedOnce = false;
    vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => {
      const title = o?.title ?? "";
      if (title === "Drafts" && !refusedOnce) {
        refusedOnce = true; // the new parent is not known yet on the replica that answers
        throw new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "cannot add" });
      }
      if (title === "Deep")
        throw new ApiError({ status: 422, code: "VALIDATION_FAILED", message: "collections nest at most 8 deep" });
      return { namespace: `col-${title.toLowerCase()}`, parent: o?.parent ?? "", created: true, title };
    });
    const paths = ["Q3/Drafts/a.md", "Q3/Deep/b.md"];
    const rows = await run(pick(paths), collections(paths));
    expect(rows[0]).toMatchObject({ state: "done", target: { namespace: "col-drafts" } });
    expect(rows[1].state).toBe("failed");
    expect(rows[1].line).toMatch(/^Not added: the collection Deep couldn't be created: .*nest at most 8 deep/);
  });

  it("asks once for a folder's collection that is refused, failing all its files", async () => {
    vi.mocked(createCollection).mockRejectedValue(
      new ApiError({ status: 422, code: "VALIDATION_FAILED", message: "collections nest at most 8 deep" }),
    );
    const paths = ["Q3/a.md", "Q3/b.md", "Q3/c.md", "Q3/d.md", "Q3/e.md"];
    const rows = await run(pick(paths), collections(paths));
    expect(createCollection).toHaveBeenCalledTimes(1);
    expect(rows.every((r) => r.state === "failed")).toBe(true);
  });

  it("marks a file already in the collection its folder went to, and none in one made new", async () => {
    vi.mocked(createCollection).mockResolvedValueOnce({
      namespace: "col-q3",
      parent: "col-team",
      created: false,
      title: "Q3",
    });
    const holds = vi.fn(async () => new Map([["a.md", { title: "A", description: "", tags: ["t"] }]]));
    const paths = ["Q3/a.md", "Q3/b.md"];
    const placed = await placeFolders(pick(paths), collections(paths), holds, (f) => f, new AbortController().signal);
    expect(placed.map((p) => [p.row.key, p.row.target.namespace, p.row.replaces, p.row.kept?.title])).toEqual([
      ["Q3/a.md", "col-q3", true, "A"],
      ["Q3/b.md", "col-q3", false, undefined],
    ]);
    vi.mocked(createCollection).mockResolvedValueOnce({
      namespace: "col-q4",
      parent: "col-team",
      created: true,
      title: "Q4",
    });
    holds.mockClear();
    const fresh = await placeFolders(
      pick(["Q4/a.md"]),
      collections(["Q4/a.md"]),
      holds,
      (f) => f,
      new AbortController().signal,
    );
    expect(fresh.map((p) => [p.row.fresh, p.row.replaces])).toEqual([[true, false]]);
    expect(holds).not.toHaveBeenCalled();
  });

  it("takes a collection this upload made as new for a second folder the server answers with it", async () => {
    // Docs and Docs! land on one untitled collection; each holds "new", which the server makes once.
    const team = entry("col-team", { title: "Team" });
    const rows = [team, entry("col-docs", { parent: "col-team" })];
    const paths = ["Docs/new/a.md", "Docs!/new/b.md"];
    const plans = planFolders(paths, rows, team);
    expect(
      collectionsMade(plans)
        .filter((p) => !p.existing)
        .map((p) => p.path),
    ).toEqual(["Docs/new"]);
    vi.mocked(createCollection).mockImplementation(async (_org, _ns, o) =>
      o?.title === "new"
        ? {
            namespace: "col-docs-new",
            parent: "col-docs",
            created: vi.mocked(createCollection).mock.calls.filter(([, , c]) => c?.title === "new").length === 1,
            title: "new",
          }
        : { namespace: "col-docs", parent: "col-team", created: false, title: "" },
    );
    const folders = new FolderCollections("o", new Map(plans.map((p) => [p.path, p])), "col-team", (step) => step());
    const holds = vi.fn(async () => new Map<string, KeptFields>());
    const placed = await placeFolders(pick(paths), folders, holds, (f) => f, new AbortController().signal);
    expect(placed.map((p) => [p.row.target.namespace, p.row.fresh])).toEqual([
      ["col-docs-new", true],
      ["col-docs-new", true],
    ]);
    expect(holds).not.toHaveBeenCalled();
  });

  it("names the folder refused when a folder above a file's could not be made", async () => {
    vi.mocked(createCollection).mockRejectedValue(
      new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "cannot add to col-team" }),
    );
    const paths = ["Q3/Interviews/a.md"];
    const [row] = await run(pick(paths), collections(paths));
    expect(row.line).toMatch(/^Not added: the collection Q3 couldn't be created: /);
  });

  it("takes folders whose names differ only in case as one place", () => {
    const rows = pick(["Docs/a.md", "docs/a.md", "docs/b.md"]);
    expect(rows.map((r) => r.state)).toEqual(["waiting", "skipped", "waiting"]);
  });

  it("waits out other replicas for a collection an earlier upload from the dialog made", async () => {
    let refusedOnce = false;
    vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => {
      const title = o?.title ?? "";
      if (title === "Q3") return { namespace: "col-q3", parent: "col-team", created: false, title };
      if (!refusedOnce) {
        refusedOnce = true; // the replica that answers has not learned of col-q3 yet
        throw new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "cannot add" });
      }
      return { namespace: "col-drafts", parent: o?.parent ?? "", created: true, title };
    });
    const team = entry("col-team", { title: "Team" });
    const plans = planFolders(["Q3/Drafts/a.md"], [team], team);
    const folders = new FolderCollections("o", new Map(plans.map((p) => [p.path, p])), "col-team", (step) => step(), [
      "col-q3",
    ]);
    const signal = new AbortController().signal;
    expect(await folders.target("Q3", signal)).toMatchObject({ namespace: "col-q3", fresh: true });
    expect(await folders.target("Q3/Drafts", signal)).toMatchObject({ namespace: "col-drafts", fresh: true });
    expect(createCollection).toHaveBeenCalledTimes(3);
  });

  it("replaces a file found in its folder's collection, and sends one into a new collection as new", async () => {
    vi.mocked(createCollection).mockResolvedValueOnce({
      namespace: "col-q3",
      parent: "col-team",
      created: false,
      title: "Q3",
    });
    const there = async () => new Map([["a.md", { title: "A", description: "", tags: [] }]]);
    await run(pick(["Q3/a.md"]), collections(["Q3/a.md"]), undefined, there);
    expect(vi.mocked(ingestKnowledge).mock.calls.at(-1)?.[1]).not.toHaveProperty("expected_revision");
    vi.mocked(createCollection).mockResolvedValueOnce({
      namespace: "col-q4",
      parent: "col-team",
      created: true,
      title: "Q4",
    });
    await run(pick(["Q4/a.md"]), collections(["Q4/a.md"]));
    expect(vi.mocked(ingestKnowledge).mock.calls.at(-1)?.[1]).toMatchObject({ expected_revision: 0 });
  });

  it("stops waiting out other replicas once stopped, making no collection after it", async () => {
    const ctrl = new AbortController();
    vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => {
      if (o?.title === "Q3") return { namespace: "col-q3", parent: "col-team", created: true, title: "Q3" };
      ctrl.abort(); // Stop lands while the child waits for other replicas to learn of col-q3
      throw new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "cannot add" });
    });
    const paths = ["Q3/Drafts/a.md"];
    const rows = await run(pick(paths), collections(paths), ctrl.signal);
    expect(createCollection).toHaveBeenCalledTimes(2);
    expect(rows.map((r) => r.state)).toEqual(["not_sent"]);
  });

  it("waits out polls of a job in a collection made a moment ago that another replica refuses", async () => {
    vi.mocked(createCollection).mockResolvedValue({
      namespace: "col-q3",
      parent: "col-team",
      created: true,
      title: "Q3",
    });
    const notYet = new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "cannot read" });
    for (let i = 0; i < 4; i++) vi.mocked(knowledgeIngestStatus).mockRejectedValueOnce(notYet);
    const paths = ["Q3/a.md"];
    const rows = await run(pick(paths), collections(paths));
    expect(rows.map((r) => [r.state, r.line])).toEqual([["done", "Revision 1"]]);
  });

  it("keeps the title of a collection that was there, or none when it has none", async () => {
    vi.mocked(createCollection).mockResolvedValueOnce({
      namespace: "col-q3",
      parent: "col-team",
      created: false,
      title: "",
    });
    const folders = collections(["Q3/a.md", "Q3/Drafts/b.md"]);
    const ctrl = new AbortController();
    expect(await folders.target("Q3", ctrl.signal)).toEqual({ namespace: "col-q3", fresh: false, title: "" });
  });

  it("keeps a folder's refusal from before a Stop, and leaves unsent what the Stop cut", async () => {
    const ctrl = new AbortController();
    vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => {
      if (o?.title === "Deep")
        throw new ApiError({ status: 422, code: "VALIDATION_FAILED", message: "collections nest at most 8 deep" });
      ctrl.abort(); // Stop lands while the other folder's collection is made
      throw new DOMException("aborted", "AbortError");
    });
    const paths = ["Deep/a.md", "Q3/b.md"];
    const placed = await placeFolders(
      pick(paths),
      collections(paths),
      async () => new Map(),
      (f) => f,
      ctrl.signal,
    );
    expect(placed.map((p) => [p.row.key, p.row.state, p.placed])).toEqual([
      ["Deep/a.md", "failed", false],
      ["Q3/b.md", "not_sent", false],
    ]);
    expect(placed[0].row.line).toMatch(/^Not added: the collection Deep couldn't be created: .*nest at most 8 deep/);
  });

  it("makes no more collections once stopped", async () => {
    const ctrl = new AbortController();
    vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => {
      ctrl.abort(); // Stop lands while the first collection is made
      return { namespace: "col-q3", parent: o?.parent ?? "", created: true, title: o?.title };
    });
    const paths = ["Q3/a.md", "Q3/Drafts/b.md", "Q3/Notes/c.md", "Q3/Notes/d.md"];
    const rows = await run(pick(paths), collections(paths), ctrl.signal);
    expect(createCollection).toHaveBeenCalledTimes(1);
    expect(rows.filter((r) => r.state === "not_sent").length).toBeGreaterThanOrEqual(3);
  });
});

describe("afterPlacing", () => {
  const rows = (): BatchRow[] =>
    planBatch(
      ["Q3/a.md", "Q3/b.md", "R/a.md"].map((p) => ({ file: new File(["x"], p.split("/").pop() as string), path: p })),
      null,
      () => ({ org: "o", namespace: "col-team" }),
      (e) => ({ folder: folderOf(e.path), sourceUri: e.file.name }),
    ).map((r, i) => ({ ...r, target: { org: "o", namespace: "col-q3" }, replaces: i === 0 }));
  const all = new Set([0, 1, 2]);

  it("skips a file found where the check did not look unless Replace was picked, and a repeat", () => {
    const predicted = (r: BatchRow) => (r.folder === "Q3" ? "col-other" : "");
    const out = afterPlacing(rows(), all, new Set(), predicted, { asked: false, skip: false });
    expect(out.map((r) => [r.key, r.state, r.line])).toEqual([
      ["Q3/a.md", "skipped", NOT_ASKED],
      ["Q3/b.md", "waiting", expect.any(String)],
      // R/a.md goes to the same collection as Q3/a.md, which no longer goes: it is the one sent.
      ["R/a.md", "waiting", expect.any(String)],
    ]);
    // Replace picked: Q3/a.md goes, and R/a.md, going to the same collection, is its repeat.
    const asked = afterPlacing(rows(), all, new Set(), predicted, { asked: true, skip: false });
    expect(asked.map((r) => [r.state, r.line])).toEqual([
      ["waiting", expect.any(String)],
      ["waiting", expect.any(String)],
      ["skipped", REPEAT],
    ]);
  });

  it("holds the rows it sends to one upload's limits, the rest waiting for the next", () => {
    const many: BatchRow[] = planBatch(
      Array.from({ length: 250 }, (_, i) => {
        const p = `Q3/f${String(i).padStart(3, "0")}.md`;
        return { file: new File(["x"], p.split("/").pop() as string), path: p };
      }),
      null,
      () => ({ org: "o", namespace: "col-team" }),
      (e) => ({ folder: folderOf(e.path), sourceUri: e.file.name }),
    ).map((r) => ({ ...r, target: { org: "o", namespace: "col-q3" } }));
    const ids = new Set(many.map((r) => r.id));
    const out = afterPlacing(many, ids, ids, () => "col-q3", { asked: false, skip: false });
    expect(out.filter((r) => r.state === "waiting")).toHaveLength(MAX_BATCH_FILES);
    expect(out.filter((r) => r.state === "next")).toHaveLength(50);
  });

  it("holds only the rows this upload sends to its limits, leaving the others as they are", () => {
    const held = rows().map((r) => (r.id === 2 ? { ...r, state: "next" as const } : r));
    const out = afterPlacing(held, new Set([0, 1]), new Set([0, 1]), () => "col-q3", { asked: false, skip: false });
    expect(out.map((r) => r.state)).toEqual(["waiting", "waiting", "next"]);
  });
});
