# Loom chat memory store for LangChain4j

`LoomChatMemoryStore` implements LangChain4j's `ChatMemoryStore` on the
[Loom Java client](../README.md): each memory is one Loom conversation thread whose id is
the memory id, so the turns a chat memory keeps are also in Loom's transcript and
`conversation.search`.

Requires Java 17+ and LangChain4j 1.x (built against `langchain4j-core` 1.24.0).

## Example

```java
LoomClient loom = LoomClient.builder().baseUrl("http://localhost:7777")
    .org("dev").namespace("local").agent("support-bot").build();

ChatMemory memory = MessageWindowChatMemory.builder()
    .id("order-1234")
    .maxMessages(20)
    .chatMemoryStore(LoomChatMemoryStore.builder().client(loom).build())
    .build();
```

## Recording must be on

Loom stores conversation turns only in a namespace whose recording switch is on. While it
is off, `conversation.append` answers `stored: false, reason: "logging_disabled"`, and the
store throws `LoomException` with `code()` `LOGGING_DISABLED`: a memory that silently keeps
nothing would lose the conversation on its next read. An admin turns recording on once per
namespace:

```java
loom.conversationSetLogging(true); // conversation.set_logging, admin role
```

To log on a best-effort basis instead, build the store with
`.onNotStored(NotStored.IGNORE)`.

## How messages are stored

| LangChain4j message          | Loom `role` | Loom `content`                                        |
| ---------------------------- | ----------- | ----------------------------------------------------- |
| `SystemMessage`              | `system`    | its text                                              |
| `UserMessage`                | `user`      | its text                                              |
| `AiMessage`                  | `assistant` | its text (empty for a reply that only requests tools) |
| `ToolExecutionResultMessage` | `tool`      | its text                                              |
| `CustomMessage`              | `system`    | empty                                                 |

When the role and the text cannot rebuild a message exactly (tool execution requests, a
user name, images, attributes, a custom message), the turn's `metadata` also holds the
message as LangChain4j's `ChatMessageSerializer` writes it, under `langchain4j_message`.

The log stays append-only. `updateMessages` reuses the stored turns the new list starts
with, appends only the rest, and moves a window pointer in the thread's metadata
(`loom_window_start`, plus `loom_window_head` for a system message kept first) instead of
deleting turns. A `MessageWindowChatMemory` that evicts a turn therefore sends one append
per new message, and the evicted turns stay searchable. Each turn's `msg_id` is
`<memoryId>:<log>:<position>`; `loom_log` changes when a deleted memory id is used again,
so its new turns never collide with the deleted ones.

`deleteMessages` (and `ChatMemory.clear()`) calls `conversation.delete`, which needs the
`admin` role: with a writer token the server answers 403 and the store throws
`LoomException`. See [the conversation operations](../../../docs/reference/operations/conversation.md).

## Build and test

```bash
mvn -f sdk/java install && mvn -f sdk/java/langchain4j verify
```

The tests run against a local fake HTTP server. One more test runs against a real Loom
when `LOOM_LIVE_BASE_URL` is set. It turns the namespace's recording on, off for one
refused append, and on again, and deletes its thread, so its token needs the admin role:

```bash
LOOM_LIVE_BASE_URL=http://localhost:7777 mvn -f sdk/java/langchain4j verify -Dtest='Live*'
```
