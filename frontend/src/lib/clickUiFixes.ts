// Click UI 0.13's Select option list leaves three things unnamed: its filter field, the
// clear-filter button beside it (named after its icon, "cross") and the dialog that holds
// them (axe: label, aria-dialog-name). The list is portalled and rendered on open, so the
// names are added as it appears. Delete once Click UI names them.
const SEARCH = '[data-testid="select-search-input"]';
const CLEAR = '[data-testid="select-search-close"]';

function nameSelectLists(root: ParentNode): void {
  for (const input of root.querySelectorAll(SEARCH)) {
    if (!input.hasAttribute("aria-label")) input.setAttribute("aria-label", "Filter options");
    const dialog = input.closest('[role="dialog"]');
    if (dialog && !dialog.hasAttribute("aria-label")) dialog.setAttribute("aria-label", "Options");
  }
  for (const clear of root.querySelectorAll(CLEAR)) clear.setAttribute("aria-label", "Clear filter");
}

export function labelSelectSearch(root: HTMLElement = document.body): () => void {
  const observer = new MutationObserver(() => nameSelectLists(root));
  observer.observe(root, { childList: true, subtree: true });
  return () => observer.disconnect();
}
