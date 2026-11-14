import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import type { CollectionEntry } from "../api";
import { distinctTitle } from "../lib/collections";
import { ApiError } from "../lib/errors";
import { errorLine } from "../lib/errorWords";
import { displaySubject, isServiceSubject } from "../lib/format";
import { allGrants, type GrantsAt } from "../lib/grantPages";
import { PHONE_QUERY, useMedia } from "../lib/useMedia";
import { useAbortableEffect } from "../lib/hooks";
import { useStore } from "../lib/state";
import { Skeleton } from "../components/Skeleton";
import { IconButton } from "../components/IconButton";
import { LoadingSkeleton } from "../components/ui";
import { focusInMain } from "../components/ui/ListState";
import { type InheritedAccess, MembersDialog } from "../flows/MembersDialog";
import { collectionPath, type HiddenPart, hiddenWhat } from "./LibraryHierarchy";

/** How far up the tree a collection's grants reach it (docs/architecture/sharing.md). */
const INHERIT_DEPTH = 8;

/** The grants read on one collection of the way up: the one shown first, then its parents.
 *  `failed`: a parent's grants could not be read. */
export type ReadAt = GrantsAt & { failed?: boolean };

/** What the header says about who can see a collection: one line naming them, and one
 *  saying how many and where their access comes from. */
export interface WhoCanSee {
  who: string;
  how: string;
  /** No one but the person: drawn with a lock. */
  onlyYou: boolean;
}

/** A grant's subject as a person reads it; a service account is an agent. */
function subjectLabel(subject: string): string {
  if (subject.startsWith("group:")) return `group ${subject.slice(6)}`;
  if (subject.startsWith("agent:")) return `agent ${subject.slice(6)}`;
  if (isServiceSubject(subject)) return displaySubject(subject);
  if (subject.startsWith("user:")) return subject.slice(5);
  return subject;
}

/** An agent's subject: one an end-user token signs in, or a service account. */
function isAgentSubject(subject: string): boolean {
  return subject.startsWith("agent:") || isServiceSubject(subject);
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "a", "a and b", "a, b and c". */
function joined(parts: string[]): string {
  return parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}` : (parts[0] ?? "");
}

/** Who can see a collection, in words, from the active grants on it and on the parents the
 *  person can read. `me`: the person's spellings, counted among the people. `above`: a parent
 *  missing from the list, in words, whose grants reach it unseen. `empty`: it holds no
 *  documents yet. A read cut short counts "or more", and a parent whose grants could not be
 *  read is named as one; neither concludes "Only you" nor no one else. */
export function whoCanSee({
  grants,
  me,
  org,
  titleOf,
  above = null,
  empty = false,
}: {
  grants: ReadAt[];
  me: ReadonlySet<string>;
  org: string;
  titleOf: (namespace: string) => string;
  above?: string | null;
  empty?: boolean;
}): WhoCanSee {
  // Each subject once, by where its access is given nearest to the collection.
  const from = new Map<string, string>();
  for (const at of grants) {
    for (const r of at.failed ? [] : at.rows) {
      if (r.status !== "active") continue;
      const key = [r.subject, ...(r.also ?? [])].some((s) => me.has(s)) ? "" : r.subject;
      if (!from.has(key)) from.set(key, at.namespace);
    }
  }
  const cut = grants.some((g) => g.cut);
  // The parents whose grants reach it unseen: one whose grants failed to read, then the
  // first one missing from the list. Each says who it is and its notice.
  const unseen = [
    ...grants
      .filter((g) => g.failed)
      .map((g) => ({ who: titleOf(g.namespace), notice: "whose members couldn't be read" })),
    ...(above ? [{ who: above, notice: "" }] : []),
  ];
  const noticeOf = (u: { who: string; notice: string }) =>
    `Also everyone who can see ${u.who}${u.notice ? `, ${u.notice}` : ""}`;
  const shown = grants[0]?.namespace ?? "";
  const inherited = [...new Set([...from.values()].filter((ns) => ns !== shown))].map(titleOf);
  const subjects = [...from.keys()];
  const others = subjects.filter((s) => s !== "");
  const inheritedFrom = inherited.length > 0 ? `Inherited from ${joined(inherited)}` : "";
  const tail = [inheritedFrom, ...unseen.map(noticeOf)];
  const shared = empty ? "Anything added here is shared with them" : "";
  if (others.includes("everyone")) {
    return { who: `Everyone in ${org}`, how: [...tail, shared].filter(Boolean).join(" · "), onlyYou: false };
  }
  if (others.length === 0 && unseen.length === 0 && !cut) {
    return {
      who: subjects.length > 0 ? "Only you" : "No one is named on it",
      how: "Shared with no one else",
      onlyYou: subjects.length > 0,
    };
  }
  const groups = others.filter((s) => s.startsWith("group:"));
  const agents = others.filter(isAgentSubject);
  const people = others.filter((s) => !groups.includes(s) && !agents.includes(s));
  const you = subjects.includes("");
  const names = [...(you ? ["You"] : []), ...[...groups, ...people, ...agents].map(subjectLabel)];
  const head = names.slice(0, 3);
  const more = names.length - head.length;
  // Named by no grant the person can read: the first parent unseen says who, and the line
  // keeps the others' notices.
  if (names.length === 0) {
    const [through, ...rest] = unseen;
    return {
      who: through ? `Everyone who can see ${through.who}` : "More people than are shown here",
      how: [inheritedFrom, ...rest.map(noticeOf), shared].filter(Boolean).join(" · "),
      onlyYou: false,
    };
  }
  const who = cut ? `${head.join(", ")} and more` : more > 0 ? `${head.join(", ")} and ${more} more` : joined(head);
  const counts = joined(
    [
      people.length + Number(you) > 0 ? plural(people.length + Number(you), "person", "people") : "",
      groups.length > 0 ? plural(groups.length, "group", "groups") : "",
      agents.length > 0 ? plural(agents.length, "agent", "agents") : "",
    ].filter(Boolean),
  );
  const counted = cut && counts ? `${counts}, or more` : counts;
  return { who, how: [counted, ...tail, shared].filter(Boolean).join(" · "), onlyYou: false };
}

/** The collection and the parents above it the person can read, nearest first, at most as
 *  far as the gate walks, and the first parent they cannot read, if any. */
function wayUp(collection: CollectionEntry, byName: Map<string, CollectionEntry>) {
  const path = collectionPath(collection, byName)
    .reverse()
    .slice(0, INHERIT_DEPTH + 1);
  const chain = path.filter((p): p is CollectionEntry => "namespace" in p);
  const hidden = path.find((p): p is HiddenPart => !("namespace" in p));
  return { chain, above: hidden ? hidden.hidden : null };
}

type SharingAnswer = { key: string; grants: ReadAt[] } | { key: string; error: unknown };

/**
 * A collection's sharing in the Library's header, read once for both places that show it:
 * `line`, who can see it in words (the people, groups and agents its grants and its parents'
 * grants name, and where their access comes from); `button`, **Share** or **See who**; and
 * `dialog`, the members dialog that opens in place, with the collection's own members and,
 * read-only, who else can see it through each parent. The person's own collection takes no
 * grant, so it says it is theirs alone, reads nothing and offers no button.
 *
 * The dialog belongs to the place it opened in: a move elsewhere closes it, so a grant never
 * lands on another collection, and coming back does not reopen it. The grants are read again
 * as it closes.
 */
export function useCollectionSharing({
  org,
  collection,
  byName,
  repeated,
  truncated = false,
  empty = false,
  place,
  onOpenPlace,
}: {
  org: string;
  /** The collection shown, or null for a namespace. */
  collection: CollectionEntry | null;
  /** Every collection the person can read, by name: the parents whose grants reach it. */
  byName: Map<string, CollectionEntry>;
  /** Titles two collections share (`repeatedTitles`): those name their address too, as the
   *  path does. */
  repeated: Set<string>;
  /** The list of collections was cut short: a parent missing from it may be past its end. */
  truncated?: boolean;
  /** It holds no documents yet. */
  empty?: boolean;
  /** Where the page is: a move elsewhere closes the dialog. */
  place: string;
  /** Show a parent in the Library, from the dialog. */
  onOpenPlace: (collection: string) => void;
}) {
  const whoami = useStore((s) => s.whoami);
  const [openFor, setOpenFor] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => setOpenFor(null), [place]);
  const { chain, above } = useMemo(
    () => (collection ? wayUp(collection, byName) : { chain: [], above: null }),
    [collection, byName],
  );
  // The places read, by name: the list of collections is read again (and `chain` rebuilt)
  // on every write elsewhere, which must not read the grants again.
  const chainKey = chain.map((c) => c.namespace).join("/");
  const key = `${org} ${chainKey}`;
  // The last answer, by the places it was read for: one for another collection reads as
  // loading, so its words never show under this one; a read again of these places keeps it.
  const [state, setState] = useState<SharingAnswer | null>(null);
  const [reading, setReading] = useState(false);
  const [retries, setRetries] = useState(0);
  const personal = !collection || collection.personal;
  useAbortableEffect(
    (signal, live) => {
      if (personal) return;
      setReading(true);
      // The collection's own grants decide the line; a parent's failure is named, not fatal.
      const [own, ...parents] = chain.map((c) => c.namespace);
      Promise.all([
        allGrants({ org, namespace: own }, signal),
        ...parents.map((namespace) =>
          allGrants({ org, namespace }, signal).catch((): ReadAt => ({ namespace, rows: [], failed: true })),
        ),
      ])
        .then((grants) => {
          if (live()) setState({ key, grants });
        })
        .catch((error) => {
          if (live()) setState({ key, error });
        })
        .finally(() => {
          if (live()) setReading(false);
        });
    },
    // `chainKey` stands in for `chain`, rebuilt on every read of the list, and `key` is
    // `org` with it.
    [org, chainKey, personal, version, retries],
  );

  const titleOf = (ns: string) => {
    const c = byName.get(ns);
    return c ? distinctTitle(c, repeated) : ns;
  };
  const answer = state?.key === key ? state : null;
  const read = answer && "grants" in answer ? answer.grants : null;
  const hiddenWords = above ? hiddenWhat(truncated) : null;

  let line: ReactNode = null;
  if (collection?.personal) {
    line = <Words icon="lock" who="Only you" how="Your own files: no one else's grants reach them" />;
  } else if (collection && answer === null) {
    line = (
      <Line>
        <LoadingSkeleton label="Loading who can see it">
          <Skeleton className="h-3.5 w-72 max-w-full" />
        </LoadingSkeleton>
      </Line>
    );
  } else if (collection && answer && "error" in answer) {
    const refused = answer.error instanceof ApiError && answer.error.status === 403;
    line = (
      <Line busy={reading}>
        <Icon name="warning" size="sm" aria-hidden="true" className="shrink-0" />
        <span role="alert" className="min-w-0 break-words">
          {refused
            ? `Who can see it isn't shown to you: ${errorLine(answer.error, { read: true })}`
            : `Couldn't read who can see it: ${errorLine(answer.error, { read: true })}`}
        </span>
        {!refused && (
          <Link
            component="button"
            type="button"
            size="sm"
            // Retry swaps the error for the placeholder until the read answers, so a second
            // failure mounts a fresh alert, which is announced; focus goes to the page's
            // heading, as a list's Retry sends it.
            onClick={() => {
              focusInMain(null);
              setState(null);
              setRetries((n) => n + 1);
            }}
          >
            Retry
          </Link>
        )}
      </Line>
    );
  } else if (collection && read) {
    const words = whoCanSee({
      grants: read,
      me: new Set(whoami?.subject ? [whoami.subject] : []),
      org,
      titleOf,
      above: hiddenWords,
      empty,
    });
    line = <Words icon={words.onlyYou ? "lock" : "users"} who={words.who} how={words.how} busy={reading} />;
  }

  const button =
    collection && !collection.personal ? (
      <ShareButton collection={collection} onOpen={() => setOpenFor(collection.namespace)} />
    ) : null;

  // Who else can see it, nearest parent first, from the grants the line read.
  const inherited: InheritedAccess[] = [];
  if (read) {
    for (const at of read.slice(1)) {
      inherited.push({
        namespace: at.namespace,
        title: titleOf(at.namespace),
        rows: at.failed ? null : at.rows,
        cut: at.cut,
        onOpen: () => onOpenPlace(at.namespace),
      });
    }
    if (hiddenWords) inherited.push({ namespace: "", title: hiddenWords, rows: null, hidden: true });
  }
  const shown = collection && !collection.personal && openFor === collection.namespace ? collection : null;
  const dialog = shown && (
    <MembersDialog
      open
      onOpenChange={(v) => {
        if (v) return;
        setOpenFor(null);
        setVersion((n) => n + 1);
      }}
      org={org}
      namespace={shown.namespace}
      // A title, with the address beneath it; an untitled collection is named by its address once.
      label={shown.title?.trim() || undefined}
      admin={shown.role === "admin"}
      inherited={inherited}
    />
  );
  return { line, button, dialog };
}

function Line({ children, busy = false }: { children: ReactNode; busy?: boolean }) {
  return (
    // Its words wrap in its own column, so the figures beside it keep their line. A read
    // again keeps them, marked busy.
    <section
      aria-label="Who can see it"
      aria-busy={busy || undefined}
      className="flex min-w-0 flex-1 basis-64 items-center gap-1.5"
    >
      {children}
    </section>
  );
}

function Words({ icon, who, how, busy }: { icon: "lock" | "users"; who: string; how: string; busy?: boolean }) {
  return (
    <Line busy={busy}>
      <Icon name={icon} size="sm" aria-hidden="true" className="shrink-0" />
      <span className="min-w-0 break-words">
        <span className="text-text">{who}</span>
        {how && ` · ${how}`}
      </span>
    </Line>
  );
}

/** **Share** for an admin of the collection shown, **See who** for anyone else: both open
 *  the members dialog, which gives a non-admin its list without the controls. On a phone,
 *  where the header has no room for another label, its icon alone with a tooltip. */
export function ShareButton({ collection, onOpen }: { collection: CollectionEntry; onOpen: () => void }) {
  const phone = useMedia(PHONE_QUERY);
  const label = collection.role === "admin" ? "Share" : "See who";
  if (phone) return <IconButton icon="users" label={label} onClick={onOpen} />;
  return (
    <Button type="secondary" htmlType="button" iconLeft="users" onClick={onOpen}>
      {label}
    </Button>
  );
}
