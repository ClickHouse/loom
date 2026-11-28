use super::*;
use clickhouse_loom::{CallFailure, CallOptions, CallResult};

/// Operations the registry does not mark idempotent.
const NOT_REPLAYED: [&str; 2] = ["assistant.ask", "future.op"];

pub(super) fn unused_response() -> String {
    // A queued response keeps the fixture listening, so an extra request would arrive.
    response(200, "{}", "")
}

fn refused(status: u16, code: &str) -> String {
    response(
        status,
        json!({"detail": {"code": code, "message": "again", "retry_after_s": 0.01}}),
        "",
    )
}

fn accepted() -> String {
    response(200, json!({"answer": "ok"}), "")
}

pub(super) fn immediate() -> RetryPolicy {
    let mut policy = RetryPolicy::default();
    policy.backoff = Duration::ZERO;
    policy.backoff_jitter = 0.0;
    policy
}

async fn ask(
    client: &Client,
    op: &str,
    request_id: &str,
) -> Result<CallResult<Value>, CallFailure> {
    client
        .call::<Value>(
            op,
            &json!({"question": "which"}),
            CallOptions::default().request_id(request_id),
        )
        .await
}

#[tokio::test]
async fn a_429_is_paced_on_operations_that_declare_no_idempotence() {
    for op in NOT_REPLAYED {
        let server = Server::new(vec![
            refused(429, "RATE_LIMITED"),
            accepted(),
            unused_response(),
        ]);
        let client = Client::builder("org", "ns", "bot")
            .base_url(&server.url)
            .api_key("")
            .build()
            .unwrap();
        let result = ask(&client, op, "paced-1").await.unwrap();
        assert_eq!(result.data["answer"], "ok");
        assert_eq!(result.metadata.attempts, 2);
        for retry_count in ["0", "1"] {
            let request = server.request().await;
            assert_eq!(request.line, format!("POST /v1/{op} HTTP/1.1"));
            assert_eq!(request.headers["x-request-id"], "paced-1");
            assert_eq!(request.headers["x-loom-retry-count"], retry_count);
            assert!(!request.headers.contains_key("idempotency-key"));
        }

        let server = Server::new(vec![refused(429, "RATE_LIMITED"), accepted()]);
        let client = Client::builder("org", "ns", "bot")
            .base_url(&server.url)
            .api_key("")
            .ratelimit_max_wait(Duration::ZERO)
            .build()
            .unwrap();
        let failure = ask(&client, op, "refused-1").await.unwrap_err();
        let Error::Api(error) = failure.error else {
            panic!("expected API error");
        };
        assert_eq!(error.status, 429);
        assert_eq!(failure.metadata.unwrap().attempts, 1);
        assert_eq!(server.request().await.headers["x-request-id"], "refused-1");
        assert!(
            tokio::time::timeout(Duration::from_millis(300), server.request())
                .await
                .is_err()
        );
    }
}

#[tokio::test]
async fn a_429_with_a_hint_past_the_budget_is_not_resent() {
    let server = Server::new(vec![
        response(
            429,
            json!({"detail": {"code": "RATE_LIMITED"}}),
            "Retry-After: 3600\r\n",
        ),
        accepted(),
    ]);
    let failure = ask(&server.client(), "assistant.ask", "daily-limit")
        .await
        .unwrap_err();
    let Error::Api(error) = failure.error else {
        panic!("expected API error");
    };
    assert_eq!(error.status, 429);
    assert_eq!(error.retry_after_s, Some(3600.0));
    assert_eq!(failure.metadata.unwrap().attempts, 1);
    assert_eq!(
        server.request().await.line,
        "POST /v1/assistant.ask HTTP/1.1"
    );
    assert!(
        tokio::time::timeout(Duration::from_millis(300), server.request())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn an_answer_that_shows_nothing_ran_is_retried_on_any_operation() {
    for op in NOT_REPLAYED {
        for (status, code) in [(408, "REQUEST_TIMEOUT"), (503, "AUTH_UNAVAILABLE")] {
            let server = Server::new(vec![refused(status, code), accepted()]);
            let client = Client::builder("org", "ns", "bot")
                .base_url(&server.url)
                .api_key("")
                .retry(immediate())
                .build()
                .unwrap();
            let result = ask(&client, op, "ran-later").await.unwrap();
            assert_eq!(result.data["answer"], "ok", "{op} {code}");
            assert_eq!(result.metadata.attempts, 2);
            for retry_count in ["0", "1"] {
                let request = server.request().await;
                assert_eq!(request.headers["x-request-id"], "ran-later");
                assert_eq!(request.headers["x-loom-retry-count"], retry_count);
                assert!(!request.headers.contains_key("idempotency-key"));
            }
        }
    }
}

#[tokio::test]
async fn every_other_answer_still_needs_declared_idempotence() {
    for op in NOT_REPLAYED {
        for (status, code) in [
            (500, "INTERNAL_ERROR"),
            (503, "OVERLOADED"),
            (408, "GATEWAY_REQUIRED"),
        ] {
            let server = Server::new(vec![refused(status, code), response(200, "{}", "")]);
            let failure = ask(&server.client(), op, "once").await.unwrap_err();
            let Error::Api(error) = failure.error else {
                panic!("expected API error");
            };
            assert_eq!(error.status, status, "{op} {code}");
            assert_eq!(failure.metadata.unwrap().attempts, 1);
            assert_eq!(server.request().await.headers["x-loom-retry-count"], "0");
            assert!(
                tokio::time::timeout(Duration::from_millis(300), server.request())
                    .await
                    .is_err(),
                "{op} {code}"
            );
        }
    }
}

#[tokio::test]
async fn a_never_ran_answer_follows_the_policy() {
    for (policy, retried) in [
        (RetryPolicy::disabled(), false),
        (without(&503), false),
        (without(&408), true),
    ] {
        let server = Server::new(vec![refused(503, "AUTH_UNAVAILABLE"), accepted()]);
        let client = Client::builder("org", "ns", "bot")
            .base_url(&server.url)
            .api_key("")
            .retry(policy)
            .build()
            .unwrap();
        let result = ask(&client, "assistant.ask", "policy").await;
        assert_eq!(result.is_ok(), retried);
        server.request().await;
        assert_eq!(
            tokio::time::timeout(Duration::from_millis(300), server.request())
                .await
                .is_ok(),
            retried
        );
    }
}

fn without(status: &u16) -> RetryPolicy {
    let mut policy = immediate();
    policy.statuses.remove(status);
    policy
}
