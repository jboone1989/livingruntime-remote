from __future__ import annotations

import os
import time
from typing import Any

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier


class OIDCJWTVerifier(TokenVerifier):
    """Validate OAuth/OIDC JWT access tokens issued by an external provider."""

    def __init__(self, issuer: str, audience: str, jwks_url: str, resource: str) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.resource = resource
        self.jwks = PyJWKClient(jwks_url, cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
            scope_value = claims.get("scope") or claims.get("scp") or ""
            scopes = scope_value if isinstance(scope_value, list) else str(scope_value).split()
            return AccessToken(
                token=token,
                client_id=str(claims.get("azp") or claims.get("client_id") or claims["sub"]),
                scopes=scopes,
                expires_at=int(claims["exp"]),
                resource=self.resource,
                subject=str(claims["sub"]),
                claims=claims,
            )
        except Exception:
            return None


def verifier_from_env() -> OIDCJWTVerifier:
    required = {
        "issuer": os.environ.get("LIVINGRUNTIME_RELAY_ISSUER"),
        "audience": os.environ.get("LIVINGRUNTIME_RELAY_AUDIENCE"),
        "jwks_url": os.environ.get("LIVINGRUNTIME_RELAY_JWKS_URL"),
        "resource": os.environ.get("LIVINGRUNTIME_RELAY_RESOURCE_URL"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("missing relay OAuth settings: " + ", ".join(missing))
    return OIDCJWTVerifier(**required)
