"""Encryption at rest for provider API keys stored in TiDB.

Keys in the ``api_keys`` table were previously stored as plaintext: anyone
with a database dump (or TiDB console access) owned every provider key.
This module encrypts keys with Fernet (AES-128-CBC + HMAC, from the
``cryptography`` package already in requirements.txt) before they are
written, and decrypts on read.

Key material: ``API_KEY_ENCRYPTION_KEY`` from .env / Render env vars.
- If it is already a valid Fernet key (44-char urlsafe base64), it is used
  as-is (generate one with ``python -c "from cryptography.fernet import
  Fernet; print(Fernet.generate_key().decode())"``).
- Otherwise it is treated as a passphrase and stretched with a single
  SHA-256 into a Fernet-compatible key. (Deliberately simple and
  deterministic so deploys stay stateless; the env var itself is the
  secret, exactly like CRON_SECRET.)
- If it is unset, encryption is a no-op and values are stored as plaintext
  (legacy behaviour) with a loud warning — set the variable before adding
  keys in production.

Backwards compatibility: values already stored as plaintext (pre-encryption
rows, and rows written while the variable was unset) are detected on read —
a Fernet ``InvalidToken`` means "legacy plaintext" — and returned as-is,
then transparently re-encrypted on the next write of that key. The seed
path in ``init_db`` encrypts the .env Gemini key on first insert.

Stored format: ``"fernet:v1:" + token`` so plaintext and ciphertext are
unambiguous and future key rotation can bump the version prefix.
"""
import base64
import hashlib
import logging

from app.config import settings

logger = logging.getLogger(__name__)

_PREFIX = "fernet:v1:"
_warned_no_key = False


def _load_fernet():
    """Return a Fernet instance, or None when no encryption key is set."""
    global _warned_no_key
    raw = (settings.API_KEY_ENCRYPTION_KEY or "").strip()
    if not raw:
        if not _warned_no_key:
            _warned_no_key = True
            logger.warning(
                "API_KEY_ENCRYPTION_KEY is not set: API keys will be stored "
                "in TiDB as PLAINTEXT. Set a random encryption key in .env / "
                "Render env vars to enable encryption at rest."
            )
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover - cryptography is in requirements.txt
        logger.error("cryptography package missing: cannot encrypt API keys.")
        return None
    try:
        # Already a Fernet key?
        return Fernet(raw.encode("utf-8"))
    except Exception:
        # Treat as a passphrase: SHA-256 -> urlsafe base64 Fernet key.
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
        return Fernet(key)


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an API key for storage. Plaintext passthrough if unconfigured."""
    if not plaintext:
        return plaintext
    if plaintext.startswith(_PREFIX):
        return plaintext  # Already encrypted; do not double-encrypt.
    fernet = _load_fernet()
    if fernet is None:
        return plaintext
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt_api_key(stored: str) -> str:
    """Decrypt a stored API key. Legacy plaintext values pass through."""
    if not stored:
        return stored
    if not stored.startswith(_PREFIX):
        return stored  # Legacy plaintext row (or written while unconfigured).
    fernet = _load_fernet()
    if fernet is None:
        logger.error(
            "Stored API key is encrypted but API_KEY_ENCRYPTION_KEY is not "
            "set: cannot decrypt. Set the same key used at write time."
        )
        return ""
    try:
        from cryptography.fernet import InvalidToken
    except ImportError:  # pragma: no cover
        return ""
    try:
        return fernet.decrypt(stored[len(_PREFIX):].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error("Stored API key failed to decrypt (wrong key or corrupt value).")
        return ""
    except Exception as e:
        logger.error(f"Stored API key failed to decrypt: {e}")
        return ""
