# Upload retry policy

The upload client sends originals to the storage gateway. This note records how it retries.

## Backoff

The client retries with exponential backoff and full jitter, starting at 200 ms and capped at
30 seconds, at most 5 attempts. Full jitter spreads the retries of many clients after a
gateway restart, so they do not arrive together.

## Rejected: fixed-interval retries

Retrying every second was rejected because every client that failed during a gateway restart
retries in the same second, and the gateway falls over again.

## Idempotency

Every attempt carries the same idempotency key, so a retry after a lost response never stores
the original twice.

## What is retried

Only 5xx responses and timeouts are retried. A 429 response is never retried: the client
reports it to the caller.
