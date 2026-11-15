import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { type CollectionEntry, isAuthDisabled } from "../api";
import { distinctTitle } from "../lib/collections";
import { ApiError } from "../lib/errors";
import { errorLine } from "../lib/errorWords";
import { andList, fmtInt } from "../lib/format";
import { grantSpellings, isAgentSubject, mySpellings, namesMe, subjectLabel } from "../lib/grantSubject";
import { allGrants, type GrantsAt } from "../lib/grantPages";
import { PHONE_QUERY, useMedia } from "../lib/useMedia";
import { useAbortableEffect } from "../lib/hooks";
import { useStore } from "../lib/state";
import { Skeleton } from "../components/Skeleton";
import { IconButton } from "../components/IconButton";
import { LoadingSkeleton } from "../components/ui";
import { type InheritedAccess, MembersDialog } from "../flows/MembersDialog";
import { collectionPath, type HiddenPart, hiddenWhat } from "./LibraryHierarchy";
import { focusViewHeading } from "../lib/place";

/** How far up the tree a collection's grants reach it (docs/architecture/sharing.md). */
const INHERIT_DEPTH = 8;

/** The grants read on one collection of the way up: the one shown first, then its parents.
 *  `failed`: a parent's grants were not read; `refused`, among those: the read was refused
 *  (403), so they are not shown. */
export type ReadAt = GrantsAt & { failed?: boolean; refused?: boolean };

/** What the header says about who can see a collection: one line naming them, and one
 *  saying how many and where their access comes from. */
export interface WhoCanSee {
  who: string;
  how: string;
  /** No one but the person: drawn with a lock. */
  onlyYou: boolean;
}

function plural(n: number, one: string, many: string): string {
  return `${fmtInt(n)} ${n === 1 ? one : many}`;
}

/** Who can see a collection, in words, from the active grants on it and on the parents the
 *  person can read. `me`: whoami's subject; its other spellings come from the grants read.
 *  `above`: a parent missing from the list, in words, whose grants reach it unseen.
 *  `empty`: it holds no documents yet. A read cut short counts "or more", and a parent
 *  whose grants could not be read is named as one; neither concludes "Only you" or that no
 *  one else can see it. */
export function whoCanSee({
  grants,
  me,
  org,
  titleOf,
  above = null,
  empty = false,
}: {
  grants: ReadAt[];
  me: string;
  org: string;
  titleOf: (namespace: string) => string;
  above?: string | null;
  empty?: boolean;
}): WhoCanSee {
  // Each person once, under any of their spellings, by where their access is given nearest
  // to the collection; the person reading is "".
  const from = new Map<string, string>();
  const keyOf = new Map<string, string>();
  // The reader under every spelling a grant read here ties to them.
  const mine = mySpellings(
    me,
    grants.flatMap((g) => g.rows),
  );
  for (const at of grants) {
    for (const r of at.rows) {
      if (r.status !== "active") continue;
      const spellings = grantSpellings(r);
      // "" is the reader's key: a spelling already keyed to them stays theirs.
      const key = namesMe(r, mine)
        ? ""
        : (spellings.map((s) => keyOf.get(s)).find((k) => k !== undefined) ?? r.subject);
      for (const s of spellings) keyOf.set(s, key);
      if (!from.has(key)) from.set(key, at.namespace);
    }
  }
  const cut = grants.some((g) => g.cut);
  // The parents whose grants reach it unseen: one whose grants failed to read, then the
  // first one missing from the list. Each says who it is and its notice.
  const unseen = [
    ...grants
      .filter((g) => g.failed)
      .map((g) => ({
        who: titleOf(g.namespace),
        text: g.refused
          ? `Who can see ${titleOf(g.namespace)} isn't shown to you`
          : `Also everyone who can see ${titleOf(g.namespace)}, whose members couldn't be read`,
      })),
    ...(above ? [{ who: above, text: `Also everyone who can see ${above}` }] : []),
  ];
  const ownNs = grants[0]?.namespace ?? "";
  const fromTitles = [...new Set([...from.values()].filter((ns) => ns !== ownNs))].map(titleOf);
  const subjects = [...from.keys()];
  const others = subjects.filter((s) => s !== "");
  const inheritedFrom = fromTitles.length > 0 ? `Inherited from ${andList(fromTitles)}` : "";
  const tail = [inheritedFrom, ...unseen.map((u) => u.text)];
  const shared = empty ? "Anything added here is shared with them" : "";
  if (others.includes("everyone")) {
    // Only where the grant to everyone comes from: nothing when it is on the collection itself.
    const at = from.get("everyone");
    const via = at && at !== ownNs ? `Inherited from ${titleOf(at)}` : "";
    return { who: `Everyone in ${org}`, how: [via, shared].filter(Boolean).join(" · "), onlyYou: false };
  }
  if (others.length === 0 && unseen.length === 0 && !cut) {
    return {
      who: subjects.length > 0 ? "Only you" : "No one is named on it",
      how: "Not shared with anyone",
      onlyYou: subjects.length > 0,
    };
  }
  const groups = others.filter((s) => s.startsWith("group:"));
  const agents = others.filter(isAgentSubject);
  const people = others.filter((s) => !groups.includes(s) && !agents.includes(s));
  const you = subjects.includes("");
  const names = [...(you ? ["You"] : []), ...[...groups, ...people, ...agents].map((x) => subjectLabel(x))];
  const head = names.slice(0, 3);
  const more = names.length - head.length;
  // Named by no grant the person can read: the first parent unseen says who, and the line
  // keeps the others' notices.
  if (names.length === 0) {
    const [through, ...rest] = unseen;
    return {
      who: through ? `Everyone who can see ${through.who}` : "More people than are shown here",
      how: [...rest.map((u) => u.text), shared].filter(Boolean).join(" · "),
      onlyYou: false,
    };
  }
  const who = cut
    ? `${head.join(", ")} and more`
    : more > 0
      ? `${head.join(", ")} and ${fmtInt(more)} more`
      : andList(head);
  // Counted here only: grants.list folds a person's spellings within one place, not across the lineage.
  const here = new Set<string>();
  for (const r of grants[0]?.rows ?? []) {
    if (r.status === "active") here.add(namesMe(r, mine) ? "" : r.subject);
  }
  const hereSubjects = [...here];
  const hereGroups = hereSubjects.filter((x) => x.startsWith("group:")).length;
  // The reader ("") is an agent when their own subject is one (a service account's token).
  const hereAgents = hereSubjects.filter((x) => isAgentSubject(x === "" ? me : x)).length;
  const herePeople = hereSubjects.length - hereGroups - hereAgents;
  const counts = andList(
    [
      herePeople > 0 ? plural(herePeople, "person", "people") : "",
      hereGroups > 0 ? plural(hereGroups, "group", "groups") : "",
      hereAgents > 0 ? plural(hereAgents, "agent", "agents") : "",
    ].filter(Boolean),
  );
  const counted = counts ? `${counts} here${grants[0]?.cut ? ", or more" : ""}` : "";
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
 * as it closes, if it changed one.
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
  // The dialog closed because one of its parent links opened another place: the focus goes
  // to that page's heading, not back to Share, which the arrival could not do while the
  // dialog held it.
  const leftFor = useRef(false);
  // The dialog wrote a grant since it opened: its close reads the grants again.
  const changed = useRef(false);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    // A move closes the dialog: what it changed is read on the next place anyway.
    setOpenFor(null);
    changed.current = false;
  }, [place]);
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
  // With sign-in off every request is served, so the grants say nothing of who can see it.
  const signInOff = isAuthDisabled();
  const readsNoGrants = !collection || collection.personal || signInOff;
  useAbortableEffect(
    (signal, live) => {
      if (readsNoGrants) return;
      setReading(true);
      // The collection's own grants decide the line; a parent's failure is named, not fatal.
      const [own, ...parents] = chain.map((c) => c.namespace);
      Promise.all([
        allGrants({ org, namespace: own }, signal),
        ...parents.map((namespace) =>
          allGrants({ org, namespace }, signal).catch(
            (e): ReadAt => ({
              namespace,
              rows: [],
              failed: true,
              refused: e instanceof ApiError && e.status === 403,
            }),
          ),
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
    [org, chainKey, readsNoGrants, version],
  );

  const titleOf = (ns: string) => {
    const c = byName.get(ns);
    return c ? distinctTitle(c, repeated) : ns;
  };
  const answer = state?.key === key ? state : null;
  const read = answer && "grants" in answer ? answer.grants : null;
  const hiddenWords = above ? hiddenWhat(truncated) : null;

  let line: ReactNode = null;
  if (collection && signInOff) {
    line = <Words icon="users" who="Everyone who can reach this server" how="sign-in is off" />;
  } else if (collection?.personal) {
    line = <Words icon="lock" who="Only you" how="Your own files: they can't be shared" />;
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
              focusViewHeading();
              setState(null);
              setVersion((n) => n + 1);
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
      me: whoami?.subject ?? "",
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
        note: at.refused ? `Who can see ${titleOf(at.namespace)} isn't shown to you.` : undefined,
        cut: at.cut,
        onOpen: () => {
          leftFor.current = true;
          onOpenPlace(at.namespace);
        },
      });
    }
    if (hiddenWords) {
      inherited.push({
        namespace: "",
        title: hiddenWords,
        rows: null,
        note: truncated
          ? "Who can see that collection wasn't read: the list of collections was cut short."
          : "Who can see that collection isn't shown to you.",
      });
    }
  } else if (answer && "error" in answer && (chain.length > 1 || hiddenWords)) {
    // The header's read failed: the parents are there, but who they let in is unknown, in
    // the header's words (a refusal is not a failure).
    const refused = answer.error instanceof ApiError && answer.error.status === 403;
    inherited.push({
      namespace: "",
      title: "the collections it sits in",
      rows: null,
      note: refused ? "Who else can see it isn't shown to you." : "Who else can see it couldn't be read.",
    });
  }
  const shown = collection && !collection.personal && openFor === collection.namespace ? collection : null;
  const dialog = shown && (
    <MembersDialog
      open
      onOpenChange={(v) => {
        if (v) return;
        setOpenFor(null);
        // Read the grants again only when the dialog changed one.
        if (changed.current) setVersion((n) => n + 1);
        changed.current = false;
      }}
      onChanged={() => {
        changed.current = true;
      }}
      org={org}
      namespace={shown.namespace}
      // A title, with the address beneath it; an untitled collection is named by its address once.
      label={shown.title?.trim() || undefined}
      // The collection's role decides, not a token claim: none reaches a collection.
      manage={shown.role === "admin"}
      // [] with sign-in off or no parents; null until the header answers; else what it read.
      inherited={signInOff || (chain.length === 1 && !hiddenWords) ? [] : answer === null ? null : inherited}
      onCloseAutoFocus={(e) => {
        if (!leftFor.current) return;
        leftFor.current = false;
        e.preventDefault();
        focusViewHeading();
      }}
    />
  );
  return { line, button, dialog };
}

function Line({ children, busy = false }: { children: ReactNode; busy?: boolean }) {
  return (
    // Its words wrap in its own column, so the figures beside it keep their line. A read
    // again keeps them, marked busy.
    // biome-ignore lint/a11y/useSemanticElements: a named group of words, not form controls, so not a fieldset
    <div
      role="group"
      aria-label="Who can see it"
      aria-busy={busy || undefined}
      className="flex min-w-0 flex-1 basis-64 items-center gap-1.5"
    >
      {children}
    </div>
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
 *  the members dialog, which gives a non-admin its list with its controls unavailable. On a phone,
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
