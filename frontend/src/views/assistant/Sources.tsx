import { Link } from "@clickhouse/click-ui/Link";
import type { Citation } from "../../api/assistantChat";
import { useStore } from "../../lib/state";

/**
 * The citations of one finished answer, as the server checked them against Loom with the
 * answer's token. A verified memory or document opens where the dashboard shows it; an
 * unverified citation shows why. An answer with no verified citation is marked
 * unsupported, as an abstention is, since nothing in Loom backs what it says.
 */
export function Sources({ citations }: { citations: Citation[] }) {
  const inspect = useStore((s) => s.inspect);
  const openDocument = useStore((s) => s.openDocument);
  const verified = citations.filter((c) => c.verified);
  return (
    <div className="mt-2 space-y-1" data-testid="assistant-sources">
      {verified.length === 0 && (
        <p className="text-xs text-warn" role="note">
          Unsupported: no citation in this answer was verified against Loom.
        </p>
      )}
      {citations.length > 0 && (
        <ul className="space-y-1 text-xs" aria-label="Sources">
          {citations.map((c) => (
            <li key={c.text} className="break-words">
              <span className="text-text-muted">{c.verified ? "Verified" : "Not verified"} · </span>
              {c.verified && c.ref && (c.kind === "memory" || c.kind === "doc") ? (
                <Link
                  component="button"
                  type="button"
                  size="sm"
                  // A cited key may be any writer's: the wildcard reads it whichever agent,
                  // user or session it is filed under.
                  onClick={() => (c.kind === "memory" ? inspect(c.ref, { agent: "*" }) : openDocument(c.ref))}
                >
                  {label(c)}
                </Link>
              ) : (
                <span className={c.verified ? "text-text" : "text-text-muted"}>{label(c)}</span>
              )}
              {!c.verified && c.reason && <span className="text-text-muted"> ({c.reason})</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function label(c: Citation): string {
  if (c.kind === "sql") {
    // The statement's first line tells one query of the answer from another.
    const first =
      c.text
        .replace(/^```sql\s*/i, "")
        .trim()
        .split("\n")[0] ?? "";
    return `SQL query: ${first.length > 80 ? `${first.slice(0, 79)}…` : first}`;
  }
  return c.text.replace(/^\[|\]$/g, "");
}
