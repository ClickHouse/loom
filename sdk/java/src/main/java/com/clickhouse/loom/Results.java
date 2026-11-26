package com.clickhouse.loom;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/** The typed answers of the memory and conversation ops; each keeps the whole response as {@code raw}. */
public final class Results {
  private Results() {}

  /** {@code memory.set}: the version and id the server assigned. */
  public record SetResult(
      String memoryId, long version, String contentSha256, String status, boolean pinned, JsonNode raw) {
    static SetResult of(JsonNode r) {
      return new SetResult(
          r.path("memory_id").asText(""),
          r.path("version").asLong(),
          r.path("content_sha256").asText(""),
          r.path("status").asText(""),
          r.path("pinned").asBoolean(false),
          r);
    }
  }

  /**
   * {@code memory.get}: {@code found} is false when no live row holds the key. {@code value} is
   * the stored value as written: text, an object, a list or a scalar.
   */
  public record GetResult(
      boolean found,
      String memoryId,
      JsonNode value,
      String memoryType,
      Long version,
      String versionText,
      JsonNode raw) {
    static GetResult of(JsonNode r) {
      JsonNode version = r.get("version");
      return new GetResult(
          r.path("found").asBoolean(false),
          r.path("memory_id").asText(""),
          r.get("value"),
          r.hasNonNull("memory_type") ? r.get("memory_type").asText() : null,
          version == null || version.isNull() ? null : version.asLong(),
          r.hasNonNull("version_text") ? r.get("version_text").asText() : null,
          r);
    }
  }

  /** One {@code memory.search} hit, best first. */
  public record SearchHit(
      String memoryKey,
      String memoryType,
      String contentExcerpt,
      double similarity,
      double rankScore,
      JsonNode raw) {
    static SearchHit of(JsonNode h) {
      return new SearchHit(
          h.path("memory_key").asText(""),
          h.path("memory_type").asText(""),
          h.path("content_excerpt").asText(""),
          h.path("similarity").asDouble(),
          h.path("rank_score").asDouble(),
          h);
    }
  }

  /** {@code memory.search}: the ranked hits; empty when nothing passed the noise floor. */
  public record SearchResult(List<SearchHit> hits, JsonNode raw) {
    static SearchResult of(JsonNode r) {
      List<SearchHit> hits = new ArrayList<>();
      for (JsonNode h : r.path("results")) {
        hits.add(SearchHit.of(h));
      }
      return new SearchResult(List.copyOf(hits), r);
    }
  }

  /** {@code memory.delete}: {@code found} is false when no live row of the key exists. */
  public record DeleteResult(boolean tombstoned, boolean found, long version, JsonNode raw) {
    static DeleteResult of(JsonNode r) {
      return new DeleteResult(
          r.path("tombstoned").asBoolean(false),
          r.path("found").asBoolean(false),
          r.path("version").asLong(),
          r);
    }
  }

  /**
   * {@code conversation.append}: {@code reason} is empty (written), {@code duplicate} (this {@code
   * msg_id} already landed; {@code stored} is true), {@code logging_disabled} or {@code deleted}
   * ({@code stored} is false). {@code ts} is the server's time, as it wrote it.
   */
  public record ConversationAppendResult(
      String msgId, String ts, boolean stored, String reason, JsonNode raw) {
    static ConversationAppendResult of(JsonNode r) {
      return new ConversationAppendResult(
          r.path("msg_id").asText(""),
          r.path("ts").asText(""),
          r.path("stored").asBoolean(true),
          r.path("reason").asText(""),
          r);
    }

    /**
     * This result when the turn was kept; otherwise a {@link LoomException} whose {@code code()}
     * is {@code LOGGING_DISABLED} or {@code DELETED}.
     */
    public ConversationAppendResult requireStored() {
      if (stored) {
        return this;
      }
      String op = "conversation.append";
      if (reason.equals("logging_disabled")) {
        throw new LoomException(
            op, 200, "LOGGING_DISABLED",
            op + ": the namespace does not record conversations, so the turn was not stored;"
                + " an admin turns recording on with conversation.set_logging (enabled: true)",
            "configuration", false, null, null, raw, null);
      }
      String code = reason.isEmpty() ? "NOT_STORED" : reason.toUpperCase(Locale.ROOT);
      throw new LoomException(
          op, 200, code, op + ": the turn was not stored (reason: " + reason + ")",
          "input", false, null, null, raw, null);
    }
  }

  /** One message of a thread, as {@code conversation.get} returns it. */
  public record ConversationMessage(
      String msgId,
      String agent,
      String role,
      String content,
      String reasoning,
      String userId,
      String runId,
      JsonNode metadata,
      long tokens,
      String ts,
      JsonNode raw) {
    static ConversationMessage of(JsonNode m) {
      JsonNode meta = m.get("metadata");
      return new ConversationMessage(
          m.path("msg_id").asText(""),
          m.path("agent").asText(""),
          m.path("role").asText(""),
          m.path("content").asText(""),
          m.path("reasoning").asText(""),
          m.path("user_id").asText("-"),
          m.path("run_id").asText("-"),
          meta != null && meta.isObject() ? meta : JsonNodeFactory.instance.objectNode(),
          m.path("tokens").asLong(),
          m.path("ts").asText(""),
          m);
    }
  }

  /**
   * {@code conversation.get}: {@code found} is false when the thread has neither a thread row nor
   * a live message. {@code messages} is empty unless they were asked for; {@code msgCount} counts
   * every live message either way.
   */
  public record ConversationGetResult(
      boolean found,
      String threadId,
      String title,
      JsonNode metadata,
      String runId,
      String userId,
      String agent,
      long msgCount,
      List<ConversationMessage> messages,
      String nextCursor,
      JsonNode raw) {
    static ConversationGetResult of(JsonNode r) {
      List<ConversationMessage> messages = new ArrayList<>();
      for (JsonNode m : r.path("messages")) {
        messages.add(ConversationMessage.of(m));
      }
      JsonNode meta = r.get("metadata");
      return new ConversationGetResult(
          r.path("found").asBoolean(false),
          r.path("thread_id").asText(""),
          r.path("title").asText(""),
          meta != null && meta.isObject() ? meta : JsonNodeFactory.instance.objectNode(),
          r.path("run_id").asText("-"),
          r.path("user_id").asText("-"),
          r.path("agent").asText(""),
          r.path("msg_count").asLong(),
          List.copyOf(messages),
          r.hasNonNull("next_cursor") ? r.get("next_cursor").asText() : null,
          r);
    }
  }

  /** {@code conversation.create}: the thread id and when this call wrote its row. */
  public record ConversationCreateResult(String threadId, String createdAt, JsonNode raw) {
    static ConversationCreateResult of(JsonNode r) {
      return new ConversationCreateResult(
          r.path("thread_id").asText(""), r.path("created_at").asText(""), r);
    }
  }

  /** {@code conversation.delete}: the tombstone time and how many rows it stamped, in total. */
  public record ConversationDeleteResult(
      String threadId, String tombstonedAt, long total, JsonNode raw) {
    static ConversationDeleteResult of(JsonNode r) {
      return new ConversationDeleteResult(
          r.path("thread_id").asText(""),
          r.path("tombstoned_at").asText(""),
          r.path("total").asLong(),
          r);
    }
  }

  /** One {@code conversation.threads} entry. */
  public record ConversationThread(
      String threadId, String agent, long msgCount, String lastSeen, String title, JsonNode raw) {
    static ConversationThread of(JsonNode t) {
      return new ConversationThread(
          t.path("thread_id").asText(""),
          t.path("agent").asText(""),
          t.path("msg_count").asLong(),
          t.path("last_seen").asText(""),
          t.path("title").asText(""),
          t);
    }
  }

  /** {@code conversation.threads}: the page of threads, most recently active first. */
  public record ConversationThreadsResult(List<ConversationThread> threads, JsonNode raw) {
    static ConversationThreadsResult of(JsonNode r) {
      List<ConversationThread> threads = new ArrayList<>();
      for (JsonNode t : r.path("threads")) {
        threads.add(ConversationThread.of(t));
      }
      return new ConversationThreadsResult(List.copyOf(threads), r);
    }
  }
}
