"""The instance key and the encrypted credential envelope (ADR 008).

Every secret Chronicle holds for the GitHub App (App private key PEM, OAuth
client secret, webhook secret) is encrypted at rest with a Fernet key derived
from `data/state/instance.key`: 32 random bytes, generated on first start,
mode 0600. It is never included in a backup bundle: a bundle without the
instance key is useless for decrypting the credentials it carries, which is
the point (spec section 11, README's backup note).
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

INSTANCE_KEY_FILE_NAME = "instance.key"
INSTANCE_KEY_BYTES = 32

__all__ = [
    "INSTANCE_KEY_FILE_NAME",
    "InvalidToken",
    "decrypt",
    "encrypt",
    "instance_id",
    "load_or_create_instance_key",
]


def load_or_create_instance_key(state_dir: Path) -> bytes:
    """Read the instance key, creating it exclusively on first start.

    `O_EXCL` makes the create-or-read race safe across processes the same way
    `TokenStore` uses a flock: whichever process's `os.open` wins writes the
    key, and the loser reads back what won.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / INSTANCE_KEY_FILE_NAME
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raw = path.read_bytes()
    else:
        raw = os.urandom(INSTANCE_KEY_BYTES)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
    if len(raw) != INSTANCE_KEY_BYTES:
        raise ValueError(f"{path} does not hold {INSTANCE_KEY_BYTES} bytes of key material")
    return raw


def instance_id(instance_key: bytes) -> str:
    """A short, non-secret identifier derived from the instance key.

    Used to build a default GitHub App name; sha256 rather than the key
    itself so the id can be logged or shown on the admin page without
    exposing anything an attacker could use to decrypt stored credentials.
    """
    return hashlib.sha256(instance_key).hexdigest()[:8]


def _fernet(instance_key: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(instance_key))


def encrypt(instance_key: bytes, plaintext: str) -> str:
    return _fernet(instance_key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(instance_key: bytes, token: str) -> str:
    return _fernet(instance_key).decrypt(token.encode("ascii")).decode("utf-8")
