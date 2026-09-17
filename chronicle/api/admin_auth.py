"""Admin bootstrap: the one-time claim code, the password, and the session.

Ports the shape of coppermind's admin claim page (exclusive-create claim
record, argon2 password hash, HMAC-signed stateless session cookie, secret
rotation on re-claim) rather than its code: Chronicle's admin lives inside
the api process, not a separate service, and its record sits beside the
GitHub App credentials this round adds.

The claim code is a bootstrap secret with a short life (deleted the moment
it is used) rather than a long-lived one, so it is hashed with plain
`secrets.compare_digest` on the raw string, the same class of check ADR 007
uses for consumer tokens; the admin password is a human-chosen, low-entropy
secret held indefinitely, so it goes through argon2 instead.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .atomic import write_atomic

ADMIN_RECORD_FILE_NAME = "admin.json"
CLAIM_CODE_FILE_NAME = "claim-code"
CLAIM_CODE_BYTES = 18
MIN_PASSWORD_LENGTH = 12
SESSION_LIFETIME_HOURS = 12
SESSION_COOKIE_NAME = "chronicle_admin_session"
GITHUB_STATE_COOKIE_NAME = "chronicle_admin_github_state"

_HASHER = PasswordHasher()


class AdminAuthError(Exception):
    """Base of every admin-auth failure; each subclass names a distinct cause."""


class AlreadyClaimed(AdminAuthError):
    pass


class InvalidClaimCode(AdminAuthError):
    pass


class PasswordTooShort(AdminAuthError):
    pass


class WrongPassword(AdminAuthError):
    pass


class AdminRecordUnreadable(AdminAuthError):
    """`admin.json` exists but its shape cannot be trusted.

    Deliberately distinct from `WrongPassword`: a corrupted record is an
    operator problem (restore from backup, or delete and re-claim), never a
    typed-password problem, and the two must never be reported the same way.
    """


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class AdminCredentials:
    state_dir: Path

    def __post_init__(self) -> None:
        self.path = self.state_dir / ADMIN_RECORD_FILE_NAME
        self.claim_code_path = self.state_dir / CLAIM_CODE_FILE_NAME
        self._claim_lock = threading.Lock()

    def is_claimed(self) -> bool:
        return self.path.is_file()

    def ensure_claim_code(self) -> None:
        """Write a fresh one-time claim code if the instance is unclaimed.

        Called on every api start. An already-claimed instance is untouched;
        an unclaimed one gets a code if it does not already have one, so a
        restart before the first claim does not invalidate the code an
        operator already has in hand.
        """
        if self.is_claimed() or self.claim_code_path.exists():
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        code = secrets.token_urlsafe(CLAIM_CODE_BYTES)
        write_atomic(self.claim_code_path, code + "\n", mode=0o600)

    def _record(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdminRecordUnreadable(f"{self.path} could not be read: {exc}") from exc

    def claim(self, code: str, password: str) -> None:
        with self._claim_lock:
            if self.is_claimed():
                raise AlreadyClaimed
            if len(password) < MIN_PASSWORD_LENGTH:
                raise PasswordTooShort
            try:
                expected = self.claim_code_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise InvalidClaimCode from exc
            # Compared as bytes: a code pasted from a terminal can carry a
            # byte compare_digest refuses to compare as str.
            if not expected or not secrets.compare_digest(
                code.strip().encode("utf-8", "replace"), expected.encode("utf-8", "replace")
            ):
                raise InvalidClaimCode
            self._write_record(password)
            with contextlib.suppress(OSError):
                self.claim_code_path.unlink(missing_ok=True)

    def _write_record(self, password: str) -> None:
        body = {
            "schema_version": 1,
            "password_hash": _HASHER.hash(password),
            "session_secret": secrets.token_hex(32),
            "claimed_at": _utcnow().isoformat(),
        }
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(body, indent=2) + "\n")

    def verify_password(self, password: str) -> None:
        record = self._record()
        encoded = record.get("password_hash")
        if not isinstance(encoded, str):
            raise AdminRecordUnreadable("password_hash is missing or not a string")
        try:
            _HASHER.verify(encoded, password)
        except VerifyMismatchError as exc:
            raise WrongPassword from exc
        except (VerificationError, InvalidHashError) as exc:
            raise AdminRecordUnreadable(
                f"password_hash is not a usable argon2 hash: {exc}"
            ) from exc

    def change_password(self, current_password: str, new_password: str) -> None:
        """Rotate the session secret as a side effect of any password change.

        Every outstanding cookie is signed against the old secret, so this is
        what "changing the password ends every session" means in practice:
        there is nothing else to revoke.
        """
        with self._claim_lock:
            self.verify_password(current_password)
            if len(new_password) < MIN_PASSWORD_LENGTH:
                raise PasswordTooShort
            self._write_record(new_password)

    def session_secret(self) -> bytes:
        record = self._record()
        encoded = record.get("session_secret")
        if not isinstance(encoded, str) or len(encoded) != 64:
            raise AdminRecordUnreadable("session_secret is not a 32-byte hex value")
        try:
            secret = bytes.fromhex(encoded)
        except ValueError as exc:
            raise AdminRecordUnreadable("session_secret is not a 32-byte hex value") from exc
        return secret


class SignedSessions:
    """Session state carried entirely in a signed cookie: no server-side row.

    Rotating `session_secret` (claim, re-claim, password change) invalidates
    every outstanding cookie at once, since validity is a signature check
    against whatever secret `admin.json` holds right now.
    """

    def __init__(
        self,
        credentials: AdminCredentials,
        lifetime: timedelta = timedelta(hours=SESSION_LIFETIME_HOURS),
        now: Any = None,
    ) -> None:
        self.credentials = credentials
        self.lifetime = lifetime
        self._now = now or _utcnow

    def create(self) -> str:
        expires_at = int((self._now() + self.lifetime).timestamp())
        payload = f"v1.{expires_at}.{secrets.token_urlsafe(18)}"
        return f"{payload}.{self._signature(payload)}"

    def valid(self, token: str) -> bool:
        if not token or len(token) > 512 or not token.isascii():
            return False
        parts = token.split(".")
        if len(parts) != 4:
            return False
        version, expires_at, nonce, signature = parts
        try:
            expiry = int(expires_at)
        except ValueError:
            return False
        if version != "v1" or not nonce or expiry <= int(self._now().timestamp()):
            return False
        payload = f"{version}.{expires_at}.{nonce}"
        try:
            expected = self._signature(payload)
        except AdminRecordUnreadable:
            return False
        return hmac.compare_digest(signature, expected)

    def _signature(self, payload: str) -> str:
        digest = hmac.new(
            self.credentials.session_secret(), payload.encode("ascii"), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
