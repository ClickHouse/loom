import { useEffect, useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { listGrants, type CollectionEntry, type GrantEntry } from "../api";
import { collectionTitle } from "../lib/collections";
import { ApiError } from "../lib/errors";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import { useStore } from "../lib/state";
import { Skeleton } from "../components/Skeleton";
import { LoadingSkeleton } from "../components/ui";
import { MembersDialog } from "../flows/MembersDialog";

/** How far up the tree a collection's grants reach it (docs/architecture/sharing.md). */
const INHERIT_DEPTH = 8;

/** The grants read on one collection of the way up: the one shown first, then its parents. */
export interface GrantsAt {
  namespace: string;
  rows: GrantEntry[];
}

/** What the header says about who can see a collection: one line naming them, and one
 *  saying how many and where their access comes from. */
export interface WhoCanSee {
  who: string;
  how: string;
  /** No one but the person: drawn with a lock. */
  onlyYou: boolean;
}

/** A grant's subject as a person reads it. */
function subjectLabel(subject: string): string {
  if (subject.startsWith("group:")) return `group ${subject.slice(6)}`;
  if (subject.startsWith("agent:")) return `agent ${subject.slice(6)}`;
  if (subject.startsWith("user:")) return subject.slice(5);
  return subject;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "a", "a and b", "a, b and c". */
function joined(parts: string[]): string {
  return parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}` : (parts[0] ?? "");
}

/** Who can see a collection, in words, from the active grants on it and on the parents the
 *  person can read. `me`: the person's spellings. `above`: a parent they cannot read, whose
 *  grants reach it unseen. `empty`: it holds no documents yet. */
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
  if (others.length === 0 && !above) {
    return {
      who: subjects.length > 0 ? "Only you" : "No one is named on it",
      how: "Shared with no one else",
      onlyYou: subjects.length > 0,
    };
  }
  const groups = others.filter((s) => s.startsWith("group:"));
  const agents = others.filter((s) => s.startsWith("agent:"));
  const people = others.filter((s) => !groups.includes(s) && !agents.includes(s));
  const names = [...(subjects.includes("") ? ["You"] : []), ...[...groups, ...people, ...agents].map(subjectLabel)];
  const head = names.slice(0, 3);
  const more = names.length - head.length;
  // Named by no grant the person can read: the parent above says who.
  if (names.length === 0) {
    return { who: `Everyone who can see ${above}`, how: shared, onlyYou: false };
  }
  const who = more > 0 ? `${head.join(", ")} and ${more} more` : joined(head);
  const counts = joined(
    [
      people.length > 0 ? plural(people.length, "person", "people") : "",
      groups.length > 0 ? plural(groups.length, "group", "groups") : "",
      agents.length > 0 ? plural(agents.length, "agent", "agents") : "",
    ].filter(Boolean),
  );
  return { who, how: [counts, ...tail, shared].filter(Boolean).join(" · "), onlyYou: false };
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
  version,
  empty = false,
}: {
  org: string;
  collection: CollectionEntry;
  /** Every collection the person can read, by name: the parents whose grants reach it. */
  byName: Map<string, CollectionEntry>;
  /** Bumped as the members dialog closes: the grants are read again. */
  version: number;
  /** It holds no documents yet. */
  empty?: boolean;
}) {
  const whoami = useStore((s) => s.whoami);
  const [state, setState] = useState<{ grants: GrantsAt[] } | { error: unknown } | null>(null);
  const [retries, setRetries] = useState(0);
  const { chain, above } = wayUp(collection, byName);
  const chainKey = chain.join("/");
  useAbortableEffect(
    (signal, live) => {
      if (collection.personal) return;
      setState(null);
      Promise.all(
        chainKey
          .split("/")
          .map((namespace) =>
            listGrants({ org, namespace }, { signal }).then((r) => ({ namespace, rows: r.rows ?? [] })),
          ),
      )
        .then((grants) => {
          if (live()) setState({ grants });
        })
        .catch((error) => {
          if (live()) setState({ error });
        });
    },
    [org, chainKey, collection.personal, version, retries],
  );

  if (collection.personal) {
    return <Words icon="lock" who="Only you" how="Your own files: no one else's grants reach them" />;
  }
  if (state === null) {
    return (
      <Line>
        <LoadingSkeleton label="Loading who can see it">
          <Skeleton className="h-3.5 w-72 max-w-full" />
        </LoadingSkeleton>
      </Line>
    );
  }
  if ("error" in state) {
    const refused = state.error instanceof ApiError && state.error.status === 403;
    return (
      <Line>
        <Icon name="warning" size="sm" aria-hidden="true" className="shrink-0" />
        <span role="alert" className="min-w-0 break-words">
          {refused
            ? `Who can see it isn't shown to you: ${humanizeError(state.error, { bare: true, read: true })}`
            : `Couldn't read who can see it: ${humanizeError(state.error, { bare: true, read: true })}`}
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
    grants: state.grants,
    me: new Set(whoami?.subject ? [whoami.subject] : []),
    org,
    titleOf,
    above: above ? titleOf(above) : null,
    empty,
  });
  return <Words icon={words.onlyYou ? "lock" : "users"} who={words.who} how={words.how} />;
}

function Line({ children }: { children: ReactNode }) {
  return (
    // Its words wrap in its own column, so the figures beside it keep their line.
    <section aria-label="Who can see it" className="flex min-w-0 flex-1 basis-64 items-center gap-1.5">
      {children}
    </section>
  );
}

function Words({ icon, who, how }: { icon: "lock" | "users"; who: string; how: string }) {
  return (
    <Line>
      <Icon name={icon} size="sm" aria-hidden="true" className="shrink-0" />
      <span className="min-w-0 break-words">
        <span className="text-text">{who}</span>
        {how && ` · ${how}`}
      </span>
    </Line>
  );
}

/** **Share** for an admin of the collection shown, **See who** for anyone else: both open
 *  the members dialog, which gives a non-admin its list without the controls. Its words
 *  only where there is room; a phone shows the icon. */
export function ShareButton({ collection, onOpen }: { collection: CollectionEntry; onOpen: () => void }) {
  const label = collection.role === "admin" ? "Share" : "See who";
  return (
    <Button
      type="secondary"
      htmlType="button"
      iconLeft="users"
      aria-label={label}
      className="shrink-0"
      onClick={onOpen}
    >
      <span className="hidden sm:inline">{label}</span>
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
      label={collectionTitle(shown)}
      admin={shown.role === "admin"}
    />
  );
  return { open: () => setOpenFor(current?.namespace ?? null), version, dialog };
}
