// Click UI 0.13's Select option list leaves three things unnamed: its filter field, the
// clear-filter button beside it (named after its icon, "cross") and the dialog that holds
// them (axe: label, aria-dialog-name). The list is portalled and rendered on open, so the
// names are added as its nodes are. Delete once Click UI names them.
const SEARCH = '[data-testid="select-search-input"]';
const CLEAR = '[data-testid="select-search-close"]';

function within(node: Element, selector: string): Element[] {
  return node.matches(selector) ? [node] : [...node.querySelectorAll(selector)];
}

function nameSelectList(node: Element): void {
  for (const input of within(node, SEARCH)) {
    if (!input.hasAttribute("aria-label")) input.setAttribute("aria-label", "Filter options");
    const dialog = input.closest('[role="dialog"]');
    if (dialog && !dialog.hasAttribute("aria-label")) dialog.setAttribute("aria-label", "Options");
  }
  for (const clear of within(node, CLEAR)) clear.setAttribute("aria-label", "Clear filter");
}

export function labelSelectSearch(root: HTMLElement = document.body): () => void {
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) if (node instanceof Element) nameSelectList(node);
    }
  });
  observer.observe(root, { childList: true, subtree: true });
  return () => observer.disconnect();
}

// Click UI 0.13's Button sets `disabled` while `loading`, and a focused button that is
// disabled drops its focus to the body: a keyboard user who presses "Run audit" is sent
// back to the top of the page. Two corrections share one record of the control the user
// last pressed (by pointer, or Enter/Space): a dialog that click opens returns focus
// there (ui/Modal), and a button that loads in place takes focus back when it is enabled
// again, if focus is still on the body. Inside a dialog the focus waits on the dialog
// itself meanwhile: on the body, Tab would leave the dialog for the page behind it.
// Delete once Click UI keeps a loading button focusable.
const PRESSABLE = "button, a[href], [role='button'], [role='menuitem'], [role='tab']";
let pressed: HTMLElement | null = null;

// A press on anything but a control forgets the last one, so a stale button enabled
// later never takes the focus.
function notePress(e: Event): void {
  pressed = e.target instanceof Element ? e.target.closest<HTMLElement>(PRESSABLE) : null;
}

function noteKeyPress(e: KeyboardEvent): void {
  if (e.key === "Enter" || e.key === " ") notePress(e);
}

/** The control the user last pressed, by pointer or by Enter/Space. */
export function lastPressed(): HTMLElement | null {
  return pressed;
}

export function keepFocusThroughLoading(root: HTMLElement = document.body): () => void {
  document.addEventListener("pointerdown", notePress, true);
  document.addEventListener("keydown", noteKeyPress, true);
  const observer = new MutationObserver((records) => {
    for (const { target } of records) {
      if (target !== pressed || !(target instanceof HTMLButtonElement)) continue;
      const active = document.activeElement;
      const dialog = target.closest<HTMLElement>("[role='dialog']");
      if (target.disabled) {
        if (dialog && (active === target || active === null || active === document.body)) dialog.focus();
      } else if (active === null || active === document.body || active === dialog) {
        target.focus();
        pressed = null;
      }
    }
  });
  observer.observe(root, { attributes: true, attributeFilter: ["disabled"], subtree: true });
  return () => {
    observer.disconnect();
    document.removeEventListener("pointerdown", notePress, true);
    document.removeEventListener("keydown", noteKeyPress, true);
  };
}
