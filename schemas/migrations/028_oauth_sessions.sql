-- Refresh-token sessions in loom.oauth_state.
--
-- family: the sign-in session a refresh token belongs to. Every access token
-- of the session carries it as its sid, so revoking the family (logout, a
-- replayed refresh token) ends all of them.
-- upstream_refresh: the identity provider's refresh token, sealed, so each
-- Loom refresh can ask the provider whether the user is still active.
ALTER TABLE loom.oauth_state
    ADD COLUMN IF NOT EXISTS family String DEFAULT '',
    ADD COLUMN IF NOT EXISTS upstream_refresh String DEFAULT '';
