import { Suspense, lazy, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import * as Popover from "@radix-ui/react-popover";
import { LogOut, Plus, FileText, Sparkles, HelpCircle, Search, Settings as SettingsIcon } from "lucide-react";
import { useStore } from "../lib/state";
import { clearToken, isProxyMode } from "../api";
import { toast } from "../components/Toast";
import { NamespacePicker as RichNamespacePicker } from "../components/NamespacePicker";
// The shell evaluates what it shows. The two dialogs open on a click and the search box's
// 14 kB body renders on a keystroke; loaded statically they would be a fifth of the index
// chunk, evaluated on every page's boot (the dialogs are also mounted only while open).
// They load when first opened; the search body is fetched on idle after first paint, or
// on the first focus.
const AddMemoryDialog = lazy(() => import("../flows/AddMemoryDialog").then((m) => ({ default: m.AddMemoryDialog })));
const AddDocumentDialog = lazy(() =>
  import("../flows/AddDocumentDialog").then((m) => ({ default: m.AddDocumentDialog })),
);
const loadHeaderSearch = () => import("../components/HeaderSearch");
const HeaderSearch = lazy(() => loadHeaderSearch().then((m) => ({ default: m.HeaderSearch })));
import { PendingApprovalsBell } from "../components/PendingApprovalsBell";
import { TaskInboxBell } from "../components/TaskInboxBell";
import { Tip } from "../components/ui";
import { READ_ONLY_TIP, canWrite, useNamespaceRole } from "../lib/namespaceRole";

/**
 * Header — top chrome of every page.
 *
 * The "New" and account menus are Radix `Popover` menus on `<button>`
 * triggers: click, Enter or Space opens; arrow keys move between items; Escape closes and
 * returns focus to the trigger. The earlier CSS `group-hover` menus opened on hover only,
 * which no keyboard or touch user could reach — and the ⌘K palette did not carry create
 * or sign-out commands to compensate.
 *
 * Below `md` the search input leaves the first row: a search button reveals it on its own
 * full-width row, the action cluster wraps instead of overlapping, and "New" drops its
 * word, so at 390 px the search and the actions never overlap. "New" is disabled with a
 * tooltip when the caller's role on the namespace is viewer.
 */
export function Header() {
  const whoami = useStore((s) => s.whoami);
  const identity = useStore((s) => s.identity);
  const openHelp = useStore((s) => s.openHelp);
  const [addMemOpen, setAddMemOpen] = useState(false);
  const [addDocOpen, setAddDocOpen] = useState(false);
  const [mobileSearch, setMobileSearch] = useState(false);
  const role = useNamespaceRole(identity?.namespace);
  const readOnly = !canWrite(role);

  const onLogout = () => {
    clearToken();
    toast("Signed out", "info");
    setTimeout(() => location.reload(), 300);
  };

  return (
    <header className="min-h-14 border-b border-[var(--color-border)] bg-[var(--color-surface)] flex flex-wrap md:flex-nowrap items-center gap-x-2 gap-y-1.5 md:gap-3 px-3 py-2 md:px-5 md:py-0 shrink-0">
      {/* Left cluster — namespace context. */}
      <div className="shrink min-w-0 max-w-[60%] md:max-w-none md:shrink-0">
        <RichNamespacePicker />
      </div>

      {/* Vertical separator between context and search to make the
          two regions feel distinct without a heavy border. */}
      <div className="hidden md:block h-6 w-px bg-[var(--color-border)] shrink-0" aria-hidden />

      {/* Center cluster — search grows to fill available space but
          caps so the right cluster stays anchored at the edge on wide
          screens. On phones it is the last child and takes a full row of
          its own, shown only after the search button reveals it. */}
      <div
        id="header-search"
        className={`min-w-0 order-last basis-full md:order-none md:basis-auto md:flex-1 md:max-w-[680px] ${mobileSearch ? "" : "hidden md:block"}`}
      >
        <DeferredHeaderSearch autoFocus={mobileSearch} />
      </div>
      <div className="flex-1" aria-hidden />

      {/* Right cluster — primary action + user. Height-aligned at 32px to
          match the search bar and namespace picker; wraps when it must. */}
      <div className="flex flex-wrap items-center justify-end gap-1.5 md:gap-3 shrink-0">
        <button
          type="button"
          onClick={() => setMobileSearch((v) => !v)}
          aria-label="Search"
          aria-expanded={mobileSearch}
          aria-controls="header-search"
          className="md:hidden h-8 w-8 inline-flex items-center justify-center text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] rounded-md transition-colors"
        >
          <Search size={15} aria-hidden="true" />
        </button>

        {identity && (
          <MenuPopover
            label="New"
            disabled={readOnly}
            disabledTip={READ_ONLY_TIP}
            triggerClassName="inline-flex items-center gap-1.5 h-8 px-2.5 sm:px-3 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] data-[state=open]:bg-[var(--color-accent-strong)] transition-colors shadow-sm disabled:opacity-50 disabled:cursor-not-allowed"
            trigger={
              <>
                <Plus size={13} aria-hidden="true" /> <span className="hidden sm:inline">New</span>
              </>
            }
            align="end"
          >
            {(close) => (
              <>
                <button
                  role="menuitem"
                  onClick={() => {
                    close();
                    setAddMemOpen(true);
                  }}
                  className={MENU_ITEM_LG}
                >
                  <Sparkles size={13} /> Memory
                </button>
                <button
                  role="menuitem"
                  onClick={() => {
                    close();
                    setAddDocOpen(true);
                  }}
                  className={MENU_ITEM_LG}
                >
                  <FileText size={13} /> Document
                </button>
              </>
            )}
          </MenuPopover>
        )}

        {/* A dialog is mounted while it is open. Mounted closed, its hooks and
            element tree would run on every page's boot — about 42 ms of main
            thread at 4x CPU for nothing on the screen. */}
        {identity && addMemOpen && (
          <Suspense fallback={null}>
            <AddMemoryDialog
              open={addMemOpen}
              onOpenChange={setAddMemOpen}
              identity={identity}
              onAdded={() => useStore.getState().bumpMemoriesVersion()}
            />
          </Suspense>
        )}
        {identity && addDocOpen && (
          <Suspense fallback={null}>
            <AddDocumentDialog
              open={addDocOpen}
              onOpenChange={setAddDocOpen}
              identity={{ org: identity.org, namespace: identity.namespace }}
              onAdded={() => useStore.getState().bumpMemoriesVersion()}
            />
          </Suspense>
        )}

        {/* Pending-approvals bell: surfaces grant requests on
            namespaces the user is admin of. Counts updated every 30s. */}
        <PendingApprovalsBell />
        {/* Task inbox bell: review items and blocked tasks in
            this namespace's inboxes. One task.counts call per two minutes. */}
        <TaskInboxBell />

        {/* Keyboard shortcut + general help. Opens HelpDialog mounted
            in App.tsx (state lives in the store). */}
        <button
          onClick={() => openHelp(true)}
          title="Keyboard shortcuts (press ?)"
          aria-label="Keyboard shortcuts"
          className="h-8 w-8 inline-flex items-center justify-center text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] rounded-md transition-colors"
        >
          <HelpCircle size={15} />
        </button>

        {whoami && (
          <MenuPopover
            label={`Account: ${whoami.subject}`}
            triggerClassName="inline-flex items-center gap-2 h-8 pl-1 pr-2 rounded-md hover:bg-[var(--color-surface-2)] data-[state=open]:bg-[var(--color-surface-2)] transition-colors"
            trigger={
              <>
                <span
                  className="w-6 h-6 rounded-full bg-[var(--color-accent)] grid place-items-center text-[var(--color-on-accent)] text-xs font-semibold shrink-0"
                  aria-hidden="true"
                >
                  {(whoami.subject || "?").charAt(0).toUpperCase()}
                </span>
                <span
                  className="text-sm text-[var(--color-text)] hidden lg:inline truncate max-w-[140px]"
                  aria-hidden="true"
                >
                  {whoami.subject}
                </span>
              </>
            }
            align="end"
          >
            {(close) => (
              <>
                <div className="px-2 py-1.5" role="presentation">
                  <div className="text-xs text-[var(--color-text-muted)]">Signed in as</div>
                  <div className="text-sm font-medium text-[var(--color-text)] truncate">{whoami.subject}</div>
                  <div className="text-xs text-[var(--color-text-muted)] mt-0.5 truncate">{whoami.org}</div>
                </div>
                <div className="h-px bg-[var(--color-border)] my-1" role="separator" />
                <button
                  role="menuitem"
                  onClick={() => {
                    close();
                    useStore.getState().setView("setup");
                  }}
                  className={MENU_ITEM}
                >
                  <SettingsIcon size={12} /> Setup & MCP config
                </button>
                {!isProxyMode() && (
                  <button
                    role="menuitem"
                    onClick={() => {
                      close();
                      onLogout();
                    }}
                    className={MENU_ITEM}
                  >
                    <LogOut size={12} /> Sign out
                  </button>
                )}
              </>
            )}
          </MenuPopover>
        )}
      </div>
    </header>
  );
}

const MENU_ITEM =
  "w-full flex items-center gap-2 px-2 py-1.5 text-sm text-[var(--color-text)] hover:bg-[var(--color-surface-2)] focus-visible:bg-[var(--color-surface-2)] rounded cursor-pointer outline-none";
const MENU_ITEM_LG =
  "w-full text-left flex items-center gap-2 px-2.5 py-2 text-base rounded text-[var(--color-text)] hover:bg-[var(--color-surface-2)] focus-visible:bg-[var(--color-surface-2)] outline-none";

/**
 * Click-to-open menu on a Radix Popover. The trigger is a real `<button>`
 * with `aria-haspopup="menu"` (Radix supplies `aria-expanded`); the panel
 * is `role="menu"` and moves focus between its `role="menuitem"` children
 * with ArrowUp/ArrowDown (wrapping), Home and End. Radix portals the
 * panel, focuses its first item on open, closes on Escape or an outside
 * click, and returns focus to the trigger. Children receive `close` so an
 * item can shut the menu before it opens a dialog.
 */
function MenuPopover({
  label,
  trigger,
  triggerClassName,
  children,
  align = "start",
  disabled = false,
  disabledTip,
}: {
  label: string;
  trigger: ReactNode;
  triggerClassName: string;
  children: (close: () => void) => ReactNode;
  align?: "start" | "end";
  /** Render the trigger disabled; `disabledTip` explains why on hover and
   *  focus (a disabled button fires no pointer events, so the tooltip hangs
   *  off a focusable wrapper). */
  disabled?: boolean;
  disabledTip?: string;
}) {
  const [open, setOpen] = useState(false);
  const close = () => setOpen(false);

  if (disabled) {
    const btn = (
      <button type="button" aria-haspopup="menu" aria-label={label} disabled className={triggerClassName}>
        {trigger}
      </button>
    );
    if (!disabledTip) return btn;
    return (
      <Tip content={disabledTip}>
        <span
          tabIndex={0}
          className="inline-flex rounded-md outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
          aria-label={`${label}: ${disabledTip}`}
        >
          {btn}
        </span>
      </Tip>
    );
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const items = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])'));
    if (items.length === 0) return;
    e.preventDefault();
    const idx = items.findIndex((i) => i === document.activeElement);
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? items.length - 1
          : e.key === "ArrowDown"
            ? (idx + 1) % items.length
            : (idx - 1 + items.length) % items.length;
    items[next]?.focus();
  };

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button type="button" aria-haspopup="menu" aria-label={label} className={triggerClassName}>
          {trigger}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          role="menu"
          aria-label={label}
          align={align}
          sideOffset={4}
          onKeyDown={onKeyDown}
          className="z-50 min-w-[200px] p-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] shadow-xl animate-fade-in motion-reduce:animate-none! outline-none"
        >
          {children(close)}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

const SEARCH_INPUT_CLASS =
  "w-full h-8 pl-8 pr-14 text-sm bg-[var(--color-surface-2)] border border-[var(--color-border)] rounded-md outline-none focus:border-[var(--color-accent)] focus:bg-[var(--color-surface)] focus:ring-2 focus:ring-[var(--color-accent)]/15 transition-colors placeholder:text-[var(--color-text-subtle)]";

/** The same input in the same place, before the search body has loaded:
 *  it takes focus and keystrokes (kept as the draft the real box mounts
 * with), and carries the Graph view's tab-order rule. */
function SearchPlaceholder({
  draft,
  onDraft,
  focus,
  view,
}: {
  draft: string;
  onDraft: (v: string) => void;
  focus: boolean;
  view: string;
}) {
  const ref = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (focus) ref.current?.focus();
  }, [focus]);
  return (
    <div className="relative w-full">
      <Search
        size={13}
        className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-subtle)] pointer-events-none"
      />
      <input
        ref={ref}
        aria-label="Search Loom"
        placeholder="Search profile, memories, library…"
        className={SEARCH_INPUT_CLASS}
        value={draft}
        onChange={(e) => onDraft(e.target.value)}
        tabIndex={view === "graph" ? -1 : undefined}
      />
      <kbd className="absolute right-2.5 top-1/2 -translate-y-1/2 font-mono text-2xs px-1.5 py-0.5 bg-[var(--color-bg)] border border-[var(--color-border)] rounded text-[var(--color-text-subtle)] pointer-events-none hidden sm:inline">
        ⌘K
      </kbd>
    </div>
  );
}

/** The search box's input is on the screen at boot; its body (state, reads, the result
 *  panel — 14 kB, a tenth of the index chunk) loads on idle after first paint, or at once
 *  on the first focus, keystroke or ⌘K, and mounts with the focus and draft the person
 *  gave the placeholder. */
function DeferredHeaderSearch({ autoFocus }: { autoFocus: boolean }) {
  const view = useStore((s) => s.view);
  const [ready, setReady] = useState(autoFocus);
  const [wantFocus, setWantFocus] = useState(autoFocus);
  const [draft, setDraft] = useState("");
  useEffect(() => {
    if (autoFocus) {
      setWantFocus(true);
      setReady(true);
    }
  }, [autoFocus]);
  useEffect(() => {
    if (ready) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let idle: number | null = null;
    const go = () => {
      void loadHeaderSearch().then(() => setReady(true));
    };
    // lib.dom declares it, but Safari and jsdom still lack it.
    if (typeof window.requestIdleCallback === "function") idle = window.requestIdleCallback(go, { timeout: 1500 });
    else timer = setTimeout(go, 400);
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setWantFocus(true);
        setReady(true);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (timer) clearTimeout(timer);
      if (idle !== null && typeof window.cancelIdleCallback === "function") window.cancelIdleCallback(idle);
    };
  }, [ready]);
  const placeholder = (
    <SearchPlaceholder
      draft={draft}
      focus={wantFocus}
      view={view}
      onDraft={(v) => {
        setDraft(v);
        setWantFocus(true);
        setReady(true);
      }}
    />
  );
  if (!ready) {
    return (
      <div
        onFocusCapture={() => {
          setWantFocus(true);
          setReady(true);
        }}
      >
        {placeholder}
      </div>
    );
  }
  return (
    <Suspense fallback={placeholder}>
      <HeaderSearch autoFocus={autoFocus || wantFocus} initialQuery={draft} />
    </Suspense>
  );
}
