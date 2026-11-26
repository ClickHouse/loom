use clickhouse_loom::{SearchRequest, SetRequest};
use serde_json::{json, to_value};

#[test]
fn constructors_omit_unset_options() {
    assert_eq!(
        to_value(SetRequest::new("key", json!({"count": u64::MAX}))).unwrap(),
        json!({"memory_key": "key", "value": {"count": u64::MAX}})
    );
    assert_eq!(
        to_value(SearchRequest::new("query")).unwrap(),
        json!({"query": "query"})
    );
}

#[test]
fn set_chain_preserves_wire_values_and_last_assignment() {
    let request = SetRequest::new("key", false)
        .memory_type(String::from("semantic"))
        .tags(["first"])
        .tags(Vec::<String>::new())
        .ttl_seconds(60)
        .ttl_seconds(0)
        .confidence(0.9)
        .confidence(0.0)
        .if_match_sha256("");
    assert_eq!(
        to_value(request).unwrap(),
        json!({
            "memory_key": "key", "value": false, "memory_type": "semantic",
            "tags": [], "ttl_seconds": 0, "confidence": 0.0, "if_match_sha256": ""
        })
    );
}

#[test]
fn search_chain_preserves_filters_and_agent_wire_name() {
    let request = SearchRequest::new("query")
        .top_k(10)
        .top_k(0)
        .tags(vec![String::from("first"), String::from("second")])
        .memory_type("semantic")
        .search_mode(String::from("lexical"))
        .agent_id("*")
        .agent_id("reader")
        .namespace_scope("all_mine")
        .user_id("")
        .session_id("conversation");
    assert_eq!(
        to_value(request).unwrap(),
        json!({
            "query": "query", "top_k": 0, "tags": ["first", "second"],
            "memory_type": "semantic", "search_mode": "lexical", "agent": "reader",
            "namespace_scope": "all_mine", "user_id": "", "session_id": "conversation"
        })
    );
}

#[test]
fn public_fields_can_clear_chained_options() {
    let mut set = SetRequest::new("key", "value").tags(["tag"]);
    set.tags = None;
    assert_eq!(
        to_value(set).unwrap(),
        json!({"memory_key": "key", "value": "value"})
    );
    let mut search = SearchRequest::new("query")
        .tags(std::iter::empty::<&str>())
        .agent_id("reader");
    search.agent_id = None;
    assert_eq!(
        to_value(search).unwrap(),
        json!({"query": "query", "tags": []})
    );
}
