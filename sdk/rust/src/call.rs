use std::time::Duration;

use reqwest::header::HeaderMap;

use crate::{Error, RetryPolicy};

/// Overrides for one logical call. Unset fields use the client's defaults.
///
/// Retry overrides leave the client's separate 429 pacing budget in effect.
#[derive(Clone, Debug, Default)]
#[non_exhaustive]
pub struct CallOptions {
    /// Timeout per complete HTTP attempt, including the response body.
    pub timeout: Option<Duration>,
    pub retry: Option<RetryPolicy>,
    /// Applies to declared idempotent writes. Reuse only with the same operation and payload.
    pub idempotency_key: Option<String>,
    /// 1–64 letters, digits, dots, underscores or hyphens.
    pub request_id: Option<String>,
}

impl CallOptions {
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }

    pub fn retry(mut self, retry: RetryPolicy) -> Self {
        self.retry = Some(retry);
        self
    }

    pub fn idempotency_key(mut self, key: impl Into<String>) -> Self {
        self.idempotency_key = Some(key.into());
        self
    }

    pub fn request_id(mut self, id: impl Into<String>) -> Self {
        self.request_id = Some(id.into());
        self
    }

    pub(crate) fn validate(&self, replayable: bool) -> Result<(), Error> {
        if self.timeout.is_some_and(|timeout| timeout.is_zero()) {
            return Err(Error::Input("call timeout must be positive".into()));
        }
        if let Some(retry) = &self.retry {
            retry.validate()?;
        }
        if self.idempotency_key.is_some() && !replayable {
            return Err(Error::Input(
                "idempotency_key requires a declared idempotent write".into(),
            ));
        }
        if let Some(id) = &self.request_id {
            if id.is_empty()
                || id.len() > 64
                || !id
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            {
                return Err(Error::Input(
                    "request_id must be 1–64 letters, digits, dots, underscores or hyphens".into(),
                ));
            }
        }
        if let Some(key) = &self.idempotency_key {
            if key.is_empty() || !key.bytes().all(|b| b.is_ascii_graphic()) {
                return Err(Error::Input(
                    "idempotency_key must contain visible ASCII characters".into(),
                ));
            }
        }
        Ok(())
    }
}

/// X-RateLimit-* values on a response; reset is measured in seconds.
#[derive(Clone, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub struct RateLimit {
    pub limit: i64,
    pub remaining: i64,
    pub reset_s: i64,
}

/// Metadata from the final HTTP attempt, including incomplete responses.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct CallMetadata {
    pub sent_request_id: String,
    /// The echoed request ID, or the sent ID when no echo is present.
    pub request_id: String,
    /// None when no response headers arrived.
    pub status: Option<u16>,
    pub schema_version: Option<String>,
    /// Duration of the app metric in Server-Timing, in milliseconds.
    pub server_timing_ms: Option<f64>,
    /// None when any rate-limit header is missing or invalid.
    pub rate_limit: Option<RateLimit>,
    /// All HTTP attempts, including retries and 429 pacing rounds.
    pub attempts: u64,
    pub hint: Option<String>,
    pub hint_code: Option<String>,
}

impl CallMetadata {
    pub(crate) fn new(sent_id: &str, attempts: u64) -> Self {
        Self {
            sent_request_id: sent_id.to_owned(),
            request_id: sent_id.to_owned(),
            status: None,
            schema_version: None,
            server_timing_ms: None,
            rate_limit: None,
            attempts,
            hint: None,
            hint_code: None,
        }
    }

    pub(crate) fn record_response(&mut self, status: u16, headers: &HeaderMap) {
        self.status = Some(status);
        self.request_id =
            text(headers, "x-request-id").unwrap_or_else(|| self.sent_request_id.clone());
        self.schema_version = text(headers, "x-schema-version");
        self.server_timing_ms = text(headers, "server-timing")
            .as_deref()
            .and_then(app_duration_ms);
        self.rate_limit = rate_limit(headers);
        self.hint = text(headers, "x-loom-hint");
        self.hint_code = text(headers, "x-loom-hint-code");
    }
}

/// A typed response with metadata from its final successful HTTP attempt.
#[derive(Debug)]
#[non_exhaustive]
pub struct CallResult<T> {
    pub data: T,
    pub metadata: CallMetadata,
}

/// The original error and its final attempt's metadata.
#[derive(Debug, derive_more::Display)]
#[display("{error}")]
#[non_exhaustive]
pub struct CallFailure {
    pub error: Error,
    /// None for validation or encoding failures before dispatch.
    pub metadata: Option<Box<CallMetadata>>,
}

impl From<Error> for CallFailure {
    fn from(error: Error) -> Self {
        Self {
            error,
            metadata: None,
        }
    }
}

impl std::error::Error for CallFailure {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.error)
    }
}

pub(crate) fn rate_limit(headers: &HeaderMap) -> Option<RateLimit> {
    let number = |key| text(headers, key)?.parse().ok();
    Some(RateLimit {
        limit: number("x-ratelimit-limit")?,
        remaining: number("x-ratelimit-remaining")?,
        reset_s: number("x-ratelimit-reset")?,
    })
}

fn text(headers: &HeaderMap, key: &str) -> Option<String> {
    headers
        .get(key)?
        .to_str()
        .ok()
        .filter(|v| !v.is_empty())
        .map(str::to_owned)
}

fn app_duration_ms(raw: &str) -> Option<f64> {
    for metric in raw.split(',') {
        let mut parts = metric.split(';').map(str::trim);
        if parts.next() != Some("app") {
            continue;
        }
        for part in parts {
            if let Some((key, value)) = part.split_once('=') {
                if key.trim() == "dur" {
                    return value
                        .trim()
                        .parse::<f64>()
                        .ok()
                        .filter(|d| d.is_finite() && *d >= 0.0);
                }
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn metadata_preserves_absence_and_rejects_invalid_headers() {
        let mut headers = HeaderMap::new();
        let mut metadata = CallMetadata::new("sent", 2);
        metadata.record_response(200, &headers);
        assert_eq!(metadata.request_id, "sent");
        assert!(metadata.schema_version.is_none());
        assert!(metadata.hint.is_none());
        assert!(metadata.rate_limit.is_none());
        for (key, value) in [
            ("x-request-id", ""),
            ("x-schema-version", ""),
            ("x-ratelimit-limit", "10"),
            ("x-ratelimit-remaining", "2"),
            ("x-ratelimit-reset", "bad"),
        ] {
            headers.insert(key, value.parse().unwrap());
        }
        metadata.record_response(200, &headers);
        assert_eq!(metadata.request_id, "sent");
        assert!(metadata.schema_version.is_none());
        assert!(metadata.rate_limit.is_none());
        headers.insert("x-ratelimit-reset", "8".parse().unwrap());
        assert_eq!(rate_limit(&headers).unwrap().reset_s, 8);
        headers.remove("x-ratelimit-limit");
        assert!(rate_limit(&headers).is_none());
        for (raw, expected) in [
            ("db;dur=9, app;desc=handler; dur = 12.5", Some(12.5)),
            ("app;dur=0", Some(0.0)),
            ("application;dur=5", None),
            ("app;dur=NaN", None),
            ("app;dur=inf", None),
            ("app;dur=-1", None),
            ("app;dur=bad", None),
        ] {
            assert_eq!(app_duration_ms(raw), expected, "{raw}");
        }
    }
}
