package com.clickhouse.loom;

import com.clickhouse.loom.Results.DeleteResult;
import com.clickhouse.loom.Results.GetResult;
import com.clickhouse.loom.Results.SearchResult;
import com.clickhouse.loom.Results.SetResult;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.time.Duration;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;

/**
 * A minimal Loom client for the memory ops: {@code memory.set}, {@code memory.get}, {@code
 * memory.search} and {@code memory.delete}.
 *
 * <p>Every op is {@code POST <baseUrl>/v1/<op>} with a JSON body and, when a token is set,
 * {@code Authorization: Bearer <token>}. The identity {@code (org, namespace, agent)} is bound
 * once and stamped on every call. A non-2xx answer throws {@link LoomException}.
 *
 * <pre>{@code
 * LoomClient loom = LoomClient.builder().baseUrl("http://localhost:7777")
 *     .org("dev").namespace("local").agent("java-demo").build();
 * loom.set("seat_pref", "Prefers an aisle seat.");
 * }</pre>
 */
public final class LoomClient {
  static final String USER_AGENT = "loom-java/0.1.0";
  private static final ObjectMapper JSON = new ObjectMapper();

  private final URI baseUrl;
  private final String token;
  private final String org;
  private final String namespace;
  private final String agent;
  private final Duration timeout;
  private final HttpClient http;

  private LoomClient(Builder b) {
    String base = b.baseUrl != null ? b.baseUrl : env("LOOM_BASE_URL");
    if (base == null || base.isBlank()) {
      throw new IllegalArgumentException("baseUrl is required (or set LOOM_BASE_URL)");
    }
    this.baseUrl = URI.create(base.endsWith("/") ? base.substring(0, base.length() - 1) : base);
    String t = b.token != null ? b.token : env("LOOM_API_KEY");
    this.token = t == null || t.isBlank() ? null : t;
    this.org = required(b.org, "org");
    this.namespace = required(b.namespace, "namespace");
    this.agent = required(b.agent, "agent");
    this.timeout = b.timeout;
    this.http =
        b.httpClient != null
            ? b.httpClient
            : HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
  }

  public static Builder builder() {
    return new Builder();
  }

  /** A client from {@code LOOM_BASE_URL} and {@code LOOM_API_KEY}, with the identity given. */
  public static LoomClient fromEnv(String org, String namespace, String agent) {
    return builder().org(org).namespace(namespace).agent(agent).build();
  }

  /** Save a fact under {@code memoryKey}; a key that holds a row gains a new version. */
  public SetResult set(String memoryKey, Object value) {
    return set(memoryKey, value, Map.of());
  }

  /**
   * {@code memory.set} with more input fields ({@code tags}, {@code memory_type}, {@code
   * user_id}, …), named as docs/reference/operations/memory.md names them.
   */
  public SetResult set(String memoryKey, Object value, Map<String, ?> extra) {
    ObjectNode body = body(extra);
    body.put("memory_key", Objects.requireNonNull(memoryKey, "memoryKey"));
    body.set("value", JSON.valueToTree(Objects.requireNonNull(value, "value")));
    return SetResult.of(call("memory.set", body));
  }

  /** Read one memory by its exact key. */
  public GetResult get(String memoryKey) {
    return get(memoryKey, Map.of());
  }

  public GetResult get(String memoryKey, Map<String, ?> extra) {
    ObjectNode body = body(extra);
    body.put("memory_key", Objects.requireNonNull(memoryKey, "memoryKey"));
    return GetResult.of(call("memory.get", body));
  }

  /** Semantic search over the namespace's memories; at most {@code topK} hits. */
  public SearchResult search(String query, int topK) {
    return search(query, topK, Map.of());
  }

  public SearchResult search(String query, int topK, Map<String, ?> extra) {
    ObjectNode body = body(extra);
    body.put("query", Objects.requireNonNull(query, "query"));
    body.put("top_k", topK);
    return SearchResult.of(call("memory.search", body));
  }

  /** Tombstone a memory by key (recoverable from the dashboard for 14 days). */
  public DeleteResult delete(String memoryKey) {
    return delete(memoryKey, Map.of());
  }

  public DeleteResult delete(String memoryKey, Map<String, ?> extra) {
    ObjectNode body = body(extra);
    body.put("memory_key", Objects.requireNonNull(memoryKey, "memoryKey"));
    return DeleteResult.of(call("memory.delete", body));
  }

  private ObjectNode body(Map<String, ?> extra) {
    ObjectNode body = JSON.createObjectNode();
    body.put("org", org);
    body.put("namespace", namespace);
    body.put("agent", agent);
    for (Map.Entry<String, ?> e : extra.entrySet()) {
      body.set(e.getKey(), JSON.valueToTree(e.getValue()));
    }
    return body;
  }

  private JsonNode call(String op, ObjectNode body) {
    String requestId = UUID.randomUUID().toString();
    HttpRequest.Builder req =
        HttpRequest.newBuilder(URI.create(baseUrl + "/v1/" + op))
            // HTTP/1.1: the default HTTP/2 sends an h2c upgrade on http:// URLs, which
            // uvicorn answers with 400 "Invalid HTTP request received".
            .version(HttpClient.Version.HTTP_1_1)
            .timeout(timeout)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .header("User-Agent", USER_AGENT)
            .header("X-Request-Id", requestId);
    if (token != null) {
      req.header("Authorization", "Bearer " + token);
    }
    HttpResponse<String> res;
    try {
      req.POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body)));
      res = http.send(req.build(), HttpResponse.BodyHandlers.ofString());
    } catch (HttpTimeoutException e) {
      throw noAnswer(op, "TIMEOUT", "timed out after " + timeout, requestId, e);
    } catch (IOException e) {
      throw noAnswer(op, "UNREACHABLE", "no answer: " + e.getMessage(), requestId, e);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw noAnswer(op, "INTERRUPTED", "interrupted", requestId, e);
    }
    String echoed = res.headers().firstValue("X-Request-Id").orElse(requestId);
    JsonNode parsed = parse(res.body());
    if (res.statusCode() / 100 != 2) {
      Double retryAfter =
          res.headers().firstValue("Retry-After").map(LoomClient::seconds).orElse(null);
      if (parsed == null && res.body() != null && !res.body().isEmpty()) {
        String text = res.body().length() > 200 ? res.body().substring(0, 200) : res.body();
        parsed = JSON.createObjectNode().put("detail", text);
      }
      throw LoomException.fromResponse(op, res.statusCode(), parsed, echoed, retryAfter);
    }
    if (parsed == null || !parsed.isObject()) {
      throw new LoomException(
          op, res.statusCode(), "BAD_RESPONSE", op + ": the answer is not a JSON object",
          "internal", false, null, echoed, null, null);
    }
    return parsed;
  }

  private static LoomException noAnswer(
      String op, String code, String message, String requestId, Throwable cause) {
    return new LoomException(
        op, 0, code, op + ": " + message, "dependency", true, null, requestId, null, cause);
  }

  private static JsonNode parse(String text) {
    if (text == null || text.isEmpty()) {
      return null;
    }
    try {
      return JSON.readTree(text);
    } catch (JsonProcessingException e) {
      return null;
    }
  }

  private static Double seconds(String header) {
    try {
      return Double.parseDouble(header.trim());
    } catch (NumberFormatException e) {
      return null; // an HTTP date: left to the caller's backoff
    }
  }

  private static String required(String v, String name) {
    if (v == null || v.isBlank()) {
      throw new IllegalArgumentException(name + " is required");
    }
    return v;
  }

  private static String env(String name) {
    return System.getenv(name);
  }

  /** Builds a {@link LoomClient}. */
  public static final class Builder {
    private String baseUrl;
    private String token;
    private String org;
    private String namespace;
    private String agent;
    private Duration timeout = Duration.ofSeconds(30);
    private HttpClient httpClient;

    private Builder() {}

    /** The server, for example {@code http://localhost:7777}; else {@code LOOM_BASE_URL}. */
    public Builder baseUrl(String baseUrl) {
      this.baseUrl = baseUrl;
      return this;
    }

    /** The bearer token; else {@code LOOM_API_KEY}; none for a single-user local server. */
    public Builder token(String token) {
      this.token = token;
      return this;
    }

    public Builder org(String org) {
      this.org = org;
      return this;
    }

    public Builder namespace(String namespace) {
      this.namespace = namespace;
      return this;
    }

    public Builder agent(String agent) {
      this.agent = agent;
      return this;
    }

    /** Per-request timeout (default 30 s). */
    public Builder timeout(Duration timeout) {
      this.timeout = Objects.requireNonNull(timeout, "timeout");
      return this;
    }

    /** A preconfigured {@link HttpClient} (proxy, TLS, executor). */
    public Builder httpClient(HttpClient httpClient) {
      this.httpClient = httpClient;
      return this;
    }

    public LoomClient build() {
      return new LoomClient(this);
    }
  }
}
