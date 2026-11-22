package com.clickhouse.loom.langchain4j;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.clickhouse.loom.ConversationWindow;
import com.clickhouse.loom.FakeLoom;
import com.clickhouse.loom.LoomException;
import com.clickhouse.loom.NotStored;
import com.fasterxml.jackson.databind.JsonNode;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.CustomMessage;
import dev.langchain4j.data.message.SystemMessage;
import dev.langchain4j.data.message.ToolExecutionResultMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.memory.ChatMemory;
import dev.langchain4j.memory.chat.MessageWindowChatMemory;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** Every store method against a fake Loom, and a window memory evicting through it. */
class LoomChatMemoryStoreTest {
  private FakeLoom loom;
  private LoomChatMemoryStore store;

  @BeforeEach
  void start() {
    loom = new FakeLoom();
    store = LoomChatMemoryStore.builder().client(loom.client()).build();
  }

  @AfterEach
  void stop() {
    loom.close();
  }

  private static final ToolExecutionRequest LOOKUP =
      ToolExecutionRequest.builder().id("call-1").name("order_status").arguments("{\"id\":1234}").build();

  @Test
  void anUnknownMemoryIsEmpty() {
    assertEquals(List.of(), store.getMessages("nobody"));
  }

  @Test
  void everyMessageTypeRoundTrips() {
    List<ChatMessage> messages =
        List.of(
            SystemMessage.from("You answer order questions."),
            UserMessage.from("Where is order 1234?"),
            UserMessage.from("ana", "And order 99?"),
            AiMessage.from(LOOKUP),
            ToolExecutionResultMessage.from(LOOKUP, "shipped"),
            AiMessage.from("Order 1234 shipped today."),
            CustomMessage.from(Map.of("kind", "handoff", "to", "billing")));
    store.updateMessages("order-1234", messages);
    assertEquals(messages, store.getMessages("order-1234"));

    List<FakeLoom.Message> log = loom.live("order-1234");
    assertEquals(
        List.of("system", "user", "user", "assistant", "tool", "assistant", "system"),
        log.stream().map(m -> m.role).toList());
    assertEquals("Where is order 1234?", log.get(1).content);
    assertEquals("shipped", log.get(4).content);
  }

  @Test
  void onlyWhatRoleAndTextCannotCarryGoesToMetadata() {
    store.updateMessages(
        "m", List.of(UserMessage.from("plain"), UserMessage.from("ana", "named"), AiMessage.from(LOOKUP)));
    List<FakeLoom.Message> log = loom.live("m");
    assertTrue(log.get(0).metadata.isEmpty());
    JsonNode named = log.get(1).metadata.get(LoomChatMemoryStore.MESSAGE_KEY);
    assertEquals("ana", named.get("name").asText());
    JsonNode tools = log.get(2).metadata.get(LoomChatMemoryStore.MESSAGE_KEY);
    assertEquals("order_status", tools.get("toolExecutionRequests").get(0).get("name").asText());
  }

  @Test
  void aNewTurnAppendsOnlyItself() {
    store.updateMessages("m", List.of(UserMessage.from("hi"), AiMessage.from(LOOKUP)));
    loom.clearCalls();
    List<ChatMessage> next = store.getMessages("m");
    next = new java.util.ArrayList<>(next);
    next.add(ToolExecutionResultMessage.from(LOOKUP, "shipped"));
    store.updateMessages("m", next);
    assertEquals(1, loom.calls("conversation.append").size());
    assertEquals("tool", loom.calls("conversation.append").get(0).body().get("role").asText());
  }

  @Test
  void aWindowMemoryEvictsByMovingThePointer() {
    ChatMemory memory =
        MessageWindowChatMemory.builder().id("w").maxMessages(3).chatMemoryStore(store).build();
    memory.add(UserMessage.from("one"));
    memory.add(AiMessage.from("two"));
    memory.add(UserMessage.from("three"));
    memory.add(AiMessage.from("four"));
    memory.add(UserMessage.from("five"));

    List<ChatMessage> expected =
        List.of(UserMessage.from("three"), AiMessage.from("four"), UserMessage.from("five"));
    assertEquals(expected, memory.messages());
    assertEquals(expected, store.getMessages("w"));
    assertEquals(5, loom.live("w").size()); // evicted turns stay in the log
    assertEquals(5, loom.calls("conversation.append").size()); // each turn sent once
    assertEquals(List.of(), loom.calls("conversation.delete"));
    assertEquals(2, loom.threadMetadata("w").get(ConversationWindow.WINDOW_START).asLong());
  }

  @Test
  void aWindowMemoryKeepsTheSystemMessageWithoutResendingIt() {
    ChatMemory memory =
        MessageWindowChatMemory.builder().id("k").maxMessages(3).chatMemoryStore(store).build();
    memory.add(SystemMessage.from("You answer order questions."));
    for (String text : List.of("one", "two", "three", "four")) {
      memory.add(UserMessage.from(text));
    }
    assertEquals(memory.messages(), store.getMessages("k"));
    assertEquals(SystemMessage.from("You answer order questions."), memory.messages().get(0));
    assertEquals(5, loom.calls("conversation.append").size()); // each message sent once
    assertEquals(5, loom.live("k").size());
  }

  @Test
  void anEditedMessageIsAppendedAgainWithTheTurnsAfterIt() {
    store.updateMessages(
        "e", List.of(SystemMessage.from("Be brief."), UserMessage.from("hi"), AiMessage.from("hello")));
    loom.clearCalls();
    List<ChatMessage> edited =
        List.of(SystemMessage.from("Be brief."), UserMessage.from("hi there"), AiMessage.from("hello"));
    store.updateMessages("e", edited);

    assertEquals(edited, store.getMessages("e"));
    assertEquals(
        List.of("hi there", "hello"),
        loom.calls("conversation.append").stream().map(c -> c.body().get("content").asText()).toList());
    assertEquals(5, loom.live("e").size()); // the original turns stay in the log
    assertEquals(
        "[0]", loom.threadMetadata("e").get(ConversationWindow.WINDOW_HEAD).toString());
  }

  @Test
  void aNewSystemMessageMovesThePointerAndAppendsOnlyItself() {
    ChatMemory memory =
        MessageWindowChatMemory.builder().id("s").maxMessages(10).chatMemoryStore(store).build();
    memory.add(SystemMessage.from("Be brief."));
    memory.add(UserMessage.from("hi"));
    memory.add(SystemMessage.from("Be thorough."));

    // The memory drops the old system message and adds the new one: the pointer moves past
    // "Be brief." and only "Be thorough." is appended.
    assertEquals(memory.messages(), store.getMessages("s"));
    assertTrue(memory.messages().contains(SystemMessage.from("Be thorough.")));
    assertFalse(memory.messages().contains(SystemMessage.from("Be brief.")));
    assertEquals(3, loom.calls("conversation.append").size());
    assertEquals(3, loom.live("s").size()); // nothing deleted
  }

  @Test
  void loggingDisabledThrowsUnlessIgnored() {
    loom.logging = false;
    LoomException e =
        assertThrows(
            LoomException.class, () -> store.updateMessages("m", List.of(UserMessage.from("hi"))));
    assertEquals("LOGGING_DISABLED", e.code());
    assertTrue(e.getMessage().contains("conversation.set_logging"));

    LoomChatMemoryStore bestEffort =
        LoomChatMemoryStore.builder().client(loom.client()).onNotStored(NotStored.IGNORE).build();
    bestEffort.updateMessages("m", List.of(UserMessage.from("hi")));
    assertEquals(List.of(), bestEffort.getMessages("m"));
  }

  @Test
  void aDuplicateAnswerCountsAsStored() {
    loom.nextAppendReason = "duplicate";
    store.updateMessages("m", List.of(UserMessage.from("retried")));
  }

  @Test
  void deleteRemovesTheMemoryAndTheIdCanBeReused() {
    store.updateMessages("m", List.of(UserMessage.from("old")));
    store.deleteMessages("m");
    JsonNode body = loom.calls("conversation.delete").get(0).body();
    assertEquals("m", body.get("confirm_thread_id").asText());
    assertEquals(List.of(), store.getMessages("m"));

    store.updateMessages("m", List.of(UserMessage.from("new")));
    assertEquals(List.of(UserMessage.from("new")), store.getMessages("m"));
  }

  @Test
  void deleteWithoutAdminThrows() {
    loom.admin = false;
    LoomException e = assertThrows(LoomException.class, () -> store.deleteMessages("m"));
    assertEquals(403, e.status());
  }

  @Test
  void aNonStringMemoryIdIsItsText() {
    store.updateMessages(42, List.of(UserMessage.from("hi")));
    assertFalse(loom.live("42").isEmpty());
    assertEquals(List.of(UserMessage.from("hi")), store.getMessages(42));
  }

  @Test
  void theClientIsRequired() {
    assertThrows(NullPointerException.class, () -> LoomChatMemoryStore.builder().build());
  }
}
