# Loom chat memory repository for Spring AI

`LoomChatMemoryRepository` implements Spring AI's `ChatMemoryRepository` on the
[Loom Java client](../README.md): each conversation is one Loom conversation thread whose
id is the conversation id, so the turns a chat memory keeps are also in Loom's transcript
and `conversation.search`.

Requires Java 17+ and Spring AI 1.x (built against `spring-ai-model` 1.1.8).

## Example

```java
LoomClient loom = LoomClient.builder().baseUrl("http://localhost:7777")
    .org("dev").namespace("local").agent("support-bot").build();

ChatMemory memory = MessageWindowChatMemory.builder()
    .chatMemoryRepository(LoomChatMemoryRepository.builder().client(loom).build())
    .maxMessages(20)
    .build();
```

In a Spring Boot application, declare the repository as a `ChatMemoryRepository` bean.

## Recording must be on

Loom stores conversation turns only in a namespace whose recording switch is on. While it
is off, `conversation.append` answers `stored: false, reason: "logging_disabled"`, and the
repository throws `LoomException` with `code()` `LOGGING_DISABLED`: a memory that silently
keeps nothing would lose the conversation on its next read. An admin turns recording on
once per namespace:

```java
loom.conversationSetLogging(true); // conversation.set_logging, admin role
```

To log on a best-effort basis instead, build the repository with
`.onNotStored(NotStored.IGNORE)`.

## How messages are stored

| Spring AI message | Loom `role` | Loom `content` |
| --- | --- | --- |
| `UserMessage` | `user` | its text |
| `AssistantMessage` | `assistant` | its text (empty for a reply that only calls tools) |
| `SystemMessage` | `system` | its text |
| `ToolResponseMessage` | `tool` | the responses' data, one per line |

What the role and the text cannot carry goes into the turn's `metadata` under `spring_ai`:
the message's metadata map, an assistant's tool calls, and each tool response's id and
name. Metadata values come back as JSON reads them (numbers, strings, lists, maps). Media
attachments are not stored.

The log stays append-only. `saveAll` reuses the stored turns the new list starts with,
appends only the rest, and moves a window pointer in the thread's metadata
(`loom_window_start`, plus `loom_window_head` for a system message kept first) instead of
deleting turns. A `MessageWindowChatMemory` that evicts a turn therefore sends one append
per new message, and the evicted turns stay searchable. Each turn's `msg_id` is
`<conversationId>:<log>:<position>`; `loom_log` changes when a deleted conversation id is
used again, so its new turns never collide with the deleted ones.

`findConversationIds` lists the client agent's threads with a turn in the last 365 days,
the furthest `conversation.threads` looks back.

`deleteByConversationId` (and `ChatMemory.clear(id)`) calls `conversation.delete`, which
needs the `admin` role: with a writer token the server answers 403 and the repository
throws `LoomException`. See
[the conversation operations](../../../docs/reference/operations/conversation.md).

## Build and test

```bash
mvn -f sdk/java install && mvn -f sdk/java/spring-ai verify
```

The tests run against a local fake HTTP server. One more test runs against a real Loom
when `LOOM_LIVE_BASE_URL` is set (it turns recording on and deletes its thread, so its
token needs the admin role):

```bash
LOOM_LIVE_BASE_URL=http://localhost:7777 mvn -f sdk/java/spring-ai verify -Dtest='Live*'
```
