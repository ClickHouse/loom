use super::*;
use clickhouse_loom::{
    CallOptions, ContextPackInput, KnowledgeAddInput, KnowledgeGetInput, KnowledgeSearchInput,
    OpsCapabilitiesInput, OpsHealthInput,
};

fn payloads() -> Vec<(&'static str, Value)> {
    vec![
        (
            "context.pack",
            json!({"rendered": "context", "sections": {}, "section_count": 0, "token_budget": 2000}),
        ),
        (
            "knowledge.add",
            json!({"doc_id": "doc", "chunks_created": 1, "embedding_model": "embed", "embedding_dim": 3}),
        ),
        (
            "knowledge.get",
            json!({"found": true, "source_uri": "urn:guide", "content": "guide"}),
        ),
        (
            "knowledge.search",
            json!({"results": [], "embedding_model": "embed"}),
        ),
        ("ops.health", json!({"checks": [], "overall": "ok"})),
        ("ops.capabilities", json!({})),
    ]
}

#[tokio::test]
async fn facades_and_generated_sends_share_declared_identity_and_metadata() {
    let replies = payloads()
        .into_iter()
        .cycle()
        .take(12)
        .map(|(_, body)| response(200, body, "X-Schema-Version: test\r\n"))
        .collect();
    let server = Server::new(replies);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("user")
        .session_id("session")
        .run_id("run")
        .build()
        .unwrap();
    let context = ContextPackInput::new()
        .query("task")
        .agent("other")
        .user_id("other");
    let add = KnowledgeAddInput::new("urn:guide", "guide")
        .agent("other")
        .session_id("other");
    let get = KnowledgeGetInput::new("urn:guide");
    let search = KnowledgeSearchInput::new("guide").session_ids(["filter"]);
    let health = OpsHealthInput::new().namespace_null();
    let capabilities = OpsCapabilitiesInput::new().consumer("rust");
    assert_eq!(
        client.context_pack(context.clone()).await.unwrap().rendered,
        "context"
    );
    assert_eq!(
        client.knowledge_add(add.clone()).await.unwrap().doc_id,
        "doc"
    );
    assert_eq!(
        client
            .knowledge_get(get.clone())
            .await
            .unwrap()
            .content
            .as_deref(),
        Some("guide")
    );
    assert!(client
        .knowledge_search(search.clone())
        .await
        .unwrap()
        .results
        .is_empty());
    assert!(client
        .health(health.clone())
        .await
        .unwrap()
        .checks
        .is_empty());
    assert!(client
        .capabilities(capabilities.clone())
        .await
        .unwrap()
        .ops
        .is_empty());

    let options = CallOptions::default().request_id("phase");
    assert_eq!(
        context
            .send(&client, options.clone())
            .await
            .unwrap()
            .metadata
            .request_id,
        "phase"
    );
    assert_eq!(
        add.send(&client, options.clone())
            .await
            .unwrap()
            .metadata
            .status,
        Some(200)
    );
    assert_eq!(
        get.send(&client, options.clone())
            .await
            .unwrap()
            .metadata
            .attempts,
        1
    );
    assert_eq!(
        search
            .send(&client, options.clone())
            .await
            .unwrap()
            .metadata
            .schema_version
            .as_deref(),
        Some("test")
    );
    health.send(&client, options.clone()).await.unwrap();
    capabilities.send(&client, options).await.unwrap();

    for (op, _) in payloads().into_iter().cycle().take(12) {
        let request = server.request().await;
        assert_eq!(request.line, format!("POST /v1/{op} HTTP/1.1"));
        assert_eq!(request.body["org"], "org");
        if op != "ops.capabilities" {
            assert_eq!(request.body["namespace"], "ns");
        } else {
            assert!(request.body.get("namespace").is_none());
        }
        match op {
            "context.pack" => {
                for (key, value) in [
                    ("agent", "bot"),
                    ("user_id", "user"),
                    ("session_id", "session"),
                    ("run_id", "run"),
                ] {
                    assert_eq!(request.body[key], value);
                }
            }
            "knowledge.add" => {
                assert_eq!(request.body["agent"], "bot");
                assert_eq!(request.body["session_id"], "session");
                assert!(request.body.get("user_id").is_none());
                assert!(request.body.get("run_id").is_none());
            }
            _ => {
                for key in ["agent", "user_id", "session_id", "run_id"] {
                    assert!(request.body.get(key).is_none(), "{op}: {key}");
                }
            }
        }
        if op == "knowledge.search" {
            assert_eq!(request.body["session_ids"], json!(["filter"]));
        }
        assert_eq!(
            request.headers.contains_key("idempotency-key"),
            matches!(op, "knowledge.add" | "ops.health")
        );
    }
}

#[tokio::test]
async fn generic_calls_cover_operations_without_facades_and_future_operations() {
    let server = Server::new(vec![response(200, "{}", ""), response(200, "{}", "")]);
    let client = server.client();
    let result = client
        .call::<Value>(
            "memory.pin",
            &json!({"memory_key": "key", "org": "other"}),
            CallOptions::default(),
        )
        .await
        .unwrap();
    assert_eq!(result.data, json!({}));
    let known = server.request().await;
    assert_eq!(known.body["org"], "org");
    assert_eq!(known.body["agent"], "bot");
    assert!(known.headers.contains_key("idempotency-key"));
    client
        .call::<Value>(
            "future.operation",
            &json!({"org": "other", "custom": 7}),
            CallOptions::default(),
        )
        .await
        .unwrap();
    let future = server.request().await;
    assert_eq!(
        future.body,
        json!({"org": "org", "namespace": "ns", "custom": 7})
    );
    assert!(!future.headers.contains_key("idempotency-key"));
    for op in [
        "",
        "memory",
        "../memory.set",
        "memory.set?x=1",
        "memory..set",
        "memory.set/x",
        "memory.set#fragment",
        "memory.%73et",
    ] {
        let failure = client
            .call::<Value>(op, &json!({}), CallOptions::default())
            .await
            .unwrap_err();
        assert!(matches!(failure.error, Error::Input(_)), "{op}");
        assert!(failure.metadata.is_none());
    }
    assert!(client
        .call::<Value>("memory.pin", &json!([]), CallOptions::default())
        .await
        .unwrap_err()
        .metadata
        .is_none());
}

#[tokio::test]
async fn unknown_and_nonidempotent_operations_never_retry_after_dispatch() {
    for op in ["future.operation", "memory.touch"] {
        for reply in [
            response(503, "{}", ""),
            response(429, "{}", "Retry-After: 0.001\r\n"),
            "HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{}".into(),
        ] {
            let server = Server::new(vec![reply, response(200, "{}", "")]);
            let mut retry = RetryPolicy::default();
            retry.retry_timeouts = RetryTimeouts::Always;
            let client = server.client().with_retry(retry).unwrap();
            let failure = client
                .call::<Value>(op, &json!({}), CallOptions::default())
                .await
                .unwrap_err();
            assert_eq!(failure.metadata.unwrap().attempts, 1, "{op}");
            assert!(!server
                .request()
                .await
                .headers
                .contains_key("idempotency-key"));
        }
    }
}

#[tokio::test]
async fn knowledge_and_health_replays_reuse_explicit_keys() {
    for op in ["knowledge.add", "ops.health", "memory.pin"] {
        let server = Server::new(vec![
            "HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{}".into(),
            response(200, "{}", ""),
        ]);
        let result = server
            .client()
            .call::<Value>(
                op,
                &json!({}),
                CallOptions::default().idempotency_key("replay"),
            )
            .await
            .unwrap();
        assert_eq!(result.metadata.attempts, 2);
        for count in ["0", "1"] {
            let request = server.request().await;
            assert_eq!(request.headers["idempotency-key"], "replay");
            assert_eq!(request.headers["x-loom-retry-count"], count);
        }
    }
    for op in [
        "knowledge.get",
        "ops.capabilities",
        "memory.touch",
        "future.operation",
    ] {
        let failure = Client::builder("org", "ns", "bot")
            .api_key("")
            .build()
            .unwrap()
            .call::<Value>(
                op,
                &json!({}),
                CallOptions::default().idempotency_key("replay"),
            )
            .await
            .unwrap_err();
        assert!(matches!(failure.error, Error::Input(_)));
        assert!(failure.metadata.is_none());
    }
}
