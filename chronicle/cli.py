"""The `chronicle` command: operator jobs that do not need the API running.

Two of them today. `reindex` rebuilds the derived SQLite index from the files
under `data/repo/` and `data/images/` (ADR 006). `token` issues, revokes, and
lists consumer tokens; an issued token is printed once to stdout and never
logged or stored in plaintext.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .api.main import DATA_DIR_ENV
from .api.store import Store
from .api.tokens import TokenStore


def _data_dir(args: argparse.Namespace) -> Path:
    path = args.data_dir or os.environ.get(DATA_DIR_ENV)
    if not path:
        raise SystemExit(f"pass --data-dir or set {DATA_DIR_ENV}")
    return Path(path)


def _reindex(args: argparse.Namespace) -> int:
    store = Store.open(_data_dir(args))
    counts = store.reindex()
    store.close()
    for name, count in counts.items():
        print(f"{name}: {count}")
    return 0


def _token(args: argparse.Namespace) -> int:
    tokens = TokenStore(_data_dir(args) / "state")
    if args.token_command == "issue":
        print(tokens.issue(args.name))
        return 0
    if args.token_command == "revoke":
        revoked = tokens.revoke(args.name)
        print(f"revoked {revoked} token(s) named {args.name}")
        return 0 if revoked else 1
    for record in tokens.load():
        print(
            f"{record.name}\tcreated {record.created_at}"
            f"\tlast used {record.last_used_at or 'never'}"
            f"\trevoked {record.revoked_at or 'no'}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chronicle", description="Chronicle operator commands")
    parser.add_argument("--data-dir", help=f"the data directory (default: ${DATA_DIR_ENV})")
    commands = parser.add_subparsers(dest="command", required=True)

    reindex = commands.add_parser("reindex", help="rebuild the derived index from the files")
    reindex.set_defaults(handler=_reindex)

    token = commands.add_parser("token", help="issue, revoke, and list consumer tokens")
    token.set_defaults(handler=_token)
    token_commands = token.add_subparsers(dest="token_command", required=True)
    issue = token_commands.add_parser("issue", help="mint a token and print it once")
    issue.add_argument("name")
    revoke = token_commands.add_parser("revoke", help="revoke every token with this name")
    revoke.add_argument("name")
    token_commands.add_parser("list", help="list token names and use, never secrets")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: object = args.handler
    assert callable(handler)
    exit_code: int = handler(args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
