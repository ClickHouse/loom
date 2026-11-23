package com.clickhouse.loom;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.Callable;

/** Application-owned monotonic conversation authority. See SESSION_PROTECTION.md. */
public final class SessionProtection {
  private static final Object LOCK = new Object();
  private static final ThreadLocal<Capture> ACTIVE = new ThreadLocal<>();
  private static final ObjectMapper JSON = new ObjectMapper(
      com.fasterxml.jackson.core.JsonFactory.builder().enable(
          com.fasterxml.jackson.core.StreamReadFeature.STRICT_DUPLICATE_DETECTION).build())
      .enable(com.fasterxml.jackson.databind.DeserializationFeature.FAIL_ON_TRAILING_TOKENS);
  private static final int MAX_PENDING = 1024;
  private final Pointer pointer;

  /** Persist independently from mutable client configuration in trusted application storage. */
  public record Pointer(Path directory, String identity, String directoryKey, String stateKey) {
    public Pointer {
      java.util.Objects.requireNonNull(directory, "directory");
      java.util.Objects.requireNonNull(identity, "identity");
      java.util.Objects.requireNonNull(directoryKey, "directoryKey");
      java.util.Objects.requireNonNull(stateKey, "stateKey");
      if (!identity.matches("[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")) {
        throw new Unavailable();
      }
    }
  }

  /** Lost or unverifiable proof is terminal until the application repairs its authority. */
  public static final class Unavailable extends IllegalStateException {
    private static final long serialVersionUID = 1L;
    Unavailable(Throwable cause) { super("session protection proof unavailable", cause); }
    Unavailable() { this(null); }
  }

  /** Automatic publication requires a separate human invocation after protection. */
  public static final class Protected extends IllegalStateException {
    private static final long serialVersionUID = 1L;
    Protected() { super("automatic operation refused by session protection"); }
  }

  private SessionProtection(Pointer pointer) {
    if (!"Linux".equals(System.getProperty("os.name"))) { throw new Unavailable(); }
    this.pointer = pointer;
  }

  /** Creates a new conversation only; never use as a resume fallback. Parent must exist. */
  public static SessionProtection create(Path directory) {
    synchronized (LOCK) {
      try {
        if (!"Linux".equals(System.getProperty("os.name"))) { throw new Unavailable(); }
        Path root = directory.toAbsolutePath().normalize();
        Files.createDirectory(root, PosixFilePermissions.asFileAttribute(
            PosixFilePermissions.fromString("rwx------")));
        String id = UUID.randomUUID().toString();
        Path state = root.resolve("state");
        Files.createFile(state, PosixFilePermissions.asFileAttribute(
            PosixFilePermissions.fromString("rw-------")));
        try (FileChannel channel = FileChannel.open(state, StandardOpenOption.WRITE)) {
          write(channel, id + "\n0\n");
        }
        sync(root);
        sync(root.getParent());
        return open(new Pointer(root, id, key(root), key(state)));
      } catch (IOException | UnsupportedOperationException e) {
        throw new Unavailable(e);
      }
    }
  }

  public static SessionProtection open(Pointer pointer) {
    SessionProtection family = new SessionProtection(java.util.Objects.requireNonNull(pointer));
    family.access(false, false, null, null, null, true);
    return family;
  }

  public Pointer pointer() { return pointer; }
  @Override public boolean equals(Object other) {
    return other instanceof SessionProtection family && pointer.equals(family.pointer);
  }
  @Override public int hashCode() { return pointer.hashCode(); }

  public Capture capture() {
    Capture current = ACTIVE.get();
    return current == null ? new Capture(List.of(this)) : current.contribute(this);
  }

  /** Immutable contributor ownership; pass this object explicitly into queued tasks. */
  public static final class Capture {
    private final List<SessionProtection> families;
    private Capture(List<SessionProtection> families) {
      if (families.isEmpty() || families.size() > 32) { throw new Unavailable(); }
      this.families = List.copyOf(families);
    }
    public Capture contribute(SessionProtection family) {
      Set<SessionProtection> all = new LinkedHashSet<>(families);
      all.add(java.util.Objects.requireNonNull(family));
      return new Capture(new ArrayList<>(all));
    }
    public <T> T call(Callable<T> work) throws Exception {
      Capture previous = ACTIVE.get();
      Set<SessionProtection> union = new LinkedHashSet<>(families);
      if (previous != null) { union.addAll(previous.families); }
      ACTIVE.set(new Capture(new ArrayList<>(union)));
      try { return work.call(); }
      finally { if (previous == null) { ACTIVE.remove(); } else { ACTIVE.set(previous); } }
    }
  }

  static Observation begin(SessionProtection bound, boolean human) {
    Set<SessionProtection> families = new LinkedHashSet<>();
    Capture capture = ACTIVE.get();
    if (capture != null) { families.addAll(capture.families); }
    if (bound != null) { families.add(bound); }
    Observation observation = new Observation(new ArrayList<>(families));
    try {
      synchronized (LOCK) {
        for (SessionProtection family : families) { family.access(!human, false, null); }
        for (SessionProtection family : families) {
          String marker = UUID.randomUUID() + ".pending";
          family.access(!human, false, marker);
          observation.markers.add(marker);
        }
      }
      return observation;
    } catch (RuntimeException e) {
      observation.close();
      throw e;
    }
  }

  static final class Observation implements AutoCloseable {
    private final List<SessionProtection> families;
    private final List<String> markers = new ArrayList<>();
    private boolean complete;
    Observation(List<SessionProtection> families) {
      if (families.size() > 32) { throw new Unavailable(); }
      this.families = families;
    }
    boolean bound() { return !families.isEmpty(); }
    void beforeSend(boolean human) {
      synchronized (LOCK) {
        for (int i = 0; i < markers.size(); i++) {
          families.get(i).access(!human, false, null, markers.get(i));
        }
      }
    }
    void observe(String raw) {
      JsonNode value;
      try { value = JSON.readTree(raw); }
      catch (IOException e) { throw new Unavailable(e); }
      boolean marked;
      try { marked = marked(value); }
      catch (IOException | RuntimeException e) { throw new Unavailable(e); }
      for (SessionProtection family : families) { family.access(false, marked, null); }
      // Remove pending proof only after every contributor acknowledges observation.
      for (int i = 0; i < markers.size(); i++) { families.get(i).finish(markers.get(i)); }
      complete = true;
    }
    @Override public void close() {
      if (!complete) {
        for (int i = 0; i < markers.size(); i++) {
          try { families.get(i).access(false, true, null); }
          catch (RuntimeException ignored) { /* Durable pending proof remains on failure. */ }
        }
      }
    }
  }

  private static boolean marked(JsonNode root) throws IOException {
    if (root == null || !root.isObject()) { throw new Unavailable(); }
    ArrayDeque<JsonNode> todo = new ArrayDeque<>();
    todo.add(root);
    int remaining = 10000;
    int textBudget = 4 * 1024 * 1024;
    boolean protectedValue = false;
    while (!todo.isEmpty()) {
      if (--remaining < 0) { throw new Unavailable(); }
      JsonNode node = todo.removeLast();
      if (!node.isObject()) { continue; }
      JsonNode mirrored = node.get("mirrored");
      if (mirrored != null) {
        if (!mirrored.isBoolean()) { throw new Unavailable(); }
        protectedValue |= mirrored.booleanValue();
      }
      for (String field : List.of("result", "structuredContent", "data", "error")) {
        JsonNode child = node.get(field);
        if (child != null && child.isObject()) { todo.add(child); }
      }
      for (String field : List.of("items", "content")) {
        JsonNode children = node.get(field);
        if (children == null || !children.isArray()) { continue; }
        if (children.size() > remaining - todo.size()) { throw new Unavailable(); }
        for (JsonNode child : children) {
          todo.add(child);
          if ("content".equals(field) && "text".equals(child.path("type").asText())
              && child.path("text").isTextual()) {
            String text = child.get("text").textValue();
            textBudget -= text.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
            if (textBudget < 0) { throw new Unavailable(); }
            if (text.stripLeading().startsWith("{")) { todo.add(JSON.readTree(text)); }
          }
        }
      }
    }
    return protectedValue;
  }

  private void access(boolean automatic, boolean protect, String marker) {
    access(automatic, protect, marker, null);
  }

  private void access(boolean automatic, boolean protect, String marker, String ownMarker) {
    access(automatic, protect, marker, ownMarker, null, false);
  }

  private void access(boolean automatic, boolean protect, String marker, String ownMarker,
      String removeMarker, boolean resume) {
    synchronized (LOCK) {
      try {
        validate();
        try (FileChannel channel = FileChannel.open(pointer.directory().resolve("state"),
            StandardOpenOption.READ, StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS);
            FileLock fileLock = channel.tryLock()) {
          if (fileLock == null || !fileLock.isValid()) { throw new Unavailable(); }
          validate();
          ByteBuffer bytes = ByteBuffer.allocate(65537);
          while (channel.read(bytes) > 0) {
            if (!bytes.hasRemaining()) { throw new Unavailable(); }
          }
          bytes.flip();
          String stored = java.nio.charset.StandardCharsets.US_ASCII.decode(bytes).toString();
          if (stored.length() < 65 || !stored.endsWith("\n")) { throw new Unavailable(); }
          String state = stored.substring(0, stored.length() - 65);
          if (!stored.substring(stored.length() - 65, stored.length() - 1).equals(digest(state))) {
            throw new Unavailable();
          }
          String prefix = pointer.identity() + "\n";
          if (!state.startsWith(prefix)) { throw new Unavailable(); }
          String rest = state.substring(prefix.length());
          if (!rest.startsWith("0\n") && !rest.startsWith("1\n")) { throw new Unavailable(); }
          boolean latched = rest.charAt(0) == '1';
          String tokens = rest.substring(2);
          Set<String> pending = new java.util.TreeSet<>();
          if (!tokens.isEmpty()) {
            if (!tokens.endsWith("\n")) { throw new Unavailable(); }
            String[] entries = tokens.split("\n", -1);
            for (int i = 0; i < entries.length - 1; i++) {
              String token = entries[i];
              if (!token.matches("[0-9a-f-]{36}\\.pending") || !pending.add(token)
                  || pending.size() > MAX_PENDING) { throw new Unavailable(); }
            }
          }
          if (ownMarker != null && !pending.contains(ownMarker)) { throw new Unavailable(); }
          boolean otherPending = pending.stream().anyMatch(token -> !token.equals(ownMarker));
          if (automatic && (latched || otherPending)) { throw new Protected(); }
          if (resume && !pending.isEmpty()) { protect = true; }
          if (marker != null && (!pending.add(marker) || pending.size() > MAX_PENDING)) {
            throw new Unavailable();
          }
          if (removeMarker != null && !pending.remove(removeMarker)) { throw new Unavailable(); }
          if (protect || marker != null || removeMarker != null) {
            StringBuilder updated = new StringBuilder(prefix).append(latched || protect ? "1\n" : "0\n");
            for (String token : pending) { updated.append(token).append('\n'); }
            write(channel, updated.toString());
          }
        }
      } catch (IOException | UnsupportedOperationException e) { throw new Unavailable(e); }
    }
  }

  private void finish(String marker) { access(false, false, null, null, marker, false); }

  private void validate() throws IOException {
    Path root = pointer.directory();
    Path state = root.resolve("state");
    if (!root.isAbsolute() || !root.normalize().equals(root)
        || !Files.isDirectory(root, LinkOption.NOFOLLOW_LINKS)
        || !Files.isRegularFile(state, LinkOption.NOFOLLOW_LINKS)
        || !key(root).equals(pointer.directoryKey()) || !key(state).equals(pointer.stateKey())
        || !Files.getPosixFilePermissions(root, LinkOption.NOFOLLOW_LINKS)
            .equals(PosixFilePermissions.fromString("rwx------"))
        || !Files.getPosixFilePermissions(state, LinkOption.NOFOLLOW_LINKS)
            .equals(PosixFilePermissions.fromString("rw-------"))
        || !Files.getOwner(root).equals(Files.getOwner(state))
        || Files.size(state) < 104 || Files.size(state) > 65536) {
      throw new Unavailable();
    }
    try (DirectoryStream<Path> entries = Files.newDirectoryStream(root)) {
      for (Path entry : entries) {
        if (!entry.getFileName().toString().equals("state")) { throw new Unavailable(); }
      }
    }
  }

  private static String key(Path path) throws IOException {
    Object key = Files.readAttributes(path, BasicFileAttributes.class,
        LinkOption.NOFOLLOW_LINKS).fileKey();
    if (key == null) { throw new Unavailable(); }
    return key.toString();
  }

  private static String digest(String value) {
    try {
      byte[] hash = java.security.MessageDigest.getInstance("SHA-256").digest(
          value.getBytes(java.nio.charset.StandardCharsets.US_ASCII));
      return java.util.HexFormat.of().formatHex(hash);
    } catch (java.security.NoSuchAlgorithmException e) { throw new Unavailable(e); }
  }

  private static void write(FileChannel channel, String value) throws IOException {
    String stored = value + digest(value) + "\n";
    ByteBuffer bytes = java.nio.charset.StandardCharsets.US_ASCII.encode(stored);
    channel.position(0);
    while (bytes.hasRemaining()) { channel.write(bytes); }
    channel.truncate(stored.length());
    channel.force(true);
  }

  private static void sync(Path root) throws IOException {
    try (FileChannel directory = FileChannel.open(root, StandardOpenOption.READ)) {
      directory.force(true);
    }
  }
}
