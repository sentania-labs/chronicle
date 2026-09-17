"""A fake `GitHubRepoOps`: in-memory state, records every call in order.

Used by the publisher, watcher, and reconciliation tests so the git data
API call sequence, PR idempotency, and merge/close handling can all be
asserted without an httpx transport or a real repository.
"""

from __future__ import annotations

from typing import Any


class FakeRepoOps:
    def __init__(self, default_branch: str = "main") -> None:
        self.default_branch = default_branch
        self.calls: list[str] = []
        self._counter = 0
        self.refs: dict[str, str] = {f"heads/{default_branch}": "base-commit-1"}
        self.commits: dict[str, dict[str, Any]] = {
            "base-commit-1": {"tree": {"sha": "base-tree-1"}}
        }
        self.trees: dict[str, list[dict[str, Any]]] = {"base-tree-1": []}
        self.blobs: dict[str, str] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self._next_pr = 1
        # Test-only: name a call that should raise instead of succeeding,
        # so a publish run failure can be simulated without a real network.
        self.fail_on: str | None = None

    def _maybe_fail(self, call: str) -> None:
        if self.fail_on == call:
            from chronicle.api.github_client import GitHubApiError

            raise GitHubApiError("simulated_failure", f"simulated failure at {call}")

    def _sha(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def get_ref(self, ref: str) -> dict[str, Any] | None:
        self.calls.append("get_ref")
        sha = self.refs.get(ref)
        return {"ref": f"refs/{ref}", "object": {"sha": sha}} if sha else None

    def create_ref(self, ref: str, sha: str) -> None:
        self.calls.append("create_ref")
        self.refs[ref] = sha

    def update_ref(self, ref: str, sha: str, force: bool = True) -> None:
        self.calls.append("update_ref")
        self.refs[ref] = sha

    def delete_ref(self, ref: str) -> None:
        self.calls.append("delete_ref")
        self.refs.pop(ref, None)

    def get_commit(self, sha: str) -> dict[str, Any]:
        self.calls.append("get_commit")
        return self.commits[sha]

    def get_contents(self, path: str, ref: str) -> dict[str, Any] | None:
        self.calls.append("get_contents")
        return None

    def create_blob(self, content_b64: str) -> str:
        self._maybe_fail("create_blob")
        self.calls.append("create_blob")
        sha = self._sha("blob")
        self.blobs[sha] = content_b64
        return sha

    def create_tree(self, base_tree: str, entries: list[dict[str, Any]]) -> str:
        self.calls.append("create_tree")
        sha = self._sha("tree")
        self.trees[sha] = entries
        return sha

    def create_commit(self, message: str, tree_sha: str, parents: list[str]) -> str:
        self.calls.append("create_commit")
        sha = self._sha("commit")
        self.commits[sha] = {"tree": {"sha": tree_sha}, "message": message, "parents": parents}
        return sha

    def create_pull(self, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        self.calls.append("create_pull")
        number = self._next_pr
        self._next_pr += 1
        pr = {
            "number": number,
            "html_url": f"https://github.com/o/r/pull/{number}",
            "title": title,
            "head": {"ref": head},
            "base": {"ref": base},
            "body": body,
            "state": "open",
            "merged": False,
        }
        self.pulls[number] = pr
        return pr

    def get_pull(self, number: int) -> dict[str, Any]:
        self.calls.append("get_pull")
        return self.pulls[number]

    def list_open_pulls_by_head(self, head: str) -> list[dict[str, Any]]:
        self.calls.append("list_open_pulls_by_head")
        return [
            pr for pr in self.pulls.values() if pr["state"] == "open" and pr["head"]["ref"] == head
        ]

    def update_pull_body(self, number: int, body: str) -> dict[str, Any]:
        self.calls.append("update_pull_body")
        self.pulls[number]["body"] = body
        return self.pulls[number]

    # Test-only helpers, not part of GitHubRepoOps.

    def merge(self, number: int) -> None:
        self.pulls[number]["state"] = "closed"
        self.pulls[number]["merged"] = True

    def close_unmerged(self, number: int) -> None:
        self.pulls[number]["state"] = "closed"
        self.pulls[number]["merged"] = False
