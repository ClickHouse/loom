import { useEffect, useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { listGrants, type CollectionEntry, type GrantEntry } from "../api";
import { collectionTitle } from "../lib/collections";
import { ApiError } from "../lib/errors";
import { errorLine } from "../lib/errorWords";
import { displaySubject, isServiceSubject } from "../lib/format";
import { PHONE_QUERY, useMedia } from "../lib/useMedia";
import { useAbortableEffect } from "../lib/hooks";
import { useStore } from "../lib/state";
import { Skeleton } from "../components/Skeleton";
import { IconButton } from "../components/IconButton";
import { LoadingSkeleton } from "../components/ui";
import { MembersDialog } from "../flows/MembersDialog";
import { hiddenWhat } from "./LibraryHierarchy";

/** How far up the tree a collection's grants reach it (docs/architecture/sharing.md). */
const INHERIT_DEPTH = 8;

/** Grants read per page, and the pages read on one collection before the words say "or more". */
const GRANTS_PAGE = 500;
const GRANTS_PAGES = 20;

/** The grants read on one collection of the way up: the one shown first, then its parents.
 *  `cut`: more pages followed than were read. */
export interface GrantsAt {
  namespace: string;
  rows: GrantEntry[];
  cut?: boolean;
}

/** Every grant on `namespace`, page by page, up to `GRANTS_PAGES` pages. */
async function grantsOn(org: string, namespace: string, signal: AbortSignal): Promise<GrantsAt> {
  const rows: GrantEntry[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < GRANTS_PAGES; page++) {
    const r = await listGrants({ org, namespace }, { limit: GRANTS_PAGE, cursor, signal });
    rows.push(...(r.rows ?? []));
    cursor = r.next_cursor ?? null;
    if (!cursor) return { namespace, rows };
  }
  return { namespace, rows, cut: true };
}

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
 *  person can read. `me`: the person's spellings. `above`: a parent missing from the list, in
 *  words, whose grants reach it unseen. `empty`: it holds no documents yet. A read cut short
 *  counts "or more" and concludes neither "Only you" nor no one else. */
export function whoCanSee({
  grants,
  me,
  org,
  titleOf,
  above = null,
  empty = false,
}: {
  grants: GrantsAt[];
  me: ReadonlySet<string>;
  org: string;
  titleOf: (namespace: string) => string;
  above?: string | null;
  empty?: boolean;
}): WhoCanSee {
  // Each subject once, by where its access is given nearest to the collection.
  const from = new Map<string, string>();
  for (const at of grants) {
    for (const r of at.rows) {
      if (r.status !== "active") continue;
      const key = [r.subject, ...(r.also ?? [])].some((s) => me.has(s)) ? "" : r.subject;
      if (!from.has(key)) from.set(key, at.namespace);
    }
  }
  const cut = grants.some((g) => g.cut);
  const shown = grants[0]?.namespace ?? "";
  const inherited = [...new Set([...from.values()].filter((ns) => ns !== shown))].map(titleOf);
  const subjects = [...from.keys()];
  const others = subjects.filter((s) => s !== "");
  const tail = [
    inherited.length > 0 ? `Inherited from ${joined(inherited)}` : "",
    above ? `Also everyone who can see ${above}` : "",
  ];
  const shared = empty ? "Anything added here is shared with them" : "";
  if (others.includes("everyone")) {
    return { who: `Everyone in ${org}`, how: [...tail, shared].filter(Boolean).join(" · "), onlyYou: false };
  }
  if (others.length === 0 && !above && !cut) {
    return {
      who: subjects.length > 0 ? "Only you" : "No one is named on it",
      how: "Shared with no one else",
      onlyYou: subjects.length > 0,
    };
  }
  const groups = others.filter((s) => s.startsWith("group:"));
  const agents = others.filter(isAgentSubject);
  const people = others.filter((s) => !groups.includes(s) && !agents.includes(s));
  const names = [...(subjects.includes("") ? ["You"] : []), ...[...groups, ...people, ...agents].map(subjectLabel)];
  const head = names.slice(0, 3);
  const more = names.length - head.length;
  // Named by no grant the person can read: the parent above says who.
  if (names.length === 0) {
    return {
      who: above ? `Everyone who can see ${above}` : "More people than are shown here",
      how: shared,
      onlyYou: false,
    };
  }
  const who = cut ? `${head.join(", ")} and more` : more > 0 ? `${head.join(", ")} and ${more} more` : joined(head);
  const counts = joined(
    [
      people.length > 0 ? plural(people.length, "person", "people") : "",
      groups.length > 0 ? plural(groups.length, "group", "groups") : "",
      agents.length > 0 ? plural(agents.length, "agent", "agents") : "",
    ].filter(Boolean),
  );
  const counted = cut && counts ? `${counts}, or more` : counts;
  return { who, how: [counted, ...tail, shared].filter(Boolean).join(" · "), onlyYou: false };
}

/** The collection and the parents above it the person can read, nearest first, and the first
 *  parent they cannot read, if any. */
function wayUp(collection: CollectionEntry, byName: Map<string, CollectionEntry>) {
  const chain = [collection.namespace];
  let parent = collection.parent;
  while (parent && chain.length < INHERIT_DEPTH + 1 && !chain.includes(parent)) {
    if (!byName.has(parent)) return { chain, above: parent };
    chain.push(parent);
    parent = byName.get(parent)?.parent ?? "";
  }
  return { chain, above: null };
}

/**
 * Who can see the collection shown, in one line of the Library's header: the people, groups
 * and agents its grants and its parents' grants name, in words, and where their access
 * comes from. The person's own collection takes no grant, so it says it is theirs alone and
 * reads nothing.
 */
export function CollectionSharingLine({
  org,
  collection,
  byName,
  truncated = false,
  version,
  empty = false,
}: {
  org: string;
  collection: CollectionEntry;
  /** Every collection the person can read, by name: the parents whose grants reach it. */
  byName: Map<string, CollectionEntry>;
  /** The list of collections was cut short: a parent missing from it may be past its end. */
  truncated?: boolean;
  /** Bumped as the members dialog closes: the grants are read again. */
  version: number;
  /** It holds no documents yet. */
  empty?: boolean;
}) {
  const whoami = useStore((s) => s.whoami);
  const { chain, above } = wayUp(collection, byName);
  const chainKey = chain.join("/");
  const key = `${org} ${chainKey}`;
  // The last answer, by the places it was read for: one for another collection reads as
  // loading, so its words never show under this one; a read again of these places keeps it.
  const [state, setState] = useState<{ key: string; grants: GrantsAt[] } | { key: string; error: unknown } | null>(
    null,
  );
  const [reading, setReading] = useState(false);
  const [retries, setRetries] = useState(0);
  useAbortableEffect(
    (signal, live) => {
      if (collection.personal) return;
      setReading(true);
      Promise.all(chainKey.split("/").map((namespace) => grantsOn(org, namespace, signal)))
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
    [org, chainKey, key, collection.personal, version, retries],
  );

  if (collection.personal) {
    return <Words icon="lock" who="Only you" how="Your own files: no one else's grants reach them" />;
  }
  const answer = state?.key === key ? state : null;
  if (answer === null) {
    return (
      <Line>
        <LoadingSkeleton label="Loading who can see it">
          <Skeleton className="h-3.5 w-72 max-w-full" />
        </LoadingSkeleton>
      </Line>
    );
  }
  if ("error" in answer) {
    const refused = answer.error instanceof ApiError && answer.error.status === 403;
    return (
      <Line busy={reading}>
        <Icon name="warning" size="sm" aria-hidden="true" className="shrink-0" />
        <span role="alert" className="min-w-0 break-words">
          {refused
            ? `Who can see it isn't shown to you: ${errorLine(answer.error, { read: true })}`
            : `Couldn't read who can see it: ${errorLine(answer.error, { read: true })}`}
        </span>
        {!refused && (
          <Link component="button" type="button" size="sm" onClick={() => setRetries((n) => n + 1)}>
            Retry
          </Link>
        )}
      </Line>
    );
  }
  const titleOf = (ns: string) => {
    const c = byName.get(ns);
    return c ? collectionTitle(c) : ns;
  };
  const words = whoCanSee({
    grants: answer.grants,
    me: new Set(whoami?.subject ? [whoami.subject] : []),
    org,
    titleOf,
    above: above ? hiddenWhat(truncated) : null,
    empty,
  });
  return <Words icon={words.onlyYou ? "lock" : "users"} who={words.who} how={words.how} busy={reading} />;
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

/** The members dialog of the collection shown, opened by Share in place. It belongs to the
 *  place it opened in: a move elsewhere closes it, so a grant never lands on another
 *  collection, and coming back does not reopen it. `version` is bumped as it closes, so the
 *  header reads the grants it may have changed. */
export function useShareDialog(org: string, current: CollectionEntry | null, place: string) {
  const [openFor, setOpenFor] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => setOpenFor(null), [place]);
  const shown = current && !current.personal && openFor === current.namespace ? current : null;
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
    />
  );
  return { open: () => setOpenFor(current?.namespace ?? null), version, dialog };
}
