use clickhouse_loom::{
    CallOptions, Client, MemoryDeleteInput, MemoryDeleteVersionInput,
    MemoryDeleteVersionInputVersion, MemoryGetInput, MemorySetInput, NamespaceGetInput,
    NamespaceRegisterInput,
};
use serde_json::{json, Value};

#[path = "live_targets/mod.rs"]
mod live_targets;

fn client(namespace: &str, agent: &str, user: &str, session: &str) -> Client {
    Client::builder(
        std::env::var("LOOM_RUST_LIVE_ORG").unwrap(),
        namespace,
        agent,
    )
    .base_url(std::env::var("LOOM_RUST_LIVE_BASE_URL").unwrap())
    .api_key(std::env::var("LOOM_RUST_LIVE_ADMIN_TOKEN").unwrap())
    .user_id(user)
    .session_id(session)
    .run_id(format!("run-{agent}"))
    .build()
    .unwrap()
}

async fn call(client: &Client, op: &str, body: Value) -> Value {
    client
        .call::<Value>(op, &body, CallOptions::default())
        .await
        .unwrap_or_else(|error| panic!("{op}: {error:?}"))
        .data
}

async fn seed(namespace: &str) -> (Client, Client) {
    let bound = client(namespace, "bound", "bound-user", "bound-session");
    let target = client(namespace, "target", "target-user", "target-session");
    for (writer, marker) in [(&bound, "bound"), (&target, "target")] {
        for key in ["read", "delete", "version", "lock"] {
            let input = MemorySetInput::new(key, json!(format!("{marker} {key} orchid evidence")));
            let input = if key == "read" {
                input.tags(["identity-read"])
            } else {
                input
            };
            writer
                .memory_set(
                    input
                        .agent("forged")
                        .user_id("forged")
                        .session_id("forged")
                        .run_id("forged"),
                )
                .await
                .unwrap();
        }
    }
    (bound, target)
}

fn target_body(key: &str) -> Value {
    json!({
        "memory_key": key, "agent": "target", "user_id": "target-user",
        "session_id": "target-session", "run_id": "run-target"
    })
}

// The integration harness supplies a private database and authenticated tenant.
#[tokio::test]
#[ignore = "requires the authenticated integration server and disposable tenant tokens"]
async fn memory_targets_and_filters_select_stored_rows() {
    let (bound, target) = seed("identity-rows").await;
    let saved = bound
        .memory_get(
            MemoryGetInput::new("read")
                .agent("target")
                .user_id("target-user")
                .session_id("target-session"),
        )
        .await
        .unwrap();
    assert_eq!(saved.value, Some(json!("target read orchid evidence")));
    assert_eq!(saved.agent.as_deref(), Some("target"));
    assert_eq!(saved.user_id.as_deref(), Some("target-user"));
    assert_eq!(saved.session_id.as_deref(), Some("target-session"));
    assert_eq!(
        call(&bound, "memory.get", json!({"memory_key": "read"})).await["value"],
        "bound read orchid evidence"
    );
    for op in [
        "memory.list",
        "memory.bulk_get",
        "memory.history",
        "memory.evidence",
    ] {
        let mut body = target_body("read");
        body["memory_keys"] = json!(["read"]);
        let result = call(&bound, op, body).await;
        assert!(
            result.to_string().contains("target read orchid evidence"),
            "{op}: {result}"
        );
        assert!(
            !result.to_string().contains("bound read orchid evidence"),
            "{op}: {result}"
        );
    }
    for op in ["memory.search", "memory.ask", "memory.brief"] {
        let mut body = target_body("read");
        body["query"] = json!("orchid evidence");
        if op == "memory.brief" {
            body.as_object_mut().unwrap().remove("memory_key");
            body["tags"] = json!(["identity-read"]);
        } else {
            body["search_mode"] = json!("lexical");
            body["allow_synthesis"] = json!(false);
            body["reinforce"] = json!(false);
        }
        let result = call(&bound, op, body).await;
        assert!(
            result.to_string().contains("target read orchid evidence"),
            "{op}: {result}"
        );
        assert!(
            !result.to_string().contains("bound read orchid evidence"),
            "{op}: {result}"
        );
    }
    let deleted = MemoryDeleteInput::new("delete")
        .agent("target")
        .user_id("target-user")
        .session_id("target-session")
        .send(&bound, CallOptions::default())
        .await
        .unwrap();
    assert!(deleted.data.tombstoned);
    assert_eq!(
        call(&target, "memory.get", json!({"memory_key": "delete"})).await["found"],
        false
    );
    assert_eq!(
        call(&bound, "memory.get", json!({"memory_key": "delete"})).await["value"],
        "bound delete orchid evidence"
    );
    let version = target
        .memory_get(MemoryGetInput::new("version"))
        .await
        .unwrap()
        .version
        .flatten()
        .unwrap();
    MemoryDeleteVersionInput::new(
        "version",
        MemoryDeleteVersionInputVersion::Variant1(version.to_string()),
    )
    .agent("target")
    .user_id("target-user")
    .session_id("target-session")
    .send(&bound, CallOptions::default())
    .await
    .unwrap();
    assert_eq!(
        call(&target, "memory.get", json!({"memory_key": "version"})).await["found"],
        false
    );
    assert_eq!(
        call(&bound, "memory.get", json!({"memory_key": "version"})).await["found"],
        true
    );
    let mut body = target_body("lock");
    body["mode"] = json!("no_delete");
    body["reason"] = json!("Selected row regression");
    assert_eq!(
        call(&bound, "memory.lock", body.clone()).await["locked"],
        true
    );
    assert_eq!(
        call(&target, "memory.get", json!({"memory_key": "lock"})).await["lock_mode"],
        "no_delete"
    );
    assert_eq!(
        call(&bound, "memory.get", json!({"memory_key": "lock"})).await["lock_mode"],
        ""
    );
    assert_eq!(call(&bound, "memory.unlock", body).await["unlocked"], true);
}

#[tokio::test]
#[ignore = "requires the authenticated integration server and disposable tenant tokens"]
async fn namespace_targets_and_org_rollups_reach_both_namespaces() {
    let bound = client("scope-bound", "bound", "bound-user", "bound-session");
    for namespace in ["scope-bound", "scope-target"] {
        bound
            .namespace_register(
                NamespaceRegisterInput::new("sdk@example.test")
                    .namespace(namespace)
                    .description(format!("description-{namespace}")),
            )
            .await
            .unwrap();
    }
    let result = NamespaceGetInput::new()
        .namespace("scope-target")
        .send(&bound, CallOptions::default())
        .await
        .unwrap();
    assert_eq!(result.data.namespace, "scope-target");
    assert_eq!(
        result.data.description.as_deref(),
        Some("description-scope-target")
    );
    assert_eq!(
        bound
            .namespace_get(NamespaceGetInput::new())
            .await
            .unwrap()
            .namespace,
        "scope-bound"
    );
    for namespace in ["scope-bound", "scope-target"] {
        assert_eq!(
            call(
                &bound,
                "namespace.guide_set",
                json!({
                    "namespace": namespace, "content": format!("guide-{namespace}"),
                    "expected_revision": 0
                })
            )
            .await["written"],
            true
        );
        let writer = client(namespace, "bound", "bound-user", "bound-session");
        call(
            &writer,
            "memory.set",
            json!({"memory_key": "scope-marker", "value": namespace}),
        )
        .await;
    }
    assert_eq!(
        call(
            &bound,
            "namespace.guide_get",
            json!({"namespace": "scope-target"})
        )
        .await["content"],
        "guide-scope-target"
    );
    assert_eq!(
        call(&bound, "namespace.guide_get", json!({})).await["content"],
        "guide-scope-bound"
    );
    for filter in [json!("scope-target"), Value::Null, json!({})] {
        let mut body = json!({"user_id": "bound-user"});
        if filter != json!({}) {
            body["namespace"] = filter.clone();
        }
        let report = call(&bound, "memory.owner_report", body.clone()).await;
        let text = report.to_string();
        assert!(text.contains("scope-target"), "{report}");
        assert_eq!(
            text.contains("scope-bound"),
            filter != json!("scope-target")
        );
        body = json!({
            "from_date": "2000-01-01", "to_date": "2100-01-01",
            "group_by": ["namespace"], "user_id": "bound-user"
        });
        if filter != json!({}) {
            body["namespace"] = filter.clone();
        }
        let mut costs = Value::Null;
        for _ in 0..40 {
            costs = call(&bound, "cost.summary", body.clone()).await;
            if costs["rows"]
                .as_array()
                .unwrap()
                .iter()
                .any(|row| row["namespace"] == "scope-target")
            {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        let text = costs.to_string();
        assert!(text.contains("scope-target"), "{costs}");
        assert_eq!(
            text.contains("scope-bound"),
            filter != json!("scope-target")
        );
        let mut body = json!({"event_type": "namespace.guide_set"});
        if filter != json!({}) {
            body["namespace"] = filter.clone();
        }
        let audit = call(&bound, "audit.list", body).await;
        let text = audit.to_string();
        assert!(text.contains("scope-target"), "{audit}");
        assert_eq!(
            text.contains("scope-bound"),
            filter != json!("scope-target")
        );
    }
}

#[tokio::test]
#[ignore = "requires the authenticated integration server and disposable tenant tokens"]
async fn recall_queue_filters_and_scratchpad_run_select_the_requested_identity() {
    let bound = client("identity-turns", "bound", "bound-user", "bound-session");
    let target = client("identity-turns", "target", "target-user", "target-session");
    for (writer, marker) in [(&bound, "bound"), (&target, "target")] {
        let body = json!({
            "messages": [{"role": "user", "content": format!("{marker} orchidturn evidence")}],
            "defer": false
        });
        let ingested = call(writer, "memory.set_from_messages", body.clone()).await;
        assert!(!ingested["evidence_ids"].as_array().unwrap().is_empty());
        let mut deferred = body;
        deferred["defer"] = json!(true);
        assert_eq!(
            call(writer, "memory.set_from_messages", deferred).await["deferred"],
            true
        );
        call(
            writer,
            "working_memory.set",
            json!({"block_key": "notes", "content": format!("{marker} notes")}),
        )
        .await;
    }
    let mut body = target_body("unused");
    body["query"] = json!("orchidturn");
    let recalled = call(&bound, "memory.recall", body.clone()).await;
    assert_eq!(recalled["returned"], 1);
    assert_eq!(
        recalled["turns"][0]["content"],
        "target orchidturn evidence"
    );
    let queued = call(&bound, "memory.extraction_status", body.clone()).await;
    assert_eq!(queued["batches"].as_array().unwrap().len(), 1);
    assert_eq!(queued["batches"][0]["agent"], "target");
    body["block_key"] = json!("notes");
    let scratchpad = call(&bound, "working_memory.get", body).await;
    assert_eq!(scratchpad["found"], true);
    assert_eq!(scratchpad["content"], "target notes");
    let queued = call(
        &bound,
        "memory.extraction_status",
        json!({"agent": "*", "user_id": "-", "session_id": "-"}),
    )
    .await;
    assert_eq!(queued["batches"].as_array().unwrap().len(), 2);
    let queued = call(&bound, "memory.extraction_status", json!({})).await;
    assert_eq!(queued["batches"].as_array().unwrap().len(), 2);
}

#[tokio::test]
#[ignore = "requires the authenticated integration server and disposable tenant tokens"]
async fn procedure_revision_updates_the_selected_users_procedure() {
    let bound = client(
        "identity-procedures",
        "bound",
        "bound-user",
        "bound-session",
    );
    let target = client(
        "identity-procedures",
        "target",
        "target-user",
        "target-session",
    );
    for writer in [&bound, &target] {
        call(
            writer,
            "procedure.add",
            json!({
                "name": "Live procedure", "situation": "Inspect stored identity",
                "steps": ["Gather evidence", "Check evidence"]
            }),
        )
        .await;
    }
    let key = "procedure_live_procedure";
    let mut body = target_body(key);
    body["graph"] = json!({
        "nodes": [
            {"id": "n1", "kind": "step", "text": "Gather evidence"},
            {"id": "n2", "kind": "step", "text": "Verify target evidence"}
        ],
        "edges": [{"src": "n1", "dst": "n2", "kind": "dep"}]
    });
    call(&bound, "procedure.revise", body.clone()).await;
    let target_row = call(&target, "memory.get", json!({"memory_key": key})).await;
    let bound_row = call(&bound, "memory.get", json!({"memory_key": key})).await;
    assert_eq!(target_row["value"]["steps"][1], "Verify target evidence");
    assert_eq!(bound_row["value"]["steps"][1], "Check evidence");
    call(&bound, "procedure.evolve", target_body(key)).await;
}

#[cfg(feature = "blocking")]
#[test]
#[ignore = "requires the authenticated integration server and disposable tenant tokens"]
fn blocking_targets_read_and_delete_the_selected_row() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let (bound, target) = runtime.block_on(seed("identity-blocking"));
    let client = clickhouse_loom::blocking::Client::builder(
        std::env::var("LOOM_RUST_LIVE_ORG").unwrap(),
        "identity-blocking",
        "bound",
    )
    .base_url(std::env::var("LOOM_RUST_LIVE_BASE_URL").unwrap())
    .api_key(std::env::var("LOOM_RUST_LIVE_ADMIN_TOKEN").unwrap())
    .user_id("bound-user")
    .session_id("bound-session")
    .build()
    .unwrap();
    let result = client
        .memory_get(
            MemoryGetInput::new("delete")
                .agent("target")
                .user_id("target-user")
                .session_id("target-session"),
        )
        .unwrap();
    assert_eq!(result.value, Some(json!("target delete orchid evidence")));
    client
        .memory_delete(
            MemoryDeleteInput::new("delete")
                .agent("target")
                .user_id("target-user")
                .session_id("target-session"),
        )
        .unwrap();
    assert_eq!(
        runtime.block_on(call(&target, "memory.get", json!({"memory_key": "delete"})))["found"],
        false
    );
    assert_eq!(
        runtime.block_on(call(&bound, "memory.get", json!({"memory_key": "delete"})))["value"],
        "bound delete orchid evidence"
    );
}
