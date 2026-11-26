use super::*;
use clickhouse_loom::{
    ConversationAppendInput, ConversationAppendInputRole, ConversationCreateInput,
    ProcedureRecordRunInput,
};

fn recording_client(server: &Server, run: Option<&str>) -> Client {
    let mut builder = Client::builder("org", "ns", "runner")
        .base_url(&server.url)
        .api_key("")
        .user_id("runner-user")
        .session_id("new-session");
    if let Some(run) = run {
        builder = builder.run_id(run);
    }
    builder.build().unwrap()
}

fn recording_response() -> String {
    response(
        200,
        json!({
            "memory_key": "procedure_shared", "version": 0,
            "run_stats": {"total": 1, "ok": 1, "unfolded": 1},
            "thread_id": "thread", "created_at": "2026-01-01T00:00:00",
            "msg_id": "message", "ts": "2026-01-01T00:00:00"
        }),
        "",
    )
}

async fn check_procedure_selectors(generic: bool) {
    let server = Server::new(vec![recording_response()]);
    let client = recording_client(&server, None);
    let input = ProcedureRecordRunInput::new("procedure_shared")
        .agent("author")
        .user_id("author-user")
        .session_id("saved-session");
    if generic {
        client
            .call::<Value>("procedure.record_run", &input, CallOptions::default())
            .await
            .unwrap();
    } else {
        input.send(&client, CallOptions::default()).await.unwrap();
    }
    let body = server.request().await.body;
    assert_eq!(
        (
            body["agent"].clone(),
            body["user_id"].clone(),
            body["session_id"].clone()
        ),
        (
            json!("author"),
            json!("author-user"),
            json!("saved-session")
        ),
        "The server loads the procedure using this tuple; caller identity selects another row"
    );
}

#[tokio::test]
async fn procedure_record_run_preserves_explicit_procedure_selectors() {
    check_procedure_selectors(false).await;
}

#[tokio::test]
async fn procedure_record_run_generic_preserves_explicit_procedure_selectors() {
    check_procedure_selectors(true).await;
}

#[tokio::test]
async fn procedure_record_run_defaults_omitted_selectors() {
    let server = Server::new(vec![recording_response()]);
    let client = recording_client(&server, None);
    ProcedureRecordRunInput::new("procedure_shared")
        .send(&client, CallOptions::default())
        .await
        .unwrap();
    let body = server.request().await.body;
    assert_eq!(body["agent"], "runner");
    assert_eq!(body["user_id"], "runner-user");
    assert_eq!(body["session_id"], "new-session");
}

async fn check_conversation_run(op: &str, bound_run: Option<&str>, explicit_run: Option<&str>) {
    let server = Server::new(vec![recording_response()]);
    let client = recording_client(&server, bound_run);
    if op == "conversation.create" {
        let mut input = ConversationCreateInput::new("thread");
        if let Some(run) = explicit_run {
            input = input.run_id(run);
        }
        input.send(&client, CallOptions::default()).await.unwrap();
    } else {
        let mut input =
            ConversationAppendInput::new("thread", ConversationAppendInputRole::Tool, "message");
        if let Some(run) = explicit_run {
            input = input.run_id(run);
        }
        input.send(&client, CallOptions::default()).await.unwrap();
    }
    let request = server.request().await;
    assert_eq!(request.body["agent"], "runner");
    assert_eq!(request.body["user_id"], "runner-user");
    assert_eq!(
        request.headers.get("x-loom-run-id").map(String::as_str),
        bound_run
    );
    match bound_run.or(explicit_run) {
        Some(run) => assert_eq!(
            request.body["run_id"], run,
            "{op} persists its body's run_id; the run header only labels the call ledger"
        ),
        None => assert!(request.body.get("run_id").is_none()),
    }
}

#[tokio::test]
async fn conversation_create_uses_bound_run_for_recording() {
    check_conversation_run("conversation.create", Some("run-42"), None).await;
}

#[tokio::test]
async fn conversation_append_uses_bound_run_for_recording() {
    check_conversation_run("conversation.append", Some("run-42"), None).await;
}

#[tokio::test]
async fn conversation_recording_preserves_unbound_run_inputs() {
    for op in ["conversation.create", "conversation.append"] {
        for explicit in [None, Some("explicit-run")] {
            check_conversation_run(op, None, explicit).await;
        }
    }
}
