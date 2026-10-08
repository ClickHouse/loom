import { Lock } from "lucide-react";
import type { ScopeRepo, TokenReadScope } from "../lib/tokenScope";

/** "What this token will read and write", shown under the namespace pick
 * before the token is minted. The pick is one namespace; the token reads many, and a page
 *  that shows one namespace reads as one namespace. This lists them. */
export function TokenReadScopePanel({
  scope,
  docsHref,
  included,
  onToggleRepo,
}: {
  scope: TokenReadScope;
  docsHref?: string;
  /** Restricted repositories the person chose to add. The token then names them and gets
   *  a direct read grant of its own, revoked with the token. Absent = read-only panel. */
  included?: ReadonlySet<string>;
  onToggleRepo?: (repo: ScopeRepo) => void;
}) {
  const addable = !!onToggleRepo;
  const includedRepos = scope.excludedRestricted.filter((r) => included?.has(r.repo));
  const stillExcluded = scope.excludedRestricted.filter((r) => !included?.has(r.repo));
  const chip =
    "inline-flex items-center px-1.5 py-0.5 rounded font-mono text-[11px] bg-[var(--color-surface)] border border-[var(--color-border)] text-[var(--color-text)] max-w-[16rem] truncate";
  const repoCount = scope.repositories.length;
  const nsCount = scope.namespaces.length;
  return (
    <div
      className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2.5 text-xs space-y-2"
      data-testid="token-read-scope"
      aria-label="What this token can read and write"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-medium text-[var(--color-text)] shrink-0">Writes to</span>
        <span className={chip} title={scope.home}>
          {scope.home || "—"}
        </span>
        <span className="text-[var(--color-text-muted)]">memories, captured sessions and working notes land here.</span>
      </div>

      <div className="space-y-1">
        <div className="text-[var(--color-text)] font-medium">
          Reads {nsCount} namespace{nsCount === 1 ? "" : "s"}
          {repoCount > 0 ? (
            <>
              {" "}
              and {repoCount} repositor{repoCount === 1 ? "y" : "ies"}
            </>
          ) : null}
          <span className="font-normal text-[var(--color-text-muted)]">
            {" "}
            — everything you belong to, including what you join later.
          </span>
        </div>
        <div className="flex flex-wrap gap-1">
          {scope.namespaces.map((ns) => (
            <span key={ns} className={chip} title={ns}>
              {ns}
            </span>
          ))}
          {scope.repositories.map((r) => (
            <span key={r.repo} className={chip} title={r.branches.map((b) => b.namespace).join(", ")}>
              {r.repo}
              {r.branches.length > 1 ? (
                <span className="ml-1 text-[var(--color-text-muted)]">· {r.branches.length} branches</span>
              ) : null}
            </span>
          ))}
          {includedRepos.map((r) => (
            <span
              key={r.repo}
              className={chip}
              title={`${r.branches.map((b) => b.namespace).join(", ")} — restricted; the token gets its own read grant`}
              data-testid="token-read-scope-included"
            >
              <Lock size={10} className="mr-1 shrink-0" aria-hidden="true" />
              {r.repo}
              {r.branches.length > 1 ? (
                <span className="ml-1 text-[var(--color-text-muted)]">· {r.branches.length} branches</span>
              ) : null}
            </span>
          ))}
        </div>
      </div>

      {scope.pending.length > 0 && (
        <div className="text-[var(--color-text-muted)]">
          Awaiting approval: {scope.pending.join(", ")}. Readable the moment an admin approves — no new token.
        </div>
      )}

      {scope.excludedRestricted.length > 0 && (
        <div
          className="flex items-start gap-1.5 text-[var(--color-text-muted)]"
          data-testid="token-read-scope-restricted"
        >
          <Lock size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
          <div className="space-y-1">
            {addable ? (
              <>
                <div>
                  <span className="text-[var(--color-text)] font-medium">Restricted repositories</span> are not read
                  through membership. Tick one to add it: the token names it and gets a read grant of its own, revoked
                  with the token, and with your access to the repository. A restricted tracker record bound to the
                  repository comes with it when you hold a grant there.
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-1">
                  {scope.excludedRestricted.map((r) => (
                    <label key={r.repo} className="inline-flex items-center gap-1.5 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={!!included?.has(r.repo)}
                        onChange={() => onToggleRepo?.(r)}
                        className="accent-[var(--color-accent)]"
                        aria-label={`Add ${r.repo} to this token`}
                      />
                      <span className={chip} title={r.branches.map((b) => b.namespace).join(", ")}>
                        {r.repo}
                      </span>
                      {r.branches.length > 1 ? (
                        <span className="text-[var(--color-text-muted)]">{r.branches.length} branches</span>
                      ) : null}
                    </label>
                  ))}
                </div>
                {docsHref ? (
                  <div>
                    <a className="underline" href={docsHref} target="_blank" rel="noreferrer">
                      How restricted access works
                    </a>
                    .
                  </div>
                ) : null}
              </>
            ) : (
              <div>
                <span className="text-[var(--color-text)] font-medium">Not included: </span>
                {stillExcluded.map((r) => (
                  <span key={r.repo} className={`${chip} mr-1`} title={r.branches.map((b) => b.namespace).join(", ")}>
                    {r.repo}
                  </span>
                ))}
                <span>
                  {stillExcluded.length === 1 ? "is a restricted repository" : "are restricted repositories"}. A
                  restricted repository is read only by a token minted by name for a subject with a direct grant; tokens
                  minted here run under their own subject, so they never read one.
                  {docsHref ? (
                    <>
                      {" "}
                      <a className="underline" href={docsHref} target="_blank" rel="noreferrer">
                        How to get one
                      </a>
                      .
                    </>
                  ) : null}
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
