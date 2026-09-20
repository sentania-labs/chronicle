"""Named bearer tokens, hashed at rest, in the restricted state directory.

Tokens live in `data/state/`, which spec section 13 keeps outside `repo/` and
therefore outside git: a credential store is not domain history, and it must
never end up in a backup bundle's plaintext or a diff.

Hashing is a plain sha256 of the token bytes, not a slow KDF. See
docs/decisions/007-token-hashing-and-frontmatter-allowlist.md: these are
32-byte CSPRNG secrets, so there is no low-entropy guess for argon2 to slow
down.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import threading
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from .atomic import write_atomic
from .models import now_stamp

TOKENS_FILE_NAME = "tokens.json"
TOKENS_LOCK_FILE_NAME = "tokens.lock"
UI_TOKEN_FILE_NAME = "ui_token.txt"
UI_DISABLED_FILE_NAME = "ui_disabled"
UI_TOKEN_NAME = "ui"
# Whoever is at the keyboard behind the `ui` token. A role, not a person: this
# lands in git history, in version authors, and as a claim holder.
UI_COMMIT_AUTHOR = "editor"
# Names an admin cannot issue an ordinary consumer token under. `ui` is the
# UI backend's own token name; `editor` is the identity `commit_author` maps
# it to. An ordinary token named `editor` would write with the exact same
# version author, event actor, claim holder and git author as the UI, which
# defeats the attribution the mapping exists for.
RESERVED_TOKEN_NAMES = frozenset({UI_TOKEN_NAME, UI_COMMIT_AUTHOR})
TOKEN_BYTES = 32


class TokenRecord(BaseModel):
    name: str
    hash: str
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.path = self.state_dir / TOKENS_FILE_NAME
        self.lock_path = self.state_dir / TOKENS_LOCK_FILE_NAME
        self.ui_token_path = self.state_dir / UI_TOKEN_FILE_NAME
        self.ui_disabled_path = self.state_dir / UI_DISABLED_FILE_NAME
        # Every write here is a load-mutate-save of the whole file, including
        # the last_used_at stamp on an ordinary request. The api and the
        # `chronicle token` CLI are separate processes with separate address
        # spaces, so a thread lock alone does not stop them interleaving; the
        # flock on tokens.lock is what makes load-mutate-save atomic across
        # processes, and the thread lock still serializes threads within one
        # process before either even reaches the file lock.
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _locked_file(self) -> Iterator[None]:
        with self._lock:
            self.lock_path.touch(exist_ok=True)
            with self.lock_path.open("r+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self) -> list[TokenRecord]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [TokenRecord.model_validate(item) for item in raw["tokens"]]

    def _save(self, records: list[TokenRecord]) -> None:
        payload = {"tokens": [record.model_dump(mode="json") for record in records]}
        write_atomic(self.path, json.dumps(payload, indent=2) + "\n", mode=0o600)

    def issue(self, name: str) -> str:
        """Mint a token, store only its hash, and return the plaintext once."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with self._locked_file():
            records = self.load()
            records.append(TokenRecord(name=name, hash=hash_token(token), created_at=now_stamp()))
            self._save(records)
        return token

    def revoke(self, name: str) -> int:
        with self._locked_file():
            records = self.load()
            revoked = 0
            for record in records:
                if record.name == name and record.revoked_at is None:
                    record.revoked_at = now_stamp()
                    revoked += 1
            if revoked:
                self._save(records)
        if revoked and name == UI_TOKEN_NAME:
            # A revoke through /admin/tokens or `chronicle token revoke ui` is
            # the documented UI kill switch; without this marker,
            # `ensure_ui_token` (called on every process start) cannot tell
            # "deliberately revoked" from "bootstrap never finished" and
            # re-mints a fresh token into the same plaintext file, silently
            # re-enabling the UI on the operator's next restart. The marker
            # is the only thing that makes the revocation survive one.
            self.ui_disabled_path.touch(exist_ok=True)
            os.chmod(self.ui_disabled_path, 0o600)
        return revoked

    def authenticate(self, token: str) -> TokenRecord | None:
        digest = hash_token(token)
        with self._locked_file():
            records = self.load()
            for record in records:
                if record.revoked_at is None and hmac.compare_digest(record.hash, digest):
                    record.last_used_at = now_stamp()
                    self._save(records)
                    return record
        return None

    def _ui_plaintext_is_valid(self, record: TokenRecord) -> bool:
        if not self.ui_token_path.exists():
            return False
        plaintext = self.ui_token_path.read_text(encoding="utf-8").strip()
        return hmac.compare_digest(hash_token(plaintext), record.hash)

    def ensure_ui_token(self) -> None:
        """Mint the UI backend's token on first run and write it once, 0600.

        A record with no readable plaintext behind it is not a completed
        bootstrap: the process may have died between issuing the hash and
        writing the file, or the file may have been lost since. Either way the
        UI backend can never authenticate with it, so the old record is
        revoked and a fresh one is minted rather than left in place.

        An explicit revoke through `/admin/tokens` or `chronicle token
        revoke ui` is a different case entirely: the operator's kill switch,
        not a bootstrap failure, and `ui_disabled_path` (set by `revoke`)
        says so. This runs on every process start, so without that check a
        restart would silently re-mint and hand the UI a working token
        again, undoing the revoke the operator relied on.
        """
        if self.ui_disabled_path.exists():
            return
        records = self.load()
        record = next(
            (r for r in records if r.name == UI_TOKEN_NAME and r.revoked_at is None), None
        )
        if record is not None and self._ui_plaintext_is_valid(record):
            return
        if record is not None:
            self.revoke(UI_TOKEN_NAME)
        token = self.issue(UI_TOKEN_NAME)
        self.ui_token_path.write_text(token + "\n", encoding="utf-8")
        os.chmod(self.ui_token_path, 0o600)

    def reenable_ui_token(self) -> str:
        """Clear the kill switch `revoke(UI_TOKEN_NAME)` set and mint a fresh token.

        Deliberately not just "clear the marker and let the next
        `ensure_ui_token` handle it": that call only happens at startup, and
        an admin clicking "re-enable" wants the UI usable on this request,
        not after the next restart.
        """
        if self.ui_disabled_path.exists():
            self.ui_disabled_path.unlink()
        token = self.issue(UI_TOKEN_NAME)
        self.ui_token_path.write_text(token + "\n", encoding="utf-8")
        os.chmod(self.ui_token_path, 0o600)
        return token


def commit_author(token_name: str) -> str:
    """The git author a consumer commits as; the UI backend commits as the editor."""
    return UI_COMMIT_AUTHOR if token_name == UI_TOKEN_NAME else token_name
