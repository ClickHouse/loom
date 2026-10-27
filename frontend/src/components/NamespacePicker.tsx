import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Popover } from "@clickhouse/click-ui/Popover";
import { setSavedNamespace, isAuthDisabled } from "../api";
import { invalidateShared } from "../lib/sharedReads";
import { useStore } from "../lib/state";
import { WILDCARD_NAMESPACE } from "../lib/namespace";
import { Loading } from "./ui";
import { PopoverTrigger } from "./ui/Menu";
import { useClamped } from "./ui/TextClamp";
import { TipTarget } from "./ui/TipTarget";
import { Tip } from "./ui/Tooltip";
// The create dialog loads when first opened.
const CreateNamespaceDialog = lazy(() =>
  import("../flows/CreateNamespaceDialog").then((m) => ({ default: m.CreateNamespaceDialog })),
);
// The open panel (search, list, footer) is drawn only once the picker opens: its code loads
// on idle after first paint, or at that first open.
const loadList = () => import("./NamespacePickerList");
const NamespacePickerList = lazy(() => loadList().then((m) => ({ default: m.NamespacePickerList })));

/**
 * Namespace picker — searchable, sectioned, click-to-open. A flat hover-dropdown works for
 * ~3 namespaces and is unusable past ~20: the open panel (`NamespacePickerList`) sections
 * and groups them.
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
    return <StaticNamespace name={identity?.namespace ?? "local"} />;
  }
  return <NamespacePickerMenu />;
}

/** The one namespace, not a control. A cut name is focusable, whole in a tooltip. */
function StaticNamespace({ name }: { name: string }) {
  const [el, setEl] = useState<HTMLElement | null>(null);
  const cut = useClamped(el, name);
  return (
    <div className="inline-flex items-center gap-1.5 max-w-full min-w-0 h-8 px-3 text-sm text-text">
      <span className="text-text-muted">ns</span>
      <TipTarget ref={setEl} tip={name} disabled={!cut} className="font-medium truncate rounded-sm">
        {name}
      </TipTarget>
    </div>
  );
}

function NamespacePickerMenu() {
  const whoami = useStore((s) => s.whoami);
  const identity = useStore((s) => s.identity);
  const setIdentity = useStore((s) => s.setIdentity);
  const pickerRequested = useStore((s) => s.namespacePickerOpen);
  const openNamespacePicker = useStore((s) => s.openNamespacePicker);
  const stored = useStore((s) => s.myNamespaces);
  const [open, setOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [nameEl, setNameEl] = useState<HTMLElement | null>(null);

  // The panel's code loads on idle after first paint, so the first open draws it at once.
  useEffect(() => {
    const go = () =>
      void loadList().catch(() => {
        /* the open's Suspense boundary surfaces a failed chunk */
      });
    // lib.dom declares it, but Safari and jsdom still lack it.
    if (typeof window.requestIdleCallback === "function") {
      const idle = window.requestIdleCallback(go, { timeout: 1500 });
      return () => window.cancelIdleCallback(idle);
    }
    const timer = setTimeout(go, 400);
    return () => clearTimeout(timer);
  }, []);

  // The no-namespace landing asks for the picker through the store.
  useEffect(() => {
    if (!pickerRequested) return;
    setOpen(true);
    openNamespacePicker(false);
  }, [pickerRequested, openNamespacePicker]);

  const current = identity?.namespace || "—";
  // A cut name is whole in a tooltip on the trigger, so hover and keyboard focus both show it.
  const cut = useClamped(nameEl, current);
  const isCode = stored?.find((r) => r.namespace === current)?.kind === "code";
  // `*` is an access wildcard, never a namespace to switch into.
  const allowed = (whoami?.namespaces || []).filter((ns) => ns !== WILDCARD_NAMESPACE);
  if (!whoami || (allowed.length === 0 && !stored)) return null;

  const onPick = (ns: string) => {
    if (!identity) return;
    setIdentity({ ...identity, namespace: ns });
    setSavedNamespace(ns);
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Tip content={current} disabled={!cut || open}>
        <PopoverTrigger>
          <button
            ref={triggerRef}
            type="button"
            className="inline-flex items-center gap-1.5 max-w-full min-w-0 h-8 px-2 sm:px-3 rounded-sm hover:bg-surface-2 transition-colors text-sm text-text"
            aria-label={`Namespace: ${current}${isCode ? ", code index" : ""}`}
          >
            {isCode ? (
              <Icon name="code" size="sm" aria-hidden="true" />
            ) : (
              <span className="text-text-muted" aria-hidden="true">
                ns
              </span>
            )}
            <span ref={setNameEl} className="font-medium truncate">
              {current}
            </span>
            {isCode && <Badge text="code" state="neutral" size="sm" aria-hidden="true" />}
            <Icon name="chevron-down" size="xs" aria-hidden="true" />
          </button>
        </PopoverTrigger>
      </Tip>
      <Popover.Content
        align="start"
        aria-label="Namespaces"
        // The search field takes the focus on open, at once: a timer would pull it back from
        // a row someone already arrowed to.
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          inputRef.current?.focus();
        }}
        onCloseAutoFocus={(e) => {
          e.preventDefault();
          triggerRef.current?.focus();
        }}
        className="w-[360px] max-w-[calc(100vw-16px)] max-h-[480px] flex flex-col gap-2"
      >
        <Suspense fallback={<Loading label="Loading namespaces" />}>
          <NamespacePickerList
            inputRef={inputRef}
            triggerRef={triggerRef}
            onPick={onPick}
            onClose={() => setOpen(false)}
            onCreate={() => {
              setOpen(false);
              setCreateOpen(true);
            }}
          />
        </Suspense>
      </Popover.Content>
      {createOpen && (
        <Suspense fallback={null}>
          <CreateNamespaceDialog
            open={createOpen}
            onOpenChange={setCreateOpen}
            onCreated={(ns) => {
              // Switch to the just-created namespace; the next open reads list_mine afresh,
              // with the new admin grant.
              if (!identity) return;
              setIdentity({ ...identity, namespace: ns });
              setSavedNamespace(ns);
              invalidateShared("mine:");
            }}
          />
        </Suspense>
      )}
    </Popover>
  );
}
