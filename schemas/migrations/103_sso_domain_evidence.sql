-- Missing sign-in evidence denies hosted-domain permissions.
ALTER TABLE loom.sso_links ADD COLUMN IF NOT EXISTS hosted_domain String DEFAULT '';
ALTER TABLE loom.sso_links ADD COLUMN IF NOT EXISTS issuer String DEFAULT '';
