package com.clickhouse.loom;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.ArrayList;
import java.util.List;

/** The typed answers of the four memory ops; each keeps the whole response as {@code raw}. */
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
}
