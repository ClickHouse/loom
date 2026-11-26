use super::*;
use clickhouse_loom::{
    ConversationDeleteInput, ConversationUpdateInput, MemoryPromoteInput, MemoryVerifyInput,
};

#[tokio::test]
#[ignore = "requires the authenticated integration server and disposable tenant tokens"]
async fn promotion_and_verification_use_the_selected_source() {
    let bound = client("source-targets", "bound", "bound-user", "bound-session");
    let target = client("source-targets", "target", "target-user", "target-session");
    for (writer, value) in [(&bound, "bound source"), (&target, "target source")] {
        writer
            .memory_set(MemorySetInput::new("source", value))
            .await
            .unwrap();
    }
    let verified = MemoryVerifyInput::new()
        .memory_key("source")
        .agent("target")
        .user_id("target-user")
        .session_id("target-session")
        .send(&bound, CallOptions::default())
        .await
        .unwrap()
        .data;
    assert!(verified.found && verified.changed);
    let selected = call(&target, "memory.get", json!({"memory_key": "source"})).await;
    let neighbour = call(&bound, "memory.get", json!({"memory_key": "source"})).await;
    assert_eq!(selected["verified_by"], verified.verified_by);
    assert_eq!(neighbour["verified_by"], "");
    bound
        .memory_promote(
            MemoryPromoteInput::new("source")
                .target_key("shared-source")
                .agent("target")
                .user_id("target-user")
                .session_id("target-session"),
        )
        .await
        .unwrap();
    let shared = call(
        &bound,
        "memory.search",
        json!({
            "query": "target source", "search_mode": "lexical", "scope": "shared", "reinforce": false
        }),
    )
    .await;
    assert!(
        shared["results"]
            .as_array()
            .unwrap()
            .iter()
            .any(|hit| hit["memory_key"] == "shared-source"
                && hit["content_excerpt"]
                    .as_str()
                    .unwrap()
                    .contains("target source")),
        "{shared}"
    );
    assert_eq!(
        call(&bound, "memory.get", json!({"memory_key": "source"})).await["value"],
        "bound source"
    );
}

#[tokio::test]
#[ignore = "requires the authenticated integration server and disposable tenant tokens"]
async fn conversation_update_and_delete_leave_the_other_agents_thread_intact() {
    let bound = client("thread-targets", "bound", "bound-user", "bound-session");
    let target = client("thread-targets", "target", "target-user", "target-session");
    for (writer, thread, title) in [
        (&bound, "bound-thread", "bound title"),
        (&target, "target-thread", "target title"),
    ] {
        call(
            writer,
            "conversation.create",
            json!({"thread_id": thread, "title": title}),
        )
        .await;
    }
    bound
        .conversation_update(
            ConversationUpdateInput::new("target-thread")
                .agent("target")
                .title("revised target"),
        )
        .await
        .unwrap();
    assert_eq!(
        call(
            &target,
            "conversation.get",
            json!({"thread_id": "target-thread"})
        )
        .await["title"],
        "revised target"
    );
    assert_eq!(
        call(
            &bound,
            "conversation.get",
            json!({"thread_id": "bound-thread"})
        )
        .await["title"],
        "bound title"
    );
    ConversationDeleteInput::new("target-thread", "target-thread")
        .agent("target")
        .send(&bound, CallOptions::default())
        .await
        .unwrap();
    assert_eq!(
        call(
            &target,
            "conversation.get",
            json!({"thread_id": "target-thread"})
        )
        .await["found"],
        false
    );
    assert_eq!(
        call(
            &bound,
            "conversation.get",
            json!({"thread_id": "bound-thread"})
        )
        .await["title"],
        "bound title"
    );
}
