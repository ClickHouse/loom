package com.clickhouse.loom;

import com.clickhouse.loom.Results.ConversationAppendResult;
import com.clickhouse.loom.Results.ConversationCreateResult;
import com.clickhouse.loom.Results.ConversationDeleteResult;
import com.clickhouse.loom.Results.ConversationGetResult;
import com.clickhouse.loom.Results.ConversationThreadsResult;
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
 * A minimal Loom client for the memory ops ({@code memory.set}, {@code get}, {@code search} and
 * {@code delete}) and the conversation ops a chat memory needs ({@code conversation.append},
 * {@code get}, {@code create}, {@code delete}, {@code threads} and {@code set_logging}).
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
  private final SessionProtection sessionProtection;

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
    this.sessionProtection = b.sessionProtection;
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

  /** Log one chat turn; {@code role} is {@code user}, {@code assistant}, {@code system} or {@code tool}. */
  public ConversationAppendResult conversationAppend(String threadId, String role, String content) {
    return conversationAppend(threadId, role, content, Map.of());
  }

  /**
   * {@code conversation.append} with more input fields ({@code msg_id}, {@code metadata}, {@code
   * reasoning}, {@code user_id}, …), named as docs/reference/operations/conversation.md names
   * them. A turn the server did not keep is not an error: check {@link
   * ConversationAppendResult#stored()}, or call {@link ConversationAppendResult#requireStored()}.
   */
  public ConversationAppendResult conversationAppend(
      String threadId, String role, String content, Map<String, ?> extra) {
    ObjectNode body = body(extra);
    body.put("thread_id", Objects.requireNonNull(threadId, "threadId"));
    body.put("role", Objects.requireNonNull(role, "role"));
    body.put("content", Objects.requireNonNull(content, "content"));
    return ConversationAppendResult.of(call("conversation.append", body));
  }

  /** A thread's title, metadata and live message count, without its messages. */
  public ConversationGetResult conversationGet(String threadId) {
    return conversationGet(threadId, Map.of());
  }

  /**
   * A thread with one page of its messages, oldest first: at most {@code limit} (1-1000) after
   * skipping {@code offset}.
   */
  public ConversationGetResult conversationGet(String threadId, int limit, int offset) {
    return conversationGet(
        threadId, Map.of("include_messages", true, "limit", limit, "offset", offset));
  }

  /** {@code conversation.get} with more input fields ({@code include_messages}, {@code order}, …). */
  public ConversationGetResult conversationGet(String threadId, Map<String, ?> extra) {
    ObjectNode body = body(extra);
    body.put("thread_id", Objects.requireNonNull(threadId, "threadId"));
    return ConversationGetResult.of(call("conversation.get", body));
  }

  /** Open a thread; re-creating the same id replaces its title, metadata and run. */
  public ConversationCreateResult conversationCreate(String threadId) {
    return conversationCreate(threadId, Map.of());
  }

  /**
   * {@code conversation.create} with more input fields ({@code title}, {@code metadata}, {@code
   * run_id}, {@code user_id}).
   */
  public ConversationCreateResult conversationCreate(String threadId, Map<String, ?> extra) {
    ObjectNode body = body(extra);
    body.put("thread_id", Objects.requireNonNull(threadId, "threadId"));
    return ConversationCreateResult.of(call("conversation.create", body));
  }

  /**
   * Soft-delete a thread: its messages, its thread row and their embeddings. Needs the {@code
   * admin} role. The call sends {@code confirm_thread_id} equal to {@code threadId}.
   */
  public ConversationDeleteResult conversationDelete(String threadId) {
    return conversationDelete(threadId, Map.of());
  }

  public ConversationDeleteResult conversationDelete(String threadId, Map<String, ?> extra) {
    ObjectNode body = body(extra);
    body.put("thread_id", Objects.requireNonNull(threadId, "threadId"));
    body.put("confirm_thread_id", threadId);
    return ConversationDeleteResult.of(call("conversation.delete", body));
  }

  /**
   * This agent's threads with a live message in the last {@code windowDays} days (1-365), most
   * recently active first: at most {@code limit} (1-200) after skipping {@code offset}.
   */
  public ConversationThreadsResult conversationThreads(int windowDays, int limit, int offset) {
    return conversationThreads(
        Map.of("window_days", windowDays, "limit", limit, "offset", offset));
  }

  /** {@code conversation.threads} with its input fields. */
  public ConversationThreadsResult conversationThreads(Map<String, ?> extra) {
    return ConversationThreadsResult.of(call("conversation.threads", body(extra)));
  }

  /**
   * Turn transcript recording on or off for this client's namespace; returns the switch as it now
   * stands. Needs the {@code admin} role. While it is off, {@code conversation.append} answers
   * {@code stored: false}.
   */
  public boolean conversationSetLogging(boolean enabled) {
    ObjectNode body = JSON.createObjectNode();
    body.put("org", org);
    body.put("namespace", namespace);
    body.put("enabled", enabled);
    return call("conversation.set_logging", body).path("enabled").asBoolean(false);
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

  /** Generic wire operation. Inputs use the operation reference's exact field names. */
  public JsonNode call(String op, Map<String, ?> input) {
    ObjectNode wire = JSON.valueToTree(input);
    return call(op, wire);
  }

  /** One explicit human invocation; callbacks retain no permission from this call. */
  public JsonNode callHuman(String op, Map<String, ?> input) {
    return dispatch(op, JSON.valueToTree(input), true);
  }

  /** Retains source contributors across a queued callback or another client. */
  public JsonNode callAutomatic(SessionProtection.Capture capture, String op, Map<String, ?> input) {
    try { return capture.call(() -> call(op, input)); }
    catch (RuntimeException e) { throw e; }
    catch (Exception e) { throw new SessionProtection.Unavailable(e); }
  }

  public JsonNode sourceMcpRead(Map<String, ?> input) {
    return call("knowledge.source.mcp_read", sourceBody(input));
  }

  public JsonNode connectionMcpBrowse(Map<String, ?> input) {
    ObjectNode wire = JSON.createObjectNode().put("org", org);
    input.forEach((key, value) -> wire.set(key, JSON.valueToTree(value)));
    return call("knowledge.connection.mcp_browse", wire);
  }

  public JsonNode sourceMcpList(Map<String, ?> input) {
    return call("knowledge.source.mcp_list", sourceBody(input));
  }

  public JsonNode sourceMcpPause(Map<String, ?> input) {
    return call("knowledge.source.mcp_pause", sourceBody(input));
  }

  public JsonNode sourceMcpRefresh(Map<String, ?> input) {
    return call("knowledge.source.mcp_refresh", sourceBody(input));
  }

  private ObjectNode sourceBody(Map<String, ?> input) {
    ObjectNode wire = JSON.createObjectNode().put("org", org).put("namespace", namespace);
    input.forEach((key, value) -> wire.set(key, JSON.valueToTree(value)));
    return wire;
  }

  private JsonNode call(String op, ObjectNode body) {
    return dispatch(op, body, false);
  }

  private JsonNode dispatch(String op, ObjectNode body, boolean human) {
    if (!op.matches("[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+")) {
      throw new IllegalArgumentException("op must be a dotted operation name");
    }
    try (SessionProtection.Observation observation = SessionProtection.begin(sessionProtection, human)) {
      if (observation.bound() && (http.followRedirects() != HttpClient.Redirect.NEVER
          || http.authenticator().isPresent()
          || !("http".equals(baseUrl.getScheme()) || "https".equals(baseUrl.getScheme()))
          || baseUrl.getHost() == null || baseUrl.getRawUserInfo() != null
          || baseUrl.getRawQuery() != null || baseUrl.getRawFragment() != null)) {
        throw new SessionProtection.Unavailable();
      }
      return send(op, body, observation, human);
    }
  }

  private JsonNode send(
      String op, ObjectNode body, SessionProtection.Observation observation, boolean human) {
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
    if (observation.bound()) { req.header("x-loom-session-protection", "v1"); }
    HttpResponse<String> res;
    try {
      String encoded = observation.bound() ? ProtectedBody.encode(JSON, body) : JSON.writeValueAsString(body);
      req.POST(HttpRequest.BodyPublishers.ofString(encoded));
      observation.beforeSend(human);
      if (observation.bound()) {
        res = ProtectedBody.send(http, req.build(), timeout);
      } else {
        res = http.send(req.build(), HttpResponse.BodyHandlers.ofString());
      }
    } catch (HttpTimeoutException e) {
      throw noAnswer(op, "TIMEOUT", "timed out after " + timeout, requestId, e);
    } catch (IOException e) {
      throw noAnswer(op, "UNREACHABLE", "no answer: " + e.getMessage(), requestId, e);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw noAnswer(op, "INTERRUPTED", "interrupted", requestId, e);
    }
    String echoed = res.headers().firstValue("X-Request-Id").orElse(requestId);
    if (observation.bound()) { observation.observe(res.body()); }
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
    private SessionProtection sessionProtection;

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

    /** Bind the same application-owned authority on every client for a conversation. */
    public Builder sessionProtection(SessionProtection family) {
      this.sessionProtection = Objects.requireNonNull(family, "family");
      return this;
    }

    public LoomClient build() {
      return new LoomClient(this);
    }
  }
}
