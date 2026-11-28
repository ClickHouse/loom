use super::*;
use clickhouse_loom::{CallFailure, CallOptions, RouteRequest, RouteResponse};

pub(super) fn unused_response() -> String {
    // A queued response keeps the fixture listening, so an extra request would arrive.
    response(200, "{}", "")
}

#[tokio::test]
async fn routes_receive_what_they_are_given_and_no_identity() {
    let server = Server::new(vec![
        response(200, json!({"subject": "s", "org": "o"}), ""),
        response(200, json!({"default_namespace": "eng"}), ""),
        response(200, json!({"revoked": true}), ""),
    ]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("test-token")
        .user_id("user")
        .session_id("session")
        .run_id("run")
        .build()
        .unwrap();
    let me: Value = client
        .request_json(RouteRequest::get("/v1/whoami"))
        .await
        .unwrap();
    assert_eq!(me, json!({"subject": "s", "org": "o"}));
    let chosen: Value = client
        .request_json(
            RouteRequest::post("/v1/me/set-default-namespace").json(&json!({"namespace": "eng"})),
        )
        .await
        .unwrap();
    assert_eq!(chosen["default_namespace"], "eng");
    let revoked: Value = client
        .request_json(RouteRequest::delete("/v1/me/service_accounts").json(&json!({"id": 7})))
        .await
        .unwrap();
    assert_eq!(revoked["revoked"], true);

    let request = server.request().await;
    assert_eq!(request.line, "GET /v1/whoami HTTP/1.1");
    assert_eq!(request.headers["authorization"], "Bearer test-token");
    assert_eq!(request.headers["x-request-id"].len(), 36);
    assert!(request.headers["x-loom-sdk"].starts_with("loom-rust/"));
    assert!(request.headers["x-loom-runtime"].starts_with("rust "));
    assert!(request.raw.is_empty());
    for absent in [
        "content-type",
        "idempotency-key",
        "x-loom-run-id",
        "x-loom-retry-count",
    ] {
        assert!(!request.headers.contains_key(absent), "{absent}");
    }
    let request = server.request().await;
    assert_eq!(request.line, "POST /v1/me/set-default-namespace HTTP/1.1");
    assert_eq!(request.headers["content-type"], "application/json");
    assert_eq!(request.raw, br#"{"namespace":"eng"}"#);
    let request = server.request().await;
    assert_eq!(request.line, "DELETE /v1/me/service_accounts HTTP/1.1");
    assert_eq!(request.body, json!({"id": 7}));
}

#[tokio::test]
async fn raw_bodies_and_query_parameters_reach_the_route() {
    let server = Server::new(vec![response(
        200,
        json!({"upload_id": "up", "sha256": "abc"}),
        "",
    )]);
    let client = server.client();
    let bytes = b"%PDF-1.7\n\x00\xff".to_vec();
    let stored: Value = client
        .request_json(
            RouteRequest::post("/v1/knowledge.upload")
                .query("org", "o rg")
                .query("filename", "guide notes.pdf")
                .query("sha256", "abc")
                .bytes("application/pdf", bytes.clone()),
        )
        .await
        .unwrap();
    assert_eq!(stored["upload_id"], "up");
    let request = server.request().await;
    assert_eq!(
        request.line,
        "POST /v1/knowledge.upload?org=o+rg&filename=guide+notes.pdf&sha256=abc HTTP/1.1"
    );
    assert_eq!(request.headers["content-type"], "application/pdf");
    assert_eq!(request.raw, bytes);
    assert!(request.body.is_null());
}

#[tokio::test]
async fn a_429_is_resent_after_the_hint_within_the_pacing_budget() {
    let refused = response(
        429,
        json!({"detail": {"code": "RATE_LIMITED", "message": "busy", "retry_after_s": 0.05}}),
        "X-Request-Id: echoed-429\r\n",
    );
    let server = Server::new(vec![
        refused.clone(),
        response(200, json!({"shares": 0}), ""),
        refused.clone(),
        refused,
        unused_response(),
    ]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .build()
        .unwrap();
    let route = || RouteRequest::get("/v1/me/sharing-summary");
    let result = client
        .request_json_with_options::<Value>(route(), CallOptions::default().request_id("route-1"))
        .await
        .unwrap();
    assert_eq!(result.data, json!({"shares": 0}));
    assert_eq!(result.metadata.attempts, 2);
    assert_eq!(result.metadata.request_id, "route-1");
    assert_eq!(server.request().await.headers["x-request-id"], "route-1");
    assert_eq!(server.request().await.headers["x-request-id"], "route-1");

    for builder in [
        Client::builder("org", "ns", "bot")
            .base_url(&server.url)
            .api_key("")
            .ratelimit_max_wait(Duration::ZERO),
        Client::builder("org", "ns", "bot")
            .base_url(&server.url)
            .api_key("")
            .pacing(false),
    ] {
        let failure = builder
            .build()
            .unwrap()
            .request_json_with_options::<Value>(route(), CallOptions::default())
            .await
            .unwrap_err();
        let Error::Api(error) = failure.error else {
            panic!("expected API error");
        };
        assert_eq!(error.status, 429);
        assert_eq!(error.code, "RATE_LIMITED");
        assert_eq!(error.retry_after_s, Some(0.05));
        assert_eq!(error.request_id.as_deref(), Some("echoed-429"));
        assert_eq!(failure.metadata.unwrap().attempts, 1);
        server.request().await;
        assert!(
            tokio::time::timeout(Duration::from_millis(300), server.request())
                .await
                .is_err()
        );
    }
}

#[tokio::test]
async fn a_429_with_no_wait_to_take_is_returned() {
    let refused = |wait| {
        response(
            429,
            json!({"detail": {"code": "RATE_LIMITED", "message": "busy", "retry_after_s": wait}}),
            "",
        )
    };
    let route = || RouteRequest::get("/v1/me/sharing-summary");
    let attempts_of = |failure: CallFailure| {
        let Error::Api(error) = failure.error else {
            panic!("expected API error");
        };
        assert_eq!(error.status, 429);
        failure.metadata.unwrap().attempts
    };

    // A hint of zero asks for no wait.
    let server = Server::new(vec![refused(json!(0)), unused_response()]);
    let failure = server
        .client()
        .request_json_with_options::<Value>(route(), CallOptions::default())
        .await
        .unwrap_err();
    assert_eq!(attempts_of(failure), 1);
    server.request().await;
    assert!(
        tokio::time::timeout(Duration::from_millis(300), server.request())
            .await
            .is_err()
    );

    // The last wait is cut to what is left of the budget, then the 429 stands.
    let server = Server::new(vec![
        refused(json!(0.05)),
        refused(json!(0.05)),
        refused(json!(0.05)),
        unused_response(),
    ]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .ratelimit_max_wait(Duration::from_millis(80))
        .build()
        .unwrap();
    let failure = client
        .request_json_with_options::<Value>(route(), CallOptions::default())
        .await
        .unwrap_err();
    assert_eq!(attempts_of(failure), 3);
    for _ in 0..3 {
        server.request().await;
    }
    assert!(
        tokio::time::timeout(Duration::from_millis(300), server.request())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn a_route_is_not_resent_when_the_connection_closes_without_an_answer() {
    let server = Server::new(vec!["close".into(), unused_response()]);
    let failure = server
        .client()
        .request_with_options(RouteRequest::get("/v1/whoami"), CallOptions::default())
        .await
        .unwrap_err();
    assert!(matches!(failure.error, Error::Transport(_)), "{failure:?}");
    assert_eq!(failure.metadata.unwrap().status, None);
    assert_eq!(server.request().await.line, "GET /v1/whoami HTTP/1.1");
    assert!(
        tokio::time::timeout(Duration::from_millis(300), server.request())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn route_errors_are_returned_without_a_retry() {
    for (status, body, code, message) in [
        (
            503,
            r#"{"detail":{"code":"OVERLOADED","message":"busy"}}"#,
            "OVERLOADED",
            "busy",
        ),
        (404, r#"{"detail":"Not Found"}"#, "HTTP_ERROR", "Not Found"),
        (
            422,
            r#"{"detail":[{"loc":["body","x"],"msg":"field required"}]}"#,
            "HTTP_ERROR",
            r#"{"detail":[{"loc":["body","x"],"msg":"field required"}]}"#,
        ),
    ] {
        let server = Server::new(vec![
            response(status, body, "X-Request-Id: echoed\r\n"),
            unused_response(),
        ]);
        let Error::Api(error) = server
            .client()
            .request_json::<Value>(RouteRequest::get("/v1/admin/revocations"))
            .await
            .unwrap_err()
        else {
            panic!("expected API error");
        };
        assert_eq!(error.status, status, "{body}");
        assert_eq!(error.code, code);
        assert_eq!(error.message, message);
        assert_eq!(error.request_id.as_deref(), Some("echoed"));
        assert_eq!(
            server.request().await.line,
            "GET /v1/admin/revocations HTTP/1.1"
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(300), server.request())
                .await
                .is_err()
        );
    }
}

#[tokio::test]
async fn a_redirect_is_a_response_and_the_base_path_is_preserved() {
    let server = Server::new(vec![response(
        302,
        "",
        "Location: http://127.0.0.1:1/elsewhere\r\n",
    )]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(format!("{}/proxy/loom", server.url))
        .api_key("")
        .build()
        .unwrap();
    let RouteResponse {
        status, headers, ..
    } = client
        .request(RouteRequest::get("/v1/whoami"))
        .await
        .unwrap();
    assert_eq!(status, 302);
    assert_eq!(
        headers["location"].to_str().unwrap(),
        "http://127.0.0.1:1/elsewhere"
    );
    assert_eq!(
        server.request().await.line,
        "GET /proxy/loom/v1/whoami HTTP/1.1"
    );
}

#[tokio::test]
async fn a_colon_in_the_first_segment_stays_in_the_path() {
    let server = Server::new(vec![unused_response()]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(format!("{}/proxy/loom", server.url))
        .api_key("")
        .build()
        .unwrap();
    client
        .request(RouteRequest::get("/status:ready?detail=1").query("format", "json"))
        .await
        .unwrap();
    assert_eq!(
        server.request().await.line,
        "GET /proxy/loom/status:ready?detail=1&format=json HTTP/1.1"
    );
}

#[tokio::test]
async fn a_path_must_stay_on_this_server() {
    let server = Server::new(vec![unused_response()]);
    let client = server.client();
    for path in [
        "v1/whoami?sample=query-marker#fragment-marker",
        // A tab or a backslash is dropped or read as a slash before the path is resolved.
        "/\t//127.0.0.1:1/whoami",
        "/\\\\127.0.0.1:1/whoami",
        "//127.0.0.1:1/whoami",
        "http://127.0.0.1:1/whoami",
    ] {
        let error = client.request(RouteRequest::get(path)).await.unwrap_err();
        assert!(
            matches!(&error, Error::Input(message) if message.contains("not a path on this server")),
            "{path}"
        );
        for text in [error.to_string(), format!("{error:?}")] {
            assert!(!text.contains("query-marker"));
            assert!(!text.contains("fragment-marker"));
        }
    }
    assert!(
        tokio::time::timeout(Duration::from_millis(300), server.request())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn an_empty_body_is_an_empty_object_and_an_invalid_one_is_a_protocol_error() {
    #[derive(serde::Deserialize, Debug, PartialEq)]
    struct Settings {
        #[serde(default)]
        namespace: String,
    }
    let server = Server::new(vec![
        response(204, "", ""),
        response(204, "", ""),
        response(200, "not json", ""),
        response(200, r#"{"text":"file"}"#, "Content-Type: text/plain\r\n"),
    ]);
    let client = server.client();
    assert_eq!(
        client
            .request_json::<Value>(RouteRequest::get("/v1/me/preferences"))
            .await
            .unwrap(),
        json!({})
    );
    assert_eq!(
        client
            .request_json::<Settings>(RouteRequest::get("/v1/me/preferences"))
            .await
            .unwrap(),
        Settings {
            namespace: String::new()
        }
    );
    // The response arrived; its attempt is the one to name.
    let failure = client
        .request_json_with_options::<Value>(
            RouteRequest::get("/v1/operations"),
            CallOptions::default().request_id("route-2"),
        )
        .await
        .unwrap_err();
    assert!(
        matches!(&failure.error, Error::Protocol { status: 200, .. }),
        "{failure:?}"
    );
    let metadata = failure.metadata.unwrap();
    assert_eq!(metadata.attempts, 1);
    assert_eq!(metadata.request_id, "route-2");
    let RouteResponse {
        status, headers, ..
    } = client
        .request(RouteRequest::get("/v1/knowledge.download"))
        .await
        .unwrap();
    assert_eq!(status, 200);
    assert_eq!(headers["content-type"], "text/plain");
}

#[tokio::test]
async fn route_options_reject_write_keys_and_a_body_that_cannot_be_encoded() {
    let server = Server::new(vec![unused_response()]);
    let client = server.client();
    let failure = client
        .request_with_options(
            RouteRequest::get("/v1/whoami"),
            CallOptions::default().idempotency_key("key"),
        )
        .await
        .unwrap_err();
    assert!(matches!(&failure.error, Error::Input(message) if message.contains("idempotency_key")));
    let unencodable = std::collections::HashMap::from([((1, 2), "value")]);
    let failure = client
        .request(RouteRequest::get("/v1/whoami").json(&unencodable))
        .await
        .unwrap_err();
    assert!(
        matches!(failure, Error::Input(ref message) if message.contains("cannot encode request body"))
    );
    assert!(
        tokio::time::timeout(Duration::from_millis(300), server.request())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn an_unreachable_route_keeps_the_status_and_headers_it_received() {
    let unreachable = Client::builder("org", "ns", "bot")
        .base_url("http://127.0.0.1:1")
        .api_key("")
        .build()
        .unwrap();
    let failure = unreachable
        .request_with_options(RouteRequest::get("/v1/whoami"), CallOptions::default())
        .await
        .unwrap_err();
    assert!(matches!(failure.error, Error::Transport(_)));
    assert_eq!(failure.metadata.unwrap().status, None);

    let server = Server::new(vec!["hold-body".into()]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .timeout(Duration::from_millis(50))
        .build()
        .unwrap();
    let failure = client
        .request_with_options(RouteRequest::get("/v1/whoami"), CallOptions::default())
        .await
        .unwrap_err();
    assert!(
        matches!(&failure.error, Error::Transport(error) if error.is_timeout()),
        "{failure:?}"
    );
    assert_eq!(failure.metadata.unwrap().status, Some(200));
    assert_eq!(server.request().await.line, "GET /v1/whoami HTTP/1.1");
}
