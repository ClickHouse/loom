import { Suspense, lazy, useEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { Avatar } from "@clickhouse/click-ui/Avatar";
import { Button } from "@clickhouse/click-ui/Button";
import { Dropdown } from "@clickhouse/click-ui/Dropdown";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { useStore } from "../lib/state";
import { isProxyMode, signOut } from "../api";
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
import { IconButton } from "../components/IconButton";
import { Kbd, Unavailable } from "../components/ui";
import { READ_ONLY_TIP, canWrite, useNamespaceRole } from "../lib/namespaceRole";
import { MenuTrigger } from "../components/ui/Menu";
import { paletteShortcut } from "../lib/platform";

/**
 * Header — top chrome of every page.
 *
 * The "New" and account menus are Click UI `Dropdown` menus (Radix menu semantics):
 * click, Enter or Space opens; arrow keys move between items; Escape closes and returns
 * focus to the trigger.
 *
 * Below `md` the search input leaves the first row: a search button reveals it on its own
 * full-width row and the action cluster wraps instead of overlapping, so at 390 px the
 * search and the actions never overlap. "New" keeps its word at every width, and is
 * disabled with a tooltip when the caller's role on the namespace is viewer.
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
    toast(signOut() ? "Signed out" : "Another account is signed in here", "info");
    setTimeout(() => location.reload(), 300);
  };

  return (
    <header className="min-h-14 border-b border-border bg-surface flex flex-wrap md:flex-nowrap items-center gap-x-2 gap-y-1.5 md:gap-3 px-3 py-2 md:px-5 md:py-0 shrink-0">
      {/* Left cluster — namespace context. Capped so a long namespace truncates
          instead of squeezing the search. */}
      <div className="shrink min-w-0 max-w-[60%] md:min-w-24 md:max-w-[min(320px,24vw)]">
        <RichNamespacePicker />
      </div>

      {/* Vertical separator between context and search to make the
          two regions feel distinct without a heavy border. */}
      <div className="hidden md:block h-6 w-px bg-border shrink-0" aria-hidden />

      {/* Center cluster — search grows to fill available space but
          caps so the right cluster stays anchored at the edge on wide
          screens. On phones it is the last child and takes a full row of
          its own, shown only after the search button reveals it. */}
      <div
        id="header-search"
        className={`min-w-0 order-last basis-full md:order-none md:basis-auto md:flex-1 md:min-w-18 lg:min-w-40 md:max-w-[680px] ${mobileSearch ? "" : "hidden md:block"}`}
      >
        <DeferredHeaderSearch autoFocus={mobileSearch} />
      </div>
      <div className="flex-1" aria-hidden />

      {/* Right cluster — primary action + user. Height-aligned at 32px to
          match the search bar and namespace picker; wraps when it must. */}
      <div className="flex flex-wrap items-center justify-end gap-1.5 md:gap-3 shrink-0">
        <IconButton
          icon="search"
          label="Search"
          onClick={() => setMobileSearch((v) => !v)}
          aria-expanded={mobileSearch}
          aria-controls="header-search"
          className="md:hidden"
        />

        {identity &&
          (readOnly ? (
            <DisabledNew reason={READ_ONLY_TIP} />
          ) : (
            <Menu
              label="New"
              trigger={
                <Button type="secondary" htmlType="button" iconLeft="plus">
                  New
                </Button>
              }
            >
              <Dropdown.Item onSelect={() => setAddMemOpen(true)}>
                <ItemLabel icon="sparkle">Memory</ItemLabel>
              </Dropdown.Item>
              <Dropdown.Item onSelect={() => setAddDocOpen(true)}>
                <ItemLabel icon="document">Document</ItemLabel>
              </Dropdown.Item>
            </Menu>
          ))}

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
        <IconButton
          icon="question"
          label="Keyboard shortcuts"
          tooltip="Keyboard shortcuts · ?"
          onClick={() => openHelp(true)}
        />

        {whoami && (
          <Menu
            label={`Signed in as ${whoami.subject}, org ${whoami.org}`}
            trigger={
              <button
                type="button"
                aria-label={`Account: ${whoami.subject}`}
                className="inline-flex items-center gap-2 h-8 pl-1 pr-2 rounded-sm hover:bg-surface-2 data-[state=open]:bg-surface-2 transition-colors"
              >
                <Avatar text={(whoami.subject || "?").charAt(0)} aria-hidden="true" />
                <span className="text-sm text-text hidden lg:inline truncate max-w-[140px]" aria-hidden="true">
                  {whoami.subject}
                </span>
              </button>
            }
          >
            {/* No menu item, so a screen reader in the menu never reads it: the menu's name says it. */}
            <Dropdown.Group aria-hidden="true">
              <div className="px-4 py-2">
                <div className="text-xs text-text-muted">Signed in as</div>
                <div className="text-sm font-medium text-text truncate">{whoami.subject}</div>
                <div className="text-xs text-text-muted mt-0.5 truncate">{whoami.org}</div>
              </div>
            </Dropdown.Group>
            <Dropdown.Item onSelect={() => useStore.getState().setView("setup")}>
              <ItemLabel icon="gear">Settings</ItemLabel>
            </Dropdown.Item>
            {!isProxyMode() && (
              <Dropdown.Item onSelect={onLogout}>
                <ItemLabel icon="slide-out">Sign out</ItemLabel>
              </Dropdown.Item>
            )}
          </Menu>
        )}
      </div>
    </header>
  );
}

/** A header menu: the trigger button and its labelled items. */
function Menu({ label, trigger, children }: { label: string; trigger: ReactElement; children: ReactNode }) {
  return (
    <Dropdown modal={false}>
      <MenuTrigger>{trigger}</MenuTrigger>
      {/* Radix names the menu after its trigger with aria-labelledby, which outranks
          aria-label; dropping it lets the label name the menu. */}
      <Dropdown.Content align="end" aria-label={label} aria-labelledby={undefined}>
        {children}
      </Dropdown.Content>
    </Dropdown>
  );
}

/** A menu item's icon and label. Not Dropdown.Item's `icon`: Click UI names that svg
 *  after the icon ("sparkle Memory"), so the item's spoken name would carry it. */
function ItemLabel({ icon, children }: { icon: IconName; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2">
      <Icon name={icon} size="sm" aria-hidden="true" />
      {children}
    </span>
  );
}

/** "New" for a read-only role: unavailable, with the reason. */
function DisabledNew({ reason }: { reason: string }) {
  return (
    <Unavailable reason={reason}>
      <Button type="secondary" htmlType="button" iconLeft="plus">
        New
      </Button>
    </Unavailable>
  );
}

/** The same input in the same place, before the search body has loaded (HeaderSearch
 *  draws the same SearchField with the ⌘K / Ctrl K hint over its end while empty):
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
    <div className="group relative w-full">
      <SearchField
        ref={ref}
        aria-label="Search Loom"
        placeholder="Search profile, memories, library…"
        value={draft}
        onChange={(v) => onDraft(v)}
        tabIndex={view === "graph" ? -1 : undefined}
      />
      {/* The hint leaves once the field has the focus or any text. */}
      {!draft && (
        <Kbd className="absolute right-2.5 top-1/2 -translate-y-1/2 pointer-events-none hidden sm:inline-flex sm:group-focus-within:hidden">
          {paletteShortcut()}
        </Kbd>
      )}
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
  // The real box takes the focus only if the placeholder still holds it when it arrives: a
  // keyboard user who tabbed on while it loaded keeps the focus where they put it.
  const placeholder = (
    <div
      onFocusCapture={() => {
        setWantFocus(true);
        setReady(true);
      }}
      onBlurCapture={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setWantFocus(false);
      }}
    >
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
    </div>
  );
  if (!ready) return placeholder;
  return (
    <Suspense fallback={placeholder}>
      <HeaderSearch autoFocus={autoFocus || wantFocus} initialQuery={draft} />
    </Suspense>
  );
}
