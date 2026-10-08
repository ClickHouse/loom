"""Auth — JWT mint/verify + role checks.

Verifies JWTs signed HS256 (shared secret) or RS256 (JWKS public key);
the header `kid` selects the key during rotation. Federated deployments
can mint Loom JWTs upstream (oauth2-proxy, Cloudflare Access, custom);
brokered OIDC login is also supported (server/auth/oidc.py).
"""
