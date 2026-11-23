//! Use ClickHouse Loom's memory, knowledge and context operations from Rust.
//!
//! Loom is a self-hosted server for shared long-term AI agent memory, document
//! search and code indexing. [`Client`] provides typed async methods for every
//! registered operation. Method names replace dots with underscores.
//! [`Client::call`] accepts a serializable request and a typed response,
//! including `serde_json::Value`.
//!
//! Identity is bound once on [`Client`]. Calls return typed responses, and
//! [`Error::Api`] preserves the server's error classification.
//! Search defaults to all agents with no user or session filter. Set filters on
//! [`SearchRequest`] explicitly. Builder identity values apply to declared identity
//! fields; optional reader filters keep the values in the request.
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
//! Retries after dispatch require a known, declared idempotent operation.
//! Unknown operations receive bound org and namespace and retry only connection
//! failures. Operation names must be dotted identifiers.
//!
//! Enable the `blocking` feature for a synchronous client in the `blocking` module.
//! It runs the async transport on a dedicated thread and rejects calls on Tokio
//! runtime threads; use the async client there or call from a separate OS thread.
//!
//! # Capability probe
//!
//! [`Client::probe`] fetches fresh capabilities and reports missing operations
//! and request fields. A mismatch returns [`ProbeReport`] with `ok = false`;
//! HTTP and transport failures return [`Error`].
//!
//! ```
//! use clickhouse_loom::{Client, Error, ProbeRequest};
//!
//! # async fn example(client: Client) -> Result<(), Error> {
//! let report = client.probe(
//!     ProbeRequest::new()
//!         .ops(["memory.set", "memory.search"])
//!         .fields("memory.set", ["tags"]),
//! ).await?;
//! println!("{}: {:?}", report.ok, report.missing_ops);
//! # Ok(())
//! # }
//! ```
//!
//! # Per-call options and metadata
//!
//! [`CallOptions`] overrides timeout, retry policy and request identifiers for
//! one call. The `*_with_options` methods return [`CallResult`] or [`CallFailure`].
//! Both carry final-attempt metadata; validation failures have no attempt metadata.
//! The original methods return their typed response and [`Error`].
//!
//! ```
//! use clickhouse_loom::{CallFailure, CallOptions, Client, RetryPolicy};
//! use std::time::Duration;
//!
//! # async fn example(client: Client) -> Result<(), CallFailure> {
//! let options = CallOptions::default()
//!     .timeout(Duration::from_secs(5))
//!     .retry(RetryPolicy::disabled())
//!     .request_id("recall-1");
//! let result = client.get_with_options("preference", options).await?;
//! assert_eq!(result.metadata.sent_request_id, "recall-1");
//! println!("{} attempts={}", result.metadata.request_id, result.metadata.attempts);
//! # Ok(())
//! # }
//! ```
//!
//! # Using public types
//!
//! Request, response and API error structs are non-exhaustive.
//! Construct requests with `new`, then set their public options. Destructure responses
//! with `..`. Match errors with a fallback arm and [`Error::Protocol`] with `..`.
//!
//! Response decoding ignores unknown JSON fields. Missing required fields produce
//! [`Error::Protocol`].
//! The memory facade preserves its public types and decoding defaults.
//! Generated methods use full wire contracts such as [`MemorySetInput`] and
//! [`MemoryGetOutput`], without changing [`SetRequest`] or [`GetResponse`].
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

mod call;
mod capabilities;
mod client;
mod error;
mod models;
mod operations;
mod retry;
mod wire_models;

#[cfg(feature = "blocking")]
pub mod blocking;
#[cfg(feature = "blocking")]
mod blocking_operations;

pub use call::{CallFailure, CallMetadata, CallOptions, CallResult, RateLimit};
pub use capabilities::{ProbeReport, ProbeRequest};
pub use client::{Client, ClientBuilder};
pub use error::{ApiError, Error};
pub use models::*;
pub use retry::{RetryPolicy, RetryTimeouts};
pub use wire_models::*;
