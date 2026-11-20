use super::*;

#[tokio::test]
async fn body_retry_hint_drives_status_retries_and_429_pacing() {
    init_logger();
    for status in [503, 429] {
        let server = Server::new(vec![
            response(status, json!({"detail":{"retry_after_s":0.001}}), ""),
            response(200, json!({"found":false}), ""),
        ]);
        let mut policy = immediate_policy();
        policy.max_attempts = if status == 429 { 1 } else { 2 };
        policy.total_backoff_budget = Some(Duration::from_millis(2));
        let client = Client::builder("org", "ns", "bot")
            .base_url(&server.url)
            .api_key("")
            .retry(policy)
            .ratelimit_max_wait(Duration::from_millis(2))
            .build()
            .unwrap();
        assert!(!client.get("key").await.unwrap().found);
        let first = server.request().await;
        let second = server.request().await;
        assert_eq!(
            first.headers["x-request-id"],
            second.headers["x-request-id"]
        );
        assert_eq!(second.headers["x-loom-retry-count"], "1");
        assert!(!second.headers.contains_key("idempotency-key"));
        assert!(LOGS.0.lock().unwrap().iter().any(|(_, level, text)| {
            *level == log::Level::Warn
                && text.contains(&first.headers["x-request-id"])
                && text.contains("wait_ms=1 ")
        }));
    }
}

#[tokio::test]
async fn a_capacity_reading_clears_the_wait_for_all_client_clones() {
    let server = Server::new(vec![
        response(
            200,
            json!({"found":false}),
            "X-RateLimit-Limit: 1\r\nX-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 1\r\n",
        ),
        response(
            200,
            json!({"found":false}),
            "X-RateLimit-Limit: 1\r\nX-RateLimit-Remaining: 1\r\nX-RateLimit-Reset: 1\r\n",
        ),
        response(200, json!({"found":false}), ""),
    ]);
    let client = server.client();
    let cloned = client.clone();
    client.get("exhausted").await.unwrap();
    server.request().await;
    client.get("refilled").await.unwrap();
    server.request().await;
    assert!(
        !tokio::time::timeout(Duration::from_millis(500), cloned.get("after-refill"))
            .await
            .expect("restored capacity must clear the one-second preflight wait")
            .unwrap()
            .found
    );
    assert_eq!(server.request().await.body["memory_key"], "after-refill");
}

fn immediate_policy() -> RetryPolicy {
    let mut policy = RetryPolicy::default();
    policy.backoff = Duration::ZERO;
    policy.backoff_jitter = 0.0;
    policy
}

#[tokio::test]
async fn retries_writes_with_one_key_and_counts_every_send() {
    let server = Server::new(vec![
        response(503, json!({"detail":{"retryable":false}}), ""),
        response(500, "busy", ""),
        response(200, json!({"version":1}), ""),
        response(200, json!({"version":2}), ""),
    ]);
    let client = server.client().with_retry(immediate_policy()).unwrap();
    client.set(SetRequest::new("key", "fact")).await.unwrap();
    let first = server.request().await;
    assert_eq!(first.headers["x-loom-retry-count"], "0");
    let key = &first.headers["idempotency-key"];
    assert_eq!(key.len(), 36);
    assert_eq!(&key[14..15], "4");
    for count in ["1", "2"] {
        let next = server.request().await;
        assert_eq!(&next.headers["idempotency-key"], key);
        assert_eq!(next.headers["x-request-id"], first.headers["x-request-id"]);
        assert_eq!(next.headers["x-loom-retry-count"], count);
        assert_eq!(next.body, first.body);
    }
    client.set(SetRequest::new("key", "fact")).await.unwrap();
    let second = server.request().await;
    assert_ne!(second.headers["idempotency-key"], *key);
    assert_ne!(
        second.headers["x-request-id"],
        first.headers["x-request-id"]
    );
    assert_eq!(second.headers["x-loom-sdk"], "loom-rust/0.1.0");
}

#[tokio::test]
async fn read_status_retry_does_not_add_an_idempotency_key() {
    let server = Server::new(vec![
        response(408, "", ""),
        response(200, json!({"found":false}), ""),
    ]);
    server
        .client()
        .with_retry(immediate_policy())
        .unwrap()
        .get("key")
        .await
        .unwrap();
    for _ in 0..2 {
        assert!(!server
            .request()
            .await
            .headers
            .contains_key("idempotency-key"));
    }
}

#[tokio::test]
async fn refusal_codes_configuration_and_status_selection_stop_retries() {
    for (status, detail) in [
        (503, json!({"code":"ANALYSIS_UNAVAILABLE"})),
        (503, json!({"code":"ASSISTANT_UNAVAILABLE"})),
        (503, json!({"code":"CODED_WIDTH_MISMATCH"})),
        (503, json!({"code":"GATEWAY_REQUIRED"})),
        (503, json!({"code":"MODEL_CALL_UNATTRIBUTED"})),
        (503, json!({"code":"PUBLICATION_PENDING"})),
        (503, json!({"code":"TENANCY_REFUSED"})),
        (500, json!({"category":"configuration"})),
        (401, json!({})),
        (403, json!({})),
        (409, json!({})),
    ] {
        let server = Server::new(vec![
            response(status, json!({"detail":detail}), ""),
            response(200, json!({"found":false}), ""),
        ]);
        let client = server.client().with_retry(immediate_policy()).unwrap();
        assert!(matches!(client.get("key").await, Err(Error::Api(e)) if e.status == status));
        server.request().await;
        client.get("probe").await.unwrap();
        assert_eq!(server.request().await.body["memory_key"], "probe");
    }
}

#[tokio::test]
async fn attempts_budget_and_override_are_independent() {
    let server = Server::new(vec![response(503, "", ""); 5]);
    let client = server.client().with_retry(immediate_policy()).unwrap();
    assert!(matches!(client.get("key").await, Err(Error::Api(_))));
    for count in ["0", "1", "2"] {
        assert_eq!(server.request().await.headers["x-loom-retry-count"], count);
    }
    assert!(matches!(
        client
            .with_retry(RetryPolicy::disabled())
            .unwrap()
            .get("single")
            .await,
        Err(Error::Api(_))
    ));
    assert_eq!(server.request().await.body["memory_key"], "single");
    let mut policy = RetryPolicy::default();
    policy.total_backoff_budget = Some(Duration::ZERO);
    assert!(matches!(
        client.with_retry(policy).unwrap().get("budget").await,
        Err(Error::Api(_))
    ));
    assert_eq!(server.request().await.body["memory_key"], "budget");
}

#[tokio::test]
async fn pacing_has_its_own_budget_and_resets_retry_attempts() {
    let server = Server::new(vec![
        response(503, "", ""),
        response(429, "", "Retry-After: 0.001\r\n"),
        response(503, "", ""),
        response(200, json!({"version":1}), ""),
    ]);
    let mut policy = immediate_policy();
    policy.max_attempts = 2;
    server
        .client()
        .with_retry(policy)
        .unwrap()
        .set(SetRequest::new("key", "fact"))
        .await
        .unwrap();
    let first = server.request().await;
    for count in ["1", "2", "3"] {
        let next = server.request().await;
        assert_eq!(next.headers["x-loom-retry-count"], count);
        assert_eq!(
            next.headers["idempotency-key"],
            first.headers["idempotency-key"]
        );
    }
}

#[tokio::test]
async fn pacing_zero_and_exhausted_budget_return_429() {
    for hint in ["0", "0.01"] {
        let server = Server::new(vec![
            response(429, "", &format!("Retry-After: {hint}\r\n"));
            2
        ]);
        let client = Client::builder("org", "ns", "bot")
            .base_url(&server.url)
            .api_key("")
            .ratelimit_max_wait(Duration::from_millis(1))
            .build()
            .unwrap();
        assert!(matches!(client.get("key").await, Err(Error::Api(e)) if e.status == 429));
        server.request().await;
        if hint != "0" {
            assert_eq!(server.request().await.headers["x-loom-retry-count"], "1");
        }
    }
}

#[tokio::test]
async fn response_cutoff_retries_only_replayable_calls_by_default() {
    let cutoff = "HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{".to_owned();
    let server = Server::new(vec![
        cutoff.clone(),
        response(200, json!({"version":1}), ""),
    ]);
    let client = server.client().with_retry(immediate_policy()).unwrap();
    client.set(SetRequest::new("key", "fact")).await.unwrap();
    let first = server.request().await;
    assert_eq!(
        server.request().await.headers["idempotency-key"],
        first.headers["idempotency-key"]
    );
    let server = Server::new(vec![
        cutoff.clone(),
        response(200, json!({"found":false}), ""),
    ]);
    assert!(matches!(
        server
            .client()
            .with_retry(immediate_policy())
            .unwrap()
            .get("key")
            .await,
        Err(Error::Transport(_))
    ));
    server.request().await;
    let mut always = immediate_policy();
    always.retry_timeouts = RetryTimeouts::Always;
    let server = Server::new(vec![cutoff, response(200, json!({"found":false}), "")]);
    server
        .client()
        .with_retry(always)
        .unwrap()
        .get("key")
        .await
        .unwrap();
    assert_eq!(server.request().await.headers["x-loom-retry-count"], "0");
    assert_eq!(server.request().await.headers["x-loom-retry-count"], "1");
}

#[tokio::test]
async fn cancelled_backoff_and_pacing_never_send_another_attempt() {
    init_logger();
    for status in [503, 429] {
        let server = Server::new(vec![
            response(status, "", "Retry-After: 1\r\n"),
            response(200, json!({"found":false}), ""),
        ]);
        let client = server.client();
        let cloned = client.clone();
        let call = tokio::spawn(async move { cloned.set(SetRequest::new("cancel", "fact")).await });
        let first = server.request().await;
        let rid = &first.headers["x-request-id"];
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let waiting = LOGS
                    .0
                    .lock()
                    .unwrap()
                    .iter()
                    .any(|(_, level, text)| *level == log::Level::Warn && text.contains(rid));
                if waiting {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("call must enter its retry or pacing sleep");
        call.abort();
        assert!(call.await.unwrap_err().is_cancelled());
        client.get("probe").await.unwrap();
        assert_eq!(server.request().await.body["memory_key"], "probe");
    }
}

#[tokio::test]
async fn timed_out_writes_reuse_their_key_when_retried() {
    for held in ["hold", "hold-body"] {
        let server = Server::new(vec![held.into(), response(200, json!({"version":1}), "")]);
        let mut policy = immediate_policy();
        policy.backoff = Duration::from_millis(300);
        let client = Client::builder("org", "ns", "bot")
            .base_url(&server.url)
            .api_key("")
            .timeout(Duration::from_millis(50))
            .retry(policy)
            .build()
            .unwrap();
        client.set(SetRequest::new("key", "fact")).await.unwrap();
        let first = server.request().await;
        let second = server.request().await;
        assert_eq!(
            first.headers["idempotency-key"],
            second.headers["idempotency-key"]
        );
        assert_eq!(second.headers["x-loom-retry-count"], "1");
    }
}

#[tokio::test]
async fn rate_state_is_shared_but_scoped_to_the_operation() {
    let server = Server::new(vec![
        response(
            200,
            json!({"found":false}),
            "X-RateLimit-Limit: 10\r\nX-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 10\r\n",
        ),
        response(200, json!({"tombstoned":false,"version":1}), ""),
        response(200, json!({"found":false}), ""),
    ]);
    let client = server.client();
    client.get("first").await.unwrap();
    server.request().await;
    let cloned = client.with_retry(RetryPolicy::disabled()).unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(30), cloned.get("paced"))
            .await
            .is_err()
    );
    client.delete("other-op").await.unwrap();
    assert_eq!(server.request().await.body["memory_key"], "other-op");
    assert!(
        tokio::time::timeout(Duration::from_millis(30), client.get("still-paced"))
            .await
            .is_err()
    );
}

#[test]
fn policy_configuration_is_validated_on_builder_and_override() {
    let client = Client::builder("org", "ns", "bot").build().unwrap();
    let mut invalid = RetryPolicy::default();
    invalid.max_attempts = 0;
    assert!(client.with_retry(invalid).is_err());
    for jitter in [f64::NAN, f64::INFINITY, -0.1, 1.1] {
        let mut invalid = RetryPolicy::default();
        invalid.backoff_jitter = jitter;
        assert!(Client::builder("org", "ns", "bot")
            .retry(invalid)
            .build()
            .is_err());
    }
    let mut invalid = RetryPolicy::default();
    invalid.statuses.insert(600);
    assert!(client.with_retry(invalid).is_err());
}

#[tokio::test]
async fn disabling_pacing_returns_a_429_even_when_statuses_include_it() {
    let server = Server::new(vec![
        response(429, "", "Retry-After: 1\r\n"),
        response(200, json!({"found":false}), ""),
    ]);
    let mut policy = immediate_policy();
    policy.statuses.insert(429);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .retry(policy)
        .pacing(false)
        .build()
        .unwrap();
    let Error::Api(error) = client.get("key").await.unwrap_err() else {
        panic!("API error")
    };
    let first = server.request().await;
    assert_eq!(
        error.request_id.as_deref(),
        Some(first.headers["x-request-id"].as_str())
    );
    client.get("probe").await.unwrap();
    assert_eq!(server.request().await.body["memory_key"], "probe");
}

#[tokio::test]
async fn writes_can_disable_incomplete_response_retries() {
    let cutoff = "HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{";
    let server = Server::new(vec![cutoff.into(), response(200, json!({"version":1}), "")]);
    let mut policy = immediate_policy();
    policy.retry_timeouts = RetryTimeouts::Never;
    let client = server.client().with_retry(policy).unwrap();
    assert!(matches!(
        client.set(SetRequest::new("key", "fact")).await,
        Err(Error::Transport(_))
    ));
    server.request().await;
    client.set(SetRequest::new("probe", "fact")).await.unwrap();
    assert_eq!(server.request().await.body["memory_key"], "probe");
}

#[tokio::test]
async fn a_connection_failure_retries_only_when_allowed() {
    init_logger();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    drop(listener);
    let client = Client::builder("org", "ns", "bot")
        .base_url(format!("http://{addr}"))
        .api_key("")
        .build()
        .unwrap();
    let mut disabled = immediate_policy();
    disabled.retry_connection_errors = false;
    assert!(
        matches!(client.with_retry(disabled).unwrap().get("key").await, Err(Error::Transport(e)) if e.is_connect())
    );
    assert!(
        matches!(client.with_retry(immediate_policy()).unwrap().get("key").await, Err(Error::Transport(e)) if e.is_connect())
    );
    let records = LOGS.0.lock().unwrap();
    let records: Vec<_> = records
        .iter()
        .filter(|(id, _, _)| *id == thread::current().id())
        .collect();
    assert!(records
        .iter()
        .any(|(_, level, text)| *level == log::Level::Error && text.contains("attempts=1")));
    assert!(records
        .iter()
        .any(|(_, level, text)| *level == log::Level::Error && text.contains("attempts=3")));
}

struct CaptureLog(std::sync::Mutex<Vec<(thread::ThreadId, log::Level, String)>>);
static LOGS: CaptureLog = CaptureLog(std::sync::Mutex::new(Vec::new()));

fn init_logger() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        log::set_logger(&LOGS).unwrap();
        log::set_max_level(log::LevelFilter::Debug);
    });
}

impl log::Log for CaptureLog {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        metadata.target() == "clickhouse_loom"
    }
    fn log(&self, record: &log::Record<'_>) {
        if self.enabled(record.metadata()) {
            self.0.lock().unwrap().push((
                thread::current().id(),
                record.level(),
                record.args().to_string(),
            ));
        }
    }
    fn flush(&self) {}
}

#[tokio::test]
async fn logs_record_attempts_without_credentials_payloads_or_error_text() {
    init_logger();
    let server = Server::new(vec![
        response(503, "log-private-server-message", ""),
        response(200, json!({"version":1}), ""),
        response(403, "log-private-server-message", ""),
    ]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("log-private-bearer")
        .retry(immediate_policy())
        .build()
        .unwrap();
    client
        .set(SetRequest::new("log-private-key", "log-private-content"))
        .await
        .unwrap();
    let first = server.request().await;
    server.request().await;
    let rid = &first.headers["x-request-id"];
    assert!(matches!(client.get("key").await, Err(Error::Api(_))));
    let records = LOGS.0.lock().unwrap();
    for (_, _, text) in records.iter() {
        assert!(!text.contains("log-private"));
        assert!(!text.contains(&server.url));
    }
    for level in [log::Level::Debug, log::Level::Warn, log::Level::Info] {
        assert!(records
            .iter()
            .any(|(_, l, text)| *l == level && text.contains(rid)));
    }
    assert!(records
        .iter()
        .any(|(_, l, text)| *l == log::Level::Error && text.contains("status=403")));
}
