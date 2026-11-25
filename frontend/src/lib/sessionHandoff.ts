import { NAMESPACE_NAME } from "./namespace";

export const HANDOFF_QUERY_KEYS = ["brief", "revision", "snapshot", "commit", "org"] as const;

export interface SessionHandoffRef {
  org: string;
  namespace: string;
  brief: string;
  revision: number;
  snapshot: string;
  commit: string;
}

const BRIEF_KEY = /^(?:~[A-Za-z0-9_.%-]+~(?:[A-Za-z0-9_.%-]+~)?)?[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}$/;
const SNAPSHOT = /^[1-9][0-9]{0,19}\.[0-9a-f]{64}$/;
const COMMIT = /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/;

export function readSessionHandoff(
  params: URLSearchParams,
  pageNamespace: string,
): SessionHandoffRef | "invalid" | null {
  if (!params.has("brief")) return null;
  const org = params.get("org") ?? "";
  const namespace = params.get("namespace") ?? pageNamespace;
  const brief = params.get("brief") ?? "";
  const revision = Number(params.get("revision"));
  const snapshot = params.get("snapshot") ?? "";
  const commit = params.get("commit") ?? "";
  if (
    !org ||
    !NAMESPACE_NAME.test(namespace) ||
    brief.length > 600 ||
    !BRIEF_KEY.test(brief) ||
    !/^[1-9][0-9]*$/.test(params.get("revision") ?? "") ||
    revision >= 2 ** 32 ||
    !SNAPSHOT.test(snapshot) ||
    !COMMIT.test(commit)
  )
    return "invalid";
  return { org, namespace, brief, revision, snapshot, commit };
}

export function sessionHandoffUrl(path: string, handoff: SessionHandoffRef): string {
  const { revision, ...params } = handoff;
  const url = new URL(path, window.location.origin);
  url.search = new URLSearchParams({ ...params, revision: String(revision) }).toString();
  return url.toString();
}
