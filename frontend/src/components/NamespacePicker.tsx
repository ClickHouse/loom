import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Popover } from "@clickhouse/click-ui/Popover";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { listMine, setSavedNamespace, cancelNamespaceRequest, isAuthDisabled, type MyNamespaceEntry } from "../api";
import { invalidateShared, sharedMine } from "../lib/sharedReads";
import { useStore } from "../lib/state";
import { WILDCARD_NAMESPACE } from "../lib/namespace";
import { isSystemKind } from "../lib/format";
import { toast, toastError } from "./Toast";
import { confirmAction } from "./ConfirmDialog";
import { Tip } from "./ui";
import { PopoverTrigger } from "./ui/Menu";
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
      <div className="inline-flex items-center gap-1.5 h-8 px-3 text-sm text-text">
        <span className="text-text-subtle">ns</span>
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
  const triggerRef = useRef<HTMLButtonElement | null>(null);

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
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger>
        <button
          ref={triggerRef}
          type="button"
          className="inline-flex items-center gap-1.5 max-w-full min-w-0 h-8 px-2 sm:px-3 rounded-sm hover:bg-surface-2 transition-colors text-sm text-text"
          aria-label={`Namespace: ${current}`}
        >
          <span className="text-text-subtle" aria-hidden="true">
            ns
          </span>
          <span className="font-medium truncate">{current}</span>
          <Icon name="chevron-down" size="xs" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <Popover.Content
        align="start"
        onCloseAutoFocus={(e) => {
          e.preventDefault();
          triggerRef.current?.focus();
        }}
        className="w-[360px] max-w-[calc(100vw-16px)] max-h-[480px] flex flex-col gap-2"
      >
        <SearchField
          aria-label="Search namespaces"
          ref={inputRef}
          value={query}
          onChange={(v) => setQuery(v)}
          placeholder="Search namespaces…"
        />

        <div className="flex-1 overflow-y-auto -mx-2">
          {known === null && <div className="text-xs text-text-muted p-3 text-center">Loading…</div>}
          {known != null && filtered.length === 0 && (
            <div className="text-xs text-text-muted p-3 text-center">
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
            <Button
              type="empty"
              htmlType="button"
              iconLeft="globe"
              align="left"
              fillWidth
              onClick={() => {
                setOpen(false);
                useStore.getState().setView("browse");
              }}
            >
              Find public namespaces to join in Spaces
            </Button>
          )}
        </div>

        <div className="flex flex-col items-stretch border-t border-border pt-2 -mx-2">
          {systemHiddenCount > 0 && (
            <Button type="empty" htmlType="button" align="left" onClick={() => setShowSystem(true)}>
              Show {systemHiddenCount} system / bench namespace{systemHiddenCount === 1 ? "" : "s"}
            </Button>
          )}
          {showSystem && (
            <Button type="empty" htmlType="button" align="left" onClick={() => setShowSystem(false)}>
              Hide system namespaces
            </Button>
          )}
          {/* Browsing and joining spaces is part of picking one,
              so the Spaces page is reached from here, not from the rail. One
              call to action: the empty list above already carries it. */}
          {known != null && (joined.length > 0 || transient.length > 0 || !!query) && (
            <Button
              type="empty"
              htmlType="button"
              iconLeft="globe"
              align="left"
              onClick={() => {
                setOpen(false);
                setView("browse");
              }}
            >
              Browse and join spaces
            </Button>
          )}
          <Button
            type="empty"
            htmlType="button"
            iconLeft="plus"
            align="left"
            onClick={() => {
              setOpen(false);
              setCreateOpen(true);
            }}
          >
            Create namespace
          </Button>
        </div>
      </Popover.Content>
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
    </Popover>
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
      <div className="section-label px-2 pt-1 pb-0.5">
        {title} <span className="tabular">({rows.length})</span>
      </div>
      {rows.map((r) => {
        const active = r.namespace === current;
        const pendingRole = r.pending_role;
        return (
          <div
            key={r.namespace}
            className={`flex items-center gap-1 rounded-sm transition-colors ${active ? "bg-surface-3" : "hover:bg-surface-2"}`}
          >
            <button
              type="button"
              onClick={() => onPick(r.namespace)}
              aria-current={active ? "true" : undefined}
              className="flex-1 min-w-0 text-left flex items-center gap-2 px-2 py-1.5 rounded-sm text-text"
            >
              <RoleIcon role={r.role} />
              <span className="flex-1 min-w-0">
                <span className="text-sm truncate flex items-center gap-1.5">
                  <span className="truncate">{r.namespace}</span>
                  {(r.kind === "code" || r.kind === "qa") && <Badge text={r.kind} size="sm" />}
                </span>
                {r.description && <span className="text-2xs text-text-muted truncate block">{r.description}</span>}
              </span>
              <VisibilityBadge v={r.visibility} />
            </button>
            {pendingRole && (
              <Tip
                content={
                  r.pending_admins && r.pending_admins.length > 0
                    ? `${r.pending_role} request awaiting approval from: ${r.pending_admins.join(", ")} — click to withdraw`
                    : `${r.pending_role} request awaiting admin approval — click to withdraw`
                }
              >
                <Button
                  type="secondary"
                  htmlType="button"
                  onClick={() => onCancelPending(r.namespace, pendingRole)}
                  className="shrink-0 mr-1"
                >
                  {`pending ${r.pending_role}`}
                </Button>
              </Tip>
            )}
          </div>
        );
      })}
    </div>
  );
}

function RoleIcon({ role }: { role: MyNamespaceEntry["role"] }) {
  if (role === "admin") {
    return <Icon name="star" size="xs" color="var(--color-warn)" aria-label="You're admin" />;
  }
  return <Icon name="user" size="xs" color="var(--color-text-subtle)" aria-label={role} />;
}

function VisibilityBadge({ v }: { v: MyNamespaceEntry["visibility"] }) {
  if (v === "public") {
    return <Icon name="globe" size="xs" color="var(--color-text-subtle)" aria-label="Public" />;
  }
  return <Icon name="lock" size="xs" color="var(--color-text-subtle)" aria-label="Private" />;
}
