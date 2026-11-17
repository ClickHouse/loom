use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

use clickhouse_loom::{Client, Error, RetryPolicy, RetryTimeouts, SearchRequest, SetRequest};
use serde_json::{json, Value};
use tokio::{
    process::Command,
    sync::{mpsc, Mutex},
};

const VERSION: u64 = 1_790_000_000_000_000_123;

#[path = "network_cases/mod.rs"]
mod network;

struct Request {
    line: String,
    headers: HashMap<String, String>,
    body: Value,
}

struct Server {
    url: String,
    requests: Mutex<mpsc::UnboundedReceiver<Request>>,
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl Server {
    fn new(responses: Vec<String>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let (sender, requests) = mpsc::unbounded_channel();
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let worker = thread::spawn(move || {
            let mut paired: Option<TcpStream> = None;
            for response in responses {
                let deadline = Instant::now() + Duration::from_secs(5);
                let mut stream = loop {
                    if stopped.load(Ordering::Relaxed) || Instant::now() >= deadline {
                        return;
                    }
                    match listener.accept() {
                        Ok((stream, _)) => break stream,
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::park_timeout(Duration::from_millis(5));
                        }
                        Err(e) => panic!("accept: {e}"),
                    }
                };
                // Accepted sockets inherit nonblocking mode on some platforms.
                stream.set_nonblocking(false).unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                stream
                    .set_write_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                sender.send(read_request(&mut stream)).unwrap();
                if response == "hold" {
                    thread::park_timeout(Duration::from_millis(250));
                } else if response == "hold-body" {
                    stream
                        .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
                        .unwrap();
                    thread::park_timeout(Duration::from_millis(250));
                } else if response == "pair" {
                    // Neither request completes until both connections arrive.
                    paired = Some(stream);
                } else {
                    stream.write_all(response.as_bytes()).unwrap();
                    if let Some(mut first) = paired.take() {
                        first.write_all(response.as_bytes()).unwrap();
                    }
                }
            }
        });
        Self {
            url,
            requests: Mutex::new(requests),
            stop,
            worker: Some(worker),
        }
    }

    fn client(&self) -> Client {
        Client::builder("org", "ns", "bot")
            .base_url(&self.url)
            .api_key("")
            .build()
            .unwrap()
    }

    async fn request(&self) -> Request {
        tokio::time::timeout(Duration::from_secs(5), self.requests.lock().await.recv())
            .await
            .unwrap()
            .unwrap()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(worker) = self.worker.take() {
            worker.thread().unpark();
            let _ = worker.join();
        }
    }
}

fn read_request(stream: &mut TcpStream) -> Request {
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    let mut headers = HashMap::new();
    loop {
        let mut header = String::new();
        reader.read_line(&mut header).unwrap();
        if header == "\r\n" {
            break;
        }
        let (key, value) = header.split_once(':').unwrap();
        headers.insert(key.to_ascii_lowercase(), value.trim().to_owned());
    }
    let len: usize = headers["content-length"].parse().unwrap();
    let mut body = vec![0; len];
    reader.read_exact(&mut body).unwrap();
    Request {
        line: line.trim().into(),
        headers,
        body: serde_json::from_slice(&body).unwrap(),
    }
}

fn response(status: u16, body: impl ToString, headers: &str) -> String {
    let body = body.to_string();
    format!(
        "HTTP/1.1 {status} Test\r\nContent-Length: {}\r\nConnection: close\r\n{headers}\r\n{body}",
        body.len()
    )
}

#[tokio::test]
async fn four_operations_use_post_json_and_bound_identity() {
    let server = Server::new(vec![
        response(200, json!({"version":VERSION,"memory_id":"row"}), ""),
        response(
            200,
            json!({"found":true,"version":VERSION,"value":{"count":VERSION},"future_field":42}),
            "",
        ),
        response(
            200,
            json!({"results":[{"memory_key":"key","memory_type":"semantic",
                "content_excerpt":"The user prefers short answers.",
                "distance":0.1,"similarity":0.9,"version":VERSION}]}),
            "",
        ),
        response(200, json!({"tombstoned":true,"version":VERSION}), ""),
    ]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("test-token")
        .user_id("user")
        .session_id("session")
        .build()
        .unwrap();
    let mut set = SetRequest::new("key", json!({"count": VERSION}));
    set.confidence = Some(0.0);
    set.ttl_seconds = Some(0);
    set.tags = Some(vec![]);
    set.if_match_sha256 = Some("hash".into());
    let written = client.set(set).await.unwrap();
    assert_eq!(written.version, VERSION);
    assert_eq!(written.memory_id, "row");
    let memory = client.get("key").await.unwrap();
    assert!(memory.found);
    assert_eq!(memory.version, Some(VERSION));
    assert_eq!(memory.value["count"].as_u64(), Some(VERSION));
    let mut search = SearchRequest::new("preferences");
    search.top_k = Some(3);
    let hits = client.search(search).await.unwrap();
    assert_eq!(hits.results[0].similarity, 0.9);
    assert_eq!(hits.results[0].version, Some(VERSION));
    assert!(client.delete("key").await.unwrap().tombstoned);

    for op in ["set", "get", "search", "delete"] {
        let request = server.request().await;
        assert_eq!(request.line, format!("POST /v1/memory.{op} HTTP/1.1"));
        assert_eq!(request.headers["authorization"], "Bearer test-token");
        assert_eq!(request.headers["content-type"], "application/json");
        assert_eq!(request.headers["x-loom-retry-count"], "0");
        assert_eq!(request.headers["x-request-id"].len(), 36);
        assert_eq!(
            request.headers.contains_key("idempotency-key"),
            matches!(op, "set" | "delete")
        );
        assert_eq!(request.body["org"], "org");
        assert_eq!(request.body["namespace"], "ns");
        if op == "search" {
            assert_eq!(request.body["agent"], "*");
            assert!(request.body.get("user_id").is_none());
            assert!(request.body.get("session_id").is_none());
        } else {
            assert_eq!(request.body["agent"], "bot");
            assert_eq!(request.body["user_id"], "user");
            assert_eq!(request.body["session_id"], "session");
        }
        if op == "set" {
            assert_eq!(request.body["confidence"], 0.0);
            assert_eq!(request.body["ttl_seconds"], 0);
            assert_eq!(request.body["tags"], json!([]));
            assert_eq!(request.body["if_match_sha256"], "hash");
            assert!(request.body.get("memory_type").is_none());
        }
        if op == "search" {
            assert!(request.body.get("namespace_scope").is_none());
            assert_eq!(request.body["top_k"], 3);
        }
    }
}

#[tokio::test]
async fn explicit_search_filters_override_authorship_without_replacing_scope() {
    let server = Server::new(vec![response(200, json!({"results": []}), "")]);
    let client = Client::builder("org", "ns", "writer")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer-user")
        .session_id("writer-session")
        .build()
        .unwrap();
    let mut search = SearchRequest::new("query");
    search.agent_id = Some("reader".into());
    search.namespace_scope = Some("all_mine".into());
    search.user_id = Some("".into());
    search.session_id = Some("conversation".into());
    client.search(search).await.unwrap();
    assert_eq!(
        server.request().await.body,
        json!({
            "org": "org", "namespace": "ns", "agent": "reader", "query": "query",
            "namespace_scope": "all_mine", "user_id": "", "session_id": "conversation"
        })
    );
}

#[tokio::test]
async fn missing_memories_and_empty_search_are_successful() {
    let server = Server::new(vec![
        response(200, json!({"found":false}), ""),
        response(200, json!({"results":[]}), ""),
        response(
            200,
            json!({"found":false,"tombstoned":false,"version":VERSION}),
            "",
        ),
    ]);
    let client = server.client();
    assert!(!client.get("absent").await.unwrap().found);
    assert!(client
        .search(SearchRequest::new("absent"))
        .await
        .unwrap()
        .results
        .is_empty());
    let deleted = client.delete("absent").await.unwrap();
    assert!(!deleted.found);
    assert!(!deleted.tombstoned);
    let request = server.request().await;
    assert!(!request.headers.contains_key("authorization"));
    assert!(request.body.get("user_id").is_none());
    assert!(request.body.get("session_id").is_none());
}

#[tokio::test]
async fn values_preserve_all_supported_json_shapes() {
    let values = [
        json!(false),
        json!(0),
        json!(""),
        json!([]),
        json!({}),
        json!(1.5),
    ];
    let server = Server::new(
        values
            .iter()
            .map(|_| response(200, json!({"version":VERSION}), ""))
            .collect(),
    );
    let client = server.client();
    for value in values {
        client
            .set(SetRequest::new("key", value.clone()))
            .await
            .unwrap();
        assert_eq!(server.request().await.body["value"], value);
    }
    assert!(matches!(
        client.set(SetRequest::new("key", Value::Null)).await,
        Err(Error::Input(_))
    ));
}

#[tokio::test]
async fn a_merge_keeps_its_noop_and_canonical_key() {
    let server = Server::new(vec![response(
        200,
        json!({"version":0,"noop":true,"merged_into_key":"canonical"}),
        "",
    )]);
    let result = server
        .client()
        .set(SetRequest::new("key", "fact"))
        .await
        .unwrap();
    assert!(result.noop);
    assert_eq!(result.merged_into_key, "canonical");
}

#[tokio::test]
async fn api_errors_preserve_categories_and_retry_hints() {
    for category in [
        "input",
        "auth",
        "capacity",
        "dependency",
        "configuration",
        "internal",
        "future",
    ] {
        let server = Server::new(vec![response(
            429,
            json!({"detail":{"code":"RATE_LIMITED","category":category,
                "retryable":false,"message":"busy","retry_after_s":2.5,"custom":42}}),
            "Retry-After: 8\r\nX-Request-Id: request-1\r\n",
        )]);
        let client = Client::builder("org", "ns", "bot")
            .base_url(&server.url)
            .api_key("")
            .pacing(false)
            .build()
            .unwrap();
        let Error::Api(error) = client.get("key").await.unwrap_err() else {
            panic!("expected API error");
        };
        assert_eq!(error.status, 429);
        assert_eq!(error.code, "RATE_LIMITED");
        assert_eq!(error.category.as_deref(), Some(category));
        assert_eq!(error.retryable, Some(false));
        assert_eq!(error.retry_after_s, Some(8.0));
        assert_eq!(error.retry_after.as_deref(), Some("8"));
        assert_eq!(error.request_id.as_deref(), Some("request-1"));
        assert_eq!(error.detail["custom"], 42);
    }
}

#[tokio::test]
async fn nonstandard_http_errors_keep_the_status() {
    for body in [
        "",
        "proxy unavailable",
        r#"{"detail":"denied"}"#,
        r#"{"detail":[{"type":"missing"}]}"#,
    ] {
        let server = Server::new(vec![response(503, body, "Retry-After: 3\r\n")]);
        let Error::Api(error) = server
            .client()
            .with_retry(RetryPolicy::disabled())
            .unwrap()
            .get("key")
            .await
            .unwrap_err()
        else {
            panic!("expected API error");
        };
        assert_eq!(error.status, 503);
        assert_eq!(error.code, "HTTP_ERROR");
        assert_eq!(error.category, None);
        assert_eq!(error.retryable, None);
        assert_eq!(error.retry_after_s, Some(3.0));
    }
}

#[tokio::test]
async fn unknown_error_codes_and_date_retry_headers_survive() {
    let server = Server::new(vec![response(
        409,
        json!({"detail":{"code":"FUTURE_CONFLICT","message":"conflict"}}),
        "Retry-After: Thu, 01 Jan 1970 00:00:00 GMT\r\n",
    )]);
    let Error::Api(error) = server.client().get("key").await.unwrap_err() else {
        panic!("expected API error");
    };
    assert_eq!(error.code, "FUTURE_CONFLICT");
    assert_eq!(error.retry_after_s, Some(0.0));
    assert!(error.retry_after.unwrap().contains("GMT"));
}

#[tokio::test]
async fn successful_but_invalid_payloads_are_protocol_errors() {
    for body in ["not json", "[]", "{}"] {
        let server = Server::new(vec![response(200, body, "")]);
        assert!(matches!(
            server.client().get("key").await,
            Err(Error::Protocol { status: 200, .. })
        ));
    }
}

#[tokio::test]
async fn base_path_is_preserved_and_redirects_are_not_followed() {
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
    assert!(matches!(client.get("key").await, Err(Error::Api(error)) if error.status == 302));
    assert_eq!(
        server.request().await.line,
        "POST /proxy/loom/v1/memory.get HTTP/1.1"
    );
}

#[test]
fn invalid_configuration_and_debug_never_expose_credentials() {
    for endpoint in [
        "not a url",
        "file:///etc/passwd",
        "https://user:secret@localhost",
        "http://localhost?q=secret",
        "http://localhost#secret",
    ] {
        let builder = Client::builder("org", "ns", "bot")
            .base_url(endpoint)
            .api_key("secret");
        assert!(!format!("{builder:?}").contains("secret"));
        let error = builder.build().unwrap_err();
        assert!(matches!(error, Error::Configuration(_)));
        assert!(!format!("{error:?} {error}").contains("secret"));
    }
    assert!(Client::builder("", "ns", "bot").build().is_err());
    assert!(Client::builder("org", "ns", "bot")
        .timeout(Duration::ZERO)
        .build()
        .is_err());
    assert!(Client::builder("org", "ns", "bot")
        .api_key("secret\ninvalid")
        .build()
        .is_err());
    let client = Client::builder("org", "ns", "bot")
        .base_url("http://localhost:1")
        .api_key("secret")
        .build()
        .unwrap();
    assert!(!format!("{client:?}").contains("secret"));
}

#[tokio::test]
async fn timeout_is_a_transport_error_without_an_automatic_retry() {
    for held in ["hold", "hold-body"] {
        let server = Server::new(vec![held.into()]);
        let client = Client::builder("org", "ns", "bot")
            .base_url(&server.url)
            .api_key("")
            .timeout(Duration::from_millis(50))
            .build()
            .unwrap();
        assert!(
            matches!(client.get("key").await, Err(Error::Transport(error)) if error.is_timeout())
        );
        assert_eq!(server.request().await.body["memory_key"], "key");
    }
}

#[tokio::test]
async fn environment_configuration_is_tested_in_a_private_process() {
    let server = Server::new(vec![response(200, json!({"found":false}), "")]);
    let explicit = Server::new(vec![response(200, json!({"found":false}), "")]);
    let result = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "environment_child"])
        .env("LOOM_RUST_ENV_TEST", "1")
        .env("LOOM_BASE_URL", &server.url)
        .env("LOOM_API_KEY", "environment-token")
        .env("LOOM_RUST_EXPLICIT_URL", &explicit.url)
        .output()
        .await
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stdout)
    );
    assert_eq!(
        server.request().await.headers["authorization"],
        "Bearer environment-token"
    );
    assert!(!explicit
        .request()
        .await
        .headers
        .contains_key("authorization"));
}

#[tokio::test]
async fn environment_child() {
    if std::env::var("LOOM_RUST_ENV_TEST").as_deref() != Ok("1") {
        return;
    }
    let client = Client::builder("org", "ns", "bot").build().unwrap();
    assert!(!client.get("key").await.unwrap().found);
    let explicit = Client::builder("org", "ns", "bot")
        .base_url(std::env::var("LOOM_RUST_EXPLICIT_URL").unwrap())
        .api_key("")
        .build()
        .unwrap();
    assert!(!explicit.get("key").await.unwrap().found);
}

#[tokio::test]
async fn cloned_clients_issue_concurrent_requests() {
    fn assert_send_sync<T: Send + Sync>() {}
    assert_send_sync::<Client>();
    let server = Server::new(vec![
        "pair".into(),
        response(200, json!({"found":false}), ""),
    ]);
    let client = server.client();
    let cloned = client.clone();
    let first = tokio::spawn(async move { client.get("first").await });
    let second = tokio::spawn(async move { cloned.get("second").await });
    let results = tokio::time::timeout(Duration::from_secs(2), async {
        (
            first.await.unwrap().unwrap(),
            second.await.unwrap().unwrap(),
        )
    })
    .await
    .expect("both requests must arrive before either response is sent");
    assert!(!results.0.found && !results.1.found);
    let mut keys = vec![
        server.request().await.body["memory_key"]
            .as_str()
            .unwrap()
            .to_owned(),
        server.request().await.body["memory_key"]
            .as_str()
            .unwrap()
            .to_owned(),
    ];
    keys.sort();
    assert_eq!(keys, ["first", "second"]);
}

#[tokio::test]
async fn cancelling_a_write_does_not_resend_it() {
    let server = Server::new(vec![
        "hold".into(),
        response(200, json!({"found":false}), ""),
        response(200, json!({"found":false}), ""),
    ]);
    let client = server.client();
    let cloned = client.clone();
    let task = tokio::spawn(async move { cloned.set(SetRequest::new("cancelled", "fact")).await });
    assert_eq!(server.request().await.body["memory_key"], "cancelled");
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert!(!client.get("probe").await.unwrap().found);
    assert_eq!(server.request().await.body["memory_key"], "probe");
    assert!(
        tokio::time::timeout(Duration::from_millis(300), server.request())
            .await
            .is_err()
    );
}
