"""Clients for the external services Loom calls.

`_http.get_model_client()` is the shared connection pool for model calls (the
LLM and embedding providers, the inference gateway), with the gateway check on
every hop; `_http.get_client()` is the pool for other calls (GitHub, an
issuer's discovery document). `_http_errors` keeps a provider's error reason on
its `HTTPStatusError`. `github` is the GitHub REST client. `gdrive`, `linear`,
`notion` and `recording` read the knowledge sources; `_vendor` holds their shared
errors, status mapping and size-capped reads.
"""
