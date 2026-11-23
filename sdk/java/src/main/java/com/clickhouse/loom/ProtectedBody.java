package com.clickhouse.loom;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Flow;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/** Bounded owned subscription: no response bytes escape before envelope observation. */
final class ProtectedBody implements HttpResponse.BodySubscriber<String> {
  static final int MAX_BYTES = 4 * 1024 * 1024;
  private final CompletableFuture<String> body = new CompletableFuture<>();
  private final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
  private Flow.Subscription subscription;
  private boolean stopped;

  static String encode(com.fasterxml.jackson.databind.ObjectMapper json,
      com.fasterxml.jackson.databind.node.ObjectNode input) {
    BoundedOutput output = new BoundedOutput();
    try { json.writeValue(output, input); }
    catch (IOException e) { throw new SessionProtection.Unavailable(e); }
    return output.toString(StandardCharsets.UTF_8);
  }

  private static final class BoundedOutput extends ByteArrayOutputStream {
    @Override public synchronized void write(byte[] value, int offset, int length) {
      if (length > MAX_BYTES - count) { throw new SessionProtection.Unavailable(); }
      super.write(value, offset, length);
    }
    @Override public synchronized void write(int value) {
      if (count == MAX_BYTES) { throw new SessionProtection.Unavailable(); }
      super.write(value);
    }
  }

  static HttpResponse<String> send(HttpClient http, HttpRequest request, Duration timeout)
      throws IOException, InterruptedException {
    ProtectedBody subscriber = new ProtectedBody();
    CompletableFuture<HttpResponse<String>> response = http.sendAsync(request, info -> {
      String type = info.headers().firstValue("Content-Type").orElse("")
          .split(";", 2)[0].trim().toLowerCase(Locale.ROOT);
      if (!type.equals("application/json") && !type.endsWith("+json")) {
        subscriber.cancel(new SessionProtection.Unavailable());
      }
      if (info.headers().firstValueAsLong("Content-Length").orElse(0) > MAX_BYTES) {
        subscriber.cancel(new SessionProtection.Unavailable());
      }
      return subscriber;
    });
    try {
      return response.get(timeout.toNanos(), TimeUnit.NANOSECONDS);
    } catch (TimeoutException e) {
      throw new HttpTimeoutException("protected response deadline exceeded");
    } catch (ExecutionException e) {
      if (e.getCause() instanceof RuntimeException runtime) { throw runtime; }
      throw new IOException("protected response failed", e.getCause());
    } finally {
      // Cancellation synchronizes with onNext and cancels the owned network subscription.
      subscriber.cancel(new IOException("response closed"));
      response.cancel(true);
    }
  }

  @Override public CompletionStage<String> getBody() { return body; }

  @Override public synchronized void onSubscribe(Flow.Subscription value) {
    if (subscription != null || stopped) { value.cancel(); return; }
    subscription = value;
    value.request(1);
  }

  @Override public synchronized void onNext(List<ByteBuffer> chunks) {
    if (stopped) { return; }
    for (ByteBuffer chunk : chunks) {
      if (chunk.remaining() > MAX_BYTES - bytes.size()) {
        cancel(new SessionProtection.Unavailable());
        return;
      }
      byte[] copy = new byte[chunk.remaining()];
      chunk.get(copy);
      bytes.writeBytes(copy);
    }
    subscription.request(1);
  }

  @Override public synchronized void onError(Throwable error) { cancel(error); }

  @Override public synchronized void onComplete() {
    if (!stopped) {
      try {
        String decoded = StandardCharsets.UTF_8.newDecoder()
            .onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
            .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(bytes.toByteArray())).toString();
        stopped = true;
        body.complete(decoded);
      } catch (java.nio.charset.CharacterCodingException e) {
        cancel(e);
      }
    }
  }

  synchronized void cancel(Throwable error) {
    if (!stopped) {
      stopped = true;
      if (subscription != null) { subscription.cancel(); }
      body.completeExceptionally(error);
    }
  }
}
