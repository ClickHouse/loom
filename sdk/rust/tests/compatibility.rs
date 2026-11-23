use clickhouse_loom::{
    Client, DeleteResponse, GetResponse, SearchHit, SearchRequest, SearchResponse, SetRequest,
    SetResponse,
};
use serde_json::{json, Value};

// Public fields and method results remain usable by callers of the memory facade.
#[allow(dead_code)]
async fn memory_facade_types(client: &Client) -> Result<(), clickhouse_loom::Error> {
    let mut request = SetRequest::new("key", "fact");
    request.memory_type = Some("semantic".to_owned());
    request.tags = Some(vec!["tag".to_owned()]);
    request.ttl_seconds = Some(300_i64);
    request.confidence = Some(0.5_f64);
    request.if_match_sha256 = Some("hash".to_owned());
    let _: SetResponse = client.set(request).await?;
    let memory: GetResponse = client.get("key").await?;
    let _: Option<bool> = memory.pinned;
    let _: Option<u64> = memory.version;
    let _: Option<String> = memory.memory_type;
    let _: Option<f64> = memory.confidence;
    let mut request = SearchRequest::new("fact");
    request.top_k = Some(10_u32);
    request.search_mode = Some("lexical".to_owned());
    request.namespace_scope = Some("this".to_owned());
    request.agent_id = Some("*".to_owned());
    request.memory_type = Some("semantic".to_owned());
    request.tags = Some(vec![]);
    request.user_id = Some("reader".to_owned());
    request.session_id = Some("session".to_owned());
    let response: SearchResponse = client.search(request).await?;
    for hit in response.results {
        let _: Option<u64> = hit.version;
        let _: Option<String> = hit.namespace;
        let _: Option<Vec<String>> = hit.tags;
        let _: Option<u64> = hit.reinforcement_count;
        let _: Option<u64> = hit.used_count;
        let _: Option<u64> = hit.used_cited_count;
        let _: Option<u64> = hit.harm_count;
        let _: Option<u64> = hit.evidence_count;
        let _: Option<u64> = hit.acted_count;
        let _: Option<bool> = hit.pinned;
        let _: Option<bool> = hit.governed;
        let _: Option<bool> = hit.below_threshold;
    }
    let _: DeleteResponse = client.delete("key").await?;
    Ok(())
}

#[test]
fn memory_facade_preserves_missing_null_and_unknown_response_fields() {
    for extra in [json!({}), json!({"pinned": null, "version": null})] {
        let mut payload = json!({"found": false});
        payload
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        let memory: GetResponse = serde_json::from_value(payload).unwrap();
        assert_eq!(memory.pinned, None);
        assert_eq!(memory.version, None);
        assert_eq!(memory.value, Value::Null);
    }
    let memory: GetResponse =
        serde_json::from_value(json!({"found": true, "pinned": false, "future": []})).unwrap();
    assert_eq!(memory.pinned, Some(false));
    for extra in [
        json!({}),
        json!({"version": null, "namespace": null}),
        json!({"version": u64::MAX, "namespace": ""}),
    ] {
        let mut payload = json!({
            "memory_key": "key", "memory_type": "semantic",
            "content_excerpt": "fact", "distance": 0.1, "similarity": 0.9
        });
        payload
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        let hit: SearchHit = serde_json::from_value(payload).unwrap();
        assert_eq!(hit.version, extra.get("version").and_then(Value::as_u64));
        assert_eq!(
            hit.namespace.as_deref(),
            extra.get("namespace").and_then(Value::as_str)
        );
        assert_eq!(hit.source, "fact");
    }
    // The original facade does not require the full wire model's embedding_model.
    let search: SearchResponse = serde_json::from_value(json!({"results": []})).unwrap();
    assert_eq!(search.retrieval_risk, "");
    assert_eq!(search.term_leg, "");
    assert_eq!(search.candidates_total, 0);
}

#[test]
fn memory_facade_serializes_public_fields_without_changing_wire_values() {
    let mut write = SetRequest::new("key", json!({"number": u64::MAX}));
    write.ttl_seconds = Some(0);
    write.confidence = Some(0.0);
    write.tags = Some(vec![]);
    assert_eq!(
        serde_json::to_value(write).unwrap(),
        json!({
            "memory_key": "key", "value": {"number": u64::MAX},
            "ttl_seconds": 0, "confidence": 0.0, "tags": []
        })
    );
    let mut search = SearchRequest::new("query");
    search.agent_id = Some("reader".to_owned());
    assert_eq!(
        serde_json::to_value(search).unwrap(),
        json!({"query": "query", "agent": "reader"})
    );
}
