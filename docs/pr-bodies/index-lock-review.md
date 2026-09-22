# Adversarial review: stale index.lock and stderr logging (issue #57)

Blind pass against the full diff (`git diff origin/main...HEAD`), read as a
reviewer whose job is to break it. Findings and disposition below.

## Findings

### 1. `DigestError` was not caught by `reconcile.run_once_logged` (fixed)

`clone_or_update` raises `digest.DigestError` when it refuses a lock that is
not yet older than `STALE_GIT_LOCK_SECONDS`. `reconcile.run_once_logged`'s
except tuple was `(OSError, subprocess.CalledProcessError, GitHubApiError)`,
which does not include `DigestError`. A refused fresh lock would have
propagated past `run_once_logged` into `_run_once_never_raises`'s broad
`except Exception`, logging "reconcile: unexpected failure" instead of the
normal "reconcile: run failed, will retry next trigger" path. Functionally
harmless (the loop still survives and retries either way), but it misreports
an expected, self-recovering condition as a bug needing investigation.

Fixed by adding `digest_mod.DigestError` to the except tuple in
`chronicle/api/reconcile.py`. Covered by
`test_run_once_logged_treats_a_refused_stale_lock_as_an_expected_failure` in
`tests/test_reconcile.py`.

### 2. A live git process outside this container could still hold a lock past the threshold (accepted risk, documented)

The staleness check is `age >= STALE_GIT_LOCK_SECONDS`, not "definitely no
process holds this." Checked what else could run git against `data/site/`:
the builder never shells out to git against it (`chronicle/builder/hugo.py`
pins `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` to `/dev/null` and never sets
`--enableGitInfo`; `chronicle/builder/runner.py` copies the site tree with
hardlinks, no git call), and neither `docker-compose.yml` nor
`examples/k8s/` run git against this path from any other container. Inside
the api process, `_SITE_GIT_LOCK` serializes every call, so a lock this
process is holding is provably still fresh when checked. The only remaining
way to hit this is someone running git by hand inside the api container
against `data/site/`, which AGENTS.md's "no manual infra changes" rule
already rules out. Left as designed; noted in the PR body.

### 3. The process-wide lock is held across an untimed network fetch (noted, out of scope)

`_run`'s `subprocess.run` call has no `timeout`, and this was true before
this change. Before this PR, one thread hanging on a fetch would still hold
`.git/index.lock` for that duration, and any concurrent caller would fail
fast with a lock error rather than block. After this PR, a concurrent caller
blocks on `_SITE_GIT_LOCK` instead, so a genuinely hung fetch now stalls
every future reconcile, watch, and manual digest indefinitely instead of
each failing fast and retrying on its own schedule. This is a real behavior
change, not just a latent pre-existing gap, but adding a timeout is a
separate piece of work (choosing a sane bound, deciding what happens to a
half-finished reset) and is out of scope for this fix. Noted as a one-line
item in the PR body, not fixed here.

### 4. Symlinked lock path (checked, no change needed)

`_clear_stale_git_locks` uses `Path.lstat`, not `stat`, so a lock path that
is itself a symlink is judged and removed by its own age and identity, never
by following it into wherever it points. `Path.unlink()` on a symlink
removes the symlink, not its target. Confirmed safe as written.

### 5. Clock skew making age negative (checked, no change needed)

`age_seconds = time.time() - info.st_mtime` can go negative if the system
clock moves backward after the lock is created. The check is
`age_seconds < STALE_GIT_LOCK_SECONDS`, which is true for any negative age,
so skew can only make the code refuse to remove a lock it otherwise would
have, never the other way around. Safe by construction.

### 6. Scrubber coverage (checked against the three shapes named in the ask)

Verified against `test_git_command_error_scrubs_userinfo_url_and_authorization_header`:
a `https://x-access-token:SECRET@host/...` URL and an
`AUTHORIZATION: basic ...` line are both redacted. The `x-access-token`
form is caught twice over: once by the userinfo-URL pattern, once by its own
pattern for a bare `x-access-token:...` outside a URL. No case where a
token from `_auth_env`'s two injection points (`http.extraheader` value, or
a URL) survives scrubbing in the shapes git would actually emit them.

### 7. `GitCommandError` still satisfies existing `except subprocess.CalledProcessError` handlers (checked, no change needed)

`GitCommandError.__init__` calls `super().__init__(returncode, cmd, output,
stderr)`, the same four positional arguments `CalledProcessError` itself
takes, so every attribute (`.returncode`, `.cmd`, `.output`, `.stderr`)
existing handlers in `admin_status.py`, `main.py`, `reconcile.py`, and
`digest.py`'s own `_submodule_commits` read is present and correct. Grepped
the whole tree for `CalledProcessError`; no site does an exact
`type(exc) is CalledProcessError` check that a subclass would fail.

## Disposition summary

One valid finding (#1), fixed and covered by a new test. Six other angles
checked and found already safe by construction, with #3 recorded as a
known, accepted, out-of-scope tradeoff rather than a defect.
