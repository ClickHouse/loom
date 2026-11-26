package com.clickhouse.loom;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * An in-memory stand-in for Loom's conversation ops over HTTP, for tests that need state across
 * calls: append (with the logging switch and {@code msg_id} dedup that counts tombstoned turns),
 * get (paged), create, delete (soft, admin only) and threads. Every call is recorded.
 */
public final class FakeLoom implements AutoCloseable {
  private static final ObjectMapper JSON = new ObjectMapper();

  /** One recorded call. */
  public record Call(String op, JsonNode body) {}

  /** One stored turn. */
  public static final class Message {
    public final String msgId;
    public final String role;
    public final String content;
    public final JsonNode metadata;
    final long seq;
    boolean tombstoned;

    Message(String msgId, String role, String content, JsonNode metadata, long seq) {
      this.msgId = msgId;
      this.role = role;
      this.content = content;
      this.metadata = metadata;
      this.seq = seq;
    }
  }

  private static final class Thread {
    ObjectNode row; // title, metadata, run_id, user_id; null when never created or deleted
    final List<Message> messages = new ArrayList<>();
  }

  private final HttpServer server;
  private final List<Call> calls = new ArrayList<>();
  private final Map<String, Thread> threads = new LinkedHashMap<>();
  private long seq;

  /** Whether the namespace records conversations ({@code conversation.set_logging}). */
  public volatile boolean logging = true;

  /** False answers {@code conversation.delete} with 403, as for a writer token. */
  public volatile boolean admin = true;

  /** When set, the next append answers {@code stored} and {@code reason} with it and stores nothing. */
  public volatile String nextAppendReason;

  public FakeLoom() {
    try {
      server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    } catch (IOException e) {
      throw new UncheckedIOException(e);
    }
    server.createContext("/", this::handle);
    server.start();
  }

  public String url() {
    return "http://127.0.0.1:" + server.getAddress().getPort();
  }

  /** A client for {@code dev/local}, agent {@code java-test}. */
  public LoomClient client() {
    return LoomClient.builder().baseUrl(url()).org("dev").namespace("local").agent("java-test").build();
  }

  public synchronized List<Call> calls() {
    return List.copyOf(calls);
  }

  public synchronized List<Call> calls(String op) {
    return calls.stream().filter(c -> c.op().equals(op)).toList();
  }

  public synchronized void clearCalls() {
    calls.clear();
  }

  /** The thread's live turns in log order. */
  public synchronized List<Message> live(String threadId) {
    Thread t = threads.get(threadId);
    return t == null ? List.of() : t.messages.stream().filter(m -> !m.tombstoned).toList();
  }

  /** The thread row's metadata, or null when the thread has no live row. */
  public synchronized JsonNode threadMetadata(String threadId) {
    Thread t = threads.get(threadId);
    return t == null || t.row == null ? null : t.row.get("metadata");
  }

  /** Store a turn directly, as another writer would. */
  public synchronized void seed(String threadId, String msgId, String role, String content) {
    thread(threadId).messages.add(new Message(msgId, role, content, JSON.createObjectNode(), ++seq));
  }

  /** Write a thread row directly, as {@code conversation.create} would. */
  public synchronized void seedThread(String threadId, String title, ObjectNode metadata) {
    ObjectNode row = JSON.createObjectNode();
    row.put("title", title);
    row.set("metadata", metadata);
    row.put("run_id", "-");
    row.put("user_id", "-");
    thread(threadId).row = row;
  }

  @Override
  public void close() {
    server.stop(0);
  }

  private Thread thread(String id) {
    return threads.computeIfAbsent(id, k -> new Thread());
  }

  private void handle(HttpExchange ex) throws IOException {
    String op = ex.getRequestURI().getPath().substring("/v1/".length());
    JsonNode body = JSON.readTree(ex.getRequestBody().readAllBytes());
    int status = 200;
    JsonNode out;
    synchronized (this) {
      calls.add(new Call(op, body));
      try {
        out = answer(op, body);
      } catch (Refusal r) {
        status = r.status;
        out = JSON.createObjectNode().set("detail", r.detail);
      }
    }
    byte[] bytes = JSON.writeValueAsBytes(out);
    ex.getResponseHeaders().add("Content-Type", "application/json");
    ex.sendResponseHeaders(status, bytes.length);
    try (OutputStream os = ex.getResponseBody()) {
      os.write(bytes);
    }
  }

  private JsonNode answer(String op, JsonNode in) {
    String threadId = in.path("thread_id").asText();
    ObjectNode out = JSON.createObjectNode();
    switch (op) {
      case "conversation.set_logging" -> {
        logging = in.path("enabled").asBoolean();
        out.put("namespace", in.path("namespace").asText());
        out.put("enabled", logging);
      }
      case "conversation.append" -> {
        String msgId = in.path("msg_id").asText();
        out.put("msg_id", msgId);
        out.put("ts", "2026-01-01T00:00:00.000000");
        String forced = nextAppendReason;
        if (forced != null) {
          nextAppendReason = null;
          out.put("stored", forced.equals("duplicate"));
          out.put("reason", forced);
        } else if (!logging) {
          out.put("stored", false);
          out.put("reason", "logging_disabled");
        } else if (thread(threadId).messages.stream().anyMatch(m -> m.msgId.equals(msgId))) {
          out.put("stored", true); // a tombstoned turn counts: a retry cannot recreate it
          out.put("reason", "duplicate");
        } else {
          JsonNode meta = in.has("metadata") ? in.get("metadata") : JSON.createObjectNode();
          thread(threadId).messages.add(
              new Message(msgId, in.path("role").asText(), in.path("content").asText(), meta, ++seq));
          out.put("stored", true);
          out.put("reason", "");
        }
      }
      case "conversation.create" -> {
        ObjectNode row = JSON.createObjectNode();
        row.put("title", in.path("title").asText(""));
        row.set("metadata", in.has("metadata") ? in.get("metadata") : JSON.createObjectNode());
        row.put("run_id", in.path("run_id").asText("-"));
        row.put("user_id", in.path("user_id").asText("-"));
        thread(threadId).row = row;
        out.put("thread_id", threadId);
        out.put("created_at", "2026-01-01T00:00:00");
      }
      case "conversation.get" -> {
        Thread t = threads.get(threadId);
        List<Message> live = live(threadId);
        boolean found = t != null && (t.row != null || !live.isEmpty());
        out.put("found", found);
        out.put("thread_id", threadId);
        ObjectNode row = found && t.row != null ? t.row : JSON.createObjectNode();
        out.put("title", row.path("title").asText(""));
        out.set("metadata", row.has("metadata") ? row.get("metadata") : JSON.createObjectNode());
        out.put("run_id", row.path("run_id").asText("-"));
        out.put("user_id", row.path("user_id").asText("-"));
        out.put("msg_count", live.size());
        ArrayNode messages = out.putArray("messages");
        if (in.path("include_messages").asBoolean(false)) {
          int offset = in.path("offset").asInt(0);
          int limit = in.path("limit").asInt(100);
          for (Message m : live.subList(Math.min(offset, live.size()), Math.min(offset + limit, live.size()))) {
            ObjectNode e = messages.addObject();
            e.put("msg_id", m.msgId);
            e.put("agent", in.path("agent").asText());
            e.put("role", m.role);
            e.put("content", m.content);
            e.put("reasoning", "");
            e.put("user_id", "-");
            e.put("run_id", "-");
            e.set("metadata", m.metadata);
            e.put("tokens", 0);
            e.put("ts", "2026-01-01T00:00:00.000000");
          }
        }
        out.putNull("next_cursor");
      }
      case "conversation.delete" -> {
        if (!admin) {
          throw new Refusal(403, "FORBIDDEN", "conversation.delete needs the admin role");
        }
        if (!threadId.equals(in.path("confirm_thread_id").asText())) {
          throw new Refusal(400, "CONFIRM_THREAD_ID_MISMATCH", "confirm_thread_id must equal thread_id");
        }
        Thread t = threads.get(threadId);
        int total = 0;
        if (t != null) {
          for (Message m : t.messages) {
            if (!m.tombstoned) {
              m.tombstoned = true;
              total++;
            }
          }
          if (t.row != null) {
            t.row = null;
            total++;
          }
        }
        out.put("thread_id", threadId);
        out.put("tombstoned_at", "2026-01-01T00:00:00");
        out.put("total", total);
      }
      case "conversation.threads" -> {
        int offset = in.path("offset").asInt(0);
        int limit = in.path("limit").asInt(30);
        List<Map.Entry<String, Long>> active = new ArrayList<>();
        threads.forEach(
            (id, t) ->
                t.messages.stream()
                    .filter(m -> !m.tombstoned)
                    .mapToLong(m -> m.seq)
                    .max()
                    .ifPresent(last -> active.add(Map.entry(id, last))));
        active.sort(Comparator.comparing((Map.Entry<String, Long> e) -> e.getValue()).reversed());
        ArrayNode list = out.putArray("threads");
        for (Map.Entry<String, Long> e : active.subList(Math.min(offset, active.size()), Math.min(offset + limit, active.size()))) {
          ObjectNode s = list.addObject();
          s.put("thread_id", e.getKey());
          s.put("agent", in.path("agent").asText());
          s.put("msg_count", live(e.getKey()).size());
          s.put("last_seen", "2026-01-01T00:00:00");
          s.put("title", "");
        }
      }
      default -> throw new Refusal(404, "NOT_FOUND", "no op " + op);
    }
    return out;
  }

  private static final class Refusal extends RuntimeException {
    private static final long serialVersionUID = 1L;
    final int status;
    final transient ObjectNode detail;

    Refusal(int status, String code, String message) {
      super(message);
      this.status = status;
      this.detail = JSON.createObjectNode().put("code", code).put("message", message);
    }
  }
}
