// The `knowledge.source.*` client, imported from here rather than through the `api.ts`
// barrel (as `api/tasks.ts`): a re-export there adds bytes to the boot chunk, and only the
// repository card's Sources facet and its dialogs call these.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";
import type {
  KnowledgeSourceAddResult,
  KnowledgeSourceCheckResult,
  KnowledgeSourceList,
  KnowledgeSourceRemoveResult,
  KnowledgeSourceRotateResult,
  KnowledgeSourceSyncNowResult,
  SourceForm,
} from "../lib/knowledgeSources";
import { sourceAddBody } from "../lib/knowledgeSources";

// A repository's document sources, addressed by its trunk code namespace. Reading them
// needs the namespace's viewer role; every other call its admin.

type Ns = Pick<Identity, "org" | "namespace">;

export async function listKnowledgeSources(id: Ns, opts: RequestOpts = {}): Promise<KnowledgeSourceList> {
  return post<KnowledgeSourceList>("/v1/knowledge.source.list", { org: id.org, namespace: id.namespace }, opts.signal);
}

/** `dryRun` probes the credential and stores nothing. */
export async function addKnowledgeSource(id: Ns, form: SourceForm, dryRun: boolean): Promise<KnowledgeSourceAddResult> {
  return post<KnowledgeSourceAddResult>("/v1/knowledge.source.add", sourceAddBody(id.org, id.namespace, form, dryRun));
}

export async function checkKnowledgeSource(id: Ns, name: string): Promise<KnowledgeSourceCheckResult> {
  return post<KnowledgeSourceCheckResult>("/v1/knowledge.source.check", { org: id.org, namespace: id.namespace, name });
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
