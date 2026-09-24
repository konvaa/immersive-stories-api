"""
tests/test_auth.py — Unit testy JWT verifikace (api/routes/auth.py).

Bez sítě a bez DB: ES256 klíče se generují lokálně a _resolve_key
se monkeypatchuje, aby nevolal JWKS endpoint.

Pokrytí:
  - validní ES256 token → vrátí payload se sub
  - padělaný podpis (cizí klíč) → InvalidTokenError
  - expirovaný token → ExpiredSignatureError
  - chybějící sub → InvalidTokenError
  - špatná audience → InvalidTokenError
  - alg=none / nesmyslný formát → InvalidTokenError
  - HS256 bez SUPABASE_JWT_SECRET → InvalidTokenError
  - HS256 se secretem → vrátí payload
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ec import (
    SECP256R1, generate_private_key,
)

from api.routes import auth as auth_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def es256_keys():
    """Dva ES256 páry: 'správný' a 'útočníkův'."""
    good = generate_private_key(SECP256R1())
    evil = generate_private_key(SECP256R1())
    return good, evil


@pytest.fixture(autouse=True)
def patch_jwks(monkeypatch, es256_keys):
    """ES256/RS256 tokeny se ověřují proti lokálnímu 'good' klíči (bez JWKS sítě)."""
    good, _ = es256_keys
    _orig = auth_mod._resolve_key
    monkeypatch.setattr(
        auth_mod, "_resolve_key",
        lambda token, alg: good.public_key()
        if alg in auth_mod._ASYMMETRIC_ALGS else _orig(token, alg),
    )


def _make_token(key, *, alg="ES256", exp_offset=3600,
                sub="user-123", aud="authenticated", **extra) -> str:
    payload = {"exp": int(time.time()) + exp_offset, "aud": aud, **extra}
    if sub is not None:
        payload["sub"] = sub
    return jwt.encode(payload, key, algorithm=alg)


# ---------------------------------------------------------------------------
# ES256
# ---------------------------------------------------------------------------

def test_valid_es256_token(es256_keys):
    good, _ = es256_keys
    payload = auth_mod.verify_token(_make_token(good))
    assert payload["sub"] == "user-123"


def test_forged_signature_rejected(es256_keys):
    """Token podepsaný cizím klíčem musí být odmítnut."""
    _, evil = es256_keys
    token = _make_token(evil, sub="victim-user")
    with pytest.raises(jwt.InvalidTokenError):
        auth_mod.verify_token(token)


def test_tampered_payload_rejected(es256_keys):
    """Změna payloadu (sub) po podpisu musí být odmítnuta."""
    import base64, json  # noqa: E401
    good, _ = es256_keys
    header_b64, payload_b64, sig_b64 = _make_token(good).split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    payload["sub"] = "someone-else"
    forged_payload = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=").decode()
    forged = f"{header_b64}.{forged_payload}.{sig_b64}"
    with pytest.raises(jwt.InvalidTokenError):
        auth_mod.verify_token(forged)


def test_expired_token_rejected(es256_keys):
    good, _ = es256_keys
    token = _make_token(good, exp_offset=-3600)
    with pytest.raises(jwt.ExpiredSignatureError):
        auth_mod.verify_token(token)


def test_missing_sub_rejected(es256_keys):
    good, _ = es256_keys
    token = _make_token(good, sub=None)
    with pytest.raises(jwt.InvalidTokenError):
        auth_mod.verify_token(token)


def test_wrong_audience_rejected(es256_keys):
    good, _ = es256_keys
    token = _make_token(good, aud="anon")
    with pytest.raises(jwt.InvalidTokenError):
        auth_mod.verify_token(token)


# ---------------------------------------------------------------------------
# Nevalidní vstupy
# ---------------------------------------------------------------------------

def test_garbage_token_rejected():
    with pytest.raises(jwt.InvalidTokenError):
        auth_mod.verify_token("not.a.jwt")


def test_alg_none_rejected():
    """Klasický 'alg: none' útok musí být odmítnut."""
    import base64, json  # noqa: E401
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "attacker", "aud": "authenticated",
                    "exp": int(time.time()) + 3600}).encode()
    ).rstrip(b"=").decode()
    with pytest.raises(jwt.InvalidTokenError):
        auth_mod.verify_token(f"{header}.{payload}.")


# ---------------------------------------------------------------------------
# HS256 (legacy)
# ---------------------------------------------------------------------------

def test_hs256_without_secret_rejected(monkeypatch):
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    token = _make_token("some-secret", alg="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        auth_mod.verify_token(token)


def test_hs256_with_secret_accepted(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    token = _make_token("test-secret", alg="HS256")
    assert auth_mod.verify_token(token)["sub"] == "user-123"


def test_hs256_wrong_secret_rejected(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    token = _make_token("wrong-secret", alg="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        auth_mod.verify_token(token)
