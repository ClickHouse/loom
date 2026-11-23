use super::*;
use clickhouse_loom::{
    CallOptions, CollectionDeleteInput, MemoryListInput, MemoryListOutput, PushConfirmInput,
    PushStatusOutput,
};

#[tokio::test]
async fn collection_delete_preserves_explicit_target_and_requires_one() {
    let server = Server::new(vec![
        response(
            200,
            json!({"namespace": "col-b", "deleted": true}),
            ""
        );
        2
    ]);
    let client = Client::builder("org", "col-a", "bot")
        .base_url(&server.url)
        .api_key("")
        .build()
        .unwrap();
    client
        .collection_delete(CollectionDeleteInput::new("col-b"))
        .await
        .unwrap();
    assert_eq!(
        server.request().await.body,
        json!({"org": "org", "namespace": "col-b"})
    );
    CollectionDeleteInput::new("col-b")
        .send(&client, CallOptions::default())
        .await
        .unwrap();
    assert_eq!(server.request().await.body["namespace"], "col-b");
    assert!(serde_json::from_value::<CollectionDeleteInput>(json!({})).is_err());
    let missing = client
        .call::<Value>("collection.delete", &json!({}), CallOptions::default())
        .await;
    assert!(matches!(missing.unwrap_err().error, Error::Input(_)));
}

#[tokio::test]
async fn list_filters_default_only_when_omitted() {
    let server = Server::new(vec![
        response(
            200,
            json!({"entries": [], "truncated": false}),
            ""
        );
        3
    ]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer")
        .session_id("session")
        .build()
        .unwrap();
    for filter in [None, Some("*"), Some("target")] {
        let mut input = MemoryListInput::new();
        if let Some(value) = filter {
            input = input.agent(value).user_id(value).session_id(value);
        }
        input.send(&client, CallOptions::default()).await.unwrap();
        let body = server.request().await.body;
        assert_eq!(body["agent"], filter.unwrap_or("bot"));
        assert_eq!(body["user_id"], filter.unwrap_or("writer"));
        assert_eq!(body["session_id"], filter.unwrap_or("session"));
    }
}

#[cfg(feature = "blocking")]
#[test]
fn blocking_preserves_collection_target_and_list_filters() {
    let server = Server::new(vec![
        response(200, json!({"namespace": "col-b", "deleted": true}), ""),
        response(200, json!({"entries": [], "truncated": false}), ""),
    ]);
    let client = clickhouse_loom::blocking::Client::builder("org", "col-a", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer")
        .session_id("session")
        .build()
        .unwrap();
    client
        .collection_delete(CollectionDeleteInput::new("col-b"))
        .unwrap();
    client
        .memory_list(
            MemoryListInput::new()
                .agent("*")
                .user_id("*")
                .session_id("*"),
        )
        .unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    assert_eq!(
        runtime.block_on(server.request()).body["namespace"],
        "col-b"
    );
    let body = runtime.block_on(server.request()).body;
    for key in ["agent", "user_id", "session_id"] {
        assert_eq!(body[key], "*");
    }
}

#[test]
fn projected_rows_preserve_missing_null_and_value() {
    let payload = json!({"entries": [
        {"memory_key": "key", "sensitivity": "", "value": false}
    ], "truncated": false});
    let page: MemoryListOutput = serde_json::from_value(payload.clone()).unwrap();
    assert_eq!(
        serde_json::to_value(page).unwrap()["entries"],
        payload["entries"]
    );
    for payload in [
        json!({"found": true}),
        json!({"found": true, "confidence": null}),
        json!({"found": true, "confidence": 0.0}),
    ] {
        let row: clickhouse_loom::MemoryGetOutput =
            serde_json::from_value(payload.clone()).unwrap();
        assert_eq!(serde_json::to_value(row).unwrap(), payload);
    }
}

#[tokio::test]
async fn list_decodes_populated_projected_response() {
    let entry = json!({
        "memory_key": "key", "sensitivity": "", "value": false, "tags": ["travel"]
    });
    let server = Server::new(vec![response(
        200,
        json!({"entries": [entry.clone()], "truncated": false}),
        "",
    )]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .build()
        .unwrap();
    let page = client
        .memory_list(MemoryListInput::new().fields(["value", "tags"]))
        .await
        .unwrap();
    assert_eq!(serde_json::to_value(&page.entries[0]).unwrap(), entry);
    assert_eq!(page.entries[0].memory_type, None);
    assert_eq!(
        server.request().await.body["fields"],
        json!(["value", "tags"])
    );
}

#[test]
fn secret_fields_serialize_but_do_not_appear_in_debug() {
    let confirm = PushConfirmInput::new("source", "generation").signing_secret("signing-marker");
    assert_eq!(
        serde_json::to_value(&confirm).unwrap()["signing_secret"],
        "signing-marker"
    );
    assert!(!format!("{confirm:?}").contains("signing-marker"));
    let status: PushStatusOutput =
        serde_json::from_value(json!({"verification_token": "token-marker"})).unwrap();
    assert_eq!(
        serde_json::to_value(&status).unwrap()["verification_token"],
        "token-marker"
    );
    assert!(!format!("{status:?}").contains("token-marker"));
}
