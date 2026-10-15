-- Device sign-in and refresh-token families in loom.oauth_state.
--
-- family: the sign-in session a code, device grant or refresh token belongs
-- to. Every access token of the session carries it as its sid, so revoking
-- the family (logout, a replayed refresh token) ends all of them.
-- user_code, status, address, ticket: a device grant's user code (keyed with
-- the server secret), its state (pending, approved, denied), the address that
-- asked for it, and the sha256 of the one-time ticket its approval page posts
-- back.
-- upstream_refresh: the identity provider's refresh token, sealed, so each
-- Loom refresh can ask the provider whether the user is still active.
ALTER TABLE loom.oauth_state
    ADD COLUMN IF NOT EXISTS family String DEFAULT '',
    ADD COLUMN IF NOT EXISTS user_code String DEFAULT '',
    ADD COLUMN IF NOT EXISTS status LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS address String DEFAULT '',
    ADD COLUMN IF NOT EXISTS ticket String DEFAULT '',
    ADD COLUMN IF NOT EXISTS upstream_refresh String DEFAULT '';
