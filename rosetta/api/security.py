"""Authentication and authorisation.

Tokens   OAuth2 bearer tokens, JWT. Locally the API issues them itself
         (password grant, HS256). With ROSETTA_OIDC_JWKS_URL set it accepts
         tokens from an external identity provider instead (OIDC, RS256),
         which is how it is meant to run in production.
RBAC     Roles travel in the token. Each endpoint names the roles it accepts.
Tenancy  A token may carry a tenant id. Every query for vehicle, driver or
         alert data is filtered by it on the server. A tenant id is never
         taken from the request.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer

from ..config import get_settings

ALGORITHM = "HS256"
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)
_ephemeral_secret: str | None = None
_jwks: Any = None

ADMIN, ENGINEER, ANALYST, MANAGER, SERVICE = "admin", "platform_engineer", "analyst", "fleet_manager", "service"
STAFF = (ADMIN, ENGINEER, ANALYST)
OPERATORS = (ADMIN, ENGINEER)
EVERYONE = (ADMIN, ENGINEER, ANALYST, MANAGER)


@dataclass(frozen=True)
class Principal:
    user_id: int | None
    email: str
    roles: tuple[str, ...]
    tenant_id: int | None = None
    name: str = ""
    claims: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def has(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)

    @property
    def sees_precise_location(self) -> bool:
        """Precise positions are for people who operate the vehicles or the platform."""
        return self.has(ADMIN, ENGINEER, MANAGER)

    @property
    def sees_personal_data(self) -> bool:
        return self.has(ADMIN, MANAGER)


def secret() -> str:
    """Signing key. Must come from the environment in any shared deployment."""
    global _ephemeral_secret
    s = get_settings().jwt_secret
    if s:
        if len(s) < 32:
            raise RuntimeError("ROSETTA_JWT_SECRET must be at least 32 characters")
        return s
    if get_settings().env not in ("local", "test"):
        raise RuntimeError("ROSETTA_JWT_SECRET is required outside local mode")
    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_urlsafe(48)   # tokens die with the process
    return _ephemeral_secret


def issue_token(user_id: int, email: str, roles: list[str], tenant_id: int | None, name: str = "",
                ttl_s: int | None = None) -> tuple[str, int]:
    st = get_settings()
    now = int(time.time())
    ttl = ttl_s or st.jwt_ttl_s
    claims = {"iss": st.jwt_issuer, "sub": str(user_id), "email": email, "roles": roles, "name": name,
              "iat": now, "nbf": now, "exp": now + ttl, "jti": secrets.token_hex(8)}
    if tenant_id is not None:
        claims["tenant_id"] = tenant_id
    return jwt.encode(claims, secret(), algorithm=ALGORITHM), ttl


def decode_token(token: str) -> Principal:
    st = get_settings()
    try:
        if st.oidc_jwks_url:
            global _jwks
            if _jwks is None:
                _jwks = jwt.PyJWKClient(st.oidc_jwks_url, cache_keys=True)
            key = _jwks.get_signing_key_from_jwt(token).key
            c = jwt.decode(token, key, algorithms=["RS256", "ES256"], audience=st.oidc_audience,
                           options={"require": ["exp", "sub"]})
            roles = c.get("roles") or (c.get("realm_access") or {}).get("roles") or []
        else:
            # The algorithm list is fixed: a token that names "none" or another
            # algorithm in its header is rejected.
            c = jwt.decode(token, secret(), algorithms=[ALGORITHM], issuer=st.jwt_issuer,
                           options={"require": ["exp", "sub", "iat"]})
            roles = c.get("roles") or []
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"invalid token: {type(e).__name__}", {"WWW-Authenticate": "Bearer"}) from None
    sub = c.get("sub")
    tid = c.get("tenant_id")
    return Principal(user_id=int(sub) if str(sub).isdigit() else None, email=c.get("email", str(sub)),
                     roles=tuple(str(r) for r in roles), tenant_id=int(tid) if tid is not None else None,
                     name=c.get("name", ""), claims=c)


def current_user(request: Request, token: str | None = Depends(oauth2)) -> Principal:
    if not token:
        raise HTTPException(401, "authentication required", {"WWW-Authenticate": "Bearer"})
    p = decode_token(token)
    request.state.principal = p
    return p


def require(*roles: str):
    """Dependency: the caller must hold at least one of `roles`."""

    def dep(request: Request, p: Principal = Depends(current_user)) -> Principal:
        if not p.has(*roles):
            request.state.denied = True
            raise HTTPException(403, f"requires one of: {', '.join(roles)}")
        return p

    return dep
