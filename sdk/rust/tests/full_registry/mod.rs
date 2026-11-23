use super::*;
use clickhouse_loom::{
    CallOptions, KnowledgeUpsertInput, MemoryAskInput, ObserveRecordInput, RunBeginInput,
    RunCompleteInput, RunCompleteInputStatus, RunGetInput,
};

pub(super) fn conversation_cases() -> Vec<(&'static str, Value, Value)> {
    let mut cases = vec![
        (
            "conversation.search",
            json!({"query": "q", "agent": "*", "user_id": "reader", "run_id": "filter"}),
        ),
        (
            "conversation.search",
            json!({"query": "q", "agent": "-", "user_id": null}),
        ),
        (
            "conversation.get",
            serde_json::to_value(clickhouse_loom::ConversationGetInput::new("thread").agent("*"))
                .unwrap(),
        ),
        (
            "conversation.list",
            json!({"thread_id": "thread", "agent": "-", "run_id": "filter"}),
        ),
        ("conversation.threads", json!({"agent": "*"})),
        ("conversation.threads", json!({})),
    ];
    for op in [
        "conversation.search_saved_messages",
        "conversation.list_saved_messages",
        "conversation.saved_status",
    ] {
        cases.push((op, json!({"agent": "other", "user_id": "reader"})));
        cases.push((op, json!({})));
    }
    cases.push(("conversation.search", json!({"query": "q"})));
    cases.push(("conversation.get", json!({"thread_id": "thread"})));
    cases.push((
        "conversation.create",
        json!({"agent": "other", "user_id": "reader"}),
    ));
    cases
        .into_iter()
        .map(|(op, input)| {
            let mut expected = input.clone();
            expected["org"] = json!("org");
            expected["namespace"] = json!("ns");
            if op == "conversation.create" {
                expected["agent"] = json!("bot");
                expected["user_id"] = json!("writer");
            } else if op != "conversation.threads" {
                expected
                    .as_object_mut()
                    .unwrap()
                    .entry("agent")
                    .or_insert(json!("bot"));
                if op.contains("saved") {
                    expected
                        .as_object_mut()
                        .unwrap()
                        .entry("user_id")
                        .or_insert(json!("writer"));
                }
            }
            (op, input, expected)
        })
        .collect()
}

#[tokio::test]
async fn conversation_reads_preserve_filters_and_default_only_required_identity() {
    let cases = conversation_cases();
    let server = Server::new(vec![response(400, "{}", ""); cases.len()]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer")
        .build()
        .unwrap();
    for (op, input, expected) in cases {
        client
            .call::<Value>(op, &input, CallOptions::default())
            .await
            .unwrap_err();
        let request = server.request().await;
        assert_eq!(request.line, format!("POST /v1/{op} HTTP/1.1"));
        assert_eq!(request.body, expected, "{op}");
    }
}

#[tokio::test]
async fn run_targets_filters_and_write_authorship_keep_their_meaning() {
    let server = Server::new(vec![response(400, "{}", ""); 6]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("user")
        .session_id("session")
        .run_id("ambient")
        .build()
        .unwrap();
    let options = CallOptions::default();
    client
        .run_get(RunGetInput::new("target"))
        .await
        .unwrap_err();
    client
        .run_complete(RunCompleteInput::new(
            "target",
            RunCompleteInputStatus::Completed,
        ))
        .await
        .unwrap_err();
    client
        .memory_ask(
            MemoryAskInput::new("query")
                .run_id("filter")
                .user_id("reader"),
        )
        .await
        .unwrap_err();
    client
        .observe_record(ObserveRecordInput::new("tool"))
        .await
        .unwrap_err();
    client
        .knowledge_upsert(KnowledgeUpsertInput::new("urn:guide", "text"))
        .await
        .unwrap_err();
    let failure = RunBeginInput::new("goal")
        .budget_usd(1.0)
        .send(&client, options)
        .await
        .unwrap_err();
    assert_eq!(failure.metadata.unwrap().status, Some(400));
    for op in [
        "run.get",
        "run.complete",
        "memory.ask",
        "observe.record",
        "knowledge.upsert",
        "run.begin",
    ] {
        let request = server.request().await;
        assert_eq!(request.line, format!("POST /v1/{op} HTTP/1.1"));
        assert_eq!(request.headers["x-loom-run-id"], "ambient");
        assert_eq!(request.body["org"], "org");
        match op {
            "run.get" | "run.complete" => assert_eq!(request.body["run_id"], "target"),
            "memory.ask" => {
                assert_eq!(request.body["run_id"], "filter");
                assert_eq!(request.body["user_id"], "reader");
            }
            "observe.record" => {
                assert_eq!(request.body["run_id"], "ambient");
                assert_eq!(request.body["user_id"], "user");
                assert_eq!(request.body["session_id"], "session");
            }
            "knowledge.upsert" => {
                assert_eq!(request.body["agent"], "bot");
                assert_eq!(request.body["session_id"], "session");
            }
            "run.begin" => {
                assert_eq!(request.body["session_id"], "session");
                assert_eq!(request.body["budget_usd"], 1.0);
            }
            _ => unreachable!(),
        }
    }
}

#[cfg(feature = "blocking")]
#[test]
fn blocking_run_methods_preserve_explicit_targets_and_write_authorship() {
    use clickhouse_loom::blocking;
    let server = Server::new(vec![response(400, "{}", ""); 2]);
    let client = blocking::Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .session_id("session")
        .run_id("ambient")
        .build()
        .unwrap();
    client.run_get(RunGetInput::new("target")).unwrap_err();
    client
        .run_begin(RunBeginInput::new("goal").budget_usd(1.0))
        .unwrap_err();
    let runtime = tokio::runtime::Runtime::new().unwrap();
    let read = runtime.block_on(server.request());
    assert_eq!(read.body["run_id"], "target");
    assert_eq!(read.headers["x-loom-run-id"], "ambient");
    let write = runtime.block_on(server.request());
    assert_eq!(write.body["session_id"], "session");
    assert_eq!(write.line, "POST /v1/run.begin HTTP/1.1");
}
