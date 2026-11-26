package com.clickhouse.loom;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.clickhouse.loom.ConversationWindow.Turn;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** The window pointer, the three replace cases and the not-stored answers, against a fake Loom. */
class ConversationWindowTest {
  private static final ObjectMapper JSON = new ObjectMapper();
  private static final Turn S1 = new Turn("system", "Be brief.");
  private static final Turn S2 = new Turn("system", "Be thorough.");
  private static final Turn U1 = new Turn("user", "Where is my order?");
  private static final Turn A1 = new Turn("assistant", "It ships today.");
  private static final Turn U2 = new Turn("user", "Thanks.");

  private FakeLoom loom;
  private ConversationWindow window;

  @BeforeEach
  void start() {
    loom = new FakeLoom();
    window = new ConversationWindow(loom.client(), NotStored.FAIL);
  }

  @AfterEach
  void stop() {
    loom.close();
  }

  private List<String> appendedIds() {
    return loom.calls("conversation.append").stream().map(c -> c.body().get("msg_id").asText()).toList();
  }

  private long pointer(String thread) {
    return loom.threadMetadata(thread).get(ConversationWindow.WINDOW_START).asLong();
  }

  @Test
  void anUnknownThreadReadsEmpty() {
    assertEquals(List.of(), window.read("nobody"));
  }

  @Test
  void newTurnsAtTheEndAppendOnlyTheTail() {
    window.replace("t", List.of(U1));
    loom.clearCalls();
    window.replace("t", List.of(U1, A1));

    assertEquals(1, loom.calls("conversation.append").size());
    String log = loom.threadMetadata("t").get(ConversationWindow.LOG).asText();
    assertEquals(List.of("t:" + log + ":1"), appendedIds());
    assertEquals("assistant", loom.calls("conversation.append").get(0).body().get("role").asText());
    assertEquals(List.of(), loom.calls("conversation.create"));
    assertEquals(List.of(U1, A1), window.read("t"));
  }

  @Test
  void aFirstWriteMintsALogAndPointsAtItsStart() {
    window.replace("t", List.of(U1, A1));
    JsonNode meta = loom.threadMetadata("t");
    String log = meta.get(ConversationWindow.LOG).asText();
    assertEquals(8, log.length());
    assertEquals(0, meta.get(ConversationWindow.WINDOW_START).asLong());
    assertEquals(List.of("t:" + log + ":0", "t:" + log + ":1"), appendedIds());
  }

  @Test
  void droppingTheOldestTurnsMovesThePointerAndDeletesNothing() {
    window.replace("t", List.of(U1, A1, U2));
    loom.clearCalls();
    window.replace("t", List.of(A1, U2));

    assertEquals(List.of(), loom.calls("conversation.append"));
    assertEquals(List.of(), loom.calls("conversation.delete"));
    assertEquals(1, pointer("t"));
    assertEquals(List.of(A1, U2), window.read("t"));
    assertEquals(3, loom.live("t").size()); // the log keeps every turn
  }

  @Test
  void anEditedFirstTurnAppendsTheWholeListAndRepoints() {
    window.replace("t", List.of(S1, U1));
    loom.clearCalls();
    window.replace("t", List.of(S2, U1));

    String log = loom.threadMetadata("t").get(ConversationWindow.LOG).asText();
    assertEquals(List.of("t:" + log + ":2", "t:" + log + ":3"), appendedIds());
    assertEquals(2, pointer("t"));
    assertEquals(List.of(S2, U1), window.read("t"));
    assertEquals(4, loom.live("t").size());
  }

  @Test
  void evictingAndAddingInOneUpdateMovesThePointerAndAppendsOnlyTheNewTurn() {
    window.replace("t", List.of(U1, A1, U2));
    loom.clearCalls();
    Turn a2 = new Turn("assistant", "You're welcome.");
    window.replace("t", List.of(A1, U2, a2));

    String log = loom.threadMetadata("t").get(ConversationWindow.LOG).asText();
    assertEquals(List.of("t:" + log + ":3"), appendedIds());
    assertEquals(1, pointer("t"));
    assertEquals(List.of(A1, U2, a2), window.read("t"));
  }

  @Test
  void evictingBehindAKeptSystemMessageListsItInTheHead() {
    Turn a2 = new Turn("assistant", "You're welcome.");
    Turn u3 = new Turn("user", "Bye.");
    window.replace("t", List.of(S1, U1, A1, U2));
    loom.clearCalls();
    window.replace("t", List.of(S1, A1, U2, a2)); // U1 evicted, the system message kept first

    String log = loom.threadMetadata("t").get(ConversationWindow.LOG).asText();
    assertEquals(List.of("t:" + log + ":4"), appendedIds());
    assertEquals(2, pointer("t"));
    assertEquals("[0]", loom.threadMetadata("t").get(ConversationWindow.WINDOW_HEAD).toString());
    assertEquals(List.of(S1, A1, U2, a2), window.read("t"));

    loom.clearCalls();
    window.replace("t", List.of(S1, U2, a2, u3)); // again: one append, the head stays
    assertEquals(1, appendedIds().size());
    assertEquals(3, pointer("t"));
    assertEquals(List.of(S1, U2, a2, u3), window.read("t"));
    assertEquals(6, loom.live("t").size());
  }

  @Test
  void aPlainAppendWritesNoMetadata() {
    window.replace("t", List.of(S1, U1));
    loom.clearCalls();
    window.replace("t", List.of(S1, U1, A1));
    window.replace("t", List.of(S1, U1, A1, U2));
    assertEquals(List.of(), loom.calls("conversation.create"));
    assertEquals(null, loom.threadMetadata("t").get(ConversationWindow.WINDOW_HEAD));
  }

  @Test
  void aReorderKeepsWhatStillLeadsTheNewList() {
    window.replace("t", List.of(U1, A1));
    loom.clearCalls();
    window.replace("t", List.of(A1, U1));
    assertEquals(1, appendedIds().size()); // only U1 again; A1 stays where it is
    assertEquals(1, pointer("t"));
    assertEquals(List.of(A1, U1), window.read("t"));
  }

  @Test
  void identicalTurnsAreTwoTurns() {
    Turn ok = new Turn("user", "ok");
    window.replace("t", List.of(ok, ok));
    assertEquals(2, appendedIds().stream().distinct().count());
    assertEquals(List.of(ok, ok), window.read("t"));
  }

  @Test
  void clearingMovesThePointerPastTheLogAndTheNextTurnFollowsIt() {
    window.replace("t", List.of(U1, A1));
    window.replace("t", List.of());
    assertEquals(List.of(), window.read("t"));
    assertEquals(2, pointer("t"));

    window.replace("t", List.of(U2));
    String log = loom.threadMetadata("t").get(ConversationWindow.LOG).asText();
    assertEquals("t:" + log + ":2", appendedIds().get(appendedIds().size() - 1));
    assertEquals(List.of(U2), window.read("t"));
  }

  @Test
  void anEmptyListOnANewThreadWritesNothing() {
    window.replace("t", List.of());
    assertEquals(List.of(), loom.calls("conversation.append"));
    assertEquals(List.of(), loom.calls("conversation.create"));
  }

  @Test
  void metadataRoundTripsAndTakesPartInTheComparison() {
    ObjectNode meta = JSON.createObjectNode();
    meta.putArray("tool_calls").addObject().put("name", "lookup").put("arguments", "{\"id\":7}");
    meta.put("ratio", 0.5);
    meta.put("big", 1L << 40);
    Turn withTools = new Turn("assistant", "", meta);
    window.replace("t", List.of(U1, withTools));
    assertEquals(List.of(U1, withTools), window.read("t"));

    loom.clearCalls();
    window.replace("t", List.of(U1, withTools, U2)); // still a tail append
    assertEquals(1, loom.calls("conversation.append").size());
    assertTrue(loom.calls("conversation.append").get(0).body().path("metadata").isMissingNode());
  }

  @Test
  void loggingDisabledThrowsNamingTheSwitch() {
    loom.logging = false;
    LoomException e = assertThrows(LoomException.class, () -> window.replace("t", List.of(U1)));
    assertEquals("LOGGING_DISABLED", e.code());
    assertEquals("configuration", e.category());
    assertTrue(e.getMessage().contains("conversation.set_logging"), e.getMessage());
    assertEquals(List.of(), loom.calls("conversation.create")); // no pointer for nothing stored
  }

  @Test
  void loggingDisabledIsIgnoredWhenAskedTo() {
    loom.logging = false;
    ConversationWindow bestEffort = new ConversationWindow(loom.client(), NotStored.IGNORE);
    bestEffort.replace("t", List.of(U1));
    assertEquals(List.of(), bestEffort.read("t"));
  }

  @Test
  void aTurnDeletedWhileStoringThrows() {
    loom.nextAppendReason = "deleted";
    LoomException e = assertThrows(LoomException.class, () -> window.replace("t", List.of(U1)));
    assertEquals("DELETED", e.code());
  }

  @Test
  void aDuplicateCountsAsStored() {
    loom.nextAppendReason = "duplicate";
    window.replace("t", List.of(U1, A1)); // no exception
    assertEquals(2, loom.calls("conversation.append").size());
  }

  @Test
  void deleteSendsTheConfirmationAndAReusedIdStartsAFreshLog() {
    window.replace("t", List.of(U1, A1));
    String before = loom.threadMetadata("t").get(ConversationWindow.LOG).asText();
    window.delete("t");
    JsonNode del = loom.calls("conversation.delete").get(0).body();
    assertEquals("t", del.get("thread_id").asText());
    assertEquals("t", del.get("confirm_thread_id").asText());
    assertEquals(List.of(), window.read("t"));

    // The deleted turns' ids still dedupe; a fresh log keeps the new turn from colliding.
    window.replace("t", List.of(U2));
    assertNotEquals(before, loom.threadMetadata("t").get(ConversationWindow.LOG).asText());
    assertEquals(List.of(U2), window.read("t"));
  }

  @Test
  void deleteWithoutAdminIsTheServersRefusal() {
    loom.admin = false;
    LoomException e = assertThrows(LoomException.class, () -> window.delete("t"));
    assertEquals(403, e.status());
    assertEquals("auth", e.category());
  }

  @Test
  void thePointerWriteKeepsTheThreadsTitleAndOtherMetadata() {
    ObjectNode meta = JSON.createObjectNode().put("tenant", "acme");
    loom.seedThread("t", "Support chat", meta);
    window.replace("t", List.of(U1));
    JsonNode create = loom.calls("conversation.create").get(0).body();
    assertEquals("Support chat", create.get("title").asText());
    assertEquals("acme", create.get("metadata").get("tenant").asText());
    assertEquals("-", create.get("run_id").asText());
  }

  @Test
  void otherWritersTurnsAreNotRead() {
    loom.seed("t", "someone-else", "user", "hi");
    window.replace("t", List.of(U1));
    assertEquals(List.of(U1), window.read("t"));
  }

  @Test
  void aLostPointerWriteIsRecoveredFromTheTurns() {
    loom.seed("t", "t:abcd1234:0", "user", "Where is my order?");
    loom.seed("t", "t:abcd1234:1", "assistant", "It ships today.");
    assertEquals(List.of(U1, A1), window.read("t"));
    window.replace("t", List.of(U1, A1, U2));
    assertEquals(List.of("t:abcd1234:2"), appendedIds());
  }

  @Test
  void aLongLogIsReadInPages() {
    List<Turn> expected = new ArrayList<>();
    for (int i = 0; i < 1001; i++) {
      loom.seed("t", "t:abcd1234:" + i, "user", "turn " + i);
      expected.add(new Turn("user", "turn " + i));
    }
    assertEquals(expected, window.read("t"));
    List<FakeLoom.Call> gets = loom.calls("conversation.get");
    assertEquals(2, gets.size());
    assertEquals(1000, gets.get(1).body().get("offset").asInt());
  }

  @Test
  void threadIdsPagesThroughThreads() {
    for (int i = 0; i < 201; i++) {
      loom.seed("c" + i, "m", "user", "x");
    }
    List<String> ids = window.threadIds();
    assertEquals(201, ids.size());
    assertEquals("c200", ids.get(0)); // most recently active first
    List<FakeLoom.Call> pages = loom.calls("conversation.threads");
    assertEquals(2, pages.size());
    assertEquals(365, pages.get(0).body().get("window_days").asInt());
    assertEquals(200, pages.get(1).body().get("offset").asInt());
  }

  @Test
  void aBlankConversationIdIsRefused() {
    assertThrows(IllegalArgumentException.class, () -> window.read(" "));
  }
}
