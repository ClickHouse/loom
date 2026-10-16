// Click UI 0.13's Select draws a filter field in its option list with no label (axe:
// label). The list is portalled and rendered on open, so a label is added as it appears.
// Delete once Click UI names the field.
const SELECT_SEARCH = '[data-testid="select-search-input"]:not([aria-label])';

export function labelSelectSearch(root: Node = document.body): () => void {
  const label = () => {
    for (const input of document.querySelectorAll(SELECT_SEARCH)) input.setAttribute("aria-label", "Filter options");
  };
  const observer = new MutationObserver(label);
  observer.observe(root, { childList: true, subtree: true });
  return () => observer.disconnect();
}
