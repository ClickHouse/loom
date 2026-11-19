package com.clickhouse.loom;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.clickhouse.loom.Results.DeleteResult;
import com.clickhouse.loom.Results.GetResult;
import com.clickhouse.loom.Results.SearchResult;
import com.clickhouse.loom.Results.SetResult;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

/** Against a local fake server: what each call sends, what it reads back, and the errors. */
class LoomClientTest {
  private static final ObjectMapper JSON = new ObjectMapper();

  /** One recorded request. */
  record Seen(String method, String path, Map<String, List<String>> headers, JsonNode body) {
    String header(String name) {
      return headers.entrySet().stream()
          .filter(e -> e.getKey().equalsIgnoreCase(name))
          .map(e -> e.getValue().get(0))
          .findFirst()
          .orElse(null);
    }
  }

  private HttpServer server;
  private final List<Seen> seen = new ArrayList<>();
  private volatile int status = 200;
  private volatile String answer = "{}";
  private volatile Map<String, String> answerHeaders = Map.of();

  @BeforeEach
  void start() throws IOException {
    server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    server.createContext(
        "/",
        ex -> {
          byte[] in = ex.getRequestBody().readAllBytes();
          seen.add(
              new Seen(
                  ex.getRequestMethod(),
                  ex.getRequestURI().getPath(),
                  Map.copyOf(ex.getRequestHeaders()),
                  JSON.readTree(in)));
          answerHeaders.forEach((k, v) -> ex.getResponseHeaders().add(k, v));
          ex.getResponseHeaders().add("Content-Type", "application/json");
          byte[] out = answer.getBytes(StandardCharsets.UTF_8);
          ex.sendResponseHeaders(status, out.length == 0 ? -1 : out.length);
          try (OutputStream os = ex.getResponseBody()) {
            os.write(out);
          }
        });
    server.start();
  }

  @AfterEach
  void stop() {
    server.stop(0);
  }

  private String url() {
    return "http://127.0.0.1:" + server.getAddress().getPort();
  }

  private LoomClient client(String token) {
    return LoomClient.builder()
        .baseUrl(url() + "/")
        .token(token)
        .org("dev")
        .namespace("local")
        .agent("java-test")
        .build();
  }

  private void answer(int status, String body) {
    this.status = status;
    this.answer = body;
  }

  @Test
  void setSendsTheIdentityKeyAndValueAndReadsTheVersion() {
    answer(200, "{\"version\":1731888000000000001,\"memory_id\":\"m-1\",\"content_sha256\":\"ab\","
        + "\"status\":\"active\",\"pinned\":false}");
    SetResult r =
        client("tok").set("seat_pref", Map.of("seat", "aisle"), Map.of("tags", List.of("travel")));

    Seen s = seen.get(0);
    assertEquals("POST", s.method());
    assertEquals("/v1/memory.set", s.path());
    assertEquals("Bearer tok", s.header("Authorization"));
    assertEquals("application/json", s.header("Content-Type"));
    assertEquals(LoomClient.USER_AGENT, s.header("User-Agent"));
    assertTrue(s.header("X-Request-Id").matches("[0-9a-f-]{36}"));
    assertEquals("dev", s.body().get("org").asText());
    assertEquals("local", s.body().get("namespace").asText());
    assertEquals("java-test", s.body().get("agent").asText());
    assertEquals("seat_pref", s.body().get("memory_key").asText());
    assertEquals("aisle", s.body().get("value").get("seat").asText());
    assertEquals("travel", s.body().get("tags").get(0).asText());

    assertEquals(1731888000000000001L, r.version()); // above 2^53, exact as a long
    assertEquals("m-1", r.memoryId());
    assertEquals("active", r.status());
  }

  @Test
  void noTokenSendsNoAuthorizationHeader() {
    answer(200, "{\"found\":false}");
    client(null).get("k");
    assertNull(seen.get(0).header("Authorization"));
  }

  @Test
  void getReadsTheValueAsWrittenOrNotFound() {
    answer(200, "{\"found\":true,\"memory_id\":\"m-2\",\"value\":\"Prefers an aisle seat.\","
        + "\"memory_type\":\"semantic\",\"version\":42,\"version_text\":\"42\"}");
    GetResult r = client(null).get("seat_pref");
    assertEquals("/v1/memory.get", seen.get(0).path());
    assertEquals("seat_pref", seen.get(0).body().get("memory_key").asText());
    assertTrue(r.found());
    assertEquals("Prefers an aisle seat.", r.value().asText());
    assertEquals(42L, r.version());

    answer(200, "{\"found\":false,\"value\":null,\"version\":null}");
    GetResult missing = client(null).get("nope");
    assertFalse(missing.found());
    assertNull(missing.version());
  }

  @Test
  void searchSendsQueryAndTopKAndReadsHitsInOrder() {
    answer(200, "{\"results\":[{\"memory_key\":\"a\",\"memory_type\":\"semantic\","
        + "\"content_excerpt\":\"aisle\",\"similarity\":0.91,\"rank_score\":0.8},"
        + "{\"memory_key\":\"b\",\"memory_type\":\"preference\",\"content_excerpt\":\"window\","
        + "\"similarity\":0.5,\"rank_score\":0.4}]}");
    SearchResult r = client(null).search("which seat?", 5);
    JsonNode body = seen.get(0).body();
    assertEquals("/v1/memory.search", seen.get(0).path());
    assertEquals("which seat?", body.get("query").asText());
    assertEquals(5, body.get("top_k").asInt());
    assertEquals(List.of("a", "b"), r.hits().stream().map(Results.SearchHit::memoryKey).toList());
    assertEquals(0.91, r.hits().get(0).similarity(), 1e-9);
  }

  @Test
  void deleteSendsTheKeyAndReadsTheTombstone() {
    answer(200, "{\"tombstoned\":true,\"found\":true,\"version\":7}");
    DeleteResult r = client(null).delete("seat_pref");
    assertEquals("/v1/memory.delete", seen.get(0).path());
    assertEquals("seat_pref", seen.get(0).body().get("memory_key").asText());
    assertTrue(r.tombstoned());
    assertEquals(7L, r.version());
  }

  @ParameterizedTest(name = "{0} {1}")
  @CsvSource({
    "422, VALIDATION_ERROR, input, false",
    "403, NAMESPACE_NOT_ACCESSIBLE, auth, false",
    "429, RATE_LIMITED, capacity, true",
    "503, BACKEND_UNAVAILABLE, dependency, true",
    "503, SCHEMA_PENDING, configuration, false",
    "500, INTERNAL_ERROR, internal, false",
  })
  void anErrorCarriesTheEnvelopesCodeCategoryAndRetryable(
      int status, String code, String category, boolean retryable) {
    answer(status, "{\"detail\":{\"code\":\"" + code + "\",\"message\":\"m\",\"category\":\""
        + category + "\",\"retryable\":" + retryable + ",\"retry_after_s\":2}}");
    answerHeaders = Map.of("X-Request-Id", "req-1");
    LoomException e = assertThrows(LoomException.class, () -> client(null).get("k"));
    assertEquals(status, e.status());
    assertEquals(code, e.code());
    assertEquals(category, e.category());
    assertEquals(retryable, e.retryable());
    assertEquals(2.0, e.retryAfterSeconds());
    assertEquals("req-1", e.requestId());
    assertEquals("memory.get", e.op());
    assertEquals("m", e.getMessage());
  }

  @Test
  void requestsAreHttp11WithoutAnH2cUpgrade() {
    // uvicorn rejects an "Upgrade: h2c" request with 400; the default HTTP/2 client sends one.
    answer(200, "{\"deleted\":true}");
    client(null).delete("k");
    assertNull(seen.get(0).header("Upgrade"));
    assertNull(seen.get(0).header("HTTP2-Settings"));
  }

  @Test
  void notFoundIsMatchedByCode() {
    answer(404, "{\"detail\":{\"code\":\"NOT_FOUND\",\"message\":\"no such key\","
        + "\"category\":\"input\",\"retryable\":false}}");
    LoomException e = assertThrows(LoomException.class, () -> client(null).delete("k"));
    assertEquals("NOT_FOUND", e.code());
    assertFalse(e.retryable());
  }

  @ParameterizedTest(name = "HTTP {0} without an envelope is {1}")
  @CsvSource({
    "400, input, false",
    "401, auth, false",
    "408, capacity, true",
    "429, capacity, true",
    "502, dependency, true",
    "500, internal, false",
  })
  void aBodyWithoutAnEnvelopeIsClassedByStatus(int status, String category, boolean retryable) {
    answer(status, "<html>proxy error</html>");
    answerHeaders = Map.of("Retry-After", "30");
    LoomException e = assertThrows(LoomException.class, () -> client(null).set("k", "v"));
    assertEquals("", e.code());
    assertEquals(category, e.category());
    assertEquals(retryable, e.retryable());
    assertEquals(30.0, e.retryAfterSeconds());
    assertEquals("<html>proxy error</html>", e.getMessage());
  }

  @Test
  void anUnreachableServerIsARetryableDependencyError() throws Exception {
    String dead = url();
    server.stop(0);
    LoomClient c =
        LoomClient.builder().baseUrl(dead).org("dev").namespace("local").agent("a")
            .timeout(Duration.ofSeconds(2)).build();
    LoomException e = assertThrows(LoomException.class, () -> c.get("k"));
    assertEquals(0, e.status());
    assertEquals("dependency", e.category());
    assertTrue(e.retryable());
    server = HttpServer.create(); // so stop() in @AfterEach has a server
  }

  @Test
  void theIdentityAndBaseUrlAreRequired() {
    assertThrows(
        IllegalArgumentException.class,
        () -> LoomClient.builder().baseUrl(url()).namespace("n").agent("a").build());
    assertThrows(
        IllegalArgumentException.class,
        () -> LoomClient.builder().baseUrl(url()).org("o").namespace("n").agent(" ").build());
  }
}
