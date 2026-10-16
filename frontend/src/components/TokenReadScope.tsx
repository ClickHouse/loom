import type { ReactNode } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon } from "@clickhouse/click-ui/Icon";
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
  const repoCount = scope.repositories.length;
  const nsCount = scope.namespaces.length;
  return (
    // biome-ignore lint/a11y/useSemanticElements: a read-only summary, not form controls, so a named group and not a fieldset
    <div
      className="rounded-sm border border-border bg-surface-2 px-3 py-2.5 text-xs space-y-2"
      data-testid="token-read-scope"
      role="group"
      aria-label="What this token can read and write"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-medium text-text shrink-0">Writes to</span>
        <Name title={scope.home}>{scope.home || "—"}</Name>
        <span className="text-text-muted">memories, captured sessions and working notes land here.</span>
      </div>

      <div className="space-y-1">
        <div className="text-text font-medium">
          Reads {nsCount} namespace{nsCount === 1 ? "" : "s"}
          {repoCount > 0 ? (
            <>
              {" "}
              and {repoCount} repositor{repoCount === 1 ? "y" : "ies"}
            </>
          ) : null}
          <span className="font-normal text-text-muted">
            {" "}
            — everything you belong to, including what you join later.
          </span>
        </div>
        <div className="flex flex-wrap gap-1">
          {scope.namespaces.map((ns) => (
            <Name key={ns} title={ns}>
              {ns}
            </Name>
          ))}
          {scope.repositories.map((r) => (
            <Name key={r.repo} title={r.branches.map((b) => b.namespace).join(", ")}>
              {r.repo}
              {r.branches.length > 1 ? <span className="font-sans"> · {r.branches.length} branches</span> : null}
            </Name>
          ))}
          {includedRepos.map((r) => (
            <Name
              key={r.repo}
              title={`${r.branches.map((b) => b.namespace).join(", ")} — restricted; the token gets its own read grant`}
              data-testid="token-read-scope-included"
              locked
            >
              {r.repo}
              {r.branches.length > 1 ? <span className="font-sans"> · {r.branches.length} branches</span> : null}
            </Name>
          ))}
        </div>
      </div>

      {scope.pending.length > 0 && (
        <div className="text-text-muted">
          Awaiting approval: {scope.pending.join(", ")}. Readable the moment an admin approves — no new token.
        </div>
      )}

      {scope.excludedRestricted.length > 0 && (
        <div className="flex items-start gap-1.5 text-text-muted" data-testid="token-read-scope-restricted">
          <Icon name="lock" size="xs" aria-hidden="true" className="mt-0.5" />
          <div className="space-y-1">
            {addable ? (
              <>
                <div>
                  <span className="text-text font-medium">Restricted repositories</span> are not read through
                  membership. Tick one to add it: the token names it and gets a read grant of its own, revoked with the
                  token, and with your access to the repository. A restricted tracker record bound to the repository
                  comes with it when you hold a grant there.
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-1">
                  {scope.excludedRestricted.map((r) => (
                    <Checkbox
                      key={r.repo}
                      checked={!!included?.has(r.repo)}
                      onCheckedChange={() => onToggleRepo?.(r)}
                      aria-label={`Add ${r.repo} to this token`}
                      label={
                        <span className="inline-flex items-center gap-1.5">
                          <Name title={r.branches.map((b) => b.namespace).join(", ")}>{r.repo}</Name>
                          {r.branches.length > 1 ? (
                            <span className="text-text-muted">{r.branches.length} branches</span>
                          ) : null}
                        </span>
                      }
                    />
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
                <span className="text-text font-medium">Not included: </span>
                {stillExcluded.map((r) => (
                  <Name key={r.repo} title={r.branches.map((b) => b.namespace).join(", ")} className="mr-1">
                    {r.repo}
                  </Name>
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

/** A namespace or repository name: Click UI's badge, the name in mono. */
function Name({
  children,
  locked = false,
  ...rest
}: {
  children: ReactNode;
  locked?: boolean;
  title?: string;
  className?: string;
  "data-testid"?: string;
}) {
  return (
    <Badge
      {...rest}
      text={
        <span className="inline-flex items-center gap-1 font-mono">
          {locked && <Icon name="lock" size="xs" aria-hidden="true" />}
          <span>{children}</span>
        </span>
      }
    />
  );
}
