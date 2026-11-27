use std::{
    collections::HashMap,
    env, fmt,
    sync::Arc,
    time::{Duration, Instant},
};

use reqwest::{
    header::{HeaderMap, HeaderValue, AUTHORIZATION},
    redirect::Policy,
    Client as HttpClient, RequestBuilder, StatusCode, Url,
};
use serde::{de::DeserializeOwned, Serialize};
use serde_json::{json, Value};

use crate::{
    ApiError, CallFailure, CallMetadata, CallOptions, CallResult, DeleteResponse, Error,
    GetResponse, RetryPolicy, SearchRequest, SearchResponse, SetRequest, SetResponse,
};
use tokio::sync::Mutex;

const DEFAULT_URL: &str = "http://localhost:7777";

macro_rules! typed_call {
    ($method:ident, $with_options:ident, $request:ty, $response:ty, $op:literal) => {
        pub async fn $method(&self, request: $request) -> Result<$response, Error> {
            self.$with_options(request, CallOptions::default())
                .await
                .map(|result| result.data)
                .map_err(|failure| failure.error)
        }

        /// Call with per-request controls and final-attempt metadata.
        pub async fn $with_options(
            &self,
            request: $request,
            options: CallOptions,
        ) -> Result<CallResult<$response>, CallFailure> {
            self.call($op, &request, options).await
        }
    };
}

/// A client for ClickHouse Loom, bound to one identity.
///
/// Clones share a connection pool and may issue requests concurrently.
#[derive(Clone)]
pub struct Client {
    pub(crate) http: HttpClient,
    pub(crate) base_url: Url,
    authorization: Option<HeaderValue>,
    identity: Value,
    retry: RetryPolicy,
    pub(crate) pacing: bool,
    pub(crate) ratelimit_max_wait: Duration,
    rate_state: Arc<Mutex<HashMap<String, Duration>>>,
}

/// Explicit endpoint and token options override LOOM_BASE_URL and LOOM_API_KEY.
pub struct ClientBuilder {
    base_url: Option<String>,
    api_key: Option<String>,
    identity: Value,
    timeout: Duration,
    retry: RetryPolicy,
    pacing: bool,
    ratelimit_max_wait: Duration,
}

impl Client {
    typed_call!(
        health,
        health_with_options,
        crate::OpsHealthInput,
        crate::OpsHealthOutput,
        "ops.health"
    );
    typed_call!(
        capabilities,
        capabilities_with_options,
        crate::OpsCapabilitiesInput,
        crate::OpsCapabilitiesOutput,
        "ops.capabilities"
    );

    pub fn builder(
        org: impl Into<String>,
        namespace: impl Into<String>,
        agent: impl Into<String>,
    ) -> ClientBuilder {
        ClientBuilder {
            base_url: None,
            api_key: None,
            identity: json!({"org": org.into(), "namespace": namespace.into(), "agent": agent.into()}),
            timeout: Duration::from_secs(60),
            retry: RetryPolicy::default(),
            pacing: true,
            ratelimit_max_wait: Duration::from_secs(15),
        }
    }

    /// Fetch capabilities and report missing operations and request fields.
    ///
    /// Each probe fetches a fresh report. Only call failures return an error.
    pub async fn probe(&self, request: crate::ProbeRequest) -> Result<crate::ProbeReport, Error> {
        self.probe_with_options(request, CallOptions::default())
            .await
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// Probe with per-request controls and final-attempt metadata.
    pub async fn probe_with_options(
        &self,
        request: crate::ProbeRequest,
        options: CallOptions,
    ) -> Result<CallResult<crate::ProbeReport>, CallFailure> {
        let result = self
            .capabilities_with_options(crate::OpsCapabilitiesInput::new(), options)
            .await?;
        Ok(CallResult {
            data: crate::capabilities::compare(request, result.data),
            metadata: result.metadata,
        })
    }

    pub async fn set(&self, request: SetRequest) -> Result<SetResponse, Error> {
        self.set_with_options(request, CallOptions::default())
            .await
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// Save memory with per-call options and response or failure metadata.
    pub async fn set_with_options(
        &self,
        request: SetRequest,
        options: CallOptions,
    ) -> Result<CallResult<SetResponse>, CallFailure> {
        self.call("memory.set", &request, options).await
    }

    pub async fn get(&self, memory_key: &str) -> Result<GetResponse, Error> {
        self.get_with_options(memory_key, CallOptions::default())
            .await
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// Read memory with per-call options and response or failure metadata.
    pub async fn get_with_options(
        &self,
        memory_key: &str,
        options: CallOptions,
    ) -> Result<CallResult<GetResponse>, CallFailure> {
        self.call(
            "memory.get",
            &crate::MemoryGetInput::new(memory_key),
            options,
        )
        .await
    }

    pub async fn search(&self, request: SearchRequest) -> Result<SearchResponse, Error> {
        self.search_with_options(request, CallOptions::default())
            .await
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// Search memory with per-call options and response or failure metadata.
    pub async fn search_with_options(
        &self,
        request: SearchRequest,
        options: CallOptions,
    ) -> Result<CallResult<SearchResponse>, CallFailure> {
        self.call("memory.search", &request, options).await
    }

    pub async fn delete(&self, memory_key: &str) -> Result<DeleteResponse, Error> {
        self.delete_with_options(memory_key, CallOptions::default())
            .await
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// Delete memory with per-call options and response or failure metadata.
    pub async fn delete_with_options(
        &self,
        memory_key: &str,
        options: CallOptions,
    ) -> Result<CallResult<DeleteResponse>, CallFailure> {
        self.call(
            "memory.delete",
            &crate::MemoryDeleteInput::new(memory_key),
            options,
        )
        .await
    }

    /// Return a client with another policy, sharing the pool and rate-limit state.
    /// Use this clone for calls needing an override; the original is unchanged.
    pub fn with_retry(&self, retry: RetryPolicy) -> Result<Self, Error> {
        retry.validate()?;
        Ok(Self {
            retry,
            ..self.clone()
        })
    }

    /// Call any dotted wire operation, decoding its response as `T`.
    ///
    /// Known operations use generated identity and retry policies. Unknown operations
    /// receive bound org/namespace and retry only failures before connection.
    pub async fn call<T: DeserializeOwned>(
        &self,
        op: &str,
        request: &impl Serialize,
        options: CallOptions,
    ) -> Result<CallResult<T>, CallFailure> {
        if !valid_operation(op) {
            return Err(Error::Input("operation must be a dotted identifier".into()).into());
        }
        let policy = crate::wire_models::OP_POLICIES
            .iter()
            .find(|(name, _, _, _, _)| *name == op);
        let idempotent = policy.is_some_and(|(_, idempotent, _, _, _)| *idempotent);
        let replayable = policy.is_some_and(|(_, idem, read_only, _, _)| *idem && !*read_only);
        options.validate(replayable)?;
        let retry = options.retry.as_ref().unwrap_or(&self.retry);
        let mut body = serde_json::to_value(request)
            .map_err(|e| Error::Input(format!("cannot encode request: {e}")))?;
        let fields = body
            .as_object_mut()
            .ok_or_else(|| Error::Input("request must be an object".into()))?;
        if op == "memory.set" && fields.get("value").is_some_and(Value::is_null) {
            return Err(Error::Input("memory.set value cannot be null".into()).into());
        }
        if op == "collection.delete"
            && fields
                .get("namespace")
                .and_then(Value::as_str)
                .is_none_or(|target| target.trim().is_empty())
        {
            return Err(
                Error::Input("collection.delete requires a target namespace".into()).into(),
            );
        }
        if op == "memory.search" {
            // Search identity fields are filters; write authorship must not narrow recall.
            fields.entry("agent").or_insert_with(|| json!("*"));
            for key in ["org", "namespace"] {
                fields.insert(key.into(), self.identity[key].clone());
            }
        }
        // Only declared identity fields are stamped; optional reader filters survive.
        let identity_fields = policy.map_or(&["org", "namespace"][..], |(_, _, _, keys, _)| *keys);
        for key in identity_fields {
            if let Some(value) = self.identity.get(*key) {
                fields.insert((*key).into(), value.clone());
            }
        }
        if let Some((_, _, _, _, defaults)) = policy {
            for key in *defaults {
                if let Some(value) = self.identity.get(*key) {
                    fields.entry(*key).or_insert_with(|| value.clone());
                }
            }
        }
        let url = self
            .base_url
            .join(&format!("v1/{op}"))
            .map_err(|_| Error::Configuration("cannot construct operation URL".into()))?;
        let idempotency_key = options
            .idempotency_key
            .or_else(|| replayable.then(request_id));
        let request_id = options.request_id.unwrap_or_else(request_id);
        if self.pacing {
            let delay = self
                .rate_state
                .lock()
                .await
                .get(op)
                .copied()
                .unwrap_or_default();
            if !delay.is_zero() {
                log::warn!(target: "clickhouse_loom", "{op} pacing wait_ms={} rid={request_id}", delay.as_millis());
                tokio::time::sleep(delay).await;
            }
        }
        let started = Instant::now();
        let mut sent = 0_u64;
        let mut attempt = 0_u32;
        let mut slept = Duration::ZERO;
        let mut paced = Duration::ZERO;
        loop {
            attempt += 1;
            let mut request = self
                .identified(
                    self.http.post(url.clone()).json(&body),
                    &request_id,
                    options.timeout,
                )
                .header("x-loom-retry-count", sent.to_string());
            if let Some(key) = &idempotency_key {
                request = request.header("idempotency-key", key);
            }
            if let Some(run_id) = self.identity.get("run_id").and_then(Value::as_str) {
                request = request.header("x-loom-run-id", run_id);
            }
            sent += 1;
            let mut metadata = CallMetadata::new(&request_id, sent);
            log::debug!(target: "clickhouse_loom", "{op} attempt={attempt} sent={sent} rid={request_id}");
            let received = async {
                let (status, headers, response) = send(request, &mut metadata).await?;
                let bytes = response.bytes().await?;
                Ok::<_, reqwest::Error>((status, headers, bytes))
            }
            .await;
            let (error, allowed, hint) = match received {
                Err(error) => {
                    let allowed = retry.retries_transport(&error, replayable)
                        && (error.is_connect() || idempotent);
                    (Error::Transport(error.without_url()), allowed, None)
                }
                Ok((status, headers, bytes)) => {
                    self.record_rate_state(op, &headers).await;
                    if status.is_success() {
                        let result =
                            serde_json::from_slice(&bytes).map_err(|source| Error::Protocol {
                                status: status.as_u16(),
                                source,
                            });
                        if result.is_ok() {
                            log::info!(target: "clickhouse_loom", "{op} status={} elapsed_ms={} attempts={sent} rid={request_id}", status.as_u16(), started.elapsed().as_millis());
                        } else {
                            log::error!(target: "clickhouse_loom", "{op} invalid_response status={} attempts={sent} rid={request_id}", status.as_u16());
                        }
                        return match result {
                            Ok(data) => Ok(CallResult { data, metadata }),
                            Err(error) => Err(CallFailure {
                                error,
                                metadata: Some(Box::new(metadata)),
                            }),
                        };
                    }
                    let mut error = ApiError::from_response(status.as_u16(), &headers, &bytes);
                    error.request_id.get_or_insert_with(|| request_id.clone());
                    if status.as_u16() == 429 && self.pacing && idempotent {
                        let delay = pacing_delay(
                            error.retry_after_s,
                            self.ratelimit_max_wait.saturating_sub(paced),
                        );
                        if !delay.is_zero() {
                            log::warn!(target: "clickhouse_loom", "{op} status=429 wait_ms={} attempts={sent} rid={request_id}", delay.as_millis());
                            tokio::time::sleep(delay).await;
                            paced += delay;
                            attempt = 0;
                            slept = Duration::ZERO;
                            continue;
                        }
                    }
                    let allowed = idempotent
                        && status.as_u16() != 429
                        && retry.statuses.contains(&status.as_u16())
                        && !error.forbids_retry();
                    let hint = error.retry_after_s;
                    (Error::Api(Box::new(error)), allowed, hint)
                }
            };
            let delay = retry.delay(attempt, hint);
            let status = match &error {
                Error::Api(api) => api.status,
                _ => 0,
            };
            if !allowed
                || attempt >= retry.max_attempts
                || retry
                    .total_backoff_budget
                    .is_some_and(|budget| delay > budget.saturating_sub(slept))
            {
                log::error!(target: "clickhouse_loom", "{op} failed status={status} elapsed_ms={} attempts={sent} rid={request_id}", started.elapsed().as_millis());
                return Err(CallFailure {
                    error,
                    metadata: Some(Box::new(metadata)),
                });
            }
            log::warn!(target: "clickhouse_loom", "{op} retry status={status} attempt={attempt} wait_ms={} rid={request_id}", delay.as_millis());
            tokio::time::sleep(delay).await;
            slept = slept.saturating_add(delay);
        }
    }

    async fn record_rate_state(&self, op: &str, headers: &HeaderMap) {
        if let Some(rate) = crate::call::rate_limit(headers) {
            let delay = if rate.remaining < 1 {
                Duration::from_secs_f64(
                    (rate.reset_s as f64 / rate.limit.max(1) as f64).clamp(0.0, 5.0),
                )
            } else {
                Duration::ZERO
            };
            self.rate_state.lock().await.insert(op.to_owned(), delay);
        }
    }

    /// One request to this server with the SDK identity headers, `X-Request-Id`,
    /// the credential and a per-call timeout.
    pub(crate) fn identified(
        &self,
        request: RequestBuilder,
        request_id: &str,
        timeout: Option<Duration>,
    ) -> RequestBuilder {
        let request = request
            .header("x-request-id", request_id)
            .header(
                "user-agent",
                concat!("loom-rust/", env!("CARGO_PKG_VERSION")),
            )
            .header(
                "x-loom-sdk",
                concat!("loom-rust/", env!("CARGO_PKG_VERSION")),
            )
            .header(
                "x-loom-runtime",
                format!("rust {} {}", env::consts::OS, env::consts::ARCH),
            );
        let request = match timeout {
            Some(timeout) => request.timeout(timeout),
            None => request,
        };
        match &self.authorization {
            Some(authorization) => request.header(AUTHORIZATION, authorization),
            None => request,
        }
    }
}

/// Send one request and record its status and headers, which the metadata
/// keeps when the body then fails to arrive.
pub(crate) async fn send(
    request: RequestBuilder,
    metadata: &mut CallMetadata,
) -> Result<(StatusCode, HeaderMap, reqwest::Response), reqwest::Error> {
    let response = request.send().await?;
    let status = response.status();
    let headers = response.headers().clone();
    metadata.record_response(status.as_u16(), &headers);
    Ok((status, headers, response))
}

impl ClientBuilder {
    pub fn base_url(mut self, base_url: impl Into<String>) -> Self {
        self.base_url = Some(base_url.into());
        self
    }

    /// An empty explicit token disables authorization, including the environment token.
    pub fn api_key(mut self, api_key: impl Into<String>) -> Self {
        self.api_key = Some(api_key.into());
        self
    }

    pub fn user_id(mut self, user_id: impl Into<String>) -> Self {
        self.identity["user_id"] = json!(user_id.into());
        self
    }

    pub fn session_id(mut self, session_id: impl Into<String>) -> Self {
        self.identity["session_id"] = json!(session_id.into());
        self
    }

    pub fn run_id(mut self, run_id: impl Into<String>) -> Self {
        self.identity["run_id"] = json!(run_id.into());
        self
    }

    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    pub fn retry(mut self, retry: RetryPolicy) -> Self {
        self.retry = retry;
        self
    }

    /// Wait on 429 responses and recorded rate-limit headers (default: true).
    pub fn pacing(mut self, pacing: bool) -> Self {
        self.pacing = pacing;
        self
    }

    /// Total 429 sleep budget per call (default: 15 seconds).
    pub fn ratelimit_max_wait(mut self, wait: Duration) -> Self {
        self.ratelimit_max_wait = wait;
        self
    }

    pub fn build(self) -> Result<Client, Error> {
        self.retry.validate()?;
        for key in ["org", "namespace", "agent"] {
            if self.identity[key].as_str().unwrap_or("").trim().is_empty() {
                return Err(Error::Configuration(format!("{key} must not be empty")));
            }
        }
        if self.timeout.is_zero() {
            return Err(Error::Configuration("timeout must be positive".into()));
        }
        if let Some(run_id) = self.identity.get("run_id").and_then(Value::as_str) {
            HeaderValue::from_str(run_id)
                .map_err(|_| Error::Configuration("invalid run ID header".into()))?;
        }
        let endpoint = self
            .base_url
            .or_else(|| env_value("LOOM_BASE_URL"))
            .unwrap_or_else(|| DEFAULT_URL.into());
        let mut base_url =
            Url::parse(&endpoint).map_err(|_| Error::Configuration("invalid base URL".into()))?;
        if !matches!(base_url.scheme(), "http" | "https")
            || base_url.host_str().is_none()
            || !base_url.username().is_empty()
            || base_url.password().is_some()
            || base_url.query().is_some()
            || base_url.fragment().is_some()
        {
            return Err(Error::Configuration(
                "base URL must be HTTP(S) with no credentials, query or fragment".into(),
            ));
        }
        if !base_url.path().ends_with('/') {
            let path = format!("{}/", base_url.path());
            base_url.set_path(&path);
        }
        let token = self.api_key.or_else(|| env_value("LOOM_API_KEY"));
        let authorization = token
            .filter(|s| !s.trim().is_empty())
            .map(|token| {
                let mut header = HeaderValue::from_str(&format!("Bearer {}", token.trim()))
                    .map_err(|_| Error::Configuration("invalid bearer token".into()))?;
                header.set_sensitive(true);
                Ok(header)
            })
            .transpose()?;
        let http = HttpClient::builder()
            .redirect(Policy::none())
            .retry(reqwest::retry::never())
            .timeout(self.timeout)
            .build()
            .map_err(|e| Error::Transport(e.without_url()))?;
        Ok(Client {
            http,
            base_url,
            authorization,
            identity: self.identity,
            retry: self.retry,
            pacing: self.pacing,
            ratelimit_max_wait: self.ratelimit_max_wait,
            rate_state: Arc::new(Mutex::new(HashMap::new())),
        })
    }
}

pub(crate) fn request_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

fn valid_operation(op: &str) -> bool {
    op.contains('.')
        && op.split('.').all(|part| {
            part.as_bytes().first().is_some_and(u8::is_ascii_alphabetic)
                && part.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
        })
}

pub(crate) fn pacing_delay(hint: Option<f64>, budget: Duration) -> Duration {
    let hint = hint
        .filter(|s| s.is_finite() && *s <= 60.0)
        .unwrap_or(1.0)
        .max(0.0);
    Duration::from_secs_f64(hint)
        .min(Duration::from_secs(5))
        .min(budget)
}

fn env_value(name: &str) -> Option<String> {
    env::var(name).ok().filter(|v| !v.trim().is_empty())
}

impl fmt::Debug for Client {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Client")
            .field("base_url", &self.base_url)
            .field("identity", &self.identity)
            .finish_non_exhaustive()
    }
}

impl fmt::Debug for ClientBuilder {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // An unvalidated URL can contain credentials, so omit it along with the token.
        f.debug_struct("ClientBuilder")
            .field("identity", &self.identity)
            .field("timeout", &self.timeout)
            .finish_non_exhaustive()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_timeout_matches_python_and_can_be_overridden() {
        let builder = Client::builder("org", "ns", "bot");
        assert_eq!(builder.timeout, Duration::from_secs(60));
        assert_eq!(
            builder.timeout(Duration::from_secs(2)).timeout,
            Duration::from_secs(2)
        );
    }

    #[tokio::test]
    async fn rate_state_updates_capacity_and_ignores_incomplete_readings() {
        let client = Client::builder("org", "ns", "bot")
            .api_key("")
            .build()
            .unwrap();
        for (limit, remaining, reset, expected) in [
            ("10", "0", "10", Duration::from_secs(1)),
            ("10", "1", "10", Duration::ZERO),
            ("0", "0", "10", Duration::from_secs(5)),
            ("10", "-1", "-10", Duration::ZERO),
        ] {
            let mut headers = HeaderMap::new();
            for (key, value) in [
                ("x-ratelimit-limit", limit),
                ("x-ratelimit-remaining", remaining),
                ("x-ratelimit-reset", reset),
            ] {
                headers.insert(key, value.parse().unwrap());
            }
            client.record_rate_state("memory.get", &headers).await;
            assert_eq!(client.rate_state.lock().await["memory.get"], expected);
            assert!(!client.rate_state.lock().await.contains_key("memory.search"));
            headers.insert("x-ratelimit-reset", "invalid".parse().unwrap());
            client.record_rate_state("memory.get", &headers).await;
            assert_eq!(client.rate_state.lock().await["memory.get"], expected);
            headers.remove("x-ratelimit-reset");
            client.record_rate_state("memory.get", &headers).await;
            assert_eq!(client.rate_state.lock().await["memory.get"], expected);
        }
    }

    #[test]
    fn pacing_caps_hints_and_budget_and_does_not_spin_on_zero() {
        for (hint, budget, expected) in [
            (None, 15, 1),
            (Some(61.0), 15, 1),
            (Some(60.0), 15, 5),
            (Some(30.0), 2, 2),
            (Some(0.0), 15, 0),
            (Some(1.0), 0, 0),
            (Some(-1.0), 15, 0),
            (Some(f64::NAN), 15, 1),
        ] {
            assert_eq!(
                pacing_delay(hint, Duration::from_secs(budget)),
                Duration::from_secs(expected)
            );
        }
    }
}
