package com.clickhouse.loom.springai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.clickhouse.loom.ConversationWindow;
import com.clickhouse.loom.FakeLoom;
import com.clickhouse.loom.LoomException;
import com.clickhouse.loom.NotStored;
import com.fasterxml.jackson.databind.JsonNode;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.memory.ChatMemory;
import org.springframework.ai.chat.memory.MessageWindowChatMemory;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.ToolResponseMessage;
import org.springframework.ai.chat.messages.UserMessage;

/** Every repository method against a fake Loom, and a window memory evicting through it. */
class LoomChatMemoryRepositoryTest {
  private FakeLoom loom;
  private LoomChatMemoryRepository repo;

  @BeforeEach
  void start() {
    loom = new FakeLoom();
    repo = LoomChatMemoryRepository.builder().client(loom.client()).build();
  }

  @AfterEach
  void stop() {
    loom.close();
  }

  private static final AssistantMessage.ToolCall LOOKUP =
      new AssistantMessage.ToolCall("call-1", "function", "order_status", "{\"id\":1234}");

  @Test
  void anUnknownConversationIsEmpty() {
    assertEquals(List.of(), repo.findByConversationId("nobody"));
  }

  @Test
  void everyMessageTypeRoundTrips() {
    List<Message> messages =
        List.of(
            new SystemMessage("You answer order questions."),
            UserMessage.builder().text("Where is order 1234?").metadata(Map.of("channel", "web")).build(),
            AssistantMessage.builder().content(null).toolCalls(List.of(LOOKUP)).build(),
            ToolResponseMessage.builder()
                .responses(List.of(new ToolResponseMessage.ToolResponse("call-1", "order_status", "shipped")))
                .build(),
            new AssistantMessage("Order 1234 shipped today."));
    repo.saveAll("order-1234", messages);
    assertEquals(messages, repo.findByConversationId("order-1234"));

    List<FakeLoom.Message> log = loom.live("order-1234");
    assertEquals(
        List.of("system", "user", "assistant", "tool", "assistant"),
        log.stream().map(m -> m.role).toList());
    assertEquals("shipped", log.get(3).content);
    assertTrue(log.get(0).metadata.isEmpty()); // role and text carry a plain message
    JsonNode user = log.get(1).metadata.get(LoomChatMemoryRepository.METADATA_KEY);
    assertEquals("web", user.get("metadata").get("channel").asText());
    JsonNode call = log.get(2).metadata.get(LoomChatMemoryRepository.METADATA_KEY);
    assertEquals("order_status", call.get("tool_calls").get(0).get("name").asText());
  }

  @Test
  void aWindowMemoryEvictsByMovingThePointer() {
    ChatMemory memory =
        MessageWindowChatMemory.builder().chatMemoryRepository(repo).maxMessages(3).build();
    for (String text : List.of("one", "two", "three", "four", "five")) {
      memory.add("w", new UserMessage(text));
    }
    List<Message> expected =
        List.of(new UserMessage("three"), new UserMessage("four"), new UserMessage("five"));
    assertEquals(expected, memory.get("w"));
    assertEquals(expected, repo.findByConversationId("w"));
    assertEquals(5, loom.live("w").size()); // evicted turns stay in the log
    assertEquals(5, loom.calls("conversation.append").size()); // each turn sent once
    assertEquals(List.of(), loom.calls("conversation.delete"));
    assertEquals(2, loom.threadMetadata("w").get(ConversationWindow.WINDOW_START).asLong());
  }

  @Test
  void aWindowMemoryKeepsTheSystemMessageWithoutResendingIt() {
    ChatMemory memory =
        MessageWindowChatMemory.builder().chatMemoryRepository(repo).maxMessages(3).build();
    memory.add("k", new SystemMessage("You answer order questions."));
    for (String text : List.of("one", "two", "three", "four")) {
      memory.add("k", new UserMessage(text));
    }
    assertEquals(memory.get("k"), repo.findByConversationId("k"));
    assertEquals(new SystemMessage("You answer order questions."), memory.get("k").get(0));
    assertEquals(5, loom.calls("conversation.append").size()); // each message sent once
    assertEquals(5, loom.live("k").size());
  }

  @Test
  void aNewSystemMessageReplacesTheOldWithoutDeleting() {
    ChatMemory memory =
        MessageWindowChatMemory.builder().chatMemoryRepository(repo).maxMessages(10).build();
    memory.add("s", new SystemMessage("Be brief."));
    memory.add("s", new UserMessage("hi"));
    memory.add("s", new SystemMessage("Be thorough."));
    assertEquals(memory.get("s"), repo.findByConversationId("s"));
    assertTrue(memory.get("s").contains(new SystemMessage("Be thorough.")));
    assertTrue(!memory.get("s").contains(new SystemMessage("Be brief.")));
    assertEquals(List.of(), loom.calls("conversation.delete"));
  }

  @Test
  void anEditedMessageIsAppendedAgainWithTheTurnsAfterIt() {
    repo.saveAll(
        "e", List.of(new SystemMessage("Be brief."), new UserMessage("hi"), new AssistantMessage("hello")));
    loom.clearCalls();
    List<Message> edited =
        List.of(new SystemMessage("Be brief."), new UserMessage("hi there"), new AssistantMessage("hello"));
    repo.saveAll("e", edited);

    assertEquals(edited, repo.findByConversationId("e"));
    assertEquals(
        List.of("hi there", "hello"),
        loom.calls("conversation.append").stream().map(c -> c.body().get("content").asText()).toList());
    assertEquals(5, loom.live("e").size()); // the original turns stay in the log
    assertEquals(
        "[0]", loom.threadMetadata("e").get(ConversationWindow.WINDOW_HEAD).toString());
  }

  @Test
  void findConversationIdsListsThisAgentsThreads() {
    repo.saveAll("a", List.of(new UserMessage("x")));
    repo.saveAll("b", List.of(new UserMessage("y")));
    assertEquals(List.of("b", "a"), repo.findConversationIds());
    assertEquals(365, loom.calls("conversation.threads").get(0).body().get("window_days").asInt());
  }

  @Test
  void loggingDisabledThrowsUnlessIgnored() {
    loom.logging = false;
    LoomException e =
        assertThrows(LoomException.class, () -> repo.saveAll("c", List.of(new UserMessage("hi"))));
    assertEquals("LOGGING_DISABLED", e.code());
    assertTrue(e.getMessage().contains("conversation.set_logging"));

    LoomChatMemoryRepository bestEffort =
        LoomChatMemoryRepository.builder().client(loom.client()).onNotStored(NotStored.IGNORE).build();
    bestEffort.saveAll("c", List.of(new UserMessage("hi")));
    assertEquals(List.of(), bestEffort.findByConversationId("c"));
  }

  @Test
  void aDuplicateAnswerCountsAsStored() {
    loom.nextAppendReason = "duplicate";
    repo.saveAll("c", List.of(new UserMessage("retried")));
  }

  @Test
  void deleteRemovesTheConversationAndTheIdCanBeReused() {
    repo.saveAll("c", List.of(new UserMessage("old")));
    repo.deleteByConversationId("c");
    JsonNode body = loom.calls("conversation.delete").get(0).body();
    assertEquals("c", body.get("confirm_thread_id").asText());
    assertEquals(List.of(), repo.findByConversationId("c"));
    assertEquals(List.of(), repo.findConversationIds());

    repo.saveAll("c", List.of(new UserMessage("new")));
    assertEquals(List.of(new UserMessage("new")), repo.findByConversationId("c"));
  }

  @Test
  void clearThroughTheMemoryNeedsAdmin() {
    loom.admin = false;
    ChatMemory memory = MessageWindowChatMemory.builder().chatMemoryRepository(repo).build();
    LoomException e = assertThrows(LoomException.class, () -> memory.clear("c"));
    assertEquals(403, e.status());
  }

  @Test
  void theClientIsRequired() {
    assertThrows(NullPointerException.class, () -> LoomChatMemoryRepository.builder().build());
  }
}
