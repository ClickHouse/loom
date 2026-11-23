package com.clickhouse.loom;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledOnOs;
import org.junit.jupiter.api.condition.OS;
import org.junit.jupiter.api.io.TempDir;

/** Hosted only: real JDK HTTP, owned loopback fixtures, no Loom service or providers. */
@EnabledOnOs(OS.LINUX)
class SessionProtectionHttpTest {
  private static final ObjectMapper JSON = new ObjectMapper();
  private static final String PRIVATE = "{\"result\":{\"mirrored\":true}}";
  @TempDir Path temporary;

  record Seen(String method, String path, Map<String, List<String>> headers, JsonNode body) {
    List<String> header(String name) {
      return headers.entrySet().stream().filter(e -> e.getKey().equalsIgnoreCase(name))
          .map(Map.Entry::getValue).findFirst().orElse(List.of());
    }
  }

  @FunctionalInterface interface Answer { void send(HttpExchange exchange) throws IOException; }

  static final class Wire implements AutoCloseable {
    final HttpServer server;
    final ExecutorService executor = Executors.newFixedThreadPool(4);
    final BlockingQueue<Seen> seen = new LinkedBlockingQueue<>();
    final AtomicReference<Throwable> failure = new AtomicReference<>();
    volatile Answer answer = exchange -> json(exchange, 200, "{}");

    Wire() throws IOException {
      server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
      server.setExecutor(executor);
      server.createContext("/", exchange -> {
        try (exchange) {
          byte[] input = exchange.getRequestBody().readNBytes(4 * 1024 * 1024 + 1);
          if (input.length > 4 * 1024 * 1024) { throw new IOException("oversize request"); }
          seen.add(new Seen(exchange.getRequestMethod(), exchange.getRequestURI().getPath(),
              Map.copyOf(exchange.getRequestHeaders()), JSON.readTree(input)));
          answer.send(exchange);
        } catch (Throwable error) {
          failure.compareAndSet(null, error);
        }
      });
      server.start();
    }

    String url() { return "http://127.0.0.1:" + server.getAddress().getPort(); }
    Seen next() throws InterruptedException {
      Seen request = seen.poll(5, TimeUnit.SECONDS);
      assertNotNull(request, "wire request did not arrive");
      return request;
    }
    static void json(HttpExchange exchange, int status, String body) throws IOException {
      byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
      exchange.getResponseHeaders().set("Content-Type", "application/json");
      exchange.sendResponseHeaders(status, bytes.length);
      exchange.getResponseBody().write(bytes);
    }
    @Override public void close() {
      server.stop(0);
      executor.shutdownNow();
      try { assertTrue(executor.awaitTermination(5, TimeUnit.SECONDS), "fixture worker leaked"); }
      catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new AssertionError(e); }
      assertNull(failure.get(), "HTTP fixture failed");
    }
  }

  private SessionProtection family(String name) {
    return SessionProtection.create(temporary.resolve(name));
  }

  private LoomClient client(Wire wire, SessionProtection family, String namespace, String token) {
    LoomClient.Builder builder = LoomClient.builder().baseUrl(wire.url()).org("test-org")
        .namespace(namespace).agent("java-test").token(token).timeout(Duration.ofSeconds(3));
    if (family != null) { builder.sessionProtection(family); }
    return builder.build();
  }

  private static void declared(Seen seen) {
    assertEquals("POST", seen.method());
    assertEquals(List.of("v1"), seen.header("x-loom-session-protection"));
    assertEquals(List.of("application/json"), seen.header("Content-Type"));
  }

  @Test void successfulReadPersistsAcrossClientsTokensNamespacesOriginsAndResume() throws Exception {
    SessionProtection family = family("conversation");
    SessionProtection.Capture capture = family.capture();
    try (Wire wire = new Wire(); Wire other = new Wire()) {
      LoomClient reader = client(wire, family, "private", "reader");
      wire.answer = exchange -> Wire.json(exchange, 200, PRIVATE);
      assertTrue(reader.sourceMcpRead(Map.of("name", "selected", "resource_id", "doc"))
          .path("result").path("mirrored").asBoolean());
      Seen read = wire.next();
      declared(read);
      assertEquals("/v1/knowledge.source.mcp_read", read.path());
      assertEquals("reader", read.header("Authorization").get(0).substring(7));
      assertEquals("selected", read.body().path("name").asText());
      assertEquals("doc", read.body().path("resource_id").asText());
      assertEquals("private", read.body().path("namespace").asText());
      assertEquals("test-org", read.body().path("org").asText());
      assertEquals(4, read.body().size());
      SessionProtection.Pointer pointer = family.pointer();
      SessionProtection reopened = SessionProtection.open(new SessionProtection.Pointer(
          Path.of(pointer.directory().toString()), pointer.identity(), pointer.directoryKey(), pointer.stateKey()));
      LoomClient rebuilt = client(other, reopened, "public", "writer");
      assertThrows(SessionProtection.Protected.class, () -> rebuilt.set("key", "value"));
      LoomClient unbound = client(other, null, "public", "different");
      assertThrows(SessionProtection.Protected.class,
          () -> unbound.callAutomatic(capture, "memory.set", Map.of("memory_key", "key", "value", 1)));
      assertTrue(other.seen.isEmpty());
      rebuilt.callHuman("memory.set", Map.of("org", "test-org", "namespace", "public",
          "agent", "java-test", "memory_key", "key", "value", "human"));
      Seen human = other.next();
      declared(human);
      assertEquals("/v1/memory.set", human.path());
      assertEquals("public", human.body().path("namespace").asText());
      assertEquals(List.of("Bearer writer"), human.header("Authorization"));
      assertEquals("human", human.body().path("value").asText());
      assertThrows(SessionProtection.Protected.class, () -> rebuilt.set("again", "automatic"));
      assertTrue(other.seen.isEmpty());
    }
  }

  @Test void nonSuccessErrorAndTypedMethodsObserveBeforeReturning() throws Exception {
    try (Wire wire = new Wire()) {
      SessionProtection errorFamily = family("error");
      LoomClient errorClient = client(wire, errorFamily, "private", "token");
      wire.answer = exchange -> Wire.json(exchange, 409,
          "{\"error\":{\"code\":\"CONFLICT\",\"data\":{\"mirrored\":true}}}");
      assertThrows(LoomException.class, () -> errorClient.call("custom.read", Map.of()));
      declared(wire.next());
      assertThrows(SessionProtection.Protected.class, () -> errorClient.delete("key"));
      SessionProtection decodeFamily = family("decode");
      LoomClient decode = client(wire, decodeFamily, "private", "token");
      wire.answer = exchange -> Wire.json(exchange, 200, "{\"items\":[{\"mirrored\":true}]}");
      // The raw observer precedes every typed method even if its result has unexpected fields.
      decode.get("key");
      declared(wire.next());
      assertThrows(SessionProtection.Protected.class, () -> decode.set("key", 1));
      assertTrue(wire.seen.isEmpty());
    }
  }

  @Test void cleanResponsesAndOrdinaryUnboundRequestsKeepTheirBehavior() throws Exception {
    try (Wire wire = new Wire()) {
      LoomClient ordinary = client(wire, null, "ordinary", "token");
      wire.answer = exchange -> Wire.json(exchange, 200, PRIVATE);
      ordinary.call("custom.read", Map.of());
      assertTrue(wire.next().header("x-loom-session-protection").isEmpty());
      wire.answer = exchange -> Wire.json(exchange, 200, "{}");
      ordinary.call("custom.write", Map.of());
      assertTrue(wire.next().header("x-loom-session-protection").isEmpty());
      SessionProtection clean = family("clean");
      LoomClient bound = client(wire, clean, "clean", "token");
      bound.call("custom.first", Map.of());
      declared(wire.next());
      bound.sourceMcpPause(Map.of("name", "selected"));
      declared(wire.next());
      SessionProtection.open(clean.pointer());
      bound.call("custom.next", Map.of());
      declared(wire.next());
    }
  }

  @Test void boundProtectedBIsCheckedWhenRetainedAIsClean() throws Exception {
    try (Wire wire = new Wire()) {
      SessionProtection cleanA = family("a");
      SessionProtection protectedB = family("b");
      LoomClient clientB = client(wire, protectedB, "private", "token");
      wire.answer = exchange -> Wire.json(exchange, 200, PRIVATE);
      clientB.call("custom.read", Map.of());
      declared(wire.next());
      assertThrows(SessionProtection.Protected.class, () -> clientB.callAutomatic(cleanA.capture(),
          "memory.set", Map.of("memory_key", "key", "value", 1)));
      assertTrue(wire.seen.isEmpty());
      wire.answer = exchange -> Wire.json(exchange, 200, "{}");
      client(wire, cleanA, "ordinary", "token").call("custom.write", Map.of());
      declared(wire.next());
    }
  }

  @Test void captureRetainsUnionThroughTasksExceptionsAndPerInvocationHumanCalls() throws Exception {
    try (Wire wire = new Wire()) {
      SessionProtection a = family("capture-a");
      SessionProtection b = family("capture-b");
      LoomClient boundB = client(wire, b, "private", "token");
      wire.answer = exchange -> Wire.json(exchange, 200, PRIVATE);
      SessionProtection.Capture retained = a.capture();
      retained.call(() -> boundB.call("custom.read", Map.of()));
      declared(wire.next());
      LoomClient newClient = client(wire, null, "destination", "new-token");
      ExecutorService task = Executors.newSingleThreadExecutor();
      try {
        Future<Boolean> result = task.submit(() -> {
          assertThrows(SessionProtection.Protected.class,
              () -> newClient.callAutomatic(retained, "custom.write", Map.of()));
          assertThrows(IOException.class, () -> retained.call(() -> { throw new IOException("callback"); }));
          newClient.call("custom.ordinary", Map.of());
          return true;
        });
        assertTrue(result.get(5, TimeUnit.SECONDS));
        assertTrue(wire.next().header("x-loom-session-protection").isEmpty());
      } finally {
        task.shutdownNow();
        assertTrue(task.awaitTermination(5, TimeUnit.SECONDS));
      }
      wire.answer = exchange -> Wire.json(exchange, 200, "{}");
      retained.call(() -> newClient.callHuman("custom.write", Map.of()));
      declared(wire.next());
      assertThrows(SessionProtection.Protected.class,
          () -> newClient.callAutomatic(retained, "custom.write", Map.of()));
      assertTrue(wire.seen.isEmpty());
    }
  }

  @Test void missingReplacedCorruptAndPendingProofNeverCreateACleanResume() throws Exception {
    try (Wire wire = new Wire()) {
      SessionProtection missing = family("missing");
      Files.delete(missing.pointer().directory().resolve("state"));
      assertThrows(SessionProtection.Unavailable.class, () -> SessionProtection.open(missing.pointer()));
      assertThrows(SessionProtection.Unavailable.class,
          () -> client(wire, missing, "n", "t").callHuman("custom.write", Map.of()));
      SessionProtection replaced = family("replaced");
      Path original = replaced.pointer().directory().resolve("state");
      Path saved = temporary.resolve("saved-authority");
      Files.move(original, saved);
      Files.copy(saved, original);
      Files.setPosixFilePermissions(original, PosixFilePermissions.fromString("rw-------"));
      assertThrows(SessionProtection.Unavailable.class, () -> SessionProtection.open(replaced.pointer()));
      SessionProtection corrupt = family("corrupt");
      Files.writeString(corrupt.pointer().directory().resolve("state"), "corrupt");
      assertThrows(SessionProtection.Unavailable.class, () -> SessionProtection.open(corrupt.pointer()));
      SessionProtection pending = family("pending");
      try (SessionProtection.Observation inFlight = SessionProtection.begin(pending, false)) {
        assertTrue(inFlight.bound());
        SessionProtection resumed = SessionProtection.open(pending.pointer());
        assertThrows(SessionProtection.Protected.class,
            () -> client(wire, resumed, "n", "t").call("custom.write", Map.of()));
      }
      assertTrue(wire.seen.isEmpty());
    }
  }

  @Test void malformedMetadataNestedJsonAndOversizeResponsesFailClosed() throws Exception {
    List<String> invalid = List.of("not json", "{} {}", "{\"mirrored\":\"true\"}",
        "{\"mirrored\":true,\"mirrored\":false}",
        "{\"content\":[{\"type\":\"text\",\"text\":\"{invalid\"}]}",
        "{\"payload\":\"" + "x".repeat(4 * 1024 * 1024) + "\"}");
    try (Wire wire = new Wire()) {
      int index = 0;
      for (String body : invalid) {
        SessionProtection family = family("invalid-" + index++);
        LoomClient client = client(wire, family, "n", "t");
        // Chunked bodies exercise the subscriber's byte bound without relying on Content-Length.
        wire.answer = exchange -> {
          exchange.getResponseHeaders().set("Content-Type", "application/json");
          exchange.sendResponseHeaders(200, 0);
          try { exchange.getResponseBody().write(body.getBytes(StandardCharsets.UTF_8)); }
          catch (IOException cancelled) { /* The bounded client may close before the final chunk. */ }
        };
        assertThrows(RuntimeException.class, () -> client.call("custom.read", Map.of()));
        declared(wire.next());
        assertThrows(SessionProtection.Protected.class, () -> client.call("custom.write", Map.of()));
        assertTrue(wire.seen.isEmpty());
      }
    }
  }

  @Test void nestedTextProtectionAndRetainedNestedContributorsReachTheWireObserver() throws Exception {
    try (Wire wire = new Wire()) {
      SessionProtection a = family("nested-a");
      SessionProtection b = family("nested-b");
      SessionProtection.Capture retained = a.capture().call(b::capture);
      LoomClient reader = client(wire, null, "n", "t");
      String inner = JSON.writeValueAsString(Map.of("mirrored", true));
      String envelope = JSON.writeValueAsString(Map.of("structuredContent", Map.of(
          "content", List.of(Map.of("type", "text", "text", inner)))));
      wire.answer = exchange -> Wire.json(exchange, 200, envelope);
      reader.callAutomatic(retained, "custom.read", Map.of());
      declared(wire.next());
      assertThrows(SessionProtection.Protected.class,
          () -> client(wire, a, "other", "new").call("custom.write", Map.of()));
      assertThrows(SessionProtection.Protected.class,
          () -> client(wire, b, "other", "new").call("custom.write", Map.of()));
      assertTrue(wire.seen.isEmpty());
    }
  }

  @Test void oversizeRequestAndInvalidUtf8RefuseBeforeAnyPublication() throws Exception {
    try (Wire wire = new Wire()) {
      SessionProtection requestFamily = family("request-limit");
      LoomClient requestClient = client(wire, requestFamily, "n", "t");
      assertThrows(SessionProtection.Unavailable.class, () -> requestClient.call("custom.write",
          Map.of("value", "x".repeat(4 * 1024 * 1024))));
      assertTrue(wire.seen.isEmpty());
      assertThrows(SessionProtection.Protected.class,
          () -> requestClient.call("custom.next", Map.of()));
      SessionProtection utf8Family = family("utf8");
      LoomClient utf8Client = client(wire, utf8Family, "n", "t");
      wire.answer = exchange -> {
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        byte[] bytes = new byte[] {'{', '"', 'x', '"', ':', '"', (byte) 0xc3, '"', '}'};
        exchange.sendResponseHeaders(200, bytes.length);
        exchange.getResponseBody().write(bytes);
      };
      assertThrows(RuntimeException.class, () -> utf8Client.call("custom.read", Map.of()));
      declared(wire.next());
      assertThrows(SessionProtection.Protected.class, () -> utf8Client.call("custom.write", Map.of()));
      assertTrue(wire.seen.isEmpty());
    }
  }

  @Test void pendingResponseBlocksNewAutomaticSendsAndCancellationRetainsProof() throws Exception {
    try (Wire wire = new Wire()) {
      SessionProtection family = family("cancel");
      LoomClient client = client(wire, family, "n", "t");
      CountDownLatch started = new CountDownLatch(1);
      CountDownLatch cancelled = new CountDownLatch(1);
      wire.answer = exchange -> {
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(200, 0);
        exchange.getResponseBody().write("{".getBytes(StandardCharsets.UTF_8));
        exchange.getResponseBody().flush();
        started.countDown();
        try {
          byte[] chunk = new byte[4096];
          for (int i = 0; i < 2048; i++) {
            exchange.getResponseBody().write(chunk);
            exchange.getResponseBody().flush();
            try { TimeUnit.MILLISECONDS.sleep(5); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); break; }
          }
        } catch (IOException closed) { cancelled.countDown(); }
      };
      AtomicReference<Throwable> outcome = new AtomicReference<>();
      Thread request = new Thread(() -> {
        try { client.call("custom.read", Map.of()); }
        catch (Throwable error) { outcome.set(error); }
      });
      request.start();
      try {
        assertTrue(started.await(5, TimeUnit.SECONDS));
        declared(wire.next());
        assertThrows(SessionProtection.Protected.class, () -> client.call("custom.write", Map.of()));
        request.interrupt();
        request.join(5000);
        assertFalse(request.isAlive(), "cancelled request did not join");
        assertNotNull(outcome.get());
        assertTrue(cancelled.await(5, TimeUnit.SECONDS), "response connection did not close");
        SessionProtection resumed = SessionProtection.open(family.pointer());
        assertThrows(SessionProtection.Protected.class,
            () -> client(wire, resumed, "new", "new").call("custom.write", Map.of()));
        assertTrue(wire.seen.isEmpty());
      } finally {
        request.interrupt();
        request.join(5000);
        assertFalse(request.isAlive());
      }
    }
  }

  @Test void unsupportedStreamRefusesBeforePublicationAndTruncatedJsonRetainsProof() throws Exception {
    try (Wire wire = new Wire()) {
      SessionProtection stream = family("stream");
      LoomClient client = client(wire, stream, "n", "t");
      wire.answer = exchange -> {
        exchange.getResponseHeaders().set("Content-Type", "text/event-stream");
        exchange.sendResponseHeaders(200, 0);
        try { exchange.getResponseBody().write("data: {}\n\n".getBytes(StandardCharsets.UTF_8)); }
        catch (IOException closed) { /* Media refusal cancels the response subscription. */ }
      };
      assertThrows(RuntimeException.class, () -> client.call("custom.read", Map.of()));
      declared(wire.next());
      assertThrows(SessionProtection.Protected.class, () -> client.call("custom.write", Map.of()));
      SessionProtection truncated = family("truncated");
      LoomClient other = client(wire, truncated, "n", "t");
      wire.answer = exchange -> Wire.json(exchange, 200, "{\"result\":");
      assertThrows(SessionProtection.Unavailable.class, () -> other.call("custom.read", Map.of()));
      declared(wire.next());
      assertThrows(SessionProtection.Protected.class, () -> other.call("custom.write", Map.of()));
      assertTrue(wire.seen.isEmpty());
    }
  }

  @Test void redirectsAndCustomRoutesCannotBypassTheObservedBoundary() throws Exception {
    try (Wire wire = new Wire()) {
      SessionProtection family = family("redirect");
      LoomClient client = LoomClient.builder().baseUrl(wire.url()).org("test-org")
          .namespace("n").agent("java-test").sessionProtection(family)
          .httpClient(HttpClient.newBuilder().followRedirects(HttpClient.Redirect.ALWAYS).build()).build();
      assertThrows(SessionProtection.Unavailable.class, () -> client.call("custom.read", Map.of()));
      LoomClient guarded = client(wire, family("route"), "n", "t");
      assertThrows(IllegalArgumentException.class,
          () -> guarded.call("../custom", Map.of()));
      assertTrue(wire.seen.isEmpty());
    }
  }
}
