// The `knowledge.source.*` client, imported from here rather than through the `api.ts`
// barrel (as `api/tasks.ts`): a re-export there adds bytes to the boot chunk, and only the
// repository card's Sources facet and its dialogs call these.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";
import type {
  KnowledgeSourceAddResult,
  KnowledgeSourceCheckResult,
  KnowledgeSourceDetail,
  KnowledgeSourceList,
  KnowledgeSourceProbe,
  KnowledgeSourceRemoveResult,
  KnowledgeSourceRotateResult,
  KnowledgeSourceSyncNowResult,
  SourceForm,
} from "../lib/knowledgeSources";
import { sourceAddBody } from "../lib/knowledgeSources";

// Namespace-scoped requests; the server authorizes each source-management action.

type Ns = Pick<Identity, "org" | "namespace">;

export interface KnowledgeSourceParsedLink {
  link: string;
  kind: string;
  item: Record<string, unknown>;
  unreadable: string;
}

export interface KnowledgeSourceUpdateChanges {
  add_items?: Record<string, unknown>[];
  add_links?: string[];
  remove_items?: Record<string, unknown>[];
  remove_links?: string[];
  include?: string[];
}

export interface KnowledgeSourceUpdateResult {
  name: string;
  items: Record<string, unknown>[];
  include: string[];
  config: Record<string, unknown>;
  added: Record<string, unknown>[];
  removed: Record<string, unknown>[];
  probe: KnowledgeSourceProbe;
}

/** Resolve link types without accessing the provider or importing content. */
export function parseKnowledgeSourceLinks(
  id: Ns,
  links: string[],
  opts: RequestOpts = {},
): Promise<{ links: KnowledgeSourceParsedLink[] }> {
  return post("/v1/knowledge.source.parse_links", { org: id.org, namespace: id.namespace, links }, opts.signal);
}

export function updateKnowledgeSource(
  id: Ns,
  name: string,
  changes: KnowledgeSourceUpdateChanges,
  opts: RequestOpts = {},
): Promise<KnowledgeSourceUpdateResult> {
  return post("/v1/knowledge.source.update", { ...changes, org: id.org, namespace: id.namespace, name }, opts.signal);
}

export async function listKnowledgeSources(id: Ns, opts: RequestOpts = {}): Promise<KnowledgeSourceList> {
  return post<KnowledgeSourceList>("/v1/knowledge.source.list", { org: id.org, namespace: id.namespace }, opts.signal);
}

export async function inspectKnowledgeSource(
  id: Ns,
  name: string,
  opts: RequestOpts = {},
): Promise<KnowledgeSourceDetail> {
  return post("/v1/knowledge.source.inspect", { org: id.org, namespace: id.namespace, name }, opts.signal);
}

/** `dryRun` probes the credential and stores nothing. */
export async function addKnowledgeSource(id: Ns, form: SourceForm, dryRun: boolean): Promise<KnowledgeSourceAddResult> {
  return post<KnowledgeSourceAddResult>("/v1/knowledge.source.add", sourceAddBody(id.org, id.namespace, form, dryRun));
}

/** Closing the Check dialog aborts the probe through `opts.signal`. */
export async function checkKnowledgeSource(
  id: Ns,
  name: string,
  opts: RequestOpts = {},
): Promise<KnowledgeSourceCheckResult> {
  return post<KnowledgeSourceCheckResult>(
    "/v1/knowledge.source.check",
    { org: id.org, namespace: id.namespace, name },
    opts.signal,
  );
}

export async function rotateKnowledgeSource(
  id: Ns,
  name: string,
  credential: string,
): Promise<KnowledgeSourceRotateResult> {
  return post<KnowledgeSourceRotateResult>("/v1/knowledge.source.rotate", {
    org: id.org,
    namespace: id.namespace,
    name,
    credential,
  });
}

export async function removeKnowledgeSource(
  id: Ns,
  name: string,
  confirm: string,
  deleteDocuments: boolean,
): Promise<KnowledgeSourceRemoveResult> {
  return post<KnowledgeSourceRemoveResult>("/v1/knowledge.source.remove", {
    org: id.org,
    namespace: id.namespace,
    name,
    confirm,
    delete_documents: deleteDocuments,
  });
}

export async function syncKnowledgeSourceNow(
  id: Ns,
  name: string,
  full: boolean,
): Promise<KnowledgeSourceSyncNowResult> {
  return post<KnowledgeSourceSyncNowResult>("/v1/knowledge.source.sync_now", {
    org: id.org,
    namespace: id.namespace,
    name,
    full,
  });
}
