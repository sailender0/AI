"""
Thin wrapper around Azure Key Vault for secret storage.
Falls back to local encryption when AZURE_KEYVAULT_URL is not set (dev mode).
"""
import base64
import os

from cryptography.fernet import Fernet

from app.config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = base64.urlsafe_b64encode(settings.SECRET_KEY.encode().ljust(32)[:32])
        _fernet = Fernet(key)
    return _fernet


def encrypt_token(plaintext: str) -> str:
    if settings.AZURE_KEYVAULT_URL:
        return _keyvault_encrypt(plaintext)
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    if settings.AZURE_KEYVAULT_URL:
        return _keyvault_decrypt(ciphertext)
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def _keyvault_encrypt(plaintext: str) -> str:
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=settings.AZURE_KEYVAULT_URL, credential=DefaultAzureCredential())
    secret_name = f"token-{os.urandom(8).hex()}"
    client.set_secret(secret_name, plaintext)
    return secret_name


def _keyvault_decrypt(secret_name: str) -> str:
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=settings.AZURE_KEYVAULT_URL, credential=DefaultAzureCredential())
    return client.get_secret(secret_name).value
