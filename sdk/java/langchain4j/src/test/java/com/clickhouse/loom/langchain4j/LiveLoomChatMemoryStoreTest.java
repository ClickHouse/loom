package com.clickhouse.loom.langchain4j;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.clickhouse.loom.ConversationWindow;
import com.clickhouse.loom.LoomClient;
import com.clickhouse.loom.LoomException;
import com.clickhouse.loom.Results.ConversationGetResult;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.SystemMessage;
import dev.langchain4j.data.message.ToolExecutionResultMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.memory.ChatMemory;
import dev.langchain4j.memory.chat.MessageWindowChatMemory;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Against a running Loom: {@code LOOM_LIVE_BASE_URL=http://localhost:7777 mvn verify}. The token
 * ({@code LOOM_API_KEY}, none on a single-user server) needs the admin role: the test turns
 * recording on with {@code conversation.set_logging} and deletes its thread.
 */
@EnabledIfEnvironmentVariable(named = "LOOM_LIVE_BASE_URL", matches = ".+")
class LiveLoomChatMemoryStoreTest {
  private static void say(String format, Object... args) {
    System.out.printf("[langchain4j] " + format + "%n", args);
  }

  private static String show(List<ChatMessage> messages) {
    return messages.stream()
        .map(m -> m.type() + ":" + LoomChatMemoryStore.toTurn(m).content())
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
            .agent("java-langchain4j-live")
            .build();
    String id = "lc4j-live-" + UUID.randomUUID().toString().substring(0, 8);
    say("conversation.set_logging enabled=true -> %s", loom.conversationSetLogging(true));
    say("memory id %s, MessageWindowChatMemory maxMessages=4", id);

    LoomChatMemoryStore store = LoomChatMemoryStore.builder().client(loom).build();
    ChatMemory memory =
        MessageWindowChatMemory.builder().id(id).maxMessages(4).chatMemoryStore(store).build();
    ToolExecutionRequest lookup =
        ToolExecutionRequest.builder().id("call-1").name("order_status").arguments("{\"id\":1234}").build();
    List<ChatMessage> turns =
        List.of(
            SystemMessage.from("You answer order questions."),
            UserMessage.from("Where is order 1234?"),
            AiMessage.from(lookup),
            ToolExecutionResultMessage.from(lookup, "shipped"),
            AiMessage.from("Order 1234 shipped today."),
            UserMessage.from("ok"),
            AiMessage.from("ok"));
    for (ChatMessage m : turns) {
      memory.add(m);
      ConversationGetResult thread = loom.conversationGet(id);
      say(
          "add %-22s -> window %d, log %d, %s=%s %s=%s",
          m.type(),
          memory.messages().size(),
          thread.msgCount(),
          ConversationWindow.WINDOW_START,
          thread.metadata().path(ConversationWindow.WINDOW_START),
          ConversationWindow.WINDOW_HEAD,
          thread.metadata().path(ConversationWindow.WINDOW_HEAD));
    }
    List<ChatMessage> window = store.getMessages(id);
    say("window: %s", show(window));
    assertEquals(memory.messages(), window);
    assertEquals(4, window.size());
    assertEquals(turns.size(), loom.conversationGet(id).msgCount()); // evicted turns stay in the log

    ChatMemory reloaded =
        MessageWindowChatMemory.builder()
            .id(id)
            .maxMessages(4)
            .chatMemoryStore(LoomChatMemoryStore.builder().client(loom).build())
            .build();
    say("reloaded in a new store: %s", show(reloaded.messages()));
    assertEquals(window, reloaded.messages());

    reloaded.add(SystemMessage.from("You answer billing questions."));
    say("new system message: %s", show(store.getMessages(id)));
    assertTrue(store.getMessages(id).contains(SystemMessage.from("You answer billing questions.")));

    LoomException off;
    try {
      loom.conversationSetLogging(false);
      off = assertThrows(LoomException.class, () -> reloaded.add(UserMessage.from("lost?")));
    } finally {
      say("conversation.set_logging enabled=true -> %s", loom.conversationSetLogging(true));
    }
    say("recording off -> %s: %s", off.code(), off.getMessage());
    assertEquals("LOGGING_DISABLED", off.code());

    reloaded.clear();
    say("clear() -> conversation.delete; found=%s, window %s", loom.conversationGet(id).found(),
        show(store.getMessages(id)));
    assertEquals(List.of(), store.getMessages(id));

    memory.add(UserMessage.from("Where is order 1234?"));
    say("same id after delete: %s (log %s)", show(store.getMessages(id)),
        loom.conversationGet(id).metadata().path(ConversationWindow.LOG));
    assertEquals(List.of(UserMessage.from("Where is order 1234?")), store.getMessages(id));
    store.deleteMessages(id);
    say("done");
  }
}
