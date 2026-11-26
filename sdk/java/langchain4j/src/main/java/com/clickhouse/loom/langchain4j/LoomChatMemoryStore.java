package com.clickhouse.loom.langchain4j;

import com.clickhouse.loom.ConversationWindow;
import com.clickhouse.loom.ConversationWindow.Turn;
import com.clickhouse.loom.LoomClient;
import com.clickhouse.loom.LoomException;
import com.clickhouse.loom.NotStored;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.ChatMessageDeserializer;
import dev.langchain4j.data.message.ChatMessageSerializer;
import dev.langchain4j.data.message.Content;
import dev.langchain4j.data.message.SystemMessage;
import dev.langchain4j.data.message.TextContent;
import dev.langchain4j.data.message.ToolExecutionResultMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.store.memory.chat.ChatMemoryStore;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * A {@link ChatMemoryStore} that keeps each memory in a Loom conversation thread whose id is the
 * memory id.
 *
 * <p>Each message is one turn: system, user, AI and tool-result messages are logged with role
 * {@code system}, {@code user}, {@code assistant} and {@code tool} and their text as content.
 * When the role and the text cannot rebuild the message exactly (tool-call requests, a user name,
 * images, attributes, a custom message), the turn's metadata also carries the message as
 * LangChain4j serializes it, under {@value #MESSAGE_KEY}.
 *
 * <p>{@link #updateMessages} keeps the log append-only and moves a window pointer instead of
 * deleting turns; {@link ConversationWindow} describes how. {@link #deleteMessages} calls {@code
 * conversation.delete}, which needs the {@code admin} role.
 *
 * <pre>{@code
 * ChatMemory memory = MessageWindowChatMemory.builder()
 *     .id("order-1234").maxMessages(20)
 *     .chatMemoryStore(LoomChatMemoryStore.builder().client(loom).build())
 *     .build();
 * }</pre>
 */
public final class LoomChatMemoryStore implements ChatMemoryStore {
  /** The turn metadata key that holds a message the role and content cannot rebuild. */
  public static final String MESSAGE_KEY = "langchain4j_message";

  private static final ObjectMapper JSON = new ObjectMapper();

  private final ConversationWindow window;

  private LoomChatMemoryStore(Builder b) {
    this.window =
        new ConversationWindow(Objects.requireNonNull(b.client, "client is required"), b.onNotStored);
  }

  public static Builder builder() {
    return new Builder();
  }

  @Override
  public List<ChatMessage> getMessages(Object memoryId) {
    List<ChatMessage> messages = new ArrayList<>();
    for (Turn t : window.read(threadId(memoryId))) {
      messages.add(toMessage(t));
    }
    return messages;
  }

  /**
   * Store {@code messages} as the memory's window.
   *
   * @throws LoomException when the server refuses a call, or answers an append with {@code stored:
   *     false} (recording is off: an admin turns it on with {@code conversation.set_logging}),
   *     unless the store was built with {@code onNotStored(NotStored.IGNORE)}
   */
  @Override
  public void updateMessages(Object memoryId, List<ChatMessage> messages) {
    List<Turn> turns = new ArrayList<>();
    for (ChatMessage m : messages) {
      turns.add(toTurn(m));
    }
    window.replace(threadId(memoryId), turns);
  }

  /**
   * Soft-delete the memory's thread with {@code conversation.delete}.
   *
   * @throws LoomException with status 403 for a token without the {@code admin} role
   */
  @Override
  public void deleteMessages(Object memoryId) {
    window.delete(threadId(memoryId));
  }

  private static String threadId(Object memoryId) {
    return String.valueOf(Objects.requireNonNull(memoryId, "memoryId"));
  }

  static Turn toTurn(ChatMessage message) {
    String role;
    String content;
    switch (message.type()) {
      case SYSTEM -> {
        role = "system";
        content = ((SystemMessage) message).text();
      }
      case USER -> {
        role = "user";
        content = text((UserMessage) message);
      }
      case AI -> {
        role = "assistant";
        content = ((AiMessage) message).text();
      }
      case TOOL_EXECUTION_RESULT -> {
        role = "tool";
        content = ((ToolExecutionResultMessage) message).text();
      }
      default -> {
        role = "system";
        content = "";
      }
    }
    content = content == null ? "" : content;
    String json = ChatMessageSerializer.messageToJson(message);
    ChatMessage plain = plain(role, content);
    if (plain != null && ChatMessageSerializer.messageToJson(plain).equals(json)) {
      return new Turn(role, content);
    }
    try {
      ObjectNode metadata = JSON.createObjectNode();
      metadata.set(MESSAGE_KEY, JSON.readTree(json));
      return new Turn(role, content, metadata);
    } catch (JsonProcessingException e) {
      throw new IllegalStateException("a serialized message is not JSON", e);
    }
  }

  static ChatMessage toMessage(Turn turn) {
    JsonNode serialized = turn.metadata().get(MESSAGE_KEY);
    if (serialized != null) {
      try {
        return ChatMessageDeserializer.messageFromJson(JSON.writeValueAsString(serialized));
      } catch (JsonProcessingException e) {
        throw new IllegalStateException("a stored message is not JSON", e);
      }
    }
    ChatMessage plain = plain(turn.role(), turn.content());
    if (plain == null) {
      throw new IllegalStateException(
          "a " + turn.role() + " turn without " + MESSAGE_KEY + " cannot be rebuilt as a message");
    }
    return plain;
  }

  /** The message that role and text alone make; null when they make none. */
  private static ChatMessage plain(String role, String content) {
    try {
      return switch (role) {
        case "system" -> SystemMessage.from(content);
        case "user" -> UserMessage.from(content);
        case "assistant" -> AiMessage.from(content);
        default -> null;
      };
    } catch (IllegalArgumentException e) {
      return null; // a blank system or user text: the serialized message carries it
    }
  }

  private static String text(UserMessage message) {
    if (message.hasSingleText()) {
      return message.singleText();
    }
    StringBuilder text = new StringBuilder();
    for (Content c : message.contents()) {
      if (c instanceof TextContent t) {
        if (text.length() > 0) {
          text.append('\n');
        }
        text.append(t.text());
      }
    }
    return text.toString();
  }

  /** Builds a {@link LoomChatMemoryStore}. */
  public static final class Builder {
    private LoomClient client;
    private NotStored onNotStored = NotStored.FAIL;

    private Builder() {}

    /** The client whose identity owns the threads. */
    public Builder client(LoomClient client) {
      this.client = client;
      return this;
    }

    /**
     * What an append that the server did not store does: {@link NotStored#FAIL} (the default)
     * throws, {@link NotStored#IGNORE} carries on without the turn.
     */
    public Builder onNotStored(NotStored onNotStored) {
      this.onNotStored = Objects.requireNonNull(onNotStored, "onNotStored");
      return this;
    }

    public LoomChatMemoryStore build() {
      return new LoomChatMemoryStore(this);
    }
  }
}
