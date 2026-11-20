package com.clickhouse.loom;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;

/**
 * A non-2xx answer from Loom, or a call that got no answer.
 *
 * <p>Match on {@link #code()} (for example {@code NOT_FOUND} or {@code RATE_LIMITED}), never on
 * the message. {@link #category()} says whose move it is ({@code input}, {@code auth}, {@code
 * capacity}, {@code dependency}, {@code configuration} or {@code internal}) and {@link
 * #retryable()} whether resending the same request can succeed later. When the body names
 * neither, both come from the HTTP status, as docs/reference/errors.md describes.
 */
public final class LoomException extends RuntimeException {
  private static final long serialVersionUID = 1L;

  private final String op;
  private final int status;
  private final String code;
  private final String category;
  private final boolean retryable;
  private final Double retryAfterSeconds;
  private final String requestId;
  private final transient JsonNode detail;

  LoomException(
      String op,
      int status,
      String code,
      String message,
      String category,
      boolean retryable,
      Double retryAfterSeconds,
      String requestId,
      JsonNode detail,
      Throwable cause) {
    super(message, cause);
    this.op = op;
    this.status = status;
    this.code = code;
    this.category = category;
    this.retryable = retryable;
    this.retryAfterSeconds = retryAfterSeconds;
    this.requestId = requestId;
    this.detail = detail == null ? JsonNodeFactory.instance.objectNode() : detail;
  }

  /** The error for a non-2xx answer: the envelope's fields, else the status's class. */
  static LoomException fromResponse(
      String op, int status, JsonNode body, String requestId, Double retryAfterHeader) {
    JsonNode detail = body == null ? null : body.get("detail");
    if (detail != null && detail.isTextual()) {
      detail = JsonNodeFactory.instance.objectNode().put("message", detail.asText());
    } else if (detail == null || !detail.isObject()) {
      detail = JsonNodeFactory.instance.objectNode();
    }
    String code = detail.path("code").asText("");
    String message = detail.path("message").asText("");
    if (message.isEmpty()) {
      message = op + ": HTTP " + status;
    }
    String category =
        detail.hasNonNull("category") ? detail.get("category").asText() : categoryOf(status);
    boolean retryable =
        detail.hasNonNull("retryable") ? detail.get("retryable").asBoolean() : retryableOf(status);
    Double retryAfter =
        detail.hasNonNull("retry_after_s") ? Double.valueOf(detail.get("retry_after_s").asDouble()) : retryAfterHeader;
    return new LoomException(
        op, status, code, message, category, retryable, retryAfter, requestId, detail, null);
  }

  /** The class docs/reference/errors.md gives a status whose body names none. */
  static String categoryOf(int status) {
    if (status == 401 || status == 403) {
      return "auth";
    }
    if (status == 408 || status == 429) {
      return "capacity";
    }
    if (status == 502 || status == 503 || status == 504) {
      return "dependency";
    }
    if (status >= 500) {
      return "internal";
    }
    return "input";
  }

  static boolean retryableOf(int status) {
    return status == 408 || status == 429 || status == 502 || status == 503 || status == 504;
  }

  /** The op that failed, for example {@code memory.get}. */
  public String op() {
    return op;
  }

  /** The HTTP status; 0 when no answer came (a connection failure or a timeout). */
  public int status() {
    return status;
  }

  /** The server's {@code detail.code}; empty when the answer named none. */
  public String code() {
    return code;
  }

  public String category() {
    return category;
  }

  public boolean retryable() {
    return retryable;
  }

  /** Seconds to wait before resending ({@code detail.retry_after_s} or {@code Retry-After}). */
  public Double retryAfterSeconds() {
    return retryAfterSeconds;
  }

  /** The {@code X-Request-Id} of the call, which finds it in the server's log. */
  public String requestId() {
    return requestId;
  }

  /** The whole {@code detail} object, for the fields a code adds (such as {@code current_sha256}). */
  public JsonNode detail() {
    return detail;
  }
}
