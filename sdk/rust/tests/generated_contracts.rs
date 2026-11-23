use clickhouse_loom::{
    CodeLinkState, Integer, MemoryDeleteOutput as DeleteResponse, MemoryGetInput as GetRequest,
    MemoryGetOutput as GetResponse, MemorySearchInput as SearchRequest,
    MemorySearchOutput as SearchResponse, MemorySetInput as SetRequest,
    MemorySetInputOnExpire as SetRequestOnExpire, MemorySetOutput as SetResponse,
};
use serde_json::{from_value, json, to_value};

#[test]
fn nullable_requests_preserve_omission_null_and_value() {
    let omitted = SetRequest::new("key", "value");
    assert!(to_value(&omitted).unwrap().get("ttl_seconds").is_none());
    assert_eq!(omitted.ttl_seconds, None);
    let null = omitted.ttl_seconds_null();
    assert_eq!(null.ttl_seconds, Some(None));
    assert_eq!(to_value(&null).unwrap()["ttl_seconds"], json!(null));
    let roundtrip: SetRequest = from_value(to_value(null).unwrap()).unwrap();
    assert_eq!(roundtrip.ttl_seconds, Some(None));
    let value = roundtrip.ttl_seconds(0);
    assert_eq!(value.ttl_seconds, Some(Some(0)));
    assert_eq!(to_value(value).unwrap()["ttl_seconds"], 0);
    let search: SearchRequest = from_value(json!({
        "query": "q", "user_id": null, "explain": false
    }))
    .unwrap();
    assert_eq!(search.user_id, Some(None));
    assert_eq!(search.explain, Some(Some(false)));
    assert_eq!(search.session_id, None);
}

#[test]
fn nonnullable_optional_requests_reject_null() {
    for field in ["tags", "confidence", "memory_type", "pinned"] {
        let mut payload = json!({"memory_key": "key", "value": false});
        payload[field] = json!(null);
        assert!(from_value::<SetRequest>(payload).is_err(), "{field}");
    }
    assert!(from_value::<GetRequest>(json!({"memory_key": "key", "follow": null})).is_err());
}

#[test]
fn complete_requests_include_tuples_enums_and_reader_controls() {
    let write = SetRequest::new("key", json!({"counter": u64::MAX}))
        .relations(vec![("alice".into(), "knows".into(), "bob".into())])
        .on_expire(SetRequestOnExpire::Demote)
        .clear_expiry(true)
        .files_null()
        .char_limit(1024)
        .dry_run(true);
    let payload = to_value(write).unwrap();
    assert_eq!(payload["relations"], json!([["alice", "knows", "bob"]]));
    assert_eq!(payload["on_expire"], "demote");
    assert_eq!(payload["files"], json!(null));
    assert_eq!(payload["char_limit"], 1024);
    assert!(from_value::<SetRequest>(json!({
        "memory_key": "key", "value": false, "relations": [["alice", "knows"]]
    }))
    .is_err());
    let get = to_value(
        GetRequest::new("key")
            .follow(true)
            .fields(["value", "tags"]),
    )
    .unwrap();
    assert_eq!(
        get,
        json!({"memory_key": "key", "follow": true, "fields": ["value", "tags"]})
    );
}

#[test]
fn complete_responses_follow_server_defaults_and_nested_models() {
    let memory: GetResponse = from_value(json!({
        "found": false, "code": [{
            "code_namespace": "code", "path": "main.rs", "state": "changed"
        }]
    }))
    .unwrap();
    assert!(!memory.pinned);
    assert!(memory.tags.is_empty());
    assert_eq!(memory.code.len(), 1);
    assert_eq!(memory.code[0].state, CodeLinkState::Changed);
    assert_eq!(memory.verification, "none");
    let saved: SetResponse = from_value(json!({
        "version": u64::MAX, "advisories": ["review"], "chunks_written": 2
    }))
    .unwrap();
    assert_eq!(saved.advisories, ["review"]);
    assert_eq!(saved.chunks_written.to_string(), "2");
    let deleted: DeleteResponse = from_value(json!({"version": 1, "tombstoned": true})).unwrap();
    assert_eq!(deleted.dependents_tombstoned.to_string(), "0");
    let search: SearchResponse = from_value(json!({
        "results": [], "embedding_model": "embed",
        "retrieval_work": {"rows": u64::MAX}, "derived_aggregate": {"value": false}
    }))
    .unwrap();
    assert_eq!(search.retrieval_risk, "abstain");
    assert_eq!(search.term_leg, "off");
    assert_eq!(search.retrieval_work["rows"], json!(u64::MAX));
    assert!(from_value::<SearchResponse>(json!({"results": []})).is_err());
}

#[test]
fn integer_tokens_preserve_unbounded_precision_and_reject_fractions() {
    let token = "18446744073709551616000000000000000000000000";
    let value: Integer = serde_json::from_str(token).unwrap();
    assert_eq!(serde_json::to_string(&value).unwrap(), token);
    assert_eq!(value.to_string(), token);
    for token in ["1.5", "1e2", "true", "\"1\""] {
        assert!(serde_json::from_str::<Integer>(token).is_err(), "{token}");
    }
}

#[test]
fn numeric_unions_decode_json_text_and_preserve_integer_precision() {
    use clickhouse_loom::{QueryFilterValue, QueryFilterValuesItem, RecordFieldInputValue};
    for token in ["1", "1.5", "1e2", "18446744073709551616000000000000"] {
        let expected: serde_json::Value = serde_json::from_str(token).unwrap();
        let scalar: QueryFilterValue = serde_json::from_str(token).unwrap();
        let item: QueryFilterValuesItem = serde_json::from_str(token).unwrap();
        let record: RecordFieldInputValue = serde_json::from_str(token).unwrap();
        for value in [
            to_value(scalar).unwrap(),
            to_value(item).unwrap(),
            to_value(record).unwrap(),
        ] {
            if token == "1e2" {
                assert_eq!(value.as_f64(), expected.as_f64());
            } else {
                assert_eq!(value, expected, "{token}");
            }
        }
    }
    for token in ["[]", "{}"] {
        assert!(serde_json::from_str::<QueryFilterValue>(token).is_err());
        assert!(serde_json::from_str::<QueryFilterValuesItem>(token).is_err());
        assert!(serde_json::from_str::<RecordFieldInputValue>(token).is_err());
    }
}

#[test]
fn record_and_filter_builders_restore_fractional_json_values() {
    use clickhouse_loom::{
        QueryFilter, QueryFilterValue, QueryFilterValuesItem, RecordFieldInput,
        RecordFieldInputValue,
    };
    let record = RecordFieldInput::new().value(RecordFieldInputValue::Variant2(1.5));
    let payload = serde_json::to_string(&record).unwrap();
    let restored: RecordFieldInput = serde_json::from_str(&payload).unwrap();
    assert_eq!(to_value(restored).unwrap(), to_value(record).unwrap());
    let filter = QueryFilter::new(
        clickhouse_loom::QueryColumn::new("amount"),
        clickhouse_loom::QueryFilterOp::Eq,
    )
    .value(QueryFilterValue::Variant2(1.5))
    .values(vec![QueryFilterValuesItem::Variant2(100.0)]);
    let payload = serde_json::to_string(&filter).unwrap();
    let restored: QueryFilter = serde_json::from_str(&payload).unwrap();
    assert_eq!(to_value(restored).unwrap(), to_value(filter).unwrap());
}

#[test]
fn boolean_constants_reject_other_json_values() {
    use clickhouse_loom::LiveSearchResultsLive;
    let live: LiveSearchResultsLive = from_value(json!(true)).unwrap();
    assert_eq!(to_value(live).unwrap(), json!(true));
    for value in [json!(false), json!(null), json!(1), json!("true")] {
        assert!(from_value::<LiveSearchResultsLive>(value).is_err());
    }
}
