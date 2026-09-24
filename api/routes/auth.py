"""
api/routes/auth.py — Supabase JWT middleware (GDD 19.1).

Ověřuje JWT tokeny vydané Supabase Auth včetně KRYPTOGRAFICKÉHO PODPISU.
Nikdy nedekóduj payload bez verifikace — padělaný token s cizím `sub`
by jinak umožnil přístup k cizím kampaním.

Podporované algoritmy:
  - ES256 / RS256: veřejný klíč z JWKS endpointu projektu
    ({SUPABASE_URL}/auth/v1/.well-known/jwks.json), cachováno přes PyJWKClient.
  - HS256 (legacy projekty): symetrický secret z env SUPABASE_JWT_SECRET.

Použití v routes:
    from api.routes.auth import get_current_user_id
    user_id: Annotated[str, Depends(get_current_user_id)]
"""

from __future__ import annotations

import logging
import os
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
security = HTTPBearer()

# Algoritmy, které akceptujeme. Nikdy nepřidávat "none".
_ASYMMETRIC_ALGS = {"ES256", "RS256"}
_SYMMETRIC_ALGS = {"HS256"}

_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    """Singleton PyJWKClient s interní cache klíčů (lifespan 1 h)."""
    global _jwks_client
    if _jwks_client is None:
        base_url = os.environ["SUPABASE_URL"].rstrip("/")
        _jwks_client = PyJWKClient(
            f"{base_url}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
            lifespan=3600,
        )
    return _jwks_client


def _resolve_key(token: str, alg: str):
    """Vrátí verifikační klíč podle algoritmu v hlavičce tokenu."""
    if alg in _ASYMMETRIC_ALGS:
        return _get_jwks_client().get_signing_key_from_jwt(token).key
    if alg in _SYMMETRIC_ALGS:
        secret = os.environ.get("SUPABASE_JWT_SECRET")
        if not secret:
            raise jwt.InvalidTokenError(
                "HS256 token, ale SUPABASE_JWT_SECRET není nastaven"
            )
        return secret
    raise jwt.InvalidAlgorithmError(f"Nepodporovaný algoritmus: {alg}")


def verify_token(token: str) -> dict:
    """
    Plně ověří JWT (podpis, exp, aud) a vrátí payload.

    Raises:
        jwt.InvalidTokenError (a podtřídy) při jakémkoli selhání.
    """
    header = jwt.get_unverified_header(token)
    alg = header.get("alg", "")
    key = _resolve_key(token, alg)
    return jwt.decode(
        token,
        key,
        algorithms=[alg],
        audience="authenticated",
        leeway=30,  # tolerance drobného clock skew
        options={"require": ["exp", "sub"]},
    )


async def get_current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
) -> str:
    """FastAPI dependency: Bearer token → ověřený user_id (sub)."""
    try:
        payload = verify_token(credentials.credentials)
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expiroval — přihlas se znovu.",
        )
    except jwt.exceptions.PyJWKClientConnectionError as exc:
        logger.error("JWKS endpoint nedostupný: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ověřovací služba dočasně nedostupná, zkus to znovu.",
        )
    except jwt.exceptions.PyJWKClientError as exc:
        logger.warning("Podpisový klíč nenalezen (rotace klíčů?): %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token je podepsán neznámým klíčem — přihlas se znovu.",
        )
    except jwt.InvalidTokenError as exc:
        logger.warning("JWT verifikace selhala: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Neplatný JWT token.",
        )
    except Exception as exc:
        logger.error("JWT verifikace — neočekávaná chyba: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ověření tokenu selhalo.",
        )
