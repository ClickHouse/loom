use super::*;
use clickhouse_loom::{CallFailure, CallOptions};

#[tokio::test]
async fn generated_send_methods_share_transport_identity_and_metadata() {
    use clickhouse_loom::{
        MemoryDeleteInput as DeleteRequest, MemoryGetInput as GetRequest,
        MemorySearchInput as SearchRequest, MemorySetInput as SetRequest,
    };

    let server = Server::new(vec![
        response(200, json!({"version": 1}), "X-Request-Id: write-echo\r\n"),
        response(200, json!({"found": false}), ""),
        response(200, json!({"results": [], "embedding_model": "embed"}), ""),
        response(200, json!({"tombstoned": true, "version": 2}), ""),
    ]);
    let client = server.client();
    let failure = SetRequest::new("key", Value::Null)
        .send(&client, options("null"))
        .await
        .unwrap_err();
    assert!(matches!(failure.error, Error::Input(_)));
    assert!(failure.metadata.is_none());
    let saved = SetRequest::new("key", false)
        .dry_run(true)
        .ttl_seconds_null()
        .send(&client, options("write").idempotency_key("save-key"))
        .await
        .unwrap();
    assert_eq!(saved.metadata.request_id, "write-echo");
    let write = server.request().await;
    assert_eq!(write.headers["idempotency-key"], "save-key");
    assert_eq!(write.body["dry_run"], true);
    assert_eq!(write.body["ttl_seconds"], Value::Null);
    GetRequest::new("key")
        .org("other")
        .namespace("other")
        .agent("other")
        .fields(["value"])
        .follow(true)
        .send(&client, options("get"))
        .await
        .unwrap();
    let get = server.request().await;
    assert_eq!(get.body["org"], "org");
    assert_eq!(get.body["namespace"], "ns");
    assert_eq!(get.body["agent"], "bot");
    assert_eq!(get.body["fields"], json!(["value"]));
    assert_eq!(get.headers["x-request-id"], "get");
    SearchRequest::new("query")
        .user_id_null()
        .send(&client, options("search"))
        .await
        .unwrap();
    let search = server.request().await;
    assert_eq!(search.body["agent"], "*");
    assert_eq!(search.body["user_id"], Value::Null);
    DeleteRequest::new("key")
        .send(&client, options("delete"))
        .await
        .unwrap();
    let delete = server.request().await;
    assert_eq!(delete.line, "POST /v1/memory.delete HTTP/1.1");
    assert!(delete.headers.contains_key("idempotency-key"));
}

fn options(id: &str) -> CallOptions {
    CallOptions::default().request_id(id)
}

fn immediate_retry() -> RetryPolicy {
    let mut policy = RetryPolicy::default();
    policy.backoff = Duration::ZERO;
    policy.backoff_jitter = 0.0;
    policy.max_attempts = 2;
    policy
}

#[tokio::test]
async fn success_preserves_typed_data_and_final_response_metadata() {
    let headers = "X-Request-Id: echoed\r\nX-Schema-Version: 7\r\n\
        Server-Timing: db;dur=80, app;dur=12.5\r\n\
        X-RateLimit-Limit: 10\r\nX-RateLimit-Remaining: 4\r\nX-RateLimit-Reset: 2\r\n\
        X-Loom-Hint: Narrow your search\r\nX-Loom-Hint-Code: SEARCH_SCOPE\r\n";
    let server = Server::new(vec![response(201, json!({"version":VERSION}), headers)]);
    let result = server
        .client()
        .set_with_options(
            SetRequest::new("key", "fact"),
            options("sent").idempotency_key("save-1"),
        )
        .await
        .unwrap();
    assert_eq!(result.data.version, VERSION);
    let meta = result.metadata;
    assert_eq!(meta.status, Some(201));
    assert_eq!(meta.sent_request_id, "sent");
    assert_eq!(meta.request_id, "echoed");
    assert_eq!(meta.schema_version.as_deref(), Some("7"));
    assert_eq!(meta.server_timing_ms, Some(12.5));
    let rate = meta.rate_limit.unwrap();
    assert_eq!((rate.limit, rate.remaining, rate.reset_s), (10, 4, 2));
    assert_eq!(meta.attempts, 1);
    assert_eq!(meta.hint.as_deref(), Some("Narrow your search"));
    assert_eq!(meta.hint_code.as_deref(), Some("SEARCH_SCOPE"));
    let request = server.request().await;
    assert_eq!(request.headers["x-request-id"], "sent");
    assert_eq!(request.headers["idempotency-key"], "save-1");
    assert_eq!(request.body["agent"], "bot");
}

#[tokio::test]
async fn retries_and_pacing_keep_one_key_and_count_all_sends() {
    let server = Server::new(vec![
        response(503, "", "X-Schema-Version: stale\r\n"),
        response(429, "", "Retry-After: 0.001\r\n"),
        response(503, "", ""),
        response(200, json!({"tombstoned":true,"version":VERSION}), ""),
    ]);
    let result = server
        .client()
        .delete_with_options(
            "key",
            options("delete-attempts")
                .retry(immediate_retry())
                .idempotency_key("delete-1"),
        )
        .await
        .unwrap();
    assert!(result.data.tombstoned);
    assert_eq!(result.metadata.attempts, 4);
    assert_eq!(result.metadata.request_id, "delete-attempts");
    assert!(result.metadata.schema_version.is_none());
    for count in ["0", "1", "2", "3"] {
        let request = server.request().await;
        assert_eq!(request.headers["x-request-id"], "delete-attempts");
        assert_eq!(request.headers["idempotency-key"], "delete-1");
        assert_eq!(request.headers["x-loom-retry-count"], count);
    }
}

#[tokio::test]
async fn resubmission_can_keep_the_key_with_another_request_id() {
    let server = Server::new(vec![
        response(503, "", ""),
        response(200, json!({"version":VERSION}), ""),
        response(200, json!({"version":VERSION}), ""),
    ]);
    let client = server.client();
    let first = options("first")
        .retry(RetryPolicy::disabled())
        .idempotency_key("logical-save");
    let failure = client
        .set_with_options(SetRequest::new("key", "fact"), first)
        .await
        .unwrap_err();
    assert_eq!(failure.metadata.unwrap().request_id, "first");
    client
        .set_with_options(
            SetRequest::new("key", "fact"),
            options("resubmission").idempotency_key("logical-save"),
        )
        .await
        .unwrap();
    let first = server.request().await;
    let next = server.request().await;
    assert_eq!(first.body, next.body);
    assert_eq!(
        first.headers["idempotency-key"],
        next.headers["idempotency-key"]
    );
    assert_eq!(next.headers["x-request-id"], "resubmission");
    client.set(SetRequest::new("other", "fact")).await.unwrap();
    let default = server.request().await;
    assert_ne!(default.headers["idempotency-key"], "logical-save");
    assert_ne!(default.headers["x-request-id"], "resubmission");
}

#[tokio::test]
async fn concurrent_retry_overrides_do_not_change_defaults() {
    let server = Server::new(vec![
        "pair".into(),
        response(503, "", ""),
        response(200, json!({"found":false}), ""),
        response(200, json!({"found":false}), ""),
    ]);
    let client = server.client().with_retry(immediate_retry()).unwrap();
    let (once, retrying) = tokio::join!(
        client.get_with_options("once", options("once").retry(RetryPolicy::disabled())),
        client.get_with_options("retrying", options("retrying")),
    );
    let once = once.unwrap_err();
    assert!(matches!(once.error, Error::Api(_)));
    assert_eq!(once.metadata.unwrap().attempts, 1);
    assert_eq!(retrying.unwrap().metadata.attempts, 2);
    let first = server.request().await;
    let second = server.request().await;
    assert_ne!(
        first.headers["x-request-id"],
        second.headers["x-request-id"]
    );
    let retry = server.request().await;
    assert_eq!(retry.headers["x-request-id"], "retrying");
    assert_eq!(retry.headers["x-loom-retry-count"], "1");
    assert!(!retry.headers.contains_key("idempotency-key"));
    assert!(!client.get("default").await.unwrap().found);
    assert_ne!(server.request().await.headers["x-request-id"], "once");
}

#[tokio::test]
async fn timeout_override_covers_headers_and_body_without_leaking() {
    for (held, status) in [("hold", None), ("hold-body", Some(200))] {
        let server = Server::new(vec![held.into(), response(200, json!({"found":false}), "")]);
        let client = server.client();
        let failure = client
            .get_with_options("short", options("short").timeout(Duration::from_millis(50)))
            .await
            .unwrap_err();
        assert!(matches!(failure.error, Error::Transport(ref e) if e.is_timeout()));
        let meta = failure.metadata.unwrap();
        assert_eq!(meta.status, status);
        assert_eq!(meta.request_id, "short");
        assert_eq!(meta.attempts, 1);
        assert_eq!(server.request().await.headers["x-request-id"], "short");
        assert!(!client.get("default").await.unwrap().found);
        assert_eq!(server.request().await.body["memory_key"], "default");
    }
}

#[tokio::test]
async fn timed_out_write_retries_with_its_explicit_key() {
    let server = Server::new(vec![
        "hold-body".into(),
        response(200, json!({"version":VERSION}), ""),
    ]);
    let mut retry = immediate_retry();
    retry.backoff = Duration::from_millis(300);
    let result = server
        .client()
        .set_with_options(
            SetRequest::new("key", "fact"),
            options("write-timeout")
                .timeout(Duration::from_millis(50))
                .retry(retry)
                .idempotency_key("write-timeout-key"),
        )
        .await
        .unwrap();
    assert_eq!(result.metadata.attempts, 2);
    assert_eq!(result.data.version, VERSION);
    let first = server.request().await;
    let retry = server.request().await;
    assert_eq!(first.body, retry.body);
    assert_eq!(first.headers["idempotency-key"], "write-timeout-key");
    assert_eq!(retry.headers["idempotency-key"], "write-timeout-key");
    assert_eq!(retry.headers["x-request-id"], "write-timeout");
}

#[tokio::test]
async fn timeout_override_can_exceed_the_client_timeout() {
    let server = Server::new(vec![
        "pair".into(),
        response(200, json!({"found":false}), ""),
    ]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .timeout(Duration::from_millis(20))
        .build()
        .unwrap();
    let cloned = client.clone();
    let task = tokio::spawn(async move {
        cloned
            .get_with_options("long", options("long").timeout(Duration::from_secs(2)))
            .await
    });
    assert_eq!(server.request().await.body["memory_key"], "long");
    tokio::time::sleep(Duration::from_millis(80)).await;
    assert!(!client.get("release").await.unwrap().found);
    assert_eq!(task.await.unwrap().unwrap().metadata.request_id, "long");
    assert_eq!(server.request().await.body["memory_key"], "release");
}

#[tokio::test]
async fn search_options_preserve_filters_and_missing_metadata() {
    let server = Server::new(vec![response(
        200,
        json!({"results":[], "embedding_model": "embed"}),
        "",
    )]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer")
        .session_id("writing")
        .build()
        .unwrap();
    let result = client
        .search_with_options(
            SearchRequest::new("facts").user_id("reader"),
            options("search"),
        )
        .await
        .unwrap();
    assert!(result.data.results.is_empty());
    assert!(result.metadata.rate_limit.is_none());
    let request = server.request().await;
    assert_eq!(request.body["agent"], "*");
    assert_eq!(request.body["user_id"], "reader");
    assert!(request.body.get("session_id").is_none());
    assert!(!request.headers.contains_key("idempotency-key"));
}

#[tokio::test]
async fn api_and_protocol_failures_keep_headers_and_original_sources() {
    use std::error::Error as _;
    for (status, body) in [
        (
            403,
            json!({"detail":{"code":"DENIED","message":"refused"}}).to_string(),
        ),
        (200, "{".into()),
    ] {
        let server = Server::new(vec![response(
            status,
            body,
            "X-Request-Id: echoed\r\nX-Schema-Version: 7\r\nX-Loom-Hint-Code: HINT\r\n",
        )]);
        let failure = server
            .client()
            .get_with_options("key", options("sent"))
            .await
            .unwrap_err();
        assert_eq!(failure.to_string(), failure.error.to_string());
        assert!(std::ptr::eq(
            failure.source().unwrap().downcast_ref::<Error>().unwrap(),
            &failure.error
        ));
        let meta = failure.metadata.as_ref().unwrap();
        assert_eq!(meta.sent_request_id, "sent");
        assert_eq!(meta.request_id, "echoed");
        assert_eq!(meta.status, Some(status));
        assert_eq!(meta.schema_version.as_deref(), Some("7"));
        assert_eq!(meta.hint_code.as_deref(), Some("HINT"));
        match failure.error {
            Error::Api(api) => {
                assert_eq!(api.code, "DENIED");
                assert_eq!(api.request_id.as_deref(), Some("echoed"));
            }
            Error::Protocol { status: 200, .. } => {}
            error => panic!("unexpected error: {error}"),
        }
    }
}

#[tokio::test]
async fn empty_error_request_id_preserves_the_echo_and_metadata_falls_back() {
    let server = Server::new(vec![response(
        403,
        json!({"detail":{"code":"DENIED","message":"refused"}}),
        "X-Request-Id:\r\n",
    )]);
    let failure = server
        .client()
        .get_with_options("key", options("sent"))
        .await
        .unwrap_err();
    assert_eq!(failure.metadata.unwrap().request_id, "sent");
    match failure.error {
        Error::Api(api) => assert_eq!(api.request_id.as_deref(), Some("")),
        error => panic!("unexpected error: {error}"),
    }
}

#[tokio::test]
async fn incomplete_response_keeps_headers_but_later_disconnect_drops_stale_headers() {
    let truncated = "HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\
        X-Request-Id: echoed\r\nX-Schema-Version: 7\r\n\r\n{}";
    for (responses, expected_status, expected_id, attempts) in [
        (vec![truncated.into()], Some(200), "echoed", 1),
        (
            vec![
                response(503, "", "X-Request-Id: stale\r\nX-Schema-Version: 7\r\n"),
                "".into(),
            ],
            None,
            "sent",
            2,
        ),
    ] {
        let server = Server::new(responses);
        let failure = server
            .client()
            .get_with_options("key", options("sent").retry(immediate_retry()))
            .await
            .unwrap_err();
        assert!(matches!(failure.error, Error::Transport(_)));
        let meta = failure.metadata.unwrap();
        assert_eq!(meta.status, expected_status);
        assert_eq!(meta.request_id, expected_id);
        assert_eq!(meta.attempts, attempts);
        assert_eq!(meta.schema_version.as_deref(), expected_status.map(|_| "7"));
    }
}

#[tokio::test]
async fn invalid_options_and_payloads_fail_before_dispatch() {
    let server = Server::new(vec![response(200, json!({"found":false}), "")]);
    let client = server.client();
    let mut invalid = Vec::new();
    for id in ["", "two words", "line\r\nbreak", "é", &"x".repeat(65)] {
        invalid.push(options(id));
    }
    invalid.push(options("valid").timeout(Duration::ZERO));
    invalid.push(options("valid").idempotency_key("read-key"));
    let mut retry = RetryPolicy::default();
    retry.max_attempts = 0;
    invalid.push(options("valid").retry(retry));
    for options in invalid {
        let failure = client
            .get_with_options("invalid", options)
            .await
            .unwrap_err();
        assert!(matches!(
            failure.error,
            Error::Input(_) | Error::Configuration(_)
        ));
        assert!(failure.metadata.is_none());
    }
    for key in ["", "two words", "line\nbreak", "é"] {
        let failure = client
            .set_with_options(
                SetRequest::new("invalid", "fact"),
                options("valid").idempotency_key(key),
            )
            .await
            .unwrap_err();
        assert!(matches!(failure.error, Error::Input(_)));
        assert!(failure.metadata.is_none());
    }
    let CallFailure {
        error, metadata, ..
    } = client
        .set_with_options(SetRequest::new("invalid", Value::Null), options("valid"))
        .await
        .unwrap_err();
    assert!(matches!(error, Error::Input(_)));
    assert!(metadata.is_none());
    assert!(!client.get("first-sent").await.unwrap().found);
    assert_eq!(server.request().await.body["memory_key"], "first-sent");
}
