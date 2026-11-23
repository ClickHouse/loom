use clickhouse_loom::{
    CallOptions, Client, Error, MemoryGetByIdInput, MemoryGetInput, MemorySetInput,
    NamespaceGetInput, NamespaceRegisterInput, SearchRequest, SetRequest,
};
use serde_json::json;

fn client(base_url: &str, org: &str, namespace: &str, token: &str) -> Client {
    Client::builder(org, namespace, "rust-live")
        .base_url(base_url)
        .api_key(token)
        .user_id("reader")
        .session_id("conversation")
        .build()
        .unwrap()
}

// The authenticated integration job supplies a disposable tenant and its tokens.
#[tokio::test]
#[ignore = "requires the authenticated integration server and disposable tenant tokens"]
async fn authenticated_memory_and_namespace_contracts() {
    let base_url = std::env::var("LOOM_RUST_LIVE_BASE_URL").unwrap();
    let org = std::env::var("LOOM_RUST_LIVE_ORG").unwrap();
    let admin_token = std::env::var("LOOM_RUST_LIVE_ADMIN_TOKEN").unwrap();
    let writer_token = std::env::var("LOOM_RUST_LIVE_WRITER_TOKEN").unwrap();
    let other_token = std::env::var("LOOM_RUST_LIVE_OTHER_TOKEN").unwrap();
    let admin = client(&base_url, &org, "rust-live", &admin_token);
    admin
        .namespace_register(
            NamespaceRegisterInput::new("sdk@example.test").description("SDK contracts"),
        )
        .await
        .unwrap();
    let writer = client(&base_url, &org, "rust-live", &writer_token);
    let namespace = writer
        .namespace_get(NamespaceGetInput::new())
        .await
        .unwrap();
    assert!(namespace.found);
    assert_eq!(namespace.namespace, "rust-live");
    assert_eq!(namespace.description.as_deref(), Some("SDK contracts"));

    let value = json!({"message": "orchid evidence", "integer": 9_007_199_254_740_993_u64});
    let saved = writer
        .set(
            SetRequest::new("legacy", value.clone())
                .memory_type("semantic")
                .tags(["orchid"])
                .confidence(0.75),
        )
        .await
        .unwrap();
    let legacy = writer.get("legacy").await.unwrap();
    assert!(legacy.found);
    assert_eq!(legacy.value, value);
    assert_eq!(legacy.version, Some(saved.version));
    assert_eq!(legacy.memory_id, saved.memory_id);
    assert_eq!(legacy.memory_type.as_deref(), Some("semantic"));
    assert_eq!(legacy.confidence, Some(0.75));
    assert_eq!(legacy.pinned, Some(false));
    let stored = writer
        .memory_get(MemoryGetInput::new("legacy"))
        .await
        .unwrap();
    assert_eq!(stored.tags, ["orchid"]);
    assert_eq!(stored.agent, "rust-live");
    assert_eq!(stored.user_id, "reader");
    assert_eq!(stored.session_id, "conversation");
    assert_eq!(stored.actor, "process:rust-writer");
    assert_eq!(stored.source_class, "asserted");
    assert_eq!(stored.content_sha256, saved.content_sha256);

    let search = writer
        .search(
            SearchRequest::new("orchid evidence")
                .search_mode("lexical")
                .user_id("reader")
                .session_id("conversation"),
        )
        .await
        .unwrap();
    assert!(search.results.iter().any(|hit| hit.memory_key == "legacy"));

    let write = MemorySetInput::new("full", json!({"flag": false}))
        .memory_type("semantic")
        .tags(["full"])
        .confidence(0.5);
    let first = write
        .clone()
        .send(
            &writer,
            CallOptions::default()
                .request_id("full-first")
                .idempotency_key("full-replay"),
        )
        .await
        .unwrap();
    let replay = write
        .send(
            &writer,
            CallOptions::default()
                .request_id("full-replay")
                .idempotency_key("full-replay"),
        )
        .await
        .unwrap();
    assert_eq!(first.data, replay.data);
    assert_eq!(first.metadata.sent_request_id, "full-first");
    assert_eq!(replay.metadata.sent_request_id, "full-replay");
    assert_eq!(replay.metadata.status, Some(200));
    assert!(replay.metadata.schema_version.is_some());
    let full = writer
        .memory_get(MemoryGetInput::new("full"))
        .await
        .unwrap();
    assert!(full.found);
    assert_eq!(full.value, json!({"flag": false}));
    assert_eq!(full.version, Some(first.data.version));
    assert_eq!(full.memory_id, first.data.memory_id);
    assert_eq!(full.content_sha256, first.data.content_sha256);
    assert_eq!(full.memory_type.as_deref(), Some("semantic"));
    assert_eq!(full.confidence, Some(0.5));
    assert_eq!(full.tags, ["full"]);
    assert_eq!(full.agent, "rust-live");
    assert_eq!(full.user_id, "reader");
    assert_eq!(full.session_id, "conversation");
    assert_eq!(full.source_class, "asserted");
    assert_eq!(full.actor, "process:rust-writer");
    assert!(!full.pinned);

    let other = client(&base_url, &org, "other", &other_token);
    let hidden = other
        .memory_get_by_id(MemoryGetByIdInput::new(&full.memory_id))
        .await
        .unwrap();
    let missing = other
        .memory_get_by_id(MemoryGetByIdInput::new(uuid::Uuid::new_v4().to_string()))
        .await
        .unwrap();
    assert_eq!(hidden, missing);
    assert!(!hidden.found);
    let forbidden = client(&base_url, &org, "rust-live", &other_token);
    let failure = forbidden
        .namespace_get_with_options(
            NamespaceGetInput::new(),
            CallOptions::default().request_id("wrong-scope"),
        )
        .await
        .unwrap_err();
    let metadata = failure.metadata.unwrap();
    assert_eq!(metadata.status, Some(403));
    assert_eq!(metadata.sent_request_id, "wrong-scope");
    assert!(!metadata.request_id.is_empty());
    assert!(matches!(failure.error, Error::Api(_)));

    writer.delete("legacy").await.unwrap();
    assert!(!writer.get("legacy").await.unwrap().found);
}
