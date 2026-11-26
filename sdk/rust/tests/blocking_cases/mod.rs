use super::*;
use clickhouse_loom::{
    blocking, CallOptions, ContextPackInput, KnowledgeAddInput, KnowledgeGetInput,
    KnowledgeSearchInput, OpsCapabilitiesInput, OpsHealthInput,
};

fn request(server: &Server) -> Request {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(server.request())
}

#[test]
fn bound_run_ids_are_validated_before_calls() {
    for run_id in ["private\nrun", "private\rrun", "private\0run"] {
        let error = blocking::Client::builder("org", "ns", "bot")
            .api_key("")
            .run_id(run_id)
            .build()
            .unwrap_err();
        assert!(matches!(error, Error::Configuration(_)));
        assert!(!format!("{error:?} {error}").contains("private"));
    }
    assert!(blocking::Client::builder("org", "ns", "bot")
        .api_key("")
        .run_id("run-123")
        .build()
        .is_ok());
}

#[test]
fn digest_session_filters_survive_bound_identity() {
    let cases = full_registry::digest_filter_cases();
    let server = Server::new(vec![response(400, "{}", ""); cases.len()]);
    let client = blocking::Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .session_id("current-session")
        .build()
        .unwrap();
    for input in cases {
        let expected = input.session_id.clone();
        client.memory_digests(input).unwrap_err();
        let received = request(&server);
        assert_eq!(
            received.body.get("session_id").is_some(),
            expected.is_some()
        );
        if let Some(expected) = expected {
            assert_eq!(received.body["session_id"], expected);
        }
    }
}

#[test]
fn decisions_inherit_bound_run_authorship() {
    let cases = full_registry::decision_run_cases();
    let server = Server::new(vec![response(400, "{}", ""); cases.len()]);
    let client = blocking::Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .run_id("current-run")
        .build()
        .unwrap();
    for input in cases {
        client.decision_record(input).unwrap_err();
        let received = request(&server);
        assert_eq!(received.body["run_id"], "current-run");
        assert_eq!(received.headers["x-loom-run-id"], "current-run");
    }
}

#[test]
fn conversation_reads_preserve_filters_and_default_only_required_identity() {
    let cases = full_registry::conversation_cases();
    let server = Server::new(vec![response(400, "{}", ""); cases.len()]);
    let client = blocking::Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer")
        .build()
        .unwrap();
    for (op, input, expected) in cases {
        client
            .call::<Value>(op, &input, CallOptions::default())
            .unwrap_err();
        let request = request(&server);
        assert_eq!(request.line, format!("POST /v1/{op} HTTP/1.1"));
        assert_eq!(request.body, expected, "{op}");
    }
}

#[test]
fn synchronous_facades_preserve_wire_contracts_and_metadata() {
    let payloads = [
        json!({"version": 1}),
        json!({"found": true, "value": "value"}),
        json!({"results": [], "embedding_model": "embed"}),
        json!({"version": 2, "tombstoned": true}),
        json!({"rendered": "context", "sections": {}, "section_count": 0, "token_budget": 2000}),
        json!({"doc_id": "doc", "chunks_created": 1, "embedding_model": "embed", "embedding_dim": 3}),
        json!({"found": true, "source_uri": "urn:guide", "content": "guide"}),
        json!({"results": [], "embedding_model": "embed"}),
        json!({"checks": [], "overall": "ok"}),
        json!({}),
    ];
    let server = Server::new(
        payloads
            .into_iter()
            .map(|body| response(200, body, "X-Request-Id: echoed\r\n"))
            .collect(),
    );
    let client = blocking::Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .session_id("session")
        .build()
        .unwrap();
    assert_eq!(
        client.set(SetRequest::new("key", "value")).unwrap().version,
        1
    );
    assert_eq!(client.get("key").unwrap().value, "value");
    assert!(client
        .search(SearchRequest::new("value"))
        .unwrap()
        .results
        .is_empty());
    assert!(client.delete("key").unwrap().tombstoned);
    assert_eq!(
        client
            .context_pack(ContextPackInput::new())
            .unwrap()
            .rendered,
        "context"
    );
    assert_eq!(
        client
            .knowledge_add(KnowledgeAddInput::new("urn:guide", "guide"))
            .unwrap()
            .doc_id,
        "doc"
    );
    assert!(
        client
            .knowledge_get(KnowledgeGetInput::new("urn:guide"))
            .unwrap()
            .found
    );
    assert!(client
        .knowledge_search(KnowledgeSearchInput::new("guide"))
        .unwrap()
        .results
        .is_empty());
    assert!(client
        .health(OpsHealthInput::new())
        .unwrap()
        .checks
        .is_empty());
    let result = client
        .capabilities_with_options(
            OpsCapabilitiesInput::new(),
            CallOptions::default().request_id("sent"),
        )
        .unwrap();
    assert_eq!(result.metadata.sent_request_id, "sent");
    assert_eq!(result.metadata.request_id, "echoed");
    assert_eq!(result.metadata.attempts, 1);
    for op in [
        "memory.set",
        "memory.get",
        "memory.search",
        "memory.delete",
        "context.pack",
        "knowledge.add",
        "knowledge.get",
        "knowledge.search",
        "ops.health",
        "ops.capabilities",
    ] {
        let request = request(&server);
        assert_eq!(request.line, format!("POST /v1/{op} HTTP/1.1"));
        assert_eq!(request.body["org"], "org");
        if op.starts_with("knowledge.") && op != "knowledge.add" {
            assert!(request.body.get("agent").is_none());
            assert!(request.body.get("session_id").is_none());
        }
        if op == "memory.search" {
            assert_eq!(request.body["agent"], "*");
            assert!(request.body.get("session_id").is_none());
        }
    }
}

#[test]
fn blocking_generic_call_propagates_retries_and_failure_metadata() {
    let server = Server::new(vec![
        response(503, "{}", ""),
        response(200, r#"{"ok":true}"#, ""),
        response(
            400,
            r#"{"detail":{"code":"invalid_input","message":"bad"}}"#,
            "X-Request-Id: failure\r\n",
        ),
    ]);
    let client = blocking::Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .build()
        .unwrap();
    let result = client
        .call::<Value>(
            "knowledge.delete",
            &json!({"source_uri":"urn:guide"}),
            CallOptions::default().idempotency_key("key"),
        )
        .unwrap();
    assert_eq!(result.metadata.attempts, 2);
    assert_eq!(result.data["ok"], true);
    for _ in 0..2 {
        assert_eq!(request(&server).headers["idempotency-key"], "key");
    }
    let failure = client
        .with_retry(RetryPolicy::disabled())
        .unwrap()
        .get_with_options("key", CallOptions::default())
        .unwrap_err();
    assert!(matches!(failure.error, Error::Api(_)));
    assert_eq!(failure.metadata.unwrap().request_id, "failure");
    request(&server);
    let failure = client.set(SetRequest::new("key", Value::Null)).unwrap_err();
    assert!(matches!(failure, Error::Input(_)));
    assert!(client
        .call::<Value>("../memory.set", &json!({}), CallOptions::default())
        .unwrap_err()
        .metadata
        .is_none());
    assert!(blocking::Client::builder("", "ns", "bot")
        .api_key("")
        .build()
        .is_err());
}

#[test]
fn clones_run_concurrently_and_survive_original_drop() {
    fn send_sync<T: Send + Sync>() {}
    send_sync::<blocking::Client>();
    let server = Server::new(vec!["pair".into(), response(200, r#"{"found":false}"#, "")]);
    let client = blocking::Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .build()
        .unwrap();
    let first = client.clone();
    let second = client.clone();
    drop(client);
    let first = thread::spawn(move || first.get("first").unwrap());
    let second = thread::spawn(move || second.get("second").unwrap());
    assert!(!first.join().unwrap().found);
    assert!(!second.join().unwrap().found);
    request(&server);
    request(&server);
}

#[tokio::test]
async fn runtime_construction_drop_and_calls_have_explicit_boundaries() {
    let client = blocking::Client::builder("org", "ns", "bot")
        .api_key("")
        .build()
        .unwrap();
    let failure = client
        .get_with_options("key", CallOptions::default())
        .unwrap_err();
    assert!(matches!(failure.error, Error::Configuration(_)));
    assert!(failure.metadata.is_none());
    let clone = client.clone();
    drop(client);
    let rejected = tokio::task::spawn_blocking(move || clone.get("key"))
        .await
        .unwrap()
        .unwrap_err();
    assert!(matches!(rejected, Error::Configuration(_)));
}
