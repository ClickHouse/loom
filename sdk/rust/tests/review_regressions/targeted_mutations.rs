use super::*;
use clickhouse_loom::{
    ConversationCompactInput, ConversationDeleteInput, ConversationUnsaveMessageInput,
    ConversationUpdateInput, MemoryPromoteInput, MemoryVerifyInput,
};

fn output() -> Value {
    json!({
        "shared_memory_key": "key", "promotion_event": "event",
        "reversible_until": "2020-01-01T00:00:00Z", "found": false,
        "updated": false, "thread_id": "thread",
        "tombstoned_at": "2020-01-01T00:00:00Z", "rows_affected": {}, "total": 0,
        "compacted": false, "messages_summarized": 0,
        "conversation_id": "thread", "message_id": "message", "ts": "2020-01-01T00:00:00Z"
    })
}

fn cases(fields: &[&str]) -> Vec<(Value, Value)> {
    [None, Some("target"), Some("-"), Some("")]
        .into_iter()
        .map(|target| {
            let mut input = json!({
                "org": "other-org", "namespace": "other-ns",
                "memory_key": "key", "thread_id": "thread", "confirm_thread_id": "thread",
                "title": "revision", "conversation_id": "thread", "message_id": "message"
            });
            if fields == ["agent", "user_id"] {
                // The saved-message owner is a required constructor argument.
                input["user_id"] = json!("writer");
            }
            let mut expected = input.clone();
            expected["org"] = json!("org");
            expected["namespace"] = json!("ns");
            for &field in fields {
                if let Some(value) = target {
                    input[field] = json!(value);
                }
                expected[field] = json!(target.unwrap_or(match field {
                    "agent" => "bot",
                    "user_id" => "writer",
                    "session_id" => "session",
                    _ => unreachable!(),
                }));
            }
            (input, expected)
        })
        .collect()
}

#[tokio::test]
async fn source_and_mutation_targets_survive_all_async_entry_points() {
    macro_rules! check {
        ($op:literal, $ty:ty, $method:ident, $fields:expr) => {{
            let cases = cases($fields);
            let server = Server::new(vec![response(200, output(), ""); cases.len() * 3]);
            let client = Client::builder("org", "ns", "bot")
                .base_url(&server.url)
                .api_key("")
                .user_id("writer")
                .session_id("session")
                .build()
                .unwrap();
            for (body, expected) in cases {
                let input: $ty = serde_json::from_value(body.clone()).unwrap();
                client.$method(input.clone()).await.unwrap();
                input.send(&client, CallOptions::default()).await.unwrap();
                client
                    .call::<Value>($op, &body, CallOptions::default())
                    .await
                    .unwrap();
                for _ in 0..3 {
                    let request = server.request().await;
                    assert_eq!(request.line, concat!("POST /v1/", $op, " HTTP/1.1"));
                    for field in ["org", "namespace"].iter().chain($fields.iter()) {
                        assert_eq!(request.body[*field], expected[*field], "{} {}", $op, field);
                    }
                }
            }
        }};
    }
    check!(
        "memory.promote",
        MemoryPromoteInput,
        memory_promote,
        &["agent", "user_id", "session_id"]
    );
    check!(
        "memory.verify",
        MemoryVerifyInput,
        memory_verify,
        &["agent", "user_id", "session_id"]
    );
    check!(
        "conversation.update",
        ConversationUpdateInput,
        conversation_update,
        &["agent"]
    );
    check!(
        "conversation.delete",
        ConversationDeleteInput,
        conversation_delete,
        &["agent"]
    );
    check!(
        "conversation.compact",
        ConversationCompactInput,
        conversation_compact,
        &["agent"]
    );
    check!(
        "conversation.unsave_message",
        ConversationUnsaveMessageInput,
        conversation_unsave_message,
        &["agent", "user_id"]
    );
}

#[cfg(feature = "blocking")]
#[test]
fn source_and_mutation_targets_survive_blocking_calls() {
    macro_rules! check {
        ($op:literal, $ty:ty, $method:ident, $fields:expr) => {{
            let cases = cases($fields);
            let server = Server::new(vec![response(200, output(), ""); cases.len() * 2]);
            let client = clickhouse_loom::blocking::Client::builder("org", "ns", "bot")
                .base_url(&server.url)
                .api_key("")
                .user_id("writer")
                .session_id("session")
                .build()
                .unwrap();
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            for (body, expected) in cases {
                let input: $ty = serde_json::from_value(body.clone()).unwrap();
                client.$method(input).unwrap();
                client
                    .call::<Value>($op, &body, CallOptions::default())
                    .unwrap();
                for _ in 0..2 {
                    let request = runtime.block_on(server.request());
                    for field in ["org", "namespace"].iter().chain($fields.iter()) {
                        assert_eq!(request.body[*field], expected[*field], "{} {}", $op, field);
                    }
                }
            }
        }};
    }
    check!(
        "memory.promote",
        MemoryPromoteInput,
        memory_promote,
        &["agent", "user_id", "session_id"]
    );
    check!(
        "memory.verify",
        MemoryVerifyInput,
        memory_verify,
        &["agent", "user_id", "session_id"]
    );
    check!(
        "conversation.update",
        ConversationUpdateInput,
        conversation_update,
        &["agent"]
    );
    check!(
        "conversation.delete",
        ConversationDeleteInput,
        conversation_delete,
        &["agent"]
    );
    check!(
        "conversation.compact",
        ConversationCompactInput,
        conversation_compact,
        &["agent"]
    );
    check!(
        "conversation.unsave_message",
        ConversationUnsaveMessageInput,
        conversation_unsave_message,
        &["agent", "user_id"]
    );
}
