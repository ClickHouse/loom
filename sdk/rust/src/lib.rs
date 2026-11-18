//! Use ClickHouse Loom's memory operations from Rust.
//!
//! Loom is a self-hosted server for shared long-term AI agent memory, document
//! search and code indexing. This crate exposes async `set`, `get`, `search` and
//! `delete` operations through [`Client`].
//!
//! Identity is bound once on [`Client`]. Calls return typed responses, and
//! [`Error::Api`] preserves the server's error classification.
//! Search defaults to all agents with no user or session filter. Set filters on
//! [`SearchRequest`] explicitly; builder user and session values apply to other calls.
//! The default timeout covers each complete HTTP attempt for 60 seconds.
//! Search hits preserve chunk parent keys and provenance. Check [`SearchResponse`]
//! abstention, degradation and pending extraction before answering from the hits.
//! Calls use the application's Tokio runtime. Cloned clients share a connection pool.
//! [`RetryPolicy`] defaults to three attempts on connection failures, HTTP 408 and
//! 5xx, with exponential backoff. Writes reuse an idempotency key and can retry
//! incomplete responses; reads do not retry those failures by default.
//! 429 pacing has a separate 15 second sleep budget. Use
//! [`ClientBuilder::pacing`] and [`ClientBuilder::retry`] to configure both.
//! [`Client::with_retry`] creates a policy override sharing connections and rate state.
//! Dropping a call future cancels its retries and waits, but cannot undo a write.
//! Logs use the `log` facade without installing a logger or recording payloads.
//!
//! # Using public types
//!
//! Request, response and API error structs are non-exhaustive.
//! Construct requests with `new`, then set their public options. Destructure responses
//! with `..`. Match errors with a fallback arm and [`Error::Protocol`] with `..`.
//!
//! Response decoding ignores unknown JSON fields. Missing required fields produce
//! [`Error::Protocol`].
//!
//! ```
//! use clickhouse_loom::{ApiError, Error, GetResponse, SearchRequest, SetRequest};
//!
//! let mut write = SetRequest::new("preference", "Concise answers");
//! write.tags = Some(vec!["style".into()]);
//! let mut search = SearchRequest::new("answer style");
//! search.top_k = Some(10);
//!
//! fn found(response: GetResponse) -> bool {
//!     let GetResponse { found, .. } = response;
//!     found
//! }
//!
//! fn describe(error: Error) -> String {
//!     match error {
//!         Error::Api(api) => {
//!             let ApiError { code, message, .. } = *api;
//!             format!("{code}: {message}")
//!         }
//!         Error::Protocol { status, .. } => format!("Invalid response: {status}"),
//!         other => other.to_string(),
//!     }
//! }
//! ```
//!
//! Request structs cannot be constructed with a struct literal outside this crate:
//!
//! ```compile_fail,E0639
//! use clickhouse_loom::SearchRequest;
//!
//! let request = SearchRequest {
//!     query: "answer style".into(),
//!     top_k: None,
//!     tags: None,
//!     memory_type: None,
//!     search_mode: None,
//!     agent_id: None,
//!     namespace_scope: None,
//!     user_id: None,
//!     session_id: None,
//! };
//! ```
//!
//! Match [`Error`] with a fallback arm:
//!
//! ```compile_fail,E0004
//! use clickhouse_loom::Error;
//!
//! fn describe(error: Error) -> String {
//!     match error {
//!         Error::Configuration(message) | Error::Input(message) => message,
//!         Error::Transport(source) => source.to_string(),
//!         Error::Protocol { source, .. } => source.to_string(),
//!         Error::Api(api) => api.to_string(),
//!     }
//! }
//! ```
//!
//! Destructure [`Error::Protocol`] with `..`:
//!
//! ```compile_fail,E0638
//! use clickhouse_loom::Error;
//!
//! fn describe(error: Error) -> String {
//!     match error {
//!         Error::Protocol { status, source } => format!("{status}: {source}"),
//!         other => other.to_string(),
//!     }
//! }
//! ```

mod client;
mod error;
mod models;
mod retry;

pub use client::{Client, ClientBuilder};
pub use error::{ApiError, Error};
pub use models::{
    DeleteResponse, GetResponse, SearchHit, SearchRequest, SearchResponse, SetRequest, SetResponse,
};
pub use retry::{RetryPolicy, RetryTimeouts};
