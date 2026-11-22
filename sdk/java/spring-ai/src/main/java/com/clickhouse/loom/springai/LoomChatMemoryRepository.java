package com.clickhouse.loom.springai;

import com.clickhouse.loom.ConversationWindow;
import com.clickhouse.loom.ConversationWindow.Turn;
import com.clickhouse.loom.LoomClient;
import com.clickhouse.loom.LoomException;
import com.clickhouse.loom.NotStored;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import org.springframework.ai.chat.memory.ChatMemoryRepository;
import org.springframework.ai.chat.messages.AbstractMessage;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.ToolResponseMessage;
import org.springframework.ai.chat.messages.UserMessage;

/**
 * A {@link ChatMemoryRepository} that keeps each conversation in a Loom conversation thread whose
 * id is the conversation id.
 *
 * <p>Each message is one turn: user, assistant, system and tool messages are logged with role
 * {@code user}, {@code assistant}, {@code system} and {@code tool}. The content is the message
 * text; for a tool message, the tool responses' data, one per line. What the role and text cannot
 * carry (message metadata, an assistant's tool calls, the tool responses' ids and names) goes into
 * the turn's metadata under {@value #METADATA_KEY}. Media attachments are not stored.
 *
 * <p>{@link #saveAll} keeps the log append-only and moves a window pointer instead of deleting
 * turns; {@link ConversationWindow} describes how. {@link #deleteByConversationId} calls {@code
 * conversation.delete}, which needs the {@code admin} role.
 *
 * <pre>{@code
 * ChatMemory memory = MessageWindowChatMemory.builder()
 *     .chatMemoryRepository(LoomChatMemoryRepository.builder().client(loom).build())
 *     .maxMessages(20)
 *     .build();
 * }</pre>
 */
public final class LoomChatMemoryRepository implements ChatMemoryRepository {
  /** The turn metadata key that holds what the role and content cannot carry. */
  public static final String METADATA_KEY = "spring_ai";

  private static final ObjectMapper JSON = new ObjectMapper();
  private static final TypeReference<Map<String, Object>> MAP = new TypeReference<>() {};

  private final ConversationWindow window;

  private LoomChatMemoryRepository(Builder b) {
    this.window =
        new ConversationWindow(Objects.requireNonNull(b.client, "client is required"), b.onNotStored);
  }

  public static Builder builder() {
    return new Builder();
  }

  /**
   * The ids of this agent's conversations with a live turn in the last {@value
   * ConversationWindow#THREADS_WINDOW_DAYS} days, most recently active first ({@code
   * conversation.threads} looks back no further).
   */
  @Override
  public List<String> findConversationIds() {
    return window.threadIds();
  }

  @Override
  public List<Message> findByConversationId(String conversationId) {
    List<Message> messages = new ArrayList<>();
    for (Turn t : window.read(conversationId)) {
      messages.add(toMessage(t));
    }
    return messages;
  }

  /**
   * Store {@code messages} as the conversation's window.
   *
   * @throws LoomException when the server refuses a call, or answers an append with {@code stored:
   *     false} (recording is off: an admin turns it on with {@code conversation.set_logging}),
   *     unless the repository was built with {@code onNotStored(NotStored.IGNORE)}
   */
  @Override
  public void saveAll(String conversationId, List<Message> messages) {
    List<Turn> turns = new ArrayList<>();
    for (Message m : messages) {
      turns.add(toTurn(m));
    }
    window.replace(conversationId, turns);
  }

  /**
   * Soft-delete the conversation's thread with {@code conversation.delete}.
   *
   * @throws LoomException with status 403 for a token without the {@code admin} role
   */
  @Override
  public void deleteByConversationId(String conversationId) {
    window.delete(conversationId);
  }

  static Turn toTurn(Message message) {
    ObjectNode extra = JSON.createObjectNode();
    Map<String, Object> metadata = new HashMap<>(message.getMetadata());
    metadata.remove(AbstractMessage.MESSAGE_TYPE); // implied by the role
    if (!metadata.isEmpty()) {
      extra.set("metadata", JSON.valueToTree(metadata));
    }
    String role;
    String content;
    switch (message.getMessageType()) {
      case USER -> {
        role = "user";
        content = message.getText();
      }
      case SYSTEM -> {
        role = "system";
        content = message.getText();
      }
      case ASSISTANT -> {
        role = "assistant";
        content = message.getText();
        if (content == null) {
          extra.put("text_null", true); // a reply that only calls tools has no text
        }
        List<AssistantMessage.ToolCall> calls = ((AssistantMessage) message).getToolCalls();
        if (!calls.isEmpty()) {
          ArrayNode list = extra.putArray("tool_calls");
          for (AssistantMessage.ToolCall c : calls) {
            list.addObject()
                .put("id", c.id())
                .put("type", c.type())
                .put("name", c.name())
                .put("arguments", c.arguments());
          }
        }
      }
      case TOOL -> {
        role = "tool";
        List<String> data = new ArrayList<>();
        ArrayNode list = extra.putArray("responses");
        for (ToolResponseMessage.ToolResponse r : ((ToolResponseMessage) message).getResponses()) {
          list.addObject().put("id", r.id()).put("name", r.name()).put("response_data", r.responseData());
          data.add(r.responseData() == null ? "" : r.responseData());
        }
        content = String.join("\n", data);
      }
      default -> throw new IllegalArgumentException("unknown message type " + message.getMessageType());
    }
    ObjectNode turnMetadata = JSON.createObjectNode();
    if (!extra.isEmpty()) {
      turnMetadata.set(METADATA_KEY, extra);
    }
    return new Turn(role, content, turnMetadata);
  }

  static Message toMessage(Turn turn) {
    JsonNode extra = turn.metadata().path(METADATA_KEY);
    Map<String, Object> metadata =
        extra.has("metadata") ? JSON.convertValue(extra.get("metadata"), MAP) : new HashMap<>();
    return switch (turn.role()) {
      case "user" -> UserMessage.builder().text(turn.content()).metadata(metadata).build();
      case "system" -> SystemMessage.builder().text(turn.content()).metadata(metadata).build();
      case "assistant" -> {
        List<AssistantMessage.ToolCall> calls = new ArrayList<>();
        for (JsonNode c : extra.path("tool_calls")) {
          calls.add(
              new AssistantMessage.ToolCall(
                  text(c, "id"), text(c, "type"), text(c, "name"), text(c, "arguments")));
        }
        yield AssistantMessage.builder()
            .content(extra.path("text_null").asBoolean(false) ? null : turn.content())
            .properties(metadata)
            .toolCalls(calls)
            .build();
      }
      case "tool" -> {
        List<ToolResponseMessage.ToolResponse> responses = new ArrayList<>();
        for (JsonNode r : extra.path("responses")) {
          responses.add(
              new ToolResponseMessage.ToolResponse(
                  text(r, "id"), text(r, "name"), text(r, "response_data")));
        }
        yield ToolResponseMessage.builder().responses(responses).metadata(metadata).build();
      }
      default -> throw new IllegalStateException("a turn with role " + turn.role());
    };
  }

  private static String text(JsonNode node, String field) {
    JsonNode v = node.get(field);
    return v == null || v.isNull() ? null : v.asText();
  }

  /** Builds a {@link LoomChatMemoryRepository}. */
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

    public LoomChatMemoryRepository build() {
      return new LoomChatMemoryRepository(this);
    }
  }
}
