Closes #57

## What changes for someone running it

The lab instance's hourly reconcile and the watcher's post-merge reconcile
have been failing every run since 3:18 PM on Sept 21 with
`git reset --hard origin/main returned non-zero exit status 128`, because a
git step killed mid-write (most likely the builder's own sqlite "database is
locked" crash loop) left an empty `.git/index.lock` behind, and nothing ever
cleared it. After this change, the first reconcile or watch cycle after
deploy clears that lock on its own: one warning log line naming the lock
path and its age, then the sync proceeds normally. No manual step on the
host, no restart needed.

Every git call against `data/site/` (reconcile's fetch, the watcher's
post-merge fetch, the admin "run digest now" button) now runs behind one
in-process lock, so two of those can never race on the same clone again.
A lock is only cleared if it is at least five minutes old; a younger one
fails with a clear error naming the path and its age instead, on the theory
that something might still be using it. Every git failure through this path
now logs git's own stderr (with any token-bearing text redacted), not just
the exit code, so a future failure here is diagnosable from the log alone.
Every git call also now times out after five minutes rather than running
forever, so one hung fetch can no longer hold the new in-process lock
indefinitely and stall every later reconcile, watch, and admin digest
behind it; a timed-out call fails with a clear error naming the git
subcommand, and the same stale-lock clearing above cleans up whatever the
killed process left behind.

## Blast radius

`chronicle/api/digest.py` only: the one function every reconcile, watch, and
manual digest already funnels through. No change to what gets fetched,
parsed, or written; no change to any HTTP route's behavior; no schema or
data-directory layout change. The only new failure mode is an error naming
a lock file that is less than five minutes old, which did not exist as a
condition before (previously that same young lock would have failed the
git command itself with a less specific message).

## Recovery

Revert this PR. Nothing here is a one-way migration: the lock-clearing
behavior is additive to `clone_or_update`, and reverting returns to the
exact prior code path (which still runs correctly against a clone with no
stale lock).

## What was checked before this went up

Reviewed what else could run git against `data/site/`: the builder never
does (`chronicle/builder/hugo.py` never sets `--enableGitInfo`, and
`chronicle/builder/runner.py` copies the site with hardlinks rather than a
git operation), and neither `docker-compose.yml` nor `examples/k8s/` run a
second container against this path. The api process is the only place git
touches this directory, which is what makes the in-process lock sufficient
rather than needing a filesystem-level lock.

## Adversarial review findings and disposition

Full review in `docs/pr-bodies/index-lock-review.md`. Summary:

- **Fixed:** `reconcile.run_once_logged` did not catch `digest.DigestError`
  (the refusal on a too-young lock), so that case would have logged as an
  "unexpected failure" instead of the normal retry path. Added to the
  except tuple, covered by a new test.
- **Checked, no change needed:** a symlinked lock path (uses `lstat`, never
  follows it), clock skew making a lock's age look negative (only makes the
  code more conservative, never less), and the stderr scrubber against both
  a credential-bearing URL and an `AUTHORIZATION` header line.
- **Fixed:** the process-wide lock was held across an untimed git fetch
  (`_run` set no `subprocess` timeout, true before this change too), so a
  genuinely hung fetch would have stalled every later reconcile, watch, and
  manual digest instead of each failing fast on its own lock error. `_run`
  now times out every git call after five minutes and raises `DigestError`
  on a timeout, covered by a new test.

## Reported, not fixed

The builder's "database is locked" crash on every pod start, believed to be
the actual source of the lock in the first place, is out of scope here and
is a separate, already-known issue.

## Codex round

Codex filed one finding (P2, `chronicle/api/digest.py:595`): the shared
site-clone lock covered `clone_or_update` alone, not the reads that follow
it. When two refreshes overlap, a second caller's clone could land between
the first caller's clone and its own read of the tree, pairing one
commit's blob shas with another commit's file content in the record
Chronicle persists.

Fixed by making `_SITE_GIT_LOCK` reentrant and holding it across the whole
clone-through-read/apply span in `digest_runner.py`'s `refresh_from_target`
and `run`, and in `reconcile.py`'s `run` (through its own
`read_hugo_conventions`/`discover_posts` and the `_content_drift` loop).
Full writeup and the fix commit in `docs/pr-bodies/index-lock-review.md`'s
Codex round section. Covered by a new test in `tests/test_digest.py`,
confirmed failing against the pre-fix commit first.

Out of scope: the builder's hardlink copy of `data/site/` and the image
routes never touch `data/site/` directly, so this lock does not reach
them.
