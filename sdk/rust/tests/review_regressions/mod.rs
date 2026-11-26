use super::*;
mod recording_identity;
mod targeted_mutations;
use clickhouse_loom::{
    CallOptions, CollectionCreateInput, CollectionDeleteInput, CollectionMoveInput,
    MemoryExtractionStatusInput, MemoryListInput, MemoryListOutput, NamespaceDeleteInput,
    NamespaceRollbackInput, PushConfirmInput, PushStatusOutput,
};

fn target_responses() -> Vec<String> {
    [
        json!({"namespace": "col-b", "deleted": true, "updated_at": "2020-01-01T00:00:00Z"}),
        json!({
            "dry_run": true, "as_of": "2020-01-01T00:00:00Z", "version_at": 1,
            "reinstate": 0, "remove": 0
        }),
        json!({"namespace": "col-b", "parent": "", "created": true}),
        json!({"namespace": "col-b", "parent": "", "moved": true}),
    ]
    .into_iter()
    .map(|body| response(200, body, ""))
    .collect()
}

#[tokio::test]
async fn generic_namespace_targets_and_derived_collection_names_survive() {
    let mut responses = vec![response(200, json!({}), ""); 4];
    responses.push(response(
        200,
        json!({"namespace": "col-travel", "parent": "", "created": true}),
        "",
    ));
    let server = Server::new(responses);
    let client = Client::builder("org", "col-a", "bot")
        .base_url(&server.url)
        .api_key("")
        .build()
        .unwrap();
    for op in [
        "namespace.delete",
        "namespace.rollback",
        "collection.create",
        "collection.move",
    ] {
        client
            .call::<Value>(op, &json!({"namespace": "col-b"}), CallOptions::default())
            .await
            .unwrap();
        assert_eq!(
            server.request().await.body,
            json!({"org": "org", "namespace": "col-b"})
        );
    }
    client
        .collection_create_with_options(
            CollectionCreateInput::new()
                .title("Travel")
                .derive_name(true),
            CallOptions::default(),
        )
        .await
        .unwrap();
    assert_eq!(
        server.request().await.body,
        json!({"org": "org", "title": "Travel", "derive_name": true})
    );
}

#[tokio::test]
async fn namespace_and_collection_requests_preserve_targets() {
    assert!(serde_json::from_value::<NamespaceDeleteInput>(json!({})).is_err());
    assert!(serde_json::from_value::<NamespaceRollbackInput>(
        json!({"as_of": "2020-01-01T00:00:00Z"})
    )
    .is_err());
    assert!(serde_json::from_value::<CollectionMoveInput>(json!({})).is_err());
    let server = Server::new(target_responses());
    let client = Client::builder("org", "col-a", "bot")
        .base_url(&server.url)
        .api_key("")
        .build()
        .unwrap();
    NamespaceDeleteInput::new("col-b")
        .send(&client, CallOptions::default())
        .await
        .unwrap();
    NamespaceRollbackInput::new("col-b", "2020-01-01T00:00:00Z")
        .send(&client, CallOptions::default())
        .await
        .unwrap();
    CollectionCreateInput::new()
        .namespace("col-b")
        .send(&client, CallOptions::default())
        .await
        .unwrap();
    CollectionMoveInput::new("col-b")
        .send(&client, CallOptions::default())
        .await
        .unwrap();
    for op in [
        "namespace.delete",
        "namespace.rollback",
        "collection.create",
        "collection.move",
    ] {
        let request = server.request().await;
        assert_eq!(request.line, format!("POST /v1/{op} HTTP/1.1"));
        assert_eq!(request.body["org"], "org");
        assert_eq!(request.body["namespace"], "col-b");
    }
}

#[cfg(feature = "blocking")]
#[test]
fn blocking_namespace_and_collection_requests_preserve_targets() {
    let server = Server::new(target_responses());
    let client = clickhouse_loom::blocking::Client::builder("org", "col-a", "bot")
        .base_url(&server.url)
        .api_key("")
        .build()
        .unwrap();
    client
        .namespace_delete(NamespaceDeleteInput::new("col-b"))
        .unwrap();
    client
        .namespace_rollback(NamespaceRollbackInput::new("col-b", "2020-01-01T00:00:00Z"))
        .unwrap();
    client
        .collection_create(CollectionCreateInput::new().namespace("col-b"))
        .unwrap();
    client
        .collection_move(CollectionMoveInput::new("col-b"))
        .unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    for _ in 0..4 {
        assert_eq!(
            runtime.block_on(server.request()).body["namespace"],
            "col-b"
        );
    }
}

#[tokio::test]
async fn extraction_status_preserves_filters_and_server_defaults() {
    let server = Server::new(vec![response(200, json!({}), ""); 3]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer")
        .session_id("session")
        .run_id("run")
        .build()
        .unwrap();
    for filter in [None, Some("*"), Some("target")] {
        let mut input = MemoryExtractionStatusInput::new();
        if let Some(value) = filter {
            input = input.agent(value).user_id(value).session_id(value);
        }
        input.send(&client, CallOptions::default()).await.unwrap();
        let mut expected = json!({"org": "org", "namespace": "ns"});
        if let Some(value) = filter {
            for key in ["agent", "user_id", "session_id"] {
                expected[key] = json!(value);
            }
        }
        assert_eq!(server.request().await.body, expected);
    }
}

#[cfg(feature = "blocking")]
#[test]
fn blocking_extraction_status_preserves_filters() {
    let server = Server::new(vec![response(200, json!({}), ""); 2]);
    let client = clickhouse_loom::blocking::Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .user_id("writer")
        .session_id("session")
        .build()
        .unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    for filter in ["*", "target"] {
        client
            .memory_extraction_status(
                MemoryExtractionStatusInput::new()
                    .agent(filter)
                    .user_id(filter)
                    .session_id(filter),
            )
            .unwrap();
        let body = runtime.block_on(server.request()).body;
        for key in ["agent", "user_id", "session_id"] {
            assert_eq!(body[key], filter);
        }
    }
}

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
        let mut input = MemoryListInput::new()
            .pinned_only(true)
            .conflicted_only(true);
        if let Some(value) = filter {
            input = input.agent(value).user_id(value).session_id(value);
        }
        input.send(&client, CallOptions::default()).await.unwrap();
        let body = server.request().await.body;
        assert_eq!(body["agent"], filter.unwrap_or("bot"));
        assert_eq!(body["user_id"], filter.unwrap_or("writer"));
        assert_eq!(body["session_id"], filter.unwrap_or("session"));
        assert_eq!(body["pinned_only"], true);
        assert_eq!(body["conflicted_only"], true);
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

#[tokio::test]
async fn grep_preserves_dynamic_default_omission_and_explicit_booleans() {
    use clickhouse_loom::{CodeGrepInput, CodeGrepInputMode};

    let server = Server::new(vec![
        response(
            200,
            json!({"matches": [], "chunks_matched": 0}),
            ""
        );
        12
    ]);
    let client = Client::builder("org", "ns", "bot")
        .base_url(&server.url)
        .api_key("")
        .build()
        .unwrap();
    for mode in [
        CodeGrepInputMode::Matches,
        CodeGrepInputMode::Files,
        CodeGrepInputMode::Presence,
        CodeGrepInputMode::Message,
    ] {
        for include_body in [None, Some(false), Some(true)] {
            let pattern = serde_json::from_value(json!("needle")).unwrap();
            let mut input = CodeGrepInput::new(pattern).mode(mode.clone());
            if let Some(value) = include_body {
                input = input.include_body(value);
            }
            let encoded = serde_json::to_value(&input).unwrap();
            input.send(&client, CallOptions::default()).await.unwrap();
            let body = server.request().await.body;
            assert_eq!(body["mode"], serde_json::to_value(&mode).unwrap());
            let expected = include_body.map(Value::Bool);
            for value in [encoded, body] {
                assert_eq!(value.get("include_body"), expected.as_ref());
            }
        }
    }
}
