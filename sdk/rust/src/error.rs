use std::{fmt, time::SystemTime};

use reqwest::header::{HeaderMap, RETRY_AFTER};
use serde_json::Value;

/// An HTTP error carrying the server's code, message and error classification.
#[derive(Debug)]
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
    pub(crate) fn forbids_retry(&self) -> bool {
        self.category.as_deref() == Some("configuration")
            || matches!(
                self.code.as_str(),
                "ANALYSIS_UNAVAILABLE"
                    | "ASSISTANT_UNAVAILABLE"
                    | "CODED_WIDTH_MISMATCH"
                    | "GATEWAY_REQUIRED"
                    | "MODEL_CALL_UNATTRIBUTED"
                    | "PUBLICATION_PENDING"
                    | "TENANCY_REFUSED"
            )
    }
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "HTTP {} [{}] {}", self.status, self.code, self.message)
    }
}

impl std::error::Error for ApiError {}

#[derive(Debug)]
#[non_exhaustive]
pub enum Error {
    Configuration(String),
    Input(String),
    Transport(reqwest::Error),
    #[non_exhaustive]
    Protocol {
        status: u16,
        source: serde_json::Error,
    },
    Api(Box<ApiError>),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Configuration(message) => write!(f, "invalid client configuration: {message}"),
            Self::Input(message) => write!(f, "invalid request: {message}"),
            Self::Transport(source) => write!(f, "HTTP transport failed: {source}"),
            Self::Protocol { status, source } => {
                write!(f, "invalid JSON response (HTTP {status}): {source}")
            }
            Self::Api(error) => error.fmt(f),
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
}
