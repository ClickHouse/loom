/** Each exact name a finding or draft says in words, to copy. A build that sends none (`refs` absent or empty) shows nothing. */
export function ExactNames({ refs }: { refs?: string[] }) {
  if (!refs || refs.length === 0) return null;
  return (
    <p className="text-xs text-text-muted" data-testid="exact-names">
      Exact names:{" "}
      {refs.map((r, k) => (
        <span key={r}>
          {k > 0 && ", "}
          <code className="font-mono break-all">{r}</code>
        </span>
      ))}
    </p>
  );
}
