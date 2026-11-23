use super::*;
use clickhouse_loom::{CallOptions, MemoryUpdateInput};

fn cases() -> Vec<(MemoryUpdateInput, Value)> {
    let filters = [
        None,
        Some(("target-agent", "target-user", "target-session")),
        Some(("*", "*", "*")),
        Some(("-", "-", "-")),
        Some(("", "", "")),
        Some(("bot", "writer", "session")),
    ];
    filters
        .into_iter()
        .map(|filter| {
            let mut input = MemoryUpdateInput::new("key")
                .org("other-org")
                .namespace("other-namespace")
                .run_id("other-run")
                .value("revision");
            let mut expected = json!({
                "org": "org", "namespace": "ns", "run_id": "run",
                "memory_key": "key", "value": "revision"
            });
            if let Some((agent, user, session)) = filter {
                input = input.agent(agent).user_id(user).session_id(session);
                expected["agent"] = json!(agent);
                expected["user_id"] = json!(user);
                expected["session_id"] = json!(session);
            }
            (input, expected)
        })
        .collect()
}

#[tokio::test]
async fn typed_update_preserves_target_filters() {
    let cases = cases();
    let server = Server::new(vec![
        response(200, json!({"updated": true}), "");
        cases.len()
    ]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer")
        .session_id("session")
        .run_id("run")
        .build()
        .unwrap();
    for (input, expected) in cases {
        assert!(client.memory_update(input).await.unwrap().updated);
        let request = server.request().await;
        assert_eq!(request.line, "POST /v1/memory.update HTTP/1.1");
        assert_eq!(request.body, expected);
    }
}

#[tokio::test]
async fn request_send_preserves_target_filters() {
    let cases = cases();
    let server = Server::new(vec![
        response(200, json!({"updated": true}), "");
        cases.len()
    ]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer")
        .session_id("session")
        .run_id("run")
        .build()
        .unwrap();
    for (input, expected) in cases {
        assert!(
            input
                .send(&client, CallOptions::default())
                .await
                .unwrap()
                .data
                .updated
        );
        let request = server.request().await;
        assert_eq!(request.line, "POST /v1/memory.update HTTP/1.1");
        assert_eq!(request.body, expected);
    }
}

#[cfg(feature = "blocking")]
#[test]
fn blocking_update_preserves_target_filters() {
    let cases = cases();
    let server = Server::new(vec![
        response(200, json!({"updated": true}), "");
        cases.len()
    ]);
    let client = clickhouse_loom::blocking::Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer")
        .session_id("session")
        .run_id("run")
        .build()
        .unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    for (input, expected) in cases {
        assert!(client.memory_update(input).unwrap().updated);
        let request = runtime.block_on(server.request());
        assert_eq!(request.line, "POST /v1/memory.update HTTP/1.1");
        assert_eq!(request.body, expected);
    }
}
