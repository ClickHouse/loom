import type { MastraDBMessage, StorageThreadType } from "@mastra/core/memory";
import type {
  StorageListMessagesByResourceIdInput,
  StorageListMessagesInput,
  StorageListMessagesOutput,
  StorageListThreadsInput,
  StorageListThreadsOutput,
  StorageResourceType,
} from "@mastra/core/storage";
import { MemoryStorage } from "@mastra/core/storage";
import type { Agent, Body } from "../agent.js";
import { LoomError } from "../errors.js";
import type { OpName } from "../generated/ops.js";
import { hasIdentityWhitespace } from "../whitespace.js";

/** One resource is one Loom user, including every thread and message belonging to it. */
export interface LoomMemoryStorageOptions {
  resourceId: string;
  /** Separates independent Mastra applications using the same Loom agent and user. */
  storeId?: string;
}

type Entities = { thread: StorageThreadType; message: MastraDBMessage; resource: StorageResourceType };
type Kind = keyof Entities;
type Stored<K extends Kind> = { key: string; data: Entities[K]; hash: string; version: string };
type UpdateMessages = Omit<Parameters<MemoryStorage["updateMessages"]>[0], "messages"> & {
  messages: (Omit<Partial<MastraDBMessage>, "content" | "createdAt"> & {
    id: string;
    content?: Partial<MastraDBMessage["content"]>;
  })[];
};
type MessageOptions = Omit<StorageListMessagesInput, "threadId" | "resourceId">;

/**
 * Mastra's mutable memory domain for one resource, stored as versioned Loom memories.
 * Construct one storage and Memory instance per end-user. The resource ID is the
 * actual Loom user_id, so user erasure includes every row without an ownership cache.
 */
export class LoomMemoryStorage extends MemoryStorage {
  override readonly supportsPartialThreadUpdate = true;
  override readonly listThreadsPaginatesInMemory = true;
  private readonly resourceId: string;
  private readonly prefix: Promise<string>;
  private readonly identity: ReturnType<Agent["identity"]>;
  private readonly client: Agent["client"];

  constructor(agent: Agent, options: LoomMemoryStorageOptions) {
    super();
    const { resourceId, storeId = "default" } = options;
    if (!resourceId || ["-", "*"].includes(resourceId) || hasIdentityWhitespace(resourceId)) {
      throw new Error("resourceId must name a Loom user without surrounding whitespace");
    }
    if (!storeId || hasIdentityWhitespace(storeId)) throw new Error("storeId must not be empty or padded");
    this.resourceId = resourceId;
    this.identity = Object.freeze(agent.identity({ userId: resourceId }));
    this.client = agent.client;
    this.prefix = digest(storeId).then((id) => `mastra/${id}/`);
  }

  private call<K extends OpName>(op: K, body: Body<K>) {
    return this.client.call(op, { ...body, ...this.identity } as never);
  }

  private belongs(resourceId?: string): boolean {
    return resourceId === undefined || resourceId === this.resourceId;
  }

  private requireResource(resourceId?: string): void {
    if (!this.belongs(resourceId)) throw new Error("The resource does not belong to this Mastra storage instance");
  }

  private async key(kind: Kind, id: string): Promise<string> {
    if (typeof id !== "string" || !id.trim()) throw new Error("Entity id must not be empty");
    return `${await this.prefix}${kind}/${await digest(id)}`;
  }

  private async readKey<K extends Kind>(kind: K, key: string): Promise<Stored<K> | null> {
    const out = await this.call("memory.get", {
      memory_key: key,
      fields: ["value", "content_sha256", "agent", "user_id", "session_id", "version_text"],
    });
    if (!out.found) return null;
    if (
      (out.agent !== undefined && out.agent !== this.identity.agent) ||
      (out.user_id !== undefined && out.user_id !== this.resourceId) ||
      (out.session_id !== undefined && out.session_id !== this.identity.session_id)
    ) {
      return null;
    }
    const value = typeof out.value === "string" ? JSON.parse(out.value) : out.value;
    if (!isObject(value) || value.kind !== kind || !isObject(value.data)) {
      throw new Error("Invalid Mastra memory envelope");
    }
    const data = value.data as unknown as Entities[K];
    if (value.resourceId !== this.resourceId || key !== (await this.key(kind, data.id))) return null;
    if (kind !== "resource" && (data as StorageThreadType).resourceId !== this.resourceId) return null;
    if (kind === "resource" && data.id !== this.resourceId) return null;
    data.createdAt = validDate(data.createdAt);
    if (kind !== "message") (data as StorageThreadType).updatedAt = validDate(value.data.updatedAt);
    return { key, data, hash: out.content_sha256 ?? "", version: out.version_text ?? "" };
  }

  private read<K extends Kind>(kind: K, id: string): Promise<Stored<K> | null> {
    return this.key(kind, id).then((key) => this.readKey(kind, key));
  }

  private async write<K extends Kind>(kind: K, data: Entities[K], hash = ""): Promise<void> {
    const key = await this.key(kind, data.id);
    const out = await this.call("memory.set", {
      memory_key: key,
      memory_type: "episodic",
      value: JSON.stringify({ kind, resourceId: this.resourceId, data }),
      if_match_sha256: hash,
      clear_expiry: true,
    });
    if (
      !(out.version > 0) ||
      out.duplicate_of ||
      out.merged_into_key ||
      (out.memory_key !== undefined && out.memory_key !== key) ||
      ["duplicate", "proposed", "superseded", "tombstoned", "rejected"].includes(out.status ?? "")
    ) {
      throw new Error("Loom did not persist the requested Mastra entity");
    }
  }

  private async remove(row: Stored<Kind>): Promise<void> {
    if (!/^[0-9]+$/.test(row.version)) {
      throw new Error("Loom omitted the exact version required for deletion");
    }
    const out = await this.call("memory.delete_version", { memory_key: row.key, version: row.version });
    if (!out.tombstoned && out.found !== false) {
      throw new Error("Mastra entity changed during deletion; retry the operation");
    }
  }

  private async change<K extends Kind>(
    kind: K,
    id: string,
    update: (data: Entities[K]) => Entities[K] | undefined,
    create?: () => Entities[K],
  ): Promise<Entities[K] | null> {
    for (let attempt = 0; attempt < 4; attempt++) {
      const row = await this.read(kind, id);
      if (!row && !create) return null;
      if (row && !row.hash) throw new Error("Loom omitted the entity hash required for a partial update");
      const data = row ? update(row.data) : create?.();
      if (data === undefined) return row?.data ?? null;
      try {
        await this.write(kind, data, row ? row.hash : "absent");
        return data;
      } catch (error) {
        if (!(error instanceof LoomError) || error.code !== "PRECONDITION_FAILED" || attempt === 3) throw error;
      }
    }
    throw new Error("Mastra entity changed during the update");
  }

  private async all<K extends Kind>(kind: K): Promise<Stored<K>[]> {
    const rows: Stored<K>[] = [];
    const seen = new Set<string>();
    for (let offset = 0; ; ) {
      const page = await this.call("memory.list", {
        prefix: `${await this.prefix}${kind}/`,
        limit: 100,
        offset,
        include_duplicates: true,
        include_superseded: true,
        fields: ["memory_key"],
      });
      offset += Math.max(1, page.entries.length);
      for (const entry of page.entries) {
        if (seen.has(entry.memory_key)) continue;
        seen.add(entry.memory_key);
        const row = await this.readKey(kind, entry.memory_key);
        if (!row) throw new Error("Mastra history changed during listing; retry the read");
        rows.push(row);
      }
      if (!page.truncated) return rows;
    }
  }

  private async touch(threadIds: Iterable<string | undefined>): Promise<void> {
    for (const id of new Set(threadIds)) {
      if (id) await this.change("thread", id, (thread) => ({ ...thread, updatedAt: nextDate(thread.updatedAt) }));
    }
  }

  override async dangerouslyClearAll(): Promise<void> {
    for (const kind of ["message", "thread", "resource"] as const) {
      const rows = await this.all(kind);
      for (const row of rows) await this.remove(row);
    }
  }

  override async getThreadById({ threadId, resourceId }: Parameters<MemoryStorage["getThreadById"]>[0]) {
    if (!this.belongs(resourceId)) return null;
    return (await this.read("thread", threadId))?.data ?? null;
  }

  override async saveThread({ thread, fence }: Parameters<MemoryStorage["saveThread"]>[0]) {
    refuseFence(fence);
    this.requireResource(thread.resourceId);
    const saved = { ...copy(thread), resourceId: this.resourceId };
    validDate(saved.createdAt);
    validDate(saved.updatedAt);
    await this.write("thread", saved);
    return saved;
  }

  override async updateThread({ id, title, metadata, fence }: Parameters<MemoryStorage["updateThread"]>[0]) {
    refuseFence(fence);
    const updated = await this.change("thread", id, (thread) => ({
      ...thread,
      ...(title !== undefined ? { title } : {}),
      ...(metadata !== undefined ? { metadata: { ...thread.metadata, ...copy(metadata) } } : {}),
      updatedAt: nextDate(thread.updatedAt),
    }));
    if (!updated) throw new Error(`Thread with id ${id} not found`);
    return updated;
  }

  override async updateThreadMetadata(args: Parameters<MemoryStorage["updateThreadMetadata"]>[0]) {
    refuseFence(args.fence);
    if (!this.belongs(args.resourceId)) return null;
    return this.change("thread", args.id, (thread) => {
      const metadata = args.update(copy(thread));
      return metadata
        ? { ...thread, metadata: { ...thread.metadata, ...copy(metadata) }, updatedAt: nextDate(thread.updatedAt) }
        : undefined;
    });
  }

  override async deleteThread({ threadId }: Parameters<MemoryStorage["deleteThread"]>[0]) {
    const thread = await this.read("thread", threadId);
    const messages = (await this.all("message")).filter((row) => row.data.threadId === threadId);
    for (const row of messages) await this.remove(row);
    if (thread) await this.remove(thread);
  }

  override async listThreads(args: StorageListThreadsInput): Promise<StorageListThreadsOutput> {
    const pagination = this.pagination(args, 100);
    this.validateMetadataKeys(args.filter?.metadata);
    const field = args.orderBy?.field ?? "createdAt";
    if (field !== "createdAt" && field !== "updatedAt") throw new Error("Invalid thread order field");
    const direction = checkedDirection(args.orderBy?.direction, "DESC");
    const threads = this.belongs(args.filter?.resourceId)
      ? (await this.all("thread"))
          .map((row) => row.data)
          .filter((thread) => matches(thread.metadata, args.filter?.metadata))
      : [];
    threads.sort(byDate(field, direction));
    return { ...pageInfo(threads.length, pagination), threads: slicePage(threads, pagination) };
  }

  private pagination(args: { page?: number; perPage?: number | false }, fallback: number) {
    const page = args.page ?? 0;
    const perPage = args.perPage ?? fallback;
    this.validatePaginationInput(page, perPage);
    return { page, perPage };
  }

  override async listMessages(args: StorageListMessagesInput): Promise<StorageListMessagesOutput> {
    const threadIds = Array.isArray(args.threadId) ? args.threadId : [args.threadId];
    if (!threadIds.length || threadIds.some((id) => typeof id !== "string" || !id.trim())) {
      throw new Error("threadId must name one or more threads");
    }
    return this.messages(args, (message) => threadIds.includes(message.threadId ?? ""), this.belongs(args.resourceId));
  }

  override async listMessagesByResourceId(args: StorageListMessagesByResourceIdInput) {
    return this.messages(args, () => true, this.belongs(args.resourceId), true);
  }

  private async messages(
    args: MessageOptions,
    selected: (message: MastraDBMessage) => boolean,
    inResource: boolean,
    resourceQuery = false,
  ): Promise<StorageListMessagesOutput> {
    const pagination = this.pagination(args, 40);
    const direction = checkedDirection(args.orderBy?.direction, "ASC");
    if (args.orderBy?.field !== undefined && args.orderBy.field !== "createdAt") {
      throw new Error("Invalid message order field");
    }
    this.validateMetadataKeys(args.filter?.metadata);
    const range = args.filter?.dateRange;
    for (const [key, value] of Object.entries(args.filter?.metadata ?? {})) {
      if (key.length > 128 || (value !== null && !["string", "number", "boolean"].includes(typeof value))) {
        throw new Error("Message metadata filters require scalar values and keys of at most 128 characters");
      }
    }
    const start = range?.start === undefined ? undefined : validDate(range.start).getTime();
    const end = range?.end === undefined ? undefined : validDate(range.end).getTime();
    const all = inResource ? (await this.all("message")).map((row) => row.data) : [];
    const matching = all.filter((message) => {
      const time = message.createdAt.getTime();
      return (
        selected(message) &&
        (start === undefined || (range?.startExclusive ? time > start : time >= start)) &&
        (end === undefined || (range?.endExclusive ? time < end : time <= end)) &&
        matches(message.content.metadata, args.filter?.metadata)
      );
    });
    matching.sort(byDate("createdAt", direction));
    const messages = slicePage(matching, pagination);
    const included = new Set(messages.map((message) => message.id));
    for (const anchor of args.include ?? []) {
      const target = all.find((message) => message.id === anchor.id);
      if (!target || (anchor.threadId !== undefined && anchor.threadId !== target.threadId)) continue;
      const previous = contextSize(anchor.withPreviousMessages);
      const next = contextSize(anchor.withNextMessages);
      const context = all.filter((message) => message.threadId === target.threadId).sort(byDate("createdAt", "ASC"));
      const index = context.findIndex((message) => message.id === target.id);
      for (const message of context.slice(Math.max(0, index - previous), index + next + 1)) {
        if (!included.has(message.id)) {
          messages.push(message);
          included.add(message.id);
        }
      }
    }
    messages.sort(byDate("createdAt", direction));
    const info = pageInfo(pagination.perPage === 0 && !resourceQuery ? 0 : matching.length, pagination);
    if (
      !resourceQuery &&
      args.include?.length &&
      !Object.keys(args.filter?.metadata ?? {}).length &&
      matching.every((message) => included.has(message.id))
    ) {
      info.hasMore = false;
    }
    return { ...info, messages };
  }

  override async listMessagesById({ messageIds }: Parameters<MemoryStorage["listMessagesById"]>[0]) {
    const messages: MastraDBMessage[] = [];
    for (const id of new Set(messageIds)) {
      const row = await this.read("message", id);
      if (row) messages.push(row.data);
    }
    return { messages };
  }

  override async saveMessages({ messages, fence }: Parameters<MemoryStorage["saveMessages"]>[0]) {
    refuseFence(fence);
    const saved = messages.map((message) => {
      this.requireResource(message.resourceId);
      validDate(message.createdAt);
      return { ...copy(message), resourceId: this.resourceId };
    });
    for (const message of saved) await this.write("message", message);
    await this.touch(saved.map((message) => message.threadId));
    return { messages: saved };
  }

  override async updateMessages({ messages, fence }: UpdateMessages) {
    refuseFence(fence);
    for (const update of messages) this.requireResource(update.resourceId);
    const updated: MastraDBMessage[] = [];
    const threads = new Set<string | undefined>();
    for (const patch of messages) {
      const result = await this.change("message", patch.id, (message) => {
        threads.add(message.threadId);
        const content =
          patch.content === undefined ? message.content : { ...message.content, ...defined(copy(patch.content)) };
        if (patch.content?.metadata !== undefined) {
          content.metadata = { ...message.content.metadata, ...copy(patch.content.metadata) };
        }
        return {
          ...message,
          ...defined(copy(patch)),
          content,
          createdAt: message.createdAt,
          resourceId: this.resourceId,
        };
      });
      if (result) {
        updated.push(result);
        threads.add(result.threadId);
      }
    }
    await this.touch(threads);
    return updated;
  }

  override async deleteMessages(messageIds: string[], options?: Parameters<MemoryStorage["deleteMessages"]>[1]) {
    refuseFence(options?.fence);
    const threads = new Set<string | undefined>();
    for (const id of new Set(messageIds)) {
      const row = await this.read("message", id);
      if (!row) continue;
      await this.remove(row);
      threads.add(row.data.threadId);
    }
    await this.touch(threads);
  }

  override async getResourceById({ resourceId }: Parameters<MemoryStorage["getResourceById"]>[0]) {
    if (!this.belongs(resourceId)) return null;
    return (await this.read("resource", resourceId))?.data ?? null;
  }

  override async saveResource({ resource }: Parameters<MemoryStorage["saveResource"]>[0]) {
    this.requireResource(resource.id);
    const saved = copy(resource);
    validDate(saved.createdAt);
    validDate(saved.updatedAt);
    await this.write("resource", saved);
    return saved;
  }

  override async updateResource({
    resourceId,
    workingMemory,
    metadata,
    fence,
  }: Parameters<MemoryStorage["updateResource"]>[0]) {
    refuseFence(fence);
    this.requireResource(resourceId);
    const result = await this.change(
      "resource",
      resourceId,
      (resource) => ({
        ...resource,
        ...(workingMemory !== undefined ? { workingMemory } : {}),
        ...(metadata !== undefined ? { metadata: { ...resource.metadata, ...copy(metadata) } } : {}),
        updatedAt: nextDate(resource.updatedAt),
      }),
      () => {
        const now = new Date();
        return { id: resourceId, workingMemory, metadata: copy(metadata), createdAt: now, updatedAt: now };
      },
    );
    if (!result) throw new Error("Loom did not persist the requested Mastra resource");
    return result;
  }

  override async updateThreadResourceId({
    threadId,
    resourceId,
  }: Parameters<MemoryStorage["updateThreadResourceId"]>[0]) {
    this.requireResource(resourceId);
    const thread = await this.getThreadById({ threadId });
    if (!thread) throw new Error(`Thread with id ${threadId} not found`);
    return thread;
  }
}

function refuseFence(fence: unknown): void {
  if (fence !== undefined) throw new Error("Mastra run fencing is not supported by LoomMemoryStorage");
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function copy<T>(value: T): T {
  return structuredClone(value);
}

function defined<T extends object>(value: T): Partial<T> {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== undefined)) as Partial<T>;
}

function validDate(value: unknown): Date {
  const date = value instanceof Date ? value : new Date(value as string);
  if (!Number.isFinite(date.getTime())) throw new Error("Invalid Mastra timestamp");
  return date;
}

function nextDate(previous: Date): Date {
  return new Date(Math.max(Date.now(), previous.getTime() + 1));
}

function checkedDirection(value: string | undefined, fallback: "ASC" | "DESC"): "ASC" | "DESC" {
  if (value !== undefined && value !== "ASC" && value !== "DESC") throw new Error("Invalid order direction");
  return value ?? fallback;
}

function byDate<T extends { id: string; createdAt: Date; updatedAt?: Date }>(
  field: "createdAt" | "updatedAt",
  direction: "ASC" | "DESC",
) {
  return (a: T, b: T) => {
    const difference = (a[field]?.getTime() ?? 0) - (b[field]?.getTime() ?? 0);
    return difference ? difference * (direction === "ASC" ? 1 : -1) : a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  };
}

function matches(value: Record<string, unknown> | undefined, filter: Record<string, unknown> | undefined): boolean {
  return Object.entries(filter ?? {}).every(([key, expected]) => jsonEqual(value?.[key], expected));
}

function jsonEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((item, i) => jsonEqual(item, b[i]));
  if (!isObject(a) || !isObject(b)) return false;
  const keys = Object.keys(a);
  return (
    keys.length === Object.keys(b).length && keys.every((key) => Object.hasOwn(b, key) && jsonEqual(a[key], b[key]))
  );
}

function contextSize(value: number | undefined): number {
  if (value !== undefined && (!Number.isSafeInteger(value) || value < 0))
    throw new Error("Invalid message context size");
  return value ?? 0;
}

type Pagination = { page: number; perPage: number | false };

function pageInfo(total: number, pagination: Pagination) {
  const { page, perPage } = pagination;
  return { total, page, perPage, hasMore: perPage !== false && (page + 1) * perPage < total };
}

function slicePage<T>(items: T[], { page, perPage }: Pagination): T[] {
  return perPage === false ? items : items.slice(page * perPage, (page + 1) * perPage);
}

async function digest(value: string): Promise<string> {
  const hash = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
