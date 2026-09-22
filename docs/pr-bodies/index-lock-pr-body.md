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
- **Accepted, out of scope:** the process-wide lock is held across an
  untimed git fetch (`_run` sets no `subprocess` timeout, true before this
  change too), so a genuinely hung fetch now stalls every later reconcile,
  watch, and manual digest instead of each failing fast on its own lock
  error. Fixing this needs a timeout policy of its own and is a separate
  piece of work, not folded into this fix.

## Reported, not fixed

The builder's "database is locked" crash on every pod start, believed to be
the actual source of the lock in the first place, is out of scope here and
is a separate, already-known issue.
