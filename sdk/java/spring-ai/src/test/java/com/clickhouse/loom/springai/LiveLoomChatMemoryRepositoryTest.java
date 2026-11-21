package com.clickhouse.loom.springai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.clickhouse.loom.ConversationWindow;
import com.clickhouse.loom.LoomClient;
import com.clickhouse.loom.LoomException;
import com.clickhouse.loom.Results.ConversationGetResult;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.ai.chat.memory.ChatMemory;
import org.springframework.ai.chat.memory.MessageWindowChatMemory;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.ToolResponseMessage;
import org.springframework.ai.chat.messages.UserMessage;

/**
 * Against a running Loom: {@code LOOM_LIVE_BASE_URL=http://localhost:7777 mvn verify}. The token
 * ({@code LOOM_API_KEY}, none on a single-user server) needs the admin role: the test turns
 * recording on with {@code conversation.set_logging} and deletes its thread.
 */
@EnabledIfEnvironmentVariable(named = "LOOM_LIVE_BASE_URL", matches = ".+")
class LiveLoomChatMemoryRepositoryTest {
  private static void say(String format, Object... args) {
    System.out.printf("[spring-ai] " + format + "%n", args);
  }

  private static String show(List<Message> messages) {
    return messages.stream()
        .map(m -> m.getMessageType() + ":" + LoomChatMemoryRepository.toTurn(m).content())
        .toList()
        .toString();
  }

  @Test
  void storeEvictReloadDelete() {
    LoomClient loom =
        LoomClient.builder()
            .baseUrl(System.getenv("LOOM_LIVE_BASE_URL"))
            .org(System.getenv().getOrDefault("LOOM_LIVE_ORG", "dev"))
            .namespace(System.getenv().getOrDefault("LOOM_LIVE_NAMESPACE", "local"))
            .agent("java-spring-ai-live")
            .build();
    String id = "spring-live-" + UUID.randomUUID().toString().substring(0, 8);
    say("conversation.set_logging enabled=true -> %s", loom.conversationSetLogging(true));
    say("conversation id %s, MessageWindowChatMemory maxMessages=4", id);

    LoomChatMemoryRepository repo = LoomChatMemoryRepository.builder().client(loom).build();
    ChatMemory memory =
        MessageWindowChatMemory.builder().chatMemoryRepository(repo).maxMessages(4).build();
    AssistantMessage.ToolCall lookup =
        new AssistantMessage.ToolCall("call-1", "function", "order_status", "{\"id\":1234}");
    List<Message> turns =
        List.of(
            new SystemMessage("You answer order questions."),
            new UserMessage("Where is order 1234?"),
            AssistantMessage.builder().content(null).toolCalls(List.of(lookup)).build(),
            ToolResponseMessage.builder()
                .responses(List.of(new ToolResponseMessage.ToolResponse("call-1", "order_status", "shipped")))
                .build(),
            new AssistantMessage("Order 1234 shipped today."),
            new UserMessage("ok"),
            new AssistantMessage("ok"));
    for (Message m : turns) {
      memory.add(id, m);
      ConversationGetResult thread = loom.conversationGet(id);
      say(
          "add %-10s -> window %d, log %d, %s=%s %s=%s",
          m.getMessageType(),
          memory.get(id).size(),
          thread.msgCount(),
          ConversationWindow.WINDOW_START,
          thread.metadata().path(ConversationWindow.WINDOW_START),
          ConversationWindow.WINDOW_HEAD,
          thread.metadata().path(ConversationWindow.WINDOW_HEAD));
    }
    List<Message> window = repo.findByConversationId(id);
    say("window: %s", show(window));
    assertEquals(memory.get(id), window);
    assertEquals(4, window.size());
    assertEquals(turns.size(), loom.conversationGet(id).msgCount()); // evicted turns stay in the log

    LoomChatMemoryRepository other = LoomChatMemoryRepository.builder().client(loom).build();
    say("reloaded in a new repository: %s", show(other.findByConversationId(id)));
    assertEquals(window, other.findByConversationId(id));
    List<String> ids = other.findConversationIds();
    say("findConversationIds() holds %s: %s", id, ids.contains(id));
    assertTrue(ids.contains(id));

    loom.conversationSetLogging(false);
    LoomException off =
        assertThrows(LoomException.class, () -> memory.add(id, new UserMessage("lost?")));
    say("recording off -> %s: %s", off.code(), off.getMessage());
    assertEquals("LOGGING_DISABLED", off.code());
    say("conversation.set_logging enabled=true -> %s", loom.conversationSetLogging(true));

    memory.clear(id);
    say("clear() -> conversation.delete; found=%s, window %s", loom.conversationGet(id).found(),
        show(repo.findByConversationId(id)));
    assertEquals(List.of(), repo.findByConversationId(id));
    assertFalse(repo.findConversationIds().contains(id));

    memory.add(id, new UserMessage("Where is order 1234?"));
    say("same id after delete: %s (log %s)", show(repo.findByConversationId(id)),
        loom.conversationGet(id).metadata().path(ConversationWindow.LOG));
    assertEquals(List.of(new UserMessage("Where is order 1234?")), repo.findByConversationId(id));
    repo.deleteByConversationId(id);
    say("done");
  }
}
