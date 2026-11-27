export function LoomBrand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2.5 text-text" aria-hidden="true">
      <svg className="workspace-brand-mark" width="32" height="32" viewBox="0 0 40 40" fill="none" aria-hidden="true">
        <path d="M20 6v28M6 20h28" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
        <circle cx="20" cy="6" r="3" fill="currentColor" />
        <circle cx="34" cy="20" r="3" fill="currentColor" />
        <circle cx="20" cy="34" r="3" fill="currentColor" />
        <circle cx="6" cy="20" r="3" fill="currentColor" />
        <circle cx="20" cy="20" r="4" stroke="currentColor" strokeWidth="4" />
      </svg>
      {!compact && <span className="text-xl font-semibold leading-none">Loom</span>}
    </span>
  );
}
