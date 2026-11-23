use super::*;
use clickhouse_loom::{CallOptions, ProbeRequest};

fn capability(name: &str, fields: &[&str]) -> Value {
    json!({
        "name": name,
        "path": format!("/v1/{name}"),
        "mcp_tool": format!("loom_{}", name.replace('.', "_")),
        "mcp_tier": "core",
        "required_role": "reader",
        "budget_p99_ms": 100,
        "idempotent": false,
        "audit": false,
        "request_fields": fields,
        "response_fields": ["response_only"]
    })
}

fn capabilities() -> Value {
    json!({
        "build_commit": "build",
        "schema_version": "schema",
        "ops": [
            capability("future.z", &[]),
            capability("memory.set", &["value"]),
            capability("future.a", &[]),
            capability("future.z", &[]),
            capability("memory.set", &["value", "tags"])
        ]
    })
}

fn needs() -> ProbeRequest {
    ProbeRequest::new()
        .ops(["memory.ask", "memory.set", "memory.ask", "memory.search"])
        .fields(
            "memory.set",
            ["tags", "response_only", "missing", "missing"],
        )
        .fields("memory.ask", ["question", "question"])
        .fields("memory.search", Vec::<String>::new())
}

fn check_report(report: &clickhouse_loom::ProbeReport) {
    assert!(!report.ok);
    assert_eq!(report.build_commit, "build");
    assert_eq!(report.schema_version, "schema");
    assert_eq!(
        report.missing_ops,
        ["memory.ask", "memory.ask", "memory.search"]
    );
    assert_eq!(
        report.missing_fields["memory.set"],
        ["response_only", "missing", "missing"]
    );
    assert_eq!(
        report.missing_fields["memory.ask"],
        ["question", "question"]
    );
    assert_eq!(report.missing_fields.len(), 2);
    assert_eq!(report.unknown_to_sdk, ["future.a", "future.z"]);
}

#[tokio::test]
async fn missing_capabilities_are_reported_with_options_and_metadata() {
    let server = Server::new(vec![response(
        200,
        capabilities(),
        "X-Request-Id: echoed\r\nX-Schema-Version: header-schema\r\n",
    )]);
    let result = server
        .client()
        .probe_with_options(needs(), CallOptions::default().request_id("probe-id"))
        .await
        .unwrap();
    check_report(&result.data);
    assert_eq!(result.metadata.sent_request_id, "probe-id");
    assert_eq!(result.metadata.request_id, "echoed");
    assert_eq!(
        result.metadata.schema_version.as_deref(),
        Some("header-schema")
    );
    assert_eq!(result.metadata.status, Some(200));
    assert_eq!(result.metadata.attempts, 1);
    let request = server.request().await;
    assert_eq!(request.line, "POST /v1/ops.capabilities HTTP/1.1");
    assert_eq!(request.headers["x-request-id"], "probe-id");
    assert_eq!(request.body, json!({"org": "org"}));
}

#[tokio::test]
async fn each_probe_fetches_fresh_capabilities() {
    let server = Server::new(vec![
        response(200, capabilities(), ""),
        response(200, json!({}), ""),
        response(200, capabilities(), ""),
    ]);
    let client = server.client();
    let first = client
        .probe(
            ProbeRequest::new()
                .ops(["memory.set"])
                .fields("memory.set", ["tags"]),
        )
        .await
        .unwrap();
    assert!(first.ok);
    assert_eq!(first.unknown_to_sdk, ["future.a", "future.z"]);
    let second = client
        .probe(ProbeRequest::new().ops(["memory.set"]))
        .await
        .unwrap();
    assert!(!second.ok);
    assert_eq!(second.missing_ops, ["memory.set"]);
    assert!(second.unknown_to_sdk.is_empty());
    let empty = client.probe(ProbeRequest::default()).await.unwrap();
    assert!(empty.ok);
    for _ in 0..3 {
        assert_eq!(
            server.request().await.line,
            "POST /v1/ops.capabilities HTTP/1.1"
        );
    }
}

#[tokio::test]
async fn fields_alone_can_make_a_probe_unsatisfied() {
    let server = Server::new(vec![response(200, json!({}), "")]);
    let report = server
        .client()
        .probe(ProbeRequest::new().fields("memory.set", ["value"]))
        .await
        .unwrap();
    assert!(!report.ok);
    assert!(report.missing_ops.is_empty());
    assert_eq!(report.missing_fields["memory.set"], ["value"]);
}

#[tokio::test]
async fn http_and_decode_errors_propagate_instead_of_becoming_mismatches() {
    let server = Server::new(vec![
        response(403, json!({"detail": "denied"}), "X-Request-Id: denied\r\n"),
        response(200, json!({"ops": [{"name": "memory.set"}]}), ""),
    ]);
    let client = server.client();
    let failure = client
        .probe_with_options(ProbeRequest::new(), CallOptions::default())
        .await
        .unwrap_err();
    assert!(matches!(failure.error, Error::Api(ref error) if error.status == 403));
    assert_eq!(failure.metadata.as_ref().unwrap().request_id, "denied");
    assert!(matches!(
        client.probe(ProbeRequest::new()).await.unwrap_err(),
        Error::Protocol { status: 200, .. }
    ));
}

#[tokio::test]
async fn probe_retries_follow_per_call_options() {
    let server = Server::new(vec![
        response(503, json!({"detail": "busy"}), ""),
        response(200, capabilities(), ""),
    ]);
    let mut retry = RetryPolicy::default();
    retry.max_attempts = 2;
    retry.backoff = Duration::ZERO;
    retry.backoff_jitter = 0.0;
    let result = server
        .client()
        .probe_with_options(needs(), CallOptions::default().retry(retry))
        .await
        .unwrap();
    check_report(&result.data);
    assert_eq!(result.metadata.attempts, 2);
    let first = server.request().await;
    let second = server.request().await;
    assert_eq!(first.body, second.body);
    assert_eq!(
        first.headers["x-request-id"],
        second.headers["x-request-id"]
    );
}

#[cfg(feature = "blocking")]
#[test]
fn blocking_probe_matches_async_and_retains_metadata() {
    let server = Server::new(vec![
        response(200, capabilities(), "X-Request-Id: echoed\r\n"),
        response(200, json!({}), ""),
        response(403, json!({"detail": "denied"}), ""),
    ]);
    let client = clickhouse_loom::blocking::Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .build()
        .unwrap();
    let result = client
        .probe_with_options(needs(), CallOptions::default().request_id("blocking-probe"))
        .unwrap();
    check_report(&result.data);
    assert_eq!(result.metadata.sent_request_id, "blocking-probe");
    assert_eq!(result.metadata.request_id, "echoed");
    let empty = client.probe(ProbeRequest::new()).unwrap();
    assert!(empty.ok);
    assert!(empty.unknown_to_sdk.is_empty());
    assert!(matches!(
        client.probe(ProbeRequest::new()).unwrap_err(),
        Error::Api(ref error) if error.status == 403
    ));
}

#[tokio::test]
async fn probe_connection_failures_and_invalid_options_keep_their_error_types() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    drop(listener);
    let client = Client::builder("org", "ns", "bot")
        .base_url(url)
        .api_key("")
        .retry(RetryPolicy::disabled())
        .build()
        .unwrap();
    let failure = client
        .probe_with_options(ProbeRequest::new(), CallOptions::default())
        .await
        .unwrap_err();
    assert!(matches!(failure.error, Error::Transport(_)));
    let metadata = failure.metadata.unwrap();
    assert_eq!(metadata.attempts, 1);
    assert_eq!(metadata.status, None);
    let invalid = client
        .probe_with_options(
            ProbeRequest::new(),
            CallOptions::default().timeout(Duration::ZERO),
        )
        .await
        .unwrap_err();
    assert!(matches!(invalid.error, Error::Input(_)));
    assert!(invalid.metadata.is_none());
}

#[cfg(feature = "blocking")]
#[tokio::test]
async fn blocking_probe_refuses_calls_inside_tokio() {
    let client = clickhouse_loom::blocking::Client::builder("org", "ns", "bot")
        .base_url("http://127.0.0.1:1")
        .api_key("")
        .build()
        .unwrap();
    assert!(matches!(
        client.probe(ProbeRequest::new()).unwrap_err(),
        Error::Configuration(_)
    ));
    let failure = client
        .probe_with_options(ProbeRequest::new(), CallOptions::default())
        .unwrap_err();
    assert!(matches!(failure.error, Error::Configuration(_)));
    assert!(failure.metadata.is_none());
}
