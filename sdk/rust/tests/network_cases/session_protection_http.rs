//! SDK wire qualification against owned HTTP fixtures, without a Loom backend.

#![cfg(unix)]

use super::{read_request, response, Request, Server};
use clickhouse_loom::{Client, Error, RetryPolicy, SessionProtection};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    net::{Shutdown, TcpListener, TcpStream},
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex as StdMutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tokio::sync::oneshot;

const SOURCE_READ: &str = "knowledge.source.mcp_read";
const PRIVATE_VALUE: &str = "synthetic-private-source-value";

struct Roots(PathBuf);

impl Roots {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("loom-sdk-wire-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }

    fn family(&self, name: &str) -> SessionProtection {
        SessionProtection::create(self.0.join(name)).unwrap()
    }
}

impl Drop for Roots {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

fn client(url: &str, family: Option<&SessionProtection>, namespace: &str, token: &str) -> Client {
    let mut builder = Client::builder("org", namespace, "bot")
        .base_url(url)
        .api_key(token)
        .timeout(Duration::from_secs(3))
        .pacing(false)
        .retry(RetryPolicy {
            max_attempts: 1,
            ..RetryPolicy::default()
        });
    if let Some(family) = family {
        builder = builder.session_protection(family.clone());
    }
    builder.build().unwrap()
}

fn source_body() -> Value {
    json!({"name":"synthetic-source", "resource_id":"synthetic-resource"})
}

fn write_body() -> Value {
    json!({"memory_key":"wire-key", "value":"explicit-value"})
}

fn private_response() -> String {
    response(
        200,
        json!({"result":{"mirrored":true,"value":PRIVATE_VALUE}}),
        "Content-Type: application/json\r\n",
    )
}

fn assert_wire(request: &Request, op: &str, namespace: &str, token: &str, bound: bool) {
    assert_eq!(request.line, format!("POST /v1/{op} HTTP/1.1"));
    assert_eq!(request.headers["authorization"], format!("Bearer {token}"));
    assert_eq!(request.headers["content-type"], "application/json");
    if bound {
        assert_eq!(request.headers["x-loom-session-protection"], "v1");
    } else {
        assert!(!request.headers.contains_key("x-loom-session-protection"));
    }
    let mut expected = if op == SOURCE_READ {
        source_body()
    } else {
        write_body()
    };
    expected["org"] = json!("org");
    expected["namespace"] = json!(namespace);
    expected["agent"] = json!("bot");
    assert_eq!(request.body, expected);
}

fn suppressed(error: Error) {
    assert!(
        matches!(error, Error::Input(ref message)
            if message == "automatic capture is suppressed for this session"),
        "{error:?}"
    );
}

async fn no_request(server: &Server) {
    assert!(matches!(
        server.requests.lock().await.try_recv(),
        Err(tokio::sync::mpsc::error::TryRecvError::Empty)
            | Err(tokio::sync::mpsc::error::TryRecvError::Disconnected)
    ));
}

#[tokio::test]
async fn source_read_reopens_proof_and_blocks_clean_capture_on_other_tokens_and_clones() {
    let roots = Roots::new();
    let bound = roots.family("bound");
    let clean = roots.family("clean");
    let capture = clean.capture();
    let server = Server::new(vec![
        response(
            200,
            json!({"status":"ok"}),
            "Content-Type: application/json\r\n",
        ),
        private_response(),
        response(
            200,
            json!({"status":"ok"}),
            "Content-Type: application/json\r\n",
        ),
        response(
            200,
            json!({"status":"ok"}),
            "Content-Type: application/json\r\n",
        ),
    ]);
    let reader = client(&server.url, Some(&bound), "ns", "synthetic-token");
    let answer: Value = reader
        .call_automatic(&capture, "memory.set", &write_body())
        .await
        .unwrap();
    assert_eq!(answer["status"], "ok");
    assert_wire(
        &server.request().await,
        "memory.set",
        "ns",
        "synthetic-token",
        true,
    );
    let answer: Value = reader.call(SOURCE_READ, &source_body()).await.unwrap();
    assert_eq!(answer["result"]["value"], PRIVATE_VALUE);
    assert!(!clean.protected().unwrap());
    assert!(!pending(&clean));
    assert_wire(
        &server.request().await,
        SOURCE_READ,
        "ns",
        "synthetic-token",
        true,
    );
    let reopened = SessionProtection::open(bound.pointer()).unwrap();
    assert!(reopened.protected().unwrap());
    assert!(!pending(&bound));
    for writer in [
        reader.clone(),
        reader.with_retry(RetryPolicy::default()).unwrap(),
        client(&server.url, Some(&reopened), "other", "other-token"),
    ] {
        suppressed(
            writer
                .call_automatic::<Value>(&capture, "memory.set", &write_body())
                .await
                .unwrap_err(),
        );
        no_request(&server).await;
    }
    let answer: Value = reader.call("memory.set", &write_body()).await.unwrap();
    assert_eq!(answer["status"], "ok");
    assert_wire(
        &server.request().await,
        "memory.set",
        "ns",
        "synthetic-token",
        true,
    );
    let ordinary = client(&server.url, None, "ordinary", "ordinary-token");
    let answer: Value = ordinary.call("memory.set", &write_body()).await.unwrap();
    assert_eq!(answer["status"], "ok");
    assert_wire(
        &server.request().await,
        "memory.set",
        "ordinary",
        "ordinary-token",
        false,
    );
}

#[tokio::test]
async fn error_metadata_is_durable_before_api_error_and_typed_decode_error() {
    for status in [403, 200] {
        let roots = Roots::new();
        let family = roots.family("bound");
        let server = Server::new(vec![response(
            status,
            json!({"detail":{"code":"FORBIDDEN","message":"denied"},
                "error":{"data":{"mirrored":true,"value":PRIVATE_VALUE}}}),
            "Content-Type: application/json\r\n",
        )]);
        let reader = client(&server.url, Some(&family), "ns", "synthetic-token");
        let error = reader
            .call::<u64>(SOURCE_READ, &source_body())
            .await
            .unwrap_err();
        if status == 403 {
            assert!(matches!(error, Error::Api(ref api) if api.status == 403));
        } else {
            assert!(matches!(error, Error::Protocol { status: 200, .. }));
        }
        assert!(SessionProtection::open(family.pointer())
            .unwrap()
            .protected()
            .unwrap());
        assert!(!pending(&family));
        assert_wire(
            &server.request().await,
            SOURCE_READ,
            "ns",
            "synthetic-token",
            true,
        );
    }
}

fn pending(family: &SessionProtection) -> bool {
    fs::read_dir(family.pointer().path).unwrap().any(|entry| {
        entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with("pending-")
    })
}

// Parsing is shared with the normal network fixture. This worker owns one socket
// and reports EOF after cancellation, so dropping the test cannot strand it.
struct ChunkServer {
    url: String,
    started: Option<oneshot::Receiver<Request>>,
    peer_closed: Option<oneshot::Receiver<bool>>,
    stop: Arc<AtomicBool>,
    socket: Arc<StdMutex<Option<TcpStream>>>,
    worker: Option<JoinHandle<()>>,
}

impl ChunkServer {
    fn new(interrupt: bool) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let socket = Arc::new(StdMutex::new(None));
        let active = socket.clone();
        let (sent, started) = oneshot::channel();
        let (closed, peer_closed) = oneshot::channel();
        let worker = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(5);
            let mut stream = loop {
                if stopped.load(Ordering::Relaxed) || Instant::now() >= deadline {
                    return;
                }
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::park_timeout(Duration::from_millis(5));
                    }
                    Err(error) => panic!("accept: {error}"),
                }
            };
            stream.set_nonblocking(false).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            stream
                .set_write_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            *active.lock().unwrap() = Some(stream.try_clone().unwrap());
            let request = read_request(&mut stream);
            let chunk = b"{\"result\":{\"mirrored\":true,\"value\":\"";
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n{:x}\r\n", chunk.len()).unwrap();
            stream.write_all(chunk).unwrap();
            stream.write_all(b"\r\n").unwrap();
            stream.flush().unwrap();
            let _ = sent.send(request);
            if interrupt {
                stream.shutdown(Shutdown::Both).unwrap();
            } else {
                let mut byte = [0];
                let eof = matches!(stream.read(&mut byte), Ok(0));
                let _ = closed.send(eof);
            }
            active.lock().unwrap().take();
        });
        Self {
            url,
            started: Some(started),
            peer_closed: Some(peer_closed),
            stop,
            socket,
            worker: Some(worker),
        }
    }

    async fn started(&mut self) -> Request {
        tokio::time::timeout(Duration::from_secs(5), self.started.take().unwrap())
            .await
            .unwrap()
            .unwrap()
    }

    async fn closed(&mut self) -> bool {
        tokio::time::timeout(Duration::from_secs(3), self.peer_closed.take().unwrap())
            .await
            .unwrap()
            .unwrap()
    }
}

impl Drop for ChunkServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(socket) = self.socket.lock().unwrap().as_ref() {
            let _ = socket.shutdown(Shutdown::Both);
        }
        if let Some(worker) = self.worker.take() {
            worker.thread().unpark();
            worker.join().unwrap();
        }
    }
}

#[tokio::test]
async fn chunked_interruption_and_future_cancellation_keep_proof_and_close_the_owned_socket() {
    for interrupt in [true, false] {
        let roots = Roots::new();
        let family = roots.family("bound");
        let mut server = ChunkServer::new(interrupt);
        let reader = client(&server.url, Some(&family), "ns", "synthetic-token");
        let input = source_body();
        let mut work = Box::pin(reader.call::<Value>(SOURCE_READ, &input));
        if interrupt {
            let (request, result) = tokio::join!(
                server.started(),
                tokio::time::timeout(Duration::from_secs(4), &mut work),
            );
            assert_wire(&request, SOURCE_READ, "ns", "synthetic-token", true);
            assert!(matches!(result.unwrap(), Err(Error::Transport(_))));
        } else {
            let request = tokio::select! {
                request = server.started() => request,
                result = &mut work => panic!("incomplete response finished: {result:?}"),
            };
            assert_wire(&request, SOURCE_READ, "ns", "synthetic-token", true);
            assert!(tokio::time::timeout(Duration::from_millis(50), &mut work)
                .await
                .is_err());
            drop(work);
            assert!(server.closed().await);
        }
        assert!(pending(&family));
        assert!(SessionProtection::open(family.pointer())
            .unwrap()
            .protected()
            .unwrap());
        suppressed(
            reader
                .call_automatic::<Value>(
                    &roots.family("clean").capture(),
                    "memory.set",
                    &write_body(),
                )
                .await
                .unwrap_err(),
        );
    }
}

#[tokio::test]
async fn retry_and_pacing_recheck_protection_acquired_after_a_completed_first_attempt() {
    for status in [429, 503] {
        wait_barrier(status).await;
    }
}

async fn wait_barrier(status: u16) {
    let roots = Roots::new();
    let bound = roots.family("bound");
    let clean = roots.family("clean");
    let capture = clean.capture();
    let server = Server::new(vec![response(
        status,
        json!({"detail":{"code":if status == 429 { "RATE_LIMITED" } else { "INTERNAL_ERROR" },"message":"wait"}}),
        "Content-Type: application/json\r\nRetry-After: 5\r\n",
    )]);
    let writer = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("synthetic-token")
        .session_protection(bound.clone())
        .timeout(Duration::from_secs(3))
        .ratelimit_max_wait(Duration::from_secs(5))
        .retry(RetryPolicy {
            max_attempts: 2,
            backoff: Duration::from_secs(5),
            backoff_max: Duration::from_secs(5),
            backoff_jitter: 0.0,
            statuses: [503].into_iter().collect(),
            respect_retry_after: true,
            max_retry_after: Duration::from_secs(5),
            ..RetryPolicy::default()
        })
        .build()
        .unwrap();
    let input = write_body();
    let mut work = Box::pin(writer.call_automatic::<Value>(&capture, "memory.set", &input));
    let request = tokio::select! {
        request = server.request() => request,
        result = &mut work => panic!("first attempt finished without waiting: {result:?}"),
    };
    assert_wire(&request, "memory.set", "ns", "synthetic-token", true);
    let acknowledged = tokio::time::timeout(Duration::from_secs(2), async {
        while pending(&bound) {
            tokio::task::yield_now().await;
        }
    });
    tokio::select! {
        result = acknowledged => result.unwrap(),
        result = &mut work => panic!("wait ended before protection acquisition: {result:?}"),
    }
    let source = Server::new(vec![private_response()]);
    let reader = client(&source.url, Some(&bound), "other", "other-token");
    let input = source_body();
    let answer: Value = tokio::select! {
        result = reader.call(SOURCE_READ, &input) => result.unwrap(),
        result = &mut work => panic!("wait ended before protection acquisition: {result:?}"),
    };
    assert_eq!(answer["result"]["value"], PRIVATE_VALUE);
    assert_wire(
        &source.request().await,
        SOURCE_READ,
        "other",
        "other-token",
        true,
    );
    let error = tokio::time::timeout(Duration::from_secs(6), &mut work)
        .await
        .unwrap()
        .unwrap_err();
    suppressed(error);
    no_request(&server).await;
    assert!(SessionProtection::open(bound.pointer())
        .unwrap()
        .protected()
        .unwrap());
}

#[tokio::test]
async fn lost_and_changed_trusted_pointers_refuse_before_http() {
    for lost in [true, false] {
        let roots = Roots::new();
        let family = roots.family("bound");
        let server = Server::new(vec![response(
            200,
            "{}",
            "Content-Type: application/json\r\n",
        )]);
        let writer = client(&server.url, Some(&family), "ns", "synthetic-token");
        let pointer = family.pointer();
        if lost {
            fs::remove_file(pointer.path.join("state")).unwrap();
        } else {
            fs::write(
                pointer.path.join("identity"),
                "00000000-0000-0000-0000-000000000000",
            )
            .unwrap();
        }
        assert!(matches!(
            SessionProtection::open(pointer),
            Err(Error::Configuration(_))
        ));
        assert!(matches!(
            writer.call::<Value>("memory.set", &write_body()).await,
            Err(Error::Configuration(_))
        ));
        no_request(&server).await;
    }
}
