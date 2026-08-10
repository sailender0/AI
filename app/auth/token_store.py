"""
Encryption for the OAuth tokens held in `integrations.*_token_enc`.

Fernet (AES-128-CBC + HMAC) keyed by a SHA-256 of SECRET_KEY, so rotating
SECRET_KEY invalidates every stored token and users reconnect — that is the
intended blast radius, not a bug.

encrypt_token / decrypt_token stay async even though Fernet is pure-CPU: every
caller already awaits them, and a key store that needs real I/O (Key Vault, KMS)
drops in without touching app/auth/oauth.py.
"""
import base64
import hashlib

from cryptography.fernet import Fernet

from app.config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
        _fernet = Fernet(key)
    return _fernet


async def encrypt_token(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


async def decrypt_token(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def demo() -> None:
    """Self-check: a round-trip must return the input, and ciphertext must differ."""
    settings.SECRET_KEY = settings.SECRET_KEY or "test-key"
    import asyncio

    token = "ghp_example_token_value"
    enc = asyncio.run(encrypt_token(token))
    assert enc != token
    assert asyncio.run(decrypt_token(enc)) == token
    print("ok")


if __name__ == "__main__":
    demo()
