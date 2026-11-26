package com.clickhouse.loom;

import com.clickhouse.loom.Results.ConversationGetResult;
import com.clickhouse.loom.Results.ConversationMessage;
import com.clickhouse.loom.Results.ConversationThread;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;

/**
 * A chat memory's message list kept in one Loom conversation thread, for framework adapters.
 *
 * <p>The thread's log stays append-only: nothing is deleted to shorten a window, and {@code
 * conversation.search} still finds every turn. The thread's metadata says which turns form the
 * window: every turn from log position {@value #WINDOW_START} on, after the turns at the earlier
 * positions listed in {@value #WINDOW_HEAD} (absent when none; in practice a system message a
 * window memory keeps first while it evicts the turns after it).
 *
 * <p>{@link #replace} reuses stored turns for the longest start of the new list that the window
 * holds in order, appends the rest, and rewrites the metadata only when the window's shape
 * changed:
 *
 * <ul>
 *   <li>the window plus new turns at the end: only the new turns are appended;
 *   <li>the oldest turns dropped: only the pointer moves;
 *   <li>a window memory evicting while it adds, with or without a system message kept first: the
 *       pointer moves and only the new turn is appended;
 *   <li>an edited turn: that turn and the ones after it are appended again.
 * </ul>
 *
 * <p>Each turn is sent with {@code msg_id} {@code <threadId>:<log>:<position>}. {@code log}
 * ({@value #LOG} in the metadata) is minted when the thread has no window yet, so a thread
 * deleted and then reused never reuses a deleted turn's id: Loom answers such an append {@code
 * duplicate} and stores nothing. Within one log, two identical turns are two turns, and a retried
 * append answers {@code duplicate}, which counts as stored.
 *
 * <p>A read returns only the turns this class wrote under the thread's current log; other turns
 * in the thread are left alone.
 */
public final class ConversationWindow {
  /** The metadata key of the window pointer: the window holds every turn from this log position on. */
  public static final String WINDOW_START = "loom_window_start";

  /** The metadata key of the earlier positions kept at the window's front, in order. */
  public static final String WINDOW_HEAD = "loom_window_head";

  /** The metadata key of the log id that prefixes each turn's position in its {@code msg_id}. */
  public static final String LOG = "loom_log";

  /** Days of activity {@link #threadIds()} looks back over: {@code conversation.threads}' maximum. */
  public static final int THREADS_WINDOW_DAYS = 365;

  private static final ObjectMapper JSON = new ObjectMapper();
  private static final int PAGE = 1000;
  private static final int THREADS_PAGE = 200;
  private static final int THREADS_MAX_OFFSET = 100_000;

  /**
   * One message as the thread stores it: {@code role} ({@code user}, {@code assistant}, {@code
   * system} or {@code tool}), {@code content} and the {@code metadata} that carries what those
   * two cannot. Two turns are equal when all three are.
   */
  public record Turn(String role, String content, ObjectNode metadata) {
    public Turn {
      Objects.requireNonNull(role, "role");
      content = content == null ? "" : content;
      metadata = canonical(metadata);
    }

    public Turn(String role, String content) {
      this(role, content, null);
    }
  }

  private final LoomClient client;
  private final NotStored onNotStored;

  public ConversationWindow(LoomClient client, NotStored onNotStored) {
    this.client = Objects.requireNonNull(client, "client");
    this.onNotStored = Objects.requireNonNull(onNotStored, "onNotStored");
  }

  /** The turns in the thread's window, oldest first; empty for a thread that does not exist. */
  public List<Turn> read(String threadId) {
    List<Turn> turns = new ArrayList<>();
    for (Stored s : load(threadId).window) {
      turns.add(s.turn);
    }
    return List.copyOf(turns);
  }

  /** Make the thread's window equal {@code turns}, appending as little as it can. */
  public void replace(String threadId, List<Turn> turns) {
    State state = load(threadId);
    List<Turn> next = List.copyOf(turns);
    List<Turn> window = new ArrayList<>();
    for (Stored s : state.window) {
      window.add(s.turn);
    }
    if (next.isEmpty() && window.isEmpty()) {
      return; // nothing stored, nothing to store
    }
    boolean newLog = state.log == null;
    String log = newLog ? UUID.randomUUID().toString().substring(0, 8) : state.log;

    // Reuse stored turns for the longest start of the new list that the window holds in order
    // (earliest match first, which finds the longest), then append the rest. The new window's
    // positions are the reused ones followed by the appended ones.
    List<Long> positions = new ArrayList<>();
    int matched = 0;
    int from = 0;
    while (matched < next.size()) {
      int at = indexOf(window, next.get(matched), from);
      if (at < 0) {
        break;
      }
      positions.add(state.window.get(at).position);
      from = at + 1;
      matched++;
    }
    List<Turn> tail = next.subList(matched, next.size());
    long end = state.nextPos + tail.size(); // the log's next position once the tail is in
    for (long p = state.nextPos; p < end; p++) {
      positions.add(p);
    }
    append(threadId, log, state.nextPos, tail);

    // The open run: the longest end of the window that is every position up to the log's end.
    int run = positions.size();
    while (run > 0 && positions.get(run - 1) == end - (positions.size() - run) - 1) {
      run--;
    }
    long start = run < positions.size() ? positions.get(run) : end;
    List<Long> head = List.copyOf(positions.subList(0, run));
    if (newLog || start != state.windowStart || !head.equals(state.head)) {
      writePointer(threadId, state, log, start, head);
    }
  }

  private static int indexOf(List<Turn> window, Turn turn, int from) {
    for (int i = from; i < window.size(); i++) {
      if (window.get(i).equals(turn)) {
        return i;
      }
    }
    return -1;
  }

  /** Soft-delete the thread with {@code conversation.delete}, which needs the {@code admin} role. */
  public void delete(String threadId) {
    client.conversationDelete(requireId(threadId));
  }

  /**
   * The ids of this agent's threads with a live turn in the last {@value #THREADS_WINDOW_DAYS}
   * days, most recently active first.
   */
  public List<String> threadIds() {
    Set<String> ids = new LinkedHashSet<>();
    for (int offset = 0; offset <= THREADS_MAX_OFFSET; offset += THREADS_PAGE) {
      List<ConversationThread> page =
          client.conversationThreads(THREADS_WINDOW_DAYS, THREADS_PAGE, offset).threads();
      for (ConversationThread t : page) {
        ids.add(t.threadId());
      }
      if (page.size() < THREADS_PAGE) {
        break;
      }
    }
    return List.copyOf(ids);
  }

  private void append(String threadId, String log, long from, List<Turn> turns) {
    long position = from;
    for (Turn t : turns) {
      Map<String, Object> extra = new HashMap<>();
      extra.put("msg_id", threadId + ":" + log + ":" + position);
      if (!t.metadata().isEmpty()) {
        extra.put("metadata", t.metadata());
      }
      Results.ConversationAppendResult r =
          client.conversationAppend(threadId, t.role(), t.content(), extra);
      if (onNotStored == NotStored.FAIL) {
        r.requireStored();
      }
      position++;
    }
  }

  private void writePointer(String threadId, State state, String log, long start, List<Long> head) {
    ObjectNode metadata = state.otherMetadata.deepCopy();
    metadata.put(LOG, log);
    metadata.put(WINDOW_START, start);
    if (!head.isEmpty()) {
      ArrayNode kept = metadata.putArray(WINDOW_HEAD);
      head.forEach(kept::add);
    }
    Map<String, Object> extra = new HashMap<>();
    extra.put("metadata", metadata);
    // Re-creating a thread replaces its title, run and owner too, so they are written back.
    extra.put("title", state.title);
    extra.put("run_id", state.runId);
    extra.put("user_id", state.userId);
    client.conversationCreate(threadId, extra);
  }

  private State load(String threadId) {
    requireId(threadId);
    ConversationGetResult first = client.conversationGet(threadId, PAGE, 0);
    List<ConversationMessage> messages = new ArrayList<>(first.messages());
    ConversationGetResult page = first;
    for (int offset = PAGE; page.messages().size() == PAGE; offset += PAGE) {
      page = client.conversationGet(threadId, PAGE, offset);
      messages.addAll(page.messages());
    }

    State s = new State();
    s.title = first.title();
    s.runId = first.runId();
    s.userId = first.userId();
    ObjectNode meta = (ObjectNode) first.metadata().deepCopy();
    JsonNode log = meta.remove(LOG);
    JsonNode start = meta.remove(WINDOW_START);
    JsonNode head = meta.remove(WINDOW_HEAD);
    s.otherMetadata = meta;
    s.log = log != null && log.isTextual() && !log.asText().isEmpty() ? log.asText() : null;
    s.windowStart = start != null && start.canConvertToLong() ? Math.max(0, start.asLong()) : 0;
    List<Long> kept = new ArrayList<>();
    if (head != null) {
      for (JsonNode p : head) {
        if (p.canConvertToLong() && p.asLong() >= 0 && p.asLong() < s.windowStart) {
          kept.add(p.asLong());
        }
      }
    }
    s.head = List.copyOf(kept);
    if (s.log == null) {
      // A pointer write that never landed: the log of the newest turn this class wrote.
      for (ConversationMessage m : messages) {
        String[] id = parse(threadId, m.msgId());
        if (id != null) {
          s.log = id[0];
        }
      }
    }

    long maxPosition = -1;
    Map<Long, Turn> byPosition = new HashMap<>();
    if (s.log != null) {
      for (ConversationMessage m : messages) {
        String[] id = parse(threadId, m.msgId());
        if (id == null || !id[0].equals(s.log)) {
          continue;
        }
        long position = Long.parseLong(id[1]);
        maxPosition = Math.max(maxPosition, position);
        byPosition.putIfAbsent(position, new Turn(m.role(), m.content(), (ObjectNode) m.metadata()));
      }
    }
    // The kept head positions in their order, then every live position from the start on.
    List<Stored> window = new ArrayList<>();
    for (long p : s.head) {
      Turn t = byPosition.get(p);
      if (t != null) {
        window.add(new Stored(p, t));
      }
    }
    List<Stored> run = new ArrayList<>();
    byPosition.forEach(
        (p, t) -> {
          if (p >= s.windowStart) {
            run.add(new Stored(p, t));
          }
        });
    run.sort(Comparator.comparingLong(st -> st.position));
    window.addAll(run);
    s.window = window;
    s.nextPos = Math.max(maxPosition + 1, s.windowStart);
    return s;
  }

  /** {@code [log, position]} of a {@code msg_id} this class wrote in the thread, else null. */
  private static String[] parse(String threadId, String msgId) {
    String prefix = threadId + ":";
    if (!msgId.startsWith(prefix)) {
      return null;
    }
    String rest = msgId.substring(prefix.length());
    int colon = rest.lastIndexOf(':');
    if (colon <= 0 || colon == rest.length() - 1) {
      return null;
    }
    String log = rest.substring(0, colon);
    String position = rest.substring(colon + 1);
    for (int i = 0; i < position.length(); i++) {
      if (!Character.isDigit(position.charAt(i))) {
        return null;
      }
    }
    if (position.length() > 18 || log.indexOf(':') >= 0) {
      return null;
    }
    return new String[] {log, position};
  }

  private static String requireId(String threadId) {
    if (threadId == null || threadId.isBlank()) {
      throw new IllegalArgumentException("the conversation id is required");
    }
    return threadId;
  }

  /** The metadata as the server reads it back, so a stored turn equals the one that was sent. */
  private static ObjectNode canonical(ObjectNode metadata) {
    if (metadata == null || metadata.isEmpty()) {
      return JSON.createObjectNode();
    }
    try {
      return (ObjectNode) JSON.readTree(JSON.writeValueAsString(metadata));
    } catch (JsonProcessingException e) {
      throw new IllegalArgumentException("metadata is not serializable as JSON", e);
    }
  }

  private record Stored(long position, Turn turn) {}

  private static final class State {
    String title;
    String runId;
    String userId;
    ObjectNode otherMetadata;
    String log;
    long windowStart;
    List<Long> head;
    long nextPos;
    List<Stored> window;
  }
}
