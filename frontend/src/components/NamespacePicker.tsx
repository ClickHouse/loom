import { Suspense, lazy, useEffect, useRef, useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import { ChevronDown, Search, Lock, Globe, Crown, UserRound, X, Plus } from "lucide-react";
import { listMine, setSavedNamespace, cancelNamespaceRequest, isAuthDisabled, type MyNamespaceEntry } from "../api";
import { invalidateShared, sharedMine } from "../lib/sharedReads";
import { useStore } from "../lib/state";
import { WILDCARD_NAMESPACE } from "../lib/namespace";
import { isSystemKind } from "../lib/format";
import { toast, toastError } from "./Toast";
import { confirmAction } from "./ConfirmDialog";
// The create dialog loads when first opened.
const CreateNamespaceDialog = lazy(() =>
  import("../flows/CreateNamespaceDialog").then((m) => ({ default: m.CreateNamespaceDialog })),
);

/**
 * Namespace picker — searchable, sectioned, click-to-open.
 *
 * A flat hover-dropdown works for ~3 namespaces and is unusable past ~20. Sections by
 * role (you own → joined → other), sort by recency within section, badges for visibility,
 * and a hide- system-namespaces toggle so bench artefacts don't drown out a user's actual
 * namespaces.
 */
export function NamespacePicker() {
  const identity = useStore((s) => s.identity);

  // Auth-disabled mode serves exactly one namespace (LOOM_DEFAULT_NAMESPACE):
  // there is nothing to switch to (list_mine has no grants/JWT to build a
  // list from), and a pick would persist server-side to `loom.user_state` —
  // which would silently retarget MCP/SDK writes while the dashboard snapped back to the
  // default. Render the namespace as static context. The menu is its own component so
  // every hook in it runs unconditionally.
  if (isAuthDisabled()) {
    return (
      <div className="inline-flex items-center gap-1.5 px-3 py-1.5 text-base text-[var(--color-text)]">
        <span className="text-[var(--color-text-subtle)]">ns</span>
        <span className="font-medium">{identity?.namespace ?? "local"}</span>
      </div>
    );
  }
  return <NamespacePickerMenu />;
}

function NamespacePickerMenu() {
  const whoami = useStore((s) => s.whoami);
  const identity = useStore((s) => s.identity);
  const setIdentity = useStore((s) => s.setIdentity);
  const setView = useStore((s) => s.setView);
  const pickerRequested = useStore((s) => s.namespacePickerOpen);
  const openNamespacePicker = useStore((s) => s.openNamespacePicker);
  // The rows the app already fetched at boot (App.tsx) seed the list until the picker's
  // own read lands, so a token whose access is grants only (no claim) has a picker at all
  // — and its code/QA rows carry their kind tag from the first paint.
  const stored = useStore((s) => s.myNamespaces);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [showSystem, setShowSystem] = useState(false);
  const [rows, setRows] = useState<MyNamespaceEntry[] | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Lazy-load on first open. Refetch when identity.org changes (rare
  // but happens when a multi-tenant user switches orgs upstream).
  useEffect(() => {
    if (!open || !identity) return;
    // The shared read — the header, the banner and the search palette ask the same
    // question on the same page.
    sharedMine(identity.org)
      .then(setRows)
      .catch(() => setRows([]));
  }, [open, identity?.org]);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 50);
    else setQuery("");
  }, [open]);

  // The no-namespace landing asks for the picker through the store.
  useEffect(() => {
    if (!pickerRequested) return;
    setOpen(true);
    openNamespacePicker(false);
  }, [pickerRequested, openNamespacePicker]);

  const current = identity?.namespace || "—";
  // `*` is an access wildcard, never a namespace to switch into.
  const allowed = (whoami?.namespaces || []).filter((ns) => ns !== WILDCARD_NAMESPACE);

  // Fallback: when list_mine returns nothing (single-user with no
  // memberships, just JWT access), synthesize entries from the
  // whoami.namespaces list so the picker has SOMETHING to show.
  const synthFromJwt: MyNamespaceEntry[] = allowed.map((ns) => ({
    namespace: ns,
    role: "jwt" as const,
    visibility: "private" as const,
    description: "",
    joined_at: null,
    is_new: false,
  }));

  const known = rows ?? stored;
  const allRows = known && known.length > 0 ? known : known === null ? null : synthFromJwt;

  // A system / bench namespace: the server's `kind` when it sends one
  // (qa, bench — kept on the namespaces row); the name-shape heuristic only for rows
  // without a kind. Bench-shaped names look like "dx-a1b2c3" (2–4 letters + hex) or
  // "test-*", "snap-*", etc. A tracker namespace the user holds a claim for is not
  // hidden: the Explorer's Patterns mode lives on it and on the code namespaces bound to
  // it; only bench artefacts fold under "system".
  const isSystemShaped = (r: MyNamespaceEntry): boolean => {
    if (r.kind) return isSystemKind(r.kind) && r.kind !== "qa";
    return (
      /^[a-z]{2,4}-[a-f0-9]{6,}$/.test(r.namespace) ||
      /^(test|snap|src|trace|mb|sh|kbd|onboard|bx|ax|cx|dx)-/.test(r.namespace)
    );
  };

  const filtered = (allRows || []).filter((r) => {
    // kind=code rows are indexed repos (see the Code section) and kind=qa
    // rows are tracker records: reference data, never the landing default, but a claim on
    // one is pickable — the Explorer's Patterns mode reads them. `*` is not a namespace
    // at all.
    if (r.namespace === WILDCARD_NAMESPACE) return false;
    if (!showSystem && isSystemShaped(r) && r.namespace !== current) return false;
    if (query) {
      const q = query.toLowerCase();
      return r.namespace.toLowerCase().includes(q) || (r.description || "").toLowerCase().includes(q);
    }
    return true;
  });

  const owned = filtered.filter((r) => r.role === "admin");
  const joined = filtered.filter((r) => r.role === "writer" || r.role === "viewer");
  const transient = filtered.filter((r) => r.role === "jwt");

  // Sort each group: current selection first, then most-recently-joined.
  const sort = (arr: MyNamespaceEntry[]) =>
    [...arr].sort((a, b) => {
      if (a.namespace === current) return -1;
      if (b.namespace === current) return 1;
      const at = a.joined_at ? Date.parse(a.joined_at) : 0;
      const bt = b.joined_at ? Date.parse(b.joined_at) : 0;
      return bt - at;
    });

  const systemHiddenCount = !showSystem
    ? (allRows || []).filter((r) => isSystemShaped(r) && r.namespace !== current).length
    : 0;

  if (!whoami || (allowed.length === 0 && !known)) return null;

  const onPick = (ns: string) => {
    if (!identity) return;
    setIdentity({ ...identity, namespace: ns });
    setSavedNamespace(ns);
    setOpen(false);
  };

  const onCancelPending = async (ns: string, role: string) => {
    if (!identity) return;
    if (
      !(await confirmAction({
        title: `Withdraw your ${role} request on ${ns}?`,
        message: "Keeps any current access; the request just leaves the admin queue.",
        confirmLabel: "Withdraw",
      }))
    )
      return;
    try {
      await cancelNamespaceRequest(identity.org, ns);
      toast(`Withdrew ${role} request`, "ok");
      invalidateShared("mine:");
      listMine(identity.org)
        .then(setRows)
        .catch(() => undefined);
    } catch (err) {
      toastError("Cancel failed", err);
    }
  };

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          className="inline-flex items-center gap-1.5 max-w-full min-w-0 px-2 sm:px-3 py-1.5 rounded-lg hover:bg-[var(--color-surface-2)] transition-colors text-base text-[var(--color-text)]"
          aria-label={`Namespace: ${current}`}
        >
          <span className="text-[var(--color-text-subtle)]" aria-hidden="true">
            ns
          </span>
          <span className="font-medium truncate">{current}</span>
          <ChevronDown size={12} className="text-[var(--color-text-subtle)] shrink-0" aria-hidden="true" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={4}
          className="z-50 w-[360px] max-h-[480px] flex flex-col rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] shadow-2xl animate-fade-in"
        >
          <div className="relative p-2 border-b border-[var(--color-border)]">
            <Search
              size={13}
              className="absolute left-4 top-1/2 -translate-y-1/2 text-[var(--color-text-muted)] pointer-events-none"
              aria-hidden="true"
            />
            <input
              aria-label="Search namespaces"
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search namespaces…"
              className="w-full pl-8 pr-7 py-1.5 text-sm rounded-md bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none"
            />
            {query && (
              <button
                onClick={() => setQuery("")}
                className="absolute right-3 top-1/2 -translate-y-1/2 p-0.5 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                aria-label="Clear"
              >
                <X size={12} />
              </button>
            )}
          </div>

          <div className="flex-1 overflow-y-auto p-1.5">
            {known === null && <div className="text-xs text-[var(--color-text-muted)] p-3 text-center">Loading…</div>}
            {known != null && filtered.length === 0 && (
              <div className="text-xs text-[var(--color-text-muted)] p-3 text-center">
                {query ? `No matches for "${query}"` : "No namespaces available."}
              </div>
            )}
            <Section
              title="Your namespaces"
              rows={sort(owned)}
              current={current}
              onPick={onPick}
              onCancelPending={onCancelPending}
            />
            <Section
              title="Joined"
              rows={sort(joined)}
              current={current}
              onPick={onPick}
              onCancelPending={onCancelPending}
            />
            <Section
              title="From your token"
              rows={sort(transient)}
              current={current}
              onPick={onPick}
              onCancelPending={onCancelPending}
            />
            {known != null && joined.length === 0 && transient.length === 0 && !query && (
              <button
                onClick={() => {
                  setOpen(false);
                  useStore.getState().setView("browse");
                }}
                className="w-full mt-1 px-3 py-2 text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] rounded-md text-left inline-flex items-center gap-2"
              >
                <Globe size={11} />
                Find public namespaces to join in Spaces →
              </button>
            )}
          </div>

          {systemHiddenCount > 0 && (
            <button
              onClick={() => setShowSystem(true)}
              className="border-t border-[var(--color-border)] p-2 text-2xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] text-center"
            >
              Show {systemHiddenCount} system / bench namespace{systemHiddenCount === 1 ? "" : "s"}
            </button>
          )}
          {showSystem && (
            <button
              onClick={() => setShowSystem(false)}
              className="border-t border-[var(--color-border)] p-2 text-2xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] text-center"
            >
              Hide system namespaces
            </button>
          )}

          {/* Browsing and joining spaces is part of picking one,
              so the Spaces page is reached from here, not from the rail. One
              call to action: the empty list above already carries it. */}
          {known != null && (joined.length > 0 || transient.length > 0 || !!query) && (
            <button
              onClick={() => {
                setOpen(false);
                setView("browse");
              }}
              className="border-t border-[var(--color-border)] px-3 py-2.5 text-sm text-[var(--color-text)] hover:bg-[var(--color-surface-2)] inline-flex items-center gap-2"
            >
              <Globe size={13} /> Browse and join spaces
            </button>
          )}
          <button
            onClick={() => {
              setOpen(false);
              setCreateOpen(true);
            }}
            className="border-t border-[var(--color-border)] px-3 py-2.5 text-sm text-[var(--color-text)] hover:bg-[var(--color-surface-2)] inline-flex items-center gap-2"
          >
            <Plus size={13} /> Create namespace
          </button>
        </Popover.Content>
      </Popover.Portal>
      {createOpen && (
        <Suspense fallback={null}>
          <CreateNamespaceDialog
            open={createOpen}
            onOpenChange={setCreateOpen}
            onCreated={(ns) => {
              // Auto-switch to the just-created namespace + refresh
              // list_mine so the picker reflects the new admin grant
              // immediately, not on next open.
              if (identity) {
                setIdentity({ ...identity, namespace: ns });
                setSavedNamespace(ns);
                invalidateShared("mine:");
                listMine(identity.org)
                  .then(setRows)
                  .catch(() => setRows([]));
              } else {
                setRows(null);
              }
            }}
          />
        </Suspense>
      )}
    </Popover.Root>
  );
}

function Section({
  title,
  rows,
  current,
  onPick,
  onCancelPending,
}: {
  title: string;
  rows: MyNamespaceEntry[];
  current: string;
  onPick: (ns: string) => void;
  onCancelPending: (ns: string, role: string) => void;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="mb-2 last:mb-0">
      <div className="px-2 pt-1 pb-0.5 text-2xs font-semibold uppercase tracking-wider text-[var(--color-text-subtle)]">
        {title} <span className="opacity-60 tabular">({rows.length})</span>
      </div>
      {rows.map((r) => {
        const active = r.namespace === current;
        const pendingRole = r.pending_role;
        return (
          <div
            key={r.namespace}
            className={`group flex items-stretch gap-1 rounded-md transition-colors ${
              active ? "bg-[var(--color-accent-soft)]" : "hover:bg-[var(--color-surface-2)]"
            }`}
          >
            <button
              onClick={() => onPick(r.namespace)}
              className={`flex-1 text-left flex items-center gap-2 px-2 py-1.5 rounded-md ${
                active ? "text-[var(--color-accent)]" : "text-[var(--color-text)]"
              }`}
            >
              <RoleIcon role={r.role} />
              <span className="flex-1 min-w-0">
                <span className="font-medium text-sm truncate block">
                  {r.namespace}
                  {(r.kind === "code" || r.kind === "qa") && (
                    <span className="ml-1.5 align-middle text-2xs font-normal rounded px-1 py-px border border-border text-text-muted">
                      {r.kind}
                    </span>
                  )}
                </span>
                {r.description && (
                  <span className="text-2xs text-[var(--color-text-muted)] truncate block">{r.description}</span>
                )}
              </span>
              {pendingRole && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    e.preventDefault();
                    onCancelPending(r.namespace, pendingRole);
                  }}
                  className="text-2xs px-1.5 py-0.5 rounded bg-warn/15 text-warn shrink-0 hover:bg-warn/15"
                  title={
                    r.pending_admins && r.pending_admins.length > 0
                      ? `${r.pending_role} request awaiting approval from: ${r.pending_admins.join(", ")} — click to withdraw`
                      : `${r.pending_role} request awaiting admin approval — click to withdraw`
                  }
                >
                  pending {r.pending_role}
                </button>
              )}
              <VisibilityBadge v={r.visibility} />
            </button>
          </div>
        );
      })}
    </div>
  );
}

function RoleIcon({ role }: { role: MyNamespaceEntry["role"] }) {
  if (role === "admin") {
    return <Crown size={12} className="text-[var(--color-warn)] shrink-0" aria-label="You're admin" />;
  }
  return <UserRound size={12} className="text-[var(--color-text-subtle)] shrink-0" aria-label={role} />;
}

function VisibilityBadge({ v }: { v: MyNamespaceEntry["visibility"] }) {
  if (v === "public") {
    return <Globe size={11} className="text-[var(--color-text-subtle)] shrink-0" aria-label="Public" />;
  }
  return <Lock size={11} className="text-[var(--color-text-subtle)] shrink-0" aria-label="Private" />;
}
