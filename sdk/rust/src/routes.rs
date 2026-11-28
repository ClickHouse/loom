//! Requests to the routes the server serves outside the op registry.

use std::{
    fmt,
    time::{Duration, Instant},
};

pub use reqwest::Method;
use reqwest::{header::HeaderMap, header::CONTENT_TYPE};
use serde::{de::DeserializeOwned, Serialize};
use serde_json::Value;

use crate::client::{pacing_delay, request_id, send};
use crate::{ApiError, CallFailure, CallMetadata, CallOptions, CallResult, Client, Error};

#[derive(Clone, Debug)]
enum Body {
    Empty,
    Json(Value),
    Raw {
        content_type: String,
        bytes: Vec<u8>,
    },
    Unencodable(String),
}

/// A request to a route the op registry does not hold, such as `/v1/whoami`
/// or `/v1/knowledge.upload`.
///
/// The body and query go out as given: no org, namespace, agent or consumer is
/// added.
#[derive(Clone)]
#[non_exhaustive]
pub struct RouteRequest {
    method: Method,
    path: String,
    query: Vec<(String, String)>,
    body: Body,
}

/// A route may carry a secret in its query or body, so neither is printed.
impl fmt::Debug for RouteRequest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let body = match &self.body {
            Body::Empty => "none".to_owned(),
            Body::Json(value) => format!("json {} bytes", value.to_string().len()),
            Body::Raw {
                content_type,
                bytes,
            } => {
                format!("{content_type} {} bytes", bytes.len())
            }
            Body::Unencodable(_) => "unencodable".to_owned(),
        };
        f.debug_struct("RouteRequest")
            .field("method", &self.method)
            .field("path", &self.path.split(['?', '#']).next().unwrap_or(""))
            .field("query", &self.query.len())
            .field("body", &body)
            .finish_non_exhaustive()
    }
}

impl RouteRequest {
    /// `path` is a path on the server the client is bound to, starting with `/`.
    pub fn new(method: Method, path: impl Into<String>) -> Self {
        Self {
            method,
            path: path.into(),
            query: Vec::new(),
            body: Body::Empty,
        }
    }

    pub fn get(path: impl Into<String>) -> Self {
        Self::new(Method::GET, path)
    }

    pub fn post(path: impl Into<String>) -> Self {
        Self::new(Method::POST, path)
    }

    pub fn delete(path: impl Into<String>) -> Self {
        Self::new(Method::DELETE, path)
    }

    /// Send a JSON body. A body that cannot be encoded fails the request
    /// with `Error::Input`.
    pub fn json(mut self, body: &impl Serialize) -> Self {
        self.body = match serde_json::to_value(body) {
            Ok(value) => Body::Json(value),
            Err(error) => Body::Unencodable(error.to_string()),
        };
        self
    }

    /// Add one query parameter, after those already set.
    pub fn query(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.query.push((name.into(), value.into()));
        self
    }

    /// Send raw bytes with their media type.
    pub fn bytes(mut self, content_type: impl Into<String>, bytes: Vec<u8>) -> Self {
        self.body = Body::Raw {
            content_type: content_type.into(),
            bytes,
        };
        self
    }
}

/// The complete answer of a route outside the op registry.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct RouteResponse {
    pub status: u16,
    pub headers: HeaderMap,
    pub bytes: Vec<u8>,
}

impl Client {
    /// Send a request to a route outside the op registry and read its whole
    /// response. A status below 400 is a response; any other is `Error::Api`.
    ///
    /// Only `CallOptions::timeout` and `CallOptions::request_id` apply. The
    /// SDK resends only on a 429, after the server's hint and within the
    /// client's pacing budget. A refused HTTP/2 stream returns a transport
    /// error without a retry.
    pub async fn request(&self, request: RouteRequest) -> Result<RouteResponse, Error> {
        self.request_with_options(request, CallOptions::default())
            .await
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// Request with per-request controls and final-attempt metadata.
    pub async fn request_with_options(
        &self,
        request: RouteRequest,
        options: CallOptions,
    ) -> Result<CallResult<RouteResponse>, CallFailure> {
        options.validate(false)?;
        let RouteRequest {
            method,
            path,
            query,
            body,
        } = request;
        let not_a_path = || Error::Input("not a path on this server".into());
        // The credential goes to this server only. A second leading slash, or
        // its backslash form, names another authority.
        if !path.starts_with('/') || path.starts_with("//") {
            return Err(not_a_path().into());
        }
        let relative = &path[1..];
        let first_segment = relative.split(['/', '\\', '?', '#']).next().unwrap_or("");
        // A leading dot segment keeps a colon in the first segment from naming a scheme.
        let resolved = if first_segment.contains(':') {
            self.base_url.join(&format!(".{path}"))
        } else {
            self.base_url.join(relative)
        }
        .map_err(|_| not_a_path())?;
        if resolved.origin() != self.base_url.origin() {
            return Err(not_a_path().into());
        }
        let mut url = resolved;
        if !query.is_empty() {
            url.query_pairs_mut().extend_pairs(query);
        }
        // Logs name the path on the wire, never a query carried in the path.
        let route = url.path().to_owned();
        let request_id = options.request_id.unwrap_or_else(request_id);
        let started = Instant::now();
        let mut attempts = 0_u64;
        let mut paced = Duration::ZERO;
        loop {
            attempts += 1;
            let builder = self.http.request(method.clone(), url.clone());
            let builder = match &body {
                Body::Empty => builder,
                Body::Json(value) => builder.json(value),
                Body::Raw {
                    content_type,
                    bytes,
                } => builder
                    .header(CONTENT_TYPE, content_type)
                    .body(bytes.clone()),
                Body::Unencodable(reason) => {
                    return Err(
                        Error::Input(format!("cannot encode request body: {reason}")).into(),
                    );
                }
            };
            let request = self.identified(builder, &request_id, options.timeout);
            let mut metadata = CallMetadata::new(&request_id, attempts);
            log::debug!(target: "clickhouse_loom", "{route} attempt={attempts} rid={request_id}");
            let transport = |error: reqwest::Error, metadata: CallMetadata| {
                log::error!(target: "clickhouse_loom", "{route} failed status=0 elapsed_ms={} attempts={attempts} rid={request_id}", started.elapsed().as_millis());
                CallFailure {
                    error: Error::Transport(error.without_url()),
                    metadata: Some(Box::new(metadata)),
                }
            };
            let (status, headers, bytes) = match send(request, &mut metadata).await {
                Ok((status, headers, response)) => match response.bytes().await {
                    Ok(bytes) => (status, headers, bytes.to_vec()),
                    Err(error) => return Err(transport(error, metadata)),
                },
                Err(error) => return Err(transport(error, metadata)),
            };
            if status.as_u16() < 400 {
                log::info!(target: "clickhouse_loom", "{route} status={} elapsed_ms={} attempts={attempts} rid={request_id}", status.as_u16(), started.elapsed().as_millis());
                return Ok(CallResult {
                    data: RouteResponse {
                        status: status.as_u16(),
                        headers,
                        bytes,
                    },
                    metadata,
                });
            }
            let mut error = ApiError::from_response(status.as_u16(), &headers, &bytes);
            error.request_id.get_or_insert_with(|| request_id.clone());
            if status.as_u16() == 429 && self.pacing {
                let delay = pacing_delay(
                    error.retry_after_s,
                    self.ratelimit_max_wait.saturating_sub(paced),
                );
                if !delay.is_zero() {
                    log::warn!(target: "clickhouse_loom", "{route} status=429 wait_ms={} attempts={attempts} rid={request_id}", delay.as_millis());
                    tokio::time::sleep(delay).await;
                    paced += delay;
                    continue;
                }
            }
            log::error!(target: "clickhouse_loom", "{route} failed status={} elapsed_ms={} attempts={attempts} rid={request_id}", status.as_u16(), started.elapsed().as_millis());
            return Err(CallFailure {
                error: Error::Api(Box::new(error)),
                metadata: Some(Box::new(metadata)),
            });
        }
    }

    /// [`Client::request`], decoding the route's JSON body as `T`. An empty body
    /// decodes as an empty object.
    pub async fn request_json<T: DeserializeOwned>(
        &self,
        request: RouteRequest,
    ) -> Result<T, Error> {
        self.request_json_with_options(request, CallOptions::default())
            .await
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// `request_json` with per-request controls and final-attempt metadata.
    pub async fn request_json_with_options<T: DeserializeOwned>(
        &self,
        request: RouteRequest,
        options: CallOptions,
    ) -> Result<CallResult<T>, CallFailure> {
        let route = request
            .path
            .split(['?', '#'])
            .next()
            .unwrap_or_default()
            .to_owned();
        let CallResult { data, metadata } = self.request_with_options(request, options).await?;
        let source = if data.bytes.is_empty() {
            &b"{}"[..]
        } else {
            &data.bytes[..]
        };
        match serde_json::from_slice(source) {
            Ok(data) => Ok(CallResult { data, metadata }),
            Err(source) => {
                log::error!(target: "clickhouse_loom", "{route} invalid_response status={} attempts={} rid={}", data.status, metadata.attempts, metadata.request_id);
                Err(CallFailure {
                    error: Error::Protocol {
                        status: data.status,
                        source,
                    },
                    metadata: Some(Box::new(metadata)),
                })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn route_request_keeps_what_it_was_given() {
        let request = RouteRequest::post("/v1/knowledge.upload")
            .query("filename", "guide notes.pdf")
            .query("sha256", "abc")
            .bytes("application/pdf", b"%PDF-1.7".to_vec());
        assert_eq!(request.method, Method::POST);
        assert_eq!(request.path, "/v1/knowledge.upload");
        assert_eq!(
            request.query,
            [
                ("filename".to_owned(), "guide notes.pdf".to_owned()),
                ("sha256".to_owned(), "abc".to_owned())
            ]
        );
        assert!(matches!(request.body, Body::Raw { .. }));
        assert_eq!(RouteRequest::get("/v1/whoami").method, Method::GET);
        assert_eq!(RouteRequest::delete("/mcp").method, Method::DELETE);
    }

    #[test]
    fn debug_output_omits_the_query_and_the_body() {
        let request = RouteRequest::post("/v1/me/service_accounts.create?token=sk-1")
            .query("secret", "s3cret")
            .json(&serde_json::json!({"password": "hunter2"}));
        let printed = format!("{request:?}");
        assert!(
            printed.contains("/v1/me/service_accounts.create"),
            "{printed}"
        );
        for secret in ["sk-1", "s3cret", "hunter2", "?"] {
            assert!(!printed.contains(secret), "{printed}");
        }
    }
}
