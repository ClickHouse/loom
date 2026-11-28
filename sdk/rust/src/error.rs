use std::{fmt, time::SystemTime};

use reqwest::header::{HeaderMap, RETRY_AFTER};
use serde_json::Value;

/// Whose move an error is: the server's `detail.category`, else the SDK's own class.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum ErrorCategory {
    /// The request does not fit the data or the operation as they are now.
    Input,
    /// The token, its role or its namespace access does not cover the call.
    Auth,
    /// The server or a limit is busy or not ready; the request itself is fine.
    Capacity,
    /// A store or a model provider failed, was too slow or is still catching up.
    Dependency,
    /// The deployment is not in a state to serve the call; an operator must act.
    Configuration,
    /// The server failed.
    Internal,
}

impl ErrorCategory {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Input => "input",
            Self::Auth => "auth",
            Self::Capacity => "capacity",
            Self::Dependency => "dependency",
            Self::Configuration => "configuration",
            Self::Internal => "internal",
        }
    }

    fn from_name(name: &str) -> Option<Self> {
        Some(match name {
            "input" => Self::Input,
            "auth" => Self::Auth,
            "capacity" => Self::Capacity,
            "dependency" => Self::Dependency,
            "configuration" => Self::Configuration,
            "internal" => Self::Internal,
            _ => return None,
        })
    }
}

impl fmt::Display for ErrorCategory {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The class of an API error, as the other SDKs pick an exception class: a
/// known `detail.code` picks it, else the HTTP status does.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum ErrorKind {
    /// `VALIDATION_FAILED`, or HTTP 400 or 422 without a known code.
    Validation,
    /// `AUTH_INVALID`, `AUTH_INSUFFICIENT`, or HTTP 401 or 403 without a known code.
    Auth,
    /// `RESOURCE_NOT_FOUND`, or HTTP 404 without a known code.
    NotFound,
    /// `PRECONDITION_FAILED`, or HTTP 409 or 412 without a known code. Read
    /// [`ApiError::current_sha256`] and the code before resending.
    PreconditionFailed,
    /// `IDEMPOTENT_CONFLICT`: an Idempotency-Key was reused with a different body.
    IdempotentConflict,
    /// `WRONG_CELL`: the org lives in another cell of the deployment. Nothing
    /// was written; [`ApiError::org`] and [`ApiError::cell`] name where to call.
    WrongCell,
    /// HTTP 429.
    RateLimited,
    /// `IDEMPOTENT_IN_PROGRESS`: the first request with this key still runs.
    IdempotentInProgress,
    /// HTTP 5xx.
    BackendUnavailable,
    /// Any other code on any other status, such as `OVERRIDE_NOT_STORED` or 423 `LOCKED`.
    Other,
}

impl ErrorKind {
    fn of(status: u16, code: &str) -> Self {
        let class = match code {
            "VALIDATION_FAILED" => Self::Validation,
            "AUTH_INVALID" | "AUTH_INSUFFICIENT" => Self::Auth,
            "RESOURCE_NOT_FOUND" => Self::NotFound,
            "RATE_LIMITED" => Self::RateLimited,
            "IDEMPOTENT_CONFLICT" => Self::IdempotentConflict,
            "WRONG_CELL" => Self::WrongCell,
            "IDEMPOTENT_IN_PROGRESS" => Self::IdempotentInProgress,
            "PRECONDITION_FAILED" => Self::PreconditionFailed,
            // ClickHouse refused to store an override: no precondition.
            "OVERRIDE_NOT_STORED" => Self::Other,
            "BACKEND_UNAVAILABLE"
            | "OVERLOADED"
            | "BACKEND_TIMEOUT"
            | "SCHEMA_PENDING"
            | "CODED_WIDTH_MISMATCH" => Self::BackendUnavailable,
            _ => match status {
                400 | 422 => Self::Validation,
                401 | 403 => Self::Auth,
                404 => Self::NotFound,
                409 | 412 => Self::PreconditionFailed,
                _ => Self::Other,
            },
        };
        if status == 429 {
            Self::RateLimited
        } else if status >= 500
            && !matches!(class, Self::BackendUnavailable | Self::IdempotentInProgress)
        {
            Self::BackendUnavailable
        } else {
            class
        }
    }
}

/// 5xx codes the SDK never resends, with their class.
const NEVER_RETRIED: [(&str, ErrorCategory); 7] = [
    ("ANALYSIS_UNAVAILABLE", ErrorCategory::Configuration),
    ("ASSISTANT_UNAVAILABLE", ErrorCategory::Configuration),
    ("CODED_WIDTH_MISMATCH", ErrorCategory::Configuration),
    ("GATEWAY_REQUIRED", ErrorCategory::Configuration),
    ("MODEL_CALL_UNATTRIBUTED", ErrorCategory::Internal),
    ("PUBLICATION_PENDING", ErrorCategory::Dependency),
    ("TENANCY_REFUSED", ErrorCategory::Configuration),
];

/// An HTTP error carrying the server's code, message and error classification.
#[derive(derive_more::Display)]
#[display("HTTP {status} [{code}] {message}")]
#[non_exhaustive]
pub struct ApiError {
    pub status: u16,
    pub code: String,
    pub message: String,
    /// None means the response did not declare a category.
    pub category: Option<String>,
    /// None means the response did not declare whether retrying is safe.
    pub retryable: Option<bool>,
    pub retry_after_s: Option<f64>,
    pub retry_after: Option<String>,
    pub request_id: Option<String>,
    pub detail: Value,
    /// The SDK's own class, for what the transport knows and the body does not say.
    pub(crate) sdk_class: Option<(ErrorCategory, bool)>,
}

impl fmt::Debug for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // The methods resolve the class in effect; the fields show what the server sent.
        f.debug_struct("ApiError")
            .field("status", &self.status)
            .field("code", &self.code)
            .field("message", &self.message)
            .field("category", &self.category)
            .field("retryable", &self.retryable)
            .field("retry_after_s", &self.retry_after_s)
            .field("retry_after", &self.retry_after)
            .field("request_id", &self.request_id)
            .field("detail", &self.detail)
            .finish()
    }
}

impl ApiError {
    pub(crate) fn from_response(status: u16, headers: &HeaderMap, body: &[u8]) -> Self {
        let payload: Value = serde_json::from_slice(body).unwrap_or(Value::Null);
        let detail = payload.get("detail").cloned().unwrap_or(Value::Null);
        let text = |key: &str| detail.get(key).and_then(Value::as_str).map(str::to_owned);
        let retry_after = headers
            .get(RETRY_AFTER)
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned);
        let raw_hint = retry_after.clone().filter(|s| !s.is_empty()).or_else(|| {
            let value = detail.get("retry_after_s")?;
            Some(
                value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| value.to_string()),
            )
        });
        let seconds = raw_hint
            .as_deref()
            .and_then(|s| parse_retry_after(s, SystemTime::now()));
        Self {
            status,
            code: text("code").unwrap_or_else(|| "HTTP_ERROR".into()),
            message: text("message")
                .or_else(|| detail.as_str().map(str::to_owned))
                .unwrap_or_else(|| String::from_utf8_lossy(body).chars().take(500).collect()),
            category: text("category"),
            retryable: detail.get("retryable").and_then(Value::as_bool),
            retry_after_s: seconds,
            retry_after,
            request_id: headers
                .get("x-request-id")
                .and_then(|v| v.to_str().ok())
                .map(str::to_owned),
            detail,
            sdk_class: None,
        }
    }
}

pub(crate) fn parse_retry_after(raw: &str, now: SystemTime) -> Option<f64> {
    let raw = raw.trim();
    if let Ok(seconds) = raw.parse::<f64>() {
        return seconds.is_finite().then_some(seconds.max(0.0));
    }
    httpdate::parse_http_date(raw)
        .ok()
        .map(|date| date.duration_since(now).unwrap_or_default().as_secs_f64())
}

impl ApiError {
    /// The typed class derived from the code and HTTP status. HTTP 429 is
    /// always [`ErrorKind::RateLimited`]. HTTP 5xx is
    /// [`ErrorKind::BackendUnavailable`] unless the code is `IDEMPOTENT_IN_PROGRESS`.
    pub fn kind(&self) -> ErrorKind {
        ErrorKind::of(self.status, &self.code)
    }

    /// Whose move the error is: the category the server declared, else the
    /// SDK's own class, the way [`Error::category`] resolves it.
    pub fn category(&self) -> ErrorCategory {
        match self.sdk_class {
            Some((category, _)) => category,
            None => self
                .category
                .as_deref()
                .and_then(ErrorCategory::from_name)
                .unwrap_or_else(|| self.fallback_class().0),
        }
    }

    /// Whether resending the same request unchanged can succeed later: the
    /// flag the server declared, else the SDK's own class, the way
    /// [`Error::retryable`] resolves it.
    pub fn retryable(&self) -> bool {
        match self.sdk_class {
            Some((_, retryable)) => retryable,
            None => self.retryable.unwrap_or_else(|| self.fallback_class().1),
        }
    }

    /// The live row's hash in a [`ErrorKind::PreconditionFailed`] response.
    /// `None` for every other class and when the server named none. For a definition's
    /// approval, show the current text again; never resend this hash unseen.
    pub fn current_sha256(&self) -> Option<&str> {
        self.detail_of(ErrorKind::PreconditionFailed, "current_sha256")
    }

    /// The org of a [`ErrorKind::WrongCell`] answer; `None` for every other class.
    pub fn org(&self) -> Option<&str> {
        self.detail_of(ErrorKind::WrongCell, "org")
    }

    /// The cell that holds the org of a [`ErrorKind::WrongCell`] answer; `None`
    /// for every other class and when the server named none.
    pub fn cell(&self) -> Option<&str> {
        self.detail_of(ErrorKind::WrongCell, "cell")
    }

    /// The SDK's class for a body that names none: a code it never retries
    /// keeps that code's class, else the HTTP status alone decides.
    fn fallback_class(&self) -> (ErrorCategory, bool) {
        if let Some(category) = never_retried(&self.code) {
            return (category, false);
        }
        match self.status {
            401 | 403 => (ErrorCategory::Auth, false),
            408 | 429 => (ErrorCategory::Capacity, true),
            502..=504 => (ErrorCategory::Dependency, true),
            status if status >= 500 => (ErrorCategory::Internal, false),
            _ => (ErrorCategory::Input, false),
        }
    }

    /// A 5xx on an operation that is not idempotent may follow a write the
    /// call made, so it is not retryable whatever its status alone says,
    /// unless its code proves the handler never ran.
    pub(crate) fn mark_maybe_applied(&mut self, idempotent: bool) {
        if idempotent || self.retryable.is_some() || self.status < 500 || self.handler_never_ran() {
            return;
        }
        self.sdk_class = Some((self.category(), false));
    }

    pub(crate) fn handler_never_ran(&self) -> bool {
        self.status == 408 || self.code == "AUTH_UNAVAILABLE"
    }

    pub(crate) fn forbids_retry(&self) -> bool {
        self.category.as_deref() == Some("configuration") || never_retried(&self.code).is_some()
    }

    /// A detail field of one class only, so a value of another class is not read as its own.
    fn detail_of(&self, kind: ErrorKind, key: &str) -> Option<&str> {
        (self.kind() == kind)
            .then(|| self.detail.get(key))
            .flatten()
            .and_then(Value::as_str)
    }
}

fn never_retried(code: &str) -> Option<ErrorCategory> {
    NEVER_RETRIED
        .iter()
        .find(|(known, _)| *known == code)
        .map(|(_, category)| *category)
}

impl std::error::Error for ApiError {}

#[derive(Debug, derive_more::Display)]
#[non_exhaustive]
pub enum Error {
    #[display("invalid client configuration: {_0}")]
    Configuration(String),
    #[display("invalid request: {_0}")]
    Input(String),
    #[display("HTTP transport failed: {_0}")]
    Transport(reqwest::Error),
    #[display("invalid JSON response (HTTP {status}): {source}")]
    #[non_exhaustive]
    Protocol {
        status: u16,
        source: serde_json::Error,
    },
    #[display("{_0}")]
    Api(Box<ApiError>),
}

impl Error {
    /// Whose move the error is, for every variant. An API error resolves as
    /// [`ApiError::category`]. A transport failure is `Dependency`, an invalid
    /// response `Internal`, and an invalid request or client configuration `Input`.
    pub fn category(&self) -> ErrorCategory {
        match self {
            Self::Api(error) => error.category(),
            Self::Transport(_) => ErrorCategory::Dependency,
            Self::Protocol { .. } => ErrorCategory::Internal,
            _ => ErrorCategory::Input,
        }
    }

    /// Whether resending the same request unchanged can succeed later, for
    /// every variant. An API error resolves as [`ApiError::retryable`]. A
    /// transport failure is retryable here only if it happened during
    /// connection setup. This error does not know the operation's retry
    /// policy. No other SDK-raised error is retryable.
    pub fn retryable(&self) -> bool {
        match self {
            Self::Api(error) => error.retryable(),
            Self::Transport(source) => source.is_connect(),
            _ => false,
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Transport(source) => Some(source),
            Self::Protocol { source, .. } => Some(source),
            Self::Api(error) => Some(error.as_ref()),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::time::Duration;

    #[test]
    fn retry_after_seconds_and_dates_use_an_explicit_clock() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(100);
        for (raw, expected) in [
            ("0.5", Some(0.5)),
            (" -2 ", Some(0.0)),
            ("0", Some(0.0)),
            ("NaN", None),
            ("inf", None),
            ("", None),
            ("invalid", None),
            ("Thu, 01 Jan 1970 00:02:00 GMT", Some(20.0)),
            ("Thu, 01 Jan 1970 00:00:00 GMT", Some(0.0)),
        ] {
            assert_eq!(parse_retry_after(raw, now), expected, "{raw}");
        }
    }

    #[test]
    fn invalid_header_takes_precedence_over_detail_and_keeps_raw_hint() {
        let mut headers = HeaderMap::new();
        headers.insert(RETRY_AFTER, "invalid".parse().unwrap());
        let error = ApiError::from_response(429, &headers, br#"{"detail":{"retry_after_s":0.25}}"#);
        assert_eq!(error.retry_after_s, None);
        assert_eq!(error.retry_after.as_deref(), Some("invalid"));
    }

    #[test]
    fn body_retry_hints_are_parsed_when_the_header_is_absent_or_empty() {
        for (hint, expected) in [
            (serde_json::json!(0.25), Some(0.25)),
            (serde_json::json!("0.5"), Some(0.5)),
            (serde_json::json!(0), Some(0.0)),
            (serde_json::json!(-2), Some(0.0)),
            (serde_json::json!("-2"), Some(0.0)),
            (serde_json::json!("NaN"), None),
            (serde_json::json!("inf"), None),
            (serde_json::json!("invalid"), None),
            (serde_json::json!(null), None),
            (serde_json::json!(true), None),
        ] {
            for header in [None, Some("")] {
                let mut headers = HeaderMap::new();
                if let Some(header) = header {
                    headers.insert(RETRY_AFTER, header.parse().unwrap());
                }
                let body = serde_json::json!({"detail":{"retry_after_s":hint}});
                let error =
                    ApiError::from_response(429, &headers, &serde_json::to_vec(&body).unwrap());
                assert_eq!(
                    error.retry_after_s, expected,
                    "hint={hint}, header={header:?}"
                );
                assert_eq!(error.retry_after.as_deref(), header);
                assert_eq!(error.detail["retry_after_s"], hint);
            }
        }
        let error = ApiError::from_response(429, &HeaderMap::new(), br#"{"detail":{}}"#);
        assert_eq!(error.retry_after_s, None);
        assert_eq!(error.retry_after, None);
    }

    #[test]
    fn display_preserves_each_error_kind_and_its_context() {
        for (error, expected) in [
            (
                Error::Configuration("timeout must be positive".into()),
                "invalid client configuration: timeout must be positive",
            ),
            (
                Error::Input("value cannot be null".into()),
                "invalid request: value cannot be null",
            ),
        ] {
            assert_eq!(error.to_string(), expected);
        }
        let api = ApiError::from_response(
            429,
            &HeaderMap::new(),
            br#"{"detail":{"code":"RATE_LIMITED","message":"busy"}}"#,
        );
        assert_eq!(api.to_string(), "HTTP 429 [RATE_LIMITED] busy");
        assert_eq!(
            Error::Api(Box::new(api)).to_string(),
            "HTTP 429 [RATE_LIMITED] busy"
        );

        let source = serde_json::from_str::<Value>("{").unwrap_err();
        let expected = format!("invalid JSON response (HTTP 200): {source}");
        assert_eq!(
            Error::Protocol {
                status: 200,
                source
            }
            .to_string(),
            expected
        );

        let source = reqwest::Client::new().get("not a URL").build().unwrap_err();
        let expected = format!("HTTP transport failed: {source}");
        assert_eq!(Error::Transport(source).to_string(), expected);
    }

    #[test]
    fn source_exposes_the_original_error_and_stops_at_leaf_errors() {
        use std::error::Error as _;

        for error in [
            Error::Configuration("invalid".into()),
            Error::Input("invalid".into()),
        ] {
            assert!(error.source().is_none());
        }
        let api = ApiError::from_response(
            503,
            &HeaderMap::new(),
            br#"{"detail":{"code":"OVERLOADED","message":"busy"}}"#,
        );
        assert!(api.source().is_none());
        let error = Error::Api(Box::new(api));
        let Error::Api(original) = &error else {
            unreachable!()
        };
        assert!(std::ptr::eq(
            error.source().unwrap().downcast_ref::<ApiError>().unwrap(),
            original.as_ref(),
        ));
        let error = Error::Protocol {
            status: 200,
            source: serde_json::from_str::<Value>("{").unwrap_err(),
        };
        let Error::Protocol {
            source: original, ..
        } = &error
        else {
            unreachable!()
        };
        assert!(std::ptr::eq(
            error
                .source()
                .unwrap()
                .downcast_ref::<serde_json::Error>()
                .unwrap(),
            original,
        ));
        let error = Error::Transport(reqwest::Client::new().get("not a URL").build().unwrap_err());
        let Error::Transport(original) = &error else {
            unreachable!()
        };
        assert!(std::ptr::eq(
            error
                .source()
                .unwrap()
                .downcast_ref::<reqwest::Error>()
                .unwrap(),
            original,
        ));
    }

    fn api(status: u16, detail: Value) -> ApiError {
        let body = json!({"detail": detail}).to_string();
        ApiError::from_response(status, &HeaderMap::new(), body.as_bytes())
    }

    #[test]
    fn the_class_follows_the_code_then_the_status() {
        for (status, detail, expected) in [
            (
                400,
                json!({"code": "VALIDATION_FAILED"}),
                ErrorKind::Validation,
            ),
            (422, json!({}), ErrorKind::Validation),
            (401, json!({}), ErrorKind::Auth),
            (403, json!({"code": "MODEL_CALL_REFUSED"}), ErrorKind::Auth),
            (404, json!({"code": "AUTH_INSUFFICIENT"}), ErrorKind::Auth),
            (404, json!({}), ErrorKind::NotFound),
            (
                409,
                json!({"code": "PINNED_CONFLICT"}),
                ErrorKind::PreconditionFailed,
            ),
            (412, json!({}), ErrorKind::PreconditionFailed),
            (
                409,
                json!({"code": "IDEMPOTENT_CONFLICT"}),
                ErrorKind::IdempotentConflict,
            ),
            (409, json!({"code": "WRONG_CELL"}), ErrorKind::WrongCell),
            (
                503,
                json!({"code": "IDEMPOTENT_IN_PROGRESS"}),
                ErrorKind::IdempotentInProgress,
            ),
            (
                503,
                json!({"code": "AUTH_UNAVAILABLE"}),
                ErrorKind::BackendUnavailable,
            ),
            (500, json!({}), ErrorKind::BackendUnavailable),
            (
                503,
                json!({"code": "RATE_LIMITED"}),
                ErrorKind::BackendUnavailable,
            ),
            (429, json!({}), ErrorKind::RateLimited),
            (
                409,
                json!({"code": "OVERRIDE_NOT_STORED"}),
                ErrorKind::Other,
            ),
            (423, json!({"code": "LOCKED"}), ErrorKind::Other),
        ] {
            assert_eq!(api(status, detail).kind(), expected);
        }
    }

    #[test]
    fn the_class_in_effect_is_the_declared_one_else_the_sdk_s() {
        for (status, category, retryable) in [
            (400, ErrorCategory::Input, false),
            (401, ErrorCategory::Auth, false),
            (403, ErrorCategory::Auth, false),
            (404, ErrorCategory::Input, false),
            (408, ErrorCategory::Capacity, true),
            (409, ErrorCategory::Input, false),
            (429, ErrorCategory::Capacity, true),
            (500, ErrorCategory::Internal, false),
            (502, ErrorCategory::Dependency, true),
            (503, ErrorCategory::Dependency, true),
            (504, ErrorCategory::Dependency, true),
        ] {
            let error = api(status, json!({}));
            assert_eq!(
                (error.category(), error.retryable()),
                (category, retryable),
                "{status}"
            );
        }
        for (code, category) in [
            ("ANALYSIS_UNAVAILABLE", ErrorCategory::Configuration),
            ("ASSISTANT_UNAVAILABLE", ErrorCategory::Configuration),
            ("CODED_WIDTH_MISMATCH", ErrorCategory::Configuration),
            ("GATEWAY_REQUIRED", ErrorCategory::Configuration),
            ("MODEL_CALL_UNATTRIBUTED", ErrorCategory::Internal),
            ("PUBLICATION_PENDING", ErrorCategory::Dependency),
            ("TENANCY_REFUSED", ErrorCategory::Configuration),
        ] {
            let error = api(503, json!({"code": code}));
            assert_eq!(
                (error.category(), error.retryable()),
                (category, false),
                "{code}"
            );
            assert!(error.forbids_retry());
        }
        let error = api(
            503,
            json!({"code": "INTERNAL_ERROR", "category": "capacity", "retryable": true}),
        );
        assert_eq!(
            (error.category(), error.retryable()),
            (ErrorCategory::Capacity, true)
        );
        // A name the SDK does not know falls back; the raw fields keep it.
        let error = api(500, json!({"category": "future", "retryable": "yes"}));
        assert_eq!(
            (error.category(), error.retryable()),
            (ErrorCategory::Internal, false)
        );
        assert_eq!(error.category.as_deref(), Some("future"));
        assert_eq!(error.retryable, None);
        let error = api(423, json!({"category": "configuration"}));
        assert_eq!(error.category(), ErrorCategory::Configuration);
        assert!(error.forbids_retry());
        assert_eq!(ErrorCategory::Dependency.to_string(), "dependency");
    }

    #[test]
    fn a_5xx_that_may_follow_a_write_is_not_retryable() {
        for (status, code, idempotent, declared, expected) in [
            (503, "OVERLOADED", false, None, false),
            (503, "OVERLOADED", true, None, true),
            (503, "OVERLOADED", false, Some(true), true),
            (503, "OVERLOADED", false, Some(false), false),
            (500, "INTERNAL_ERROR", false, None, false),
            (503, "AUTH_UNAVAILABLE", false, None, true),
            (429, "RATE_LIMITED", false, None, true),
        ] {
            let mut detail = json!({"code": code});
            if let Some(declared) = declared {
                detail["retryable"] = json!(declared);
            }
            let mut error = api(status, detail);
            let category = error.category();
            error.mark_maybe_applied(idempotent);
            assert_eq!(
                error.retryable(),
                expected,
                "{code} {idempotent} {declared:?}"
            );
            assert_eq!(error.category(), category);
            assert_eq!(error.retryable, declared);
        }
    }

    #[test]
    fn typed_details_belong_to_their_class() {
        for (status, detail) in [
            (
                409,
                json!({"code": "PRECONDITION_FAILED", "current_sha256": "live"}),
            ),
            (412, json!({"current_sha256": "live"})),
        ] {
            assert_eq!(api(status, detail).current_sha256(), Some("live"));
        }
        assert_eq!(
            api(409, json!({"code": "PRECONDITION_FAILED"})).current_sha256(),
            None
        );
        assert_eq!(
            api(
                409,
                json!({"code": "PRECONDITION_FAILED", "current_sha256": 7})
            )
            .current_sha256(),
            None
        );
        for code in ["IDEMPOTENT_CONFLICT", "WRONG_CELL", "OVERRIDE_NOT_STORED"] {
            assert_eq!(
                api(409, json!({"code": code, "current_sha256": "live"})).current_sha256(),
                None
            );
        }

        let elsewhere = api(
            409,
            json!({"code": "WRONG_CELL", "org": "acme", "cell": "eu"}),
        );
        assert_eq!(
            (elsewhere.org(), elsewhere.cell()),
            (Some("acme"), Some("eu"))
        );
        let unplaced = api(
            409,
            json!({"code": "WRONG_CELL", "org": "acme", "cell": ""}),
        );
        assert_eq!(unplaced.cell(), Some(""));
        let home = api(
            409,
            json!({"code": "PRECONDITION_FAILED", "org": "acme", "cell": "eu"}),
        );
        assert_eq!((home.org(), home.cell()), (None, None));

        let mut error = api(503, json!({"code": "OVERLOADED"}));
        error.mark_maybe_applied(false);
        assert!(!format!("{error:?}").contains("sdk_class"));
    }

    #[tokio::test]
    async fn errors_the_sdk_raises_have_a_class() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let unreachable = Error::Transport(
            reqwest::Client::new()
                .get(format!("http://{addr}"))
                .send()
                .await
                .unwrap_err(),
        );
        assert_eq!(
            (unreachable.category(), unreachable.retryable()),
            (ErrorCategory::Dependency, true)
        );

        let held = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let client = reqwest::Client::builder()
            .timeout(Duration::from_millis(50))
            .build()
            .unwrap();
        let lost = Error::Transport(
            client
                .get(format!("http://{}", held.local_addr().unwrap()))
                .send()
                .await
                .unwrap_err(),
        );
        assert_eq!(
            (lost.category(), lost.retryable()),
            (ErrorCategory::Dependency, false)
        );

        let source = serde_json::from_str::<Value>("{").unwrap_err();
        let invalid = Error::Protocol {
            status: 200,
            source,
        };
        assert_eq!(
            (invalid.category(), invalid.retryable()),
            (ErrorCategory::Internal, false)
        );
        for error in [
            Error::Input("invalid".into()),
            Error::Configuration("invalid".into()),
        ] {
            assert_eq!(
                (error.category(), error.retryable()),
                (ErrorCategory::Input, false)
            );
        }
        let error = Error::Api(Box::new(api(404, json!({}))));
        assert_eq!(
            (error.category(), error.retryable()),
            (ErrorCategory::Input, false)
        );
    }
}
