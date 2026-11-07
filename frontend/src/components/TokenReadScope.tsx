import type { ReactNode } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon } from "@clickhouse/click-ui/Icon";
import type { ScopeRepo, TokenReadScope } from "../lib/tokenScope";
import { ExternalLink } from "./ui/ExternalLink";
import { TipTarget } from "./ui/TipTarget";

/** What a token that reads everything you belong to will read, listed before it is
 *  made, with a checkbox for each restricted repository it reads only when added. */
export function TokenReadScopePanel({
  scope,
  docsHref,
  included,
  onToggleRepo,
}: {
  scope: TokenReadScope;
  docsHref?: string;
  /** Restricted repositories added to the token. Each gets a read grant of the token's
   *  own, revoked with it. */
  included: ReadonlySet<string>;
  onToggleRepo: (repo: ScopeRepo) => void;
}) {
  const includedRepos = scope.excludedRestricted.filter((r) => included.has(r.repo));
  const repoCount = scope.repositories.length + includedRepos.length;
  const nsCount = scope.namespaces.length;
  return (
    // biome-ignore lint/a11y/useSemanticElements: a summary with one optional checkbox list, named as a group
    <div
      className="rounded-sm border border-border bg-surface-2 px-3 py-2.5 text-xs space-y-2"
      data-testid="token-read-scope"
      role="group"
      aria-label="What this token can read"
    >
      <div className="space-y-1">
        <div className="text-text font-medium">
          Reads {nsCount} namespace{nsCount === 1 ? "" : "s"}
          {repoCount > 0 ? (
            <>
              {" "}
              and {repoCount} repositor{repoCount === 1 ? "y" : "ies"}
            </>
          ) : null}
          <span className="font-normal text-text-muted"> today, and any you join later.</span>
        </div>
        <div className="flex flex-wrap gap-1">
          {scope.namespaces.map((ns) => (
            <Name key={ns}>{ns}</Name>
          ))}
          {scope.repositories.map((r) => (
            <Name key={r.repo} detail={branchNamespaces(r)}>
              {r.repo}
              {r.branches.length > 1 ? <span className="font-sans"> · {r.branches.length} branches</span> : null}
            </Name>
          ))}
          {includedRepos.map((r) => (
            <Name
              key={r.repo}
              detail={`${branchNamespaces(r)} — restricted, added to this token`}
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
          Awaiting approval: {scope.pending.join(", ")}. Readable once approved, with no new token.
        </div>
      )}

      {scope.excludedRestricted.length > 0 && (
        <div className="flex items-start gap-1.5 text-text-muted" data-testid="token-read-scope-restricted">
          <Icon name="lock" size="xs" aria-hidden="true" className="mt-0.5" />
          <div className="space-y-1">
            <div>
              <span className="text-text font-medium">Restricted repositories</span> are read only if you add them here.
              {docsHref ? (
                <>
                  {" "}
                  <ExternalLink href={docsHref} size="sm">
                    How restricted access works
                  </ExternalLink>
                </>
              ) : null}
            </div>
            <div className="flex flex-wrap gap-x-3 gap-y-1">
              {scope.excludedRestricted.map((r) => (
                <Checkbox
                  key={r.repo}
                  checked={included.has(r.repo)}
                  onCheckedChange={() => onToggleRepo(r)}
                  aria-label={`Also read ${r.repo}`}
                  label={
                    <span className="inline-flex items-center gap-1.5">
                      <Name detail={branchNamespaces(r)}>{r.repo}</Name>
                      {r.branches.length > 1 ? (
                        <span className="text-text-muted">{r.branches.length} branches</span>
                      ) : null}
                    </span>
                  }
                />
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const branchNamespaces = (r: ScopeRepo) => r.branches.map((b) => b.namespace).join(", ");

/** A namespace or repository name: Click UI's badge, the name in mono. */
function Name({
  children,
  locked = false,
  detail,
  ...rest
}: {
  children: ReactNode;
  locked?: boolean;
  /** More about the name, e.g. the namespaces a repository spans: its tooltip. */
  detail?: string;
  className?: string;
  "data-testid"?: string;
}) {
  const badge = (
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
  return detail ? <TipTarget tip={detail}>{badge}</TipTarget> : badge;
}
