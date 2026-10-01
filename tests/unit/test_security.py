"""rosetta.api.security: bearer tokens, roles and the signing key."""
from __future__ import annotations

import base64
import dataclasses
import json
import time
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from fastapi import HTTPException

from rosetta import config
from rosetta.api import security as S
from rosetta.api.security import ADMIN, ANALYST, ENGINEER, MANAGER, SERVICE, Principal, decode_token, issue_token

SECRET = "unit-test-secret-that-is-long-enough-0123456789-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv("ROSETTA_ENV", "test")
    monkeypatch.setenv("ROSETTA_JWT_SECRET", SECRET)
    monkeypatch.setattr(S, "_ephemeral_secret", None)
    monkeypatch.setattr(S, "_jwks", None)
    config.reset_settings()
    yield
    config.reset_settings()


def claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    c = {"iss": "rosetta", "sub": "7", "email": "engineer@rosetta.example", "roles": [ENGINEER], "name": "Eng",
         "iat": now, "nbf": now, "exp": now + 600}
    c.update(overrides)
    return {k: v for k, v in c.items() if v is not None}


def sign(payload: dict[str, Any], key: str = SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode(payload, key, algorithm=algorithm)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def rejected(token: str) -> HTTPException:
    with pytest.raises(HTTPException) as info:
        decode_token(token)
    assert info.value.status_code == 401
    assert info.value.headers == {"WWW-Authenticate": "Bearer"}
    assert info.value.detail.startswith("invalid token: ")
    return info.value


# ----------------------------------------------------------------- round trip
def test_issue_and_decode_round_trip():
    token, ttl = issue_token(7, "manager@northwind.example", [MANAGER], tenant_id=3, name="Mia")
    p = decode_token(token)
    assert (p.user_id, p.email, p.roles, p.tenant_id, p.name) == (7, "manager@northwind.example", (MANAGER,), 3, "Mia")
    assert ttl == 3600


def test_token_claims():
    before = int(time.time())
    token, ttl = issue_token(7, "a@b.example", [ADMIN, ENGINEER], tenant_id=None, ttl_s=120)
    c = decode_token(token).claims
    assert ttl == 120
    assert (c["iss"], c["sub"], c["email"], c["roles"]) == ("rosetta", "7", "a@b.example", [ADMIN, ENGINEER])
    assert before <= c["iat"] == c["nbf"] <= int(time.time())
    assert c["exp"] == c["iat"] + 120
    assert "tenant_id" not in c, "staff tokens carry no tenant"
    assert len(c["jti"]) == 16


def test_token_is_signed_with_hs256():
    token, _ = issue_token(7, "a@b.example", [ADMIN], None)
    assert jwt.get_unverified_header(token) == {"alg": "HS256", "typ": "JWT"}
    assert jwt.decode(token, SECRET, algorithms=["HS256"], issuer="rosetta")["sub"] == "7"


def test_every_token_has_its_own_id():
    ids = {decode_token(issue_token(7, "a@b.example", [ADMIN], None)[0]).claims["jti"] for _ in range(20)}
    assert len(ids) == 20


def test_lifetime_and_issuer_come_from_the_settings(monkeypatch):
    monkeypatch.setenv("ROSETTA_JWT_TTL_S", "90")
    monkeypatch.setenv("ROSETTA_JWT_ISSUER", "rosetta-staging")
    config.reset_settings()
    token, ttl = issue_token(7, "a@b.example", [ADMIN], None)
    c = decode_token(token).claims
    assert ttl == 90 and c["exp"] - c["iat"] == 90 and c["iss"] == "rosetta-staging"


def test_tenant_zero_is_a_tenant():
    assert decode_token(issue_token(7, "a@b.example", [MANAGER], tenant_id=0)[0]).tenant_id == 0


# ------------------------------------------------------------------- rejection
def test_expired_token():
    token, _ = issue_token(7, "a@b.example", [ADMIN], None, ttl_s=-5)
    assert rejected(token).detail == "invalid token: ExpiredSignatureError"


def test_token_that_is_not_valid_yet():
    assert rejected(sign(claims(nbf=int(time.time()) + 600))).detail == "invalid token: ImmatureSignatureError"


def test_algorithm_none_is_rejected():
    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = b64(json.dumps(claims(roles=[ADMIN])).encode())
    for token in (f"{header}.{body}.", f"{header}.{body}.AAAA"):
        assert "Error" in rejected(token).detail


def test_algorithm_none_with_the_original_signature_is_rejected():
    token, _ = issue_token(7, "a@b.example", [ANALYST], None)
    _, body, signature = token.split(".")
    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    rejected(f"{header}.{body}.{signature}")


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_other_hmac_algorithms_are_rejected_even_with_the_right_key(algorithm):
    assert rejected(sign(claims(), algorithm=algorithm)).detail == "invalid token: InvalidAlgorithmError"


def test_tampered_signature():
    token, _ = issue_token(7, "a@b.example", [ANALYST], None)
    head, body, signature = token.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    assert rejected(f"{head}.{body}.{flipped}").detail == "invalid token: InvalidSignatureError"


def test_tampered_payload():
    token, _ = issue_token(7, "a@b.example", [ANALYST], None)
    head, body, signature = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["roles"] = [ADMIN]                            # privilege escalation attempt
    forged = b64(json.dumps(payload, separators=(",", ":")).encode())
    assert rejected(f"{head}.{forged}.{signature}").detail == "invalid token: InvalidSignatureError"


def test_token_signed_with_another_key():
    other = sign(claims(), key="another-secret-that-is-also-long-enough-987654321")
    assert rejected(other).detail == "invalid token: InvalidSignatureError"


def test_token_of_another_issuer():
    assert rejected(sign(claims(iss="someone-else"))).detail == "invalid token: InvalidIssuerError"


@pytest.mark.parametrize("missing", ["exp", "sub", "iat", "iss"])
def test_token_without_a_required_claim(missing):
    assert "MissingRequiredClaimError" in rejected(sign(claims(**{missing: None}))).detail


@pytest.mark.parametrize("token", ["", "garbage", "a.b", "a.b.c", "a.b.c.d", "Bearer x.y.z", "....", "é.é.é"])
def test_garbage_is_rejected(token):
    rejected(token)


def test_a_token_is_useless_after_the_key_changed(monkeypatch):
    token, _ = issue_token(7, "a@b.example", [ADMIN], None)
    monkeypatch.setenv("ROSETTA_JWT_SECRET", "rotated-secret-that-is-long-enough-abcdefghijkl")
    config.reset_settings()
    rejected(token)


# ------------------------------------------------------------------- principal
def test_roles_without_a_list_mean_no_roles():
    p = decode_token(sign(claims(roles=None)))
    assert p.roles == () and not p.has(ADMIN, ENGINEER, ANALYST, MANAGER)


def test_subject_that_is_not_a_number():
    p = decode_token(sign(claims(sub="svc-normalizer", email=None)))
    assert p.user_id is None
    assert p.email == "svc-normalizer", "the subject stands in for the address"


def test_roles_are_turned_into_strings():
    assert decode_token(sign(claims(roles=["admin", 5]))).roles == ("admin", "5")


def test_tenant_id_is_an_integer():
    assert decode_token(sign(claims(tenant_id="12"))).tenant_id == 12
    assert decode_token(sign(claims())).tenant_id is None


def test_has_needs_one_of_the_roles():
    p = Principal(1, "a@b.example", (ANALYST, MANAGER))
    assert p.has(ANALYST) and p.has(ADMIN, MANAGER)
    assert not p.has(ADMIN) and not p.has(ADMIN, ENGINEER) and not p.has()
    assert not p.has("analys"), "role names are compared whole"


@pytest.mark.parametrize("roles,location,personal", [
    ((ADMIN,), True, True), ((ENGINEER,), True, False), ((MANAGER,), True, True), ((ANALYST,), False, False),
    ((SERVICE,), False, False), ((), False, False), ((ANALYST, MANAGER), True, True),
])
def test_what_each_role_may_see(roles, location, personal):
    p = Principal(1, "a@b.example", roles)
    assert p.sees_precise_location is location
    assert p.sees_personal_data is personal


def test_role_groups():
    assert S.STAFF == (ADMIN, ENGINEER, ANALYST)
    assert S.OPERATORS == (ADMIN, ENGINEER)
    assert S.EVERYONE == (ADMIN, ENGINEER, ANALYST, MANAGER)
    assert SERVICE not in S.EVERYONE


def test_principal_cannot_be_changed():
    p = Principal(1, "a@b.example", (ANALYST,))
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.roles = (ADMIN,)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.tenant_id = 9


def test_principals_compare_by_identity_not_by_claims():
    a = Principal(1, "a@b.example", (ANALYST,), claims={"jti": "1"})
    b = Principal(1, "a@b.example", (ANALYST,), claims={"jti": "2"})
    assert a == b and hash(a) == hash(b)
    assert a != Principal(1, "a@b.example", (ADMIN,))


# ------------------------------------------------------------------ signing key
def test_secret_from_the_environment():
    assert S.secret() == SECRET


def test_short_secret_is_refused(monkeypatch):
    monkeypatch.setenv("ROSETTA_JWT_SECRET", "x" * 31)
    config.reset_settings()
    with pytest.raises(RuntimeError, match="at least 32"):
        S.secret()
    with pytest.raises(RuntimeError):
        issue_token(7, "a@b.example", [ADMIN], None)


def test_secret_of_32_characters_is_accepted(monkeypatch):
    monkeypatch.setenv("ROSETTA_JWT_SECRET", "x" * 32)
    config.reset_settings()
    assert S.secret() == "x" * 32


@pytest.mark.parametrize("env", ["local", "test"])
def test_local_mode_makes_up_a_key_for_the_life_of_the_process(monkeypatch, env):
    monkeypatch.delenv("ROSETTA_JWT_SECRET")
    monkeypatch.setenv("ROSETTA_ENV", env)
    config.reset_settings()
    first = S.secret()
    assert len(first) >= 48 and first == S.secret()
    token, _ = issue_token(7, "a@b.example", [ADMIN], None)
    assert decode_token(token).user_id == 7


@pytest.mark.parametrize("env", ["production", "staging", "prod", ""])
def test_a_secret_is_required_outside_local_mode(monkeypatch, env):
    monkeypatch.delenv("ROSETTA_JWT_SECRET")
    monkeypatch.setenv("ROSETTA_ENV", env)
    config.reset_settings()
    with pytest.raises(RuntimeError, match="required outside local mode"):
        S.secret()


# ---------------------------------------------------------------- dependencies
def fake_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


def test_current_user_needs_a_token():
    for token in (None, ""):
        with pytest.raises(HTTPException) as info:
            S.current_user(fake_request(), token)
        assert info.value.status_code == 401 and info.value.detail == "authentication required"
        assert info.value.headers == {"WWW-Authenticate": "Bearer"}


def test_current_user_puts_the_principal_on_the_request():
    req = fake_request()
    token, _ = issue_token(7, "a@b.example", [ANALYST], None)
    p = S.current_user(req, token)
    assert p.email == "a@b.example" and req.state.principal is p


def test_current_user_rejects_a_bad_token():
    req = fake_request()
    with pytest.raises(HTTPException) as info:
        S.current_user(req, "garbage")
    assert info.value.status_code == 401
    assert not hasattr(req.state, "principal")


def test_require_lets_a_matching_role_through():
    dep = S.require(ADMIN, ENGINEER)
    p = Principal(1, "e@rosetta.example", (ENGINEER,))
    req = fake_request()
    assert dep(req, p) is p
    assert not hasattr(req.state, "denied")


def test_require_refuses_other_roles_with_403():
    dep = S.require(ADMIN, ENGINEER)
    req = fake_request()
    with pytest.raises(HTTPException) as info:
        dep(req, Principal(1, "a@rosetta.example", (ANALYST,)))
    assert info.value.status_code == 403
    assert info.value.detail == "requires one of: admin, platform_engineer"
    assert req.state.denied is True


def test_require_refuses_a_principal_without_roles():
    with pytest.raises(HTTPException) as info:
        S.require(*S.EVERYONE)(fake_request(), Principal(None, "svc", ()))
    assert info.value.status_code == 403


# ------------------------------------------------------------------------ OIDC
@pytest.fixture(scope="module")
def rsa_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def oidc(monkeypatch, rsa_key):
    """An external identity provider whose key set is already known: no network."""
    monkeypatch.setenv("ROSETTA_OIDC_JWKS_URL", "https://idp.example/jwks")
    config.reset_settings()
    public = rsa_key.public_key()
    seen = []

    class Jwks:
        def get_signing_key_from_jwt(self, token: str):
            seen.append(token)
            return SimpleNamespace(key=public)

    monkeypatch.setattr(S, "_jwks", Jwks())
    return seen


def idp_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    c = {"sub": "f3c1-idp-user", "email": "ops@customer.example", "aud": "rosetta-api", "iat": now, "exp": now + 300,
         "realm_access": {"roles": [MANAGER]}, "tenant_id": 4}
    c.update(overrides)
    return {k: v for k, v in c.items() if v is not None}


def test_token_of_the_identity_provider_is_accepted(oidc, rsa_key):
    token = jwt.encode(idp_claims(), rsa_key, algorithm="RS256")
    p = decode_token(token)
    assert (p.user_id, p.email, p.roles, p.tenant_id) == (None, "ops@customer.example", (MANAGER,), 4)
    assert oidc == [token]


def test_roles_claim_wins_over_realm_roles(oidc, rsa_key):
    token = jwt.encode(idp_claims(roles=[ANALYST]), rsa_key, algorithm="RS256")
    assert decode_token(token).roles == (ANALYST,)


def test_token_for_another_audience_is_rejected(oidc, rsa_key):
    assert rejected(jwt.encode(idp_claims(aud="another-api"), rsa_key, algorithm="RS256")).detail == \
        "invalid token: InvalidAudienceError"


def test_locally_signed_token_is_rejected_when_an_identity_provider_is_configured(oidc):
    assert "Error" in rejected(sign(claims(aud="rosetta-api"))).detail


def test_expired_identity_provider_token_is_rejected(oidc, rsa_key):
    now = int(time.time())
    token = jwt.encode(idp_claims(iat=now - 900, exp=now - 600), rsa_key, algorithm="RS256")
    assert rejected(token).detail == "invalid token: ExpiredSignatureError"


def test_token_signed_by_someone_else_is_rejected(oidc):
    from cryptography.hazmat.primitives.asymmetric import rsa

    intruder = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert rejected(jwt.encode(idp_claims(), intruder, algorithm="RS256")).detail == \
        "invalid token: InvalidSignatureError"


def test_key_set_client_is_created_once(monkeypatch, rsa_key):
    monkeypatch.setenv("ROSETTA_OIDC_JWKS_URL", "https://idp.example/jwks")
    config.reset_settings()
    created = []

    class Client:
        def __init__(self, url: str, cache_keys: bool = False) -> None:
            created.append((url, cache_keys))

        def get_signing_key_from_jwt(self, token: str):
            return SimpleNamespace(key=rsa_key.public_key())

    monkeypatch.setattr(S.jwt, "PyJWKClient", Client)
    token = jwt.encode(idp_claims(), rsa_key, algorithm="RS256")
    decode_token(token)
    decode_token(token)
    assert created == [("https://idp.example/jwks", True)]
