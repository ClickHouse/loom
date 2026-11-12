/**
 * A folder into the Library: the folder and each folder inside it become collections,
 * nested as the folders are, inside the collection shown, and each file goes into its
 * folder's collection. A collection is made by its title (`collection.create` with
 * `derive_name`), so one the folder already has under that parent is used again. An
 * upload makes the collections of its folders before any file goes, and then checks what
 * each holds: the files go where the server put their folder. A file held past one
 * upload's limits, or a Stop, makes no collection.
 */
import { type CollectionEntry, createCollection } from "../api";
import {
  BATCH_OPEN,
  type BatchRow,
  capBatch,
  inTurn,
  type KeptFields,
  type PickedEntry,
  skipRepeats,
  WAITING,
  whileNew,
} from "./batchIngest";
import { errorLine } from "./errorWords";
import { distinctTitle, plainName, titleKey } from "./collections";

/** Ancestors a collection may have (server/auth/sharing.py MAX_DEPTH). */
export const MAX_DEPTH = 8;
/** The most collections one pick of folders makes, of those its preview lists as new. */
export const MAX_FOLDERS = 100;

/** Folders never read into: what an operating system or a tool keeps beside the files. */
function hiddenFolder(name: string): boolean {
  return name.startsWith(".") || name === "__MACOSX";
}

interface FsEntry {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
}
interface FsFileEntry extends FsEntry {
  file: (ok: (f: File) => void, fail: (e: unknown) => void) => void;
}
interface FsDirEntry extends FsEntry {
  createReader: () => { readEntries: (ok: (es: FsEntry[]) => void, fail: (e: unknown) => void) => void };
}

/** The entries of a drop, taken while the drop lasts (the browser empties the list once
 *  its handler returns): a folder as its entry, a loose file as itself. */
export function takeDropped(items: DataTransferItemList | ArrayLike<DataTransferItem>): {
  entries: FsEntry[];
  files: File[];
} {
  const entries: FsEntry[] = [];
  const files: File[] = [];
  for (const item of Array.from(items)) {
    if (item.kind !== "file") continue;
    const entry = (item as DataTransferItem & { webkitGetAsEntry?: () => FsEntry | null }).webkitGetAsEntry?.();
    if (entry?.isDirectory) entries.push(entry);
    else {
      // No entry (some drag sources): the file itself.
      const f = item.getAsFile();
      if (f) files.push(f);
    }
  }
  return { entries, files };
}

/** Files read from a folder before the dialog says it is too large to read here. */
export const MAX_READ = 2000;

/** What reading a folder left out, said in one line each: a hidden folder (with how many
 *  files the picker gave from it), an entry that could not be read. */
export interface ReadSkips {
  hidden: Map<string, number>;
  unreadable: number;
}

export class FolderTooLarge extends Error {
  constructor() {
    super(`The folder holds more than ${MAX_READ} files; pick a folder further down.`);
    this.name = "FolderTooLarge";
  }
}

/** Every file under the dropped folders, with its path from the dropped folder. A hidden
 *  folder is not read into; an entry that cannot be read is counted and left out; past
 *  `MAX_READ` files the read stops. `onCount` follows how many have been read. */
export async function readFolders(
  roots: FsEntry[],
  onCount?: (n: number) => void,
  signal?: AbortSignal,
): Promise<{ picked: PickedEntry[]; skips: ReadSkips }> {
  const picked: PickedEntry[] = [];
  const skips: ReadSkips = { hidden: new Map(), unreadable: 0 };
  const walk = async (entry: FsEntry, path: string): Promise<void> => {
    signal?.throwIfAborted(); // the dialog closed, or a newer pick replaced this one
    if (entry.isFile) {
      if (picked.length >= MAX_READ) throw new FolderTooLarge();
      try {
        const file = await new Promise<File>((ok, fail) => (entry as FsFileEntry).file(ok, fail));
        picked.push({ file, path });
        onCount?.(picked.length);
      } catch {
        skips.unreadable++;
      }
      return;
    }
    if (hiddenFolder(entry.name)) {
      skips.hidden.set(path, 0);
      return;
    }
    const reader = (entry as FsDirEntry).createReader();
    // A reader answers in batches (100 in Chrome) until an empty one.
    for (;;) {
      let batch: FsEntry[];
      try {
        batch = await new Promise<FsEntry[]>((ok, fail) => reader.readEntries(ok, fail));
      } catch {
        skips.unreadable++;
        return;
      }
      if (batch.length === 0) return;
      for (const child of batch) await walk(child, `${path}/${child.name}`);
    }
  };
  for (const root of roots) await walk(root, root.name);
  return { picked, skips };
}

/** The files of a folder picked with the folder input, with their paths; the files under
 *  a hidden folder, which the picker gives too, are left out and counted by that folder.
 *  Past `MAX_READ` files left, FolderTooLarge, as for a dropped folder. */
export function pickedFolder(files: File[]): { picked: PickedEntry[]; skips: ReadSkips } {
  const picked: PickedEntry[] = [];
  const skips: ReadSkips = { hidden: new Map(), unreadable: 0 };
  for (const file of files) {
    const path = (file as File & { webkitRelativePath?: string }).webkitRelativePath || "";
    const parts = path.split("/");
    const at = parts.slice(0, -1).findIndex(hiddenFolder);
    if (at >= 0) {
      const top = parts.slice(0, at + 1).join("/");
      skips.hidden.set(top, (skips.hidden.get(top) ?? 0) + 1);
    } else picked.push({ file, path });
  }
  if (picked.length > MAX_READ) throw new FolderTooLarge();
  return { picked, skips };
}

/** What reading left out, in words; "" for nothing. */
export function skipsLine(skips: ReadSkips): string {
  const hidden = [...skips.hidden].map(([path, n]) => (n ? `${path} (${n.toLocaleString()} files)` : path));
  const parts = [
    hidden.length
      ? `Hidden folders not read: ${hidden.slice(0, 5).join(", ")}${hidden.length > 5 ? ` and ${hidden.length - 5} more` : ""}.`
      : "",
    skips.unreadable ? `${skips.unreadable} item${skips.unreadable === 1 ? "" : "s"} couldn't be read.` : "",
  ];
  return parts.filter(Boolean).join(" ");
}

/** The folder a file is in ("" for a loose one). */
export function folderOf(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? "" : path.slice(0, i);
}

/** One folder that becomes a collection. */
export interface FolderPlan {
  path: string;
  title: string;
  /** Its parent folder's path; "" for a dropped folder, which sits in the place shown. */
  parentPath: string;
  /** The collection it is predicted to be already (from the list the page has), or "". */
  existing: string;
  files: number;
  /** Why the server would refuse the folder's name as a title; "" when it would not. */
  refused: string;
  /** The sibling folder with the same title whose collection this one is. */
  sameAs?: string;
}

/** The folders of the files that will be sent, parents before children, each with the
 *  collection it already is when the list says so: a child of its parent with the same
 *  title (or untitled, by the name the title spells). */
export function planFolders(paths: string[], rows: CollectionEntry[], place: CollectionEntry | null): FolderPlan[] {
  const counts = new Map<string, number>();
  // Siblings go in the order of their first file, the order their creates most likely land in.
  const firstAt = new Map<string, number>();
  paths.forEach((p, at) => {
    const folder = folderOf(p);
    if (!folder) return;
    // Every ancestor folder too: a folder of folders becomes a collection.
    const parts = folder.split("/");
    for (let i = 1; i <= parts.length; i++) {
      const f = parts.slice(0, i).join("/");
      counts.set(f, (counts.get(f) ?? 0) + (i === parts.length ? 1 : 0));
      if (!firstAt.has(f)) firstAt.set(f, at);
    }
  });
  // Parents first, then siblings by their first file.
  const plans = [...counts.keys()].sort(
    (a, b) => a.split("/").length - b.split("/").length || (firstAt.get(a) as number) - (firstAt.get(b) as number),
  );
  const made = new Map<string, FolderPlan>();
  // Sibling folders whose titles match are one collection (a title names one within a
  // parent): the first one's plan, which the others point to.
  const first = new Map<string, string>();
  // A collection a folder's plan takes, by the title that took it: the server gives it to no
  // other title, and again to the same title under another folder that lands on its parent.
  const taken = new Map<string, string>();
  for (const path of plans) {
    const rawParent = folderOf(path);
    const parentPath = made.get(rawParent)?.sameAs ?? rawParent;
    const title = path.slice(rawParent ? rawParent.length + 1 : 0);
    const sibling = `${parentPath}\0${titleKey(title)}`;
    const earlier = first.get(sibling);
    if (earlier) {
      const plan = made.get(earlier);
      if (plan) plan.files += counts.get(path) ?? 0;
      made.set(path, { path, title, parentPath, existing: "", files: 0, refused: "", sameAs: earlier });
      continue;
    }
    first.set(sibling, path);
    // At the top level the server reuses only the caller's own, guessed by the admin role
    // a creator holds; a wrong guess is caught when the server answers.
    const parent = parentPath ? made.get(parentPath)?.existing || "" : (place?.namespace ?? "");
    const under = parent
      ? rows.filter((c) => c.parent === parent && !c.personal)
      : parentPath
        ? []
        : rows.filter((c) => c.parent === "" && !c.personal && c.role === "admin");
    const key = titleKey(title);
    // The first by name that the title names, as the server picks: one with that title, or
    // an untitled one whose name the title spells.
    const same = under
      .sort((x, y) => (x.namespace < y.namespace ? -1 : x.namespace > y.namespace ? 1 : 0))
      .find(
        (c) =>
          (taken.get(c.namespace) ?? key) === key &&
          (c.title ? titleKey(c.title) === key : plainName(title) !== "" && c.namespace === plainName(title)),
      );
    // An untitled one is titled for the first folder only when the caller is its admin;
    // otherwise the server answers every folder that spells it with it.
    if (same && (same.title || same.role === "admin")) taken.set(same.namespace, key);
    made.set(path, {
      path,
      title,
      parentPath,
      existing: same?.namespace ?? "",
      files: counts.get(path) ?? 0,
      refused: titleProblem(title),
    });
  }
  return [...made.values()];
}

/** The plans that make a collection: not the folders that are another's. */
export function collectionsMade(plans: FolderPlan[]): FolderPlan[] {
  return plans.filter((p) => !p.sameAs);
}

/** What Python's `str.strip()` takes off, as the server strips a title first. */
const PY_SPACE = "[\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const PY_STRIP = new RegExp(`^${PY_SPACE}+|${PY_SPACE}+$`, "g");

/** Why the server refuses `title` (server/identity/api/models_collections.py): nothing but
 *  spaces, longer than 200 characters, or holding a control or invisible character; "" when
 *  it takes it. */
export function titleProblem(title: string): string {
  // Stripped, then normalised, in the server's order.
  const t = title.replace(PY_STRIP, "").normalize("NFC");
  if (!t) return "has no name but spaces";
  if (Array.from(t).length > 200) return "longer than 200 characters";
  // Control characters, and format characters but the joiners, soft hyphen and tags.
  if (/[\p{Cc}\u034f\u3164\u115f\u1160\uffa0\u2800\u2028\u2029]/u.test(t)) return "holds an invisible character";
  if (/(?![\u200c\u200d\u00ad\u{e0020}-\u{e007f}])\p{Cf}/u.test(t)) return "holds an invisible character";
  return "";
}

/** The ancestors the shown collection has that the caller can read, itself included: a
 *  lower bound, since `collection.list` leaves out the ones it cannot read. */
export function lineageLength(place: CollectionEntry | null, rows: CollectionEntry[]): number {
  if (!place) return 0;
  const byName = new Map(rows.map((c) => [c.namespace, c]));
  let n = 1;
  for (let p = place.parent; p && byName.has(p) && n <= MAX_DEPTH; p = byName.get(p)?.parent ?? "") n++;
  return n;
}

/** The folders that would nest deeper than collections may, inside the place shown. */
export function tooDeep(plans: FolderPlan[], placeLineage: number): string[] {
  // A folder k levels down sits under placeLineage + k - 1 collections.
  return plans.filter((p) => placeLineage + p.path.split("/").length - 1 > MAX_DEPTH).map((p) => p.path);
}

/** A folder whose collection could not be made, by the collection's title, and why. */
export class FolderRefused extends Error {
  constructor(
    readonly title: string,
    cause: unknown,
  ) {
    super(`the collection ${title} could not be made`, { cause });
    this.name = "FolderRefused";
  }
}

/** Where a folder went: the collection, whether this dialog made it, and its title. */
export interface Went {
  namespace: string;
  fresh: boolean;
  title: string;
}

/** Makes each folder's collection when asked, parents first, once per folder. A create a
 *  Stop cut short may still have been made: the next upload finds it as one that was there
 *  (not fresh), so a replica that has not learned of it yet fails its files, for Retry. */
export class FolderCollections {
  private made = new Map<string, Promise<Went>>();

  constructor(
    private org: string,
    private plans: Map<string, FolderPlan>,
    /** Where the dropped folders go: the collection shown, or "" for the top level. */
    private root: string,
    private retry: <T>(step: () => Promise<T>) => Promise<T>,
    /** Collections an earlier upload from the same dialog made: other replicas may not
     *  know them yet either. */
    private earlier: readonly string[] = [],
  ) {}

  /** The collections this upload made, parents first. */
  readonly created: string[] = [];

  /** The namespace a folder's files go to, making its collection (and its parents') first. */
  target(folder: string, signal: AbortSignal): Promise<Went> {
    // A folder whose title a sibling has: that sibling's collection, made once.
    const same = this.plans.get(folder)?.sameAs;
    if (same) return this.target(same, signal);
    let p = this.made.get(folder);
    if (!p) {
      // A refusal is kept for the rest of the upload: its other files fail at once rather
      // than asking again. Retry failed starts a new upload, which asks again.
      p = this.make(folder, signal);
      this.made.set(folder, p);
    }
    return p;
  }

  private async make(folder: string, signal: AbortSignal): Promise<Went> {
    // Every folder sent, and every one above it, was planned.
    const plan = this.plans.get(folder) as FolderPlan;
    const parent = plan.parentPath
      ? await this.target(plan.parentPath, signal)
      : { namespace: this.root, fresh: false };
    signal.throwIfAborted(); // Stop makes no more collections
    let r: Awaited<ReturnType<typeof createCollection>>;
    try {
      r = await this.retry(() =>
        whileNew(
          () => createCollection(this.org, "", { parent: parent.namespace, title: plan.title, deriveName: true }),
          parent.fresh,
          signal,
        ),
      );
    } catch (e) {
      // Named by the folder refused, which the files of the folders inside it say too.
      throw new FolderRefused(plan.title, e);
    }
    if (r.created) this.created.push(r.namespace);
    return {
      namespace: r.namespace,
      fresh: r.created || this.earlier.includes(r.namespace),
      // The title the server holds (stripped and normalised), or none: then it shows by name.
      title: r.title ?? "",
    };
  }
}

/** What a folder's collection is called in a line: an existing one as the Library names
 *  it, with its address when another readable collection shares its title. */
export function folderLabel(plan: FolderPlan, rows: CollectionEntry[], repeated: Set<string>): string {
  const existing = plan.existing ? rows.find((c) => c.namespace === plan.existing) : undefined;
  return existing ? distinctTitle(existing, repeated) : plan.title;
}

/** What a folder's file is shown with, once its collection is known. */
export interface Placed {
  row: BatchRow;
  /** It goes into its folder's collection, made for this upload. */
  placed: boolean;
  /** The collection it goes to, as the Library would name it; "" for a loose file. */
  where: string;
}

/** Makes each waiting folder's collection, parents first, and points its files at it, marked
 *  with what it already holds. A refusal fails the folder's files; a Stop leaves them not sent. */
export async function placeFolders(
  rows: BatchRow[],
  folders: FolderCollections,
  holds: (namespace: string) => Promise<Map<string, KeptFields>>,
  name: (folder: string, went: Went) => string,
  signal: AbortSignal,
): Promise<Placed[]> {
  // What a Stop cut short ends in an AbortError (or a refusal carrying one); any other answer
  // is the folder's own, kept with its reason whenever the Stop came.
  const isAbort = (e: unknown) => (e as { name?: unknown } | null)?.name === "AbortError";
  const stopped = (e: unknown) => isAbort(e) || (e instanceof FolderRefused && isAbort(e.cause));
  const wanted = [...new Set(rows.filter((r) => r.state === "waiting" && r.folder).map((r) => r.folder))];
  const went = new Map<string, Went>();
  const refused = new Map<string, unknown>();
  const cut = new Set<string>();
  await inTurn(wanted, BATCH_OPEN, (folder) =>
    folders.target(folder, signal).then(
      (w) => void went.set(folder, w),
      (e) => void (stopped(e) ? cut.add(folder) : refused.set(folder, e)),
    ),
  );
  // What each collection that was there holds, three read at a time. A read that fails
  // fails its files: sent as new, a file there would be skipped, not replaced as picked.
  const there = new Map<string, Map<string, KeptFields>>();
  const unread = new Map<string, unknown>();
  const unreadCut = new Set<string>();
  const toRead = [...new Set([...went.values()].filter((w) => !w.fresh).map((w) => w.namespace))];
  await inTurn(toRead, BATCH_OPEN, (ns) =>
    holds(ns).then(
      (m) => void there.set(ns, m),
      (e) => void (stopped(e) ? unreadCut.add(ns) : unread.set(ns, e)),
    ),
  );
  const notSent = (r: BatchRow): Placed => ({ row: { ...r, state: "not_sent", line: "" }, placed: false, where: "" });
  const fail = (r: BatchRow, line: string): Placed => ({
    row: { ...r, state: "failed", line },
    placed: false,
    where: "",
  });
  return rows.map((r) => {
    if (r.state !== "waiting" || !r.folder) return { row: r, placed: false, where: "" };
    if (cut.has(r.folder)) return notSent(r);
    const w = went.get(r.folder);
    if (!w) {
      // Every refusal is a FolderRefused: make wraps its own, a parent's passes up as it is.
      const e = refused.get(r.folder) as FolderRefused;
      return fail(r, `Not added: the collection ${e.title} couldn't be created: ${errorLine(e.cause)}`);
    }
    if (unreadCut.has(w.namespace)) return notSent(r);
    if (unread.has(w.namespace))
      return fail(
        r,
        `Not sent: couldn't check what is already in it: ${errorLine(unread.get(w.namespace), { read: true })}`,
      );
    const held = w.fresh ? undefined : there.get(w.namespace);
    return {
      row: {
        ...r,
        target: { ...r.target, namespace: w.namespace },
        // Its collection is made: it waits its turn.
        line: WAITING,
        fresh: w.fresh,
        replaces: !!held?.has(r.sourceUri),
        kept: held?.get(r.sourceUri),
      },
      placed: true,
      where: name(r.folder, w),
    };
  });
}

/** The line of a file found in a collection no one was asked about: not replaced. */
export const NOT_ASKED = "Already there: not replaced, since no one was asked";

/** The rows once placed: a file found where the check did not look is skipped unless someone
 *  picked, a repeated name goes once, and what this upload sends is held to its limits again. */
export function afterPlacing(
  rows: BatchRow[],
  placed: ReadonlySet<number>,
  sending: ReadonlySet<number>,
  predicted: (r: BatchRow) => string,
  { asked, skip }: { asked: boolean; skip: boolean },
): BatchRow[] {
  const decided = skipRepeats(
    rows.map((r) =>
      r.state === "waiting" && r.replaces && placed.has(r.id) && r.target.namespace !== predicted(r) && !asked
        ? { ...r, state: "skipped" as const, line: NOT_ASKED }
        : r,
    ),
    (r) => (r.folder && !placed.has(r.id) ? "" : r.target.namespace),
  );
  const capped = new Map(
    capBatch(
      decided.filter((r) => sending.has(r.id)),
      skip,
    ).map((r) => [r.id, r]),
  );
  return decided.map((r) => capped.get(r.id) ?? r);
}
