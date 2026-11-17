use std::{collections::BTreeSet, time::Duration};

use crate::Error;

/// Which calls may be repeated after a timeout or an incomplete response.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum RetryTimeouts {
    Never,
    /// Only writes carrying a server-deduplicated idempotency key.
    Idempotent,
    Always,
}

/// Retry settings, validated when attached to a client.
///
/// A 429 uses the client's separate pacing budget, even with one attempt.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct RetryPolicy {
    pub max_attempts: u32,
    pub backoff: Duration,
    pub backoff_max: Duration,
    pub backoff_jitter: f64,
    pub statuses: BTreeSet<u16>,
    pub retry_connection_errors: bool,
    pub retry_timeouts: RetryTimeouts,
    pub respect_retry_after: bool,
    pub max_retry_after: Duration,
    /// None permits unbounded total backoff.
    pub total_backoff_budget: Option<Duration>,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_attempts: 3,
            backoff: Duration::from_millis(100),
            backoff_max: Duration::from_secs(5),
            backoff_jitter: 0.5,
            statuses: std::iter::once(408).chain(500..600).collect(),
            retry_connection_errors: true,
            retry_timeouts: RetryTimeouts::Idempotent,
            respect_retry_after: true,
            max_retry_after: Duration::from_secs(60),
            total_backoff_budget: None,
        }
    }
}

impl RetryPolicy {
    /// Disable retries. Disable pacing separately to return a 429 immediately.
    pub fn disabled() -> Self {
        Self {
            max_attempts: 1,
            ..Self::default()
        }
    }

    pub(crate) fn validate(&self) -> Result<(), Error> {
        if self.max_attempts == 0 {
            return Err(Error::Configuration(
                "retry.max_attempts must be >= 1".into(),
            ));
        }
        if !self.backoff_jitter.is_finite() || !(0.0..=1.0).contains(&self.backoff_jitter) {
            return Err(Error::Configuration(
                "retry.backoff_jitter must be within 0..1".into(),
            ));
        }
        if self.statuses.iter().any(|s| !(100..=599).contains(s)) {
            return Err(Error::Configuration(
                "retry.statuses must contain HTTP statuses within 100..599".into(),
            ));
        }
        Ok(())
    }

    pub(crate) fn retries_transport(&self, error: &reqwest::Error, replayable: bool) -> bool {
        // Connect failures occur before dispatch; a connect timeout is also safe.
        if error.is_connect() {
            return self.retry_connection_errors;
        }
        // Response::bytes wraps truncated bodies in a Decode error.
        (error.is_timeout() || error.is_body() || error.is_decode() || error.is_request())
            && match self.retry_timeouts {
                RetryTimeouts::Never => false,
                RetryTimeouts::Idempotent => replayable,
                RetryTimeouts::Always => true,
            }
            && !error.is_builder()
    }

    pub(crate) fn delay(&self, attempt: u32, hint: Option<f64>) -> Duration {
        self.delay_with_random(attempt, hint, rand::random::<f64>())
    }

    fn delay_with_random(&self, attempt: u32, hint: Option<f64>, random: f64) -> Duration {
        if self.respect_retry_after {
            if let Some(delay) = hint
                .filter(|s| *s <= self.max_retry_after.as_secs_f64())
                .and_then(|s| Duration::try_from_secs_f64(s).ok())
            {
                return delay;
            }
        }
        let exponential = (self.backoff.as_secs_f64()
            * 2.0_f64.powf(f64::from(attempt.saturating_sub(1))))
        .min(self.backoff_max.as_secs_f64());
        let seconds =
            exponential * (1.0 - self.backoff_jitter / 2.0 + random * self.backoff_jitter);
        Duration::try_from_secs_f64(seconds).unwrap_or(Duration::MAX)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_honors_hint_cap_opt_out_and_exponential_ceiling() {
        let mut policy = RetryPolicy {
            backoff_jitter: 0.0,
            ..RetryPolicy::default()
        };
        assert_eq!(policy.delay(1, None), Duration::from_millis(100));
        assert_eq!(policy.delay(2, None), Duration::from_millis(200));
        assert_eq!(policy.delay(u32::MAX, None), Duration::from_secs(5));
        assert_eq!(policy.delay(1, Some(60.0)), Duration::from_secs(60));
        assert_eq!(policy.delay(1, Some(61.0)), Duration::from_millis(100));
        assert_eq!(policy.delay(1, Some(0.0)), Duration::ZERO);
        policy.respect_retry_after = false;
        assert_eq!(policy.delay(1, Some(1.0)), Duration::from_millis(100));
    }

    #[test]
    fn jitter_stays_in_python_default_bounds() {
        let policy = RetryPolicy::default();
        for (random, millis) in [(0.0, 75), (0.5, 100), (1.0, 125)] {
            assert_eq!(
                policy.delay_with_random(1, None, random),
                Duration::from_millis(millis)
            );
        }
    }
}
