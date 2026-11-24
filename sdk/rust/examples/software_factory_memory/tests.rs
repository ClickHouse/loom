use super::*;
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    thread,
    time::{Duration, Instant},
};

// Cached status A must not attribute bytes read from index B to A.
async fn check_range(commit: &str, status: &str, text: &str) -> Result<SourceEvidence> {
    let head = "a".repeat(40);
    let range = json!({
        "path": "src/lib.rs", "start_line": 1, "end_line": 1,
        "status": status, "indexed_commit": commit,
        "windows": [{"start_line": 1, "end_line": 1, "text": text}]
    });
    let replies = [
        (
            "code.index_status",
            json!({
                "repo": "owner/project", "branch": "main", "namespace": "source",
                "git_head": head, "fetch_overdue": false
            }),
        ),
        (
            "code.symbol",
            json!({"definitions": [{
                "path": "src/lib.rs", "start_line": 1, "end_line": 1,
                "symbol": "function", "body": "fn function() {}\n", "granularity": "unit"
            }]}),
        ),
        ("code.read", json!({"results": [range], "as_of": {}})),
    ];
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    let worker = thread::spawn(move || {
        for (op, reply) in replies {
            let deadline = Instant::now() + Duration::from_secs(5);
            let (mut socket, _) = loop {
                match listener.accept() {
                    Ok(connection) => break connection,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(Instant::now() < deadline, "Missing request for {op}");
                        thread::park_timeout(Duration::from_millis(5));
                    }
                    Err(error) => panic!("{error}"),
                }
            };
            socket
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            assert_eq!(line.trim(), format!("POST /v1/{op} HTTP/1.1"));
            let mut length = 0;
            loop {
                line.clear();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
                if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                    length = value.trim().parse::<usize>().unwrap();
                }
            }
            let mut body = vec![0; length];
            reader.read_exact(&mut body).unwrap();
            if op == "code.read" {
                let request: serde_json::Value = serde_json::from_slice(&body).unwrap();
                assert_eq!(request["path"], "src/lib.rs");
                assert_eq!(request["start_line"], 1);
                assert_eq!(request["end_line"], 1);
                assert!(request.get("ranges").is_none());
            }
            let body = reply.to_string();
            write!(
                socket,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        }
    });
    let code = Client::builder("org", "source", "reader")
        .base_url(format!("http://{address}"))
        .api_key("")
        .build()
        .unwrap();
    let result = source(&code, "owner/project", "function").await;
    worker.join().unwrap();
    result
}

#[tokio::test]
async fn evidence_rejects_an_advanced_index_partial_ranges_and_changed_bytes() {
    let head = "a".repeat(40);
    for (commit, status, body) in [
        ("b".repeat(40), "complete", "fn function() {}\n"),
        (head.clone(), "partial", "fn function() {}\n"),
        (head.clone(), "complete", "fn function() { changed(); }\n"),
    ] {
        assert!(check_range(&commit, status, body).await.is_err());
    }
    let evidence = check_range(&head, "complete", "fn function() {}\n")
        .await
        .unwrap();
    assert_eq!(evidence.commit, head);
    assert_eq!(
        evidence.returned_body_sha256,
        common::sha256("fn function() {}\n")
    );
}
