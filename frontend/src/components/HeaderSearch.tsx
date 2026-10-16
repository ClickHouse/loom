import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import * as RadixPopover from "@radix-ui/react-popover";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Popover } from "@clickhouse/click-ui/Popover";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import {
  listMemories,
  browseNamespaces,
  listKnowledge,
  listProfile,
  searchMemory,
  setSavedNamespace,
  type MemoryListEntry,
  type MyNamespaceEntry,
  type BrowseEntry,
  type KnowledgeEntry,
  type ProfileEntry,
  type SearchHit,
} from "../api";
import { sharedMine } from "../lib/sharedReads";
import { useStore, viewHref } from "../lib/state";
import { useAbortableEffect } from "../lib/hooks";
import { relativeTime } from "../lib/format";
import { Kbd } from "./ui";

/** The memory group is server-side memory.search: a debounced call per settled query,
 *  aborted by the next keystroke. */
const MEMORY_TOP_K = 8;
const MEMORY_DEBOUNCE_MS = 200;
const MEMORY_MIN_CHARS = 2;

interface NsRow {
  namespace: string;
  visibility: string;
  description: string;
  member_count?: number;
  joined: boolean;
}

/** `autoFocus`: the phone header hides this input behind a search button; when that
 *  button reveals it, focus lands here so the palette opens without a second tap. */
export function HeaderSearch({
  autoFocus = false,
  initialQuery = "",
}: {
  autoFocus?: boolean;
  initialQuery?: string;
} = {}) {
  const identity = useStore((s) => s.identity);
  const setIdentity = useStore((s) => s.setIdentity);
  const setView = useStore((s) => s.setView);
  const inspect = useStore((s) => s.inspect);
  const inspectProfile = useStore((s) => s.inspectProfile);
  const openDoc = useStore((s) => s.openDocument);
  const view = useStore((s) => s.view);

  // The draft typed into the placeholder before this body loaded; the box opens on it as
  // if typed here.
  const [q, setQ] = useState(initialQuery);
  const [open, setOpen] = useState(initialQuery.length > 0);
  // Three newest rows for the empty-state "Recent memories" group; the
  // memory group itself comes from memory.search below.
  const [recent, setRecent] = useState<MemoryListEntry[]>([]);
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [docs, setDocs] = useState<KnowledgeEntry[]>([]);
  const [profileSections, setProfileSections] = useState<ProfileEntry[]>([]);
  const [nsRows, setNsRows] = useState<NsRow[]>([]);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listboxId = useId();
  const optionId = (i: number) => `${listboxId}-${i}`;

  // The catalog groups (docs, profile, recents; namespaces) load on the first
  // open per scope rather than on every namespace switch. A load that is cut short
  // (palette closed mid-flight) clears its stamp so the next open retries.
  const catalogFor = useRef<string | null>(null);
  const nsFor = useRef<string | null>(null);

  useAbortableEffect(
    (signal, live) => {
      if (!open || !identity) return;
      const key = `${identity.org}/${identity.namespace}`;
      if (catalogFor.current === key) return;
      catalogFor.current = key;
      const retry = () => {
        if (!live() && catalogFor.current === key) catalogFor.current = null;
      };
      listKnowledge({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((r) => {
          if (live()) setDocs(r.rows || []);
        })
        .catch(() => {
          retry();
          if (live()) setDocs([]);
        });
      listProfile(identity, { signal })
        .then((r) => {
          if (live()) setProfileSections(r.entries);
        })
        .catch(() => {
          retry();
          if (live()) setProfileSections([]);
        });
      listMemories(identity, 3, { signal })
        .then((r) => {
          if (live()) setRecent(r.entries);
        })
        .catch(() => {
          retry();
          if (live()) setRecent([]);
        });
    },
    [open, identity?.org, identity?.namespace],
  );

  useAbortableEffect(
    (signal, live) => {
      if (!open || !identity) return;
      const key = identity.org;
      if (nsFor.current === key) return;
      nsFor.current = key;
      Promise.all([
        sharedMine(identity.org).catch(() => [] as MyNamespaceEntry[]),
        browseNamespaces(identity.org, { limit: 100, signal }).catch(() => [] as BrowseEntry[]),
      ]).then(([mineIn, browseIn]) => {
        if (!live()) {
          if (nsFor.current === key) nsFor.current = null;
          return;
        }
        let mine = mineIn;
        let browse = browseIn;
        // Repositories (kind = code) are not memory identities: they live in
        // the Repositories section and never appear as a namespace to switch
        // into or join from here.
        mine = mine.filter((m) => m.kind !== "code");
        browse = browse.filter((b) => b.kind !== "code");
        const mineSet = new Set(mine.map((m) => m.namespace));
        const all: NsRow[] = mine.map((m) => ({
          namespace: m.namespace,
          visibility: m.visibility || "private",
          description: m.description || "",
          joined: true,
        }));
        for (const b of browse) {
          if (mineSet.has(b.namespace)) continue;
          all.push({
            namespace: b.namespace,
            visibility: "public",
            description: b.description || "",
            member_count: b.member_count,
            joined: false,
          });
        }
        setNsRows(all);
      });
    },
    [open, identity?.org],
  );

  useEffect(() => {
    if (!autoFocus) return;
    inputRef.current?.focus();
    setOpen(true);
  }, [autoFocus]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
        setOpen(true);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // A view can ask the palette to open with a hint typed in ( the Setup checklist's "Find
  // it again" step). Honour it once, then clear.
  const paletteRequest = useStore((s) => s.paletteRequest);
  const clearPaletteRequest = useStore((s) => s.clearPaletteRequest);
  useEffect(() => {
    if (!paletteRequest) return;
    setQ(paletteRequest.hint);
    inputRef.current?.focus();
    inputRef.current?.select();
    setOpen(true);
    clearPaletteRequest();
  }, [paletteRequest, clearPaletteRequest]);

  // Count opens for the same checklist step, whichever way it opened.
  const notePaletteOpened = useStore((s) => s.notePaletteOpened);
  useEffect(() => {
    if (open) notePaletteOpened();
  }, [open, notePaletteOpened]);

  const parsed = useMemo(() => {
    const tokens = q.trim().split(/\s+/).filter(Boolean);
    const tags: string[] = [];
    const namespaces: string[] = [];
    const typeFilters: Array<"profile" | "memory" | "doc"> = [];
    const rest: string[] = [];
    for (const tok of tokens) {
      const mTag = /^tag:(.+)$/i.exec(tok);
      const mNs = /^namespace:(.+)$/i.exec(tok);
      const mType = /^type:(profile|memory|doc|document)$/i.exec(tok);
      if (mTag) tags.push(mTag[1].toLowerCase());
      else if (mNs) namespaces.push(mNs[1].toLowerCase());
      else if (mType) {
        const t = mType[1].toLowerCase();
        typeFilters.push(t === "profile" || t === "memory" ? t : "doc");
      } else rest.push(tok.toLowerCase());
    }
    return { tags, namespaces, typeFilters, rest, raw: q.trim() };
  }, [q]);

  const wantsType = (t: "profile" | "memory" | "doc") =>
    parsed.typeFilters.length === 0 || parsed.typeFilters.includes(t);
  const wantsNamespaces = parsed.namespaces.length > 0 || (parsed.typeFilters.length === 0 && parsed.raw.length > 0);

  const PER_GROUP_CAP = 5;

  const matchedNamespaces = useMemo(() => {
    if (!wantsNamespaces) return [];
    const needles = parsed.namespaces.length > 0 ? parsed.namespaces : [parsed.raw.toLowerCase()];
    if (needles.length === 0 || (needles.length === 1 && !needles[0])) return [];
    return nsRows.filter((r) =>
      needles.every((n) => r.namespace.toLowerCase().includes(n) || r.description.toLowerCase().includes(n)),
    );
  }, [nsRows, parsed, wantsNamespaces]);

  const matchedProfile = useMemo(() => {
    if (!wantsType("profile") || !parsed.raw) return [];
    return profileSections.filter((s) => {
      for (const text of parsed.rest) {
        if (!s.label.toLowerCase().includes(text) && !s.value.toLowerCase().includes(text)) return false;
      }
      // tag: doesn't apply to profile sections (no tags on profile).
      // Pure tag: queries shouldn't surface profile.
      if (parsed.rest.length === 0) return false;
      return true;
    });
  }, [profileSections, parsed]);

  // Memory group: server-side semantic search, the same leg the Memories bar
  // uses. Free text is the query; a pure `tag:` query searches the tag word
  // under the tag filter. Below MEMORY_MIN_CHARS nothing is sent.
  const memoryQuery = wantsType("memory") ? (parsed.rest.length > 0 ? parsed.rest : parsed.tags).join(" ") : "";
  const memoryTags = parsed.tags.join("\u0000");
  useAbortableEffect(
    (signal, live) => {
      if (!identity || memoryQuery.length < MEMORY_MIN_CHARS) {
        setHits([]);
        setSearching(false);
        return;
      }
      setSearching(true);
      const tags = memoryTags ? memoryTags.split("\u0000") : [];
      window.setTimeout(() => {
        if (!live()) return;
        searchMemory(identity, memoryQuery, { top_k: MEMORY_TOP_K, tags, signal })
          .then((r) => {
            if (live()) setHits(r.results || []);
          })
          .catch(() => {
            if (live()) setHits([]);
          })
          .finally(() => {
            if (live()) setSearching(false);
          });
      }, MEMORY_DEBOUNCE_MS);
    },
    [identity?.org, identity?.namespace, identity?.agent, memoryQuery, memoryTags],
  );

  const matchedDocuments = useMemo(() => {
    if (!wantsType("doc") || !parsed.raw) return [];
    return docs.filter((d) => {
      if (parsed.tags.length > 0) {
        const tagsLower = (d.tags || []).map((t) => t.toLowerCase());
        for (const want of parsed.tags) {
          if (!tagsLower.some((t) => t.includes(want))) return false;
        }
      }
      const haystack = [d.title || "", d.original_filename || "", d.source_uri || "", d.description || ""]
        .join(" ")
        .toLowerCase();
      for (const text of parsed.rest) {
        if (!haystack.includes(text)) return false;
      }
      if (parsed.rest.length === 0 && parsed.tags.length === 0) return false;
      return true;
    });
  }, [docs, parsed]);

  const namespacesShown = matchedNamespaces.slice(0, 2);
  const profileShown = matchedProfile.slice(0, PER_GROUP_CAP);
  const memoryShown = hits;
  const documentsShown = matchedDocuments.slice(0, PER_GROUP_CAP);

  const switchNs = (ns: string) => {
    if (!identity) return;
    setIdentity({ ...identity, namespace: ns });
    setSavedNamespace(ns);
    setView("memories");
    setOpen(false);
    setQ("");
  };

  const openMemory = (e: Pick<MemoryListEntry, "memory_key" | "agent">) => {
    inspect(e.memory_key, { agent: e.agent });
    setOpen(false);
  };

  const openDocument = (d: KnowledgeEntry) => {
    openDoc(d.source_uri);
    setOpen(false);
    setQ("");
  };

  const openProfileSection = (label: string) => {
    inspectProfile(label);
    setOpen(false);
    setQ("");
  };

  const hasResults =
    namespacesShown.length > 0 || profileShown.length > 0 || memoryShown.length > 0 || documentsShown.length > 0;

  // Flat activation list for ↑↓ keyboard nav.
  type Activation =
    | { kind: "ns"; ns: NsRow }
    | { kind: "profile"; section: ProfileEntry }
    | { kind: "mem"; mem: SearchHit }
    | { kind: "doc"; doc: KnowledgeEntry };
  const activations: Activation[] = useMemo(
    () => [
      ...namespacesShown.map((ns) => ({ kind: "ns" as const, ns })),
      ...profileShown.map((section) => ({ kind: "profile" as const, section })),
      ...memoryShown.map((mem) => ({ kind: "mem" as const, mem })),
      ...documentsShown.map((doc) => ({ kind: "doc" as const, doc })),
    ],
    [namespacesShown, profileShown, memoryShown, documentsShown],
  );

  const [activeIdx, setActiveIdx] = useState(0);
  useEffect(() => {
    setActiveIdx(0);
  }, [q]);

  const popoverOpen = open && (parsed.raw.length > 0 || q.length === 0);
  const listboxShown = popoverOpen && hasResults;
  // The highlighted option stays in view of the scrolling popover as ↑↓ move it.
  useEffect(() => {
    if (listboxShown) document.getElementById(`${listboxId}-${activeIdx}`)?.scrollIntoView?.({ block: "nearest" });
  }, [listboxShown, listboxId, activeIdx]);

  const activate = (i: number) => {
    const a = activations[i];
    if (!a) return;
    if (a.kind === "ns") switchNs(a.ns.namespace);
    else if (a.kind === "profile") openProfileSection(a.section.label);
    else if (a.kind === "mem") openMemory({ memory_key: a.mem.memory_key, agent: a.mem.agent || "" });
    else if (a.kind === "doc") openDocument(a.doc);
  };

  const onInputKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      setOpen(false);
      setActiveIdx(0);
      return;
    }
    if (!hasResults) return;
    if (!popoverOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => Math.min(activations.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      activate(activeIdx);
    }
  };

  const memoryPending = searching && memoryQuery.length >= MEMORY_MIN_CHARS;

  // Index offsets for activeIdx → row mapping.
  const offNs = 0;
  const offProfile = offNs + namespacesShown.length;
  const offMem = offProfile + profileShown.length;
  const offDoc = offMem + memoryShown.length;

  return (
    <Popover open={popoverOpen} onOpenChange={setOpen}>
      <RadixPopover.Anchor asChild>
        <div className="relative w-full">
          <SearchField
            ref={inputRef}
            value={q}
            onChange={(v) => {
              setQ(v);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
            onKeyDown={onInputKeyDown}
            placeholder="Search profile, memories, library…"
            aria-label="Search Loom"
            // WAI-ARIA combobox with a listbox popup: focus stays in the input and
            // aria-activedescendant names the highlighted option.
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={listboxShown}
            aria-controls={listboxId}
            aria-activedescendant={listboxShown && activations[activeIdx] ? optionId(activeIdx) : undefined}
            // On the Graph view the omnibox is the view's own input; this
            // one leaves the tab order there (⌘K still opens it, and the
            // hint beside it says so) so the first input Tab reaches is
            // the entity box, not a look-alike search.
            tabIndex={view === "graph" ? -1 : undefined}
          />
          {!q && (
            <Kbd className="absolute right-2.5 top-1/2 -translate-y-1/2 pointer-events-none hidden sm:inline-flex">
              ⌘K
            </Kbd>
          )}
        </div>
      </RadixPopover.Anchor>
      <Popover.Content
        align="start"
        aria-label="Search results"
        onOpenAutoFocus={(e) => e.preventDefault()}
        className="w-(--radix-popover-trigger-width) max-h-[70vh] overflow-y-auto"
      >
        {q.length === 0 && (
          <EmptyState
            recentMemories={recent.slice(0, 3)}
            profileLabels={profileSections.slice(0, 3)}
            onMemory={openMemory}
            onProfile={openProfileSection}
          />
        )}

        {q.length > 0 && !hasResults && (memoryPending ? <Searching /> : <NoResults q={q} />)}

        {hasResults && (
          <div className="pb-1.5 text-xs text-text-muted flex items-center justify-between">
            <span>
              {[
                matchedNamespaces.length > 0 &&
                  `${matchedNamespaces.length} namespace${matchedNamespaces.length === 1 ? "" : "s"}`,
                matchedProfile.length > 0 && `${matchedProfile.length} profile`,
                matchedDocuments.length > 0 &&
                  `${matchedDocuments.length} document${matchedDocuments.length === 1 ? "" : "s"}`,
              ]
                .filter(Boolean)
                .join(" · ")}
            </span>
            <span className="font-mono">↑↓ navigate · ↵ open</span>
          </div>
        )}

        {hasResults && (
          <div id={listboxId} role="listbox" aria-label="Search results">
            {namespacesShown.length > 0 && (
              <OptionGroup id={`${listboxId}-ns`} icon="folder-closed" label="Jump to" className="mb-1">
                {namespacesShown.map((r, i) => {
                  const idx = offNs + i;
                  return (
                    <a
                      key={r.namespace}
                      id={optionId(idx)}
                      role="option"
                      aria-selected={idx === activeIdx}
                      tabIndex={-1}
                      href={viewHref("memories")}
                      onClick={(e) => {
                        e.preventDefault();
                        switchNs(r.namespace);
                      }}
                      onMouseEnter={() => setActiveIdx(idx)}
                      className={rowClass(idx === activeIdx)}
                    >
                      <div className="flex items-center gap-2">
                        <TypeChip label="namespace" />
                        <span className="font-mono text-sm">{r.namespace}</span>
                        <Badge
                          text={r.visibility}
                          size="sm"
                          state={r.visibility === "public" ? "success" : "default"}
                        />
                        {!r.joined && <span className="text-xs text-text-muted">not joined</span>}
                      </div>
                      {r.description && <div className="text-xs text-text-muted mt-0.5 truncate">{r.description}</div>}
                    </a>
                  );
                })}
              </OptionGroup>
            )}

            {profileShown.length > 0 && (
              <OptionGroup id={`${listboxId}-profile`} icon="user" label="Profile">
                {profileShown.map((s, i) => {
                  const idx = offProfile + i;
                  return (
                    <button
                      type="button"
                      key={s.label}
                      id={optionId(idx)}
                      role="option"
                      aria-selected={idx === activeIdx}
                      tabIndex={-1}
                      onClick={() => openProfileSection(s.label)}
                      onMouseEnter={() => setActiveIdx(idx)}
                      className={rowClass(idx === activeIdx)}
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <TypeChip label="profile" />
                        <span className="font-mono text-sm truncate">{s.label}</span>
                      </div>
                      <div className="text-xs text-text-muted mt-0.5 truncate">{truncateForRow(s.value)}</div>
                    </button>
                  );
                })}
              </OptionGroup>
            )}

            {memoryShown.length > 0 && (
              <OptionGroup id={`${listboxId}-mem`} icon="document" label="Best matches">
                {memoryShown.map((e, i) => {
                  const idx = offMem + i;
                  return (
                    <button
                      type="button"
                      key={`${e.agent || ""}::${e.memory_key}`}
                      id={optionId(idx)}
                      role="option"
                      aria-selected={idx === activeIdx}
                      tabIndex={-1}
                      onClick={() => openMemory({ memory_key: e.memory_key, agent: e.agent || "" })}
                      onMouseEnter={() => setActiveIdx(idx)}
                      className={rowClass(idx === activeIdx)}
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <TypeChip label="memory" />
                        <span className="font-mono text-sm truncate">{e.memory_key}</span>
                        <span className="text-xs text-text-muted shrink-0 ml-auto">
                          {e.memory_type}
                          {e.created_at ? ` · ${relativeTime(e.created_at)}` : ""}
                        </span>
                      </div>
                      <div className="text-xs text-text-muted mt-0.5 truncate">
                        {e.agent ? (
                          <>
                            <span className="text-text-muted">{e.agent}</span> ·{" "}
                          </>
                        ) : null}
                        {e.content_excerpt || "(empty)"}
                      </div>
                    </button>
                  );
                })}
              </OptionGroup>
            )}

            {documentsShown.length > 0 && (
              <OptionGroup id={`${listboxId}-doc`} icon="book" label="Library">
                {documentsShown.map((d, i) => {
                  const idx = offDoc + i;
                  const label = d.title || d.original_filename || d.source_uri;
                  return (
                    <button
                      type="button"
                      key={d.source_uri}
                      id={optionId(idx)}
                      role="option"
                      aria-selected={idx === activeIdx}
                      tabIndex={-1}
                      onClick={() => openDocument(d)}
                      onMouseEnter={() => setActiveIdx(idx)}
                      className={rowClass(idx === activeIdx)}
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <TypeChip label="library" />
                        <span className={`text-sm truncate ${d.title ? "" : "font-mono"}`}>{label}</span>
                        {(d.tags || []).slice(0, 3).map((t) => (
                          <Badge key={t} text={`#${t}`} size="sm" className="shrink-0" />
                        ))}
                      </div>
                      <div className="text-xs text-text-muted mt-0.5 truncate">
                        {d.chunk_count} chunk{d.chunk_count === 1 ? "" : "s"}
                        {d.description ? ` · ${d.description}` : ""}
                      </div>
                    </button>
                  );
                })}
              </OptionGroup>
            )}
          </div>
        )}

        {/* Links to the full views sit after the listbox: they are not options. */}
        {hasResults && (
          <div className="mt-1">
            <MoreLink
              shown={profileShown.length}
              total={matchedProfile.length}
              label="profile section"
              onClick={() => {
                setView("profile");
                setOpen(false);
              }}
            />
            {memoryShown.length > 0 && (
              <Button
                type="empty"
                htmlType="button"
                align="left"
                fillWidth
                iconRight="arrow-right"
                onClick={() => {
                  setView("memories");
                  setOpen(false);
                }}
              >
                Search all memories
              </Button>
            )}
            <MoreLink
              shown={documentsShown.length}
              total={matchedDocuments.length}
              label="document"
              onClick={() => {
                setView("knowledge");
                setOpen(false);
              }}
            />
          </div>
        )}
      </Popover.Content>
    </Popover>
  );
}

function GroupHeader({ icon, label, id }: { icon: IconName; label: string; id?: string }) {
  return (
    <div id={id} role={id ? "presentation" : undefined} className="section-label py-1 flex items-center gap-1">
      <Icon name={icon} size="xs" aria-hidden="true" /> {label}
    </div>
  );
}

/** One labelled group of options inside the results listbox. */
function OptionGroup({
  id,
  icon,
  label,
  className,
  children,
}: {
  id: string;
  icon: IconName;
  label: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: an option group inside a listbox, not form controls, so not a fieldset
    <div role="group" aria-labelledby={id} className={className}>
      <GroupHeader id={id} icon={icon} label={label} />
      {children}
    </div>
  );
}

function TypeChip({ label }: { label: "namespace" | "profile" | "memory" | "library" }) {
  const states: Record<typeof label, BadgeState> = {
    namespace: "default",
    profile: "neutral",
    memory: "info",
    library: "neutral",
  };
  const state = states[label];
  return <Badge text={label} state={state} size="sm" className="shrink-0" />;
}

function MoreLink({
  shown,
  total,
  label,
  pluralLabel,
  onClick,
}: {
  shown: number;
  total: number;
  label: string;
  pluralLabel?: string;
  onClick: () => void;
}) {
  if (total <= shown) return null;
  const extra = total - shown;
  const word = extra === 1 ? label : pluralLabel || `${label}s`;
  return (
    <Button type="empty" htmlType="button" align="left" fillWidth iconRight="arrow-right" onClick={onClick}>
      {`+${extra} more ${word}`}
    </Button>
  );
}

/** A result row bleeds into the popover's padding so its hover fill spans the width. */
const ROW_BLEED = "block text-left -mx-2 w-[calc(100%+1rem)] px-2 rounded-sm cursor-pointer";

function rowClass(isActive: boolean): string {
  return `${ROW_BLEED} py-2 no-underline hover:no-underline text-text ${isActive ? "bg-surface-2" : "hover:bg-surface-2"}`;
}

function EmptyState({
  recentMemories,
  profileLabels,
  onMemory,
  onProfile,
}: {
  recentMemories: MemoryListEntry[];
  profileLabels: ProfileEntry[];
  onMemory: (e: MemoryListEntry) => void;
  onProfile: (label: string) => void;
}) {
  return (
    <div>
      {recentMemories.length > 0 && (
        <div>
          <GroupHeader icon="document" label="Recent memories" />
          {recentMemories.map((e) => (
            <button
              type="button"
              key={`${e.agent}::${e.memory_key}`}
              onClick={() => onMemory(e)}
              className={`${ROW_BLEED} py-1.5 hover:bg-surface-2`}
            >
              <div className="flex items-center gap-2 min-w-0">
                <TypeChip label="memory" />
                <span className="font-mono text-sm truncate">{e.memory_key}</span>
              </div>
            </button>
          ))}
        </div>
      )}
      {profileLabels.length > 0 && (
        <div className="mt-1">
          <GroupHeader icon="user" label="Your profile" />
          {profileLabels.map((s) => (
            <button
              type="button"
              key={s.label}
              onClick={() => onProfile(s.label)}
              className={`${ROW_BLEED} py-1.5 hover:bg-surface-2`}
            >
              <div className="flex items-center gap-2 min-w-0">
                <TypeChip label="profile" />
                <span className="font-mono text-sm truncate">{s.label}</span>
              </div>
            </button>
          ))}
        </div>
      )}
      <div className="section-label pt-2 pb-1 border-t border-border mt-2">Syntax</div>
      <div className="pb-1 space-y-1 text-xs text-text-muted">
        <div className="flex items-baseline gap-2">
          <code className="font-mono text-xs px-1.5 py-0.5 rounded-sm bg-surface-2 text-text">coffee</code>
          <span>match across profile, memory, library</span>
        </div>
        <div className="flex items-baseline gap-2">
          <code className="font-mono text-xs px-1.5 py-0.5 rounded-sm bg-surface-2 text-text">tag:work</code>
          <span>memories + library tagged work (partial OK)</span>
        </div>
        <div className="flex items-baseline gap-2">
          <code className="font-mono text-xs px-1.5 py-0.5 rounded-sm bg-surface-2 text-text">namespace:qa</code>
          <span>jump to a namespace</span>
        </div>
        <div className="flex items-baseline gap-2">
          <code className="font-mono text-xs px-1.5 py-0.5 rounded-sm bg-surface-2 text-text">
            type:profile|memory|doc
          </code>
          <span>constrain to one content type</span>
        </div>
      </div>
    </div>
  );
}

function Searching() {
  return (
    <div className="py-5 text-sm text-text-muted text-center" aria-live="polite">
      Searching…
    </div>
  );
}

function NoResults({ q }: { q: string }) {
  return (
    <div className="py-5 text-sm text-text-muted text-center">
      <div>
        No matches for <span className="font-mono text-text">{q}</span>
      </div>
      <div className="text-xs text-text-muted mt-1.5">
        Try <code className="font-mono">tag:</code>, <code className="font-mono">namespace:</code>, or{" "}
        <code className="font-mono">type:</code>.
      </div>
    </div>
  );
}

function truncateForRow(s: string): string {
  const flat = s.replace(/\s+/g, " ").trim();
  if (flat.length <= 80) return flat;
  return flat.slice(0, 80) + "…";
}
