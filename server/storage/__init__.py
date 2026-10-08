"""Storage below the repositories.

`blob_store` keeps uploaded document originals on the filesystem;
`retention_overrides` composes and applies each managed table's TTL from the
global retention / tiering settings and the per-namespace overrides. Typed
reads and writes of one logical table stay in its domain's `store/` package.
"""
