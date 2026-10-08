"""Clients for the external services Loom calls.

`_http.get_client()` is the shared connection pool for provider APIs (the
LLM and embedding providers, GitHub); `_http_errors` keeps a provider's
error reason on its `HTTPStatusError`. `github` is the GitHub REST client.
"""
