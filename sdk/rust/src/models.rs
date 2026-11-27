use serde::{Deserialize, Serialize};
use serde_json::Value;

/// A memory write. Unset options use the server's defaults.
#[derive(Debug, Serialize)]
#[non_exhaustive]
pub struct SetRequest {
    pub memory_key: String,
    pub value: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memory_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tags: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ttl_seconds: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub if_match_sha256: Option<String>,
}

impl SetRequest {
    pub fn new(memory_key: impl Into<String>, value: impl Into<Value>) -> Self {
        Self {
            memory_key: memory_key.into(),
            value: value.into(),
            memory_type: None,
            tags: None,
            ttl_seconds: None,
            confidence: None,
            if_match_sha256: None,
        }
    }
}

/// A search addressed to the client's namespace, across agents by default.
#[derive(Debug, Serialize)]
#[non_exhaustive]
pub struct SearchRequest {
    pub query: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub top_k: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tags: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memory_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub search_mode: Option<String>,
    #[serde(rename = "agent", skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub namespace_scope: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub user_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
}

impl SearchRequest {
    pub fn new(query: impl Into<String>) -> Self {
        Self {
            query: query.into(),
            top_k: None,
            tags: None,
            memory_type: None,
            search_mode: None,
            agent_id: None,
            namespace_scope: None,
            user_id: None,
            session_id: None,
        }
    }
}

#[derive(Debug, Deserialize)]
#[non_exhaustive]
pub struct SetResponse {
    pub version: u64,
    #[serde(default)]
    pub memory_id: String,
    #[serde(default)]
    pub content_sha256: String,
    #[serde(default)]
    pub status: String,
    /// A merge or dry run can succeed without writing a row.
    #[serde(default)]
    pub noop: bool,
    #[serde(default)]
    pub merged_into_key: String,
}

/// A missing memory is a successful response with `found == false`.
#[derive(Debug, Deserialize)]
#[non_exhaustive]
pub struct GetResponse {
    pub found: bool,
    #[serde(default)]
    pub memory_id: String,
    #[serde(default)]
    pub value: Value,
    pub version: Option<u64>,
    pub memory_type: Option<String>,
    pub confidence: Option<f64>,
    pub pinned: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[non_exhaustive]
pub struct SearchHit {
    pub memory_key: String,
    pub memory_type: String,
    pub content_excerpt: String,
    pub distance: f64,
    pub similarity: f64,
    pub version: Option<u64>,
    pub namespace: Option<String>,
    pub tags: Option<Vec<String>>,
    /// None means the server did not disclose the per-hit threshold decision.
    pub below_threshold: Option<bool>,
    /// True on a governed definition; None when the server does not disclose it.
    pub governed: Option<bool>,
    pub reinforcement_count: Option<u64>,
    pub used_count: Option<u64>,
    pub used_cited_count: Option<u64>,
    pub harm_count: Option<u64>,
    pub evidence_count: Option<u64>,
    pub acted_count: Option<u64>,
    pub pinned: Option<bool>,
    #[serde(default)]
    pub rank_score: f64,
    #[serde(default)]
    pub temporal_anchor: String,
    #[serde(default)]
    pub temporal_confidence: f64,
    #[serde(default)]
    pub valid_at: String,
    #[serde(default)]
    pub via_path: Vec<String>,
    #[serde(default)]
    pub entities: Vec<String>,
    /// Writer of the returned row, empty when no row identity is available.
    #[serde(default)]
    pub agent: String,
    #[serde(default)]
    pub user_id: String,
    #[serde(default = "default_source")]
    pub source: String,
    /// A chunk's parent key, usable with Client::get and for recall credit.
    #[serde(default)]
    pub parent_memory_key: String,
    #[serde(default)]
    pub status: String,
    pub superseded_by: Option<String>,
    pub superseded_at: Option<String>,
    #[serde(default)]
    pub created_at: String,
    #[serde(default)]
    pub code_state: String,
    #[serde(default)]
    pub body_truncated: bool,
    #[serde(default)]
    pub next: String,
    #[serde(default)]
    pub source_resource_id: String,
    #[serde(default)]
    pub source_class: String,
    #[serde(default)]
    pub verified: bool,
    #[serde(default)]
    pub explanation: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub speaker: String,
    #[serde(default)]
    pub about_other: bool,
    #[serde(default)]
    pub derived_from: Vec<String>,
    #[serde(default)]
    pub score_components: std::collections::HashMap<String, f64>,
    pub resolved_from: Option<String>,
}

#[derive(Debug, Deserialize)]
#[non_exhaustive]
pub struct SearchResponse {
    pub results: Vec<SearchHit>,
    #[serde(default)]
    pub namespaces_searched: Vec<String>,
    #[serde(default)]
    pub candidates_total: u64,
    #[serde(default)]
    pub next_page: String,
    /// True when the returned hits do not support an answer.
    #[serde(default)]
    pub abstain: bool,
    #[serde(default)]
    pub abstain_reason: String,
    #[serde(default)]
    pub abstain_code: String,
    /// Empty when the response omits the risk label.
    #[serde(default)]
    pub retrieval_risk: String,
    /// Status of the governed-definition retrieval route, empty when undisclosed.
    #[serde(default)]
    pub term_leg: String,
    /// None means recall credit was not disclosed.
    pub reinforcement: Option<String>,
    /// Number of saves whose extraction is still pending.
    #[serde(default)]
    pub pending_extraction: u64,
    #[serde(default)]
    pub degraded: Vec<String>,
    #[serde(default)]
    pub next: String,
}

/// Deletion writes a tombstone; it does not physically erase stored data.
#[derive(Debug, Deserialize)]
#[non_exhaustive]
pub struct DeleteResponse {
    pub tombstoned: bool,
    pub version: u64,
    #[serde(default = "default_true")]
    pub found: bool,
}

fn default_true() -> bool {
    true
}

fn default_source() -> String {
    "fact".into()
}

macro_rules! option_setter {
    ($name:ident, $ty:ty) => {
        pub fn $name(mut self, value: $ty) -> Self {
            self.$name = Some(value);
            self
        }
    };
}

macro_rules! string_setter {
    ($name:ident) => {
        pub fn $name(mut self, value: impl Into<String>) -> Self {
            self.$name = Some(value.into());
            self
        }
    };
}

macro_rules! tags_setter {
    () => {
        pub fn tags(mut self, tags: impl IntoIterator<Item = impl Into<String>>) -> Self {
            self.tags = Some(tags.into_iter().map(Into::into).collect());
            self
        }
    };
}

impl SetRequest {
    string_setter!(memory_type);
    tags_setter!();
    option_setter!(ttl_seconds, i64);
    option_setter!(confidence, f64);
    string_setter!(if_match_sha256);

    pub async fn send(
        self,
        client: &crate::Client,
        options: crate::CallOptions,
    ) -> Result<crate::CallResult<SetResponse>, crate::CallFailure> {
        client.set_with_options(self, options).await
    }
}

impl SearchRequest {
    option_setter!(top_k, u32);
    tags_setter!();
    string_setter!(memory_type);
    string_setter!(search_mode);
    string_setter!(agent_id);
    string_setter!(namespace_scope);
    string_setter!(user_id);
    string_setter!(session_id);

    pub async fn send(
        self,
        client: &crate::Client,
        options: crate::CallOptions,
    ) -> Result<crate::CallResult<SearchResponse>, crate::CallFailure> {
        client.search_with_options(self, options).await
    }
}
