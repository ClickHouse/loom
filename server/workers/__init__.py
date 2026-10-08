"""The background worker plane: the registry of loops (`registry`), the
supervisor that runs, times and drains them (`supervisor`), the host lease
(`leader`) and the pass status they leave (`status`, `status_store`). The
loops themselves live in each domain's `workers` package."""
