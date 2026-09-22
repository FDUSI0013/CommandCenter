"""Password hashing, API-key minting/verification, session tokens, and the
symmetric envelope used for secret material at rest.

No third-party auth service is involved: the server is the identity
authority for both browser sessions and SDK API keys.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar

from cryptography.fernet import Fernet, InvalidToken
from passlib.context import CryptContext

from .config import settings

_T = TypeVar("_T")

# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------

_pwd = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    return _pwd.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return _pwd.verify(raw, hashed)
    except ValueError:
        return False


def needs_rehash(hashed: str) -> bool:
    return _pwd.needs_update(hashed)


# argon2 is slow and memory-hard on purpose: one hash is tens of milliseconds of
# CPU and 64 MiB of working memory. Called straight from an ``async`` handler it
# holds that worker's event loop for the duration, and everything else the loop
# is serving -- ingest batches, the live-runs stream -- stands still behind a
# sign-in. The hash releases the GIL, so a thread is all it takes to give the
# loop back. The pool is deliberately small: it is what bounds the memory a
# burst of sign-ins can claim (two hashes at a time per worker, the rest queue),
# which ``asyncio.to_thread`` and its 32-thread default pool would not.
PASSWORD_POOL_WORKERS = 2
_password_pool = ThreadPoolExecutor(
    max_workers=PASSWORD_POOL_WORKERS, thread_name_prefix="password-hash"
)


async def off_loop(work: Callable[..., _T], *args: Any) -> _T:
    """Run one password hash or verification without blocking the event loop."""
    return await asyncio.get_running_loop().run_in_executor(_password_pool, work, *args)


# --------------------------------------------------------------------------
# API keys  (format: fo_<env>_<26 char id>_<52 char secret>, base32 fields)
# --------------------------------------------------------------------------

API_KEY_PREFIX = "fo"
_ID_BYTES = 16
_SECRET_BYTES = 32


@dataclass(frozen=True)
class MintedApiKey:
    """The only moment the full key exists in memory."""

    token: str  # show once, never stored
    key_id: str  # stored, safe to display
    secret_hash: str  # stored
    display_hint: str  # e.g. "fo_live_7Fq…9tQ"


def _env_tag() -> str:
    return "live" if settings.environment == "production" else "test"


def _encode(raw: bytes) -> str:
    """Encode key material for a token whose fields are split on ``_``.

    Base32 rather than base64url: the urlsafe alphabet contains ``_`` and ``-``,
    so an encoded field could carry the delimiter and ``parse_api_key`` would cut
    the token in the wrong place. RFC 4648 base32 is ``a-z2-7`` only, which makes
    the field boundaries unambiguous by construction. The cost is length, not
    entropy — the byte count is unchanged.
    """
    return base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def mint_api_key() -> MintedApiKey:
    key_id = _encode(secrets.token_bytes(_ID_BYTES))
    raw_secret = _encode(secrets.token_bytes(_SECRET_BYTES))
    token = f"{API_KEY_PREFIX}_{_env_tag()}_{key_id}_{raw_secret}"
    return MintedApiKey(
        token=token,
        key_id=key_id,
        secret_hash=hash_api_secret(raw_secret),
        display_hint=f"{token[:11]}…{token[-4:]}",
    )


def hash_api_secret(raw_secret: str) -> str:
    """SHA-256 rather than argon2: verified on every ingest request, and the
    input is 256 bits of CSPRNG entropy so it is not brute-forceable."""
    return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()


def parse_api_key(token: str) -> tuple[str, str] | None:
    """Split a presented key into (key_id, raw_secret)."""
    parts = token.strip().split("_")
    if len(parts) != 4 or parts[0] != API_KEY_PREFIX:
        return None
    _, _, key_id, raw_secret = parts
    if not key_id or not raw_secret:
        return None
    return key_id, raw_secret


def api_secret_matches(raw_secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_secret(raw_secret), stored_hash)


# --------------------------------------------------------------------------
# Session tokens (compact signed JWT-like envelope, HS256)
# --------------------------------------------------------------------------


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64u_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_session_token(
    *, user_id: str, workspace_id: str, role: str, ttl_minutes: int | None = None
) -> str:
    now = dt.datetime.now(dt.UTC)
    ttl = ttl_minutes if ttl_minutes is not None else settings.session_ttl_minutes
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "ws": workspace_id,
        "role": role,
        # Sub-second, deliberately. `iat` is compared against
        # `users.credentials_changed_at` to end the sessions a password change
        # replaces, and at whole-second resolution a token minted in the same
        # second as the change cannot be told from one minted just after it --
        # so it would survive the change for the rest of its life. Nothing else
        # reads this claim, and NumericDate permits a fractional part.
        "iat": now.timestamp(),
        "exp": int((now + dt.timedelta(minutes=ttl)).timestamp()),
        "iss": settings.service_name,
    }
    signing_input = f"{_b64u(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64u(json.dumps(payload, separators=(',', ':')).encode())}"
    sig = hmac.new(settings.secret_key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64u(sig)}"


class SessionTokenError(Exception):
    pass


def decode_session_token(token: str) -> dict:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError as exc:
        raise SessionTokenError("malformed token") from exc

    # A forged token need not carry valid base64 or JSON; a decode failure here
    # is a bad token, not a server error, so it must surface as one.
    try:
        signature = _b64u_decode(sig_b64)
    except (binascii.Error, ValueError) as exc:
        raise SessionTokenError("malformed signature") from exc

    signing_input = f"{header_b64}.{payload_b64}"
    expected = hmac.new(
        settings.secret_key.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, signature):
        raise SessionTokenError("bad signature")

    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except (binascii.Error, ValueError) as exc:
        raise SessionTokenError("malformed payload") from exc
    if not isinstance(payload, dict):
        raise SessionTokenError("malformed payload")
    if payload.get("exp", 0) < dt.datetime.now(dt.UTC).timestamp():
        raise SessionTokenError("expired")
    return payload


# --------------------------------------------------------------------------
# Envelope encryption for secret material at rest
# --------------------------------------------------------------------------


def _fernet() -> Fernet:
    key = settings.encryption_key
    if not key:
        if not settings.is_local:
            raise RuntimeError("FULCRUM_OPS_ENCRYPTION_KEY must be set outside local dev")
        # Deterministic dev key derived from the (dev) secret key so restarts
        # can still read locally stored values.
        key = base64.urlsafe_b64encode(
            hashlib.sha256(settings.secret_key.encode()).digest()
        ).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def generate_encryption_key() -> str:
    return Fernet.generate_key().decode()


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("stored secret could not be decrypted with the current key") from exc


def mask_secret(plaintext: str, *, keep: int = 4) -> str:
    """Render a value for display without revealing it."""
    if len(plaintext) <= keep:
        return "•" * 8
    return "•" * 12 + plaintext[-keep:]
