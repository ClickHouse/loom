use clickhouse_loom::{DeleteResponse, GetResponse, SearchHit, SearchResponse, SetResponse};
use serde::de::DeserializeOwned;
use serde_json::{json, Value};

#[test]
fn omitted_response_fields_keep_wire_defaults() {
    let saved: SetResponse = serde_json::from_value(json!({"version": 0})).unwrap();
    assert_eq!(saved.version, 0);
    assert_eq!(saved.memory_id, "");
    assert_eq!(saved.content_sha256, "");
    assert_eq!(saved.status, "");
    assert!(!saved.noop);
    assert_eq!(saved.merged_into_key, "");

    let memory: GetResponse = serde_json::from_value(json!({"found": false})).unwrap();
    assert!(!memory.found);
    assert_eq!(memory.memory_id, "");
    assert_eq!(memory.value, Value::Null);
    assert_eq!(memory.version, None);
    assert_eq!(memory.memory_type, None);
    assert_eq!(memory.confidence, None);
    assert_eq!(memory.pinned, None);

    let hit: SearchHit = serde_json::from_value(json!({
        "memory_key": "key", "memory_type": "semantic", "content_excerpt": "fact",
        "distance": 0.1, "similarity": 0.9
    }))
    .unwrap();
    assert_eq!(hit.version, None);
    assert_eq!(hit.namespace, None);
    assert_eq!(hit.tags, None);
    assert_eq!(hit.source, "fact");
    assert_eq!(hit.parent_memory_key, "");
    assert_eq!(hit.below_threshold, None);
    assert_eq!(hit.reinforcement_count, None);
    assert_eq!(hit.used_count, None);
    assert_eq!(hit.used_cited_count, None);
    assert_eq!(hit.harm_count, None);
    assert_eq!(hit.evidence_count, None);
    assert_eq!(hit.acted_count, None);
    assert_eq!(hit.pinned, None);
    assert_eq!(hit.rank_score, 0.0);
    assert_eq!(hit.temporal_anchor, "");
    assert_eq!(hit.temporal_confidence, 0.0);
    assert_eq!(hit.valid_at, "");
    assert!(hit.via_path.is_empty());
    assert!(hit.entities.is_empty());
    assert_eq!(hit.user_id, "");
    assert_eq!(hit.status, "");
    assert_eq!(hit.superseded_by, None);
    assert_eq!(hit.superseded_at, None);
    assert_eq!(hit.created_at, "");
    assert_eq!(hit.code_state, "");
    assert!(!hit.body_truncated);
    assert_eq!(hit.next, "");
    assert_eq!(hit.source_resource_id, "");
    assert_eq!(hit.source_class, "");
    assert!(!hit.verified);
    assert_eq!(hit.explanation, "");
    assert_eq!(hit.session_id, "");
    assert_eq!(hit.speaker, "");
    assert!(!hit.about_other);
    assert!(hit.derived_from.is_empty());
    assert!(hit.score_components.is_empty());
    assert_eq!(hit.resolved_from, None);

    let search: SearchResponse =
        serde_json::from_value(json!({"results": [], "embedding_model": "embed"})).unwrap();
    assert!(search.results.is_empty());
    assert!(search.namespaces_searched.is_empty());
    assert_eq!(search.candidates_total, 0);
    assert_eq!(search.next_page, "");
    assert!(!search.abstain);
    assert_eq!(search.abstain_reason, "");
    assert_eq!(search.abstain_code, "");
    assert_eq!(search.retrieval_risk, "");
    assert_eq!(search.term_leg, "");
    assert_eq!(search.reinforcement, None);
    assert_eq!(search.pending_extraction, 0);
    assert!(search.degraded.is_empty());
    assert_eq!(search.next, "");

    let deleted: DeleteResponse =
        serde_json::from_value(json!({"tombstoned": true, "version": 1})).unwrap();
    assert!(deleted.found);
    let absent: DeleteResponse =
        serde_json::from_value(json!({"tombstoned": false, "version": 1, "found": false})).unwrap();
    assert!(!absent.found);
}

#[test]
fn unknown_response_fields_preserve_explicit_values() {
    let saved: SetResponse = serde_json::from_value(json!({
        "version": u64::MAX, "memory_id": "row", "content_sha256": "hash",
        "status": "active", "noop": true, "merged_into_key": "canonical",
        "future_field": {"nested": true}
    }))
    .unwrap();
    assert_eq!(saved.version, u64::MAX);
    assert_eq!(saved.memory_id, "row");
    assert_eq!(saved.content_sha256, "hash");
    assert_eq!(saved.status, "active");
    assert!(saved.noop);
    assert_eq!(saved.merged_into_key, "canonical");

    let memory: GetResponse = serde_json::from_value(json!({
        "found": true, "memory_id": "row", "value": false, "version": u64::MAX,
        "memory_type": "semantic", "confidence": 0.0, "pinned": false,
        "future_field": []
    }))
    .unwrap();
    assert!(memory.found);
    assert_eq!(memory.memory_id, "row");
    assert_eq!(memory.value, json!(false));
    assert_eq!(memory.version, Some(u64::MAX));
    assert_eq!(memory.memory_type.as_deref(), Some("semantic"));
    assert_eq!(memory.confidence, Some(0.0));
    assert_eq!(memory.pinned, Some(false));

    let search: SearchResponse = serde_json::from_value(json!({
        "embedding_model": "embed", "results": [{
            "memory_key": "key", "memory_type": "semantic", "content_excerpt": "fact",
            "distance": 0.1, "similarity": 0.9, "version": u64::MAX,
            "namespace": "ns", "tags": [], "future_field": "extra"
        }],
        "namespaces_searched": ["ns"], "candidates_total": 2,
        "next_page": "page-2", "future_field": null
    }))
    .unwrap();
    assert_eq!(search.namespaces_searched, ["ns"]);
    assert_eq!(search.candidates_total, 2);
    assert_eq!(search.next_page, "page-2");
    assert_eq!(search.results.len(), 1);
    let hit = &search.results[0];
    assert_eq!(hit.memory_key, "key");
    assert_eq!(hit.memory_type, "semantic");
    assert_eq!(hit.content_excerpt, "fact");
    assert_eq!(hit.distance, 0.1);
    assert_eq!(hit.similarity, 0.9);
    assert_eq!(hit.version, Some(u64::MAX));
    assert_eq!(hit.namespace.as_deref(), Some("ns"));
    assert_eq!(hit.tags, Some(vec![]));

    let deleted: DeleteResponse = serde_json::from_value(json!({
        "tombstoned": false, "version": u64::MAX, "found": false, "future_field": 42
    }))
    .unwrap();
    assert!(!deleted.tombstoned);
    assert_eq!(deleted.version, u64::MAX);
    assert!(!deleted.found);
}

fn assert_required<T: DeserializeOwned>(payload: Value, fields: &[&str]) {
    for field in fields {
        let mut missing = payload.clone();
        missing.as_object_mut().unwrap().remove(*field);
        let error = serde_json::from_value::<T>(missing).err().unwrap();
        assert!(error
            .to_string()
            .contains(&format!("missing field `{field}`")));
    }
}

#[test]
fn required_response_fields_cannot_be_omitted() {
    assert_required::<SetResponse>(json!({"version": 1}), &["version"]);
    assert_required::<GetResponse>(json!({"found": false}), &["found"]);
    assert_required::<SearchResponse>(
        json!({"results": [], "embedding_model": "embed"}),
        &["results"],
    );
    assert_required::<SearchHit>(
        json!({
            "memory_key": "key", "memory_type": "semantic", "content_excerpt": "fact",
            "distance": 0.1, "similarity": 0.9
        }),
        &[
            "memory_key",
            "memory_type",
            "content_excerpt",
            "distance",
            "similarity",
        ],
    );
    assert_required::<DeleteResponse>(
        json!({"tombstoned": true, "version": 1}),
        &["tombstoned", "version"],
    );
}

#[test]
fn search_preserves_chunk_parent_attribution_and_decision_signals() {
    let hit: Value = serde_json::from_str(
        r#"{
            "memory_key": "chunk:conversation:0", "memory_type": "episodic",
            "content_excerpt": "Quoted evidence", "distance": 0.8, "similarity": 0.2,
            "source": "chunk", "parent_memory_key": "parent", "namespace": "other",
            "version": 18446744073709551615, "tags": ["evidence"], "below_threshold": true,
            "reinforcement_count": 1, "used_count": 2, "used_cited_count": 3,
            "harm_count": 4, "evidence_count": 5, "acted_count": 6, "pinned": true,
            "rank_score": 0.3, "temporal_anchor": "instant", "temporal_confidence": 0.7,
            "valid_at": "valid", "via_path": ["alice", "bob"], "entities": ["bob"],
            "user_id": "owner", "status": "superseded", "superseded_by": "successor",
            "superseded_at": "retired", "created_at": "created", "code_state": "changed",
            "body_truncated": true, "next": "memory.get", "source_resource_id": "resource",
            "source_class": "extracted", "verified": true, "explanation": "citation",
            "session_id": "conversation", "speaker": "alice", "about_other": true,
            "derived_from": ["origin"], "score_components": {"rank_score_pre_rerank": 0.4},
            "resolved_from": "duplicate"
    }"#,
    )
    .unwrap();
    let search: SearchResponse = serde_json::from_value(json!({
        "embedding_model": "embed", "results": [hit],
        "abstain": true, "abstain_reason": "Weak evidence", "abstain_code": "below_threshold",
        "retrieval_risk": "abstain", "reinforcement": "not_credited (no_consumer)",
        "pending_extraction": 2, "degraded": ["rerank: input_order_kept (ReadTimeout)"],
        "next": "run.complete"
    }))
    .unwrap();
    let hit = &search.results[0];
    assert_eq!(hit.source, "chunk");
    assert_eq!(hit.parent_memory_key, "parent");
    assert_eq!(hit.below_threshold, Some(true));
    assert_eq!(hit.reinforcement_count, Some(1));
    assert_eq!(hit.used_count, Some(2));
    assert_eq!(hit.used_cited_count, Some(3));
    assert_eq!(hit.harm_count, Some(4));
    assert_eq!(hit.evidence_count, Some(5));
    assert_eq!(hit.acted_count, Some(6));
    assert_eq!(hit.pinned, Some(true));
    assert_eq!(hit.rank_score, 0.3);
    assert_eq!(hit.temporal_anchor, "instant");
    assert_eq!(hit.temporal_confidence, 0.7);
    assert_eq!(hit.valid_at, "valid");
    assert_eq!(hit.via_path, ["alice", "bob"]);
    assert_eq!(hit.entities, ["bob"]);
    assert_eq!(hit.user_id, "owner");
    assert_eq!(hit.status, "superseded");
    assert_eq!(hit.superseded_by.as_deref(), Some("successor"));
    assert_eq!(hit.superseded_at.as_deref(), Some("retired"));
    assert_eq!(hit.created_at, "created");
    assert_eq!(hit.code_state, "changed");
    assert!(hit.body_truncated);
    assert_eq!(hit.next, "memory.get");
    assert_eq!(hit.source_resource_id, "resource");
    assert_eq!(hit.source_class, "extracted");
    assert!(hit.verified);
    assert_eq!(hit.explanation, "citation");
    assert_eq!(hit.session_id, "conversation");
    assert_eq!(hit.speaker, "alice");
    assert!(hit.about_other);
    assert_eq!(hit.derived_from, ["origin"]);
    assert_eq!(hit.score_components["rank_score_pre_rerank"], 0.4);
    assert_eq!(hit.resolved_from.as_deref(), Some("duplicate"));
    assert!(search.abstain);
    assert_eq!(search.abstain_reason, "Weak evidence");
    assert_eq!(search.abstain_code, "below_threshold");
    assert_eq!(search.retrieval_risk, "abstain");
    assert_eq!(
        search.reinforcement.as_deref(),
        Some("not_credited (no_consumer)")
    );
    assert_eq!(search.pending_extraction, 2);
    assert_eq!(search.degraded, ["rerank: input_order_kept (ReadTimeout)"]);
    assert_eq!(search.next, "run.complete");
}

#[test]
fn optional_search_disclosures_distinguish_absence_from_false_and_zero() {
    for nullable in [
        json!({}),
        json!({
            "below_threshold": null, "tags": null, "reinforcement_count": null,
            "used_count": null, "used_cited_count": null, "harm_count": null,
            "evidence_count": null, "acted_count": null, "pinned": null,
            "superseded_by": null, "superseded_at": null, "resolved_from": null
        }),
    ] {
        let mut payload = json!({
            "memory_key": "key", "memory_type": "semantic", "content_excerpt": "fact",
            "distance": 0.1, "similarity": 0.9
        });
        payload
            .as_object_mut()
            .unwrap()
            .extend(nullable.as_object().unwrap().clone());
        let hit: SearchHit = serde_json::from_value(payload).unwrap();
        assert_eq!(hit.below_threshold, None);
        assert_eq!(hit.tags, None);
        assert_eq!(hit.reinforcement_count, None);
        assert_eq!(hit.used_count, None);
        assert_eq!(hit.used_cited_count, None);
        assert_eq!(hit.harm_count, None);
        assert_eq!(hit.evidence_count, None);
        assert_eq!(hit.acted_count, None);
        assert_eq!(hit.pinned, None);
        assert_eq!(hit.superseded_by, None);
        assert_eq!(hit.superseded_at, None);
        assert_eq!(hit.resolved_from, None);
    }
    let hit: SearchHit = serde_json::from_value(json!({
        "memory_key": "key", "memory_type": "semantic", "content_excerpt": "fact",
        "distance": 0.1, "similarity": 0.9, "below_threshold": false,
        "reinforcement_count": 0, "pinned": false, "resolved_from": ""
    }))
    .unwrap();
    assert_eq!(hit.below_threshold, Some(false));
    assert_eq!(hit.reinforcement_count, Some(0));
    assert_eq!(hit.pinned, Some(false));
    assert_eq!(hit.resolved_from.as_deref(), Some(""));
    let search: SearchResponse = serde_json::from_value(json!({
        "embedding_model": "embed", "results": [], "reinforcement": null
    }))
    .unwrap();
    assert_eq!(search.reinforcement, None);
}
